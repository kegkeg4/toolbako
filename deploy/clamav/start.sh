#!/bin/sh
set -eu

clam_pid=''
update_pid=''
cleanup() {
    trap - EXIT INT TERM
    [ -z "$clam_pid" ] || kill "$clam_pid" 2>/dev/null || true
    [ -z "$update_pid" ] || kill "$update_pid" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM

# The official daemons drop privileges using User/DatabaseOwner=clamav.
mkdir -p /var/lib/clamav /var/log/clamav /run/clamav
chown clamav:clamav /var/lib/clamav /var/log/clamav /run/clamav
if [ ! -f /var/lib/clamav/main.cvd ] && [ ! -f /var/lib/clamav/main.cld ]; then
    # Keep validation enabled even on the first database download.
    freshclam --stdout
fi
clamd --foreground &
clam_pid=$!
freshclam --daemon --foreground --stdout --checks=12 &
update_pid=$!

# Railway must see a failed container if either the scanner or updater dies.
# The vendor's default entrypoint otherwise tails /dev/null after child exit.
while kill -0 "$clam_pid" 2>/dev/null && kill -0 "$update_pid" 2>/dev/null; do
    sleep 5 &
    wait $! || true
done
echo 'Scanner or signature updater exited; restarting service.' >&2
exit 1
