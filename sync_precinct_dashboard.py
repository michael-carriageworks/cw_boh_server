#!/usr/bin/env python3
"""
sync_precinct_dashboard.py

Scheduled sync job for the Carriageworks BOH Technician Dashboard.

What it does, each time it runs:
  1. Pulls the PRECINCT OPS AGENDA report fresh from Smartsheet's REST API
     (pinned by report ID, not by name).
  2. Pulls rostered shifts from Deputy for the next N days.
  3. Splits multi-activity Smartsheet rows into individual activity "cards"
     (same rules validated in the dashboard: use notes to find per-activity
     times when possible; otherwise keep one combined card).
  4. Matches each Deputy shift to the right card(s) using:
       - project name (normalised, matched against the known Smartsheet
         project list pulled in step 1)
       - date
       - time overlap (NOT equality) between the shift and the card
  5. Writes everything to data.json for the dashboard to fetch.

SETUP REQUIRED BEFORE RUNNING:
  - Set the environment variables listed under CONFIG below (a .env file
    with python-dotenv works well — do NOT hardcode tokens in this file).
  - Confirm the Deputy resource object name and field names for your
    install by calling:
        GET {DEPUTY_BASE}/resource/Roster/INFO
    with your token, and adjust DEPUTY_FIELD_MAP below if it differs.
  - pip install requests python-dotenv --break-system-packages
"""

import os
import re
import json
import sys
from datetime import datetime, timedelta, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if you're setting real environment variables another way


# ============================================================
# CONFIG — set these as environment variables, never hardcode
# ============================================================
SMARTSHEET_TOKEN = os.environ.get("SMARTSHEET_TOKEN")
SMARTSHEET_REPORT_ID = os.environ.get("SMARTSHEET_REPORT_ID", "5778567599181700")

DEPUTY_LOOKAHEAD_DAYS = int(os.environ.get("DEPUTY_LOOKAHEAD_DAYS", "14"))
DEPUTY_BOH_LOCATION_NAMES = [
    n.strip() for n in os.environ.get(
        "DEPUTY_BOH_LOCATION_NAMES",
        "Venue Tech – Level One,Venue Tech – Level Two,Venue Tech – Level Three,"
        "Audio Tech – Level One,Audio Tech – Level Two,Audio Tech – Level Three,"
        "Lighting Tech – Level One,Lighting Tech – Level Two,Lighting Tech – Level Three,"
        "Vision Tech – Level One,Vision Tech – Level Two,Vision Tech – Level Three,"
        "Rigger – Level One,Rigger – Level Two,Rigger – Level Three,"
        "Senior Technician – Production"
    ).split(",") if n.strip()
]
DEPUTY_TOKEN_STORE = os.environ.get("DEPUTY_TOKEN_STORE", "deputy_token_store.json")

from db import get_connection, run_schema


SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

def ensure_schema(conn):
    """The sync script no longer depends on app.py having been run first —
    every CREATE TABLE in schema.sql uses IF NOT EXISTS, so this is safe
    to call every time regardless of whether the tables already exist."""
    run_schema(conn, SCHEMA_PATH)

