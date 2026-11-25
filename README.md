# YOUKO HUB — Roblox Script Library
YOUKO HUB - Redeploy update
## Run locally
MySQL Persistence Test V1
1. `pip install -r requirements.txt`
2. Set `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` in your environment (do not publish real credentials).
3. `python app.py`
4. Open `http://127.0.0.1:5000` and sign in at `/login`.

The first startup with both ADMIN_USERNAME and ADMIN_PASSWORD creates the initial admin. Changing those environment variables afterward does not reset the existing password.

## Render deployment

- Push this folder to a **private** GitHub repository (or public repo without secrets).
- Render → New Web Service → connect repo.
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Add `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `DATA_DIR=/var/data` as environment variables.
- **Important:** Add a persistent Render disk mounted at `/var/data` (requires a plan supporting disks). Without it, SQLite data and uploaded logos can disappear after redeployment/restart. For multi-instance deployment, migrate SQLite to Postgres and logos to object storage.
- Use HTTPS and a strong admin password. Configure backups for the disk.

## Security / production checklist

This is a functional starter, not a fully hardened production application. Before publicly launching, add CSRF protection to all forms, login rate limiting, account recovery/password rotation, monitoring, and backups. Keep private/premium script code server-side if access control is required; the current public library exposes both tiers for copying. Only publish scripts you are authorized to share.
