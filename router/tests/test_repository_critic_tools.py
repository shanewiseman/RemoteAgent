from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CRITIC_TOOL = REPOSITORY_ROOT / "repository-critic" / "tools" / "repository_critic.py"
TOOLCHAIN_MANIFEST = REPOSITORY_ROOT / "repository-critic" / "toolchain-manifest.json"
HASH = "a" * 64
ARTIFACT_NAMES = {
    "coverage-details.tar.gz",
    "coverage-summary.json",
    "repository-review.json",
    "repository-review.md",
    "run-manifest.json",
    "test-run.log",
}
ENFORCED_LIMITS = {
    "max_project_roots": 6,
    "restore_timeout_seconds": 600,
    "test_timeout_seconds": 1_200,
    "total_dynamic_seconds": 2_700,
    "scratch_limit_bytes": 2 * 1024 * 1024 * 1024,
    "log_limit_bytes": 5 * 1024 * 1024,
    "native_coverage_limit_bytes": 25 * 1024 * 1024,
    "max_artifacts": 6,
}


def _invoke(
    *arguments: str | Path,
    environment: dict[str, str] | None = None,
    timeout: float = 20,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    child_environment = os.environ.copy()
    if environment:
        child_environment.update(environment)
    process = subprocess.run(
        [sys.executable, str(CRITIC_TOOL), *(str(item) for item in arguments)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=child_environment,
    )
    stream = process.stdout if process.stdout.strip() else process.stderr
    lines = [line for line in stream.splitlines() if line.strip()]
    assert lines, (
        f"critic helper returned no JSON: stdout={process.stdout!r} stderr={process.stderr!r}"
    )
    try:
        document = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"critic helper returned non-JSON: stdout={process.stdout!r} stderr={process.stderr!r}"
        ) from exc
    assert isinstance(document, dict)
    return process, document


def _write_files(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _prepare_workspace(
    tmp_path: Path, *, job_id: str = "job-1"
) -> tuple[Path, Path, dict[str, Any]]:
    workspace = tmp_path / "workspace"
    source = workspace / "companions" / "repository"
    artifacts = workspace / "artifacts"
    _write_files(source, {"source.txt": "source\n", "nested/target.txt": "target\n"})
    artifacts.mkdir(parents=True)
    record_path = workspace / ".repository-critic" / job_id / "prepare.json"
    process, record = _invoke(
        "prepare",
        "--workspace",
        workspace,
        "--job-id",
        job_id,
        "--artifacts",
        artifacts,
        "--output",
        record_path,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(record_path.read_text(encoding="utf-8")) == record
    return workspace, record_path, record


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _valid_finalizer_drafts(
    record: dict[str, Any], *, secret: str = "finalizer-secret-value"
) -> dict[str, Path]:
    scratch = Path(record["scratch_root"])
    drafts = scratch / "drafts"
    coverage_dir = scratch / "coverage" / "project"
    coverage_dir.mkdir(parents=True)
    (coverage_dir / "native.json").write_text("{}\n", encoding="utf-8")

    report_path = drafts / "repository-review.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        f"# Repository review\n\nAuthorization: Bearer {secret}\n", encoding="utf-8"
    )
    review_path = drafts / "repository-review.json"
    _write_json(
        review_path,
        {
            "schema_version": 1,
            "review_mode": "repository_snapshot",
            "status": "complete",
            "repository": {
                "companion_name": record["companion_name"],
                "source_path": record["source_path"],
                "scratch_path": record["repository_path"],
                "snapshot_sha256": record["source_snapshot_sha256"],
                "git_commit": record["git_commit"],
                "git_dirty": record["git_dirty"],
            },
            "summary": {
                "verdict": f"password={secret}",
                "p0_count": 0,
                "p1_count": 0,
                "p2_count": 0,
                "p3_count": 0,
            },
            "traceability": [
                {
                    "claim_id": "fixture-claim",
                    "claim": "The repository contains source.txt.",
                    "authority": "fixture contract",
                    "status": "verified",
                    "documentation_evidence": ["source.txt"],
                    "implementation_evidence": ["source.txt"],
                    "test_evidence": [],
                }
            ],
            "findings": [],
            "dynamic_analysis": {
                "status": "no_tests",
                "project_count": 0,
                "test_run_count": 0,
                "coverage_run_count": 0,
            },
            "limitations": [],
        },
    )
    coverage_path = drafts / "coverage-summary.json"
    _write_json(
        coverage_path,
        {
            "schema_version": 1,
            "review_mode": "repository_snapshot",
            "status": "no_tests",
            "projects": [],
            "high_priority_gaps": [f"token={secret}"],
        },
    )
    manifest_path = drafts / "run-manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "review_mode": "repository_snapshot",
            "job_id": record["job_id"],
            "started_at": record["prepared_at"],
            "finished_at": None,
            "total_duration_seconds": 0,
            "repository": {
                "companion_name": record["companion_name"],
                "source_path": record["source_path"],
                "scratch_path": record["repository_path"],
                "snapshot_sha256": record["source_snapshot_sha256"],
                "git_commit": record["git_commit"],
                "git_dirty": record["git_dirty"],
            },
            "policy": {
                "network": "managed public-host egress; HTTPS is the agent convention",
                "build_hooks": False,
                "locked_restore_required": True,
            },
            "toolchains": json.loads(TOOLCHAIN_MANIFEST.read_text(encoding="utf-8")),
            "commands": [],
            "dependency_restores": [],
            "limits": ENFORCED_LIMITS,
            "cleanup": {"status": "pending"},
            "limitations": [f"api_key={secret}"],
        },
    )
    log_path = drafts / "command.log"
    log_path.write_text(f"Authorization: Bearer {secret}\n", encoding="utf-8")
    return {
        "report": report_path,
        "review": review_path,
        "coverage": coverage_path,
        "manifest": manifest_path,
        "log": log_path,
        "coverage_dir": coverage_dir,
    }


