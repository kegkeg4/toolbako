-- Transaction lifecycle, blind mutual reviews and business documents.
-- Apply after 20260715_security_hardening.sql.

alter table public.profiles
  add column if not exists headline text check (char_length(headline) <= 80),
  add column if not exists skills text[] not null default '{}',
  add column if not exists experience jsonb not null default '[]'::jsonb,
  add column if not exists portfolio jsonb not null default '[]'::jsonb,
  add column if not exists availability text not null default '受付状況未設定',
  add column if not exists response_time text not null default '未設定',
  add column if not exists pricing_note text check (char_length(pricing_note) <= 240);

alter table public.tools
  add column if not exists estimated_delivery_days integer not null default 1 check (estimated_delivery_days between 1 and 120),
  add column if not exists purchase_notes text check (char_length(purchase_notes) <= 2000),
  add column if not exists faq jsonb not null default '[]'::jsonb;

alter table public.notifications
  add column if not exists category text not null default 'general'
  check (category in ('general','messages','transactions','requests','updates','security'));

create table if not exists public.notification_preferences (
  user_id uuid primary key references public.profiles(id) on delete cascade,
  messages boolean not null default true,
  transactions boolean not null default true,
  requests boolean not null default true,
  updates boolean not null default true,
  updated_at timestamptz not null default now()
);

create table if not exists public.favorite_folders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 30),
  created_at timestamptz not null default now(),
  unique(user_id, name)
);

