-- Read-only inventory BEFORE role creation, on the already-selected NEW DB.
-- No row data, credentials, object names, SQL bodies or account IDs are returned.
-- A single SELECT. The operator must use a READ ONLY transaction and the pinned
-- target/TLS identity. No role creation/SET ROLE/GRANT or public revoke occurs.
with namespaces as (
  select n.oid,n.nspname,
    exists(select 1 from aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a where a.grantee=0 and a.privilege_type='USAGE') as public_usage,
    exists(select 1 from aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a where a.grantee=0 and a.privilege_type='CREATE') as public_create
  from pg_catalog.pg_namespace n where n.nspname !~ '^pg_' and n.nspname <> 'information_schema'
), database_acl as (
  select a.* from pg_catalog.pg_database d,
    lateral aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a where d.datname=current_database()
), public_relation_privileges as (
  select c.oid,n.nspname,c.relkind,
    exists(select 1 from aclexplode(coalesce(c.relacl,acldefault(case when c.relkind='S' then 'S'::"char" else 'r'::"char" end,c.relowner))) a where a.grantee=0) as table_grant,
    exists(select 1 from pg_catalog.pg_attribute x,lateral aclexplode(x.attacl) a where x.attrelid=c.oid and x.attnum>0 and not x.attisdropped and a.grantee=0) as column_grant
  from pg_catalog.pg_class c join namespaces n on n.oid=c.relnamespace
  where c.relkind in ('r','p','v','m','f','S')
)
select jsonb_build_object(
  'current_database_is_postgres',current_database()='postgres',
  'runtime_role_name_exists',exists(select 1 from pg_roles where rolname='toolbako_web_live_v1'),
  'runtime_group_name_exists',exists(select 1 from pg_roles where rolname='toolbako_runtime_web'),
  'public_database_connect',exists(select 1 from database_acl where grantee=0 and privilege_type='CONNECT'),
  'public_database_create',exists(select 1 from database_acl where grantee=0 and privilege_type='CREATE'),
  'public_database_temp',exists(select 1 from database_acl where grantee=0 and privilege_type='TEMPORARY'),
  'public_schema_create_count',(select count(*) from namespaces where public_create),
  'runtime_schema_public_usage',(select public_usage from namespaces where nspname='toolbako_runtime'),
  'public_runtime_relation_grant_count',(select count(*) from public_relation_privileges where nspname='toolbako_runtime' and (table_grant or column_grant)),
  'public_outside_relation_grant_count',(select count(*) from public_relation_privileges where nspname<>'toolbako_runtime' and relkind<>'S' and (table_grant or column_grant)),
  'public_sequence_grant_count',(select count(*) from public_relation_privileges where relkind='S' and table_grant),
  'public_callable_definer_count',(
    select count(*) from pg_catalog.pg_proc p join namespaces n on n.oid=p.pronamespace
    where p.prosecdef and (n.public_usage or n.nspname='toolbako_runtime')
      and exists(select 1 from aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a where a.grantee=0 and a.privilege_type='EXECUTE')),
  'runtime_base_table_count',(select count(*) from pg_class c join namespaces n on n.oid=c.relnamespace where n.nspname='toolbako_runtime' and c.relkind in ('r','p')),
  'runtime_rls_enabled_count',(select count(*) from pg_class c join namespaces n on n.oid=c.relnamespace where n.nspname='toolbako_runtime' and c.relkind in ('r','p') and c.relrowsecurity),
  'runtime_policy_count',(select count(*) from pg_policy p join pg_class c on c.oid=p.polrelid join namespaces n on n.oid=c.relnamespace where n.nspname='toolbako_runtime'),
  'runtime_public_policy_count',(select count(*) from pg_policy p join pg_class c on c.oid=p.polrelid join namespaces n on n.oid=c.relnamespace where n.nspname='toolbako_runtime' and 0=any(p.polroles))
) as fixed_inventory;
