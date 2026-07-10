from __future__ import annotations

import csv
import os
from pathlib import Path

from .db import audit, get_connection
from .importers import clean_text, normalize_phone

OUTCOME_CHOICES = [
    "",
    "replied",
    "not_interested",
    "wrong_number",
    "hot",
    "meeting_requested",
    "booked",
    "follow_up_needed",
]

MEETING_CHOICES = {"", "meeting_requested", "booked", "follow_up_needed"}

# Editable contact fields exposed in the admin contact-edit screen.
EDITABLE_CONTACT_FIELDS = [
    "business_name",
    "first_name",
    "last_name",
    "city",
    "state",
    "vertical",
    "website",
    "address",
]

# Latest message per contact, used to keep admin listings one-row-per-contact
# even when a contact accumulates multiple message rows over re-imports.
LATEST_MESSAGE_JOIN = (
    "LEFT JOIN messages m ON m.id = "
    "(SELECT mm.id FROM messages mm WHERE mm.contact_id = c.id ORDER BY mm.id DESC LIMIT 1)"
)


def payout_rates() -> dict[str, float]:
    return {
        "approved": float(os.environ.get("PAYOUT_APPROVED_MESSAGE", "0.25")),
        "hot": float(os.environ.get("PAYOUT_HOT_REPLY", "3.00")),
        "booked": float(os.environ.get("PAYOUT_BOOKED_MEETING", "10.00")),
    }


def get_students(active_only: bool = False) -> list[dict]:
    with get_connection() as conn:
        sql = "SELECT * FROM students"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY name"
        return [dict(row) for row in conn.execute(sql)]


def scalar(conn, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def dashboard_context() -> dict:
    with get_connection() as conn:
        metrics = {
            "total_contacts": scalar(conn, "SELECT COUNT(*) FROM contacts"),
            "pending": scalar(conn, "SELECT COUNT(*) FROM contacts WHERE status = 'pending'"),
            "pending_with_messages": scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM contacts c
                JOIN messages m ON m.contact_id = c.id
                WHERE c.status = 'pending' AND m.status = 'pending'
                """,
            ),
            "assigned": scalar(conn, "SELECT COUNT(*) FROM contacts WHERE status = 'assigned'"),
            "approved": scalar(conn, "SELECT COUNT(*) FROM messages WHERE status IN ('approved', 'exported')"),
            "skipped": scalar(conn, "SELECT COUNT(*) FROM messages WHERE status = 'skipped'"),
            "blocked": scalar(conn, "SELECT COUNT(*) FROM contacts WHERE status = 'blocked'"),
            "sent_ready": scalar(conn, "SELECT COUNT(*) FROM messages WHERE status = 'approved' AND exported_at IS NULL"),
            "exported": scalar(conn, "SELECT COUNT(*) FROM messages WHERE exported_at IS NOT NULL"),
            "hot": scalar(conn, "SELECT COUNT(*) FROM contacts WHERE hot = 1"),
            "meeting_requested": scalar(
                conn,
                "SELECT COUNT(*) FROM contacts WHERE meeting_status = 'meeting_requested'",
            ),
            "booked": scalar(conn, "SELECT COUNT(*) FROM contacts WHERE meeting_status = 'booked'"),
            "follow_up": scalar(
                conn,
                "SELECT COUNT(*) FROM contacts WHERE reply_status = 'follow_up_needed' OR meeting_status = 'follow_up_needed'",
            ),
            "wrong_number": scalar(conn, "SELECT COUNT(*) FROM contacts WHERE reply_status = 'wrong_number'"),
            "replies": scalar(conn, "SELECT COUNT(*) FROM contacts WHERE COALESCE(reply_status, '') <> ''"),
        }
        batches = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM batches ORDER BY imported_at DESC, id DESC LIMIT 8"
            )
        ]
        hot_leads = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT c.*, m.id AS message_id, m.current_message, s.name AS student_name
                FROM contacts c
                {LATEST_MESSAGE_JOIN}
                LEFT JOIN students s ON s.id = c.assigned_student_id
                WHERE c.hot = 1
                ORDER BY c.updated_at DESC, c.id DESC
                LIMIT 12
                """
            )
        ]
        return {
            "metrics": metrics,
            "batches": batches,
            "hot_leads": hot_leads,
            "leaderboard": leaderboard_rows(conn),
            "rates": payout_rates(),
        }


