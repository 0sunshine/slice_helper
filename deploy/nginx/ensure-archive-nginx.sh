#!/bin/sh
set -eu

base=/mpeg/mpeg2/codex/nginx
pid_file="$base/logs/nginx.pid"

if [ -r "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    exit 0
fi

"$base/sbin/nginx" -t >/dev/null 2>&1
exec "$base/sbin/nginx"
