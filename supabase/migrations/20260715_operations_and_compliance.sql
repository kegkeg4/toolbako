-- Operations console, disputes, audit trail, consent and account lifecycle.
create table if not exists public.support_cases (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references public.profiles(id),
  order_id uuid references public.orders(id), category text not null,
  subject text not null check(char_length(subject) between 3 and 100), detail text not null check(char_length(detail) between 20 and 2000),
  priority text not null default 'normal' check(priority in ('normal','high','urgent')),
  status text not null default 'open' check(status in ('open','investigating','resolved','rejected')),
  assigned_admin_id uuid references public.profiles(id), created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create unique index if not exists one_open_dispute_per_order on public.support_cases(order_id) where order_id is not null and status in ('open','investigating');

create table if not exists public.audit_logs (
  id bigint generated always as identity primary key, actor_id uuid references public.profiles(id),
  action text not null, target_type text, target_id text, request_id text,
  metadata jsonb not null default '{}'::jsonb, created_at timestamptz not null default now()
);
create index if not exists audit_logs_created_idx on public.audit_logs(created_at desc);

create table if not exists public.account_deletion_requests (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references public.profiles(id),
  status text not null default 'scheduled' check(status in ('scheduled','cancelled','completed','blocked')),
  requested_at timestamptz not null default now(), delete_after timestamptz not null, completed_at timestamptz
);
create unique index if not exists one_active_deletion_per_user on public.account_deletion_requests(user_id) where status='scheduled';

create table if not exists public.webhook_events (
  provider text not null, event_id text not null, event_type text not null,
  status text not null default 'processed' check(status in ('processed','failed','ignored')),
  received_at timestamptz not null default now(), payload_hash text, primary key(provider,event_id)
);

create table if not exists public.legal_consents (
  user_id uuid not null references public.profiles(id) on delete cascade,
  document_type text not null check(document_type in ('terms','privacy','nda')),
  document_version text not null, consented_at timestamptz not null default now(),
  primary key(user_id,document_type,document_version)
);

create table if not exists public.delivery_files (
  id uuid primary key default gen_random_uuid(), delivery_id uuid not null references public.deliveries(id) on delete cascade,
  storage_path text not null unique, original_name text not null, size_bytes bigint not null check(size_bytes between 1 and 50000000),
  sha256 text not null, scan_status text not null default 'pending' check(scan_status in ('pending','passed','failed')),
  scan_engine text, scanned_at timestamptz, created_at timestamptz not null default now()
);

alter table public.support_cases enable row level security;
alter table public.audit_logs enable row level security;
alter table public.account_deletion_requests enable row level security;
alter table public.webhook_events enable row level security;
alter table public.legal_consents enable row level security;
alter table public.delivery_files enable row level security;

create policy "users read own cases" on public.support_cases for select using(user_id=auth.uid());
create policy "users create own cases" on public.support_cases for insert with check(user_id=auth.uid());
create policy "users read own deletion" on public.account_deletion_requests for select using(user_id=auth.uid());
create policy "users request own deletion" on public.account_deletion_requests for insert with check(user_id=auth.uid());
create policy "users read own consents" on public.legal_consents for select using(user_id=auth.uid());
create policy "users create own consents" on public.legal_consents for insert with check(user_id=auth.uid());
create policy "order parties read delivery files" on public.delivery_files for select using(scan_status='passed' and exists(select 1 from public.deliveries d join public.orders o on o.id=d.order_id where d.id=delivery_id and auth.uid() in (o.buyer_id,o.seller_id)));
create policy "seller creates delivery files" on public.delivery_files for insert with check(exists(select 1 from public.deliveries d join public.orders o on o.id=d.order_id where d.id=delivery_id and o.seller_id=auth.uid()));

-- audit_logs and webhook_events intentionally have no client policies.
-- Only server-side service-role operations may access them.
