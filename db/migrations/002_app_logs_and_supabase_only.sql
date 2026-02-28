-- Admin/system logs persisted in Supabase for operational visibility.

create table if not exists public.app_logs (
  id bigserial primary key,
  level text not null,
  event_type text not null,
  message text not null,
  owner_user_id uuid,
  run_id text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_app_logs_created_at
  on public.app_logs(created_at desc);

create index if not exists idx_app_logs_owner
  on public.app_logs(owner_user_id, created_at desc);

create index if not exists idx_app_logs_run_id
  on public.app_logs(run_id, created_at desc);
