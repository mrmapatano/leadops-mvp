from __future__ import annotations

import html
import re
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd

from .db import audit, get_connection

ALIASES = {
    "business_name": {"business_name", "company", "company_name", "name", "business"},
    "phone": {"phone", "mobile", "mobile_phone", "phone_number", "number"},
    "first_name": {"first_name", "owner_first_name"},
    "last_name": {"last_name", "owner_last_name"},
    "city": {"city"},
    "state": {"state"},
    "vertical": {"vertical", "trade", "industry"},
    "website": {"website"},
    "address": {"address"},
    "source": {"source"},
    "contact_external_id": {"contact_id"},
    "student_id": {"student_id"},
    "message": {"ai_message", "message", "sms", "text", "personalized_message"},
}


def clean_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = html.unescape(str(value))
    text = unicodedata.normalize("NFKC", text)
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
        "\ufeff": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_header(name: Any) -> str:
    value = clean_text(name).lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def normalize_phone(value: Any) -> tuple[str, bool]:
    digits = re.sub(r"\D+", "", clean_text(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits, len(digits) == 10


def business_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def read_any_table(path: str | Path, default_text_column: str = "phone") -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
    elif suffix == ".txt":
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return pd.DataFrame(columns=[default_text_column])
        delimiter = None
        for candidate in [",", "\t", "|", ";"]:
            if candidate in lines[0]:
                delimiter = candidate
                break
        if delimiter:
            df = pd.read_csv(path, sep=delimiter, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        else:
            df = pd.DataFrame({default_text_column: lines})
    else:
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="cp1252")
    df.columns = [clean_text(col) for col in df.columns]
    return df.fillna("")


def detect_column(df: pd.DataFrame, alias_key: str) -> str | None:
    aliases = ALIASES[alias_key]
    normalized = {normalize_header(column): column for column in df.columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def row_value(row: pd.Series, column: str | None) -> str:
    if not column:
        return ""
    return clean_text(row.get(column, ""))


def create_batch(conn, import_type: str, path: Path, total_rows: int) -> int:
    cursor = conn.execute(
        "INSERT INTO batches (import_type, source_file, total_rows) VALUES (?, ?, ?)",
        (import_type, path.name, total_rows),
    )
    return int(cursor.lastrowid)


def update_batch(conn, batch_id: int, **values: int | str) -> None:
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn.execute(
        f"UPDATE batches SET {assignments} WHERE id = ?",
        [*values.values(), batch_id],
    )


def import_suppression(path: str | Path, actor_role: str = "system", actor_id: int | None = None) -> dict:
    path = Path(path)
    df = read_any_table(path, default_text_column="phone")
    phone_col = detect_column(df, "phone") or (df.columns[0] if len(df.columns) else None)
    accepted = 0
    invalid = 0
    normalized_seen: set[str] = set()

    with get_connection() as conn:
        batch_id = create_batch(conn, "suppression", path, len(df))
        for index, row in df.iterrows():
            phone_original = row_value(row, phone_col)
            normalized, valid = normalize_phone(phone_original)
            if not valid:
                invalid += 1
                continue
            normalized_seen.add(normalized)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO suppression_numbers
                    (phone_original, phone_normalized, source_file, source_row, batch_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (phone_original, normalized, path.name, int(index) + 2, batch_id),
            )
            if cursor.rowcount:
                accepted += 1

        if normalized_seen:
            placeholders = ",".join("?" for _ in normalized_seen)
            conn.execute(
                f"""
                UPDATE contacts
                SET status = 'blocked',
                    block_reason = 'suppressed',
                    updated_at = CURRENT_TIMESTAMP
                WHERE phone_normalized IN ({placeholders})
                  AND status IN ('pending', 'assigned')
                """,
                tuple(normalized_seen),
            )
        update_batch(
            conn,
            batch_id,
            accepted_rows=accepted,
            blocked_invalid=invalid,
            notes=f"{accepted} suppression number(s) added or already present",
        )
        audit(conn, "import_suppression", "batch", batch_id, path.name, actor_role, actor_id)
        conn.commit()
    return {
        "batch_id": batch_id,
        "accepted": accepted,
        "invalid": invalid,
        "summary": f"{accepted} valid suppression number(s), {invalid} invalid",
    }


def import_contacts(path: str | Path, actor_role: str = "system", actor_id: int | None = None) -> dict:
    path = Path(path)
    df = read_any_table(path, default_text_column="phone")
    columns = {key: detect_column(df, key) for key in ALIASES if key != "message"}
    accepted = invalid = suppressed = duplicate = 0

    with get_connection() as conn:
        batch_id = create_batch(conn, "contacts", path, len(df))
        suppression_numbers = {
            row["phone_normalized"]
            for row in conn.execute("SELECT phone_normalized FROM suppression_numbers")
        }
        seen_numbers = {
            row["phone_normalized"]
            for row in conn.execute(
                "SELECT phone_normalized FROM contacts WHERE phone_normalized IS NOT NULL AND phone_normalized <> ''"
            )
        }

        for index, row in df.iterrows():
            phone_original = row_value(row, columns["phone"])
            normalized, valid = normalize_phone(phone_original)
            status = "pending"
            block_reason = None

            if not valid:
                status = "blocked"
                block_reason = "invalid_phone"
                invalid += 1
            elif normalized in suppression_numbers:
                status = "blocked"
                block_reason = "suppressed"
                suppressed += 1
            elif normalized in seen_numbers:
                status = "blocked"
                block_reason = "duplicate"
                duplicate += 1
            else:
                accepted += 1
                seen_numbers.add(normalized)

            conn.execute(
                """
                INSERT INTO contacts (
                    contact_external_id, business_name, first_name, last_name,
                    phone_original, phone_normalized, city, state, vertical,
                    website, address, source, source_file, source_row, batch_id,
                    status, block_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_value(row, columns["contact_external_id"]),
                    row_value(row, columns["business_name"]),
                    row_value(row, columns["first_name"]),
                    row_value(row, columns["last_name"]),
                    phone_original,
                    normalized if valid else "",
                    row_value(row, columns["city"]),
                    row_value(row, columns["state"]),
                    row_value(row, columns["vertical"]),
                    row_value(row, columns["website"]),
                    row_value(row, columns["address"]),
                    row_value(row, columns["source"]),
                    path.name,
                    int(index) + 2,
                    batch_id,
                    status,
                    block_reason,
                ),
            )

        update_batch(
            conn,
            batch_id,
            accepted_rows=accepted,
            blocked_invalid=invalid,
            blocked_suppressed=suppressed,
            blocked_duplicate=duplicate,
        )
        audit(
            conn,
            "import_contacts",
            "batch",
            batch_id,
            f"accepted={accepted}, invalid={invalid}, suppressed={suppressed}, duplicate={duplicate}",
            actor_role,
            actor_id,
        )
        conn.commit()
    return {
        "batch_id": batch_id,
        "accepted": accepted,
        "invalid": invalid,
        "suppressed": suppressed,
        "duplicate": duplicate,
        "summary": (
            f"{accepted} accepted, {invalid} invalid, "
            f"{suppressed} suppressed, {duplicate} duplicate"
        ),
    }


def contact_by_internal_or_external_id(conn, value: str):
    cleaned = clean_text(value)
    if not cleaned:
        return None
    if cleaned.isdigit():
        row = conn.execute(
            "SELECT * FROM contacts WHERE id = ? AND status <> 'blocked'",
            (int(cleaned),),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM contacts WHERE contact_external_id = ? AND status <> 'blocked'",
        (cleaned,),
    ).fetchone()


def contact_by_phone(conn, normalized: str):
    if not normalized:
        return None
    return conn.execute(
        """
        SELECT * FROM contacts
        WHERE phone_normalized = ? AND status <> 'blocked'
        ORDER BY id
        LIMIT 1
        """,
        (normalized,),
    ).fetchone()


def build_business_lookup(conn) -> dict[str, Any]:
    lookup: dict[str, list[Any]] = {}
    for row in conn.execute("SELECT * FROM contacts WHERE status <> 'blocked'"):
        key = business_key(row["business_name"])
        if key:
            lookup.setdefault(key, []).append(row)
    return {key: rows[0] for key, rows in lookup.items() if len(rows) == 1}


def upsert_message(conn, contact_id: int, message: str, path: Path, source_row: int, batch_id: int) -> int:
    existing = conn.execute(
        """
        SELECT * FROM messages
        WHERE contact_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (contact_id,),
    ).fetchone()
    contact = conn.execute("SELECT status FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    status = "blocked" if contact and contact["status"] == "blocked" else "pending"
    if existing and existing["status"] not in {"approved", "exported"}:
        conn.execute(
            """
            UPDATE messages
            SET original_ai_message = ?,
                current_message = ?,
                status = ?,
                source_file = ?,
                source_row = ?,
                batch_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (message, message, status, path.name, source_row, batch_id, existing["id"]),
        )
        return int(existing["id"])
    cursor = conn.execute(
        """
        INSERT INTO messages (
            contact_id, original_ai_message, current_message, status,
            source_file, source_row, batch_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (contact_id, message, message, status, path.name, source_row, batch_id),
    )
    return int(cursor.lastrowid)


def import_ai_messages(path: str | Path, actor_role: str = "system", actor_id: int | None = None) -> dict:
    path = Path(path)
    df = read_any_table(path, default_text_column="message")
    phone_col = detect_column(df, "phone")
    business_col = detect_column(df, "business_name")
    contact_id_col = detect_column(df, "contact_external_id")
    message_col = detect_column(df, "message")
    if not message_col and len(df.columns) >= 5:
        message_col = df.columns[4]

    matched = created = skipped_empty = 0
    with get_connection() as conn:
        batch_id = create_batch(conn, "ai_messages", path, len(df))
        business_lookup = build_business_lookup(conn)
        for index, row in df.iterrows():
            source_row = int(index) + 2
            message = clean_text(row_value(row, message_col))
            if not message:
                skipped_empty += 1
                continue

            contact = None
            if phone_col:
                normalized, valid = normalize_phone(row_value(row, phone_col))
                if valid:
                    contact = contact_by_phone(conn, normalized)
            if not contact and contact_id_col:
                contact = contact_by_internal_or_external_id(conn, row_value(row, contact_id_col))
            if not contact and business_col:
                contact = business_lookup.get(business_key(row_value(row, business_col)))

            if contact:
                upsert_message(conn, int(contact["id"]), message, path, source_row, batch_id)
                matched += 1
                created += 1

        update_batch(
            conn,
            batch_id,
            matched_rows=matched,
            created_messages=created,
            notes=f"{skipped_empty} empty message row(s) skipped",
        )
        audit(
            conn,
            "import_ai_messages",
            "batch",
            batch_id,
            f"matched={matched}, skipped_empty={skipped_empty}",
            actor_role,
            actor_id,
        )
        conn.commit()
    return {
        "batch_id": batch_id,
        "matched": matched,
        "created_messages": created,
        "skipped_empty": skipped_empty,
        "summary": f"{matched} matched message row(s), {skipped_empty} empty skipped",
    }
