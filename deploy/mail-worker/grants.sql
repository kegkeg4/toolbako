-- Reviewed, opt-in role provisioning; NOT a runtime migration or pre-deploy step.
-- Run only after the operator approves giving the dedicated worker mail access.
-- No login/password is created here. Refuse an existing role instead of silently
-- inheriting or changing privileges belonging to another service.
begin;
create role toolbako_mail_worker nologin nosuperuser nocreatedb nocreaterole
  noinherit noreplication nobypassrls;
grant usage on schema toolbako_runtime to toolbako_mail_worker;
grant select on toolbako_runtime.schema_version, toolbako_runtime.email_outbox
  to toolbako_mail_worker;
grant update (status, attempts, first_attempt_at, available_at, lease_until,
              claim_token, sent_at)
  on toolbako_runtime.email_outbox to toolbako_mail_worker;
create policy mail_worker_schema_read on toolbako_runtime.schema_version
  for select to toolbako_mail_worker using (true);
create policy mail_worker_queue_read on toolbako_runtime.email_outbox
  for select to toolbako_mail_worker using (true);
create policy mail_worker_queue_progress on toolbako_runtime.email_outbox
  for update to toolbako_mail_worker
  using (status in ('pending', 'sending'))
  with check (status in ('pending', 'sending', 'sent', 'review'));
-- No INSERT/DELETE/TRUNCATE, message-content updates, other runtime tables,
-- Auth/Storage access, BYPASSRLS, role membership or SECURITY DEFINER functions.
commit;