create table if not exists public.favorite_assignments (
  user_id uuid not null references public.profiles(id) on delete cascade,
  tool_id uuid not null references public.tools(id) on delete cascade,
  folder_id uuid not null references public.favorite_folders(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key(user_id, tool_id),
  foreign key(user_id, tool_id) references public.likes(user_id, tool_id) on delete cascade
);

alter table public.notification_preferences enable row level security;
alter table public.favorite_folders enable row level security;
alter table public.favorite_assignments enable row level security;

drop policy if exists "own notification preferences" on public.notification_preferences;
create policy "own notification preferences" on public.notification_preferences for all
using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "own favorite folders" on public.favorite_folders;
create policy "own favorite folders" on public.favorite_folders for all
using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "own favorite assignments" on public.favorite_assignments;
create policy "own favorite assignments" on public.favorite_assignments for all
using (auth.uid() = user_id) with check (
  auth.uid() = user_id and exists (
    select 1 from public.favorite_folders folder
    where folder.id = folder_id and folder.user_id = auth.uid()
  )
);

create index if not exists favorite_folders_user_created_idx on public.favorite_folders(user_id, created_at);
create index if not exists favorite_assignments_folder_idx on public.favorite_assignments(folder_id);

alter table public.orders
  add column if not exists accepted_at timestamptz,
  add column if not exists completed_at timestamptz,
  add column if not exists auto_close_at timestamptz,
  add column if not exists room_closes_at timestamptz,
  add column if not exists room_closed_at timestamptz,
  add column if not exists review_deadline timestamptz,
  add column if not exists buyer_reviewed boolean not null default false;

alter table public.reviews
  add column if not exists private_dimensions jsonb not null default '{}'::jsonb,
  add column if not exists published boolean not null default false;

create table if not exists public.buyer_reviews (
  id uuid primary key default gen_random_uuid(),
  order_id uuid unique not null references public.orders(id) on delete cascade,
  buyer_id uuid not null references public.profiles(id),
  author_id uuid not null references public.profiles(id),
  rating integer not null check (rating between 1 and 5),
  comment text check (char_length(comment) <= 500),
  private_dimensions jsonb not null default '{}'::jsonb,
  published boolean not null default false,
  created_at timestamptz not null default now()
);

alter table public.buyer_reviews enable row level security;

drop policy if exists "buyer review parties read" on public.buyer_reviews;
create policy "buyer review parties read" on public.buyer_reviews for select
using (auth.uid() in (buyer_id, author_id));

drop policy if exists "seller creates buyer review" on public.buyer_reviews;
create policy "seller creates buyer review" on public.buyer_reviews for insert
with check (
  auth.uid() = author_id and exists (
    select 1 from public.orders o
    where o.id = order_id and o.seller_id = auth.uid() and o.buyer_id = buyer_id and o.status = 'completed'
  )
);

create index if not exists orders_auto_close_idx on public.orders(auto_close_at)
where status = 'awaiting_acceptance';
create index if not exists orders_room_close_idx on public.orders(room_closes_at)
where status = 'completed' and room_closed_at is null;
create index if not exists orders_review_deadline_idx on public.orders(review_deadline)
where status = 'completed';
create index if not exists buyer_reviews_buyer_created_idx on public.buyer_reviews(buyer_id, created_at desc);

revoke update (published, private_dimensions) on public.reviews from authenticated;
revoke update (published, private_dimensions) on public.buyer_reviews from authenticated;

-- Newly added, user-authored profile and listing fields remain editable, while
-- trust/moderation fields continue to be service-role only.
grant update (headline, skills, experience, portfolio, availability, response_time, pricing_note) on public.profiles to authenticated;
grant update (estimated_delivery_days, purchase_notes, faq) on public.tools to authenticated;
grant insert (estimated_delivery_days, purchase_notes, faq) on public.tools to authenticated;

-- Conversations are created by the application server, which derives the
-- seller from the selected tool. Browser clients must not rewrite participants.
drop policy if exists "buyer starts conversation" on public.conversations;
drop policy if exists "participants update conversation" on public.conversations;
revoke insert, update, delete on public.conversations from authenticated;
revoke insert, update, delete on public.messages from authenticated;
revoke insert, update, delete on public.proposals from authenticated;

-- Blind reviews and their private dimensions are never exposed through the
-- base table. Public pages read only this safe, published-column view.
drop policy if exists "public reviews" on public.reviews;
drop policy if exists "buyer creates review" on public.reviews;
revoke select, insert, update, delete on public.reviews from anon, authenticated;
create or replace view public.public_reviews with (security_barrier=true) as
  select id, order_id, tool_id, reviewer_id, rating, comment, is_anonymous, created_at
  from public.reviews where published = true;
revoke all on public.public_reviews from public;
grant select on public.public_reviews to anon, authenticated;
revoke select, insert, update, delete on public.buyer_reviews from anon, authenticated;

-- Public metadata must disappear together with a paused/removed parent tool.
drop policy if exists "passports are public" on public.tool_passports;
create policy "published passports are public" on public.tool_passports for select using (
  exists(select 1 from public.tools t where t.id=tool_id and t.is_published and not t.is_removed)
);
drop policy if exists "versions are public" on public.tool_versions;
create policy "published versions are public" on public.tool_versions for select using (
  exists(select 1 from public.tools t where t.id=tool_id and t.is_published and not t.is_removed)
);
drop policy if exists "public comments" on public.comments;
create policy "published tool comments are public" on public.comments for select using (
  not is_removed and exists(select 1 from public.tools t where t.id=tool_id and t.is_published and not t.is_removed)
);

-- Enforce request marketplace boundaries in the database as well as FastAPI.
alter table public.job_requests drop constraint if exists job_requests_budget_check;
alter table public.job_requests add constraint job_requests_budget_check check (
  budget_min between 1000 and 10000000 and budget_max between budget_min and 10000000
);
alter table public.job_applications drop constraint if exists job_applications_terms_check;
alter table public.job_applications add constraint job_applications_terms_check check (
  amount between 100 and 10000000 and delivery_days between 1 and 120 and char_length(message) between 10 and 1000
);

-- Production webhook consumers claim `processing`, apply effects and transition
-- to `processed` in one Postgres transaction. Failed attempts remain retryable.
alter table public.webhook_events drop constraint if exists webhook_events_status_check;
alter table public.webhook_events add constraint webhook_events_status_check check(status in ('processing','processed','failed','ignored'));
alter table public.webhook_events add column if not exists attempts integer not null default 1;
alter table public.webhook_events add column if not exists last_error text;
alter table public.webhook_events add column if not exists processed_at timestamptz;
revoke all on public.webhook_events from public, anon, authenticated;
revoke all on public.audit_logs from public, anon, authenticated;
