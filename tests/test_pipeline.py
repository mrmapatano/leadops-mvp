import pandas as pd

from leadops.db import get_connection
from leadops.importers import import_ai_messages, import_contacts, import_suppression
from leadops.services import (
    approve_message,
    assign_next,
    edit_contact,
    export_approved_csv,
    get_student_queue,
    leaderboard_rows,
    skip_message,
    update_outcome,
)


def _first_student():
    with get_connection() as conn:
        return conn.execute("SELECT id FROM students ORDER BY id LIMIT 1").fetchone()["id"]


def test_import_counts(loaded):
    with get_connection() as conn:
        blocked = {
            r["block_reason"]: r["count"]
            for r in conn.execute(
                "SELECT block_reason, COUNT(*) count FROM contacts WHERE status='blocked' GROUP BY block_reason"
            )
        }
        accepted = conn.execute("SELECT COUNT(*) c FROM contacts WHERE status='pending'").fetchone()["c"]
    assert blocked.get("invalid_phone") == 1
    assert blocked.get("duplicate") == 1
    assert blocked.get("suppressed") == 1
    assert accepted == 4


def test_suppression_after_contacts_retroblocks(db, sample_dir):
    import_contacts(sample_dir / "contacts_sample.csv")  # no suppression yet
    with get_connection() as conn:
        before = conn.execute(
            "SELECT status FROM contacts WHERE phone_normalized='7195550199'"
        ).fetchone()["status"]
    assert before == "pending"
    import_suppression(sample_dir / "suppression_sample.csv")
    with get_connection() as conn:
        after = conn.execute(
            "SELECT status, block_reason FROM contacts WHERE phone_normalized='7195550199'"
        ).fetchone()
    assert after["status"] == "blocked" and after["block_reason"] == "suppressed"


def test_ai_matching_and_unmatched(loaded):
    with get_connection() as conn:
        # 4 accepted contacts each get a message; the "Unmatched Patio Co" row matches nothing
        msgs = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        unmatched = conn.execute(
            "SELECT COUNT(*) c FROM contacts WHERE business_name LIKE 'Unmatched%'"
        ).fetchone()["c"]
    assert msgs == 4
    assert unmatched == 0


def test_reimport_ai_is_idempotent_on_pending(loaded, sample_dir):
    import_ai_messages(sample_dir / "smart_drop_output_sample.csv")  # second time
    with get_connection() as conn:
        msgs = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    assert msgs == 4  # updated in place, not duplicated


def test_assign_approve_export_idempotent(loaded, tmp_path):
    sid = _first_student()
    assign_next(sid, 3)
    queue = get_student_queue(sid)
    assert len(queue) == 3
    approve_message(queue[0]["message_id"], sid, queue[0]["current_message"])
    approve_message(queue[1]["message_id"], sid, queue[1]["current_message"])
    path, count = export_approved_csv(tmp_path / "exports")
    assert count == 2
    df = pd.read_csv(path, dtype=str)
    assert len(df) == 2
    assert all(df["phone"].str.len() == 10)
    # second export yields nothing (exported_at respected)
    _, count2 = export_approved_csv(tmp_path / "exports")
    assert count2 == 0


def test_skip_after_approve_is_rejected(loaded):
    import pytest
    sid = _first_student()
    assign_next(sid, 1)
    q = get_student_queue(sid)
    mid = q[0]["message_id"]
    approve_message(mid, sid, q[0]["current_message"])
    with pytest.raises(ValueError):
        skip_message(mid, sid, "changed mind", )


def test_empty_message_rejected(loaded):
    import pytest
    sid = _first_student()
    assign_next(sid, 1)
    q = get_student_queue(sid)
    with pytest.raises(ValueError):
        approve_message(q[0]["message_id"], sid, "")


def test_payout_not_double_counted_with_two_messages(loaded, sample_dir):
    """The leaderboard must count a contact's payout once even if it has 2 message rows."""
    sid = _first_student()
    assign_next(sid, 1)
    q = get_student_queue(sid)
    approve_message(q[0]["message_id"], sid, q[0]["current_message"])
    # Re-import AI -> upsert creates a SECOND message row on the approved contact
    import_ai_messages(sample_dir / "smart_drop_output_sample.csv")
    with get_connection() as conn:
        cid = q[0]["id"]
        n = conn.execute("SELECT COUNT(*) c FROM messages WHERE contact_id=?", (cid,)).fetchone()["c"]
    assert n == 2  # confirms the double-message condition exists
    board = {r["student_id"]: r for r in leaderboard_rows()}
    # one approved contact at $0.25, counted ONCE despite two message rows
    assert round(board[sid]["payout_total"], 2) == 0.25
    assert board[sid]["approved_count"] == 1


def test_edit_contact_unblocks_invalid(loaded):
    with get_connection() as conn:
        bad = conn.execute(
            "SELECT id FROM contacts WHERE block_reason='invalid_phone'"
        ).fetchone()["id"]
    new_status = edit_contact(bad, {"phone": "719-555-0123"})
    assert new_status == "pending"
    # now assignable
    sid = _first_student()
    # assign everything; the fixed contact has no message yet, so it won't assign,
    # but its status/block_reason must be cleared
    c = __import__("leadops.services", fromlist=["get_contact"]).get_contact(bad)
    assert c["status"] == "pending" and c["block_reason"] is None


def test_edit_contact_into_duplicate_blocks(loaded):
    from leadops.services import get_contact
    with get_connection() as conn:
        ok = conn.execute("SELECT id, phone_normalized FROM contacts WHERE status='pending' LIMIT 1").fetchone()
        other = conn.execute(
            "SELECT id FROM contacts WHERE status='pending' AND id<>? LIMIT 1", (ok["id"],)
        ).fetchone()["id"]
    # set 'other' phone equal to ok's -> should block as duplicate
    status = edit_contact(other, {"phone": ok["phone_normalized"]})
    assert status == "blocked"
    assert get_contact(other)["block_reason"] == "duplicate"
