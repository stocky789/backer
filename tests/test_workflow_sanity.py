"""Sanity checks for CI, packaging, and release assumptions."""

from __future__ import annotations

import importlib
import importlib.metadata
import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

import click
import yaml

ROOT = Path(__file__).resolve().parents[1]


def read_pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_sdist_excludes_local_workspace_artifacts() -> None:
    excludes = set(read_pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"])
    assert {"/.chunkhound", "/.superpowers", "/docs", "/serverless-backups.md"} <= excludes


def test_readme_documents_legacy_repository_recovery() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for text in (
        "backer-default-password",
        "backer repo recover NAME --passphrase-stdin",
        "fresh temporary Kopia config",
        "kopia repository status",
        "kopia repository change-password --new-password",
        "unrecoverable",
    ):
        assert text in readme
    assert readme.index("kopia repository status") < readme.index("kopia repository change-password --new-password")
    assert readme.count("kopia repository status") == 2
    assert "Operators can recover repositories orphaned by 0.8.0" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def expected_android_version_code(version: str) -> int:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None
    major, minor, patch = (int(part) for part in match.groups())
    return major * 10000 + minor * 100 + patch


def test_release_version_files_match() -> None:
    pyproject = read_pyproject()
    project_version = pyproject["project"]["version"]
    version_module = importlib.import_module("backer._version")
    installer_text = (ROOT / "installer" / "backer-agent.iss").read_text(encoding="utf-8")
    android_gradle = (ROOT / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8")

    installer_match = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', installer_text)
    android_name_match = re.search(r'versionName\s*=\s*"([^"]+)"', android_gradle)
    android_code_match = re.search(r"versionCode\s*=\s*(\d+)", android_gradle)

    assert installer_match is not None
    assert android_name_match is not None
    assert android_code_match is not None
    assert project_version == version_module.__version__
    assert installer_match.group(1) == project_version
    assert android_name_match.group(1) == project_version
    assert int(android_code_match.group(1)) == expected_android_version_code(project_version)


def test_make_release_uses_the_atomic_version_bump_script() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "python scripts/bump_version.py $(VERSION)" in makefile
    assert "sed -i 's/version" not in makefile


def test_android_release_proguard_handles_optional_archive_dependencies() -> None:
    proguard_rules = (ROOT / "android" / "app" / "proguard-rules.pro").read_text(encoding="utf-8")

    assert "org.apache.commons.compress.archivers.tar" in proguard_rules
    assert "org.apache.commons.compress.compressors.gzip" in proguard_rules
    assert "-dontwarn com.github.luben.zstd.**" in proguard_rules
    assert "-dontwarn org.brotli.dec.**" in proguard_rules
    assert "-dontwarn org.objectweb.asm.**" in proguard_rules
    assert "-dontwarn org.tukaani.xz.**" in proguard_rules
    assert "-dontwarn com.google.errorprone.annotations.**" in proguard_rules


def test_docker_compose_exposes_server_port_and_persistent_data() -> None:
    compose_path = ROOT / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    backer_service = compose["services"]["backer"]

    assert "8420:8420" in backer_service["ports"]
    assert "backer-data:/data" in backer_service["volumes"]
    assert "backer-data" in compose["volumes"]


def test_public_url_is_configured_in_the_setup_wizard() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    setup_template = (ROOT / "src/backer/server/web/templates/setup.html").read_text(encoding="utf-8")

    assert "BACKER_PUBLIC_URL" not in compose_text
    assert 'name="public_url"' in setup_template
    assert "https://backer.example.com" in readme_text
    assert "http://192.168.1.100:8420" in readme_text


def test_repository_job_subfolder_replaces_unsafe_path_characters() -> None:
    repository_paths = importlib.import_module("backer.core.paths")

    assert repository_paths.get_job_subfolder("Daily:VM/Backup?*") == "Daily_VM_Backup__"


def test_expected_backend_names_are_registered() -> None:
    from backer.backends.base import BackendType
    from backer.backends.registry import BackendRegistry

    assert set(BackendRegistry.available_backends()) == {BackendType.KOPIA, BackendType.PROXY}


def test_server_route_modules_import_without_creating_app() -> None:
    app_module = importlib.import_module("backer.server.app")
    web_routes = importlib.import_module("backer.server.web.routes")

    assert callable(app_module.create_app)
    assert web_routes.router.routes


def test_installed_package_exposes_expected_console_scripts() -> None:
    pyproject = read_pyproject()
    expected_scripts = pyproject["project"]["scripts"]
    distribution = importlib.metadata.distribution(pyproject["project"]["name"])
    installed_scripts = {
        entry_point.name: entry_point.value
        for entry_point in distribution.entry_points
        if entry_point.group == "console_scripts"
    }

    assert installed_scripts["backer"] == expected_scripts["backer"]
    assert installed_scripts["backer-server"] == expected_scripts["backer-server"]

    for target in expected_scripts.values():
        module_name, function_name = target.split(":", maxsplit=1)
        module = importlib.import_module(module_name)
        assert callable(getattr(module, function_name))


def workflow_triggers(workflow_path: Path) -> dict:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    return workflow.get("on") or workflow.get(True)


def test_github_and_gitea_have_rolling_release_workflows() -> None:
    for path in (
        ".github/workflows/release.yml",
        ".github/workflows/gitea-release.yml",
        ".gitea/workflows/main-release.yml",
    ):
        triggers = workflow_triggers(ROOT / path)

        assert triggers["push"]["branches"] == ["main", "dev"]
        assert triggers["pull_request"]["branches"] == ["main", "dev"]
        assert "workflow_dispatch" in triggers


def test_branch_validation_workflows_do_not_publish_releases() -> None:
    gitea_workflows = ROOT / ".gitea" / "workflows"
    for workflow_name in ("android-build.yml", "docker-build.yml", "python-ci.yml"):
        workflow_text = (gitea_workflows / workflow_name).read_text(encoding="utf-8")
        triggers = workflow_triggers(gitea_workflows / workflow_name)

        assert triggers == "workflow_dispatch"
        assert "scripts/gitea_release.py" not in workflow_text
        assert "releases: write" not in workflow_text
        assert "GITEA_TOKEN" not in workflow_text


def test_rolling_release_workflows_only_publish_after_pr_validation() -> None:
    github = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    gitea_workflows = [
        yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
        for path in (".github/workflows/gitea-release.yml", ".gitea/workflows/main-release.yml")
    ]

    for workflow, job_names in (
        *((workflow, ("publish-release",)) for workflow in gitea_workflows),
        (github, ("publish-release", "docker-publish")),
    ):
        for job_name in job_names:
            guard = str(workflow["jobs"][job_name]["if"])
            assert "github.event_name != 'pull_request'" in guard or "github.event_name == 'push'" in guard


def test_rolling_release_permissions_are_scoped_to_publish_jobs() -> None:
    github = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    active_gitea = yaml.safe_load((ROOT / ".github/workflows/gitea-release.yml").read_text(encoding="utf-8"))
    gitea = yaml.safe_load((ROOT / ".gitea/workflows/main-release.yml").read_text(encoding="utf-8"))

    assert github["permissions"] == {"contents": "read"}
    assert active_gitea["permissions"] == {"contents": "read"}
    assert gitea["permissions"] == {"contents": "read"}

    assert github["jobs"]["publish-release"]["permissions"]["contents"] == "write"
    assert github["jobs"]["docker-publish"]["permissions"]["packages"] == "write"
    assert active_gitea["jobs"]["publish-release"]["permissions"] == {"contents": "write"}
    assert gitea["jobs"]["publish-release"]["permissions"]["code"] == "write"
    assert gitea["jobs"]["publish-release"]["permissions"]["releases"] == "write"
    assert all(
        name == "publish-release"
        or all(job.get("permissions", {}).get(scope) != "write" for scope in ("code", "releases"))
        for name, job in gitea["jobs"].items()
    )


def test_github_release_concurrency_keeps_branch_pushes_independent() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    gitea_workflow = yaml.safe_load((ROOT / ".github/workflows/gitea-release.yml").read_text(encoding="utf-8"))
    concurrency = workflow["concurrency"]
    gitea_concurrency = gitea_workflow["concurrency"]

    assert "github.ref" in str(concurrency["group"])
    assert "github.ref" in str(gitea_concurrency["group"])
    assert concurrency["group"] != gitea_concurrency["group"]
    cancel_in_progress = concurrency["cancel-in-progress"]
    assert cancel_in_progress is False or (
        "github.event_name" in str(cancel_in_progress) and "pull_request" in str(cancel_in_progress)
    )


def test_github_release_workflow_is_github_only() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert jobs["release-info"]["if"] == "github.server_url == 'https://github.com'"
    assert all(
        "release-info" in (job["needs"] if isinstance(job["needs"], list) else [job["needs"]])
        for name, job in jobs.items()
        if name != "release-info"
    )
    assert [
        step["uses"]
        for job in jobs.values()
        for step in job["steps"]
        if step.get("uses", "").split("@", maxsplit=1)[0] in {"actions/upload-artifact", "actions/download-artifact"}
    ] == [
        "actions/upload-artifact@v4",
        "actions/upload-artifact@v4",
        "actions/upload-artifact@v4",
        "actions/download-artifact@v4",
    ]
    for job_name, artifact_name, artifact_path in (
        ("python-package", "python-release-files", "dist/*"),
        ("android-release", "android-release-files", "release-assets/*"),
        ("windows-agent-package", "windows-release-files", "release-assets/*"),
    ):
        upload = next(step for step in jobs[job_name]["steps"] if step.get("uses") == "actions/upload-artifact@v4")
        assert upload["with"] == {
            "name": artifact_name,
            "path": artifact_path,
            "if-no-files-found": "error",
        }

    metadata = next(step for step in jobs["release-info"]["steps"] if step.get("id") == "info")["run"]
    assert "main) tag=release-main" in metadata
    assert "dev) tag=release-dev" in metadata

    branch_guard = "(github.ref == 'refs/heads/main' || github.ref == 'refs/heads/dev')"
    github_guard = f"github.server_url == 'https://github.com' && github.event_name != 'pull_request' && {branch_guard}"
    for job_name in ("docker-publish", "publish-release"):
        assert jobs[job_name]["if"] == github_guard

    download = next(
        step for step in jobs["publish-release"]["steps"] if step.get("uses") == "actions/download-artifact@v4"
    )
    assert download["with"] == {
        "pattern": "*-release-files",
        "path": "release-assets",
        "merge-multiple": True,
    }


def test_gitea_release_workflow_is_gitea_only() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/gitea-release.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert jobs["release-info"]["if"] == "github.server_url != 'https://github.com'"
    assert all(
        "release-info" in (job["needs"] if isinstance(job["needs"], list) else [job["needs"]])
        for name, job in jobs.items()
        if name != "release-info"
    )
    assert [
        step["uses"]
        for job in jobs.values()
        for step in job["steps"]
        if step.get("uses", "").split("@", maxsplit=1)[0] in {"actions/upload-artifact", "actions/download-artifact"}
    ] == [
        "actions/upload-artifact@v3.2.2-node20",
        "actions/upload-artifact@v3.2.2-node20",
        "actions/upload-artifact@v3.2.2-node20",
        "actions/download-artifact@v3-node20",
    ]
    for job_name, artifact_name, artifact_path in (
        ("python-package", "python-release-files", "dist/*"),
        ("android-release", "android-release-files", "release-assets/*"),
        ("windows-agent-package", "windows-release-files", "release-assets/*"),
    ):
        upload = next(
            step for step in jobs[job_name]["steps"] if step.get("uses") == "actions/upload-artifact@v3.2.2-node20"
        )
        assert upload["with"] == {
            "name": artifact_name,
            "path": artifact_path,
            "if-no-files-found": "error",
        }

    metadata = next(step for step in jobs["release-info"]["steps"] if step.get("id") == "version")["run"]
    assert 'tag, title, prerelease = "release-main", f"Backer {version}", "false"' in metadata
    assert 'tag, title, prerelease = "release-dev", f"Backer {version} dev (' in metadata

    gitea_publish = jobs["publish-release"]
    branch_guard = "(github.ref == 'refs/heads/main' || github.ref == 'refs/heads/dev')"
    assert gitea_publish["if"] == (
        "always() && github.server_url != 'https://github.com' && "
        f"github.event_name != 'pull_request' && {branch_guard}"
    )
    assert gitea_publish["permissions"] == {"contents": "write"}
    download = next(
        step for step in gitea_publish["steps"] if step.get("uses") == "actions/download-artifact@v3-node20"
    )
    assert download["with"] == {"path": "release-assets"}
    publish = next(step for step in gitea_publish["steps"] if "scripts/gitea_release.py" in step.get("run", ""))
    assert publish["env"]["GITEA_TOKEN"] == "${{ secrets.GITEA_TOKEN }}"
    assert "--prune" in publish["run"]


def test_rolling_release_workflows_publish_expected_assets_and_channels() -> None:
    for path, artifact_version in (
        (".github/workflows/release.yml", "v4"),
        (".github/workflows/gitea-release.yml", "v3"),
        (".gitea/workflows/main-release.yml", "v3"),
    ):
        workflow_text = (ROOT / path).read_text(encoding="utf-8")

        assert f"actions/upload-artifact@{artifact_version}" in workflow_text
        assert f"actions/download-artifact@{artifact_version}" in workflow_text
        assert "release-main" in workflow_text
        assert "release-dev" in workflow_text
        assert "prerelease" in workflow_text
        assert "pyproject.toml" in workflow_text
        assert "GITHUB_SHA" in workflow_text
        assert "GITHUB_BASE_REF" in workflow_text
        assert "backer-agent-setup.exe" in workflow_text
        assert "backer-agent-windows-portable.zip" in workflow_text
        assert "backer-android.apk" in workflow_text
        assert "dist/" in workflow_text

    for path in (".github/workflows/gitea-release.yml", ".gitea/workflows/main-release.yml"):
        gitea_release = (ROOT / path).read_text(encoding="utf-8")
        assert "scripts/gitea_release.py" in gitea_release
        assert "--prune" in gitea_release
    github_release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "$image:$version,$image:latest" in github_release
    assert "$image:dev,$image:dev-$shortsha" in github_release
    assert "gh release create" in github_release
    assert "--verify-tag" in github_release


def test_gitea_release_prerelease_update_prunes_stale_assets_after_upload(monkeypatch, tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    gitea_release = importlib.import_module("gitea_release")
    fresh_asset = tmp_path / "fresh.zip"
    fresh_asset.write_bytes(b"fresh")
    calls: list[tuple[str, str, dict | None]] = []

    def fake_api_request(*, method: str, path: str, payload: dict | None = None, **_kwargs):
        calls.append((method, path, payload))
        if method == "GET":
            return {
                "id": 7,
                "assets": [
                    {"id": 1, "name": "fresh.zip"},
                    {"id": 2, "name": "obsolete.zip"},
                ],
            }
        if method == "PATCH":
            return {"id": 7, "assets": [{"id": 1, "name": "fresh.zip"}, {"id": 2, "name": "obsolete.zip"}]}
        return None

    monkeypatch.setattr(gitea_release, "api_request", fake_api_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gitea_release.py",
            "--asset",
            str(fresh_asset),
            "--prerelease",
            "--prune",
            "--tag",
            "release-dev",
            "--target",
            "abc123",
        ],
    )
    monkeypatch.setenv("GITEA_BASE_URL", "https://gitea.example.test")
    monkeypatch.setenv("GITEA_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITEA_TOKEN", "token")

    assert gitea_release.main() == 0
    update = next(payload for method, _, payload in calls if method == "PATCH")
    assert update is not None and update["prerelease"] is True

    uploaded_at = next(
        index for index, (method, path, _) in enumerate(calls) if method == "POST" and "/assets?" in path
    )
    stale_delete_at = next(
        index for index, (method, path, _) in enumerate(calls) if method == "DELETE" and path.endswith("/assets/2")
    )
    assert stale_delete_at > uploaded_at

    calls.clear()
    monkeypatch.setattr(
        sys,
        "argv",
        ["gitea_release.py", "--asset", str(fresh_asset), "--tag", "release-dev", "--target", "abc123"],
    )

    assert gitea_release.main() == 0
    assert not any(method == "DELETE" and path.endswith("/assets/2") for method, path, _ in calls)


def test_windows_agent_build_stages_installer_tool_files() -> None:
    build_script = (ROOT / "scripts" / "build_agent.py").read_text(encoding="utf-8")

    assert 'DIST_TOOLS_DIR = DIST_DIR / "tools"' in build_script
    assert "download_windows_tool(tool)" in build_script
    assert '"kopia.exe"' in build_script
    assert "shutil.copy(src, DIST_TOOLS_DIR / tool)" in build_script


def test_windows_agent_executable_has_version_resource() -> None:
    build_script = (ROOT / "scripts" / "build_agent.py").read_text(encoding="utf-8")
    spec_file = (ROOT / "backer-agent.spec").read_text(encoding="utf-8")

    assert 'VERSION_FILE = BUILD_DIR / "backer-agent-version.txt"' in build_script
    assert "StringStruct('FileVersion', '{__version__}')" in build_script
    assert "StringStruct('ProductVersion', '{__version__}')" in build_script
    assert "write_pyinstaller_version_file()" in build_script
    assert "version=str(version_file) if version_file.exists() else None" in spec_file


def test_release_workflow_checks_all_release_versions_and_manual_tag_ref() -> None:
    release_workflow = (ROOT / ".gitea" / "workflows" / "release-validation.yml").read_text(encoding="utf-8")

    assert "ref: ${{ inputs.release_tag || github.ref }}" in release_workflow
    assert "installer/backer-agent.iss" in release_workflow
    assert "android/app/build.gradle.kts" in release_workflow
    assert "versionCode" in release_workflow
    assert "minio/minio:RELEASE.2025-09-07T16-13-09Z server /data" in release_workflow


def test_serverless_release_cells_are_named_and_pinned() -> None:
    workflow = (ROOT / ".gitea" / "workflows" / "release-validation.yml").read_text(encoding="utf-8")

    assert "name: Kopia S3 Contract (${{ matrix.os }})" in workflow
    assert "Run Kopia S3 end-to-end contract" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "BACKER_TEST_S3_BUCKET: backer-test" in workflow
    assert "minio.RELEASE.2025-09-07T16-13-09Z.sha256sum" in workflow
    assert "minio.RELEASE.2025-09-07T16-13-09Z" in workflow
    assert "$env:MINIO_ROOT_USER = 'backer-access'" in workflow
    assert "$env:MINIO_ROOT_PASSWORD = 'backer-secret-key'" in workflow
    assert "dperson/samba@sha256:e1d2a7366690749a7be06f72bdbf6a5a7d15726fc84e4e4f41e967214516edfd" in workflow
    assert "serverless-local:" in workflow
    assert "serverless-smb-linux:" in workflow
    assert "serverless-smb-windows:" in workflow


def test_serverless_validation_jobs_have_the_required_gates() -> None:
    workflow = yaml.safe_load((ROOT / ".gitea" / "workflows" / "release-validation.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    local, linux_smb, windows_smb, s3 = (
        jobs[name] for name in ("serverless-local", "serverless-smb-linux", "serverless-smb-windows", "s3-contract")
    )
    assert local["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest"]
    gui = next(step for step in local["steps"] if "xvfb-run" in step.get("run", ""))
    assert gui["if"] == "matrix.os == 'ubuntu-latest'"
    assert "cifs-utils" in "\n".join(step.get("run", "") for step in linux_smb["steps"])
    assert "445" in "\n".join(step.get("run", "") for step in linux_smb["steps"])
    linux_test = next(step for step in linux_smb["steps"] if "test_serverless_e2e.py" in step.get("run", ""))
    assert set(linux_test["env"]) >= {
        "BACKER_TEST_SMB_SERVER",
        "BACKER_TEST_SMB_SHARE",
        "BACKER_TEST_SMB_USERNAME",
        "BACKER_TEST_SMB_PASSWORD",
    }
    windows_setup = "\n".join(step.get("run", "") for step in windows_smb["steps"])
    assert all(command in windows_setup for command in ("New-SmbShare", "New-LocalUser", "icacls"))
    assert "Get-FileHash minio.exe -Algorithm SHA256" in "\n".join(step.get("run", "") for step in s3["steps"])
    s3_serverless = next(step for step in s3["steps"] if "test_serverless_e2e.py -k s3" in step.get("run", ""))
    assert set(s3_serverless["env"]) == {
        "BACKER_TEST_S3_ENDPOINT",
        "BACKER_TEST_S3_BUCKET",
        "BACKER_TEST_S3_ACCESS_KEY",
        "BACKER_TEST_S3_SECRET_KEY",
    }
    validation_jobs = (local, linux_smb, windows_smb, s3)
    assert not any("upload-artifact" in step.get("uses", "") for job in validation_jobs for step in job["steps"])
    linux_setup = "\n".join(step.get("run", "") for step in linux_smb["steps"])
    assert 'chmod 0777 "$RUNNER_TEMP/backer-share"' in linux_setup
    assert linux_test["run"] == "sudo -E python -m pytest -q tests/test_serverless_e2e.py -k smb_linux"
    assert "$password = ConvertTo-SecureString 'BackerCi9!Smb' -AsPlainText -Force" in windows_setup
    windows_test = next(step for step in windows_smb["steps"] if "test_serverless_e2e.py" in step.get("run", ""))
    assert windows_test["env"]["BACKER_TEST_SMB_PASSWORD"] == "BackerCi9!Smb"


def test_every_needed_job_is_checked() -> None:
    workflow = yaml.safe_load((ROOT / ".gitea" / "workflows" / "release-validation.yml").read_text(encoding="utf-8"))
    release = workflow["jobs"]["release-artifacts-ready"]
    script = next(step["run"] for step in release["steps"] if step.get("name") == "Check release jobs")
    checked = set(re.findall(r"\b[A-Z][A-Z0-9_]+_RESULT\b", script))
    result_jobs = {
        variable: match.group(1)
        for variable, value in next(step["env"] for step in release["steps"] if "env" in step).items()
        if (match := re.search(r"needs\.([\w-]+)\.result", value))
    }

    assert set(release["needs"]) <= {result_jobs[name] for name in checked}
    mandatory = re.search(r"for job in ([A-Z0-9_ ]+); do", script)
    optional = re.search(r"for optional_job in ([A-Z0-9_ ]+); do", script)
    assert mandatory and optional
    required_results = {
        "VERIFY_VERSION_RESULT",
        "SERVERLESS_LOCAL_RESULT",
        "SERVERLESS_SMB_LINUX_RESULT",
        "SERVERLESS_SMB_WINDOWS_RESULT",
    }
    assert required_results <= set(mandatory.group(1).split())
    assert not required_results & set(optional.group(1).split())


def test_cli_choices_match_ci_jobs() -> None:
    from backer.agent.gui.support import PROVEN_SERVERLESS_CELLS
    from backer.cli import main

    jobs = yaml.safe_load((ROOT / ".gitea/workflows/release-validation.yml").read_text(encoding="utf-8"))["jobs"]
    release = jobs["release-artifacts-ready"]
    gate = next(step for step in release["steps"] if step.get("name") == "Check release jobs")
    mandatory = set(re.search(r"for job in ([A-Z0-9_ ]+); do", gate["run"]).group(1).split())
    result_jobs = {
        name: match.group(1)
        for name, value in gate["env"].items()
        if (match := re.search(r"needs\.([\w-]+)\.result", value))
    }
    job_types = {
        "serverless-local": "local",
        "serverless-smb-linux": "smb",
        "serverless-smb-windows": "smb",
        "s3-contract": "s3",
    }
    assert set(job_types) <= set(release["needs"])
    assert set(job_types) <= {result_jobs[result] for result in mandatory}
    platforms = {"ubuntu-latest": "linux", "windows-latest": "win32"}
    cells = {
        (platforms[os], job_types[name])
        for name in job_types
        for os in jobs[name].get("strategy", {}).get("matrix", {}).get("os", [jobs[name]["runs-on"]])
    }

    context = click.Context(main)
    repo = main.get_command(context, "repo")
    commands = (repo.get_command(context, "add"), main.get_command(context, "init"))
    expected = {kind for _, kind in cells}
    for command in commands:
        repository_option = next(option for option in command.params if option.name == "repository_type")
        assert set(repository_option.type.choices) == expected
    assert PROVEN_SERVERLESS_CELLS == cells


def test_changelog_follows_the_documented_format() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_changelog import check

    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert check((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version) == []


def test_changelog_rejects_release_without_recognized_entries() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_changelog import check

    assert "0.7.2: no recognized section with entries" in check("## 0.7.2\n", "0.7.2")


def test_changelog_rejects_duplicate_release_versions() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_changelog import check

    changelog = "## 0.7.2\n\n### Bug Fixes\n\n- Fixed it\n\n## 0.7.2\n\n### Bug Fixes\n\n- Fixed it\n"
    assert "duplicate release version '0.7.2'" in check(changelog, "0.7.2")


def test_release_notes_come_from_the_newest_changelog_section() -> None:
    for path in (".github/workflows/gitea-release.yml", ".gitea/workflows/main-release.yml"):
        assert "CHANGELOG.md" in (ROOT / path).read_text(encoding="utf-8")
    for path in (".gitea/workflows/changelog.yml", ".github/workflows/changelog.yml"):
        workflow = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
        steps = workflow["jobs"]["changelog"]["steps"]
        assert any("scripts/check_changelog.py" in step.get("run", "") for step in steps)


def test_public_project_metadata_uses_gitea() -> None:
    project_url = "https://git.stockhome.com.au/stocky789/backer"

    assert all(url == project_url for url in read_pyproject()["project"]["urls"].values())
    assert f'#define MyAppURL "{project_url}"' in (ROOT / "installer" / "backer-agent.iss").read_text(encoding="utf-8")
    assert f"For more info, visit: {project_url}" in (ROOT / "scripts" / "build_agent.py").read_text(encoding="utf-8")
