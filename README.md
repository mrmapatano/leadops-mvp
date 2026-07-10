# LeadOps MVP

LeadOps is the control room for a student SDR team running SMS outreach. The problem it solves: AI writes personalized first-touch messages at volume, but a human needs to approve every message before it is sent, and the business needs to know that no invalid, duplicate, or opted-out number can ever reach the send list.

## What it does

An admin imports three things: a contact list, a file of AI-generated messages, and a suppression list (any of .csv, .xlsx, .txt). The importer normalizes US phone numbers, blocks invalid, duplicate, and suppressed rows while keeping them for audit, and matches each message to its contact by phone, then external id, then unique business name.

Leads get assigned to reps. Reps work a queue where they edit, approve, skip, and record outcomes. The admin sees a dashboard, a leaderboard, and payout math. Export produces a send-ready CSV for the SMS platform, and exported rows are marked so they can never export twice.

## Stack

Python, Flask, SQLite in WAL mode, pandas and openpyxl for imports, Jinja templates, vanilla JS. No external services and no API keys. All data in `sample_data/` is fictional.

## Security posture

Parameterized SQL everywhere, CSRF tokens on every POST, PIN auth compared with `hmac.compare_digest`, audit logging on state changes, idempotent upserts on import.

## Run it

```
pip install -r requirements.txt
python app.py
```

Then log in with the seeded sample PINs printed on first run, import the files from `sample_data/`, and walk the flow: import, assign, approve, export.

## Tests

22 pytest cases cover the import pipeline, phone normalization, and the web routes:

```
pip install -r requirements-dev.txt
pytest
```

`scripts/smoke_test.py` runs the whole flow end to end.

## My role and honest limits

I designed this system, specified every behavior, and built it with AI pair programming, then reviewed and tested the result. It is an MVP built for one team's workflow: single-tenant, PIN auth rather than accounts, SQLite rather than a hosted database. The CHANGELOG documents real bugs found and fixed during hardening, including a payout double-count.
