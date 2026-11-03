-- Second-stage READ ONLY catalog inventory. Object/owner/ACL metadata ONLY.
-- No table rows, function bodies, credentials, definitions or changes.
with namespaces as (
  select n.oid,n.nspname,n.nspowner,
    exists(select 1 from aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a where a.grantee=0 and a.privilege_type='USAGE') as public_usage
  from pg_namespace n where n.nspname !~ '^pg_' and n.nspname <> 'information_schema'
), objects as (
  select 'relation'::text as category,n.nspname as schema_name,c.relname::text as object_name,
    c.oid,c.relowner as owner_oid,c.relkind::text as kind,null::text as argument_types,
    coalesce(c.relacl,acldefault(case when c.relkind='S' then 'S'::"char" else 'r'::"char" end,c.relowner)) as acl,
    n.public_usage
  from pg_class c join namespaces n on n.oid=c.relnamespace
  where n.nspname<>'toolbako_runtime' and c.relkind in ('r','p','v','m','f','S')
    and (exists(select 1 from aclexplode(coalesce(c.relacl,acldefault(case when c.relkind='S' then 'S'::"char" else 'r'::"char" end,c.relowner))) a where a.grantee=0)
      or exists(select 1 from pg_attribute x,lateral aclexplode(x.attacl) a where x.attrelid=c.oid and x.attnum>0 and not x.attisdropped and a.grantee=0))
  union all
  select 'security_definer_routine',n.nspname,p.proname::text,p.oid,p.proowner,p.prokind::text,
    oidvectortypes(p.proargtypes),coalesce(p.proacl,acldefault('f',p.proowner)),n.public_usage
  from pg_proc p join namespaces n on n.oid=p.pronamespace
  where p.prosecdef and (n.public_usage or n.nspname='toolbako_runtime')
    and exists(select 1 from aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a where a.grantee=0 and a.privilege_type='EXECUTE')
)
select jsonb_build_object(
  'database_public_privileges',(
    select coalesce(jsonb_agg(a.privilege_type order by a.privilege_type),'[]'::jsonb)
    from pg_database d,lateral aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a
    where d.datname=current_database() and a.grantee=0),
  'objects',coalesce((select jsonb_agg(jsonb_build_object(
    'category',o.category,'schema',o.schema_name,'name',o.object_name,'kind',o.kind,
    'argument_types',o.argument_types,'owner',pg_get_userbyid(o.owner_oid),'schema_public_usage',o.public_usage,
    'acl',(select jsonb_agg(jsonb_build_object('grantee',case when a.grantee=0 then 'PUBLIC' else pg_get_userbyid(a.grantee) end,
      'privilege',a.privilege_type,'grantable',a.is_grantable) order by a.grantee,a.privilege_type) from aclexplode(o.acl) a),
    'public_column_grants',case when o.category='relation' then (
      select coalesce(jsonb_agg(jsonb_build_object('column',x.attname,'privilege',a.privilege_type,'grantable',a.is_grantable) order by x.attnum,a.privilege_type),'[]'::jsonb)
      from pg_attribute x,lateral aclexplode(x.attacl) a where x.attrelid=o.oid and x.attnum>0 and not x.attisdropped and a.grantee=0) else '[]'::jsonb end,
    'extension',(select e.extname from pg_depend d join pg_extension e on e.oid=d.refobjid
      where d.objid=o.oid and d.classid=case when o.category='relation' then 'pg_class'::regclass else 'pg_proc'::regclass end
        and d.refclassid='pg_extension'::regclass and d.deptype='e' limit 1)
  ) order by o.category,o.schema_name,o.object_name,o.argument_types) from objects o),'[]'::jsonb)
) as public_excess_catalog;
