FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY valeria_one ./valeria_one
RUN pip install --no-cache-dir .
COPY alembic ./alembic
COPY alembic.ini ./
# a fixed id, so the host can give the app (and only the app) write access to its request folder
RUN useradd -m -u 10001 vo && chown -R vo /app
USER vo
EXPOSE 8000
CMD ["uvicorn", "valeria_one.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
