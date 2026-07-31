-- AI-native marketplace metadata: transparent purchasing, versions and updates.
create table if not exists public.tool_passports (
  tool_id uuid primary key references public.tools(id) on delete cascade,
  data_handling text not null,
  execution text not null,
  license text not null,
  source_included boolean not null default false,
  commercial_use boolean not null default false,
  skill_level text not null,
  verified_on date,
  security_status text not null default 'pending',
  quality_score integer not null default 70 check (quality_score between 0 and 100),
  update_policy text not null,
  requirements text not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.tool_versions (
  id uuid primary key default gen_random_uuid(),
  tool_id uuid not null references public.tools(id) on delete cascade,
  version text not null,
  title text not null,
  detail text,
  released_at timestamptz not null default now(),
  unique(tool_id, version)
);

create table if not exists public.tool_update_followers (
  user_id uuid not null references public.profiles(id) on delete cascade,
  tool_id uuid not null references public.tools(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key(user_id, tool_id)
);

alter table public.tool_passports enable row level security;
alter table public.tool_versions enable row level security;
alter table public.tool_update_followers enable row level security;

create policy "passports are public" on public.tool_passports for select using (true);
create policy "versions are public" on public.tool_versions for select using (true);
create policy "owners manage passport" on public.tool_passports for all using (exists(select 1 from public.tools t where t.id=tool_id and t.author_id=auth.uid())) with check (exists(select 1 from public.tools t where t.id=tool_id and t.author_id=auth.uid()));
create policy "owners manage versions" on public.tool_versions for all using (exists(select 1 from public.tools t where t.id=tool_id and t.author_id=auth.uid())) with check (exists(select 1 from public.tools t where t.id=tool_id and t.author_id=auth.uid()));
create policy "users manage own follows" on public.tool_update_followers for all using (user_id=auth.uid()) with check (user_id=auth.uid());
