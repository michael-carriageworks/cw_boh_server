#!/usr/bin/env python3
"""
app.py — BOH Technician Dashboard backend.

Serves the dashboard HTML and a small REST API backed by Supabase (Postgres).
This is the piece that makes updates genuinely shared: the cage display and
every producer's device all read from and write to this same database, instead
of each browser tab keeping its own private copy of the data.

Run locally (talks to your Supabase database — set DATABASE_URL first):
    pip install -r requirements.txt
    python3 app.py

On Vercel this same `app` object is served as a serverless function (see
api/index.py and vercel.json).
"""

import os
import json
import hmac
from datetime import datetime, timezone, timedelta

from flask import (Flask, g, jsonify, request, send_from_directory,
                   session, redirect, render_template_string)

from db import get_connection, run_schema

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = get_connection()
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    """Run the schema (idempotent — every statement is IF NOT EXISTS). Used when
    running locally; on Supabase the schema is normally created once up front."""
    conn = get_connection()
    run_schema(conn, SCHEMA_PATH)
    conn.close()

def now_iso():
    # UTC with an explicit offset, so browsers localise it correctly (the sync
    # runs on GitHub's UTC servers; without the offset the time reads ~10h off).
    return datetime.now(timezone.utc).isoformat()

def log(db, text):
    db.execute("INSERT INTO notification_log (ts, text) VALUES (%s, %s)", (now_iso(), text))


# ============================================================
# SHARED LOGIN (single username/password for everyone)
# ============================================================
# The gate only activates once BOTH a username and password are configured
# (as Vercel environment variables), so deploying this code changes nothing
# until you set them — no risk of locking yourself out. SECRET_KEY signs the
# "you're logged in" cookie; it must be a stable random value in the cloud.
#
# Two logins, two roles:
#   DASHBOARD_USER/PASSWORD               -> 'producer' (full access — the
#                                            original login keeps working)
#   DASHBOARD_VIEWER_USER/VIEWER_PASSWORD -> 'viewer' (techs + cage screen:
#                                            sees everything, can't edit)
# The viewer pair is optional; until it's set there is simply no viewer login.
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-insecure-change-me")
app.permanent_session_lifetime = timedelta(days=30)  # stay signed in for 30 days

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
DASHBOARD_VIEWER_USER = os.environ.get("DASHBOARD_VIEWER_USER", "")
DASHBOARD_VIEWER_PASSWORD = os.environ.get("DASHBOARD_VIEWER_PASSWORD", "")
LOGIN_ENABLED = bool(DASHBOARD_USER and DASHBOARD_PASSWORD)


def current_role():
    """'producer' or 'viewer'. Sessions created before roles existed belonged
    to the only login that existed then — the producer one."""
    if not LOGIN_ENABLED:
        return "producer"
    return session.get("role", "producer")


