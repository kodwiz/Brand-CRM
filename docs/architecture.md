# Architecture and decisions

## Layers

```
PySide6 desktop  --HTTPS-->  FastAPI (api/ -> services/ -> models/)  -->  PostgreSQL
 (presentation only)          auth + permission + location checks         constraints, triggers, migrations
```

- `backend/api`: thin routers. Authorization via `deps.require(...)`. Map domain errors to HTTP codes.
- `backend/services`: all business rules and audit writes. A service never commits; the router commits once, so a change and
  its audit entry succeed or fail together.
- `backend/models`: SQLAlchemy 2 models. Schema changes only through Alembic migrations.
- `desktop/api/client.py`: HTTP only. `desktop/ui`, `desktop/pages`: presentation. No business totals are computed client-side.

## Phase 1 entities

| Table | Purpose |
|---|---|
| `locations` | Warehouse / branch / both, active flag |
| `roles`, `role_permissions` | Role definitions; permissions are rows (configurable); `scope_all_locations` flag |
| `users`, `user_locations` | Accounts, lockout counters, assigned locations |
| `auth_sessions` | One row per issued token (jti) so tokens can be revoked and expire server-side |
| `audit_log` | Append-only (trigger blocks UPDATE/DELETE); actor, action, entity, reason, before/after JSONB |

Relationships: user -> role (many:1); user <-> location (many:many via `user_locations`); audit_log.actor -> users (SET NULL on delete so history survives).

## Security decisions

- argon2id hashing; generic login error (no user enumeration); constant-ish time via dummy-hash verification.
- Lockout after 5 failures for 15 minutes (configurable); per-IP login limiter; every outcome audited.
- Deactivating a user, changing role, resetting or changing a password revokes all of that user's sessions immediately.
- Out-of-scope locations return 404 (not 403) so their existence is not confirmed.
- Self-protection: users cannot deactivate or re-role themselves; the CEO role cannot lose `users.manage`/`roles.manage`.
- New-user admin dialog never preselects a role (least privilege).
- Errors: unhandled exceptions are logged server-side; clients get a generic 500.

## Assumptions made (please confirm or correct)

1. **Inventory costing: weighted-average cost**, per location and variant. Recorded now as the intended method for Phase 2/6 and
   must not be changed after postings exist. *This is a business rule: confirm before Phase 2.*
2. Manager does **not** get ledger posting, financial reports, or user administration by default (the brief lists oversight, not accounting).
   Adjustable per role.
3. Accounts role sees all locations (company-wide books). Stock keeper and order booker are limited to assigned locations.
4. Integer primary keys; money will use `NUMERIC(14,2)`; timestamps are timezone-aware UTC.
5. Sessions last 8 hours (`ACCESS_TOKEN_MINUTES`). No refresh tokens yet.
6. No company name or branding was supplied, so the UI uses the neutral label "Operations". No product imagery is invented.

## Known limitations of the foundation

- No application-level backup/restore tooling yet (Phase 9). Use `pg_dump` meanwhile.
- No TLS termination is bundled; deploy behind a reverse proxy.
- The sidebar has text labels only; the small icon set from the design brief is not built.
- Self-approval prevention for adjustments cannot be implemented until approvals exist (Phase 8).