def _finalize(
    workspace: Path,
    record_path: Path,
    record: dict[str, Any],
    drafts: dict[str, Path],
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    return _invoke(
        "finalize",
        "--workspace",
        workspace,
        "--artifacts",
        workspace / "artifacts",
        "--job-id",
        record["job_id"],
        "--prepare-record",
        record_path,
        "--report",
        drafts["report"],
        "--review-json",
        drafts["review"],
        "--coverage-json",
        drafts["coverage"],
        "--run-manifest",
        drafts["manifest"],
        "--log",
        drafts["log"],
        "--coverage-dir",
        drafts["coverage_dir"],
    )


def _restore_plan(
    root: Path,
    ecosystem: str,
    *,
    unlocked: bool = False,
    hooks: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    arguments: list[str | Path] = [
        "restore-plan",
        "--root",
        root,
        "--ecosystem",
        ecosystem,
    ]
    if unlocked:
        arguments.append("--allow-unlocked")
    if hooks:
        arguments.append("--allow-build-hooks")
    return _invoke(*arguments)


def _run_command(
    scratch: Path,
    code: str,
    *,
    identifier: str = "test-command",
    phase: str = "test",
    hooks: bool = False,
    restore_mode: str = "locked",
    timeout_seconds: int | None = None,
    disk_limit: int | None = None,
    log_limit: int = 4096,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], Path]:
    log = scratch / f"{identifier}.log"
    output = scratch / f"{identifier}.json"
    arguments: list[str | Path] = [
        "run",
        "--id",
        identifier,
        "--scratch-root",
        scratch,
        "--cwd",
        scratch,
        "--phase",
        phase,
        "--ecosystem",
        "other",
        "--restore-mode",
        restore_mode,
        "--log-limit",
        str(log_limit),
        "--log",
        log,
        "--output",
        output,
    ]
    if hooks:
        arguments.append("--allow-build-hooks")
    if timeout_seconds is not None:
        arguments.extend(("--timeout", str(timeout_seconds)))
    if disk_limit is not None:
        arguments.extend(("--disk-limit", str(disk_limit)))
    arguments.extend(("--", sys.executable, "-c", code))
    process, record = _invoke(*arguments, environment=environment, timeout=15)
    return process, record, log


def _normalize(
    scratch: Path,
    root: Path,
    native: Path,
    coverage_format: str,
    ecosystem: str,
    *,
    status: str = "complete",
    tests: dict[str, int | None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], Path]:
    output = scratch / f"{coverage_format}-normalized.json"
    arguments: list[str | Path] = [
        "normalize",
        "--format",
        coverage_format,
        "--input",
        native,
        "--root",
        root,
        "--scratch-root",
        scratch,
        "--project-id",
        f"{coverage_format}-project",
        "--project-root",
        ".",
        "--ecosystem",
        ecosystem,
        "--status",
        status,
        "--native-artifact",
        (
            native.relative_to(scratch).as_posix()
            if native.is_relative_to(scratch)
            else "coverage/external-native-evidence"
        ),
        "--output",
        output,
    ]
    if tests is not None:
        arguments.extend(("--tests", json.dumps(tests, sort_keys=True)))
    process, document = _invoke(*arguments)
    return process, document, output


def test_prepare_selects_repository_and_keeps_relative_symlink_writes_in_scratch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "companions" / "repository"
    _write_files(source, {"nested/target.txt": "original\n"})
    (source / "link.txt").symlink_to("nested/target.txt")
    _write_files(workspace / "companions" / "secondary", {"other.txt": "other\n"})
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)

    process, record = _invoke(
        "prepare",
        "--workspace",
        workspace,
        "--job-id",
        "symlink-job",
        "--artifacts",
        artifacts,
        "--output",
        workspace / ".repository-critic" / "symlink-job" / "prepare.json",
    )

    assert process.returncode == 0, process.stderr
    assert record["companion_name"] == "repository"
    copied_link = Path(record["repository_path"]) / "link.txt"
    assert copied_link.is_symlink()
    copied_link.write_text("scratch-only\n", encoding="utf-8")
    assert (source / "nested" / "target.txt").read_text(encoding="utf-8") == "original\n"


@pytest.mark.parametrize("target_kind", ["absolute", "escape"])
def test_prepare_rejects_symlinks_that_could_reach_outside_the_copy(
    tmp_path: Path, target_kind: str
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "companions" / "repository"
    _write_files(source, {"target.txt": "source\n"})
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)
    target = (
        str((source / "target.txt").resolve()) if target_kind == "absolute" else "../../outside"
    )
    (source / "unsafe-link").symlink_to(target)

    process, error = _invoke(
        "prepare",
        "--workspace",
        workspace,
        "--job-id",
        "unsafe-job",
        "--artifacts",
        artifacts,
        "--output",
        workspace / ".repository-critic" / "unsafe-job" / "prepare.json",
    )

    assert process.returncode == 2
    assert error["status"] == "blocked"
    assert "symlink" in error["message"]
    assert not (workspace / ".repository-critic" / "unsafe-job").exists()


