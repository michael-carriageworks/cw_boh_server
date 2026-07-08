-- Cards: Smartsheet-derived activity cards (is_manual=0) plus producer-added
-- ad hoc events (is_manual=1). resolved_location is deliberately preserved
-- across syncs — see the ON CONFLICT clause in the sync script.
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    project TEXT,
    subproject TEXT,
    date TEXT NOT NULL,
    start TEXT,
    end TEXT,
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
CREATE TABLE IF NOT EXISTS tech_assignments (
    card_id TEXT NOT NULL,
    tech_name TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('deputy','manual')),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (card_id, tech_name, source)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    end TEXT,
    note TEXT,
    reason TEXT,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
