-- Player flags (2026-10, Nelson): his own colored flags ("+ Add color" - he picks the color
-- and names it), one flag per player, filterable on the Players page.
-- Run once in the Supabase SQL editor. Also folded into schema.sql. Safe to re-run.

create table if not exists flag_types (
  id uuid primary key default gen_random_uuid(),
  label text not null,
  color text not null check (color ~ '^#[0-9a-fA-F]{6}$'),
  created_at timestamptz default now()
);

-- No foreign key to players on purpose: a player whose row is replaced (e.g. at the season
-- rollover) shouldn't silently lose his flag - an orphaned flag just doesn't show, and can be
-- moved to the new player_id.
create table if not exists player_flags (
  player_id text primary key,
  flag_id uuid not null references flag_types(id) on delete cascade,
  updated_at timestamptz default now()
);

alter table flag_types enable row level security;
alter table player_flags enable row level security;

-- DEMO MODE (current live state, while Nelson is the only user): anyone with the link can
-- read and change flags, like reports. LOCKDOWN (once logins exist): swap "using (true)" /
-- "with check (true)" for auth.role() = 'authenticated' - or per-user flags, Nelson's call.
drop policy if exists "flag_types open to anyone" on flag_types;
create policy "flag_types open to anyone" on flag_types
  for all using (true) with check (true);

drop policy if exists "player_flags open to anyone" on player_flags;
create policy "player_flags open to anyone" on player_flags
  for all using (true) with check (true);
