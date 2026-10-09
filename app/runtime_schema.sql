-- Private compatibility repository for the existing marketplace domain model.
-- Financial facts below are normalized separately from the compatibility state.
create schema if not exists toolbako_runtime;
revoke all on schema toolbako_runtime from public;

create table if not exists toolbako_runtime.schema_version (
  singleton boolean primary key default true check (singleton),
  version integer not null
);

create table if not exists toolbako_runtime.marketplace_state (
  singleton boolean primary key default true check (singleton),
  revision bigint not null default 0 check (revision >= 0),
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  updated_at timestamptz not null default now()
);

create table if not exists toolbako_runtime.audit_events (
  id uuid primary key,
  actor_id text,
  action text not null,
  target text not null,
  request_id text not null,
  occurred_at timestamptz not null,
  content_hash text not null
);
create index if not exists runtime_audit_time on toolbako_runtime.audit_events (occurred_at desc);

create table if not exists toolbako_runtime.stripe_operations (
  operation_id text primary key,
  endpoint text not null,
  request_hash text not null,
  status text not null check (status in ('pending', 'succeeded', 'unknown', 'rejected')),
  response jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((status = 'succeeded') = (response is not null))
);
create index if not exists runtime_stripe_unresolved on toolbako_runtime.stripe_operations (created_at)
  where status in ('pending', 'unknown');

create table if not exists toolbako_runtime.email_outbox (
  id uuid primary key,
  recipient text not null,
  sender text not null,
  subject text not null,
  body text not null,
  status text not null default 'pending' check (status in ('pending','sending','sent','review')),
  attempts integer not null default 0,
  first_attempt_at timestamptz,
  available_at timestamptz not null default now(),
  lease_until timestamptz,
  claim_token uuid,
  created_at timestamptz not null default now(),
  sent_at timestamptz
);
create index if not exists runtime_email_queue on toolbako_runtime.email_outbox (available_at)
  where status in ('pending','sending');

create or replace function toolbako_runtime.reject_audit_mutation()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  raise exception 'audit events are append-only';
end $$;
drop trigger if exists runtime_audit_immutable on toolbako_runtime.audit_events;
create trigger runtime_audit_immutable before update or delete or truncate
  on toolbako_runtime.audit_events for each statement
  execute function toolbako_runtime.reject_audit_mutation();

create table if not exists toolbako_runtime.finance_receipts (
  payment_intent text primary key,
  order_id text not null,
  seller_id text not null,
  amount bigint not null check (amount > 0),
  fee bigint not null check (fee >= 0 and fee <= amount),
  currency text not null default 'jpy' check (currency = 'jpy'),
  created_at timestamptz not null default now()
);
create table if not exists toolbako_runtime.finance_payouts (
  id uuid primary key,
  seller_id text not null,
  account_id text not null,
  amount bigint not null check (amount > 0),
  fee bigint not null check (fee >= 0),
  net_amount bigint not null check (net_amount > 0 and net_amount + fee = amount),
  status text not null check (status in ('requested','transferring','awaiting_funds','bank_pending','paid','held','review','bank_failed','reversing','cancelled')),
  scheduled_for timestamptz not null,
  request_key text not null,
  provider_reference text unique,
  content_hash text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists runtime_payout_queue on toolbako_runtime.finance_payouts(status, scheduled_for);
create unique index if not exists runtime_payout_request on toolbako_runtime.finance_payouts(seller_id,request_key);
create table if not exists toolbako_runtime.finance_allocations (
  id uuid primary key,
  payout_id uuid not null references toolbako_runtime.finance_payouts(id),
  receipt_id text not null references toolbako_runtime.finance_receipts(payment_intent),
  amount bigint not null check (amount > 0),
  transfer_amount bigint not null check (transfer_amount >= 0 and transfer_amount <= amount),
  unique(payout_id, receipt_id)
);
create table if not exists toolbako_runtime.finance_entries (
  id text primary key,
  kind text not null,
  amount bigint not null check (amount <> 0),
  receipt_id text references toolbako_runtime.finance_receipts(payment_intent),
  payout_id uuid references toolbako_runtime.finance_payouts(id),
  provider_reference text,
  content_hash text not null,
  created_at timestamptz not null default now()
);
-- The payout/receipt unique index covers payout_id, not receipt_id. Keep all
-- financial foreign keys indexed for joins and parent-row checks.
create index if not exists runtime_allocation_receipt on toolbako_runtime.finance_allocations(receipt_id);
create index if not exists runtime_entry_receipt on toolbako_runtime.finance_entries(receipt_id);
create index if not exists runtime_entry_payout on toolbako_runtime.finance_entries(payout_id);
do $$ declare table_name text; begin
  foreach table_name in array array['finance_receipts','finance_allocations','finance_entries'] loop
    execute format('drop trigger if exists immutable_finance on toolbako_runtime.%I', table_name);
    execute format('create trigger immutable_finance before update or delete or truncate on toolbako_runtime.%I for each statement execute function toolbako_runtime.reject_audit_mutation()', table_name);
  end loop;
end $$;

-- No browser/anonymous access, even if this schema is accidentally exposed.
alter table toolbako_runtime.schema_version enable row level security;
alter table toolbako_runtime.marketplace_state enable row level security;
alter table toolbako_runtime.audit_events enable row level security;
alter table toolbako_runtime.stripe_operations enable row level security;
alter table toolbako_runtime.email_outbox enable row level security;
alter table toolbako_runtime.finance_receipts enable row level security;
alter table toolbako_runtime.finance_payouts enable row level security;
alter table toolbako_runtime.finance_allocations enable row level security;
alter table toolbako_runtime.finance_entries enable row level security;
revoke all on all tables in schema toolbako_runtime from public;
do $$ declare role_name text; begin
  foreach role_name in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = role_name) then
      execute format('revoke all on schema toolbako_runtime from %I', role_name);
      execute format('revoke all on all tables in schema toolbako_runtime from %I', role_name);
    end if;
  end loop;
end $$;
insert into toolbako_runtime.schema_version(singleton, version) values (true, 2)
  on conflict (singleton) do update set version=2 where toolbako_runtime.schema_version.version <= 2;
