# Backer Server Dockerfile
# Build: docker build -t backer .
# Run: docker run -d -p 8420:8420 -v backer-data:/data backer

FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    bzip2 \
    unzip \
    cifs-utils \
    smbclient \
    nfs-common \
    && rm -rf /var/lib/apt/lists/*

# Create app user
RUN useradd --system --home-dir /data --shell /bin/false backer

# Set working directory
WORKDIR /app

# Copy source
COPY . /app

# Install backer
RUN pip install --no-cache-dir -e ".[server]"

# Create data directory
RUN mkdir -p /data/tools /data/logs && chown -R backer:backer /data

# Download rclone and restic
RUN backer setup --data-dir /data || true

# Set environment
ENV BACKER_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

# Switch to app user
USER backer

# Expose port
EXPOSE 8420

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8420/health || exit 1

# Run server
CMD ["backer", "server", "start", "--host", "0.0.0.0", "--port", "8420"]
