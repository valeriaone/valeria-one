# Valeria One, all in ONE container: the app, its background jobs and PostgreSQL together,
# behind a single HTTP port (8000). For platforms that deploy one unmodified image and one port
# (e.g. PikaPods). All state (database + the generated secret key) lives under /data — mount it to persist.
#
# Build from the REPO ROOT:  docker build -f deploy/singlecontainer/Dockerfile -t valeria-one-aio .
# Run:                        docker run -p 8000:8000 -v vo_data:/data valeria-one-aio
# Then open the setup link printed in the logs to create the first (admin) account.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# PostgreSQL server + client from the base distro (no extra apt repos), plus a tiny process supervisor.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      postgresql postgresql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY valeria_one ./valeria_one
RUN pip install --no-cache-dir . supervisor
COPY alembic ./alembic
COPY alembic.ini ./

# the app runs as this fixed, non-root user (PostgreSQL runs as the distro's "postgres" user)
RUN useradd -m -u 10001 vo && chown -R vo /app
COPY deploy/singlecontainer/entrypoint.sh /usr/local/bin/vo-entrypoint
COPY deploy/singlecontainer/supervisord.conf /etc/vo/supervisord.conf
RUN chmod +x /usr/local/bin/vo-entrypoint

# Everything that must survive a restart (Postgres data dir + the generated secret key).
VOLUME /data
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/vo-entrypoint"]
