# XClaw – Docker Deployment
# Build: docker build -t xclaw .
# Run:   docker-compose up

FROM python:3.11-slim

# System dependencies for akshare / lxml / pandas
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY pyproject.toml ./
COPY xclaw/__init__.py xclaw/

# Install the package (deps only, no source)
RUN pip install --no-cache-dir -e . --no-build-isolation || pip install --no-cache-dir .

# Copy full source
COPY . .

# Data directory (will be mounted as a volume)
RUN mkdir -p /data && chmod 777 /data

# Expose web port (default 8080)
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run as non-root user for security
RUN useradd -m -u 1000 xclaw && chown -R xclaw:xclaw /app
USER xclaw

ENV XCLAW_DATA_DIR=/data
ENV XCLAW_WEB_HOST=0.0.0.0

CMD ["xclaw", "start"]
