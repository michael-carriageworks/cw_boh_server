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
from datetime import datetime

from flask import Flask, g, jsonify, request, send_from_directory

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
    return datetime.now().isoformat()

def log(db, text):
    db.execute("INSERT INTO notification_log (ts, text) VALUES (%s, %s)", (now_iso(), text))


# ============================================================
# SERVE THE DASHBOARD
# ============================================================
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "dashboard.html")


# Temporary diagnostic endpoint — reports whether the database is reachable
# without ever exposing the password. Safe to remove once deployment is stable.
@app.route("/api/diag")
def api_diag():
    info = {}
    url = os.environ.get("DATABASE_URL")
    info["database_url_present"] = bool(url)
    if url:
        info["db_host"] = url.split("@")[-1] if "@" in url else "(unparseable)"
    try:
        db = get_connection()
        db.execute("select 1").fetchone()
        db.close()
        info["connect"] = "ok"
    except Exception as e:
        info["connect"] = "error"
        info["error"] = str(e)[:300]
    return jsonify(info)


# ============================================================
# READ: the full state the dashboard needs, in one call
# ============================================================
@app.route("/api/state")
def api_state():
    db = get_db()

    cards = []
    for row in db.execute("SELECT * FROM cards ORDER BY date, start"):
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
        techs = db.execute(
            "SELECT tech_name, source FROM tech_assignments WHERE card_id = %s", (card["id"],)
        ).fetchall()
        card["techsAuto"] = [t["tech_name"] for t in techs if t["source"] == "deputy"]
        card["techsManual"] = [t["tech_name"] for t in techs if t["source"] == "manual" and t["tech_name"] not in card["techsAuto"]]
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

    return jsonify({
        "cards": cards,
        "tasks": tasks,
        "unmatchedShifts": unmatched,
        "logs": logs,
        "lastSyncedAt": last_synced_at,
    })


# ============================================================
# WRITE: producer console actions
# ============================================================
@app.route("/api/tech-assignment", methods=["POST"])
def api_tech_assignment():
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
    data = request.get_json(force=True)
    card_id, location = data.get("cardId"), data.get("location")
    if not card_id or not location:
        return jsonify({"error": "cardId and location required"}), 400
    db = get_db()
    db.execute("UPDATE cards SET resolved_location = %s WHERE id = %s", (location, card_id))
    log(db, f"Location resolved for a card: {location}")
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/manual-event", methods=["POST"])
def api_manual_event():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    day = data["day"]
    cat_key = data.get("category", "other")
    cat_labels = {"load": "Bump In", "strike": "Bump Out", "rehearsal": "Rehearsal",
                  "show": "Event", "facilities": "Cleaning Team Onsite", "other": "Other"}
    cat_colors = {"load": "blue", "strike": "red", "rehearsal": "purple",
                  "show": "amber", "facilities": "slate", "other": "grey"}
    location = data.get("location") or "TBC"
    card_id = f"manual__{day}__{title.lower().replace(' ', '-')}__{int(datetime.now().timestamp())}"

    db = get_db()
    db.execute("""
        INSERT INTO cards (id, project, subproject, date, start, "end", activity_label,
                            category_key, category_label, category_color, pax, notes,
                            location_options, resolved_location, is_manual, needs_review)
        VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s, 1, 0)
    """, (card_id, title, day, data.get("start"), data.get("end"), cat_labels.get(cat_key, "Other"),
          cat_key, cat_labels.get(cat_key, "Other"), cat_colors.get(cat_key, "grey"),
          json.dumps([location]), location))
    log(db, f'Manual event added: "{title}"')
    db.commit()
    return jsonify({"ok": True, "cardId": card_id})


@app.route("/api/task", methods=["POST"])
def api_task():
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
    data = request.get_json(force=True)
    card_id = data.get("cardId")
    if not card_id:
        return jsonify({"error": "cardId required"}), 400
    db = get_db()
    shift = db.execute("SELECT * FROM unmatched_shifts WHERE shift_id = %s", (shift_id,)).fetchone()
    if not shift:
        return jsonify({"error": "unknown shiftId"}), 404
    db.execute(
        "INSERT INTO tech_assignments (card_id, tech_name, source, assigned_at) VALUES (%s, %s, 'manual', %s) ON CONFLICT DO NOTHING",
        (card_id, shift["employee"], now_iso()),
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
