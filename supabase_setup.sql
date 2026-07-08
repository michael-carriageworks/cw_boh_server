-- supabase_setup.sql — one-time configuration, run AFTER schema.sql.
--
-- Two jobs:
--   1. Turn on "realtime" for the dashboard tables, so every device is notified
--      the instant a row changes (this is what makes updates feel instant).
--   2. Set row-level-security (RLS) access rules.
--
-- SECURITY NOTE (read this): the dashboard currently has no login, so the
-- policies below let anyone with the public page read the schedule data — which
-- is exactly what the dashboard already shows on screen, so it exposes nothing
-- extra. Before this is ever opened to the wider internet, we should add real
-- authentication. The private `deputy_tokens` table is deliberately locked down
-- and readable only by the backend, never by the browser.
--
-- Everything here is written to be safe to run more than once.

-- ---- Realtime: broadcast row changes on the dashboard tables ----
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'cards','tech_assignments','tasks','unmatched_shifts','notification_log','meta'
  ] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_publication_tables
      WHERE pubname = 'supabase_realtime' AND schemaname = 'public' AND tablename = t
    ) THEN
      EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I', t);
    END IF;
  END LOOP;
END $$;

-- ---- Row-level security ----
-- Dashboard tables: enable RLS and allow read (needed for the browser to receive
-- realtime change events). All writes still go through the backend, never the browser.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'cards','tech_assignments','tasks','unmatched_shifts','notification_log','meta'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS "read for realtime" ON public.%I', t);
    EXECUTE format('CREATE POLICY "read for realtime" ON public.%I FOR SELECT TO anon, authenticated USING (true)', t);
  END LOOP;
END $$;

-- Private token table: RLS on, and NO policy — so the anon/public key can never
-- read it. The backend connects with the service role, which bypasses RLS.
ALTER TABLE public.deputy_tokens ENABLE ROW LEVEL SECURITY;
