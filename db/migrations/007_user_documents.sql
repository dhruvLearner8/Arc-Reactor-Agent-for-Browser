-- 007_user_documents.sql
-- Per-user document RAG: uploaded documents, their chunks + embeddings.

create extension if not exists vector;

create table if not exists public.user_documents (
  id uuid primary key default gen_random_uuid(),
  owner_user_id uuid not null references auth.users (id) on delete cascade,
  filename text not null,
  file_type text not null check (file_type in ('pdf', 'txt', 'md')),
  status text not null default 'processing'
    check (status in ('processing', 'ready', 'failed')),
  chunk_count int not null default 0,
  error_message text,
  created_at timestamptz not null default now()
);

create index if not exists user_documents_owner_idx
  on public.user_documents (owner_user_id);

create table if not exists public.document_chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references public.user_documents (id) on delete cascade,
  owner_user_id uuid not null references auth.users (id) on delete cascade,
  chunk_index int not null,
  section_title text,
  page_number int,
  content text not null,
  embedding vector(768) not null,
  created_at timestamptz not null default now()
);

create index if not exists document_chunks_owner_idx
  on public.document_chunks (owner_user_id);

create index if not exists document_chunks_embedding_idx
  on public.document_chunks using hnsw (embedding vector_cosine_ops);

alter table public.user_documents enable row level security;
alter table public.document_chunks enable row level security;
-- No policies: backend uses service role only (same pattern as
-- user_notepad / user_gmail_credentials in prior migrations).

create or replace function public.match_document_chunks(
  query_embedding vector(768),
  match_owner uuid,
  match_count int default 5
)
returns table (
  chunk_id uuid,
  document_id uuid,
  filename text,
  section_title text,
  page_number int,
  content text,
  similarity float
)
language sql stable
as $$
  select
    c.id, c.document_id, d.filename, c.section_title, c.page_number,
    c.content,
    1 - (c.embedding <=> query_embedding) as similarity
  from public.document_chunks c
  join public.user_documents d on d.id = c.document_id
  where c.owner_user_id = match_owner
  order by c.embedding <=> query_embedding
  limit match_count
$$;
