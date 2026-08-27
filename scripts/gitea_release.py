#!/usr/bin/env python3
"""Create or update a Gitea release and attach build outputs."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


class GiteaError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Gitea API returned {status}: {message}")
        self.status = status


def api_request(
    *,
    method: str,
    base_url: str,
    token: str,
    path: str,
    payload: dict | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
) -> object | None:
    url = f"{base_url.rstrip('/')}/api/v1{path}"
    data = body
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        content_type = "application/json"

    request = Request(url, data=data, method=method)
    request.add_header("Authorization", f"token {token}")
    if content_type:
        request.add_header("Content-Type", content_type)

    try:
        with urlopen(request, timeout=120) as response:
            response_body = response.read()
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise GiteaError(exc.code, message) from exc

    if not response_body:
        return None
    return json.loads(response_body.decode("utf-8"))


def repo_path(repository: str) -> str:
    try:
        owner, repo = repository.split("/", maxsplit=1)
    except ValueError as exc:
        raise SystemExit(f"Repository must be in owner/name form, got {repository!r}") from exc
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def read_version() -> str:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


def get_release(base_url: str, token: str, repository: str, tag: str) -> dict | None:
    base_path = repo_path(repository)
    tag_path = f"{base_path}/releases/tags/{quote(tag, safe='')}"
    try:
        release = api_request(method="GET", base_url=base_url, token=token, path=tag_path)
    except GiteaError as exc:
        if exc.status != 404:
            raise
        release = None
    return release if isinstance(release, dict) else None


def create_release(
    *,
    base_url: str,
    token: str,
    repository: str,
    tag: str,
    target: str,
    name: str,
    body: str,
    prerelease: bool,
) -> dict:
    release = api_request(
        method="POST",
        base_url=base_url,
        token=token,
        path=f"{repo_path(repository)}/releases",
        payload={
            "body": body,
            "draft": False,
            "name": name,
            "prerelease": prerelease,
            "tag_name": tag,
            "target_commitish": target,
        },
    )
    if not isinstance(release, dict):
        raise RuntimeError("Gitea did not return release metadata")
    return release


def update_release(
    *,
    base_url: str,
    token: str,
    repository: str,
    release_id: int,
    tag: str,
    target: str,
    name: str,
    body: str,
    prerelease: bool,
) -> dict:
    release = api_request(
        method="PATCH",
        base_url=base_url,
        token=token,
        path=f"{repo_path(repository)}/releases/{release_id}",
        payload={
            "body": body,
            "draft": False,
            "name": name,
            "prerelease": prerelease,
            "tag_name": tag,
            "target_commitish": target,
        },
    )
    if not isinstance(release, dict):
        raise RuntimeError("Gitea did not return updated release metadata")
    return release


def collect_assets(asset_dirs: list[str], assets: list[str]) -> list[Path]:
    files = [Path(asset) for asset in assets]
    for asset_dir in asset_dirs:
        root = Path(asset_dir)
        if root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file())

    unique: dict[str, Path] = {}
    for path in files:
        if not path.exists() or not path.is_file():
            raise SystemExit(f"Release asset does not exist: {path}")
        if path.name in unique:
            raise SystemExit(f"Duplicate release asset name: {path.name}")
        unique[path.name] = path

    return [unique[name] for name in sorted(unique)]


def delete_existing_asset(base_url: str, token: str, repository: str, release: dict, asset_name: str) -> None:
    release_id = release["id"]
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            api_request(
                method="DELETE",
                base_url=base_url,
                token=token,
                path=f"{repo_path(repository)}/releases/{release_id}/assets/{asset['id']}",
            )


def upload_asset(base_url: str, token: str, repository: str, release_id: int, asset: Path) -> None:
    boundary = f"----BackerRelease{uuid.uuid4().hex}"
    content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
    payload = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="attachment"; filename="{asset.name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            asset.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    query = urlencode({"name": asset.name})
    api_request(
        method="POST",
        base_url=base_url,
        token=token,
        path=f"{repo_path(repository)}/releases/{release_id}/assets?{query}",
        body=payload,
        content_type=f"multipart/form-data; boundary={boundary}",
    )


def env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise SystemExit(f"Missing required environment variable: {' or '.join(names)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Backer build outputs to a Gitea release.")
    parser.add_argument("--asset", action="append", default=[], help="File to attach to the release.")
    parser.add_argument("--asset-dir", action="append", default=[], help="Directory tree containing release assets.")
    parser.add_argument("--body", default="", help="Release body.")
    parser.add_argument("--name", default="", help="Release title.")
    parser.add_argument("--prerelease", action="store_true", help="Mark the release as a prerelease.")
    parser.add_argument("--prune", action="store_true", help="Delete release assets not in this upload.")
    parser.add_argument("--tag", default="", help="Release tag.")
    parser.add_argument("--target", default="", help="Target commit SHA.")
    parser.add_argument("--version", default="", help="Project version.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    version = args.version or read_version()
    tag = args.tag or f"v{version}"
    target = args.target or env_value("GITEA_COMMIT", "GITHUB_SHA")
    name = args.name or f"Backer {version}"
    body = args.body or f"Automated release for {target[:12]}."
    base_url = env_value("GITEA_BASE_URL", "GITHUB_SERVER_URL")
    repository = env_value("GITEA_REPOSITORY", "GITHUB_REPOSITORY")
    token = env_value("GITEA_TOKEN", "GITHUB_TOKEN")
    assets = collect_assets(args.asset_dir, args.asset)

    if not assets:
        raise SystemExit("No release assets were found")

    release = get_release(base_url, token, repository, tag)
    existing_assets = release.get("assets", []) if release else []
    if release is None:
        release = create_release(
            base_url=base_url,
            token=token,
            repository=repository,
            tag=tag,
            target=target,
            name=name,
            body=body,
            prerelease=args.prerelease,
        )
    else:
        release = update_release(
            base_url=base_url,
            token=token,
            repository=repository,
            release_id=release["id"],
            tag=tag,
            target=target,
            name=name,
            body=body,
            prerelease=args.prerelease,
        )
    release["assets"] = existing_assets

    for asset in assets:
        delete_existing_asset(base_url, token, repository, release, asset.name)
        upload_asset(base_url, token, repository, release["id"], asset)
        print(f"Uploaded {asset.name}")

    if args.prune:
        asset_names = {path.name for path in assets}
        for asset in release.get("assets", []):
            if asset.get("name") not in asset_names:
                delete_existing_asset(base_url, token, repository, release, asset["name"])
                print(f"Deleted stale asset {asset['name']}")

    print(f"Published {name} ({tag})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