def write_to_database(cards, tech_assignments, unmatched, generated_at):
    """
    Upserts everything into the shared SQLite database, carefully preserving:
      - resolved_location on cards a producer has already picked
      - resolved=1 on unmatched_shifts a producer has already manually linked
      - all rows in tech_assignments with source='manual' (producer-added,
        never touched by sync — only source='deputy' rows are replaced)
      - all is_manual=1 cards (producer ad hoc events, never touched by sync)
    """
    conn = get_connection()
    ensure_schema(conn)

    existing_smartsheet_ids = {r["id"] for r in conn.execute("SELECT id FROM cards WHERE is_manual = 0")}
    new_ids = {c["id"] for c in cards}
    removed_ids = existing_smartsheet_ids - new_ids
    for rid in removed_ids:
        conn.execute("DELETE FROM cards WHERE id = %s", (rid,))
        conn.execute("DELETE FROM tech_assignments WHERE card_id = %s", (rid,))
    if removed_ids:
        conn.execute(
            "INSERT INTO notification_log (ts, text) VALUES (%s, %s)",
            (datetime.now(timezone.utc).isoformat(), f"{len(removed_ids)} event(s) removed from Smartsheet, no longer on the dashboard"),
        )

    for c in cards:
        auto_location = c["locationOptions"][0] if len(c["locationOptions"]) == 1 else None
        conn.execute("""
            INSERT INTO cards (id, project, subproject, date, start, "end", activity_label,
                                category_key, category_label, category_color, pax, notes,
                                location_options, resolved_location, is_manual, needs_review)
            VALUES (%(id)s, %(project)s, %(subproject)s, %(date)s, %(start)s, %(end)s, %(activityLabel)s,
                    %(category_key)s, %(category_label)s, %(category_color)s, %(pax)s, %(notes)s,
                    %(location_options)s, %(resolved_location)s, 0, %(needs_review)s)
            ON CONFLICT(id) DO UPDATE SET
                project=excluded.project, subproject=excluded.subproject, date=excluded.date,
                start=excluded.start, "end"=excluded."end", activity_label=excluded.activity_label,
                category_key=excluded.category_key, category_label=excluded.category_label,
                category_color=excluded.category_color, pax=excluded.pax, notes=excluded.notes,
                location_options=excluded.location_options, needs_review=excluded.needs_review
                -- deliberately NOT overwriting resolved_location — a producer's pick survives resync
        """, {
            "id": c["id"], "project": c["project"], "subproject": c["subproject"], "date": c["date"],
            "start": c["start"], "end": c["end"], "activityLabel": c["activityLabel"],
            "category_key": c["category"]["key"], "category_label": c["category"]["label"],
            "category_color": c["category"]["color"], "pax": c["pax"], "notes": c["notes"],
            "location_options": json.dumps(c["locationOptions"]), "resolved_location": auto_location,
            "needs_review": int(c["needsReview"]),
        })

    conn.execute("DELETE FROM tech_assignments WHERE source = 'deputy'")
    now = datetime.now(timezone.utc).isoformat()
    for card_id, names in tech_assignments.items():
        for name in names:
            conn.execute(
                "INSERT INTO tech_assignments (card_id, tech_name, source, assigned_at) VALUES (%s, %s, 'deputy', %s) ON CONFLICT DO NOTHING",
                (card_id, name, now),
            )

    for u in unmatched:
        conn.execute("""
            INSERT INTO unmatched_shifts (shift_id, employee, date, start, "end", note, reason, resolved)
            VALUES (%(shift_id)s, %(employee)s, %(date)s, %(start)s, %(end)s, %(note)s, %(reason)s, 0)
            ON CONFLICT(shift_id) DO UPDATE SET
                employee=excluded.employee, date=excluded.date, start=excluded.start,
                "end"=excluded."end", note=excluded.note, reason=excluded.reason
                -- deliberately NOT resetting `resolved` — a producer's manual link survives resync
        """, u)

    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('last_synced_at', %s) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (generated_at,),
    )

    conn.commit()
    conn.close()

REQUIRED = {"SMARTSHEET_TOKEN": SMARTSHEET_TOKEN}
missing = [k for k, v in REQUIRED.items() if not v]
if missing:
    sys.exit(f"Missing required environment variables: {', '.join(missing)}")

SMARTSHEET_HEADERS = {"Authorization": f"Bearer {SMARTSHEET_TOKEN}"}


def load_deputy_token_store():
    """Load the Deputy OAuth token store from the database (the durable home in
    the cloud). On first run the database row won't exist yet, so we fall back
    to the local deputy_token_store.json bootstrap file and copy it into the
    database. If neither exists, there is nothing to refresh from."""
    conn = get_connection()
    ensure_schema(conn)
    row = conn.execute("SELECT data FROM deputy_tokens WHERE id = 1").fetchone()
    conn.close()
    if row:
        return json.loads(row["data"])
    if os.path.exists(DEPUTY_TOKEN_STORE):
        with open(DEPUTY_TOKEN_STORE) as f:
            store = json.load(f)
        save_deputy_token_store(store)  # migrate the local file into the database
        return store
    sys.exit(
        f"No Deputy token found in the database or at {DEPUTY_TOKEN_STORE} — "
        f"run deputy_oauth_setup.py once first to bootstrap the initial "
        f"access/refresh token pair."
    )