def test_prepare_requires_unambiguous_companion_selection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_files(workspace / "companions" / "alpha", {"a": "a"})
    _write_files(workspace / "companions" / "beta", {"b": "b"})
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)

    process, error = _invoke(
        "prepare",
        "--workspace",
        workspace,
        "--job-id",
        "ambiguous-job",
        "--artifacts",
        artifacts,
        "--output",
        workspace / ".repository-critic" / "ambiguous-job" / "prepare.json",
    )

    assert process.returncode == 2
    assert error["kind"] == "blocked_companion_selection"
    assert "alpha, beta" in error["message"]
    artifact_root = Path(error["artifact_root"])
    assert {path.name for path in artifact_root.iterdir()} == ARTIFACT_NAMES
    review = json.loads((artifact_root / "repository-review.json").read_text(encoding="utf-8"))
    coverage = json.loads((artifact_root / "coverage-summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((artifact_root / "run-manifest.json").read_text(encoding="utf-8"))
    assert review["status"] == "blocked"
    assert review["repository"]["snapshot_sha256"] is None
    assert review["dynamic_analysis"]["status"] == "blocked_companion_selection"
    assert coverage["status"] == "no_tests"
    assert manifest["repository"] == review["repository"]
    assert manifest["limits"] == ENFORCED_LIMITS
    assert manifest["cleanup"]["scratch_removed"] is True
    assert not (workspace / ".repository-critic" / "ambiguous-job").exists()


def test_python_restore_plans_distinguish_hashed_lock_and_unlocked_resolution(
    tmp_path: Path,
) -> None:
    locked = tmp_path / "locked"
    _write_files(
        locked,
        {"requirements.txt": (f"six==1.17.0 \\\n    --hash=sha256:{HASH}\n")},
    )
    locked_process, locked_plan = _restore_plan(locked, "python")
    assert locked_process.returncode == 0, locked_process.stderr
    assert locked_plan["mode"] == "locked"
    assert locked_plan["manager"] == "pip"
    assert locked_plan["lockfile"] == "requirements.txt"
    assert locked_plan["commands"][0] == ["python3.12", "-m", "venv", ".venv"]
    assert "--require-hashes" in locked_plan["commands"][1]
    assert "--only-binary=:all:" in locked_plan["commands"][1]

    unlocked = tmp_path / "unlocked"
    _write_files(unlocked, {"requirements.txt": "six\n"})
    blocked_process, blocked = _restore_plan(unlocked, "python")
    assert blocked_process.returncode == 2
    assert blocked["kind"] == "blocked_dependency_restore"

    unlocked_process, unlocked_plan = _restore_plan(unlocked, "python", unlocked=True)
    assert unlocked_process.returncode == 0, unlocked_process.stderr
    assert unlocked_plan["mode"] == "resolved_unlocked"
    assert "generated in scratch" in unlocked_plan["lockfile"]
    assert unlocked_plan["commands"][0][:3] == ["uv", "pip", "compile"]
    assert "--generate-hashes" in unlocked_plan["commands"][0]
    assert unlocked_plan["commands"][-1][:3] == ["uv", "pip", "freeze"]


@pytest.mark.parametrize(
    ("manager", "manifest_name", "manifest", "lock_name", "locked_commands", "unlocked_commands"),
    [
        (
            "uv",
            "pyproject.toml",
            "[project]\nname = 'fixture'\nversion = '1.0.0'\n",
            "uv.lock",
            [["uv", "sync", "--locked", "--no-install-project", "--no-build"]],
            [
                ["uv", "lock", "--no-build"],
                ["uv", "sync", "--locked", "--no-install-project", "--no-build"],
                ["uv", "pip", "freeze"],
            ],
        ),
        (
            "poetry",
            "pyproject.toml",
            "[tool.poetry]\nname = 'fixture'\nversion = '1.0.0'\n",
            "poetry.lock",
            [["poetry", "install", "--no-root", "--no-interaction", "--no-ansi"]],
            [
                ["poetry", "lock"],
                ["poetry", "install", "--no-root", "--no-interaction", "--no-ansi"],
                ["poetry", "show", "--tree"],
            ],
        ),
        (
            "pipenv",
            "Pipfile",
            "[packages]\nsix = '==1.17.0'\n",
            "Pipfile.lock",
            [["pipenv", "sync", "--dev"], ["pipenv", "requirements", "--dev"]],
            [
                ["pipenv", "lock"],
                ["pipenv", "sync", "--dev"],
                ["pipenv", "requirements", "--dev"],
            ],
        ),
    ],
)
def test_python_manager_restore_plan_matrix_is_exact_for_locked_and_unlocked(
    tmp_path: Path,
    manager: str,
    manifest_name: str,
    manifest: str,
    lock_name: str,
    locked_commands: list[list[str]],
    unlocked_commands: list[list[str]],
) -> None:
    locked = tmp_path / f"{manager}-locked"
    _write_files(locked, {manifest_name: manifest, lock_name: "{}\n"})

    locked_process, locked_plan = _restore_plan(locked, "python")

    assert locked_process.returncode == 0, locked_process.stderr
    assert locked_plan["manager"] == manager
    assert locked_plan["mode"] == "locked"
    assert locked_plan["lockfile"] == lock_name
    assert locked_plan["commands"] == locked_commands
    assert locked_plan["build_hooks_enabled"] is False

    unlocked = tmp_path / f"{manager}-unlocked"
    _write_files(unlocked, {manifest_name: manifest})

    unlocked_process, unlocked_plan = _restore_plan(unlocked, "python", unlocked=True)

    assert unlocked_process.returncode == 0, unlocked_process.stderr
    assert unlocked_plan["manager"] == manager
    assert unlocked_plan["mode"] == "resolved_unlocked"
    assert unlocked_plan["lockfile"] == f"{lock_name} (generated in scratch)"
    assert unlocked_plan["commands"] == unlocked_commands
    assert unlocked_plan["build_hooks_enabled"] is False


@pytest.mark.parametrize(
    ("manager", "version", "lock_name", "locked_prefix", "unlocked_prefix"),
    [
        ("npm", "10.9.3", "package-lock.json", ["npm", "ci"], ["npm", "install"]),
        (
            "pnpm",
            "9.15.9",
            "pnpm-lock.yaml",
            ["pnpm", "install", "--frozen-lockfile"],
            ["pnpm", "install", "--lockfile-only"],
        ),
        (
            "pnpm",
            "10.34.5",
            "pnpm-lock.yaml",
            ["pnpm", "install", "--frozen-lockfile"],
            ["pnpm", "install", "--lockfile-only"],
        ),
        (
            "pnpm",
            "11.25.0",
            "pnpm-lock.yaml",
            ["pnpm", "install", "--frozen-lockfile"],
            ["pnpm", "install", "--lockfile-only"],
        ),
        (
            "yarn",
            "1.22.22",
            "yarn.lock",
            ["yarn", "install", "--frozen-lockfile"],
            ["yarn", "install"],
        ),
        (
            "yarn",
            "4.18.0",
            "yarn.lock",
            ["yarn", "install", "--immutable"],
            ["yarn", "install", "--mode=skip-build"],
        ),
    ],
)
def test_javascript_restore_plan_matrix_is_frozen_and_suppresses_hooks(
    tmp_path: Path,
    manager: str,
    version: str,
    lock_name: str,
    locked_prefix: list[str],
    unlocked_prefix: list[str],
) -> None:
    package = json.dumps(
        {"name": "fixture", "version": "1.0.0", "packageManager": f"{manager}@{version}"}
    )
    locked = tmp_path / f"{manager}-{version}-locked"
    _write_files(locked, {"package.json": package, lock_name: "{}\n"})
    locked_process, locked_plan = _restore_plan(locked, "javascript-typescript")
    assert locked_process.returncode == 0, locked_process.stderr
    assert (locked_plan["manager"], locked_plan["manager_version"]) == (manager, version)
    assert locked_plan["mode"] == "locked"
    assert locked_plan["commands"][0][: len(locked_prefix)] == locked_prefix
    if manager == "yarn" and version != "1.22.22":
        assert "--mode=skip-build" in locked_plan["commands"][0]
    else:
        assert "--ignore-scripts" in locked_plan["commands"][0]
    assert locked_plan["environment"] == {
        "NPM_CONFIG_IGNORE_SCRIPTS": "true",
        "YARN_ENABLE_SCRIPTS": "false",
    }

    unlocked = tmp_path / f"{manager}-{version}-unlocked"
    _write_files(unlocked, {"package.json": package})
    unlocked_process, unlocked_plan = _restore_plan(
        unlocked, "javascript-typescript", unlocked=True
    )
    assert unlocked_process.returncode == 0, unlocked_process.stderr
    assert unlocked_plan["mode"] == "resolved_unlocked"
    assert "generated in scratch" in unlocked_plan["lockfile"]
    assert unlocked_plan["commands"][0][: len(unlocked_prefix)] == unlocked_prefix
    if manager == "yarn":
        assert unlocked_plan["commands"][-1] == (
            ["yarn", "list", "--json"]
            if version == "1.22.22"
            else ["yarn", "info", "-A", "-R", "--json"]
        )


def test_javascript_restore_hooks_require_explicit_plan_flag(tmp_path: Path) -> None:
    root = tmp_path / "npm-hooks"
    _write_files(root, {"package.json": "{}", "package-lock.json": "{}"})

    process, plan = _restore_plan(root, "javascript-typescript", hooks=True)

    assert process.returncode == 0, process.stderr
    assert plan["build_hooks_enabled"] is True
    assert "--ignore-scripts" not in plan["commands"][0]
    assert plan["environment"]["NPM_CONFIG_IGNORE_SCRIPTS"] == "false"
    assert plan["environment"]["YARN_ENABLE_SCRIPTS"] == "true"


@pytest.mark.parametrize("package_manager", ["pnpm@8.15.9", "yarn@3.8.7"])
def test_javascript_restore_rejects_package_managers_not_baked_into_image(
    tmp_path: Path, package_manager: str
) -> None:
    root = tmp_path / package_manager.replace("@", "-")
    _write_files(
        root,
        {
            "package.json": json.dumps(
                {"name": "fixture", "version": "1.0.0", "packageManager": package_manager}
            ),
            "pnpm-lock.yaml" if package_manager.startswith("pnpm") else "yarn.lock": "{}\n",
        },
    )

    process, error = _restore_plan(root, "javascript-typescript")

    assert process.returncode == 2
    assert error["kind"] == "unsupported_toolchain"
    assert "not preloaded" in error["message"]


def test_go_restore_plans_distinguish_sum_vendor_and_unlocked_resolution(tmp_path: Path) -> None:
    module = "module example.test/fixture\n\ngo 1.27\n"
    summed = tmp_path / "summed"
    _write_files(summed, {"go.mod": module, "go.sum": ""})
    summed_process, summed_plan = _restore_plan(summed, "go")
    assert summed_process.returncode == 0, summed_process.stderr
    assert summed_plan["mode"] == "locked"
    assert summed_plan["commands"] == [
        ["go", "mod", "download"],
        ["go", "mod", "verify"],
    ]

    vendored = tmp_path / "vendored"
    _write_files(vendored, {"go.mod": module, "vendor/modules.txt": ""})
    vendor_process, vendor_plan = _restore_plan(vendored, "go")
    assert vendor_process.returncode == 0, vendor_process.stderr
    assert vendor_plan["mode"] == "locked"
    assert vendor_plan["commands"] == [["go", "list", "-mod=vendor", "-m", "all"]]

    unlocked = tmp_path / "unlocked"
    _write_files(unlocked, {"go.mod": module})
    blocked_process, blocked = _restore_plan(unlocked, "go")
    assert blocked_process.returncode == 2
    assert blocked["kind"] == "blocked_dependency_restore"
    unlocked_process, unlocked_plan = _restore_plan(unlocked, "go", unlocked=True)
    assert unlocked_process.returncode == 0, unlocked_process.stderr
    assert unlocked_plan["mode"] == "resolved_unlocked"
    assert unlocked_plan["lockfile"] == "go.sum (generated in scratch)"
    assert unlocked_plan["commands"][-1] == ["go", "list", "-m", "-json", "all"]


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("requirements.txt", "-r child/../../../outside.txt\n"),
        ("requirements.txt", "--find-links file:///etc\nsix\n"),
        (
            "pyproject.toml",
            "[[tool.poetry.source]]\nname = 'private'\nurl = 'http://packages.example/simple'\n",
        ),
        ("Pipfile", "[[source]]\nurl = 'http://packages.example/simple'\nverify_ssl = false\n"),
        (
            "package-lock.json",
            '{"packages":{"node_modules/x":{"resolved":"http://packages.example/x.tgz"}}}',
        ),
        ("package.json", '{"dependencies":{"x":"git+ssh://example.com/x.git"}}'),
    ],
)
def test_dependency_validation_rejects_unsafe_declared_and_locked_sources(
    tmp_path: Path, relative: str, content: str
) -> None:
    root = tmp_path / "project"
    _write_files(root, {relative: content})

    process, result = _invoke("validate-dependencies", "--root", root)

    assert process.returncode == 2
    assert result["status"] == "blocked"
    assert result["issues"]


