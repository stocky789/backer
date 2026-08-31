"""Small S3 sidecar reader/writer; credentials stay in SigV4 headers."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import uuid4

import requests


class S3Sidecar:
    def __init__(self, settings: dict[str, str], credentials: dict[str, str]) -> None:
        self.bucket = settings["bucket"]
        self.prefix = settings.get("prefix", "").strip("/")
        self.endpoint = settings["endpoint"].rstrip("/")
        self.region = settings.get("region") or "us-east-1"
        self.access_key = credentials["access_key_id"]
        self.secret_key = credentials["secret_access_key"]

    def _key(self, key: str) -> str:
        return "/".join(part for part in (self.prefix, key.strip("/")) if part)

    def _url(self, key: str = "") -> str:
        suffix = quote(self._key(key), safe="/-_.~")
        return f"{self.endpoint}/{quote(self.bucket, safe='-_.~')}/{suffix}".rstrip("/")

    def _headers(
        self, method: str, key: str, payload: bytes = b"", extra: dict[str, str] | None = None, query: str = ""
    ) -> dict[str, str]:
        now = datetime.now(UTC)
        stamp, moment = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
        payload_hash = hashlib.sha256(payload).hexdigest()
        headers = {
            "host": self.endpoint.split("://", 1)[-1].split("/", 1)[0],
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": moment,
            **(extra or {}),
        }
        signed = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_path = f"/{quote(self.bucket, safe='-_.~')}/{quote(self._key(key), safe='/-_.~')}"
        canonical = f"{method}\n{canonical_path}\n{query}\n{canonical_headers}\n{signed}\n{payload_hash}"
        scope = f"{stamp}/{self.region}/s3/aws4_request"
        string = f"AWS4-HMAC-SHA256\n{moment}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        key_date = hmac.new(("AWS4" + self.secret_key).encode(), stamp.encode(), hashlib.sha256).digest()
        key_region = hmac.new(key_date, self.region.encode(), hashlib.sha256).digest()
        key_service = hmac.new(key_region, b"s3", hashlib.sha256).digest()
        signature = hmac.new(
            hmac.new(key_service, b"aws4_request", hashlib.sha256).digest(), string.encode(), hashlib.sha256
        ).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
        )
        return headers

    def _request(
        self, method: str, key: str = "", payload: bytes = b"", extra: dict[str, str] | None = None, query: str = ""
    ) -> requests.Response:
        response = requests.request(
            method,
            self._url(key) + (f"?{query}" if query else ""),
            data=payload,
            headers=self._headers(method, key, payload, extra, query),
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"S3 sidecar request failed ({response.status_code})")
        return response

    def get(self, key: str) -> bytes | None:
        response = requests.request("GET", self._url(key), headers=self._headers("GET", key), timeout=30)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeError(f"S3 sidecar request failed ({response.status_code})")
        return response.content

    def list(self, prefix: str) -> list[str]:
        query = f"list-type=2&prefix={quote(self._key(prefix), safe='/-_.~')}"
        response = self._request("GET", "", query=query)
        import xml.etree.ElementTree as ET

        root = ET.fromstring(response.content)
        return [item.text for item in root.findall(".//{*}Key") if item.text]

    def put_atomic(self, key: str, data: bytes) -> None:
        temporary = f"{key}.{uuid4().hex[:8]}.tmp"
        self._request("PUT", temporary, data)
        self._request("PUT", key, extra={"x-amz-copy-source": f"/{self.bucket}/{self._key(temporary)}"})
        self._request("DELETE", temporary)