def save_deputy_token_store(store):
    """Persist the token store back to the database, upserting the single row."""
    conn = get_connection()
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO deputy_tokens (id, data) VALUES (1, %s) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
        (json.dumps(store),),
    )
    conn.commit()
    conn.close()

def normalize_endpoint(endpoint):
    """Deputy's OAuth response has been observed to return the endpoint
    WITHOUT a scheme (e.g. '1707d020060814.au.deputy.com' rather than
    'https://1707d020060814.au.deputy.com') — don't assume either way."""
    endpoint = endpoint.strip().rstrip("/")
    if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
        endpoint = "https://" + endpoint
    return endpoint

def refresh_deputy_access_token():
    """
    Runs at the start of every sync — refreshes the access token using the
    stored refresh token, rather than trying to track/guess an expiry window.
    Always persists whatever refresh_token comes back, in case Deputy issues
    a new one (the docs don't guarantee it does, but don't guarantee it
    doesn't either, so we don't assume either way).
    """
    store = load_deputy_token_store()
    store["endpoint"] = normalize_endpoint(store["endpoint"])
    refresh_url = f"{store['endpoint']}/oauth/access_token"
    resp = requests.post(refresh_url, data={
        "grant_type": "refresh_token",
        "refresh_token": store["refresh_token"],
        "client_id": store["client_id"],
        "client_secret": store["client_secret"],
    })
    if not resp.ok:
        sys.exit(
            f"Deputy token refresh failed ({resp.status_code}): {resp.text}\n"
            f"If the refresh_token itself has been revoked/expired, you'll need "
            f"to re-run deputy_oauth_setup.py to get a fresh one."
        )
    data = resp.json()
    store["access_token"] = data["access_token"]
    if "refresh_token" in data:
        store["refresh_token"] = data["refresh_token"]
    if "endpoint" in data:
        store["endpoint"] = normalize_endpoint(data["endpoint"])
    save_deputy_token_store(store)
    return store


_deputy_store = refresh_deputy_access_token()
DEPUTY_BASE = f"{_deputy_store['endpoint']}/api/v1"
DEPUTY_HEADERS = {"Authorization": f"Bearer {_deputy_store['access_token']}", "Content-Type": "application/json"}


# ============================================================
# CATEGORY MAPPING — colour-coding tied to activity type
# (kept identical to the logic already validated in the dashboard)
# ============================================================
CATEGORY_MAP = {
    "bump in":                {"key": "load",       "color": "blue",   "label": "Bump In"},
    "pre-rig":                {"key": "load",       "color": "blue",   "label": "Pre-Rig"},
    "bump out":                {"key": "strike",     "color": "red",    "label": "Bump Out"},
    "rehearsals":              {"key": "rehearsal",  "color": "purple", "label": "Rehearsal"},
    "preview":                 {"key": "show",       "color": "amber",  "label": "Preview"},
    "performance":             {"key": "show",       "color": "amber",  "label": "Performance"},
    "event":                   {"key": "show",       "color": "amber",  "label": "Event"},
    "opening night function":  {"key": "function",   "color": "gold",   "label": "Opening Night Function"},
    "shoot - with sound":      {"key": "shoot",       "color": "teal",   "label": "Shoot (Sound)"},
    "cleaning team onsite":    {"key": "facilities",  "color": "slate",  "label": "Cleaning Team Onsite"},
    "open":                    {"key": "open",        "color": "slate",  "label": "Open"},
    "no activity":             {"key": "none",        "color": "dim",    "label": "No Activity"},
}

def categorize(label):
    key = (label or "").strip().lower()
    if key in CATEGORY_MAP:
        return CATEGORY_MAP[key]
    for k in sorted(CATEGORY_MAP.keys(), key=len, reverse=True):
        if k in key:
            return CATEGORY_MAP[k]
    return {"key": "other", "color": "grey", "label": label or "Other"}