def require_producer():
    """Server-side gate for editing actions. Hiding the console tab in the UI
    is cosmetic — this is the check that actually enforces it."""
    if LOGIN_ENABLED and current_role() != "producer":
        return jsonify({"error": "This action needs the producer login"}), 403
    return None

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Carriageworks BOH — Sign in</title>
<style>
 :root{--bg:#0d0d0f;--panel:#17171b;--line:#2a2a30;--text:#e8e8ea;--dim:#9a9aa2;--amber:#f2a02a;}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);
      color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}
 .card{width:min(92vw,360px);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px;}
 h1{font-size:15px;letter-spacing:.14em;text-transform:uppercase;color:var(--amber);margin:0 0 4px;}
 p.sub{margin:0 0 22px;color:var(--dim);font-size:13px;}
 label{display:block;font-size:12px;color:var(--dim);margin:14px 0 6px;text-transform:uppercase;letter-spacing:.08em;}
 input{width:100%;padding:11px 12px;background:#0e0e11;border:1px solid var(--line);border-radius:8px;color:var(--text);font-size:16px;}
 input:focus{outline:none;border-color:var(--amber);}
 button{width:100%;margin-top:22px;padding:12px;background:var(--amber);color:#111;border:0;border-radius:8px;
        font-weight:700;font-size:15px;cursor:pointer;letter-spacing:.04em;}
 .err{margin-top:16px;color:#ff6b6b;font-size:13px;min-height:16px;}
</style></head><body>
 <form class="card" method="post" action="/login">
   <h1>Carriageworks BOH</h1>
   <p class="sub">Technician Dashboard — please sign in</p>
   <label for="username">Username</label>
   <input id="username" name="username" autocomplete="username" autofocus>
   <label for="password">Password</label>
   <input id="password" name="password" type="password" autocomplete="current-password">
   <button type="submit">Sign in</button>
   <div class="err">{{ error }}</div>
 </form>
</body></html>"""


@app.before_request
def _require_login():
    if not LOGIN_ENABLED:
        return None  # gate disabled until a username + password are configured
    path = request.path
    if path in ("/login", "/logout", "/favicon.ico", "/brief") or path.startswith("/static"):
        return None  # /brief (the infrastructure brief) is intentionally public
    if session.get("authed"):
        return None
    if path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not LOGIN_ENABLED:
        return redirect("/")
    error = ""
    if request.method == "POST":
        u = request.form.get("username", "")
        pw = request.form.get("password", "")
        # constant-time comparisons, so response timing can't leak the password
        role = None
        if hmac.compare_digest(u, DASHBOARD_USER) and hmac.compare_digest(pw, DASHBOARD_PASSWORD):
            role = "producer"
        elif (DASHBOARD_VIEWER_USER and DASHBOARD_VIEWER_PASSWORD
              and hmac.compare_digest(u, DASHBOARD_VIEWER_USER)
              and hmac.compare_digest(pw, DASHBOARD_VIEWER_PASSWORD)):
            role = "viewer"
        if role:
            session.permanent = True
            session["authed"] = True
            session["role"] = role
            return redirect("/")
        error = "Incorrect username or password."
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ============================================================
# SERVE THE DASHBOARD
# ============================================================
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "dashboard.html")


@app.route("/brief")
def brief():
    # Public, read-only infrastructure & security brief (shareable link for staff/IT).
    return send_from_directory(STATIC_DIR, "brief.html")


# ============================================================
# READ: the full state the dashboard needs, in one call
# ============================================================
@app.route("/api/state")
def api_state():
    db = get_db()

    # Fetch ALL tech assignments in a single query and group them by card in
    # Python. (Previously this ran one query per card — fine on a local SQLite
    # file, but ~144 network round-trips to a cloud database, which timed out.)
    techs_by_card = {}
    for t in db.execute(
        "SELECT card_id, tech_name, source, role, shift_start, shift_end FROM tech_assignments"
    ):
        techs_by_card.setdefault(t["card_id"], []).append(t)

    cards = []
    for row in db.execute("SELECT * FROM cards ORDER BY date, start").fetchall():
        card = dict(row)
        card["locationOptions"] = json.loads(card.pop("location_options") or "[]")
        card["category"] = {
            "key": card.pop("category_key"),
            "label": card.pop("category_label"),
            "color": card.pop("category_color"),
        }
        card["isManual"] = bool(card.pop("is_manual"))
        card["needsReview"] = bool(card.pop("needs_review"))
        card["locationLabel"] = (
            card["resolved_location"]
            or (card["locationOptions"][0] if len(card["locationOptions"]) == 1 else None)
            or ("TBC" if not card["locationOptions"] else "Multiple — TBC")
        )
        techs = techs_by_card.get(card["id"], [])
        card["techsAuto"] = [t["tech_name"] for t in techs if t["source"] == "deputy"]
        card["techsManual"] = [t["tech_name"] for t in techs if t["source"] == "manual" and t["tech_name"] not in card["techsAuto"]]
        # Full staff detail: role ('senior'/'fohm'/'tech') + rostered shift times,
        # so the frontend can show seniors/FOHMs and work out who's on duty now.
        seen_names = set()
        card["staff"] = []
        for t in techs:
            if t["tech_name"] in seen_names:
                continue
            seen_names.add(t["tech_name"])
            card["staff"].append({
                "name": t["tech_name"], "role": t["role"] or "tech",
                "source": t["source"], "start": t["shift_start"], "end": t["shift_end"],
            })
        cards.append(card)

    tasks = [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY id DESC")]

    unmatched = [dict(r) for r in db.execute(
        "SELECT * FROM unmatched_shifts WHERE resolved = 0 ORDER BY date"
    )]

    logs = [dict(r) for r in db.execute(
        "SELECT * FROM notification_log ORDER BY id DESC LIMIT 50"
    )]

    meta_row = db.execute("SELECT value FROM meta WHERE key = 'last_synced_at'").fetchone()
    last_synced_at = meta_row["value"] if meta_row else None

    clock_row = db.execute("SELECT value FROM meta WHERE key = 'clock_status'").fetchone()
    try:
        clock_status = json.loads(clock_row["value"]) if clock_row else {}
    except (ValueError, TypeError):
        clock_status = {}

    return jsonify({
        "cards": cards,
        "tasks": tasks,
        "unmatchedShifts": unmatched,
        "logs": logs,
        "lastSyncedAt": last_synced_at,
        "clockStatus": clock_status,
        "role": current_role(),
    })


# ============================================================
# WRITE: producer console actions
# ============================================================
@app.route("/api/tech-assignment", methods=["POST"])
def api_tech_assignment():
    gate = require_producer()
    if gate:
        return gate
    data = request.get_json(force=True)
    card_id, tech_name = data.get("cardId"), (data.get("techName") or "").strip()
    if not card_id or not tech_name:
        return jsonify({"error": "cardId and techName required"}), 400
    db = get_db()
    card = db.execute('SELECT project, start, "end" FROM cards WHERE id = %s', (card_id,)).fetchone()
    if not card:
        return jsonify({"error": "unknown cardId"}), 404
    db.execute(
        "INSERT INTO tech_assignments (card_id, tech_name, source, assigned_at) VALUES (%s, %s, 'manual', %s) ON CONFLICT DO NOTHING",
        (card_id, tech_name, now_iso()),
    )
    log(db, f'Push sent to {tech_name}: assigned to "{card["project"]}" ({card["start"]}–{card["end"]})')
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/location", methods=["POST"])
def api_location():
    gate = require_producer()
    if gate:
        return gate
    data = request.get_json(force=True)
    card_id, location = data.get("cardId"), data.get("location")
    if not card_id or not location:
        return jsonify({"error": "cardId and location required"}), 400
    db = get_db()
    db.execute("UPDATE cards SET resolved_location = %s WHERE id = %s", (location, card_id))
    log(db, f"Location resolved for a card: {location}")
    db.commit()
    return jsonify({"ok": True})


# NOTE: the "add manual event" feature was removed at Michael's request
# (July 2026) — all events come from Smartsheet. Existing is_manual cards in
# the database are still preserved and displayed if any exist.


@app.route("/api/task", methods=["POST"])
def api_task():
    gate = require_producer()
    if gate:
        return gate
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    tech = (data.get("tech") or "All on shift").strip()
    db = get_db()
    row = db.execute(
        "INSERT INTO tasks (day, title, category, tech, status, created_at) VALUES (%s, %s, %s, %s, 'pending', %s) RETURNING id",
        (data["day"], title, data.get("category", "Other"), tech, now_iso()),
    ).fetchone()
    via = " + ".join(filter(None, [
        "Push" if data.get("notifyPush") else None,
        "SMS" if data.get("notifySms") else None,
    ])) or "no channel selected"
    log(db, f'{via} sent to {tech}: "{title}"')
    db.commit()
    return jsonify({"ok": True, "taskId": row["id"]})


@app.route("/api/task/<int:task_id>/toggle", methods=["POST"])
def api_task_toggle(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
    if not task:
        return jsonify({"error": "unknown task"}), 404
    if task["status"] == "pending":
        db.execute("UPDATE tasks SET status = 'done', completed_at = %s WHERE id = %s", (now_iso(), task_id))
        log(db, f'Producer notified: "{task["title"]}" marked complete by {task["tech"]}')
    else:
        db.execute("UPDATE tasks SET status = 'pending', completed_at = NULL WHERE id = %s", (task_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/unmatched/<shift_id>/resolve", methods=["POST"])
def api_resolve_unmatched(shift_id):
    gate = require_producer()
    if gate:
        return gate
    data = request.get_json(force=True)
    card_id = data.get("cardId")
    if not card_id:
        return jsonify({"error": "cardId required"}), 400
    db = get_db()
    shift = db.execute("SELECT * FROM unmatched_shifts WHERE shift_id = %s", (shift_id,)).fetchone()
    if not shift:
        return jsonify({"error": "unknown shiftId"}), 404
    db.execute(
        "INSERT INTO tech_assignments (card_id, tech_name, source, assigned_at, role, shift_start, shift_end) "
        "VALUES (%s, %s, 'manual', %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (card_id, shift["employee"], now_iso(), shift["role"] or "tech", shift["start"], shift["end"]),
    )
    db.execute("UPDATE unmatched_shifts SET resolved = 1 WHERE shift_id = %s", (shift_id,))
    log(db, f"Manually linked {shift['employee']}'s shift to a card")
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    # threaded=True: fine at this scale (a handful of concurrent devices).
    # host="0.0.0.0" so other devices on the network can reach it when run locally.
    app.run(host="0.0.0.0", port=8000, threaded=True)
