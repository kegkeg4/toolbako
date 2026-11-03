-- OPERATOR PREPARATION ONLY. Not run by Web, preflight, or migration startup.
-- Run only on reviewed xijdnqnsuhuciqyygnkg, after snapshot/binding verification.
-- New roles only: an existing name fails rather than adopting its privileges.
-- No password is embedded. The login remains disabled until the operator has
-- verified grants and completes a separately authorized secret-safe activation.
begin;
create role toolbako_runtime_web nologin noinherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls;
create role toolbako_web_live_v1 nologin inherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls password null;
grant toolbako_runtime_web to toolbako_web_live_v1;
commit;
