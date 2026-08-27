"""S3 settings for the single supported cloud backend: Restic."""

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
    use_path_style: bool

    @property
    def restic_repository(self) -> str:
        path = f"{self.bucket}/{self.prefix}" if self.prefix else self.bucket
        return f"s3:{self.endpoint}/{path}"

    @property
    def restic_options(self) -> list[str]:
        lookup = "path" if self.use_path_style else "dns"
        return ["-o", f"s3.region={self.region}", "-o", f"s3.bucket-lookup={lookup}"]

    @property
    def environment(self) -> dict[str, str]:
        return {
            "AWS_ACCESS_KEY_ID": self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
            "AWS_DEFAULT_REGION": self.region,
        }

    @property
    def public_config(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "endpoint": self.endpoint,
            "region": self.region,
            "use_path_style": self.use_path_style,
        }


def parse_s3_config(data: dict[str, object]) -> S3Config:
    """Validate one Restic S3 repository configuration.

    Secrets are deliberately inputs only; callers persist them separately from
    ``public_config`` and pass them to an agent only for a repository operation.
    """
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
        use_path_style=bool(data.get("use_path_style", True)),
    )


def restic_s3_config(data: dict[str, object]) -> dict[str, object]:
    """Return the destination, options and environment Restic needs for S3."""
    config = parse_s3_config(data)
    return {
        "repository": config.restic_repository,
        "options": config.restic_options,
        "environment": config.environment,
        "public_config": config.public_config,
    }