def _contact_filter_sql(filters: dict) -> tuple[str, list]:
    clauses: list[str] = []
    params: list[str | int] = []
    q = clean_text(filters.get("q"))
    if q:
        clauses.append(
            "(c.business_name LIKE ? OR c.phone_original LIKE ? OR c.phone_normalized LIKE ? OR c.city LIKE ? OR c.vertical LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    if filters.get("status"):
        clauses.append("c.status = ?")
        params.append(filters["status"])
    if filters.get("student_id"):
        clauses.append("c.assigned_student_id = ?")
        params.append(int(filters["student_id"]))
    if filters.get("block_reason"):
        clauses.append("c.block_reason = ?")
        params.append(filters["block_reason"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def get_contact_rows(filters: dict, page: int = 1, page_size: int = 50) -> dict:
    """Return a paginated page of contacts plus pagination metadata.

    One row per contact (latest message joined), so contacts never duplicate
    in the table when they carry more than one message row.
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), 500))
    where, params = _contact_filter_sql(filters)
    offset = (page - 1) * page_size
    with get_connection() as conn:
        total = scalar(conn, f"SELECT COUNT(*) FROM contacts c {where}", tuple(params))
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT c.*, s.name AS student_name, m.id AS message_id,
                       m.status AS message_status, m.current_message
                FROM contacts c
                LEFT JOIN students s ON s.id = c.assigned_student_id
                {LATEST_MESSAGE_JOIN}
                {where}
                ORDER BY c.id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, page_size, offset),
            )
        ]
    pages = max(1, (total + page_size - 1) // page_size)
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
    }


def get_contact(contact_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return dict(row) if row else None


def _block_status_for(conn, normalized: str, valid: bool, exclude_id: int) -> tuple[str, str | None]:
    """Compute (status, block_reason) for a phone number, excluding one contact."""
    if not valid:
        return "blocked", "invalid_phone"
    if conn.execute(
        "SELECT 1 FROM suppression_numbers WHERE phone_normalized = ?", (normalized,)
    ).fetchone():
        return "blocked", "suppressed"
    if conn.execute(
        "SELECT 1 FROM contacts WHERE phone_normalized = ? AND id <> ? AND status <> 'blocked' LIMIT 1",
        (normalized, exclude_id),
    ).fetchone():
        return "blocked", "duplicate"
    return "pending", None


def edit_contact(contact_id: int, fields: dict, actor_role: str = "admin", actor_id: int | None = None) -> str:
    """Edit an existing contact and re-evaluate its block status.

    Fixing a typo'd phone on a blocked contact (invalid/duplicate/suppressed)
    re-validates it; if it now passes, the contact returns to the assignable
    pool and any blocked message attached to it is reopened.
    """
    with get_connection() as conn:
        contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not contact:
            raise ValueError("Contact not found.")

        values = {key: clean_text(fields.get(key, contact[key])) for key in EDITABLE_CONTACT_FIELDS}
        phone_in = clean_text(fields.get("phone", contact["phone_original"]))
        normalized, valid = normalize_phone(phone_in)

        status = contact["status"]
        block_reason = contact["block_reason"]
        skip_reason = contact["skip_reason"]
        normalized_value = normalized if valid else ""

        # Only re-derive block state for non-terminal workflow states. Contacts
        # already assigned/approved/exported keep their workflow status.
        if status in ("pending", "blocked", "skipped"):
            status, block_reason = _block_status_for(conn, normalized, valid, contact_id)
            if status != "skipped":
                skip_reason = None

        conn.execute(
            """
            UPDATE contacts
            SET business_name = ?, first_name = ?, last_name = ?, city = ?, state = ?,
                vertical = ?, website = ?, address = ?, phone_original = ?,
                phone_normalized = ?, status = ?, block_reason = ?, skip_reason = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                values["business_name"], values["first_name"], values["last_name"],
                values["city"], values["state"], values["vertical"], values["website"],
                values["address"], phone_in, normalized_value, status, block_reason,
                skip_reason, contact_id,
            ),
        )

        # Keep message availability in sync with the contact's new status.
        if status == "blocked":
            conn.execute(
                "UPDATE messages SET status = 'blocked', updated_at = CURRENT_TIMESTAMP "
                "WHERE contact_id = ? AND status NOT IN ('approved', 'exported')",
                (contact_id,),
            )
        elif status == "pending":
            conn.execute(
                "UPDATE messages SET status = 'pending', assigned_student_id = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE contact_id = ? AND status = 'blocked'",
                (contact_id,),
            )

        audit(conn, "edit_contact", "contact", contact_id, f"status={status}", actor_role, actor_id)
        conn.commit()
    return status


def assign_next(student_id: int, count: int, actor_role: str = "system") -> list[dict]:
    with get_connection() as conn:
        student = conn.execute("SELECT * FROM students WHERE id = ? AND active = 1", (student_id,)).fetchone()
        if not student:
            raise ValueError("Student not found.")
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT c.id AS contact_id, m.id AS message_id, c.business_name, c.phone_normalized
                FROM contacts c
                JOIN messages m ON m.contact_id = c.id
                WHERE c.status = 'pending'
                  AND c.block_reason IS NULL
                  AND m.status = 'pending'
                  AND COALESCE(m.current_message, '') <> ''
                ORDER BY c.id
                LIMIT ?
                """,
                (count,),
            )
        ]
        for row in rows:
            conn.execute(
                """
                UPDATE contacts
                SET status = 'assigned',
                    assigned_student_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (student_id, row["contact_id"]),
            )
            conn.execute(
                """
                UPDATE messages
                SET status = 'assigned',
                    assigned_student_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (student_id, row["message_id"]),
            )
            ensure_outcome(conn, row["contact_id"], row["message_id"], student_id)
        audit(conn, "assign_leads", "student", student_id, f"assigned={len(rows)}", actor_role)
        conn.commit()
        return rows


def get_student_queue(student_id: int) -> list[dict]:
    with get_connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT c.*, m.id AS message_id, m.original_ai_message, m.current_message,
                       m.status AS message_status, o.reply_status AS outcome_reply_status,
                       o.hot AS outcome_hot, o.meeting_status AS outcome_meeting_status,
                       o.notes AS outcome_notes
                FROM contacts c
                JOIN messages m ON m.contact_id = c.id
                LEFT JOIN outcomes o ON o.contact_id = c.id AND o.message_id = m.id
                WHERE c.assigned_student_id = ?
                  AND m.assigned_student_id = ?
                  AND m.status = 'assigned'
                ORDER BY c.id
                """,
                (student_id, student_id),
            )
        ]


