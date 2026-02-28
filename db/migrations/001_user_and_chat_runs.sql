-- Supabase persistence schema for user-scoped run/chat history.

create table if not exists public.app_users (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

create table if not exists public.chat_runs (
  run_id text primary key,
  owner_user_id uuid not null references auth.users(id) on delete cascade,
  owner_email text,
  query text not null,
  status text not null,
  session_id text,
  summary jsonb not null default '{}'::jsonb,
  latest_snapshot jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_chat_runs_owner_created_at
  on public.chat_runs(owner_user_id, created_at desc);

create index if not exists idx_chat_runs_status
  on public.chat_runs(status);

-- Optional RLS (recommended even with service-role backend)
alter table public.app_users enable row level security;
alter table public.chat_runs enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public' and tablename = 'app_users' and policyname = 'Users can read own profile'
  ) then
    create policy "Users can read own profile"
      on public.app_users
      for select
      using (auth.uid() = id);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public' and tablename = 'chat_runs' and policyname = 'Users can read own runs'
  ) then
    create policy "Users can read own runs"
      on public.chat_runs
      for select
      using (auth.uid() = owner_user_id);
  end if;
end $$;
