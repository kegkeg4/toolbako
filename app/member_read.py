"""Request-local, owner-scoped views for explicitly read-only member pages."""
from threading import RLock

from .data import DemoStore
from .mfa import is_privileged_path
from .public_read import PUBLIC_READ_PATHS, PublicView


MEMBER_READ_PATHS = (PUBLIC_READ_PATHS - {'/login'}) | frozenset({
    '/favorites', '/notifications', '/settings', '/subscriptions', '/library',
})


def member_read(request):
    # SessionMiddleware must have verified the signed cookie first. Never
    # interpret a header, username, query parameter or unsigned cookie as an ID.
    session = request.scope.get('session', {})
    sid = session.get('sid')
    return (request.method == 'GET' and request.url.path in MEMBER_READ_PATHS
            and not is_privileged_path(request.url.path, request.method)
            and 'authorization' not in request.headers and not session.get('user')
            and isinstance(sid, str) and 0 < len(sid) <= 128)


class MemberView(PublicView):
    favorite_folders_for = DemoStore.favorite_folders_for

    def __init__(self, payload):
        self.__dict__.update(payload)
        self._lock = RLock()

    def badges_for(self, username):
        return self.public_badges.get(username) or {}


def member_projection(path):
    """SQL fragments are application constants; identity is parameterized."""
    catalog = path in {'/', '/tools', '/favorites'}
    tools = """coalesce((select jsonb_agg(item)
        from jsonb_array_elements(s.payload->'tools') item
        where coalesce(item->'is_removed','false'::jsonb)<>'true'::jsonb
          and coalesce(item->'is_published','true'::jsonb)='true'::jsonb
          and coalesce(item->'status','\"published\"'::jsonb)='\"published\"'::jsonb), '[]'::jsonb)""" if catalog else "'[]'::jsonb"
    badges = """coalesce((select jsonb_object_agg(key, jsonb_build_object(
        'is_founding_member',value->'is_founding_member',
        'founding_member_since',value->'founding_member_since',
        'is_certified_creator',value->'is_certified_creator',
        'certified_creator_since',value->'certified_creator_since'))
        from jsonb_each(s.payload->'registered_users')), '{}'::jsonb)""" if catalog else "'{}'::jsonb"
    def own_rows(field, key='user_id', needed=True):
        if not needed:
            return "'[]'::jsonb"
        return f"coalesce((select jsonb_agg(item) from jsonb_array_elements(s.payload->'{field}') item where item->>'{key}'=u.uid), '[]'::jsonb)"
    # Strip password material and provider tokens even though this view stays
    # server-side. Library receives only its buyer's completed orders; other
    # pages receive no orders. No payouts, audit or other users' sessions.
    return "jsonb_build_object(" + ','.join([
        "'account_sessions', case when a.record is null then '{}'::jsonb else jsonb_build_object(%s::text, a.record - 'provider_tokens') end",
        "'registered_users', case when u.username is null then '{}'::jsonb else jsonb_build_object(u.username, (s.payload->'registered_users'->u.username) - 'password_hash' - 'password_salt') end",
        "'identity_applications', case when u.uid is null then '{}'::jsonb else jsonb_build_object(u.uid, s.payload->'identity_applications'->u.uid) end",
        "'account_deletions'," + own_rows('account_deletions'),
        "'notifications'," + own_rows('notifications'),
        "'support_cases'," + own_rows('support_cases', needed=path == '/support'),
        "'subscriptions'," + own_rows('subscriptions', 'buyer_id', path in {'/subscriptions', '/library'}),
        *(["'orders',coalesce((select jsonb_agg(item) from jsonb_array_elements(s.payload->'orders') item where item->>'buyer_id'=u.uid and item->>'status'='completed'),'[]'::jsonb)"] if path == '/library' else []),
        "'tools'," + tools,
        "'public_badges'," + badges,
        "'likes',jsonb_build_object('__type__','set','value',coalesce((select jsonb_agg(item) from jsonb_array_elements(s.payload->'likes'->'value') item where item->'value'->>0=u.uid),'[]'::jsonb))" if path == '/favorites' else "'likes',jsonb_build_object('__type__','set','value','[]'::jsonb)",
        "'favorite_folders',case when u.uid is null then '{}'::jsonb else jsonb_build_object(u.uid,coalesce(s.payload->'favorite_folders'->u.uid,'[]'::jsonb)) end" if path == '/favorites' else "'favorite_folders','{}'::jsonb",
        "'favorite_assignments',coalesce((select jsonb_object_agg(key,value) from jsonb_each(s.payload->'favorite_assignments') where split_part(key,'␟',1)=u.uid),'{}'::jsonb)" if path == '/favorites' else "'favorite_assignments','{}'::jsonb",
    ]) + ")"
