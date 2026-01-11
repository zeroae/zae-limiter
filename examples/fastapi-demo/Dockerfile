FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies directly (not from pyproject.toml)
RUN pip install --no-cache-dir \
    "fastapi>=0.109.0" \
    "uvicorn[standard]>=0.27.0" \
    "pydantic>=2.0.0" \
    "pydantic-settings>=2.0.0"

# Copy application code
COPY app/ app/

# Create entrypoint script to install zae-limiter at runtime
RUN echo '#!/bin/bash\n\
set -e\n\
echo "Installing zae-limiter from mounted volume..."\n\
SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 pip install -q /zae-limiter\n\
echo "Starting API server..."\n\
exec uvicorn app.main:app --host 0.0.0.0 --port 8080\n\
' > /entrypoint.sh && chmod +x /entrypoint.sh

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run entrypoint
CMD ["/entrypoint.sh"]
