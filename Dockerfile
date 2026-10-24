FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY valeria_one ./valeria_one
RUN pip install --no-cache-dir .
COPY alembic ./alembic
COPY alembic.ini ./
RUN useradd -m vo && chown -R vo /app
USER vo
EXPOSE 8000
CMD ["uvicorn", "valeria_one.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