# ============================================================
# NOTES-BASED TIME PARSER (ported from the validated JS logic)
# ============================================================
def normalize_time_token(tok):
    tok = tok.strip()
    if ":" in tok:
        h, m = tok.split(":", 1)
        return f"{h.zfill(2)}:{(m or '00').zfill(2)}"
    if "." in tok:
        h, m = tok.split(".", 1)
        return f"{h.zfill(2)}:{(m or '00').zfill(2)}"
    if re.fullmatch(r"\d{3,4}", tok):
        tok = tok.zfill(4)
        return f"{tok[:2]}:{tok[2:]}"
    return tok

def extract_time_range(line):
    m = re.search(r"([\d:.]{3,5})\s*-\s*([\d:.]{3,5})", line)
    if not m:
        return None
    return {"start": normalize_time_token(m.group(1)), "end": normalize_time_token(m.group(2))}

def parse_notes_for_token_times(notes, tokens):
    if not notes:
        return None
    lines = [l.strip() for l in re.split(r"\\n|\n", notes) if l.strip()]
    result = {}
    for token in tokens:
        key = token.strip().lower()
        found = None
        for line in lines:
            if key in line.lower():
                tr = extract_time_range(line)
                if tr:
                    found = tr
                    break
        if not found:
            return None
        result[token] = found
    return result


# ============================================================
# EXPAND EACH SMARTSHEET ROW INTO ONE OR MORE ACTIVITY CARDS
# ============================================================
_seen_ids = {}
def slugify(s):
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "x"

def make_stable_card_id(project, date, activity_label, start):
    """
    Deterministic ID based on content, not processing order — so a producer's
    saved tech assignment or location choice for this card still applies next
    sync, even if Smartsheet rows get reordered or new rows are inserted above it.
    Collision-safe: if two cards genuinely produce the same key (rare), a
    numeric suffix is appended to keep IDs unique within a single run.
    """
    base = f"{slugify(project)}__{date}__{slugify(activity_label)}__{slugify(start)}"
    n = _seen_ids.get(base, 0) + 1
    _seen_ids[base] = n
    return base if n == 1 else f"{base}--{n}"

def expand_row_to_cards(row, row_index):
    location_options = [s.strip() for s in (row.get("location") or "").split(",") if s.strip()]
    tokens = [s.strip() for s in (row.get("activity") or "").split(",") if s.strip()]
    base = {
        "rowIndex": row_index, "project": row.get("project"), "subproject": row.get("subproject"),
        "date": row.get("date"), "pax": row.get("pax"), "notes": row.get("notes"),
        "locationOptions": location_options, "isManual": False,
    }
    if len(tokens) <= 1:
        label = tokens[0] if tokens else (row.get("activity") or "Untitled")
        cid = make_stable_card_id(row.get("project"), row.get("date"), label, row.get("start"))
        return [{**base, "id": cid, "activityLabel": label, "category": categorize(label),
                 "start": row.get("start"), "end": row.get("end"), "needsReview": False}]

    times_by_token = parse_notes_for_token_times(row.get("notes"), tokens)
    if times_by_token:
        cards = []
        for tok in tokens:
            t = times_by_token[tok]
            cid = make_stable_card_id(row.get("project"), row.get("date"), tok, t["start"])
            cards.append({**base, "id": cid, "activityLabel": tok, "category": categorize(tok),
                          "start": t["start"], "end": t["end"], "needsReview": False})
        return cards

    cid = make_stable_card_id(row.get("project"), row.get("date"), row.get("activity"), row.get("start"))
    return [{**base, "id": cid, "activityLabel": row.get("activity"),
             "category": categorize(row.get("activity")), "start": row.get("start"), "end": row.get("end"),
             "needsReview": True}]