def validate_message_text(text: str) -> str:
    cleaned = clean_text(text)
    if not cleaned:
        raise ValueError("Message cannot be empty.")
    if len(cleaned) > 320:
        raise ValueError("Message is over 320 characters. Shorten it before approval.")
    return cleaned


def approve_message(
    message_id: int,
    student_id: int,
    current_message: str,
    actor_role: str = "system",
) -> None:
    cleaned = validate_message_text(current_message)
    with get_connection() as conn:
        message = conn.execute(
            """
            SELECT m.*, c.id AS contact_id
            FROM messages m
            JOIN contacts c ON c.id = m.contact_id
            WHERE m.id = ? AND m.assigned_student_id = ? AND m.status = 'assigned'
            """,
            (message_id, student_id),
        ).fetchone()
        if not message:
            raise ValueError("Assigned message not found.")
        conn.execute(
            """
            UPDATE messages
            SET current_message = ?,
                status = 'approved',
                approved_by = ?,
                approved_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (cleaned, student_id, message_id),
        )
        conn.execute(
            """
            UPDATE contacts
            SET status = 'approved',
                assigned_student_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (student_id, message["contact_id"]),
        )
        ensure_outcome(conn, message["contact_id"], message_id, student_id)
        recalculate_contact_credit(conn, message["contact_id"])
        audit(conn, "approve_message", "message", message_id, None, actor_role, student_id)
        conn.commit()


