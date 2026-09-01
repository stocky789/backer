# Backer Server Dockerfile
# Build: docker build -t backer .
# Run: docker run -d -p 8420:8420 -v backer-data:/data backer

FROM python:3.12-slim

LABEL maintainer="Backer Contributors"
LABEL description="Self-hosted directory backup and restore management powered by Kopia"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    bzip2 \
    unzip \
    cifs-utils \
    smbclient \
    nfs-common \
    sshpass \
    sudo \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create app user
RUN useradd --system --home-dir /data --shell /bin/false backer

# Allow backer to mount/unmount for SMB/NFS repos (Docker)
RUN echo "backer ALL=(ALL) NOPASSWD: /usr/bin/mount, /usr/bin/umount" > /etc/sudoers.d/backer-mount \
    && chmod 440 /etc/sudoers.d/backer-mount

# Set working directory
WORKDIR /app

# Copy only pyproject.toml and source code (optimize layer caching)
COPY pyproject.toml /app/
COPY src /app/src
COPY README.md /app/

# Install backer and dependencies
RUN pip install --no-cache-dir ".[server]"

# Tools are downloaded and checksum-verified on first use into this writable volume.
# Create data directory for tools, logs, and local backups
RUN mkdir -p /data/tools /data/logs /data/local-backups && \
    chown -R backer:backer /data

# Set environment variables
ENV BACKER_DATA_DIR=/data
ENV HOME=/data
ENV PATH=/data/tools:$PATH
ENV PYTHONUNBUFFERED=1

# Switch to app user (before runtime, not after setup)
USER backer

# Expose port
EXPOSE 8420

# Health check (30s start period allows time for server initialization)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8420/health || exit 1

# Run server
CMD ["backer", "server", "start", "--host", "0.0.0.0", "--port", "8420", "--data-dir", "/data"]