@pytest.mark.parametrize("hooks", [False, True])
def test_run_uses_sanitized_deterministic_environment_and_hook_policy(
    tmp_path: Path, hooks: bool
) -> None:
    scratch = tmp_path / f"scratch-{hooks}"
    scratch.mkdir()
    names = [
        "TEST_API_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "PIP_INDEX_URL",
        "GOPROXY",
        "NPM_CONFIG_REGISTRY",
        "NPM_CONFIG_IGNORE_SCRIPTS",
        "YARN_ENABLE_SCRIPTS",
        "PIP_ONLY_BINARY",
        "GOFLAGS",
    ]
    code = f"import json, os; print(json.dumps({{name: os.getenv(name) for name in {names!r}}}, sort_keys=True))"
    process, record, log_path = _run_command(
        scratch,
        code,
        hooks=hooks,
        environment={
            "TEST_API_TOKEN": "must-not-reach-child",
            "HTTP_PROXY": "http://proxy.invalid",
            "HTTPS_PROXY": "https://proxy.invalid",
        },
    )

    assert process.returncode == 0, process.stderr
    assert record["status"] == "success"
    assert record["build_hooks_enabled"] is hooks
    log = log_path.read_text(encoding="utf-8")
    assert '"TEST_API_TOKEN": null' in log
    assert '"HTTP_PROXY": null' in log
    assert '"HTTPS_PROXY": null' in log
    assert '"NO_PROXY": "*"' in log
    assert '"PIP_INDEX_URL": "https://pypi.org/simple"' in log
    assert '"GOPROXY": "https://proxy.golang.org"' in log
    assert '"NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/"' in log
    assert f'"NPM_CONFIG_IGNORE_SCRIPTS": "{str(not hooks).lower()}"' in log
    assert f'"YARN_ENABLE_SCRIPTS": "{str(hooks).lower()}"' in log
    if hooks:
        assert '"PIP_ONLY_BINARY": ""' in log
    else:
        assert '"PIP_ONLY_BINARY": ":all:"' in log
    assert '"GOFLAGS": "-mod=readonly"' in log


