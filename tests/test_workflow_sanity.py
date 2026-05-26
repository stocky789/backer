"""Sanity checks for CI, packaging, and release assumptions."""

from __future__ import annotations

import importlib
import importlib.metadata
import re
import socket
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def read_pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


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


def test_public_url_default_is_not_silent_localhost_only_guidance() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "BACKER_PUBLIC_URL" in compose_text
    assert "local repos will not work without this set" in compose_text
    assert "https://backer.example.com" in readme_text
    assert "http://192.168.1.100:8420" in readme_text


def test_public_url_fallback_is_explicit_when_address_detection_fails(monkeypatch) -> None:
    public_url = importlib.import_module("backer.server.public_url")

    class FailingSocket:
        def __enter__(self) -> FailingSocket:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def connect(self, *args: object) -> None:
            raise OSError("network unavailable")

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: FailingSocket())
    monkeypatch.setattr(socket, "gethostbyname", lambda hostname: "127.0.0.1")

    assert public_url.get_default_public_url() == "http://localhost:8420"


def test_repository_job_subfolder_replaces_unsafe_path_characters() -> None:
    repository_paths = importlib.import_module("backer.server.repository_paths")

    assert repository_paths.get_job_subfolder('Daily:VM/Backup?*') == "Daily_VM_Backup__"


def test_expected_backend_names_are_registered() -> None:
    from backer.backends.base import BackendType
    from backer.backends.registry import get_backend

    expected_backends = {
        BackendType.KOPIA,
        BackendType.PROXY,
        BackendType.RCLONE,
        BackendType.RESTIC,
        BackendType.RSYNC,
    }

    loaded_backends = {get_backend(backend_type).backend_type for backend_type in expected_backends}

    assert loaded_backends == expected_backends


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


def test_gitea_replaces_github_workflows() -> None:
    github_workflows = ROOT / ".github" / "workflows"
    gitea_workflows = ROOT / ".gitea" / "workflows"

    assert not github_workflows.exists() or not list(github_workflows.glob("*.yml"))
    assert sorted(path.name for path in gitea_workflows.glob("*.yml")) == [
        "android-build.yml",
        "docker-build.yml",
        "main-release.yml",
        "python-ci.yml",
        "release-validation.yml",
    ]


def test_branch_validation_workflows_do_not_publish_releases() -> None:
    gitea_workflows = ROOT / ".gitea" / "workflows"
    for workflow_name in ("android-build.yml", "docker-build.yml", "python-ci.yml"):
        workflow_text = (gitea_workflows / workflow_name).read_text(encoding="utf-8")
        workflow = yaml.safe_load(workflow_text)
        triggers = workflow.get("on") or workflow.get(True)

        assert triggers["push"]["branches"] == ["dev"]
        assert triggers["pull_request"]["branches"] == ["main", "dev"]
        assert "scripts/gitea_release.py" not in workflow_text
        assert "releases: write" not in workflow_text
        assert "GITEA_TOKEN" not in workflow_text


def test_main_release_workflow_publishes_only_from_main() -> None:
    workflow_text = (ROOT / ".gitea" / "workflows" / "main-release.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    triggers = workflow.get("on") or workflow.get(True)

    assert triggers["push"]["branches"] == ["main"]
    assert "pull_request" not in triggers
    assert workflow["permissions"]["code"] == "write"
    assert workflow["permissions"]["releases"] == "write"
    assert "refs/heads/main" in workflow_text
    assert "release-main" in workflow_text
    assert "actions/upload-artifact@v3" in workflow_text
    assert "actions/download-artifact@v3" in workflow_text
    assert "scripts/gitea_release.py" in workflow_text
    assert "secrets.GITEA_TOKEN" in workflow_text
    assert "runs-on: windows-latest" in workflow_text
    assert "BACKER_BUILD_WINDOWS_AGENT" not in workflow_text
    assert "WINDOWS_AGENT_PACKAGE_RESULT\" != \"success\"" in workflow_text


def test_windows_agent_build_stages_installer_tool_files() -> None:
    build_script = (ROOT / "scripts" / "build_agent.py").read_text(encoding="utf-8")

    assert 'DIST_TOOLS_DIR = DIST_DIR / "tools"' in build_script
    assert "shutil.copy(src, DIST_TOOLS_DIR / tool)" in build_script


def test_release_workflow_checks_all_release_versions_and_manual_tag_ref() -> None:
    release_workflow = (ROOT / ".gitea" / "workflows" / "release-validation.yml").read_text(encoding="utf-8")

    assert "ref: ${{ inputs.release_tag || github.ref }}" in release_workflow
    assert "installer/backer-agent.iss" in release_workflow
    assert "android/app/build.gradle.kts" in release_workflow
    assert "versionCode" in release_workflow
