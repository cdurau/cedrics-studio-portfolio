FROM python:3.13-slim

# Create a non-root user
RUN groupadd -r app && useradd -r -g app app

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY source/ ./source/

# Set permissions
RUN chown -R app:app /app

# Switch to non-root user
USER app

EXPOSE 3000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/healthz')" || exit 1

# Start the application
CMD ["uvicorn", "source.main:app", "--host", "0.0.0.0", "--port", "3000"]