def test_run_redacts_logs_and_argv_and_bounds_output(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    secret = "super-secret-value"
    code = (
        "import sys; "
        f"print('Authorization: Bearer {secret}'); "
        f"print('password={secret}', file=sys.stderr); "
        "print('x' * 10000)"
    )
    process, record, log_path = _run_command(
        scratch,
        code,
        identifier="redaction",
        log_limit=512,
    )

    assert process.returncode == 0, process.stderr
    log = log_path.read_text(encoding="utf-8")
    assert secret not in log
    assert "[REDACTED]" in log
    assert "[LOG TRUNCATED]" in log
    assert record["stdout_truncated"] is True
    assert record["stderr_truncated"] is True


def test_run_enforces_timeout_disk_and_total_budget(tmp_path: Path) -> None:
    timeout_scratch = tmp_path / "timeout"
    timeout_scratch.mkdir()
    process, record, _log = _run_command(
        timeout_scratch,
        "import time; time.sleep(5)",
        identifier="timeout",
        timeout_seconds=1,
    )
    assert process.returncode == 124
    assert record["status"] == "timed_out"
    assert record["signal"] in {"SIGTERM", "SIGKILL"}

    disk_scratch = tmp_path / "disk"
    disk_scratch.mkdir()
    (disk_scratch / "large").write_bytes(b"xx")
    disk_process, disk_error, _log = _run_command(
        disk_scratch,
        "pass",
        identifier="disk",
        disk_limit=1,
    )
    assert disk_process.returncode == 2
    assert disk_error["kind"] == "resource_limited"

    budget_scratch = tmp_path / "budget"
    budget_scratch.mkdir()
    ledger = budget_scratch / ".critic-budget.json"
    ledger.write_text(
        json.dumps({"schema_version": 1, "spent_seconds": 1.0, "runs": []}),
        encoding="utf-8",
    )
    budget_process, budget = _invoke(
        "run",
        "--id",
        "budget",
        "--scratch-root",
        budget_scratch,
        "--cwd",
        budget_scratch,
        "--phase",
        "test",
        "--ecosystem",
        "other",
        "--total-budget",
        "1",
        "--",
        sys.executable,
        "-c",
        "pass",
    )
    assert budget_process.returncode == 75
    assert budget["status"] == "resource_limited"
    assert "budget exhausted" in budget["limit_reason"]


def test_run_terminates_background_descendants_after_launcher_success(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    code = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(child.pid)"
    )
    process, record, log_path = _run_command(scratch, code, identifier="descendant")
    assert process.returncode == 1, process.stderr
    assert record["status"] == "failed"
    child_pid = int(log_path.read_text(encoding="utf-8").split("## stdout\n", 1)[1].splitlines()[0])
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(child_pid, signal.SIGKILL)
        pytest.fail("run left a background descendant alive after the launcher exited")
    assert record["signal"] in {"SIGTERM_DESCENDANTS", "SIGKILL_DESCENDANTS"}


@pytest.mark.parametrize(
    ("coverage_format", "ecosystem", "content", "expected_lines", "expected_zero"),
    [
        (
            "coveragepy",
            "python",
            json.dumps(
                {
                    "totals": {
                        "covered_lines": 2,
                        "num_statements": 4,
                        "covered_branches": 1,
                        "num_branches": 2,
                    },
                    "files": {
                        "src/covered.py": {"summary": {"covered_lines": 2, "num_statements": 2}},
                        "src/zero.py": {"summary": {"covered_lines": 0, "num_statements": 2}},
                    },
                }
            ),
            {"covered": 2, "total": 4, "percent": 50.0},
            ["src/zero.py"],
        ),
        (
            "istanbul",
            "javascript-typescript",
            json.dumps(
                {
                    "src/a.js": {
                        "statementMap": {
                            "0": {"start": {"line": 1}},
                            "1": {"start": {"line": 2}},
                        },
                        "s": {"0": 1, "1": 0},
                        "f": {"0": 1, "1": 0},
                        "b": {"0": [1, 0]},
                    }
                }
            ),
            {"covered": 1, "total": 2, "percent": 50.0},
            [],
        ),
        (
            "lcov",
            "javascript-typescript",
            "SF:src/a.js\nFNF:2\nFNH:1\nBRF:2\nBRH:1\nLF:2\nLH:1\nend_of_record\n",
            {"covered": 1, "total": 2, "percent": 50.0},
            [],
        ),
        (
            "go",
            "go",
            "mode: atomic\nexample.test/fixture/a.go:1.1,2.1 2 1\n"
            "example.test/fixture/zero.go:1.1,2.1 2 0\n",
            {"covered": 2, "total": 4, "percent": 50.0},
            ["example.test/fixture/zero.go"],
        ),
    ],
)
def test_normalize_golden_formats_and_zero_coverage_files(
    tmp_path: Path,
    coverage_format: str,
    ecosystem: str,
    content: str,
    expected_lines: dict[str, int | float],
    expected_zero: list[str],
) -> None:
    scratch = tmp_path / "scratch"
    root = scratch / "repository"
    coverage = scratch / "coverage"
    root.mkdir(parents=True)
    coverage.mkdir()
    native = coverage / f"native.{coverage_format}"
    native.write_text(content, encoding="utf-8")
    tests = {"passed": 2, "failed": 0, "skipped": 1, "total": 3}

    process, project, output = _normalize(
        scratch, root, native, coverage_format, ecosystem, tests=tests
    )

    assert process.returncode == 0, process.stderr
    assert project["status"] == "complete"
    assert project["tests"] == tests
    assert project["lines"] == expected_lines
    assert project["zero_coverage_files"] == expected_zero
    if ecosystem == "go":
        assert project["branches"] is None
        assert project["functions"] is None
    assert json.loads(output.read_text(encoding="utf-8")) == project


def test_normalize_rejects_malformed_or_external_evidence(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    root = scratch / "repository"
    root.mkdir(parents=True)
    malformed = scratch / "malformed.json"
    malformed.write_text("not-json", encoding="utf-8")

    malformed_process, malformed_error, _output = _normalize(
        scratch, root, malformed, "coveragepy", "python"
    )
    assert malformed_process.returncode == 2
    assert malformed_error["kind"] == "invalid_input"

    external = tmp_path / "external.json"
    external.write_text("{}", encoding="utf-8")
    external_process, external_error, _output = _normalize(
        scratch, root, external, "coveragepy", "python"
    )
    assert external_process.returncode == 2
    assert "escapes" in external_error["message"]


@pytest.mark.parametrize(
    "document",
    [
        {"totals": {"covered_lines": "not-an-integer", "num_statements": 1}, "files": {}},
        {"totals": {"covered_lines": 3, "num_statements": 2}, "files": {}},
    ],
)
def test_normalize_rejects_malformed_or_impossible_coverage_counts(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    scratch = tmp_path / "scratch"
    root = scratch / "repository"
    root.mkdir(parents=True)
    native = scratch / "coverage.json"
    native.write_text(json.dumps(document), encoding="utf-8")

    process, error, _output = _normalize(scratch, root, native, "coveragepy", "python")

    assert process.returncode == 2
    assert error["kind"] in {"invalid_data", "invalid_input"}
    assert error["message"]


def test_normalize_no_tests_preserves_unknown_counts_without_inventing_zero_percent(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    root = scratch / "repository"
    root.mkdir(parents=True)
    native = scratch / "coverage.json"
    native.write_text(
        json.dumps({"totals": {"covered_lines": 0, "num_statements": 0}, "files": {}}),
        encoding="utf-8",
    )

    process, project, _output = _normalize(
        scratch, root, native, "coveragepy", "python", status="no_tests"
    )

    assert process.returncode == 0, process.stderr
    assert project["status"] == "no_tests"
    assert project["tests"] == {
        "passed": None,
        "failed": None,
        "skipped": None,
        "total": None,
    }
    assert project["lines"] == {"covered": 0, "total": 0, "percent": None}
    assert project["zero_coverage_files"] == []


def test_aggregate_preserves_partial_status_and_defaults_empty_to_no_tests(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    root = scratch / "repository"
    root.mkdir(parents=True)
    native = scratch / "coverage.json"
    native.write_text(
        json.dumps(
            {
                "totals": {"covered_lines": 1, "num_statements": 2},
                "files": {
                    "review_target/access.py": {
                        "summary": {"covered_lines": 1, "num_statements": 2}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    normalize_process, project, project_path = _normalize(
        scratch,
        root,
        native,
        "coveragepy",
        "python",
        status="tests_failed_partial",
        tests={"passed": 1, "failed": 1, "skipped": 0, "total": 2},
    )
    assert normalize_process.returncode == 0, normalize_process.stderr
    assert project["status"] == "tests_failed_partial"

    aggregate_path = scratch / "coverage-summary.json"
    aggregate_process, aggregate = _invoke(
        "aggregate-coverage",
        "--project",
        project_path,
        "--high-priority-gap",
        "authorization branch",
        "--output",
        aggregate_path,
    )
    assert aggregate_process.returncode == 0, aggregate_process.stderr
    assert aggregate["status"] == "tests_failed_partial"
    assert aggregate["projects"] == [project]
    assert aggregate["high_priority_gaps"] == ["authorization branch"]

    empty_process, empty = _invoke("aggregate-coverage", "--output", scratch / "empty-summary.json")
    assert empty_process.returncode == 0, empty_process.stderr
    assert empty["status"] == "no_tests"
    assert empty["projects"] == []

    malformed = scratch / "malformed-project.json"
    malformed.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    malformed_process, malformed_error = _invoke(
        "aggregate-coverage",
        "--project",
        malformed,
        "--output",
        scratch / "invalid-summary.json",
    )
    assert malformed_process.returncode == 2
    assert "missing" in malformed_error["message"]


@pytest.mark.parametrize("mutate_source", [False, True])
def test_cleanup_removes_only_job_scratch_and_records_source_integrity(
    tmp_path: Path, mutate_source: bool
) -> None:
    workspace, record_path, record = _prepare_workspace(tmp_path)
    source = Path(record["source_path"])
    scratch = Path(record["scratch_root"])
    artifact_root = Path(record["artifact_root"])
    manifest_path = artifact_root / "run-manifest.json"
    manifest_path.write_text(
        json.dumps({"job_id": "job-1", "cleanup": {"status": "pending"}}),
        encoding="utf-8",
    )
    (scratch / "temporary.txt").write_text("temporary", encoding="utf-8")
    if mutate_source:
        (source / "source.txt").write_text("changed\n", encoding="utf-8")

    process, result = _invoke(
        "cleanup",
        "--workspace",
        workspace,
        "--artifacts",
        workspace / "artifacts",
        "--job-id",
        "job-1",
        "--prepare-record",
        record_path,
    )

    assert process.returncode == (2 if mutate_source else 0)
    assert result["scratch_removed"] is True
    assert result["source_unchanged"] is (not mutate_source)
    assert result["status"] == ("partial" if mutate_source else "complete")
    assert not scratch.exists()
    assert source.is_dir()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["cleanup"] == result


@pytest.mark.parametrize("tamper", ["record", "scratch_symlink"])
def test_cleanup_rejects_tampered_job_identity_without_following_symlinks(
    tmp_path: Path, tamper: str
) -> None:
    workspace, record_path, record = _prepare_workspace(tmp_path)
    scratch = Path(record["scratch_root"])
    source = Path(record["source_path"])
    original_source = (source / "source.txt").read_bytes()

    if tamper == "record":
        other = workspace / ".repository-critic" / "other"
        other.mkdir()
        tampered = {**record, "scratch_root": str(other)}
        _write_json(record_path, tampered)
        protected = scratch
    else:
        backup = workspace / ".repository-critic" / "job-1-backup"
        scratch.rename(backup)
        external = tmp_path / "external"
        _write_files(external, {"sentinel.txt": "do not remove\n"})
        scratch.symlink_to(external, target_is_directory=True)
        protected = external

    process, error = _invoke(
        "cleanup",
        "--workspace",
        workspace,
        "--artifacts",
        workspace / "artifacts",
        "--job-id",
        "job-1",
        "--prepare-record",
        record_path,
    )

    assert process.returncode == 2
    assert error["status"] == "blocked"
    assert protected.exists()
    if tamper == "scratch_symlink":
        assert (protected / "sentinel.txt").read_text(encoding="utf-8") == "do not remove\n"
    assert (source / "source.txt").read_bytes() == original_source


def test_archive_coverage_is_deterministic_regular_relative_evidence(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    first = scratch / "coverage" / "python"
    second = scratch / "coverage" / "go"
    _write_files(first, {"coverage.json": "{}\n", "coverage.xml": "<coverage/>\n"})
    _write_files(second, {"coverage.out": "mode: atomic\n"})
    output = tmp_path / "coverage-details.tar.gz"

    process, result = _invoke(
        "archive-coverage",
        "--scratch-root",
        scratch,
        "--coverage-dir",
        first,
        "--coverage-dir",
        second,
        "--output",
        output,
    )

    assert process.returncode == 0, process.stderr
    assert result["entries"] == 3
    with tarfile.open(output, mode="r:gz") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == [
        "project-1/coverage.json",
        "project-1/coverage.xml",
        "project-2/coverage.out",
    ]
    assert all(member.isfile() and member.uid == 0 and member.gid == 0 for member in members)
    assert all(member.mtime == 0 and not Path(member.name).is_absolute() for member in members)


def test_coverage_plan_confines_outputs_and_uses_baked_adapters(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    python_root = scratch / "python"
    interpreter = python_root / ".venv" / "bin" / "python"
    _write_files(python_root, {".venv/bin/python": "", "tests/test_example.py": ""})
    interpreter.chmod(0o755)
    coverage_dir = scratch / "coverage" / "python"

    process, plan = _invoke(
        "coverage-plan",
        "--root",
        python_root,
        "--scratch-root",
        scratch,
        "--coverage-dir",
        coverage_dir,
        "--ecosystem",
        "python",
        "--test-argv-json",
        json.dumps(["python3.12", "-m", "unittest", "discover"]),
    )

    assert process.returncode == 0, process.stderr
    assert plan["normalizer"]["format"] == "coveragepy"
    injection = plan["commands"][0]
    assert injection[:3] == ["uv", "pip", "install"]
    assert "--offline" in injection
    assert "/opt/remoteagent/agent/python-wheelhouse" in injection
    coverage_run = plan["commands"][2]
    assert coverage_run[:4] == [str(interpreter), "-m", "coverage", "run"]
    assert coverage_run[-3:] == ["-m", "unittest", "discover"]

    escaped_process, escaped = _invoke(
        "coverage-plan",
        "--root",
        python_root,
        "--scratch-root",
        scratch,
        "--coverage-dir",
        tmp_path / "outside",
        "--ecosystem",
        "python",
    )
    assert escaped_process.returncode == 2
    assert "escapes" in escaped["message"]


@pytest.mark.parametrize(
    ("ecosystem", "files", "expected_command", "expected_format", "native_names"),
    [
        (
            "javascript-typescript",
            {
                "package.json": json.dumps(
                    {
                        "name": "fixture",
                        "version": "1.0.0",
                        "packageManager": "npm@10.9.3",
                    }
                )
            },
            ["c8", "--all", "--reporter=json", "--reporter=cobertura"],
            "istanbul",
            {"coverage-final.json", "cobertura-coverage.xml"},
        ),
        (
            "go",
            {"go.mod": "module example.test/fixture\n\ngo 1.27\n"},
            [
                "go",
                "test",
                "-count=1",
                "-covermode=atomic",
                "-coverpkg=./...",
            ],
            "go",
            {"coverage.out"},
        ),
    ],
)
def test_coverage_plan_for_node_and_go_is_deterministic_and_confined(
    tmp_path: Path,
    ecosystem: str,
    files: dict[str, str],
    expected_command: list[str],
    expected_format: str,
    native_names: set[str],
) -> None:
    scratch = tmp_path / "scratch"
    root = scratch / "repository"
    _write_files(root, files)
    coverage_dir = scratch / "coverage" / ecosystem

    process, plan = _invoke(
        "coverage-plan",
        "--root",
        root,
        "--scratch-root",
        scratch,
        "--coverage-dir",
        coverage_dir,
        "--ecosystem",
        ecosystem,
    )

    assert process.returncode == 0, process.stderr
    assert plan["normalizer"]["format"] == expected_format
    assert plan["commands"][0][: len(expected_command)] == expected_command
    if ecosystem == "javascript-typescript":
        assert plan["commands"][0][-2:] == ["npm", "test"]
        assert plan["normalizer"]["package_manager"] == "npm@10.9.3"
    else:
        assert plan["commands"][0].count("-coverpkg=./...") == 1
        assert plan["commands"][0][-2:] == ["-json", "./..."]
        assert plan["commands"][1][:3] == ["go", "tool", "cover"]
    native = {Path(path).name for path in plan["native_artifacts"]}
    assert native == native_names
    for path in plan["native_artifacts"]:
        assert Path(path).is_relative_to(scratch)


def test_finalize_validates_redacts_cleans_and_publishes_exact_six_artifacts(
    tmp_path: Path,
) -> None:
    secret = "finalizer-secret-value"
    workspace, record_path, record = _prepare_workspace(tmp_path)
    source = Path(record["source_path"])
    source_before = (source / "source.txt").read_bytes()
    scratch = Path(record["scratch_root"])
    drafts = _valid_finalizer_drafts(record, secret=secret)

    process, result = _finalize(workspace, record_path, record, drafts)

    assert process.returncode == 0, process.stderr
    artifact_root = Path(result["artifact_root"])
    assert {path.name for path in artifact_root.iterdir()} == ARTIFACT_NAMES
    assert result["artifacts"] == sorted(ARTIFACT_NAMES)
    assert result["cleanup"]["status"] == "complete"
    assert result["cleanup"]["scratch_removed"] is True
    assert result["cleanup"]["source_unchanged"] is True
    assert not scratch.exists()
    assert (source / "source.txt").read_bytes() == source_before

    for name in ARTIFACT_NAMES - {"coverage-details.tar.gz"}:
        assert secret not in (artifact_root / name).read_text(encoding="utf-8")
    assert "[REDACTED]" in (artifact_root / "repository-review.md").read_text(encoding="utf-8")
    assert "[REDACTED]" in (artifact_root / "test-run.log").read_text(encoding="utf-8")
    review = json.loads((artifact_root / "repository-review.json").read_text(encoding="utf-8"))
    coverage = json.loads((artifact_root / "coverage-summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((artifact_root / "run-manifest.json").read_text(encoding="utf-8"))
    assert review["schema_version"] == coverage["schema_version"] == manifest["schema_version"] == 1
    assert review["review_mode"] == coverage["review_mode"] == manifest["review_mode"]
    assert manifest["cleanup"] == result["cleanup"]
    assert manifest["finished_at"] == result["cleanup"]["finished_at"]
    with tarfile.open(artifact_root / "coverage-details.tar.gz", mode="r:gz") as archive:
        assert [member.name for member in archive.getmembers()] == ["project-1/native.json"]


def test_finalize_rejects_invalid_schema_before_cleanup_or_publication(tmp_path: Path) -> None:
    workspace, record_path, record = _prepare_workspace(tmp_path)
    drafts = _valid_finalizer_drafts(record)
    coverage = json.loads(drafts["coverage"].read_text(encoding="utf-8"))
    coverage["unexpected"] = True
    _write_json(drafts["coverage"], coverage)

    process, error = _finalize(workspace, record_path, record, drafts)

    assert process.returncode == 2
    assert error["status"] == "blocked"
    assert "coverage summary" in error["message"]
    assert Path(record["scratch_root"]).is_dir()
    assert list(Path(record["artifact_root"]).iterdir()) == []


def test_finalize_rejects_oversized_primary_artifact_before_cleanup(tmp_path: Path) -> None:
    workspace, record_path, record = _prepare_workspace(tmp_path)
    drafts = _valid_finalizer_drafts(record)
    drafts["report"].write_bytes(b"x" * (16 * 1024 * 1024 + 1))

    process, error = _finalize(workspace, record_path, record, drafts)

    assert process.returncode == 2
    assert error["kind"] == "resource_limited"
    assert "16 MiB" in error["message"]
    assert Path(record["scratch_root"]).is_dir()
    assert list(Path(record["artifact_root"]).iterdir()) == []
