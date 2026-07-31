-- Marketplace finance ledger and controlled seller payouts.
-- 合同会社ONE receives marketplace payments, recognises its commission when
-- an order completes, and releases only the seller net amount for payout.

alter table public.orders
  add column if not exists payment_status text not null default 'pending',
  add column if not exists refund_status text,
  add column if not exists dispute_status text,
  add column if not exists seller_net integer,
  add column if not exists processor_fee integer not null default 0,
  add column if not exists stripe_checkout_session_id text,
  add column if not exists stripe_charge_id text,
  add column if not exists stripe_balance_transaction_id text,
  add column if not exists stripe_refund_id text,
  add column if not exists stripe_dispute_id text,
  add column if not exists stripe_transfer_id text,
  add column if not exists funds_available_at timestamptz;

update public.orders set seller_net=greatest(0,amount-platform_fee) where seller_net is null;
alter table public.orders alter column seller_net set not null;

alter table public.orders drop constraint if exists orders_financial_amounts_check;
alter table public.orders add constraint orders_financial_amounts_check
  check(amount>=0 and platform_fee>=0 and platform_fee<=amount and processor_fee>=0 and seller_net=amount-platform_fee);

do $$ begin
  alter table public.orders add constraint orders_payment_status_check
    check(payment_status in ('pending','paid','past_due','failed','expired','cancelled'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.orders add constraint orders_refund_status_check
    check(refund_status is null or refund_status in ('requested','processing','pending','partial','completed','failed'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.orders add constraint orders_dispute_status_check
    check(dispute_status is null or dispute_status in ('open','investigating','won','lost','closed'));
exception when duplicate_object then null; end $$;

create unique index if not exists orders_checkout_session_unique on public.orders(stripe_checkout_session_id) where stripe_checkout_session_id is not null;
create unique index if not exists orders_charge_unique on public.orders(stripe_charge_id) where stripe_charge_id is not null;
create unique index if not exists orders_balance_transaction_unique on public.orders(stripe_balance_transaction_id) where stripe_balance_transaction_id is not null;
create unique index if not exists orders_refund_unique on public.orders(stripe_refund_id) where stripe_refund_id is not null;
create unique index if not exists orders_dispute_unique on public.orders(stripe_dispute_id) where stripe_dispute_id is not null;
create unique index if not exists orders_transfer_unique on public.orders(stripe_transfer_id) where stripe_transfer_id is not null;

create table if not exists public.financial_ledger_entries (
  id uuid primary key default gen_random_uuid(),
  order_id uuid references public.orders(id) on delete restrict,
  seller_id uuid references public.profiles(id) on delete restrict,
  entry_type text not null check(entry_type in (
    'payment','platform_fee','processor_fee','seller_payable','seller_release','payout',
    'refund','dispute_hold','dispute_release','chargeback','adjustment'
  )),
  amount integer not null check(amount<>0),
  currency text not null default 'jpy' check(currency='jpy'),
  provider text not null default 'stripe',
  provider_reference text,
  idempotency_key text not null unique,
  description text check(char_length(description)<=500),
  metadata jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create index if not exists finance_ledger_order_idx on public.financial_ledger_entries(order_id, occurred_at);
create index if not exists finance_ledger_seller_idx on public.financial_ledger_entries(seller_id, occurred_at desc);
create index if not exists finance_ledger_provider_idx on public.financial_ledger_entries(provider, provider_reference) where provider_reference is not null;

create or replace function public.reject_financial_ledger_mutation()
returns trigger language plpgsql set search_path=public as $$
begin
  raise exception 'financial ledger entries are append-only';
end $$;

drop trigger if exists financial_ledger_immutable on public.financial_ledger_entries;
create trigger financial_ledger_immutable before update or delete on public.financial_ledger_entries
for each row execute function public.reject_financial_ledger_mutation();

alter table public.payouts drop constraint if exists payouts_status_check;
alter table public.payouts
  add column if not exists requested_by uuid references public.profiles(id),
  add column if not exists approved_by uuid references public.profiles(id),
  add column if not exists scheduled_for timestamptz,
  add column if not exists provider_payout_id text,
  add column if not exists idempotency_key text,
  add column if not exists failure_reason text,
  add column if not exists updated_at timestamptz not null default now();

do $$ begin
  alter table public.payouts add constraint payouts_status_check
    check(status in ('requested','approved','processing','paid','failed','cancelled'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.payouts add constraint payouts_two_person_check
    check(approved_by is null or requested_by is null or approved_by<>requested_by);
exception when duplicate_object then null; end $$;

create unique index if not exists payouts_provider_unique on public.payouts(provider_payout_id) where provider_payout_id is not null;
create unique index if not exists payouts_idempotency_unique on public.payouts(idempotency_key) where idempotency_key is not null;
create index if not exists payouts_operations_queue_idx on public.payouts(status, scheduled_for, requested_at);

create table if not exists public.payout_allocations (
  payout_id uuid not null references public.payouts(id) on delete restrict,
  order_id uuid not null references public.orders(id) on delete restrict,
  amount integer not null check(amount>0),
  created_at timestamptz not null default now(),
  primary key(payout_id,order_id)
);

create unique index if not exists payout_allocation_order_unique on public.payout_allocations(order_id);

create table if not exists public.seller_payout_controls (
  seller_id uuid primary key references public.profiles(id) on delete restrict,
  paused boolean not null default false,
  reason text check(char_length(reason)<=500),
  updated_by uuid references public.profiles(id),
  updated_at timestamptz not null default now()
);

alter table public.financial_ledger_entries enable row level security;
alter table public.payout_allocations enable row level security;
alter table public.seller_payout_controls enable row level security;

drop policy if exists "seller reads own ledger" on public.financial_ledger_entries;
create policy "seller reads own ledger" on public.financial_ledger_entries for select
  using(seller_id=auth.uid());

drop policy if exists "seller reads own payout allocations" on public.payout_allocations;
create policy "seller reads own payout allocations" on public.payout_allocations for select
  using(exists(select 1 from public.payouts p where p.id=payout_id and p.seller_id=auth.uid()));

drop policy if exists "seller reads own payout control" on public.seller_payout_controls;
create policy "seller reads own payout control" on public.seller_payout_controls for select
  using(seller_id=auth.uid());

-- Only trusted server/service-role code may create money movements or change
-- payout state. Sellers receive read access to their own records only.
revoke insert, update, delete on public.financial_ledger_entries from anon, authenticated;
revoke insert, update, delete on public.payout_allocations from anon, authenticated;
revoke insert, update, delete on public.seller_payout_controls from anon, authenticated;
revoke insert, update, delete on public.payouts from anon, authenticated;

comment on table public.financial_ledger_entries is 'Append-only marketplace money ledger; Stripe and order events are reconciled by idempotency_key.';
comment on table public.payout_allocations is 'Exact completed orders funded by each seller payout.';
comment on table public.seller_payout_controls is 'Operations hold used by the payout worker before calling Stripe.';
