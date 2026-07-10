from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["LEADOPS_DB"] = str(ROOT / "data" / "smoke_test.db")

from leadops.db import get_connection, init_db  # noqa: E402
from leadops.importers import import_ai_messages, import_contacts, import_suppression  # noqa: E402
from leadops.services import assign_next, approve_message, export_approved_csv, get_student_queue  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    init_db(reset=True)
    sample_dir = ROOT / "sample_data"

    suppression = import_suppression(sample_dir / "suppression_sample.csv")
    assert_true(suppression["accepted"] == 2, f"expected 2 suppression numbers, got {suppression}")
    assert_true(suppression["invalid"] == 1, f"expected 1 invalid suppression number, got {suppression}")

    contacts = import_contacts(sample_dir / "contacts_sample.csv")
    assert_true(contacts["accepted"] == 4, f"expected 4 accepted contacts, got {contacts}")
    assert_true(contacts["invalid"] == 1, f"expected 1 invalid contact, got {contacts}")
    assert_true(contacts["duplicate"] == 1, f"expected 1 duplicate contact, got {contacts}")
    assert_true(contacts["suppressed"] == 1, f"expected 1 suppressed contact, got {contacts}")

    messages = import_ai_messages(sample_dir / "smart_drop_output_sample.csv")
    assert_true(messages["matched"] == 4, f"expected 4 matched AI messages, got {messages}")

    with get_connection() as conn:
        student_id = conn.execute("SELECT id FROM students ORDER BY id LIMIT 1").fetchone()["id"]
        blocked = {
            row["block_reason"]: row["count"]
            for row in conn.execute(
                "SELECT block_reason, COUNT(*) AS count FROM contacts WHERE status = 'blocked' GROUP BY block_reason"
            )
        }
    assert_true(blocked.get("invalid_phone") == 1, f"invalid block missing: {blocked}")
    assert_true(blocked.get("duplicate") == 1, f"duplicate block missing: {blocked}")
    assert_true(blocked.get("suppressed") == 1, f"suppressed block missing: {blocked}")

    assigned = assign_next(student_id, 3, actor_role="smoke_test")
    assert_true(len(assigned) == 3, f"expected 3 assigned leads, got {assigned}")

    queue = get_student_queue(student_id)
    assert_true(len(queue) == 3, f"expected 3 queue rows, got {len(queue)}")
    approve_message(queue[0]["message_id"], student_id, queue[0]["current_message"], actor_role="smoke_test")
    approve_message(queue[1]["message_id"], student_id, queue[1]["current_message"], actor_role="smoke_test")

    export_path, count = export_approved_csv(ROOT / "exports", actor_role="smoke_test")
    assert_true(count == 2, f"expected 2 exported rows, got {count}")
    assert_true(Path(export_path).exists(), f"export missing: {export_path}")

    exported = pd.read_csv(export_path, dtype=str)
    required = {
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
    }
    assert_true(required.issubset(set(exported.columns)), f"missing export fields: {exported.columns}")
    assert_true(len(exported) == 2, f"expected 2 CSV rows, got {len(exported)}")
    assert_true(all(exported["phone"].str.len() == 10), "export phone values are not normalized")

    print(f"PASS smoke test: exported {count} rows to {export_path}")


if __name__ == "__main__":
    main()

