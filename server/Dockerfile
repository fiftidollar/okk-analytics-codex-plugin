FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY server/alembic.ini /app/alembic.ini
COPY server/migrations /app/migrations
COPY server/okk_mcp /app/okk_mcp

RUN addgroup --system okk && adduser --system --ingroup okk okk \
    && chown -R okk:okk /app
USER okk

EXPOSE 8020

CMD ["uvicorn", "okk_mcp.main:app", "--host", "0.0.0.0", "--port", "8020", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
