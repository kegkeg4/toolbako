-- Trust, seller operations, recurring commerce and post-purchase library.
alter table public.tools add column if not exists status text not null default 'published' check(status in ('draft','published','paused'));
alter table public.tools add column if not exists capacity integer not null default 5 check(capacity between 1 and 50);
alter table public.tools add column if not exists support_days integer not null default 7 check(support_days between 0 and 365);
alter table public.tools add column if not exists supports_subscription boolean not null default false;
alter table public.tools add column if not exists subscription_price integer not null default 0 check(subscription_price between 0 and 1000000);
alter table public.tools add column if not exists safety_status text not null default 'pending' check(safety_status in ('pending','passed','failed'));
alter table public.orders add column if not exists billing_type text not null default 'one_time' check(billing_type in ('one_time','subscription'));
alter table public.orders add column if not exists license_key text unique;
alter table public.orders add column if not exists receipt_no text unique;
alter table public.reviews add column if not exists private_dimensions jsonb not null default '{}'::jsonb;
alter table public.proposals add column if not exists revision integer not null default 1;

create table if not exists public.identity_applications (id uuid primary key default gen_random_uuid(), user_id uuid not null references public.profiles(id) on delete cascade, legal_name text not null, birth_date date not null, document_type text not null, status text not null default 'pending' check(status in ('pending','verified','rejected')), rejection_reason text, provider_reference text, submitted_at timestamptz not null default now(), reviewed_at timestamptz);
create table if not exists public.nda_signatures (user_id uuid primary key references public.profiles(id) on delete cascade, version text not null, signed_at timestamptz not null default now(), ip_hash text);
create table if not exists public.creator_follows (follower_id uuid references public.profiles(id) on delete cascade, creator_id uuid references public.profiles(id) on delete cascade, created_at timestamptz not null default now(), primary key(follower_id,creator_id), check(follower_id<>creator_id));
create table if not exists public.subscriptions (id uuid primary key default gen_random_uuid(), order_id uuid not null references public.orders(id), buyer_id uuid not null references public.profiles(id), tool_id uuid not null references public.tools(id), amount integer not null check(amount>0), provider_subscription_id text, status text not null default 'active' check(status in ('active','past_due','cancelled')), next_billing_at timestamptz, created_at timestamptz not null default now(), cancelled_at timestamptz);
create table if not exists public.order_extra_payments (id uuid primary key default gen_random_uuid(), order_id uuid not null references public.orders(id) on delete cascade, buyer_id uuid not null references public.profiles(id), amount integer not null check(amount between 100 and 100000), note text check(char_length(note)<=200), provider_payment_intent text, created_at timestamptz not null default now());
create table if not exists public.account_security (user_id uuid primary key references public.profiles(id) on delete cascade, two_factor_enabled boolean not null default false, login_alerts boolean not null default true, updated_at timestamptz not null default now());

alter table public.identity_applications enable row level security;
alter table public.nda_signatures enable row level security;
alter table public.creator_follows enable row level security;
alter table public.subscriptions enable row level security;
alter table public.order_extra_payments enable row level security;
alter table public.account_security enable row level security;
create policy "own identity applications" on public.identity_applications for select using(user_id=auth.uid());
create policy "submit own identity application" on public.identity_applications for insert with check(user_id=auth.uid());
create policy "own nda signature" on public.nda_signatures for all using(user_id=auth.uid()) with check(user_id=auth.uid());
create policy "public creator follow counts" on public.creator_follows for select using(true);
create policy "manage own creator follows" on public.creator_follows for all using(follower_id=auth.uid()) with check(follower_id=auth.uid());
create policy "own subscriptions" on public.subscriptions for select using(buyer_id=auth.uid());
create policy "order parties read extra payments" on public.order_extra_payments for select using(exists(select 1 from public.orders o where o.id=order_id and auth.uid() in (o.buyer_id,o.seller_id)));
create policy "buyer adds extra payment" on public.order_extra_payments for insert with check(buyer_id=auth.uid() and exists(select 1 from public.orders o where o.id=order_id and o.buyer_id=auth.uid()));
create policy "own account security" on public.account_security for all using(user_id=auth.uid()) with check(user_id=auth.uid());
