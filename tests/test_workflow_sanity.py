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

import yaml

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


def test_public_url_is_configured_in_the_setup_wizard() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    setup_template = (ROOT / "src/backer/server/web/templates/setup.html").read_text(encoding="utf-8")

    assert "BACKER_PUBLIC_URL" not in compose_text
    assert "name=\"public_url\"" in setup_template
    assert "https://backer.example.com" in readme_text
    assert "http://192.168.1.100:8420" in readme_text


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

    # Builds and releases run on Gitea only; the changelog check is mirrored to
    # GitHub because the repository is also pushed there.
    assert sorted(path.name for path in github_workflows.glob("*.yml")) == ["changelog.yml"]
    assert sorted(path.name for path in gitea_workflows.glob("*.yml")) == [
        "android-build.yml",
        "changelog.yml",
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
    assert 'download_windows_tool(tool)' in build_script
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
    workflow_text = (ROOT / ".gitea" / "workflows" / "main-release.yml").read_text(encoding="utf-8")

    assert "CHANGELOG.md" in workflow_text
    for path in (".gitea/workflows/changelog.yml", ".github/workflows/changelog.yml"):
        workflow = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
        steps = workflow["jobs"]["changelog"]["steps"]
        assert any("scripts/check_changelog.py" in step.get("run", "") for step in steps)


def test_public_project_metadata_uses_gitea() -> None:
    project_url = "https://git.stockhome.com.au/stocky789/backer"

    assert all(url == project_url for url in read_pyproject()["project"]["urls"].values())
    assert f'#define MyAppURL "{project_url}"' in (ROOT / "installer" / "backer-agent.iss").read_text(encoding="utf-8")
    assert f"For more info, visit: {project_url}" in (ROOT / "scripts" / "build_agent.py").read_text(encoding="utf-8")
