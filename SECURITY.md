# Security

## Reporting a problem
Please do not open a public issue for a security problem. Use GitHub's "Report a vulnerability" (Security tab > Advisories) on this repository, with steps to reproduce. Do not include real business data.

## Using this project safely
* Never commit `.env`, backups, or real data. `.gitignore` excludes them; check `git status` before every commit.
* Demo users and data (`scripts/setup_demo.py`, `seed-demo`) are for testing only and are refused when `ENVIRONMENT=production`.
* Set `ENVIRONMENT=production` and a random `JWT_SECRET` (at least 32 characters) before real use; run `python -m backend.cli readiness`.
* Keep the backup passphrase (`BACKUP_PASSPHRASE`) separate from the backups; if it is lost, encrypted backups cannot be opened.
* Run behind HTTPS if the backend is reachable over a network; the application does not terminate TLS itself.
