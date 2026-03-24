-- Scheduled jobs for Scheduler feature
create table if not exists public.scheduled_jobs (
  id text primary key,
  owner_user_id uuid not null references auth.users(id) on delete cascade,
  owner_email text,
  name text not null default 'Scheduled Job',
  subject text not null,  -- jobs | weather | stocks | news | custom
  params jsonb not null default '{}'::jsonb,
  schedule_type text not null,  -- cron | interval
  cron_expr text,               -- e.g. "0 7 * * *" for 7am daily
  interval_minutes int,         -- for interval type
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_scheduled_jobs_owner
  on public.scheduled_jobs(owner_user_id);

alter table public.scheduled_jobs enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public' and tablename = 'scheduled_jobs' and policyname = 'Users can read own scheduled jobs'
  ) then
    create policy "Users can read own scheduled jobs"
      on public.scheduled_jobs
      for select
      using (auth.uid() = owner_user_id);
  end if;
end $$;