# ============================================================
# SMARTSHEET FETCH (real REST API — separate from the Claude connector)
# ============================================================
def fetch_smartsheet_report(report_id):
    url = f"https://api.smartsheet.com/2.0/reports/{report_id}"
    resp = requests.get(url, headers=SMARTSHEET_HEADERS, params={"pageSize": 10000})
    resp.raise_for_status()
    data = resp.json()

    # Reports (as opposed to plain sheets) can pull columns from multiple
    # source sheets, so they address columns via "virtualId"/"virtualColumnId"
    # rather than (or in addition to) the underlying sheet's own "id"/"columnId".
    # Register whichever keys are actually present rather than assuming one.
    columns = {}
    for c in data["columns"]:
        title = c.get("title")
        if "id" in c:
            columns[c["id"]] = title
        if "virtualId" in c:
            columns[c["virtualId"]] = title

    rows = []
    for row in data["rows"]:
        r = {}
        for cell in row.get("cells", []):
            col_id = cell.get("virtualColumnId", cell.get("columnId"))
            title = columns.get(col_id)
            if not title:
                continue
            r[title] = cell.get("displayValue", cell.get("value"))
        rows.append({
            "project":    r.get("PROJECT"),
            "start":      r.get("ACTIVITY START (00:00)"),
            "end":        r.get("ACTIVITY END (00:00)"),
            "date":       r.get("DATE"),
            "subproject": r.get("SUB PROJECT NAME"),
            "activity":   r.get("ACTIVITY"),
            "location":   r.get("LOCATION"),
            "notes":      r.get("NOTES"),
            "pax":        r.get("PAX"),
        })
    return rows


# ============================================================
# DEPUTY FETCH — CONFIRM FIELD NAMES AGAINST YOUR INSTALL FIRST
# (GET {DEPUTY_BASE}/resource/Roster/INFO will show the real fields)
# ============================================================
def unix_to_local_hhmm(unix_ts, date_iso_with_offset):
    """
    Deputy's Roster StartTime/EndTime are Unix timestamps, not "HH:MM"
    strings — confirmed against Deputy's own documented example (verified:
    a shift documented as 09:00-20:30 with an 11-hour paid total, matching
    an 11.5-hour span minus a 30-min meal break). Converts using the
    timezone offset embedded in that same record's Date field, rather than
    assuming a fixed offset, since AU daylight saving shifts it through the year.
    """
    if unix_ts is None:
        return None
    offset_str = date_iso_with_offset[-6:] if date_iso_with_offset else "+00:00"
    try:
        sign = 1 if offset_str[0] == '+' else -1
        oh, om = offset_str[1:].split(':')
        offset = timedelta(hours=sign * int(oh), minutes=sign * int(om))
    except (ValueError, IndexError):
        offset = timedelta(0)
    dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc) + offset
    return dt.strftime('%H:%M')


def fetch_deputy_operational_units():
    """BOH is set up as a 'Location' in Deputy's UI — in the API this is
    the OperationalUnit resource (confirmed: Deputy's own docs describe
    'In the API an Area is referred to as an Operational Unit')."""
    url = f"{DEPUTY_BASE}/resource/OperationalUnit"
    resp = requests.get(url, headers=DEPUTY_HEADERS)
    resp.raise_for_status()
    return resp.json()

_DASH_CHARS = ['\u2010', '\u2011', '\u2012', '\u2013', '\u2014', '\u2015', '\u2212', '-']

def normalize_for_match(s):
    """Collapses every dash-like unicode character (hyphen, en dash, em
    dash, minus sign, etc.) to a plain '-' and normalizes whitespace,
    so a Deputy location name matches regardless of which exact dash
    glyph shows up on either side — clipboard/editor/API round-trips
    have been observed to silently swap these."""
    s = (s or "").strip().lower()
    for d in _DASH_CHARS:
        s = s.replace(d, "-")
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def find_operational_unit_ids(target_names):
    """
    Returns {name: id} for every target_name that matches a real Deputy
    location. Any name that doesn't match anything is reported clearly
    rather than silently skipped or silently guessed — a missing role
    would mean real BOH staff quietly vanish from the dashboard.
    """
    units = fetch_deputy_operational_units()
    by_norm_name = {normalize_for_match(u.get("OperationalUnitName")): u["Id"] for u in units}
    print(f"  Found {len(units)} Deputy location(s) total")

    found = {}
    not_found = []
    for target in target_names:
        unit_id = by_norm_name.get(normalize_for_match(target))
        if unit_id is not None:
            found[target] = unit_id
        else:
            not_found.append(target)

    if not_found:
        print(f"  WARNING: could not find these BOH locations in Deputy — check spelling/dashes against the real names: {not_found}")
        print(f"  All Deputy location names found: {[u.get('OperationalUnitName') for u in units]}")

    if not found:
        sys.exit("None of the configured BOH location names matched anything in Deputy — check DEPUTY_BOH_LOCATION_NAMES in .env.")

    return found


