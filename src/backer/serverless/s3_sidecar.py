"""Small S3 sidecar reader/writer; credentials stay in SigV4 headers."""

from __future__ import annotations

import hashlib
import hmac
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
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
        if not self.prefix:
            return key
        return "/".join(part for part in (self.prefix, key.strip("/")) if part)

    def _bucket_url(self) -> str:
        return f"{self.endpoint}/{quote(self.bucket, safe='-_.~')}"

    def _url(self, key: str = "") -> str:
        suffix = quote(self._key(key), safe="/-_.~")
        return f"{self._bucket_url()}/{suffix}" if suffix else self._bucket_url()

    @staticmethod
    def _query_string(params: dict[str, str]) -> str:
        # SigV4 requires URI-encoded names/values, sorted by name.
        return "&".join(
            f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}" for name, value in sorted(params.items())
        )

    def _headers(
        self,
        method: str,
        key: str | None,
        payload: bytes = b"",
        extra: dict[str, str] | None = None,
        query: str = "",
        payload_hash: str | None = None,
    ) -> dict[str, str]:
        now = datetime.now(UTC)
        stamp, moment = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
        payload_hash = payload_hash or hashlib.sha256(payload).hexdigest()
        headers = {
            "host": self.endpoint.split("://", 1)[-1].split("/", 1)[0],
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": moment,
            **(extra or {}),
        }
        signed = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        if key is None:
            canonical_path = f"/{quote(self.bucket, safe='-_.~')}"
        else:
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

    @staticmethod
    def _error_text(response: requests.Response) -> str:
        detail = ""
        try:
            root = ET.fromstring(response.content)
            code = (root.findtext(".//{*}Code") or "").strip()
            message = (root.findtext(".//{*}Message") or "").strip()
            detail = ": ".join(part for part in (code, message) if part)
        except ET.ParseError:
            detail = ""
        return f"S3 sidecar request failed ({response.status_code}{f' {detail}' if detail else ''})"

    def _request(
        self,
        method: str,
        key: str | None = "",
        payload: bytes = b"",
        extra: dict[str, str] | None = None,
        query: str = "",
    ) -> requests.Response:
        url = self._bucket_url() if key is None else self._url(key)
        response = requests.request(
            method,
            url + (f"?{query}" if query else ""),
            data=payload,
            headers=self._headers(method, key, payload, extra, query),
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._error_text(response))
        return response

    def get(self, key: str) -> bytes | None:
        response = requests.request("GET", self._url(key), headers=self._headers("GET", key), timeout=30)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeError(self._error_text(response))
        return response.content

    def list(self, prefix: str) -> list[str]:
        scoped = self._key(prefix)
        if prefix.endswith("/") and not scoped.endswith("/"):
            scoped += "/"
        params = {"list-type": "2", "prefix": scoped}
        keys: list[str] = []
        seen_tokens: set[str] = set()
        while True:
            response = self._request("GET", None, query=self._query_string(params))
            root = ET.fromstring(response.content)
            page = [item.text for item in root.findall(".//{*}Key") if item.text]
            if any(not key.startswith(scoped) for key in page):
                raise ValueError("S3 listing escaped the requested prefix")
            keys.extend(page)
            if (root.findtext(".//{*}IsTruncated") or "").lower() != "true":
                return keys
            token = root.findtext(".//{*}NextContinuationToken")
            if not token or token in seen_tokens:
                raise ValueError("S3 listing was incomplete")
            seen_tokens.add(token)
            params["continuation-token"] = token

    def put_if_absent(self, key: str, data: bytes) -> None:
        self._request("PUT", key, data, extra={"if-none-match": "*"})

    def delete(self, key: str) -> None:
        self._request("DELETE", key)

    def upload_if_absent(self, key: str, path: Path) -> None:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            handle.seek(0)
            response = requests.request(
                "PUT", self._url(key), data=handle,
                headers=self._headers("PUT", key, extra={"if-none-match": "*"}, payload_hash=digest.hexdigest()),
                timeout=30,
            )
        if response.status_code >= 400:
            raise RuntimeError(self._error_text(response))

    def download(self, key: str, path: Path) -> None:
        with requests.request(
            "GET", self._url(key), headers=self._headers("GET", key), timeout=30, stream=True,
        ) as response:
            if response.status_code >= 400:
                raise RuntimeError(self._error_text(response))
            with path.open("xb") as handle:
                for block in response.iter_content(chunk_size=1024 * 1024):
                    handle.write(block)

    def wipe(self) -> None:
        """Delete every object under this sidecar's configured prefix.

        An empty prefix covers the dedicated bucket's contents. Kopia concatenates
        its blob names directly onto the configured prefix, without adding a slash.
        """
        root_prefix = self.prefix.strip("/")
        if "\0" in root_prefix or (root_prefix and any(part in ("", ".", "..") for part in root_prefix.split("/"))):
            raise ValueError("Invalid S3 repository prefix; refusing to delete storage")
        scoped = root_prefix
        bucket = S3Sidecar(
            {"bucket": self.bucket, "endpoint": self.endpoint, "region": self.region},
            {"access_key_id": self.access_key, "secret_access_key": self.secret_key},
        )
        token: str | None = None
        repository_key: str | None = None
        while True:
            params = {"list-type": "2", "prefix": scoped}
            if token:
                params["continuation-token"] = token
            response = self._request("GET", None, query=self._query_string(params))
            tree = ET.fromstring(response.content)
            keys = [item.text for item in tree.findall(".//{*}Key") if item.text]
            for key in keys:
                if not key.startswith(scoped):
                    raise ValueError("Listed key escapes the repository prefix; refusing to delete storage")
                if key == scoped + "kopia.repository":
                    # Keep identity verification available if a later deletion fails.
                    repository_key = key
                    continue
                bucket._request("DELETE", key)
            truncated = (tree.findtext(".//{*}IsTruncated") or "").lower() == "true"
            token = tree.findtext(".//{*}NextContinuationToken")
            if truncated and not token:
                raise ValueError("S3 listing was incomplete; local repository access was retained")
            if not truncated:
                break
        if repository_key is not None:
            bucket._request("DELETE", repository_key)

    def put_atomic(self, key: str, data: bytes) -> None:
        temporary = f"{key}.{uuid4().hex[:8]}.tmp"
        self._request("PUT", temporary, data)
        copy_source = f"/{quote(self.bucket, safe='-_.~')}/{quote(self._key(temporary), safe='/-_.~')}"
        copy_error: BaseException | None = None
        try:
            self._request("PUT", key, extra={"x-amz-copy-source": copy_source})
        except BaseException as error:
            copy_error = error
            raise
        finally:
            try:
                self._request("DELETE", temporary)
            except Exception:
                if copy_error is None:
                    raise
