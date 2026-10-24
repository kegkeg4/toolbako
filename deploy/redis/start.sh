#!/bin/sh
set -eu
umask 077

# Only URL-safe generated secrets are accepted, preventing config injection.
password=${REDIS_PASSWORD:?REDIS_PASSWORD must be configured}
case "$password" in
    *[!A-Za-z0-9_-]*|'') echo 'Invalid Redis credential format.' >&2; exit 1 ;;
esac
if [ "${#password}" -lt 40 ]; then
    echo 'Redis credential must contain at least 40 URL-safe characters.' >&2
    exit 1
fi
config=/tmp/toolbako-redis.conf
cat > "$config" <<'CONFIG'
bind 0.0.0.0 -::
port 6379
protected-mode yes
daemonize no
loglevel warning
save ""
appendonly no
maxmemory 128mb
maxmemory-policy noeviction
user default off
CONFIG
# The application can only use its expiring rate-limit counters. No FLUSH,
# arbitrary reads/writes, configuration changes, or other key prefixes.
printf 'user toolbako on >%s ~toolbako:ratelimit:* +ping +eval +incr +ttl +expire +client|setinfo +client|setname\n' "$password" >> "$config"
unset password REDIS_PASSWORD
exec redis-server "$config"
