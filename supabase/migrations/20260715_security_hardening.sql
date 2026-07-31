-- Prevent authenticated clients from promoting their own trust, moderation or KYC state.
-- Service-role operations retain full access and are the only path for verification updates.

revoke update on public.profiles from authenticated;
grant update (username, display_name, avatar_url, bio, x_url, website_url) on public.profiles to authenticated;

revoke update on public.tools from authenticated;
grant update (
  name, tagline, description_md, category, price_type, price, external_pay_url,
  demo_url, distribution, thumbnail_path, capacity,
  support_days, supports_subscription, subscription_price, updated_at
) on public.tools to authenticated;

-- New listings always enter a server-reviewed draft state. Column privileges
-- prevent a browser from supplying moderation, visibility or counter fields.
alter table public.tools alter column is_published set default false;
alter table public.tools alter column status set default 'draft';
drop policy if exists "author creates tools" on public.tools;
create policy "author creates safe draft tools" on public.tools for insert
  with check (
    auth.uid()=author_id
    and status='draft' and not is_published and not is_removed
    and safety_status='pending' and view_count=0 and like_count=0
    and not exists(select 1 from public.profiles where id=auth.uid() and is_banned)
  );
revoke insert on public.tools from authenticated;
grant insert (
  slug, author_id, name, tagline, description_md, category, price_type, price,
  demo_url, distribution, thumbnail_path, ai_used, capacity, support_days,
  supports_subscription, subscription_price, created_at, updated_at
) on public.tools to authenticated;

drop policy if exists "owners manage passport" on public.tool_passports;
create policy "owners create passport" on public.tool_passports for insert
  with check (exists(select 1 from public.tools t where t.id=tool_id and t.author_id=auth.uid()) and security_status='pending' and quality_score=70 and verified_on is null);
create policy "owners update passport" on public.tool_passports for update
  using (exists(select 1 from public.tools t where t.id=tool_id and t.author_id=auth.uid()))
  with check (exists(select 1 from public.tools t where t.id=tool_id and t.author_id=auth.uid()));
revoke update on public.tool_passports from authenticated;
grant update (data_handling, execution, license, source_included, commercial_use, skill_level, update_policy, requirements, updated_at) on public.tool_passports to authenticated;

drop policy if exists "submit own identity application" on public.identity_applications;
create policy "submit pending identity application" on public.identity_applications for insert
  with check (
    user_id=auth.uid() and status='pending' and rejection_reason is null
    and provider_reference is null and reviewed_at is null
  );
revoke update on public.identity_applications from authenticated;

-- Payout amount and state depend on the server-computed available balance.
-- Verification alone is not sufficient authorization to create a payout row.
drop policy if exists "verified seller requests payout" on public.payouts;
revoke insert, update, delete on public.payouts from authenticated;

-- Applications can only target an open request owned by someone else.
drop policy if exists "applicant submits" on public.job_applications;
create policy "applicant submits to open request" on public.job_applications for insert
  with check (
    auth.uid()=applicant_id and status='submitted'
    and exists(select 1 from public.job_requests r where r.id=request_id and r.status='open' and r.owner_id<>auth.uid())
  );

-- Index the highest-volume marketplace lookups used by dashboards and feeds.
create index if not exists orders_buyer_created_idx on public.orders(buyer_id, created_at desc);
create index if not exists orders_seller_status_created_idx on public.orders(seller_id, status, created_at desc);
create index if not exists messages_conversation_created_idx on public.messages(conversation_id, created_at);
create index if not exists notifications_user_unread_idx on public.notifications(user_id, read_at, created_at desc);

revoke execute on function public.register_view(uuid,text) from public, anon, authenticated;

-- Commerce, security and malware-scan facts are server-owned. A browser must
-- never be able to fabricate a paid extra, MFA flag or passed delivery scan.
drop policy if exists "buyer adds extra payment" on public.order_extra_payments;
revoke insert, update, delete on public.order_extra_payments from authenticated;

drop policy if exists "own account security" on public.account_security;
drop policy if exists "read own account security" on public.account_security;
create policy "read own account security" on public.account_security for select using(user_id=auth.uid());
revoke insert, update, delete on public.account_security from authenticated;

drop policy if exists "seller creates delivery files" on public.delivery_files;
revoke insert, update, delete on public.delivery_files from authenticated;

drop policy if exists "own nda signature" on public.nda_signatures;
create policy "read own nda signature" on public.nda_signatures for select using(user_id=auth.uid());
create policy "sign own nda once" on public.nda_signatures for insert with check(user_id=auth.uid());
revoke update, delete on public.nda_signatures from authenticated;

drop policy if exists "users create own cases" on public.support_cases;
create policy "users create valid own cases" on public.support_cases for insert with check(
  user_id=auth.uid() and status='open' and assigned_admin_id is null
  and (order_id is null or exists(select 1 from public.orders o where o.id=order_id and auth.uid() in (o.buyer_id,o.seller_id)))
);

drop policy if exists "users request own deletion" on public.account_deletion_requests;
create policy "users request scheduled own deletion" on public.account_deletion_requests for insert with check(
  user_id=auth.uid() and status='scheduled' and completed_at is null and delete_after >= now() + interval '7 days'
);

alter table public.subscriptions drop constraint if exists subscriptions_status_check;
alter table public.subscriptions add constraint subscriptions_status_check check(status in ('pending','active','past_due','cancelled','expired'));
