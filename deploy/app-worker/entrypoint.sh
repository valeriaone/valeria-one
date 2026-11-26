#!/bin/sh
# app + worker, external database. Require VO_DATABASE_URL, settle the secret key (env wins, else persist one
# under /data), wait for the database, migrate to head, print the first-run setup link, then supervisord keeps
# the app and the jobs worker running.
set -eu

: "${VO_DATABASE_URL:?set VO_DATABASE_URL to your PostgreSQL connection string, e.g. postgresql+psycopg://user:pass@host:5432/dbname}"
export VO_DATABASE_URL
export VO_SIGNUP="${VO_SIGNUP:-closed}"          # only the first account (/setup) and invited users; login on by default

DATA=/data
SECRETS="$DATA/secrets.env"
if [ -z "${VO_SECRET_KEY:-}" ]; then
  if [ -f "$SECRETS" ]; then
    VO_SECRET_KEY="$(sed -n 's/^VO_SECRET_KEY=//p' "$SECRETS")"
  elif mkdir -p "$DATA" 2>/dev/null && [ -w "$DATA" ]; then
    umask 077
    VO_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    printf 'VO_SECRET_KEY=%s\n' "$VO_SECRET_KEY" > "$SECRETS"
  else
    VO_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    echo "WARNING: VO_SECRET_KEY is not set and /data is not writable — generated an EPHEMERAL key. Set VO_SECRET_KEY (or mount a writable /data) so sessions and encrypted data survive a restart." >&2
  fi
fi
export VO_SECRET_KEY

cd /app

# wait for the external database to accept connections
i=0
until python -c "import sqlalchemy as sa; from valeria_one import config; \
e=sa.create_engine(config.DATABASE_URL()); \
import sys; \
c=e.connect(); c.execute(sa.text('SELECT 1')); c.close()" 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && { echo "database not reachable at VO_DATABASE_URL after 60s" >&2; exit 1; }
  sleep 1
done

# schema up to date on every boot (so swapping in a newer image migrates automatically)
alembic upgrade head

# show the first-run setup link (does nothing once an admin exists)
echo "----------------------------------------------------------------------"
python -m valeria_one.ops setup-link 2>&1 && echo "^ open this link once to create your admin account" || true
echo "----------------------------------------------------------------------"

chown -R vo "$DATA" 2>/dev/null || true      # so the vo user can read a persisted secret
exec supervisord -c /etc/vo/supervisord.conf