def fetch_deputy_employees():
    """
    Builds an {employee_id: display_name} lookup. Deputy's Roster endpoint
    only returns a plain numeric employee ID, not a name — so we fetch the
    employee list separately and join them ourselves in Python.

    Defensive about field names since this hasn't been confirmed against a
    live install before now: tries several common shapes rather than
    assuming one, and falls back to a visible placeholder (never crashes)
    so a wrong guess shows up as "Employee #47" in the output — easy to
    spot and report — rather than as a silent mismatch or a crash.
    """
    url = f"{DEPUTY_BASE}/resource/Employee"
    resp = requests.get(url, headers=DEPUTY_HEADERS)
    resp.raise_for_status()
    employees = {}
    for item in resp.json():
        emp_id = item.get("Id") or item.get("id")
        if emp_id is None:
            continue
        name = (
            item.get("DisplayName")
            or item.get("Name")
            or " ".join(filter(None, [item.get("FirstName"), item.get("LastName")])).strip()
            or None
        )
        employees[emp_id] = name or f"Employee #{emp_id}"
    return employees

def fetch_deputy_shifts(start_date, end_date, boh_unit_ids):
    """
    boh_unit_ids: set of OperationalUnit IDs considered "BOH" for this
    dashboard. Filtering happens client-side (in Python) rather than via
    a Deputy query filter matching multiple values at once, since that
    query syntax hasn't been confirmed to exist — safer to pull the date
    range once and filter here than to guess at unverified API behaviour.
    """
    employees = fetch_deputy_employees()
    print(f"  Pulled {len(employees)} Deputy employee records")

    url = f"{DEPUTY_BASE}/resource/Roster/QUERY"
    payload = {
        "search": {
            "s1": {"field": "Date", "data": start_date, "type": "ge"},
            "s2": {"field": "Date", "data": end_date, "type": "le"},
        },
    }
    resp = requests.post(url, headers=DEPUTY_HEADERS, json=payload)
    resp.raise_for_status()
    all_items = resp.json()
    print(f"  Pulled {len(all_items)} total shifts across all locations before BOH filtering")

    shifts = []
    for item in all_items:
        if item.get("OperationalUnit") not in boh_unit_ids:
            continue
        raw_employee = item.get("Employee")
        if isinstance(raw_employee, dict):
            name = raw_employee.get("DisplayName") or raw_employee.get("Name") or "Unknown"
        else:
            name = employees.get(raw_employee, f"Employee #{raw_employee}")
        date_field = item.get("Date") or ""
        shifts.append({
            "employee": name,
            "date": date_field[:10] if date_field else None,
            "start": unix_to_local_hhmm(item.get("StartTime"), date_field),
            "end": unix_to_local_hhmm(item.get("EndTime"), date_field),
            "note": item.get("Comment") or item.get("ShiftNote") or "",
        })
    return shifts

def shift_id_for(shift):
    """Stable ID so a resolved unmatched shift stays resolved across syncs,
    even though the raw Deputy API doesn't give us its own shift ID here."""
    return f"{slugify(shift['employee'])}__{shift['date']}__{slugify(shift.get('start') or '')}"


