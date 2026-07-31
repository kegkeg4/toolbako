-- Three sales paths: repeatable purchase, customization quote and exclusive transfer.
-- Apply after schema.sql and all 20260715 migrations.

alter table public.tools
  add column if not exists customization_available boolean not null default true,
  add column if not exists exclusive_available boolean not null default false,
  add column if not exists transfer_review_status text not null default 'not_requested',
  add column if not exists exclusive_price_min integer not null default 0,
  add column if not exists transfer_assets text[] not null default '{}',
  add column if not exists tech_stack text not null default '',
  add column if not exists monthly_revenue integer not null default 0,
  add column if not exists monthly_profit integer not null default 0,
  add column if not exists monthly_cost integer not null default 0,
  add column if not exists weekly_ops_hours numeric(5,1) not null default 0,
  add column if not exists handover_days integer not null default 14,
  add column if not exists exclusive_summary text not null default '';

update public.tools
set transfer_review_status = 'pending'
where exclusive_available and transfer_review_status = 'not_requested';

alter table public.tools drop constraint if exists tools_hybrid_sales_check;
alter table public.tools add constraint tools_hybrid_sales_check check (
  exclusive_price_min between 0 and 100000000
  and monthly_revenue between 0 and 100000000
  and monthly_profit between 0 and monthly_revenue
  and monthly_cost between 0 and 100000000
  and weekly_ops_hours between 0 and 168
  and handover_days between 1 and 180
  and transfer_review_status in ('not_requested','pending','approved','rejected')
  and (not exclusive_available or transfer_review_status <> 'not_requested')
  and transfer_assets <@ array['source_code','deployment_docs','domain','brand_assets','accounts','customer_data']::text[]
  and (
    not exclusive_available or (
      exclusive_price_min between 10000 and 100000000
      and cardinality(transfer_assets) >= 1
      and char_length(tech_stack) between 2 and 300
      and char_length(exclusive_summary) between 20 and 1000
    )
  )
);

grant update (
  customization_available, exclusive_available, exclusive_price_min,
  transfer_assets, tech_stack, monthly_revenue, monthly_profit, monthly_cost,
  weekly_ops_hours, handover_days, exclusive_summary
) on public.tools to authenticated;
grant insert (
  customization_available, exclusive_available, exclusive_price_min,
  transfer_assets, tech_stack, monthly_revenue, monthly_profit, monthly_cost,
  weekly_ops_hours, handover_days, exclusive_summary
) on public.tools to authenticated;

-- transfer_review_status is intentionally absent from authenticated grants.
-- Only the trusted application/admin role can publish a high-value transfer.

alter table public.conversations
  add column if not exists conversation_type text not null default 'general'
  check (conversation_type in ('general','customization','transfer'));
alter table public.conversations drop constraint if exists conversations_tool_id_buyer_id_seller_id_key;
alter table public.conversations drop constraint if exists conversations_sales_path_key;
alter table public.conversations add constraint conversations_sales_path_key
  unique(tool_id,buyer_id,seller_id,conversation_type);

create table if not exists public.transfer_inquiries (
  id uuid primary key default gen_random_uuid(),
  tool_id uuid not null references public.tools(id) on delete restrict,
  buyer_id uuid not null references public.profiles(id) on delete restrict,
  seller_id uuid not null references public.profiles(id) on delete restrict,
  conversation_id uuid unique references public.conversations(id) on delete set null,
  offer_amount integer not null check(offer_amount between 10000 and 100000000),
  intended_use text not null check(char_length(intended_use) between 10 and 500),
  message text not null check(char_length(message) between 20 and 1000),
  status text not null default 'nda_pending'
    check(status in ('nda_pending','reviewing','negotiating','declined','closed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check(buyer_id <> seller_id)
);

create unique index if not exists transfer_inquiries_one_active_per_buyer_tool
  on public.transfer_inquiries(tool_id,buyer_id)
  where status in ('nda_pending','reviewing','negotiating');
create index if not exists transfer_inquiries_seller_status_updated_idx
  on public.transfer_inquiries(seller_id,status,updated_at desc);
create index if not exists transfer_inquiries_buyer_updated_idx
  on public.transfer_inquiries(buyer_id,updated_at desc);

create table if not exists public.transfer_nda_acceptances (
  id uuid primary key default gen_random_uuid(),
  inquiry_id uuid not null references public.transfer_inquiries(id) on delete cascade,
  user_id uuid not null references public.profiles(id) on delete restrict,
  signer_role text not null check(signer_role in ('buyer','seller')),
  document_version text not null,
  document_hash text not null check(char_length(document_hash)=64),
  ip_hash text,
  signed_at timestamptz not null default now(),
  unique(inquiry_id,user_id)
);
create index if not exists transfer_nda_inquiry_signed_idx
  on public.transfer_nda_acceptances(inquiry_id,signed_at);

alter table public.transfer_inquiries enable row level security;
alter table public.transfer_nda_acceptances enable row level security;
create policy "transfer parties read cases" on public.transfer_inquiries for select
  using(auth.uid() in (buyer_id,seller_id));
create policy "transfer parties read nda acceptances" on public.transfer_nda_acceptances for select
  using(exists(
    select 1 from public.transfer_inquiries i
    where i.id=inquiry_id and auth.uid() in (i.buyer_id,i.seller_id)
  ));

-- Creation, status transitions and notifications must be one server transaction.
-- Browser clients cannot forge participants, amounts, status or NDA readiness.
revoke insert, update, delete on public.transfer_inquiries from anon, authenticated;
revoke insert, update, delete on public.transfer_nda_acceptances from anon, authenticated;
grant select on public.transfer_inquiries to authenticated;
grant select on public.transfer_nda_acceptances to authenticated;

comment on table public.transfer_inquiries is
  'Pre-contract exclusive-transfer cases. Contract signing and escrow remain external provider responsibilities.';
