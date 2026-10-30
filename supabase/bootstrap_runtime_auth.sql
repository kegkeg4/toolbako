-- ONE-TIME bootstrap for the runtime-backed app, not the legacy marketplace schema.
-- Check the target project before running. Never apply schema.sql
-- or the old public commerce migrations on top of this bootstrap.
-- Existing auth users/profiles require a separate reviewed backfill/migration.
-- This file does not change posts, replies, storage, payments or Auth settings.
begin;

do $$ begin
  if to_regclass('public.profiles') is not null
     or to_regclass('public.creator_badges') is not null then
    raise exception 'Auth bootstrap refused: profile tables already exist';
  end if;
  if exists (select 1 from auth.users) then
    raise exception 'Auth bootstrap refused: existing users require a reviewed backfill';
  end if;
  if exists (select 1 from pg_trigger
             where tgrelid = 'auth.users'::regclass and not tgisinternal) then
    raise exception 'Auth bootstrap refused: existing Auth triggers require review';
  end if;
end $$;

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  username text unique not null check (username ~ '^[a-z0-9_]{3,30}$'),
  display_name text not null check (char_length(display_name) between 1 and 60),
  avatar_url text,
  bio text not null default '' check (char_length(bio) <= 600),
  headline text not null default '' check (char_length(headline) <= 80),
  skills text[] not null default '{}' check (cardinality(skills) <= 10),
  experience jsonb not null default '[]' check (jsonb_typeof(experience) = 'array'),
  portfolio jsonb not null default '[]' check (jsonb_typeof(portfolio) = 'array'),
  availability text not null default '受付状況未設定'
    check (availability in ('受付中','内容次第','満枠対応中','受付休止中','受付状況未設定')),
  response_time text not null default '未設定'
    check (response_time in ('1時間以内','6時間以内','12時間以内','24時間以内','2日以内','3日以内','未設定')),
  pricing_note text not null default '' check (char_length(pricing_note) <= 240),
  x_url text,
  website_url text,
  is_banned boolean not null default false,
  identity_status text not null default 'unverified'
    check (identity_status in ('unverified','pending','verified','rejected')),
  created_at timestamptz not null default now()
);

create table public.creator_badges (
  profile_id uuid primary key references public.profiles(id) on delete cascade,
  is_founding_member boolean not null default false,
  founding_member_since timestamptz,
  is_certified_creator boolean not null default false,
  certified_creator_since timestamptz,
  updated_at timestamptz not null default now()
);

alter table public.profiles enable row level security;
alter table public.creator_badges enable row level security;

-- The app reads its own profile at login, then renders creator pages from its
-- private runtime repository. Do not expose all Auth IDs through public APIs.
revoke all on public.profiles, public.creator_badges from public, anon, authenticated;
grant select on public.profiles, public.creator_badges to authenticated;
grant select, insert, update, delete on public.profiles, public.creator_badges to service_role;
create policy toolbako_read_own_profile on public.profiles for select to authenticated
  using (id = (select auth.uid()) and not is_banned);
create policy toolbako_read_own_badges on public.creator_badges for select to authenticated
  using (profile_id = (select auth.uid()) and exists (
    select 1 from public.profiles p where p.id = profile_id and not p.is_banned
  ));

-- Metadata is untrusted. Only use bounded display fields; never copy bans,
-- verification, badges, administrative flags, email addresses or secret tokens.
create function public.toolbako_create_profile()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  requested_username text;
  candidate text;
  display_label text;
  inserted_id uuid;
  attempt integer;
begin
  requested_username := lower(coalesce(new.raw_user_meta_data->>'user_name', ''));
  if requested_username !~ '^[a-z0-9_]{3,30}$' then
    requested_username := 'user_' || substr(replace(new.id::text, '-', ''), 1, 20);
  end if;
  display_label := left(btrim(regexp_replace(coalesce(
    new.raw_user_meta_data->>'display_name',
    new.raw_user_meta_data->>'full_name',
    new.raw_user_meta_data->>'name', requested_username
  ), '[[:cntrl:]]', '', 'g')), 60);
  if display_label = '' then display_label := requested_username; end if;

  -- A unique index, not a SELECT-before-INSERT, arbitrates concurrent signups.
  -- OAuth providers may supply the same user_name; bound fallback attempts.
  for attempt in 0..8 loop
    candidate := case when attempt = 0 then requested_username
      else left(requested_username, 21) || '_' ||
           substr(replace(gen_random_uuid()::text, '-', ''), 1, 8) end;
    insert into public.profiles(id, username, display_name)
      values (new.id, candidate, display_label)
      on conflict (username) do nothing returning id into inserted_id;
    if inserted_id is not null then return new; end if;
  end loop;
  raise exception 'Unable to allocate a unique profile username';
end $$;
revoke all on function public.toolbako_create_profile() from public, anon, authenticated;
create trigger toolbako_on_auth_user_created after insert on auth.users
  for each row execute function public.toolbako_create_profile();

notify pgrst, 'reload schema';
commit;
