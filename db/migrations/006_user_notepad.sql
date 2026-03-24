-- Per-user Notepad: one row per user, items stored as JSON array (same shape as memory/notes/{user}.json).
create table if not exists public.user_notepad (
  owner_user_id uuid primary key references auth.users (id) on delete cascade,
  items jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.user_notepad enable row level security;

-- No policies: backend uses service role only (same pattern as user_gmail_credentials).