# ============================================================
# MATCHING: Deputy shift -> Smartsheet project -> specific card(s)
# ============================================================
def normalize_name(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def find_project_in_note(note, known_projects):
    """Pass 1: substring containment against the known Smartsheet project list."""
    norm_note = normalize_name(note)
    for proj in known_projects:
        if normalize_name(proj) in norm_note:
            return proj, "high"
    # Pass 2: crude fuzzy fallback — token overlap ratio
    note_tokens = set(norm_note.split())
    best, best_score = None, 0
    for proj in known_projects:
        proj_tokens = set(normalize_name(proj).split())
        if not proj_tokens:
            continue
        overlap = len(note_tokens & proj_tokens) / len(proj_tokens)
        if overlap > best_score:
            best, best_score = proj, overlap
    if best_score >= 0.6:
        return best, "medium"
    return None, "none"

def to_minutes(t):
    if not t:
        return None
    parts = t.split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1] or 0)
    except (ValueError, IndexError):
        return None

def match_shifts_to_cards(shifts, cards, known_projects):
    """
    Returns:
      tech_assignments: {card_id: [employee names]}
      unmatched: [shift dicts that couldn't be confidently matched]
    """
    tech_assignments = {}
    unmatched = []

    cards_by_project_date = {}
    for c in cards:
        cards_by_project_date.setdefault((c["project"], c["date"]), []).append(c)

    for shift in shifts:
        project, confidence = find_project_in_note(shift["note"], known_projects)
        if not project or confidence == "none":
            unmatched.append({**shift, "shift_id": shift_id_for(shift), "reason": "no project match in shift note"})
            continue

        candidates = cards_by_project_date.get((project, shift["date"]), [])
        if not candidates:
            unmatched.append({**shift, "shift_id": shift_id_for(shift), "reason": f"matched project '{project}' but no card on {shift['date']}"})
            continue

        s_start, s_end = to_minutes(shift["start"]), to_minutes(shift["end"])
        matched_any = False
        for card in candidates:
            c_start, c_end = to_minutes(card["start"]), to_minutes(card["end"])
            overlaps = (
                s_start is not None and s_end is not None and
                c_start is not None and c_end is not None and
                s_start < c_end and s_end > c_start
            )
            if overlaps or confidence == "medium":
                tech_assignments.setdefault(card["id"], [])
                if shift["employee"] not in tech_assignments[card["id"]]:
                    tech_assignments[card["id"]].append(shift["employee"])
                matched_any = True

        if not matched_any:
            unmatched.append({**shift, "shift_id": shift_id_for(shift), "reason": f"matched project '{project}' but shift time didn't overlap any card"})

    return tech_assignments, unmatched


# ============================================================
# MAIN
# ============================================================
def main():
    print(f"[{datetime.now().isoformat()}] Starting sync...")

    rows = fetch_smartsheet_report(SMARTSHEET_REPORT_ID)
    print(f"  Pulled {len(rows)} rows from Smartsheet report {SMARTSHEET_REPORT_ID}")

    cards = []
    for i, row in enumerate(rows):
        cards.extend(expand_row_to_cards(row, i))
    print(f"  Expanded into {len(cards)} activity cards")

    known_projects = sorted(set(r["project"] for r in rows if r.get("project")))

    print(f"  Looking up {len(DEPUTY_BOH_LOCATION_NAMES)} configured BOH locations in Deputy...")
    boh_units = find_operational_unit_ids(DEPUTY_BOH_LOCATION_NAMES)
    print(f"  Matched {len(boh_units)}/{len(DEPUTY_BOH_LOCATION_NAMES)} BOH locations: {list(boh_units.keys())}")
    boh_unit_ids = set(boh_units.values())

    start_date = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=DEPUTY_LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
    shifts = fetch_deputy_shifts(start_date, end_date, boh_unit_ids)
    print(f"  {len(shifts)} of those shifts are BOH ({start_date} to {end_date})")

    tech_assignments, unmatched = match_shifts_to_cards(shifts, cards, known_projects)
    print(f"  Matched shifts onto {len(tech_assignments)} cards; {len(unmatched)} shifts need manual review")

    generated_at = datetime.now(timezone.utc).isoformat()
    write_to_database(cards, tech_assignments, unmatched, generated_at)
    print("  Wrote results to the Supabase database")


if __name__ == "__main__":
    main()
