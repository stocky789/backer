"""Small S3 sidecar reader/writer; credentials stay in SigV4 headers."""

from __future__ import annotations

import hashlib
import hmac
import xml.etree.ElementTree as ET
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

    def _bucket_url(self) -> str:
        return f"{self.endpoint}/{quote(self.bucket, safe='-_.~')}"

    def _url(self, key: str = "") -> str:
        suffix = quote(self._key(key), safe="/-_.~")
        return f"{self._bucket_url()}/{suffix}".rstrip("/")

    @staticmethod
    def _query_string(params: dict[str, str]) -> str:
        # SigV4 requires URI-encoded names/values, sorted by name.
        return "&".join(f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}" for name, value in sorted(params.items()))

    def _headers(
        self,
        method: str,
        key: str | None,
        payload: bytes = b"",
        extra: dict[str, str] | None = None,
        query: str = "",
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
        query = self._query_string({"list-type": "2", "prefix": self._key(prefix)})
        response = self._request("GET", None, query=query)
        root = ET.fromstring(response.content)
        return [item.text for item in root.findall(".//{*}Key") if item.text]

    def wipe(self) -> None:
        """Delete every object under this sidecar's configured prefix.

        Refuses an empty prefix (bucket root). Lists with pagination, then deletes
        each key relative to the prefix so `_key` does not double-prefix.
        """
        root_prefix = self.prefix.strip("/")
        if not root_prefix or any(part in ("", ".", "..") for part in root_prefix.split("/")):
            raise ValueError("Storage deletion requires a non-root S3 repository prefix")
        scoped = root_prefix + "/"
        token: str | None = None
        while True:
            params = {"list-type": "2", "prefix": scoped}
            if token:
                params["continuation-token"] = token
            response = self._request("GET", None, query=self._query_string(params))
            tree = ET.fromstring(response.content)
            keys = [item.text for item in tree.findall(".//{*}Key") if item.text]
            for key in keys:
                if key != root_prefix and not key.startswith(scoped):
                    raise ValueError("Listed key escapes the repository prefix; refusing to delete storage")
                relative = "" if key == root_prefix else key.removeprefix(scoped)
                self._request("DELETE", relative)
            truncated = (tree.findtext(".//{*}IsTruncated") or "").lower() == "true"
            token = tree.findtext(".//{*}NextContinuationToken")
            if not truncated or not token:
                break

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
