FROM python:3.11-slim

WORKDIR /app

# System deps: gcc for a few wheels that don't ship binaries, libpq for psycopg2,
# and ca-certificates for TLS. That's all we need — no browser required.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Drop root
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV MAILMIND_PRODUCTION=1

CMD ["sh", "-c", "flask db upgrade && gunicorn --bind 0.0.0.0:8080 --workers 2 --threads 4 --worker-class gthread --timeout 300 --keep-alive 65 app:app"]
