import hashlib

import pytest

from backer.serverless.s3_sidecar import S3Sidecar


def test_paginated_listing_and_conditional_streaming(tmp_path, monkeypatch):
    calls = []

    class Response:
        status_code = 200
        content = b""

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_content(self, chunk_size):
            yield b"hello"

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = Response()
        if "list-type" in url:
            page = 2 if "continuation-token" in url else 1
            more = (
                "<IsTruncated>true</IsTruncated><NextContinuationToken>next</NextContinuationToken>"
                if page == 1 else ""
            )
            response.content = (
                f"<ListBucketResult><Contents><Key>repo/snapshots/{page}</Key></Contents>{more}</ListBucketResult>"
            ).encode()
        if method == "PUT":
            data = kwargs["data"]
            assert (data.read() if hasattr(data, "read") else data) == b"hello"
            assert kwargs["headers"]["if-none-match"] == "*"
            assert kwargs["headers"]["x-amz-content-sha256"] == hashlib.sha256(b"hello").hexdigest()
        return response

    monkeypatch.setattr("backer.serverless.s3_sidecar.requests.request", request)
    transport = S3Sidecar(
        {"bucket": "bucket", "prefix": "repo", "endpoint": "https://s3.example"},
        {"access_key_id": "access", "secret_access_key": "secret"},
    )
    assert transport.list("snapshots/") == ["repo/snapshots/1", "repo/snapshots/2"]
    assert "prefix=repo%2Fsnapshots%2F" in calls[0][1]
    path = tmp_path / "file"
    path.write_bytes(b"hello")
    transport.upload_if_absent("contents/file", path)
    transport.put_if_absent("manifest", b"hello")
    output = tmp_path / "output"
    transport.download("contents/file", output)
    assert output.read_bytes() == b"hello"
    with pytest.raises(FileExistsError):
        transport.download("contents/file", output)


@pytest.mark.parametrize("body", [
    b"<ListBucketResult><IsTruncated>true</IsTruncated></ListBucketResult>",
    b"<ListBucketResult><Contents><Key>repo-neighbor/file</Key></Contents></ListBucketResult>",
])
def test_listing_fails_closed_on_incomplete_or_out_of_scope_response(monkeypatch, body):
    monkeypatch.setattr(
        "backer.serverless.s3_sidecar.requests.request",
        lambda *_args, **_kwargs: type("Response", (), {"status_code": 200, "content": body})(),
    )
    transport = S3Sidecar(
        {"bucket": "bucket", "prefix": "repo", "endpoint": "https://s3.example"},
        {"access_key_id": "access", "secret_access_key": "secret"},
    )
    with pytest.raises(ValueError):
        transport.list("snapshots/")
