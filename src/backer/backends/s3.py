"""Validated S3 configuration for Kopia repositories."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


class S3ConfigError(ValueError):
    """Raised when an S3 repository cannot be configured safely."""


@dataclass(frozen=True)
class S3Config:
    bucket: str
    prefix: str
    endpoint: str
    region: str
    access_key_id: str
    secret_access_key: str

    @property
    def public_config(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "endpoint": self.endpoint,
            "region": self.region,
        }


def parse_s3_config(data: dict[str, object]) -> S3Config:
    """Validate S3 configuration; callers persist credentials separately."""
    def required(name: str) -> str:
        value = str(data.get(name, "")).strip()
        if not value:
            raise S3ConfigError(f"S3 {name.replace('_', ' ')} is required")
        return value

    bucket = required("bucket")
    if "/" in bucket or bucket in {".", ".."}:
        raise S3ConfigError("S3 bucket must be a bucket name, not a path")
    prefix = str(data.get("prefix", "")).strip("/")
    if ".." in prefix.split("/"):
        raise S3ConfigError("S3 prefix cannot contain '..'")
    endpoint = required("endpoint").rstrip("/")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.netloc
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise S3ConfigError("S3 endpoint must be an http(s) URL without credentials or a path")
    return S3Config(
        bucket=bucket,
        prefix=prefix,
        endpoint=endpoint,
        region=required("region"),
        access_key_id=required("access_key_id"),
        secret_access_key=required("secret_access_key"),
    )


def kopia_s3_config(data: dict[str, object]) -> dict[str, object]:
    """Return the Kopia S3 boundary without embedding provider keys in arguments."""
    config = parse_s3_config(data)
    endpoint = urlparse(config.endpoint)
    options = [
        "--bucket", config.bucket,
        "--prefix", config.prefix,
        "--endpoint", endpoint.netloc,
        "--region", config.region,
    ]
    if endpoint.scheme == "http":
        options.append("--disable-tls")
    return {
        "repository": f"s3://{config.bucket}/{config.prefix}".rstrip("/"),
        "options": options,
        "environment": {
            "AWS_ACCESS_KEY_ID": config.access_key_id,
            "AWS_SECRET_ACCESS_KEY": config.secret_access_key,
        },
        "public_config": config.public_config,
    }
