-- Per-user Gmail OAuth tokens (Mail UI uses the signed-in user's mailbox).
create table if not exists public.user_gmail_credentials (
  owner_user_id uuid primary key references auth.users (id) on delete cascade,
  google_email text,
  credentials_json jsonb not null,
  updated_at timestamptz not null default now()
);

create index if not exists idx_user_gmail_credentials_updated
  on public.user_gmail_credentials (updated_at desc);

alter table public.user_gmail_credentials enable row level security;

-- No policies: only the backend (service role) reads/writes. Users never query this table from the browser.