def skip_message(message_id: int, student_id: int, reason: str, actor_role: str = "system") -> None:
    cleaned_reason = clean_text(reason)
    if not cleaned_reason:
        raise ValueError("Add a skip reason.")
    with get_connection() as conn:
        message = conn.execute(
            """
            SELECT m.*, c.id AS contact_id
            FROM messages m
            JOIN contacts c ON c.id = m.contact_id
            WHERE m.id = ? AND m.assigned_student_id = ? AND m.status = 'assigned'
            """,
            (message_id, student_id),
        ).fetchone()
        if not message:
            raise ValueError("Assigned message not found.")
        conn.execute(
            "UPDATE messages SET status = 'skipped', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (message_id,),
        )
        conn.execute(
            """
            UPDATE contacts
            SET status = 'skipped',
                skip_reason = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (cleaned_reason, message["contact_id"]),
        )
        ensure_outcome(conn, message["contact_id"], message_id, student_id, notes=cleaned_reason)
        recalculate_contact_credit(conn, message["contact_id"])
        audit(conn, "skip_message", "message", message_id, cleaned_reason, actor_role, student_id)
        conn.commit()


def ensure_outcome(
    conn,
    contact_id: int,
    message_id: int | None,
    student_id: int | None,
    reply_status: str = "",
    hot: bool = False,
    meeting_status: str = "",
    notes: str = "",
) -> int:
    existing = conn.execute(
        """
        SELECT id FROM outcomes
        WHERE contact_id = ? AND COALESCE(message_id, 0) = COALESCE(?, 0)
        """,
        (contact_id, message_id),
    ).fetchone()
    if existing:
        if any([reply_status, hot, meeting_status, notes, student_id]):
            conn.execute(
                """
                UPDATE outcomes
                SET student_id = COALESCE(?, student_id),
                    reply_status = COALESCE(NULLIF(?, ''), reply_status),
                    hot = CASE WHEN ? THEN 1 ELSE hot END,
                    meeting_status = COALESCE(NULLIF(?, ''), meeting_status),
                    notes = COALESCE(NULLIF(?, ''), notes),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (student_id, reply_status, int(hot), meeting_status, notes, existing["id"]),
            )
        return int(existing["id"])
    cursor = conn.execute(
        """
        INSERT INTO outcomes
            (contact_id, message_id, student_id, reply_status, hot, meeting_status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (contact_id, message_id, student_id, reply_status, int(hot), meeting_status, notes),
    )
    return int(cursor.lastrowid)


def update_outcome(
    contact_id: int | None,
    message_id: int | None,
    student_id: int | None,
    reply_status: str = "",
    meeting_status: str = "",
    hot: bool = False,
    notes: str = "",
    actor_role: str = "system",
    actor_id: int | None = None,
) -> None:
    if not contact_id:
        raise ValueError("Contact is required.")
    reply_status = clean_text(reply_status)
    meeting_status = clean_text(meeting_status)
    notes = clean_text(notes)
    if reply_status not in OUTCOME_CHOICES:
        raise ValueError("Unknown outcome.")
    if reply_status == "hot":
        hot = True
    if reply_status == "meeting_requested" and not meeting_status:
        meeting_status = "meeting_requested"
    if reply_status == "booked":
        meeting_status = "booked"
    if reply_status == "follow_up_needed" and not meeting_status:
        meeting_status = "follow_up_needed"
    if meeting_status not in MEETING_CHOICES:
        raise ValueError("Unknown meeting status.")

    with get_connection() as conn:
        contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not contact:
            raise ValueError("Contact not found.")
        if not student_id:
            student_id = contact["assigned_student_id"]
        if not message_id:
            message = conn.execute(
                "SELECT id FROM messages WHERE contact_id = ? ORDER BY id DESC LIMIT 1",
                (contact_id,),
            ).fetchone()
            message_id = message["id"] if message else None
        ensure_outcome(conn, contact_id, message_id, student_id, reply_status, hot, meeting_status, notes)
        conn.execute(
            """
            UPDATE contacts
            SET reply_status = COALESCE(NULLIF(?, ''), reply_status),
                hot = CASE WHEN ? THEN 1 ELSE hot END,
                meeting_status = COALESCE(NULLIF(?, ''), meeting_status),
                notes = COALESCE(NULLIF(?, ''), notes),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (reply_status, int(hot), meeting_status, notes, contact_id),
        )
        recalculate_contact_credit(conn, contact_id)
        audit(conn, "update_outcome", "contact", contact_id, reply_status or meeting_status, actor_role, actor_id)
        conn.commit()


def recalculate_contact_credit(conn, contact_id: int) -> None:
    rates = payout_rates()
    contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not contact:
        return
    approved = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE contact_id = ? AND status IN ('approved', 'exported')",
        (contact_id,),
    ).fetchone()[0]
    credit = 0.0
    if approved:
        credit += rates["approved"]
    if contact["hot"]:
        credit += rates["hot"]
    if contact["meeting_status"] == "booked":
        credit += rates["booked"]
    conn.execute(
        """
        UPDATE contacts
        SET payout_credit = ?, commission_credit = ?
        WHERE id = ?
        """,
        (credit, credit, contact_id),
    )
    conn.execute(
        """
        UPDATE outcomes
        SET payout_credit = ?, commission_credit = ?, updated_at = CURRENT_TIMESTAMP
        WHERE contact_id = ?
        """,
        (credit, credit, contact_id),
    )


