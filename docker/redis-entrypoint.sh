#!/bin/sh
set -eu

: "${REDIS_PASSWORD:?REDIS_PASSWORD must be set}"

config_path="${TMPDIR:-/tmp}/redis.conf"
escaped_password=$(
    printf '%s' "$REDIS_PASSWORD" \
        | od -An -v -tx1 \
        | tr -d ' \n' \
        | sed 's/../\\x&/g'
)

umask 077
printf 'requirepass "%s"\n' "$escaped_password" > "$config_path"
exec redis-server "$config_path" --appendonly yes
