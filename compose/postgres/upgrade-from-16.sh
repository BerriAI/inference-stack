#!/usr/bin/env bash
# Copy the LiteLLM database from the Postgres 16 volume used by earlier releases
# into the Postgres 19 database once. The previous volume is left in place.
set -euo pipefail

old=/var/lib/postgresql/data
[[ -s "$old/PG_VERSION" ]] || exit 0
[[ "$(cat "$old/PG_VERSION")" == 16 ]] || { echo "The previous database volume holds Postgres $(cat "$old/PG_VERSION") data; only 16 can be copied." >&2; exit 1; }

export PGUSER=litellm PGDATABASE=litellm PGPASSWORD="$POSTGRES_PASSWORD"
tables="$(psql -h postgres -XAtc "select count(*) from pg_tables where schemaname = 'public'")"
[[ "$tables" == 0 ]] || exit 0

socket="$(mktemp -d)"
pg_ctl -D "$old" -w -l "$socket/log" -o "-c listen_addresses='' -k $socket" start >/dev/null
trap 'pg_ctl -D "$old" -w -m fast stop >/dev/null' EXIT
pg_dump -h "$socket" -Fc | pg_restore -h postgres --single-transaction --exit-on-error -d litellm
echo "Copied the LiteLLM database from Postgres 16 to Postgres 19."
