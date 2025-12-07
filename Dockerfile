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
    sshpass \
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
RUN mkdir -p /data/tools /data/logs

# Set environment before setup so tools are downloaded to /data/tools
ENV BACKER_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

# Download backup tools (rclone, restic, kopia)
# This will fail the build if tools cannot be downloaded - intentional for reliability
RUN backer setup

# Fix ownership after tools are downloaded
RUN chown -R backer:backer /data

# Switch to app user
USER backer

# Expose port
EXPOSE 8420

# Health check (30s start period allows time for server initialization)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8420/health || exit 1

# Run server
CMD ["backer", "server", "start", "--host", "0.0.0.0", "--port", "8420"]
