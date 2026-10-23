-- Report editing (2026-10, Nelson: "start a report and finish it later").
-- Run once in the Supabase SQL editor. Also folded into schema.sql. Safe to re-run.

alter table reports add column if not exists updated_at timestamptz;

-- Stamped by the database on every edit (not trusted from the page).
create or replace function touch_report_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists reports_touch_updated_at on reports;
create trigger reports_touch_updated_at before update on reports
  for each row execute function touch_report_updated_at();

-- DEMO MODE (current live state, while Nelson is the only user): anyone with the link can
-- edit any report, same as the public read policy. Goes away with the LOCKDOWN step once
-- scout logins exist - see the LOCKDOWN note on the reports policies in schema.sql.
drop policy if exists "reports editable by anyone" on reports;
drop policy if exists "reports editable by authenticated users" on reports;
create policy "reports editable by anyone" on reports
  for update using (true) with check (true);

-- LOCKDOWN MODE (not yet applied): only signed-in accounts may edit.
--   drop policy if exists "reports editable by anyone" on reports;
--   create policy "reports editable by authenticated users" on reports
--     for update using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');
