FROM python:3.13-slim

# Create a non-root user
RUN groupadd -r app && useradd -r -g app app

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY source/ ./source/

# Give ownership to the non-root user
# /data is used for SQLite volume (if configured)
RUN mkdir -p /data && chown -R app:app /app /data

# Switch to non-root user
USER app

EXPOSE 3000

ENV DATABASE_URL=sqlite:////data/app.db

# Healthcheck requires curl since we want to hit the API,
# or we can use Python's urllib to avoid installing curl
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/healthz')" || exit 1

# Fail closed if migrations fail
CMD ["sh", "-c", "alembic upgrade head && uvicorn source.main:app --host 0.0.0.0 --port 3000"]
