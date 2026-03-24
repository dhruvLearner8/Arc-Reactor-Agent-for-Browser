alter table public.scheduled_jobs
  add column if not exists last_run_id text,
  add column if not exists last_run_at timestamptz;
