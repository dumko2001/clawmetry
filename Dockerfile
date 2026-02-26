FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir flask==3.1.0 requests==2.32.3 gunicorn==23.0.0

# Copy app
COPY dashboard.py .

# Cloud Run sets PORT env var; pass it through at runtime
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["sh", "-c", "python3 dashboard.py --host 0.0.0.0 --port ${PORT:-8080} --no-debug"]
