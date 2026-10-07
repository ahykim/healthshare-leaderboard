-- Pushup challenge schema.
--
-- The Flask app connects to Postgres directly (psycopg) with a server-side
-- credential, so nothing here is exposed to the Data API: RLS is enabled with
-- no policies, and anon/authenticated have no table privileges.

create extension if not exists citext with schema extensions;

create table public.users (
  name  extensions.citext primary key,
  photo text
);

create table public.pushups (
  id         bigint generated always as identity primary key,
  name       extensions.citext not null
             references public.users (name) on update cascade on delete cascade,
  day        date not null,
  count      integer not null check (count > 0),
  created_at timestamptz not null default now()
);

create index pushups_name_day_idx on public.pushups (name, day);

alter table public.users   enable row level security;
alter table public.pushups enable row level security;

revoke all on public.users   from anon, authenticated;
revoke all on public.pushups from anon, authenticated;

-- Profile photos: public bucket (objects readable by URL), writes only via the
-- server's secret key, which bypasses RLS. Photos are 256x256 JPEGs.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('avatars', 'avatars', true, 1048576, array['image/jpeg'])
on conflict (id) do nothing;
