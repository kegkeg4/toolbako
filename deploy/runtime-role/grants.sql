-- OPERATOR PREPARATION ONLY; no data changes, credentials, global default grants,
-- ownership changes, PUBLIC revokes, RLS disabling or SECURITY DEFINER helpers.
-- Existing public/inherited excess privileges must be separately reviewed.
-- verify_runtime_role fails closed until these effective privileges are safe.
-- Roles must already exist from roles.sql. Exact privilege/policy readback is
-- mandatory before enabling login or changing Railway's sealed runtime secret.
begin;
set local lock_timeout='3s';
set local statement_timeout='10s';
do $$ begin
  if not exists(select 1 from toolbako_runtime.schema_version where singleton and version=2)
     or not exists(select 1 from toolbako_runtime.marketplace_state where singleton)
     or not exists(select 1 from toolbako_runtime.stripe_operations
       where operation_id='toolbako-financial-scope-v1' and endpoint='internal/financial-scope-v1'
         and status='succeeded' and response->>'mode'='live'
         and response->>'platform_account'='acct_1Ty4WnIogL39waUY') then
    raise exception 'runtime_role_existing_live_dataset_required';
  end if;
  execute format('grant connect on database %I to toolbako_runtime_web', current_database());
end $$;
grant usage on schema toolbako_runtime to toolbako_runtime_web;

grant select(singleton,version) on toolbako_runtime.schema_version to toolbako_runtime_web;
grant select(singleton,revision,payload), update(payload,revision,updated_at) on toolbako_runtime.marketplace_state to toolbako_runtime_web;
grant select(id,content_hash), insert(id,actor_id,action,target,request_id,occurred_at,content_hash) on toolbako_runtime.audit_events to toolbako_runtime_web;
grant select(operation_id,endpoint,request_hash,status,response,created_at), insert(operation_id,endpoint,request_hash,status,response), update(status,response,updated_at) on toolbako_runtime.stripe_operations to toolbako_runtime_web;
grant select(id,status,available_at,created_at,lease_until), insert(id,recipient,sender,subject,body) on toolbako_runtime.email_outbox to toolbako_runtime_web;
grant select(payment_intent,order_id,seller_id,amount,fee), insert(payment_intent,order_id,seller_id,amount,fee) on toolbako_runtime.finance_receipts to toolbako_runtime_web;
grant select(id,seller_id,request_key,provider_reference,content_hash,status,scheduled_for), insert(id,seller_id,account_id,amount,fee,net_amount,status,scheduled_for,request_key,content_hash), update(status,provider_reference,updated_at) on toolbako_runtime.finance_payouts to toolbako_runtime_web;
grant select(id,payout_id,receipt_id,amount,transfer_amount), insert(id,payout_id,receipt_id,amount,transfer_amount) on toolbako_runtime.finance_allocations to toolbako_runtime_web;
grant select(id,content_hash), insert(id,kind,amount,receipt_id,payout_id,provider_reference,content_hash) on toolbako_runtime.finance_entries to toolbako_runtime_web;

create policy toolbako_web_select on toolbako_runtime.schema_version for select to toolbako_runtime_web using(true);
create policy toolbako_web_select on toolbako_runtime.marketplace_state for select to toolbako_runtime_web using(singleton);
create policy toolbako_web_update on toolbako_runtime.marketplace_state for update to toolbako_runtime_web using(singleton) with check(singleton);
create policy toolbako_web_select on toolbako_runtime.audit_events for select to toolbako_runtime_web using(true);
create policy toolbako_web_insert on toolbako_runtime.audit_events for insert to toolbako_runtime_web with check(true);
create policy toolbako_web_select on toolbako_runtime.stripe_operations for select to toolbako_runtime_web using(true);
create policy toolbako_web_insert on toolbako_runtime.stripe_operations for insert to toolbako_runtime_web
  with check(operation_id <> 'toolbako-financial-scope-v1' and endpoint <> 'internal/financial-scope-v1');
create policy toolbako_web_update on toolbako_runtime.stripe_operations for update to toolbako_runtime_web
  using(operation_id <> 'toolbako-financial-scope-v1' and endpoint <> 'internal/financial-scope-v1')
  with check(operation_id <> 'toolbako-financial-scope-v1' and endpoint <> 'internal/financial-scope-v1');
create policy toolbako_web_select on toolbako_runtime.email_outbox for select to toolbako_runtime_web using(true);
create policy toolbako_web_insert on toolbako_runtime.email_outbox for insert to toolbako_runtime_web with check(true);
create policy toolbako_web_select on toolbako_runtime.finance_receipts for select to toolbako_runtime_web using(true);
create policy toolbako_web_insert on toolbako_runtime.finance_receipts for insert to toolbako_runtime_web with check(true);
create policy toolbako_web_select on toolbako_runtime.finance_payouts for select to toolbako_runtime_web using(true);
create policy toolbako_web_insert on toolbako_runtime.finance_payouts for insert to toolbako_runtime_web with check(true);
create policy toolbako_web_update on toolbako_runtime.finance_payouts for update to toolbako_runtime_web using(true) with check(true);
create policy toolbako_web_select on toolbako_runtime.finance_allocations for select to toolbako_runtime_web using(true);
create policy toolbako_web_insert on toolbako_runtime.finance_allocations for insert to toolbako_runtime_web with check(true);
create policy toolbako_web_select on toolbako_runtime.finance_entries for select to toolbako_runtime_web using(true);
create policy toolbako_web_insert on toolbako_runtime.finance_entries for insert to toolbako_runtime_web with check(true);
commit;
