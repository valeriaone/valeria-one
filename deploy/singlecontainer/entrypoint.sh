#!/bin/sh
# All-in-one bootstrap: prepare /data, generate+persist the secret key and DB password on first boot,
# initdb if empty, bring the schema up to date (every boot, so image updates migrate), print the first-run
# setup link, then hand off to supervisord which keeps PostgreSQL, the app and the jobs worker running.
set -eu

DATA=/data
PGDATA="$DATA/db"
SECRETS="$DATA/secrets.env"
RUNDIR=/var/run/postgresql
PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
[ -n "$PGBIN" ] || { echo "no postgres binaries found" >&2; exit 1; }

mkdir -p "$DATA" "$RUNDIR"

# --- secrets: env wins on FIRST boot, then the persisted copy is the source of truth (a stable VO_SECRET_KEY
#     is required so encrypted channel data and the setup link stay valid across restarts/updates) ---
if [ ! -f "$SECRETS" ]; then
  umask 077
  _sk="${VO_SECRET_KEY:-$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')}"
  _dp="${VO_DB_PASSWORD:-$(python -c 'import secrets; print(secrets.token_urlsafe(24))')}"
  printf 'VO_SECRET_KEY=%s\nVO_DB_PASSWORD=%s\n' "$_sk" "$_dp" > "$SECRETS"
fi
VO_SECRET_KEY="$(sed -n 's/^VO_SECRET_KEY=//p' "$SECRETS")"
VO_DB_PASSWORD="$(sed -n 's/^VO_DB_PASSWORD=//p' "$SECRETS")"
export VO_SECRET_KEY VO_DB_PASSWORD PGBIN
export VO_DATABASE_URL="postgresql+psycopg://vo:${VO_DB_PASSWORD}@127.0.0.1:5432/valeria_one"
export VO_SIGNUP="${VO_SIGNUP:-closed}"          # only the first account (/setup) and invited users; login on by default

chown -R postgres:postgres "$DATA" "$RUNDIR"
chmod 700 "$PGDATA" 2>/dev/null || true

# --- first boot: initialise the database cluster ---
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  su -s /bin/sh postgres -c "$PGBIN/initdb -D '$PGDATA' -E UTF8 --auth-local=trust --auth-host=scram-sha-256"
  {
    echo "listen_addresses = '127.0.0.1'"
    echo "unix_socket_directories = '$RUNDIR'"
  } >> "$PGDATA/postgresql.conf"
fi

# --- bring Postgres up briefly to create the role/db and run migrations ---
# Bootstrap talks to Postgres over the trusted local socket ($RUNDIR); the app connects over TCP with a password.
su -s /bin/sh postgres -c "$PGBIN/pg_ctl -D '$PGDATA' -w -t 60 -o '-c listen_addresses=127.0.0.1 -c unix_socket_directories=$RUNDIR' start"
until su -s /bin/sh postgres -c "$PGBIN/pg_isready -h '$RUNDIR' -U postgres" >/dev/null 2>&1; do sleep 1; done

PSQL="psql -v ON_ERROR_STOP=1 -h '$RUNDIR' -U postgres --no-psqlrc -tAc"
if [ "$(su -s /bin/sh postgres -c "$PSQL \"SELECT 1 FROM pg_roles WHERE rolname='vo'\"")" != "1" ]; then
  su -s /bin/sh postgres -c "psql -h '$RUNDIR' -U postgres -c \"CREATE ROLE vo LOGIN PASSWORD '$VO_DB_PASSWORD'\""
else
  su -s /bin/sh postgres -c "psql -h '$RUNDIR' -U postgres -c \"ALTER ROLE vo PASSWORD '$VO_DB_PASSWORD'\""
fi
if [ "$(su -s /bin/sh postgres -c "$PSQL \"SELECT 1 FROM pg_database WHERE datname='valeria_one'\"")" != "1" ]; then
  su -s /bin/sh postgres -c "psql -h '$RUNDIR' -U postgres -c \"CREATE DATABASE valeria_one OWNER vo\""
fi

# schema up to date on every boot (so swapping in a newer image migrates automatically)
su -s /bin/sh vo -c "cd /app && VO_DATABASE_URL='$VO_DATABASE_URL' VO_SECRET_KEY='$VO_SECRET_KEY' alembic upgrade head"

# show the first-run setup link (does nothing once an admin exists)
echo "----------------------------------------------------------------------"
su -s /bin/sh vo -c "cd /app && VO_DATABASE_URL='$VO_DATABASE_URL' VO_SECRET_KEY='$VO_SECRET_KEY' VO_PUBLIC_URL='${VO_PUBLIC_URL:-}' python -m valeria_one.ops setup-link" 2>&1 \
  && echo "^ open this link once to create your admin account" || true
echo "----------------------------------------------------------------------"

su -s /bin/sh postgres -c "$PGBIN/pg_ctl -D '$PGDATA' -w -t 60 stop"

exec supervisord -c /etc/vo/supervisord.conf
