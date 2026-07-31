create extension if not exists pgcrypto;

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  username text unique not null check (username ~ '^[a-zA-Z0-9_]{3,30}$'),
  display_name text not null check (char_length(display_name) <= 60),
  avatar_url text, bio text check (char_length(bio) <= 200), x_url text, website_url text,
  is_banned boolean not null default false, identity_status text not null default 'unverified' check(identity_status in ('unverified','pending','verified','rejected')), created_at timestamptz not null default now()
);
create table public.creator_badges (
  profile_id uuid primary key references public.profiles(id) on delete cascade,
  is_founding_member boolean not null default false,
  founding_member_since timestamptz,
  is_certified_creator boolean not null default false,
  certified_creator_since timestamptz,
  updated_at timestamptz not null default now()
);
create index creator_badges_certified_idx on public.creator_badges(is_certified_creator) where is_certified_creator;
create index creator_badges_founding_idx on public.creator_badges(is_founding_member) where is_founding_member;
create table public.tools (
  id uuid primary key default gen_random_uuid(), slug text unique not null,
  author_id uuid not null references public.profiles(id), name text not null check (char_length(name)<=60),
  tagline text not null check (char_length(tagline)<=100), description_md text not null check (char_length(description_md)<=5000),
  category text not null, price_type text not null check (price_type in ('free','paid','consultation')),
  price integer not null default 0 check (price >= 0 and price <= 1000000),
  external_pay_url text, demo_url text, distribution text not null check (distribution in ('webapp','download','github','prompt')),
  file_path text, thumbnail_path text, ogp_image_path text, ai_used text[] not null default '{}',
  view_count integer not null default 0 check (view_count>=0), like_count integer not null default 0 check (like_count>=0),
  is_published boolean not null default true, is_removed boolean not null default false,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table public.tool_screenshots (id uuid primary key default gen_random_uuid(), tool_id uuid not null references public.tools(id) on delete cascade, image_path text not null, sort_order integer default 0);
create table public.tags (id bigint generated always as identity primary key, name text unique not null check (char_length(name)<=30));
create table public.tool_tags (tool_id uuid references public.tools(id) on delete cascade, tag_id bigint references public.tags(id) on delete cascade, primary key(tool_id,tag_id));
create table public.likes (user_id uuid references public.profiles(id) on delete cascade, tool_id uuid references public.tools(id) on delete cascade, created_at timestamptz default now(), primary key(user_id,tool_id));
create table public.comments (id uuid primary key default gen_random_uuid(), tool_id uuid not null references public.tools(id) on delete cascade, user_id uuid not null references public.profiles(id), body text not null check(char_length(body)<=500), is_removed boolean default false, created_at timestamptz default now());
create table public.reports (id uuid primary key default gen_random_uuid(), reporter_id uuid not null references public.profiles(id), target_type text not null check(target_type in ('tool','comment')), target_id uuid not null, reason text check(char_length(reason)<=500), status text default 'open' check(status in ('open','resolved')), created_at timestamptz default now());
create table public.view_logs (tool_id uuid references public.tools(id) on delete cascade, session_hash text not null, viewed_at timestamptz default now(), primary key(tool_id,session_hash));
create table public.curations (id bigint generated always as identity primary key, slot text unique not null, tool_id uuid references public.tools(id), admin_comment text, starts_at timestamptz default now(), ends_at timestamptz);
create table public.orders (id uuid primary key default gen_random_uuid(), tool_id uuid references public.tools(id), buyer_id uuid references public.profiles(id), seller_id uuid references public.profiles(id), amount integer not null, platform_fee integer not null, stripe_payment_intent text, status text default 'pending' check(status in ('pending','in_progress','awaiting_acceptance','cancel_pending','completed','cancelled')), revision_count integer not null default 0, cancel_reason text, created_at timestamptz default now(), updated_at timestamptz default now());
create table public.conversations (
  id uuid primary key default gen_random_uuid(), tool_id uuid not null references public.tools(id) on delete cascade,
  buyer_id uuid not null references public.profiles(id), seller_id uuid not null references public.profiles(id),
  created_at timestamptz default now(), updated_at timestamptz default now(), unique(tool_id,buyer_id,seller_id)
);
create table public.messages (
  id uuid primary key default gen_random_uuid(), conversation_id uuid not null references public.conversations(id) on delete cascade,
  sender_id uuid not null references public.profiles(id), body text not null check(char_length(body) between 1 and 1000),
  read_at timestamptz, created_at timestamptz default now()
);
create table public.proposals (
  id uuid primary key default gen_random_uuid(), conversation_id uuid not null references public.conversations(id) on delete cascade,
  seller_id uuid not null references public.profiles(id), title text not null, detail text not null,
  amount integer not null check(amount between 100 and 1000000), delivery_days integer not null check(delivery_days between 1 and 120),
  status text not null default 'open' check(status in ('open','purchased','declined','expired')), expires_at timestamptz default now()+interval '7 days', created_at timestamptz default now()
);
create table public.order_messages (id uuid primary key default gen_random_uuid(), order_id uuid not null references public.orders(id) on delete cascade, sender_id uuid not null references public.profiles(id), body text not null check(char_length(body) between 1 and 1000), created_at timestamptz default now());
create table public.deliveries (id uuid primary key default gen_random_uuid(), order_id uuid not null references public.orders(id) on delete cascade, version integer not null default 1, note text not null, file_path text, created_at timestamptz default now());
create table public.reviews (id uuid primary key default gen_random_uuid(), order_id uuid unique not null references public.orders(id), tool_id uuid not null references public.tools(id), reviewer_id uuid not null references public.profiles(id), rating integer not null check(rating between 1 and 5), comment text check(char_length(comment)<=500), is_anonymous boolean default false, created_at timestamptz default now());
create table public.payouts (id uuid primary key default gen_random_uuid(), seller_id uuid not null references public.profiles(id), amount integer not null check(amount>0), status text not null default 'requested' check(status in ('requested','processing','paid','failed')), requested_at timestamptz default now(), paid_at timestamptz);
create table public.tool_options (id uuid primary key default gen_random_uuid(), tool_id uuid not null references public.tools(id) on delete cascade, name text not null, price integer not null check(price between 100 and 1000000), is_active boolean default true, sort_order integer default 0);
create table public.job_requests (id uuid primary key default gen_random_uuid(), owner_id uuid not null references public.profiles(id), title text not null, category text not null, detail text not null, budget_min integer not null, budget_max integer not null, deadline date not null, status text default 'open' check(status in ('open','closed','contracted','cancelled')), created_at timestamptz default now());
create table public.job_applications (id uuid primary key default gen_random_uuid(), request_id uuid not null references public.job_requests(id) on delete cascade, applicant_id uuid not null references public.profiles(id), message text not null, amount integer not null, delivery_days integer not null, status text default 'submitted' check(status in ('submitted','selected','declined','withdrawn')), created_at timestamptz default now(), unique(request_id,applicant_id));
create table public.notifications (id uuid primary key default gen_random_uuid(), user_id uuid not null references public.profiles(id) on delete cascade, title text not null, body text, url text, read_at timestamptz, created_at timestamptz default now());
create index tools_public_created_idx on public.tools(is_published,is_removed,created_at desc);
create index tools_category_idx on public.tools(category);
create index likes_created_idx on public.likes(created_at desc);
create index view_logs_viewed_idx on public.view_logs(viewed_at desc);

create or replace function public.handle_new_user() returns trigger language plpgsql security definer set search_path=public as $$
declare base text; candidate text;
begin
  base := regexp_replace(lower(coalesce(new.raw_user_meta_data->>'user_name', split_part(new.email,'@',1), 'user')), '[^a-z0-9_]', '', 'g');
  if length(base)<3 then base := 'user_' || substr(new.id::text,1,8); end if;
  candidate := base;
  while exists(select 1 from profiles where username=candidate) loop candidate := base || '_' || substr(gen_random_uuid()::text,1,4); end loop;
  insert into profiles(id,username,display_name,avatar_url,x_url) values(new.id,candidate,coalesce(new.raw_user_meta_data->>'full_name',new.raw_user_meta_data->>'name',candidate),new.raw_user_meta_data->>'avatar_url',case when new.raw_user_meta_data->>'user_name' is not null then 'https://x.com/'||(new.raw_user_meta_data->>'user_name') end);
  return new;
end $$;
create trigger on_auth_user_created after insert on auth.users for each row execute procedure public.handle_new_user();

create or replace function public.sync_like_count() returns trigger language plpgsql security definer set search_path=public as $$ begin
  update tools set like_count=(select count(*) from likes where tool_id=coalesce(new.tool_id,old.tool_id)) where id=coalesce(new.tool_id,old.tool_id); return coalesce(new,old); end $$;
create trigger likes_counter after insert or delete on public.likes for each row execute procedure public.sync_like_count();

create or replace function public.register_view(p_tool_id uuid,p_session_hash text) returns boolean language plpgsql security definer set search_path=public as $$
begin
  if exists(select 1 from view_logs where tool_id=p_tool_id and session_hash=p_session_hash and viewed_at>now()-interval '24 hours') then return false; end if;
  insert into view_logs(tool_id,session_hash) values(p_tool_id,p_session_hash) on conflict(tool_id,session_hash) do update set viewed_at=now();
  update tools set view_count=view_count+1 where id=p_tool_id; return true;
end $$;

alter table public.profiles enable row level security; alter table public.creator_badges enable row level security; alter table public.tools enable row level security;
alter table public.tool_screenshots enable row level security; alter table public.tags enable row level security;
alter table public.tool_tags enable row level security; alter table public.likes enable row level security;
alter table public.comments enable row level security; alter table public.reports enable row level security;
alter table public.view_logs enable row level security; alter table public.curations enable row level security;
alter table public.orders enable row level security;
alter table public.conversations enable row level security; alter table public.messages enable row level security;
alter table public.proposals enable row level security; alter table public.order_messages enable row level security;
alter table public.deliveries enable row level security; alter table public.reviews enable row level security; alter table public.payouts enable row level security;
alter table public.tool_options enable row level security; alter table public.job_requests enable row level security; alter table public.job_applications enable row level security; alter table public.notifications enable row level security;

create policy "public profiles" on public.profiles for select using(not is_banned);
create policy "own profile update" on public.profiles for update using(auth.uid()=id) with check(auth.uid()=id);
create policy "public creator badges" on public.creator_badges for select using(exists(select 1 from public.profiles p where p.id=profile_id and not p.is_banned));
grant select on public.creator_badges to anon, authenticated;
revoke insert, update, delete on public.creator_badges from anon, authenticated;
create policy "public tools" on public.tools for select using(is_published and not is_removed);
create policy "author creates tools" on public.tools for insert with check(auth.uid()=author_id and not exists(select 1 from profiles where id=auth.uid() and is_banned));
create policy "author updates tools" on public.tools for update using(auth.uid()=author_id) with check(auth.uid()=author_id);
create policy "public screenshots" on public.tool_screenshots for select using(exists(select 1 from tools where id=tool_id and is_published and not is_removed));
create policy "author screenshots" on public.tool_screenshots for all using(exists(select 1 from tools where id=tool_id and author_id=auth.uid())) with check(exists(select 1 from tools where id=tool_id and author_id=auth.uid()));
create policy "public tags" on public.tags for select using(true); create policy "public tool tags" on public.tool_tags for select using(true);
create policy "own likes read" on public.likes for select using(auth.uid()=user_id); create policy "own likes insert" on public.likes for insert with check(auth.uid()=user_id); create policy "own likes delete" on public.likes for delete using(auth.uid()=user_id);
create policy "public comments" on public.comments for select using(not is_removed); create policy "own comments" on public.comments for insert with check(auth.uid()=user_id); create policy "own comments delete" on public.comments for delete using(auth.uid()=user_id);
create policy "own reports" on public.reports for insert with check(auth.uid()=reporter_id); create policy "own report read" on public.reports for select using(auth.uid()=reporter_id);
create policy "order participants read" on public.orders for select using(auth.uid() in (buyer_id,seller_id));
create policy "conversation participants read" on public.conversations for select using(auth.uid() in (buyer_id,seller_id));
create policy "buyer starts conversation" on public.conversations for insert with check(auth.uid()=buyer_id);
create policy "participants update conversation" on public.conversations for update using(auth.uid() in (buyer_id,seller_id));
create policy "participants read messages" on public.messages for select using(exists(select 1 from public.conversations c where c.id=conversation_id and auth.uid() in (c.buyer_id,c.seller_id)));
create policy "participants send messages" on public.messages for insert with check(auth.uid()=sender_id and exists(select 1 from public.conversations c where c.id=conversation_id and auth.uid() in (c.buyer_id,c.seller_id)));
create policy "proposal participants read" on public.proposals for select using(exists(select 1 from public.conversations c where c.id=conversation_id and auth.uid() in (c.buyer_id,c.seller_id)));
create policy "seller creates proposal" on public.proposals for insert with check(auth.uid()=seller_id and exists(select 1 from public.conversations c where c.id=conversation_id and c.seller_id=auth.uid()));
create policy "order participants read messages" on public.order_messages for select using(exists(select 1 from public.orders o where o.id=order_id and auth.uid() in (o.buyer_id,o.seller_id)));
create policy "order participants send messages" on public.order_messages for insert with check(auth.uid()=sender_id and exists(select 1 from public.orders o where o.id=order_id and auth.uid() in (o.buyer_id,o.seller_id)));
create policy "order participants read deliveries" on public.deliveries for select using(exists(select 1 from public.orders o where o.id=order_id and auth.uid() in (o.buyer_id,o.seller_id)));
create policy "seller creates delivery" on public.deliveries for insert with check(exists(select 1 from public.orders o where o.id=order_id and o.seller_id=auth.uid()));
create policy "public reviews" on public.reviews for select using(true); create policy "buyer creates review" on public.reviews for insert with check(auth.uid()=reviewer_id and exists(select 1 from public.orders o where o.id=order_id and o.buyer_id=auth.uid() and o.status='completed'));
create policy "seller payouts read" on public.payouts for select using(auth.uid()=seller_id); create policy "verified seller requests payout" on public.payouts for insert with check(auth.uid()=seller_id and exists(select 1 from public.profiles p where p.id=auth.uid() and p.identity_status='verified'));
create policy "public active options" on public.tool_options for select using(is_active); create policy "tool author options" on public.tool_options for all using(exists(select 1 from public.tools t where t.id=tool_id and t.author_id=auth.uid())) with check(exists(select 1 from public.tools t where t.id=tool_id and t.author_id=auth.uid()));
create policy "public open requests" on public.job_requests for select using(status='open'); create policy "owner requests" on public.job_requests for all using(auth.uid()=owner_id) with check(auth.uid()=owner_id);
create policy "request parties applications" on public.job_applications for select using(auth.uid()=applicant_id or exists(select 1 from public.job_requests r where r.id=request_id and r.owner_id=auth.uid())); create policy "applicant submits" on public.job_applications for insert with check(auth.uid()=applicant_id);
create policy "own notifications" on public.notifications for select using(auth.uid()=user_id); create policy "own notifications update" on public.notifications for update using(auth.uid()=user_id);

insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types) values
('images','images',true,10485760,array['image/jpeg','image/png','image/webp']),
('tool-files','tool-files',false,52428800,null) on conflict(id) do nothing;
create policy "public images" on storage.objects for select using(bucket_id='images');
create policy "user image uploads" on storage.objects for insert to authenticated with check(bucket_id='images' and (storage.foldername(name))[1]=auth.uid()::text);
create policy "user image updates" on storage.objects for update to authenticated using(bucket_id='images' and owner_id=auth.uid()::text);
create policy "user tool files" on storage.objects for all to authenticated using(bucket_id='tool-files' and owner_id=auth.uid()::text) with check(bucket_id='tool-files' and (storage.foldername(name))[1]=auth.uid()::text);

-- Apply migrations/20260715_ai_market.sql and
-- migrations/20260715_marketplace_operations.sql and
-- migrations/20260715_operations_and_compliance.sql and finally
-- migrations/20260715_security_hardening.sql and
-- migrations/20260715_marketplace_parity.sql after this base schema.
-- Apply migrations/20260719_creator_badges.sql and
-- migrations/20260722_financial_ledger.sql for existing databases.
