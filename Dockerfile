FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for psycopg2-binary
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# Updated CMD with timeout settings
CMD ["sh", "-c", "flask db upgrade && gunicorn --bind 0.0.0.0:8080 --workers 2 --timeout 300 --keep-alive 65 --worker-class sync app:app"]