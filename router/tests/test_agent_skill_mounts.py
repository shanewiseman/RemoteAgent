from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ENTRYPOINT = Path(__file__).resolve().parents[2] / "runtime" / "agent-entrypoint.sh"


@pytest.fixture
def skill_layout(tmp_path: Path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"fixture": "preserve imported authentication"}\n')
    (codex_home / "auth.json").chmod(0o600)
    shared = tmp_path / "common-skills"
    shared.mkdir()
    skill = shared / "artifact-publishing"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Shared instructions.\n")
    (shared / "README.md").write_text("Not a skill.\n")
    # The entrypoint must not modify this source tree, as the actual mount is ro.
    for path in shared.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    shared.chmod(0o555)
    environment = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "REMOTEAGENT_WORKSPACE": str(tmp_path / "workspace"),
        "REMOTEAGENT_ARTIFACTS": str(tmp_path / "artifacts"),
        "REMOTEAGENT_SESSIONS": str(tmp_path / "sessions"),
        "REMOTEAGENT_SKILLS": str(shared),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    yield codex_home, shared, environment
    for path in shared.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    shared.chmod(0o755)


def run_entrypoint(environment: dict[str, str], command: str = "true"):
    return subprocess.run(
        [str(ENTRYPOINT), "/bin/sh", "-c", command],
        env=environment, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("legacy_link", [False, True])
def test_system_skills_writable_and_common_skills_read_only(skill_layout, legacy_link):
    codex_home, shared, environment = skill_layout
    home = codex_home / "skills"
    authentication = codex_home / "auth.json"
    original_authentication = authentication.read_bytes()
    original_authentication_stat = authentication.stat()
    if legacy_link:
        home.symlink_to(shared, target_is_directory=True)
    result = run_entrypoint(
        environment,
        'mkdir -p "$CODEX_HOME/skills/.system/probe"; '
        'printf bundled > "$CODEX_HOME/skills/.system/probe/SKILL.md"',
    )
    assert result.returncode == 0, result.stderr
    assert home.is_dir() and not home.is_symlink()
    assert (home / ".system/probe/SKILL.md").read_text() == "bundled"
    common = home / "artifact-publishing"
    assert common.is_symlink() and common.readlink() == shared / "artifact-publishing"
    assert (common / "SKILL.md").read_text() == "Shared instructions.\n"
    assert not (home / "README.md").exists()
    assert not (shared / ".system").exists()
    assert shared.stat().st_mode & 0o777 == 0o555
    assert run_entrypoint(environment).returncode == 0
    assert (home / ".system/probe/SKILL.md").read_text() == "bundled"
    assert authentication.read_bytes() == original_authentication
    current_authentication_stat = authentication.stat()
    for attribute in ("st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ino"):
        assert getattr(current_authentication_stat, attribute) == getattr(
            original_authentication_stat, attribute
        )


def test_reconcile_removes_only_stale_managed_links(skill_layout, tmp_path: Path):
    codex_home, shared, environment = skill_layout
    assert run_entrypoint(environment).returncode == 0
    home = codex_home / "skills"
    custom = home / "custom"
    custom.mkdir()
    (custom / "SKILL.md").write_text("Custom.\n")
    unrelated = home / "external"
    unrelated.symlink_to(tmp_path / "missing-external", target_is_directory=True)
    system = home / ".system"
    system.mkdir()
    (system / "marker").write_text("Retain.\n")
    shared.chmod(0o755)
    (shared / "artifact-publishing").chmod(0o755)
    shutil.rmtree(shared / "artifact-publishing")
    shared.chmod(0o555)
    result = run_entrypoint(environment)
    assert result.returncode == 0, result.stderr
    assert not (home / "artifact-publishing").is_symlink()
    assert (custom / "SKILL.md").read_text() == "Custom.\n"
    assert unrelated.is_symlink()
    assert (system / "marker").read_text() == "Retain.\n"


@pytest.mark.parametrize("collision", ["directory", "file", "dangling_link"])
def test_common_skill_collision_fails_without_overwrite(skill_layout, tmp_path: Path, collision):
    codex_home, _, environment = skill_layout
    home = codex_home / "skills"
    home.mkdir()
    destination = home / "artifact-publishing"
    if collision == "directory":
        destination.mkdir()
        (destination / "marker").write_text("Retain.\n")
    elif collision == "file":
        destination.write_text("Retain.\n")
    else:
        destination.symlink_to(tmp_path / "missing", target_is_directory=True)
    result = run_entrypoint(environment)
    assert result.returncode == 73
    assert "collides with existing" in result.stderr
    if collision == "directory":
        assert (destination / "marker").read_text() == "Retain.\n"
    elif collision == "file":
        assert destination.read_text() == "Retain.\n"
    else:
        assert destination.readlink() == tmp_path / "missing"


def test_unrelated_skills_root_symlink_is_preserved(skill_layout, tmp_path: Path):
    codex_home, _, environment = skill_layout
    home = codex_home / "skills"
    target = tmp_path / "unrelated"
    home.symlink_to(target, target_is_directory=True)
    result = run_entrypoint(environment)
    assert result.returncode == 73
    assert "unrelated CODEX_HOME/skills symlink" in result.stderr
    assert home.readlink() == target


def test_legacy_link_to_absent_shared_tree_can_be_migrated(skill_layout, tmp_path: Path):
    codex_home, _, environment = skill_layout
    missing = tmp_path / "absent-common-skills"
    environment["REMOTEAGENT_SKILLS"] = str(missing)
    home = codex_home / "skills"
    home.symlink_to(missing, target_is_directory=True)
    result = run_entrypoint(environment)
    assert result.returncode == 0, result.stderr
    assert home.is_dir() and not home.is_symlink()
    assert not missing.exists()
