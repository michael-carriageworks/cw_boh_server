-- schema.sql — Postgres (Supabase) schema for the BOH Technician Dashboard.
--
-- Ported from the original SQLite schema. Notable differences:
--   * "end" is a reserved word in Postgres, so that one column is quoted.
--   * INTEGER PRIMARY KEY AUTOINCREMENT -> GENERATED ALWAYS AS IDENTITY.
--   * Boolean-ish flags stay as INTEGER (0/1) to keep the Python untouched.
-- Every statement is idempotent (IF NOT EXISTS) so it is safe to run repeatedly,
-- exactly like the original — either the app, the sync job, or a one-time paste
-- into the Supabase SQL editor can initialise an empty database.

-- Cards: Smartsheet-derived activity cards (is_manual=0) plus producer-added
-- ad hoc events (is_manual=1). resolved_location is deliberately preserved
-- across syncs — see the ON CONFLICT clause in the sync script.
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    project TEXT,
    subproject TEXT,
    date TEXT NOT NULL,
    start TEXT,
    "end" TEXT,
    activity_label TEXT,
    category_key TEXT,
    category_label TEXT,
    category_color TEXT,
    pax TEXT,
    notes TEXT,
    location_options TEXT,      -- JSON array, e.g. ["BAY 21","BAY 22-24"]
    resolved_location TEXT,     -- producer's pick, or auto-filled if only one option
    is_manual INTEGER NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 0
);

-- Tech assignments: separate table since a card can have multiple techs,
-- and we need to distinguish auto-matched-from-Deputy vs producer-added.
-- role: 'senior' (Senior Technician), 'fohm' (FOH Manager), or 'tech' (everyone
-- else). The cage display shows only seniors + FOHMs; producers see all.
-- shift_start/shift_end: the person's rostered shift times ("HH:MM"), used to
-- work out who is on duty right now vs taking over later.
CREATE TABLE IF NOT EXISTS tech_assignments (
    card_id TEXT NOT NULL,
    tech_name TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('deputy','manual')),
    assigned_at TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'tech',
    shift_start TEXT,
    shift_end TEXT,
    PRIMARY KEY (card_id, tech_name, source)
);

CREATE TABLE IF NOT EXISTS tasks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    day TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    tech TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done')),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

-- Deputy shifts the auto-matcher couldn't confidently place. `resolved` is
-- preserved across syncs so a producer's manual link doesn't get undone
-- just because the same shift shows up again on the next Deputy pull.
CREATE TABLE IF NOT EXISTS unmatched_shifts (
    shift_id TEXT PRIMARY KEY,
    employee TEXT,
    date TEXT,
    start TEXT,
    "end" TEXT,
    note TEXT,
    reason TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'tech'
);

-- Column upgrades for databases created before these fields existed.
-- ADD COLUMN IF NOT EXISTS makes them safe to run every time.
ALTER TABLE tech_assignments ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'tech';
ALTER TABLE tech_assignments ADD COLUMN IF NOT EXISTS shift_start TEXT;
ALTER TABLE tech_assignments ADD COLUMN IF NOT EXISTS shift_end TEXT;
ALTER TABLE unmatched_shifts ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'tech';

CREATE TABLE IF NOT EXISTS notification_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Deputy OAuth token store. Holds the single JSON blob that used to live in
-- deputy_token_store.json on Michael's Mac. Kept in the database so the
-- GitHub Actions sync — which has no persistent filesystem — can refresh and
-- persist a rotated Deputy refresh token across runs. This table is private:
-- it is NOT added to the realtime publication and NOT exposed to the dashboard.
CREATE TABLE IF NOT EXISTS deputy_tokens (
    id INTEGER PRIMARY KEY,
    data TEXT NOT NULL
);