def admin_outcome_rows() -> list[dict]:
    with get_connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT c.*, m.id AS message_id, m.status AS message_status,
                       m.current_message, s.name AS student_name,
                       o.reply_status AS outcome_reply_status,
                       o.hot AS outcome_hot,
                       o.meeting_status AS outcome_meeting_status,
                       o.notes AS outcome_notes
                FROM contacts c
                {LATEST_MESSAGE_JOIN}
                LEFT JOIN students s ON s.id = c.assigned_student_id
                LEFT JOIN outcomes o ON o.contact_id = c.id AND o.message_id = m.id
                WHERE c.status IN ('assigned', 'approved', 'exported', 'skipped')
                   OR COALESCE(c.reply_status, '') <> ''
                   OR c.hot = 1
                ORDER BY c.hot DESC, c.updated_at DESC, c.id DESC
                LIMIT 300
                """
            )
        ]


def leaderboard_rows(existing_conn=None) -> list[dict]:
    """Per-student totals.

    Payout/hot/meeting/wrong counts come from contacts only (no messages join),
    so a contact with multiple message rows is never counted more than once.
    Approved-message count is a scoped subquery for the same reason.
    """
    close = False
    conn = existing_conn
    if conn is None:
        conn = get_connection()
        close = True
    try:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT s.id AS student_id,
                       s.name AS student_name,
                       (
                           SELECT COUNT(*)
                           FROM messages m
                           JOIN contacts mc ON mc.id = m.contact_id
                           WHERE mc.assigned_student_id = s.id
                             AND m.status IN ('approved', 'exported')
                       ) AS approved_count,
                       COUNT(CASE WHEN c.hot = 1 THEN 1 END) AS hot_count,
                       COUNT(CASE WHEN c.meeting_status = 'meeting_requested' THEN 1 END) AS meeting_requested_count,
                       COUNT(CASE WHEN c.meeting_status = 'booked' THEN 1 END) AS booked_count,
                       COUNT(CASE WHEN c.reply_status = 'wrong_number' THEN 1 END) AS wrong_number_count,
                       COALESCE(SUM(c.payout_credit), 0) AS payout_total
                FROM students s
                LEFT JOIN contacts c ON c.assigned_student_id = s.id
                WHERE s.active = 1
                GROUP BY s.id, s.name
                ORDER BY payout_total DESC, approved_count DESC, student_name
                """
            )
        ]
        return rows
    finally:
        if close:
            conn.close()


EXPORT_FIELDS = [
    "phone",
    "first_name",
    "last_name",
    "business_name",
    "city",
    "state",
    "vertical",
    "message",
    "contact_id",
    "student_id",
    "message_id",
    "source_file",
    "source_row",
    "status",
]


def export_approved_csv(export_dir: str | Path, actor_role: str = "system") -> tuple[str, int]:
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT c.phone_normalized AS phone,
                       c.first_name,
                       c.last_name,
                       c.business_name,
                       c.city,
                       c.state,
                       c.vertical,
                       m.current_message AS message,
                       c.id AS contact_id,
                       m.assigned_student_id AS student_id,
                       m.id AS message_id,
                       c.source_file,
                       c.source_row,
                       m.status
                FROM messages m
                JOIN contacts c ON c.id = m.contact_id
                WHERE m.status = 'approved'
                  AND m.exported_at IS NULL
                  AND COALESCE(m.current_message, '') <> ''
                  AND LENGTH(c.phone_normalized) = 10
                ORDER BY m.approved_at, m.id
                """
            )
        ]
        filename = f"smartercontact_export_{_timestamp_for_file(conn)}.csv"
        path = export_dir / filename
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: clean_text(row.get(field, "")) for field in EXPORT_FIELDS})

        message_ids = [row["message_id"] for row in rows]
        if message_ids:
            placeholders = ",".join("?" for _ in message_ids)
            conn.execute(
                f"""
                UPDATE messages
                SET status = 'exported',
                    exported_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                tuple(message_ids),
            )
            # Mark a contact 'exported' only when it has no remaining work
            # (no pending/assigned/approved messages left). This avoids
            # prematurely closing a contact that still has another message.
            contact_ids = {row["contact_id"] for row in rows}
            for contact_id in contact_ids:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE contact_id = ? "
                    "AND status IN ('pending', 'assigned', 'approved')",
                    (contact_id,),
                ).fetchone()[0]
                if remaining == 0:
                    conn.execute(
                        "UPDATE contacts SET status = 'exported', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (contact_id,),
                    )
        audit(conn, "export_approved_csv", "export", None, f"rows={len(rows)}; file={path.name}", actor_role)
        conn.commit()
    return str(path), len(rows)


def _timestamp_for_file(conn) -> str:
    value = conn.execute("SELECT strftime('%Y%m%d_%H%M%S', 'now')").fetchone()[0]
    return str(value)
