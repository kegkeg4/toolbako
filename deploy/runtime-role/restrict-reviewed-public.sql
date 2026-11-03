-- OPERATOR-ONLY, separately reviewed changes on xijdnqnsuhuciqyygnkg.
-- The caller must first verify the exact session-pooler target and TLS/CA;
-- SQL cannot prove a Supabase project hostname. No automatic Web execution.
-- This exact one-time precondition is intentionally NOT an idempotent repair.
-- If a previous commit is uncertain, read back ACLs; never broaden/reapply.
begin;
set local lock_timeout='3s';
set local statement_timeout='10s';
do $$
declare
  db_oid oid;
  function_oid oid;
  db_acl_before jsonb;
  function_acl_before jsonb;
  db_acl_after jsonb;
  function_acl_after jsonb;
  function_identity_before jsonb;
  function_identity_after jsonb;
begin
  if current_database() <> 'postgres' or current_user <> 'postgres' or session_user <> 'postgres'
     or not exists(select 1 from toolbako_runtime.schema_version where singleton and version=2)
     or not exists(select 1 from toolbako_runtime.marketplace_state where singleton)
     or not exists(select 1 from toolbako_runtime.stripe_operations
       where operation_id='toolbako-financial-scope-v1' and endpoint='internal/financial-scope-v1'
         and status='succeeded' and request_hash='3ac9431b17b3aa9a075a3d6340a202e14a35a84d376eccb97bbea621ca682230'
         and response='{"mode":"live","platform_account":"acct_1Ty4WnIogL39waUY","ledger_id":"667c8593-cee4-4bec-94ed-bbbd95600867"}'::jsonb) then
    raise exception 'reviewed_public_acl_target_mismatch';
  end if;
  select oid into strict db_oid from pg_database where datname=current_database();
  select p.oid into strict function_oid from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname='rls_auto_enable' and p.pronargs=0
      and p.prokind='f' and p.prosecdef and pg_get_userbyid(p.proowner)='postgres'
      and not exists(select 1 from pg_depend d where d.classid='pg_proc'::regclass and d.objid=p.oid
        and d.refclassid='pg_extension'::regclass and d.deptype='e');
  if not exists(select 1 from pg_namespace n,lateral aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a
                where n.nspname='public' and a.grantee=0 and a.privilege_type='USAGE') then
    raise exception 'reviewed_public_acl_schema_mismatch';
  end if;
  select jsonb_agg(jsonb_build_object('grantee',case when a.grantee=0 then 'PUBLIC' else pg_get_userbyid(a.grantee) end,
      'privilege',a.privilege_type,'grantable',a.is_grantable) order by a.grantee,a.privilege_type)
    into db_acl_before from pg_database d,lateral aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a where d.oid=db_oid;
  select jsonb_agg(jsonb_build_object('grantee',case when a.grantee=0 then 'PUBLIC' else pg_get_userbyid(a.grantee) end,
      'privilege',a.privilege_type,'grantable',a.is_grantable) order by a.grantee,a.privilege_type)
    into function_acl_before from pg_proc p,lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a where p.oid=function_oid;
  if (select jsonb_agg(v order by v->>'privilege') from jsonb_array_elements(db_acl_before) v where v->>'grantee'='PUBLIC')
      is distinct from '[{"grantee":"PUBLIC","privilege":"CONNECT","grantable":false},{"grantee":"PUBLIC","privilege":"TEMPORARY","grantable":false}]'::jsonb
     or (select jsonb_agg(v order by (v->>'grantee') collate "C") from jsonb_array_elements(function_acl_before) v)
      is distinct from '[{"grantee":"PUBLIC","privilege":"EXECUTE","grantable":false},{"grantee":"anon","privilege":"EXECUTE","grantable":false},{"grantee":"authenticated","privilege":"EXECUTE","grantable":false},{"grantee":"postgres","privilege":"EXECUTE","grantable":false},{"grantee":"service_role","privilege":"EXECUTE","grantable":false}]'::jsonb then
    raise exception 'reviewed_public_acl_expectation_mismatch';
  end if;
  -- Preserve function, owner, definer mode, return type and event-trigger
  -- wiring. Function bodies are never read or returned by this operation.
  select jsonb_build_object('oid',p.oid,'owner',p.proowner,'definer',p.prosecdef,'return_type',p.prorettype,
    'events',coalesce((select jsonb_agg(jsonb_build_array(e.oid,e.evtname,e.evtevent,e.evtowner,e.evtenabled,e.evttags) order by e.oid)
      from pg_event_trigger e where e.evtfoid=p.oid),'[]'::jsonb))
    into function_identity_before from pg_proc p where p.oid=function_oid;

  revoke temporary on database postgres from public;
  revoke execute on function public.rls_auto_enable() from public;

  select jsonb_agg(jsonb_build_object('grantee',case when a.grantee=0 then 'PUBLIC' else pg_get_userbyid(a.grantee) end,
      'privilege',a.privilege_type,'grantable',a.is_grantable) order by a.grantee,a.privilege_type)
    into db_acl_after from pg_database d,lateral aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a where d.oid=db_oid;
  select jsonb_agg(jsonb_build_object('grantee',case when a.grantee=0 then 'PUBLIC' else pg_get_userbyid(a.grantee) end,
      'privilege',a.privilege_type,'grantable',a.is_grantable) order by a.grantee,a.privilege_type)
    into function_acl_after from pg_proc p,lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a where p.oid=function_oid;
  select jsonb_build_object('oid',p.oid,'owner',p.proowner,'definer',p.prosecdef,'return_type',p.prorettype,
    'events',coalesce((select jsonb_agg(jsonb_build_array(e.oid,e.evtname,e.evtevent,e.evtowner,e.evtenabled,e.evttags) order by e.oid)
      from pg_event_trigger e where e.evtfoid=p.oid),'[]'::jsonb))
    into function_identity_after from pg_proc p where p.oid=function_oid;
  if db_acl_after is distinct from (select jsonb_agg(v) from jsonb_array_elements(db_acl_before) v
        where not(v->>'grantee'='PUBLIC' and v->>'privilege'='TEMPORARY'))
     or function_acl_after is distinct from (select jsonb_agg(v) from jsonb_array_elements(function_acl_before) v where v->>'grantee'<>'PUBLIC')
     or function_identity_after is distinct from function_identity_before then
    raise exception 'reviewed_public_acl_readback_mismatch';
  end if;
end $$;
commit;
