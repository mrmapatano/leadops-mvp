# Changelog

## V1.1 — Hardening (June 2026)

Patched against the V1 review. All changes are backward compatible; the data
model and export format are unchanged.

### Fixed
- **Payout double-count (latent bug).** The leaderboard summed `payout_credit`
  across a contact×messages join, so any contact with 2+ message rows inflated
  payouts. Rewrote `leaderboard_rows` to total contacts once and count approved
  messages via a scoped subquery. Covered by `test_payout_not_double_counted...`.
- **Duplicate rows in admin listings.** Contacts with more than one message row
  no longer appear multiple times; listings join only the latest message.
- **Premature contact close on export.** A contact is marked `exported` only when
  it has no remaining pending/assigned/approved messages.

### Added
- **Concurrency safety.** `get_connection` now enables WAL + `busy_timeout`, and
  the server runs `threaded=True`. Multiple students can review at once without
  `database is locked`.
- **CSRF protection.** Every POST form carries a per-session token, validated in
  a `before_request` hook. Dependency-free.
- **Optional admin PIN.** Set `ADMIN_PIN` to require a PIN for Abe/Admin login.
- **Contact edit / unblock.** Admin can fix a contact (e.g. a typo'd phone). The
  phone is re-validated against invalid/duplicate/suppressed; if it now passes,
  the contact returns to the assignable pool.
- **Pagination** on the Contacts page (50/page) with total counts.
- **Configurable secret key** via `LEADOPS_SECRET_KEY`.
- **Upload allow-list** (.csv/.xlsx/.txt) enforced server-side.
- **pytest suite** (`tests/`, 22 tests) covering phone normalization, import
  blocking, retro-suppression, AI matching, idempotent export, the skip-after-
  approve guard, the payout fix, contact edit, CSRF, and the admin PIN.

### Notes still open (by design for a local MVP)
- No real SmarterContact API or direct SMS send.
- HOT-lead notification is still manual (``).
- Student login is still identity-selection (low-privilege); only admin is PIN-gated.
