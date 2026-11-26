# Valeria One app + background worker in ONE container behind a single HTTP port (8000), using an
# EXTERNAL PostgreSQL (e.g. a managed database a platform like PikaPods provides). No database is bundled.
# For a bundled-database single container see deploy/singlecontainer; for a normal server use the root compose.
#
# Build from the REPO ROOT:  docker build -f deploy/app-worker/Dockerfile -t valeria-one-app .
# Run:                        docker run -p 8000:8000 -v vo_data:/data \
#                               -e VO_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db valeria-one-app
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY valeria_one ./valeria_one
RUN pip install --no-cache-dir . supervisor
COPY alembic ./alembic
COPY alembic.ini ./

# the app and worker run as this fixed, non-root user
RUN useradd -m -u 10001 vo && chown -R vo /app
COPY deploy/app-worker/entrypoint.sh /usr/local/bin/vo-entrypoint
COPY deploy/app-worker/supervisord.conf /etc/vo/supervisord.conf
RUN chmod +x /usr/local/bin/vo-entrypoint

# optional: persists a generated secret key when VO_SECRET_KEY isn't supplied via the environment
VOLUME /data
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/vo-entrypoint"]
