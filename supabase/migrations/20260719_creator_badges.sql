-- Operator-issued creator credentials live outside profiles so account owners
-- cannot self-grant them through the normal profile update policy.
create table if not exists public.creator_badges (
  profile_id uuid primary key references public.profiles(id) on delete cascade,
  is_founding_member boolean not null default false,
  founding_member_since timestamptz,
  is_certified_creator boolean not null default false,
  certified_creator_since timestamptz,
  updated_at timestamptz not null default now()
);

create index if not exists creator_badges_certified_idx on public.creator_badges(is_certified_creator) where is_certified_creator;
create index if not exists creator_badges_founding_idx on public.creator_badges(is_founding_member) where is_founding_member;

alter table public.creator_badges enable row level security;
drop policy if exists "public creator badges" on public.creator_badges;
create policy "public creator badges" on public.creator_badges
  for select
  using(exists(select 1 from public.profiles p where p.id=profile_id and not p.is_banned));

grant select on public.creator_badges to anon, authenticated;
revoke insert, update, delete on public.creator_badges from anon, authenticated;
