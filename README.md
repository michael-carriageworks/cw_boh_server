# BOH Technician Dashboard — server

## What's in this folder
- `app.py` — the web server. Serves the dashboard and a small API. Run this
  once and leave it running permanently.
- `dashboard.html` — the frontend, served by app.py (don't open this file
  directly — it needs to be served, so open http://<server-ip>:5000 instead).
- `sync_precinct_dashboard.py` — pulls Smartsheet + Deputy on a schedule and
  writes into the shared database. Run this on a timer (cron etc), separately
  from app.py.
- `schema.sql` — database structure, applied automatically on first run.
- `deputy_oauth_setup.py` — run ONCE, manually, to bootstrap Deputy access.
- `boh_dashboard.db` — created automatically. This is the single shared
  source of truth for every device viewing the dashboard.

## First-time setup
1. `pip install -r requirements.txt --break-system-packages`
2. Copy `.env.example` to `.env`, fill in `SMARTSHEET_TOKEN`.
3. Follow the Deputy OAuth steps, then run:
   `python3 deputy_oauth_setup.py <client_id> <client_secret> <code>`
4. Run the sync once manually to check it works and look over the output:
   `python3 sync_precinct_dashboard.py`
5. Start the server: `python3 app.py`
6. From any device on the same venue network, visit:
   `http://<this-machine's-local-ip>:5000`

## Ongoing operation
- `app.py` should run permanently (e.g. as a systemd service, or in a
  process manager) — this is what every device actually talks to.
- `sync_precinct_dashboard.py` should run on a schedule (cron, every
  15–30 min) — this is what keeps Smartsheet/Deputy data current.
- Both read/write the same `boh_dashboard.db` — make sure `DB_PATH` in
  `.env` matches for both.

## Still on the list (deliberately paused for now)
- Remote/off-site access for producers (currently on-site/local-network only,
  by design, until the dashboard is finalised).
- Live weather / lock-up / warden status feeds (still placeholders).
