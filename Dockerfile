FROM python:3.12-slim

WORKDIR /app

# Install deps first (cached layer)
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# App code
COPY job_watch/ ./job_watch/

# Default companies config (overridden by ConfigMap mount at /config/companies.yaml)
COPY companies.yaml ./companies.yaml

# Run as non-root
RUN useradd -m appuser
USER appuser

CMD ["python", "-m", "job_watch"]
