"""db.py — shared Postgres (Supabase) connection helper.

Both app.py (the web API) and sync_precinct_dashboard.py (the scheduled sync)
talk to the same Supabase Postgres database through here, so there is exactly
one place that knows how to connect.

Set DATABASE_URL to your Supabase connection string. For the web app running on
Vercel (many short-lived serverless calls) use the *pooler* connection string —
the one Supabase labels "Transaction" (port 6543). For the GitHub Actions sync,
either the pooler or the direct connection works.
"""
import os
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if real environment variables are set another way

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_connection():
    """Open a new Postgres connection whose rows behave like dicts, matching
    how the old sqlite3.Row rows were used (row["column"] and dict(row))."""
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at your Supabase Postgres "
            "connection string (Supabase dashboard -> Project Settings -> Database)."
        )
    # prepare_threshold=None disables implicit prepared statements, which keeps
    # us compatible with Supabase's transaction-mode connection pooler (Supavisor).
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)


def run_schema(conn, schema_path):
    """Run schema.sql. psycopg's normal execute path won't run several
    semicolon-separated statements in one call, so we strip comments and run
    each statement individually. Every statement is idempotent (IF NOT EXISTS),
    so this is safe to call repeatedly."""
    with open(schema_path) as f:
        sql = re.sub(r"--[^\n]*", "", f.read())  # drop line comments
    for statement in (s.strip() for s in sql.split(";")):
        if statement:
            conn.execute(statement)
    conn.commit()
