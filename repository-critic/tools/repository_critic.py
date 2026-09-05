#!/usr/bin/env python3.12
"""Deterministic safety and coverage helpers for the Repository Critic.

The helper deliberately uses only Python's standard library.  It prepares an
isolated snapshot, validates dependency references, plans (but does not hide)
restores, executes argv without a shell under fixed limits, and normalizes the
three supported coverage formats.  The Codex agent remains responsible for the
substantive review and for assembling the final evidence artifacts.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import gzip
import hashlib
import ipaddress
import json
import math
import os
import pathlib
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, BinaryIO


SCHEMA_VERSION = 1
REVIEW_MODE = "repository_snapshot"
JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
COMPANION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
MAX_PROJECT_ROOTS = 6
RESTORE_TIMEOUT_SECONDS = 600
TEST_TIMEOUT_SECONDS = 1_200
TOTAL_DYNAMIC_SECONDS = 2_700
SCRATCH_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
LOG_LIMIT_BYTES = 5 * 1024 * 1024
NATIVE_COVERAGE_LIMIT_BYTES = 25 * 1024 * 1024
MAX_ARTIFACTS = 6
PRIMARY_ARTIFACT_LIMIT_BYTES = 16 * 1024 * 1024
TRUSTED_RECORD_LIMIT_BYTES = 16 * 1024 * 1024
PREPARE_RECORD_LIMIT_BYTES = 1024 * 1024
TERMINATE_GRACE_SECONDS = 10
MANAGED_PROXY_PROBE_TIMEOUT_SECONDS = 0.5
MANAGED_PROXY_UNAVAILABLE = "managed_proxy_unavailable"
MANAGED_PROXY_URL_AUTHORITY_RE = re.compile(
    r"https?://(?:localhost|(?:[0-9]{1,3}\.){3}[0-9]{1,3}|\[[0-9a-f:.%]+\]):[0-9]+",
    flags=re.I,
)
PYTHON_VENV_RESET_COMMAND = ("python3.12", "-I", "-m", "venv", "--clear", ".venv")

COVERAGE_STATUSES = {
    "complete",
    "tests_failed_partial",
    "blocked_dependency_restore",
    "unsupported_toolchain",
    "timed_out",
    "resource_limited",
    "no_tests",
}

SUPPORTED_PACKAGE_MANAGERS = {
    "npm": {"10.9.3"},
    "pnpm": {"9.15.9", "10.34.5", "11.25.0"},
    "yarn": {"1.22.22", "4.18.0"},
}

IGNORED_SCAN_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}

SENSITIVE_ENV_RE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY|CREDENTIAL|AUTHORIZATION|API_KEY)",
    re.IGNORECASE,
)
SENSITIVE_FLAG_RE = re.compile(
    r"^--?(?:token|password|passwd|secret|authorization|api[-_]?key|credential)(?:=|$)",
    re.IGNORECASE,
)
REDACTION_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:token|password|passwd|secret|authorization|api[_-]?key)\s*[:=]\s*)"
        r"[^\s,;]+"
    ),
    re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
    re.compile(r"(?i)(//registry[^\s=]*:_authToken\s*=\s*)[^\s]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


class CriticError(RuntimeError):
    """A safe, caller-actionable helper failure."""

    def __init__(self, message: str, *, kind: str = "invalid_input") -> None:
        super().__init__(message)
        self.kind = kind


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _is_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def atomic_write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def atomic_write_text(path: pathlib.Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CriticError(f"cannot read JSON {path}: {exc}") from exc


def read_json_bounded(path: pathlib.Path, limit: int, label: str) -> Any:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CriticError(f"cannot inspect {label}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CriticError(f"{label} must be a regular file")
    if metadata.st_size > limit:
        raise CriticError(f"{label} exceeds {limit} bytes", kind="resource_limited")
    return read_json(path)


def ensure_within(path: pathlib.Path, root: pathlib.Path, label: str) -> pathlib.Path:
    resolved = path.resolve(strict=False)
    root_resolved = root.resolve(strict=True)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise CriticError(f"{label} escapes {root_resolved}: {path}") from exc
    return resolved


def _walk_entries(root: pathlib.Path) -> Iterable[pathlib.Path]:
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        base = pathlib.Path(current)
        for name in directories:
            yield base / name
        for name in files:
            yield base / name


def validate_snapshot_tree(root: pathlib.Path) -> None:
    """Reject symlink escapes and non-file filesystem objects before copying."""

    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise CriticError(f"repository companion is not a directory: {root}")
    for path in _walk_entries(resolved_root):
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise CriticError(f"cannot inspect snapshot entry {path}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raw_target = os.readlink(path)
            if pathlib.Path(raw_target).is_absolute():
                relative = path.relative_to(resolved_root)
                raise CriticError(
                    f"absolute symlink is not safe in a copied snapshot: {relative}"
                )
            try:
                target = path.resolve(strict=False)
                target.relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                relative = path.relative_to(resolved_root)
                raise CriticError(f"symlink escapes repository snapshot: {relative}") from exc
        elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            relative = path.relative_to(resolved_root)
            raise CriticError(f"unsupported special file in repository snapshot: {relative}")


def snapshot_sha256(root: pathlib.Path) -> str:
    """Hash paths, file modes, regular-file contents, and symlink text."""

    resolved_root = root.resolve(strict=True)
    digest = hashlib.sha256()
    for path in _walk_entries(resolved_root):
        relative = path.relative_to(resolved_root).as_posix().encode("utf-8", "surrogateescape")
        metadata = path.lstat()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path).encode("utf-8", "surrogateescape")
            digest.update(b"L")
            digest.update(len(target).to_bytes(8, "big"))
            digest.update(target)
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"D")
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"F")
            digest.update(metadata.st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        else:
            raise CriticError(f"unsupported special file while hashing: {path}")
    return digest.hexdigest()


def git_provenance(root: pathlib.Path) -> tuple[str | None, bool | None]:
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    common = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(root),
    ]
    try:
        commit_process = subprocess.run(
            [*common, "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        if commit_process.returncode != 0:
            return None, None
        commit = commit_process.stdout.strip()
        status_process = subprocess.run(
            [*common, "status", "--porcelain=v1", "--untracked-files=normal"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
        )
        dirty = None if status_process.returncode else bool(status_process.stdout)
        return commit or None, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def list_companions(workspace: pathlib.Path) -> dict[str, pathlib.Path]:
    companions_root = workspace / "companions"
    if not companions_root.is_dir():
        return {}
    candidates: dict[str, pathlib.Path] = {}
    for candidate in sorted(companions_root.iterdir(), key=lambda item: item.name):
        if not COMPANION_NAME_RE.fullmatch(candidate.name):
            continue
        try:
            resolved = ensure_within(candidate, workspace, "companion")
        except CriticError:
            continue
        if resolved.is_dir():
            candidates[candidate.name] = resolved
    return candidates


def select_companion(
    candidates: Mapping[str, pathlib.Path], requested_name: str | None
) -> tuple[str, pathlib.Path]:
    if requested_name is not None:
        if not COMPANION_NAME_RE.fullmatch(requested_name):
            raise CriticError(f"invalid companion name: {requested_name!r}")
        selected = candidates.get(requested_name)
        if selected is None:
            names = ", ".join(sorted(candidates)) or "none"
            raise CriticError(
                f"requested companion {requested_name!r} is unavailable; available: {names}",
                kind="blocked_companion_selection",
            )
        return requested_name, selected
    if "repository" in candidates:
        return "repository", candidates["repository"]
    if len(candidates) == 1:
        return next(iter(candidates.items()))
    names = ", ".join(sorted(candidates)) or "none"
    raise CriticError(
        f"repository companion is ambiguous; explicitly select one of: {names}",
        kind="blocked_companion_selection",
    )


def _blocked_repository_record() -> dict[str, None]:
    return {
        "companion_name": None,
        "source_path": None,
        "scratch_path": None,
        "snapshot_sha256": None,
        "git_commit": None,
        "git_dirty": None,
    }


def _enforced_limits() -> dict[str, int]:
    return {
        "max_project_roots": MAX_PROJECT_ROOTS,
        "restore_timeout_seconds": RESTORE_TIMEOUT_SECONDS,
        "test_timeout_seconds": TEST_TIMEOUT_SECONDS,
        "total_dynamic_seconds": TOTAL_DYNAMIC_SECONDS,
        "scratch_limit_bytes": SCRATCH_LIMIT_BYTES,
        "log_limit_bytes": LOG_LIMIT_BYTES,
        "native_coverage_limit_bytes": NATIVE_COVERAGE_LIMIT_BYTES,
        "max_artifacts": MAX_ARTIFACTS,
    }


def _publish_blocked_companion_selection(
    *,
    workspace: pathlib.Path,
    artifacts_base: pathlib.Path,
    job_id: str,
    available_companions: Sequence[str],
    requested_name: str | None,
    reason: str,
) -> dict[str, Any]:
    """Atomically publish the six-file contract when no snapshot can be selected."""

    artifacts_base = artifacts_base.resolve(strict=True)
    artifact_root = artifacts_base / f"repository-review-{job_id}"
    ensure_within(artifact_root, artifacts_base, "blocked artifact directory")
    if artifact_root.is_symlink():
        raise CriticError("blocked artifact root must not be a symlink")
    if artifact_root.exists():
        if not artifact_root.is_dir():
            raise CriticError("blocked artifact root is not a directory")
        existing = sorted(path.name for path in artifact_root.iterdir())
        if existing:
            raise CriticError(
                "blocked artifact root must be empty before publication: " + ", ".join(existing)
            )
    else:
        artifact_root.mkdir(mode=0o700)

    safe_reason = redact_text(reason)
    names = list(available_companions)
    selection = requested_name if requested_name is not None else "none"
    started_at = utc_now()
    repository = _blocked_repository_record()
    limitation = (
        "No repository snapshot was selected, so documentation, implementation, tests, "
        "coverage, and architecture could not be assessed."
    )
    report = (
        "# Repository review: blocked\n\n"
        "Review mode: `repository_snapshot`\n\n"
        "The repository companion could not be selected unambiguously. "
        f"{safe_reason}\n\n"
        f"Requested companion: `{redact_text(selection)}`\n\n"
        "Available companions: "
        + (", ".join(f"`{redact_text(name)}`" for name in names) if names else "none")
        + "\n\n"
        "Submit the job again with one exact companion name. No repository content was "
        "opened or executed.\n"
    )
    review = {
        "schema_version": SCHEMA_VERSION,
        "review_mode": REVIEW_MODE,
        "status": "blocked",
        "repository": repository,
        "summary": {
            "verdict": safe_reason,
            "p0_count": 0,
            "p1_count": 0,
            "p2_count": 0,
            "p3_count": 0,
        },
        "traceability": [],
        "findings": [],
        "dynamic_analysis": {
            "status": "blocked_companion_selection",
            "project_count": 0,
            "test_run_count": 0,
            "coverage_run_count": 0,
        },
        "limitations": [limitation],
    }
    coverage = {
        "schema_version": SCHEMA_VERSION,
        "review_mode": REVIEW_MODE,
        "status": "no_tests",
        "projects": [],
        "high_priority_gaps": [],
    }
    toolchain_path = pathlib.Path(__file__).resolve().parents[1] / "toolchain-manifest.json"
    toolchains = read_json(toolchain_path)
    finished_at = utc_now()
    cleanup = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "finished_at": finished_at,
        "duration_seconds": 0.0,
        "source_unchanged": True,
        "source_snapshot_sha256": None,
        "scratch_removed": True,
        "scratch_bytes_before": 0,
        "entries_removed": 0,
        "status": "complete",
        "error": None,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "review_mode": REVIEW_MODE,
        "job_id": job_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "total_duration_seconds": 0.0,
        "repository": repository,
        "policy": {
            "network": "managed public-host egress not used",
            "build_hooks": False,
            "locked_restore_required": True,
        },
        "toolchains": toolchains,
        "commands": [],
        "dependency_restores": [],
        "limits": _enforced_limits(),
        "cleanup": cleanup,
        "limitations": [limitation],
        "log_truncated": False,
    }
    blocked_prepare = {
        "status": "blocked",
        "job_id": job_id,
        "companion_name": None,
        "source_path": None,
        "repository_path": None,
        "source_snapshot_sha256": None,
        "git_commit": None,
        "git_dirty": None,
    }
    validate_repository_review(review, blocked_prepare)
    validate_coverage_summary(coverage)
    validate_run_manifest(manifest, blocked_prepare, job_id)

    staging_name = f".repository-review-{job_id}.blocked-finalizing"
    staging = artifacts_base / staging_name
    if staging.exists() or staging.is_symlink():
        raise CriticError("blocked artifact staging directory already exists")
    staging.mkdir(mode=0o700)
    try:
        atomic_write_text(staging / "repository-review.md", report)
        atomic_write_json(staging / "repository-review.json", review)
        atomic_write_json(staging / "coverage-summary.json", coverage)
        atomic_write_json(staging / "run-manifest.json", manifest)
        atomic_write_text(staging / "test-run.log", "")
        _write_coverage_archive([], workspace, staging / "coverage-details.tar.gz")
        artifact_root.rmdir()
        os.replace(staging, artifact_root)
    except BaseException:
        if staging.is_dir() and not staging.is_symlink():
            _bounded_remove_tree(artifacts_base, staging_name, 30)
        raise
    artifacts = sorted(path.name for path in artifact_root.iterdir())
    if len(artifacts) != MAX_ARTIFACTS:
        raise CriticError("blocked artifact publication did not produce exactly six files")
    return {
        "schema_version": SCHEMA_VERSION,
        "review_mode": REVIEW_MODE,
        "status": "blocked",
        "kind": "blocked_companion_selection",
        "message": safe_reason,
        "available_companions": names,
        "artifact_root": str(artifact_root),
        "artifacts": artifacts,
    }


def command_prepare(args: argparse.Namespace) -> int:
    workspace = pathlib.Path(args.workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise CriticError(f"workspace is not a directory: {workspace}")
    if not JOB_ID_RE.fullmatch(args.job_id):
        raise CriticError(f"invalid RemoteAgent job id: {args.job_id!r}")
    candidates = list_companions(workspace)
    try:
        companion_name, source = select_companion(candidates, args.companion_name)
    except CriticError as exc:
        if exc.kind != "blocked_companion_selection":
            raise
        result = _publish_blocked_companion_selection(
            workspace=workspace,
            artifacts_base=pathlib.Path(args.artifacts),
            job_id=args.job_id,
            available_companions=sorted(candidates),
            requested_name=args.companion_name,
            reason=str(exc),
        )
        print(json.dumps(result, sort_keys=True))
        return 2
    validate_snapshot_tree(source)
    source_digest = snapshot_sha256(source)
    git_commit, git_dirty = git_provenance(source)

    scratch_base = workspace / ".repository-critic"
    job_root = scratch_base / args.job_id
    ensure_within(job_root, workspace, "job scratch")
    if job_root.exists() or job_root.is_symlink():
        raise CriticError(
            f"job scratch already exists and will not be replaced: {job_root}",
            kind="scratch_collision",
        )
    job_root.mkdir(parents=True, mode=0o700)
    repository = job_root / "repository"
    try:
        shutil.copytree(source, repository, symlinks=True, copy_function=shutil.copy2)
    except Exception:
        # Removing a newly created, job-specific incomplete copy is recoverable
        # and avoids treating it as a valid snapshot on a later attempt.
        shutil.rmtree(job_root, ignore_errors=True)
        raise
    copied_digest = snapshot_sha256(repository)
    if copied_digest != source_digest:
        shutil.rmtree(job_root, ignore_errors=True)
        raise CriticError("scratch copy digest does not match companion digest")

    artifacts_base = pathlib.Path(args.artifacts).resolve(strict=True)
    artifact_root = artifacts_base / f"repository-review-{args.job_id}"
    ensure_within(artifact_root, artifacts_base, "artifact directory")
    artifact_root.mkdir(mode=0o700)
    for directory in (
        job_root / "home",
        job_root / "tmp",
        job_root / "cache",
        job_root / "go",
        job_root / "coverage",
    ):
        directory.mkdir(mode=0o700)

    record = {
        "schema_version": SCHEMA_VERSION,
        "review_mode": REVIEW_MODE,
        "status": "prepared",
        "job_id": args.job_id,
        "prepared_at": utc_now(),
        "companion_name": companion_name,
        "available_companions": sorted(candidates),
        "source_path": str(source),
        "source_snapshot_sha256": source_digest,
        "scratch_root": str(job_root),
        "repository_path": str(repository),
        "scratch_snapshot_sha256": copied_digest,
        "artifact_root": str(artifact_root),
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "environment": {
            "HOME": str(job_root / "home"),
            "TMPDIR": str(job_root / "tmp"),
            "XDG_CACHE_HOME": str(job_root / "cache"),
            "GOCACHE": str(job_root / "cache" / "go-build"),
            "GOMODCACHE": str(job_root / "go" / "pkg" / "mod"),
            "GOPATH": str(job_root / "go"),
        },
    }
    output = pathlib.Path(args.output)
    ensure_within(output, job_root, "prepare output")
    atomic_write_json(output, record)
    print(json.dumps(record, sort_keys=True))
    return 0


def command_verify_source(args: argparse.Namespace) -> int:
    prepare = read_json_bounded(
        pathlib.Path(args.prepare_record), PREPARE_RECORD_LIMIT_BYTES, "prepare record"
    )
    source = pathlib.Path(str(prepare.get("source_path", "")))
    repository = pathlib.Path(str(prepare.get("repository_path", "")))
    expected_source = prepare.get("source_snapshot_sha256")
    expected_scratch = prepare.get("scratch_snapshot_sha256")
    source_actual = snapshot_sha256(source)
    scratch_actual = snapshot_sha256(repository)
    result = {
        "schema_version": SCHEMA_VERSION,
        "verified_at": utc_now(),
        "source_unchanged": source_actual == expected_source,
        "source_snapshot_sha256": source_actual,
        "scratch_initial_sha256": expected_scratch,
        "scratch_current_sha256": scratch_actual,
        "scratch_changed": scratch_actual != expected_scratch,
    }
    if args.output:
        atomic_write_json(pathlib.Path(args.output), result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["source_unchanged"] else 2


def sanitize_argv(argv: Sequence[str]) -> list[str]:
    sanitized: list[str] = []
    redact_next = False
    for item in argv:
        if redact_next:
            sanitized.append("[REDACTED]")
            redact_next = False
            continue
        if SENSITIVE_FLAG_RE.match(item):
            if "=" in item:
                sanitized.append(item.split("=", 1)[0] + "=[REDACTED]")
            else:
                sanitized.append(item)
                redact_next = True
            continue
        sanitized.append(redact_text(item))
    return sanitized


def redact_text(value: str) -> str:
    redacted = value
    for pattern in REDACTION_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED PRIVATE KEY]", redacted)
    return redacted


def directory_size(root: pathlib.Path) -> int:
    total = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = [
            name for name in directories if not (pathlib.Path(current) / name).is_symlink()
        ]
        for name in files:
            path = pathlib.Path(current) / name
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
    return total


@dataclasses.dataclass
class StreamCapture:
    limit: int
    content: bytearray = dataclasses.field(default_factory=bytearray)
    truncated: bool = False

    def consume(self, stream: BinaryIO) -> None:
        while True:
            block = stream.read(65_536)
            if not block:
                return
            available = max(0, self.limit - len(self.content))
            if available:
                self.content.extend(block[:available])
            if len(block) > available:
                self.truncated = True

    def decoded_text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def text(self) -> str:
        return redact_text(self.decoded_text())


def _load_budget(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "spent_seconds": 0.0, "runs": []}
    value = read_json_bounded(path, TRUSTED_RECORD_LIMIT_BYTES, "dynamic command ledger")
    if not isinstance(value, dict):
        raise CriticError(f"dynamic budget ledger is not an object: {path}")
    return value


def _process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> str | None:
    group_id = process.pid
    if not _process_group_exists(group_id):
        return None
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return None
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while _process_group_exists(group_id) and time.monotonic() < deadline:
        if process.poll() is None:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.05)
    if _process_group_exists(group_id):
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait()
        return "SIGKILL"
    if process.poll() is None:
        process.wait()
    return "SIGTERM"


@dataclasses.dataclass(frozen=True)
class _ManagedProxyEndpoint:
    """A validated loopback proxy endpoint that is never serialized."""

    host: str
    port: int
    probe_hosts: tuple[str, ...]


class _ManagedProxyUnavailableError(RuntimeError):
    """Internal control flow for a failed, sanitized proxy readiness check."""


class _ProjectPythonToolUnavailableError(RuntimeError):
    """Internal control flow when a bare tool is absent from the project venv."""


def _managed_loopback_proxy_endpoint(value: str | None) -> _ManagedProxyEndpoint | None:
    """Return a safe-to-probe endpoint only for an explicit loopback proxy URL."""

    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    normalized_host = host.lower()
    if normalized_host == "localhost":
        return _ManagedProxyEndpoint(
            host=normalized_host,
            port=port,
            probe_hosts=("127.0.0.1", "::1"),
        )
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        return None
    if not address.is_loopback:
        return None
    compressed = address.compressed
    return _ManagedProxyEndpoint(host=compressed, port=port, probe_hosts=(compressed,))


def _managed_proxy_is_available(endpoint: _ManagedProxyEndpoint) -> bool:
    """Probe only a validated loopback address and suppress endpoint-bearing errors."""

    for host in endpoint.probe_hosts:
        try:
            with socket.create_connection(
                (host, endpoint.port), timeout=MANAGED_PROXY_PROBE_TIMEOUT_SECONDS
            ):
                return True
        except OSError:
            continue
    return False


def _redact_managed_proxy_details(
    value: str,
    inherited_proxy: str | None,
    endpoint: _ManagedProxyEndpoint | None,
) -> str:
    """Remove inherited proxy values and endpoint details from published text."""

    redacted = value
    if inherited_proxy:
        redacted = redacted.replace(inherited_proxy, "[REDACTED MANAGED PROXY]")
    redaction_host = endpoint.host if endpoint is not None else None
    redaction_port = endpoint.port if endpoint is not None else None
    if inherited_proxy and (redaction_host is None or redaction_port is None):
        try:
            parsed = urllib.parse.urlsplit(inherited_proxy)
            redaction_host = parsed.hostname
            redaction_port = parsed.port
        except (TypeError, ValueError):
            pass
    if redaction_host is not None and redaction_port is not None:
        def redact_equivalent_url(match: re.Match[str]) -> str:
            candidate = _managed_loopback_proxy_endpoint(match.group(0))
            if candidate is not None and candidate.port == redaction_port:
                return "[REDACTED MANAGED PROXY]"
            return match.group(0)

        redacted = MANAGED_PROXY_URL_AUTHORITY_RE.sub(redact_equivalent_url, redacted)
        endpoint_tokens = {
            f"{redaction_host}:{redaction_port}",
            f"[{redaction_host}]:{redaction_port}",
            f"127.0.0.1:{redaction_port}",
            f"[::1]:{redaction_port}",
            f"localhost:{redaction_port}",
        }
        for token in sorted(endpoint_tokens, key=len, reverse=True):
            redacted = re.sub(re.escape(token), "[REDACTED MANAGED PROXY]", redacted, flags=re.I)
        redacted = re.sub(
            rf"(?<!\d){redaction_port}(?!\d)", "[REDACTED MANAGED PROXY PORT]", redacted
        )
    return redact_text(redacted)


def _sanitize_command_argv(
    command: Sequence[str],
    inherited_proxy: str | None,
    endpoint: _ManagedProxyEndpoint | None,
) -> list[str]:
    proxy_redacted = [
        _redact_managed_proxy_details(item, inherited_proxy, endpoint) for item in command
    ]
    return sanitize_argv(proxy_redacted)


def _iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_string_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_string_values(item)


def _contains_inherited_managed_proxy_details(value: Any) -> bool:
    inherited_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    endpoint = _managed_loopback_proxy_endpoint(inherited_proxy)
    if endpoint is None:
        return False
    return any(
        _redact_managed_proxy_details(item, inherited_proxy, endpoint) != redact_text(item)
        for item in _iter_string_values(value)
    )


def safe_subprocess_environment(
    scratch_root: pathlib.Path,
    allow_build_hooks: bool,
    *,
    phase: str,
    restore_mode: str,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not SENSITIVE_ENV_RE.search(key)
    }
    environment.pop("VIRTUAL_ENV", None)
    for unsafe_name in (
        "NODE_OPTIONS",
        "NPM_CONFIG_SCRIPT_SHELL",
        "npm_config_script_shell",
        "BASH_ENV",
        "ENV",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(unsafe_name, None)
    home = scratch_root / "home"
    temp = scratch_root / "tmp"
    cache = scratch_root / "cache"
    go_path = scratch_root / "go"
    for directory in (home, temp, cache, go_path, cache / "go-build", go_path / "pkg" / "mod"):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temp),
            "XDG_CACHE_HOME": str(cache),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/dev/null",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_INDEX_URL": "https://pypi.org/simple",
            "PIPENV_PYPI_MIRROR": "https://pypi.org/simple",
            "PIPENV_VENV_IN_PROJECT": "1",
            "POETRY_VIRTUALENVS_IN_PROJECT": "true",
            "UV_CACHE_DIR": str(cache / "uv"),
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "GOCACHE": str(cache / "go-build"),
            "GOMODCACHE": str(go_path / "pkg" / "mod"),
            "GOPATH": str(go_path),
            "GOTOOLCHAIN": "local",
            "GOPROXY": "https://proxy.golang.org",
            "GOSUMDB": "sum.golang.org",
            "GONOSUMDB": "",
            "GONOPROXY": "",
            "GOPRIVATE": "",
            "GOFLAGS": "-mod=readonly" if restore_mode == "locked" else "",
            "COREPACK_HOME": "/opt/remoteagent/corepack",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "COREPACK_DEFAULT_TO_LATEST": "0",
            "COREPACK_ENABLE_NETWORK": "0",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_CACHE": str(cache / "npm"),
            "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "PNPM_HOME": str(scratch_root / "pnpm-home"),
            "YARN_CACHE_FOLDER": str(cache / "yarn"),
            "YARN_NPM_REGISTRY_SERVER": "https://registry.npmjs.org/",
        }
    )
    proxy_names = {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    inherited_https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    for name in proxy_names:
        environment.pop(name, None)
    if phase == "restore" and inherited_https_proxy:
        environment["HTTPS_PROXY"] = inherited_https_proxy
        environment["https_proxy"] = inherited_https_proxy
    elif phase != "restore":
        # The managed sandbox is the network boundary; removing proxy discovery
        # is additional defense against tests accidentally using public egress.
        environment["NO_PROXY"] = "*"
        environment["no_proxy"] = "*"
    if allow_build_hooks:
        environment.update(
            {
                "NPM_CONFIG_IGNORE_SCRIPTS": "false",
                "npm_config_ignore_scripts": "false",
                "YARN_ENABLE_SCRIPTS": "true",
                "POETRY_INSTALLER_NO_BINARY": "",
                "POETRY_INSTALLER_ONLY_BINARY": "",
                "PIP_ONLY_BINARY": "",
            }
        )
    else:
        environment.update(
            {
                "NPM_CONFIG_IGNORE_SCRIPTS": "true",
                "npm_config_ignore_scripts": "true",
                "YARN_ENABLE_SCRIPTS": "false",
                "POETRY_INSTALLER_ONLY_BINARY": ":all:",
                "PIP_ONLY_BINARY": ":all:",
            }
        )
    return environment


def _activate_project_venv(
    environment: dict[str, str], cwd: pathlib.Path, scratch_root: pathlib.Path
) -> pathlib.Path | None:
    venv = cwd / ".venv"
    configuration = venv / "pyvenv.cfg"
    binary_directory = venv / "bin"
    if (
        venv.is_symlink()
        or configuration.is_symlink()
        or not configuration.is_file()
        or binary_directory.is_symlink()
        or not binary_directory.is_dir()
    ):
        return None
    ensure_within(venv, scratch_root, "project virtual environment")
    ensure_within(binary_directory, scratch_root, "project virtual environment binaries")
    environment["VIRTUAL_ENV"] = str(venv)
    environment["PATH"] = str(binary_directory) + os.pathsep + environment.get("PATH", "")
    return binary_directory


def _confine_bare_python_tool(
    command: list[str], binary_directory: pathlib.Path | None, scratch_root: pathlib.Path
) -> None:
    if command[0] not in {"pytest", "py.test", "coverage"}:
        return
    if binary_directory is None:
        raise _ProjectPythonToolUnavailableError
    executable = ensure_within(
        binary_directory / command[0], scratch_root, "project Python test tool"
    )
    if not executable.is_file():
        raise _ProjectPythonToolUnavailableError
    command[0] = str(executable)


def _configure_python_source_path(
    environment: dict[str, str], cwd: pathlib.Path, scratch_root: pathlib.Path
) -> None:
    source_directory = cwd / "src"
    if not source_directory.exists() and not source_directory.is_symlink():
        return
    if source_directory.is_symlink() or not source_directory.is_dir():
        raise CriticError("conventional Python src path must be a real directory")
    source_directory = ensure_within(
        source_directory, scratch_root, "conventional Python src path"
    )
    environment["PYTHONPATH"] = str(source_directory)


def _validate_python_venv_reset(cwd: pathlib.Path, scratch_root: pathlib.Path) -> None:
    venv = cwd / ".venv"
    if venv.is_symlink():
        raise CriticError("project virtual environment reset target must not be a symlink")
    resolved = ensure_within(venv, scratch_root, "project virtual environment reset target")
    if resolved.exists() and not resolved.is_dir():
        raise CriticError("project virtual environment reset target must be a directory")


def command_run(args: argparse.Namespace) -> int:
    scratch_root = pathlib.Path(args.scratch_root).resolve(strict=True)
    cwd = ensure_within(pathlib.Path(args.cwd), scratch_root, "command working directory")
    if not cwd.is_dir():
        raise CriticError(f"command working directory is not a directory: {cwd}")
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise CriticError("run requires argv after --")
    if "\x00" in "".join(command):
        raise CriticError("command argv contains a NUL byte")
    inherited_proxy = None
    if args.phase == "restore":
        inherited_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    managed_proxy_endpoint = _managed_loopback_proxy_endpoint(inherited_proxy)

    phase_limit = RESTORE_TIMEOUT_SECONDS if args.phase == "restore" else TEST_TIMEOUT_SECONDS
    requested_timeout = args.timeout if args.timeout is not None else phase_limit
    timeout = min(max(1, requested_timeout), phase_limit)
    total_budget = min(max(1, args.total_budget), TOTAL_DYNAMIC_SECONDS)
    disk_limit = min(max(1, args.disk_limit), SCRATCH_LIMIT_BYTES)
    log_limit = min(max(1, args.log_limit), LOG_LIMIT_BYTES)
    ledger_path = pathlib.Path(args.ledger) if args.ledger else scratch_root / ".critic-budget.json"
    ensure_within(ledger_path, scratch_root, "dynamic budget ledger")
    if ledger_path.resolve(strict=False) != (scratch_root / ".critic-budget.json").resolve(
        strict=False
    ):
        raise CriticError("custom dynamic budget ledgers are not allowed")
    log_path = None
    if args.log:
        log_path = ensure_within(pathlib.Path(args.log), scratch_root, "command log")
    output_path = None
    if args.output:
        output_path = ensure_within(pathlib.Path(args.output), scratch_root, "command record")
    ledger = _load_budget(ledger_path)
    spent = float(ledger.get("spent_seconds", 0.0))
    remaining = total_budget - spent
    if remaining <= 0:
        status = "resource_limited"
        record = {
            "id": args.id,
            "cwd": str(cwd),
            "argv": _sanitize_command_argv(command, inherited_proxy, managed_proxy_endpoint),
            "ecosystem": args.ecosystem,
            "phase": args.phase,
            "restore_mode": args.restore_mode if args.phase == "restore" else None,
            "started_at": utc_now(),
            "duration_seconds": 0.0,
            "exit_code": None,
            "signal": None,
            "status": status,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "build_hooks_enabled": bool(args.allow_build_hooks),
            "managed_proxy_state": None,
            "limit_reason": "total dynamic-analysis budget exhausted",
        }
        if output_path:
            atomic_write_json(output_path, record)
        print(json.dumps(record, sort_keys=True))
        return 75
    timeout = min(timeout, max(1, int(remaining)))
    if directory_size(scratch_root) > disk_limit:
        raise CriticError("scratch limit is already exceeded", kind="resource_limited")

    started_at = utc_now()
    started = time.monotonic()
    stdout_capture = StreamCapture(log_limit)
    stderr_capture = StreamCapture(log_limit)
    process: subprocess.Popen[bytes] | None = None
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    exit_code: int | None = None
    terminating_signal: str | None = None
    status = "start_failed"
    limit_reason: str | None = None
    managed_proxy_state: str | None = None
    try:
        command_environment = safe_subprocess_environment(
            scratch_root,
            bool(args.allow_build_hooks),
            phase=args.phase,
            restore_mode=args.restore_mode,
        )
        if args.phase == "restore":
            inherited_proxy = command_environment.get("HTTPS_PROXY")
            managed_proxy_endpoint = _managed_loopback_proxy_endpoint(inherited_proxy)
            if managed_proxy_endpoint is not None:
                if not _managed_proxy_is_available(managed_proxy_endpoint):
                    managed_proxy_state = "unavailable"
                    raise _ManagedProxyUnavailableError
                managed_proxy_state = "available"
            if args.ecosystem == "python" and tuple(command) == PYTHON_VENV_RESET_COMMAND:
                _validate_python_venv_reset(cwd, scratch_root)
        if args.phase in {"test", "coverage", "static"}:
            binary_directory = _activate_project_venv(command_environment, cwd, scratch_root)
            if args.ecosystem == "python":
                _configure_python_source_path(command_environment, cwd, scratch_root)
                _confine_bare_python_tool(command, binary_directory, scratch_root)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=command_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_thread = threading.Thread(target=stdout_capture.consume, args=(process.stdout,), daemon=True)
        stderr_thread = threading.Thread(target=stderr_capture.consume, args=(process.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        deadline = started + timeout
        next_disk_check = started
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                status = "timed_out"
                limit_reason = f"command exceeded {timeout} seconds"
                terminating_signal = _terminate_process_group(process)
                break
            if now >= next_disk_check:
                if directory_size(scratch_root) > disk_limit:
                    status = "resource_limited"
                    limit_reason = f"scratch exceeded {disk_limit} bytes"
                    terminating_signal = _terminate_process_group(process)
                    break
                next_disk_check = now + 1.0
            time.sleep(0.1)
        exit_code = process.wait()
        if status not in {"timed_out", "resource_limited"}:
            status = "success" if exit_code == 0 else "failed"
            descendant_signal = _terminate_process_group(process)
            if descendant_signal:
                terminating_signal = f"{descendant_signal}_DESCENDANTS"
                limit_reason = "terminated subprocess descendants left running after launcher exit"
                status = "failed"
    except _ManagedProxyUnavailableError:
        limit_reason = MANAGED_PROXY_UNAVAILABLE
        status = "failed"
    except _ProjectPythonToolUnavailableError:
        limit_reason = "requested bare Python test tool is absent from the project environment"
        status = "start_failed"
    except OSError as exc:
        if process is not None:
            terminating_signal = _terminate_process_group(process)
        limit_reason = _redact_managed_proxy_details(
            str(exc), inherited_proxy, managed_proxy_endpoint
        )
        status = "start_failed"
    except BaseException:
        if process is not None:
            _terminate_process_group(process)
        raise
    finally:
        if stdout_thread is not None:
            stdout_thread.join(timeout=2)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2)
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    if status == "failed" and limit_reason is None and managed_proxy_endpoint is not None:
        if not _managed_proxy_is_available(managed_proxy_endpoint):
            limit_reason = MANAGED_PROXY_UNAVAILABLE
            managed_proxy_state = "unavailable"

    duration = round(time.monotonic() - started, 6)
    sanitized_command = _sanitize_command_argv(
        command, inherited_proxy, managed_proxy_endpoint
    )
    proxy_diagnostic = (
        f"managed_proxy_state: {managed_proxy_state}\n" if managed_proxy_state is not None else ""
    )
    combined_log = (
        f"# command {args.id}\n"
        f"argv: {json.dumps(sanitized_command, ensure_ascii=False)}\n"
        f"cwd: {cwd}\n"
        f"phase: {args.phase}\n"
        f"status: {status}\n"
        f"{proxy_diagnostic}\n"
        "## stdout\n"
        f"{_redact_managed_proxy_details(stdout_capture.decoded_text(), inherited_proxy, managed_proxy_endpoint)}\n\n"
        "## stderr\n"
        f"{_redact_managed_proxy_details(stderr_capture.decoded_text(), inherited_proxy, managed_proxy_endpoint)}\n"
    )
    combined_bytes = combined_log.encode("utf-8")
    combined_truncated = len(combined_bytes) > log_limit
    if combined_truncated:
        combined_log = combined_bytes[:log_limit].decode("utf-8", "ignore") + "\n[LOG TRUNCATED]\n"
    if log_path:
        atomic_write_text(log_path, combined_log)

    record = {
        "id": args.id,
        "cwd": str(cwd),
        "argv": sanitized_command,
        "ecosystem": args.ecosystem,
        "phase": args.phase,
        "restore_mode": args.restore_mode if args.phase == "restore" else None,
        "started_at": started_at,
        "duration_seconds": duration,
        "exit_code": exit_code,
        "signal": terminating_signal,
        "status": status,
        "stdout_truncated": stdout_capture.truncated or combined_truncated,
        "stderr_truncated": stderr_capture.truncated or combined_truncated,
        "build_hooks_enabled": bool(args.allow_build_hooks),
        "managed_proxy_state": managed_proxy_state,
        "limit_reason": limit_reason,
    }
    ledger.setdefault("runs", []).append(record)
    ledger["spent_seconds"] = round(spent + duration, 6)
    ledger["schema_version"] = SCHEMA_VERSION
    atomic_write_json(ledger_path, ledger)
    if output_path:
        atomic_write_json(output_path, record)
    print(json.dumps(record, sort_keys=True))
    if status == "success":
        return 0
    if status == "timed_out":
        return 124
    if status == "resource_limited":
        return 75
    if status == "start_failed":
        return 127
    return 1


def _iter_scan_files(root: pathlib.Path, names: set[str]) -> Iterable[pathlib.Path]:
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in IGNORED_SCAN_DIRS)
        for name in sorted(files):
            if name in names:
                yield pathlib.Path(current) / name


def _local_dependency_issue(spec: str, manifest: pathlib.Path, root: pathlib.Path) -> str | None:
    spec = spec.strip()
    lowered = spec.lower()
    direct_reference = re.search(r"\s@\s*(\S.*)\Z", spec)
    if direct_reference:
        return _local_dependency_issue(direct_reference.group(1), manifest, root)
    if lowered.startswith("workspace:") or lowered.startswith("npm:"):
        return None
    if lowered.startswith(
        ("git+", "git://", "ssh://", "git@", "github:", "gitlab:", "bitbucket:")
    ) or re.match(r"^[^/@\s]+@[^:/\s]+:", spec):
        return f"network VCS dependency is not allowed: {spec}"
    if lowered.startswith("http://"):
        return f"insecure HTTP dependency is not allowed: {spec}"
    if lowered.startswith("https://"):
        parsed = urllib.parse.urlsplit(spec)
        if parsed.username is not None or parsed.password is not None:
            return f"credential-bearing dependency URL is not allowed: {parsed.hostname or 'unknown'}"
        return None
    local_prefix = next(
        (prefix for prefix in ("file:", "link:", "portal:", "path:") if lowered.startswith(prefix)),
        None,
    )
    if local_prefix:
        local_value = spec[len(local_prefix) :].split("#", 1)[0]
        candidate = pathlib.Path(local_value)
        if candidate.is_absolute():
            return f"absolute local dependency is not allowed: {spec}"
        try:
            (manifest.parent / candidate).resolve(strict=False).relative_to(root)
        except ValueError:
            return f"local dependency escapes repository: {spec}"
        return None
    if spec.startswith(("./", "../", "/", "~/", ".\\", "..\\", "\\\\")) or re.match(
        r"^[A-Za-z]:[\\/]", spec
    ):
        candidate = pathlib.Path(spec.split("#", 1)[0])
        if candidate.is_absolute() or pathlib.PureWindowsPath(spec).is_absolute() or spec.startswith("~/"):
            return f"absolute local dependency is not allowed: {spec}"
        try:
            (manifest.parent / candidate).resolve(strict=False).relative_to(root)
        except ValueError:
            return f"local dependency escapes repository: {spec}"
    return None


def _scan_package_json(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [{"path": str(path.relative_to(root)), "message": f"invalid package.json: {exc}"}]
    for section in (
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
        "resolutions",
        "overrides",
    ):
        dependencies = document.get(section, {})
        if not isinstance(dependencies, dict):
            continue
        for name, spec in dependencies.items():
            if not isinstance(spec, str):
                continue
            issue = _local_dependency_issue(spec, path, root)
            if issue is None and re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:#[^\s]+)?", spec
            ):
                issue = f"network VCS shorthand dependency is not allowed: {spec}"
            if issue:
                issues.append(
                    {
                        "path": str(path.relative_to(root)),
                        "dependency": str(name),
                        "message": issue,
                    }
                )
    return issues


def _scan_requirements(
    path: pathlib.Path, root: pathlib.Path, seen: set[pathlib.Path] | None = None
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen = seen if seen is not None else set()
    resolved_path = path.resolve(strict=False)
    if resolved_path in seen:
        return issues
    seen.add(resolved_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return [{"path": str(path.relative_to(root)), "message": f"cannot read requirements: {exc}"}]
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        option_match = re.match(
            r"^(-r|--requirement|-c|--constraint|--find-links)(?:\s+|=)(\S+)", line
        )
        if option_match:
            option_name = option_match.group(1)
            option_value = option_match.group(2).rstrip("\\")
            issue = _local_dependency_issue(option_value, path, root)
            if issue is None and not re.match(
                r"(?:https?|file|link|portal|path):", option_value, re.IGNORECASE
            ):
                issue = _local_dependency_issue(f"path:{option_value}", path, root)
            if issue:
                issues.append(
                    {"path": str(path.relative_to(root)), "line": str(number), "message": issue}
                )
            elif option_name != "--find-links" and not re.match(
                r"https?://", option_value, re.IGNORECASE
            ):
                included = (path.parent / option_value).resolve(strict=False)
                try:
                    included.relative_to(root)
                except ValueError:
                    pass
                else:
                    if included.is_file():
                        issues.extend(_scan_requirements(included, root, seen))
        if line.startswith(("--trusted-host", "--index-url=http://", "--extra-index-url=http://")):
            issues.append(
                {
                    "path": str(path.relative_to(root)),
                    "line": str(number),
                    "message": "trusted-host and insecure HTTP package index overrides are not allowed",
                }
            )
            continue
        for url in re.findall(r"https?://[^\s]+", line):
            issue = _local_dependency_issue(url.rstrip("\\"), path, root)
            if issue:
                issues.append(
                    {"path": str(path.relative_to(root)), "line": str(number), "message": issue}
                )
        candidate = line.split(" ;", 1)[0]
        if " @ " in candidate:
            candidate = candidate.split(" @ ", 1)[1].strip()
        issue = _local_dependency_issue(candidate, path, root)
        if issue:
            issues.append(
                {"path": str(path.relative_to(root)), "line": str(number), "message": issue}
            )
    return issues


def _scan_pyproject(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [{"path": str(path.relative_to(root)), "message": f"invalid pyproject.toml: {exc}"}]
    issues: list[dict[str, str]] = []
    project = document.get("project", {})
    raw_specs: list[tuple[str, str]] = []
    if isinstance(project, dict):
        for spec in project.get("dependencies", []) if isinstance(project.get("dependencies", []), list) else []:
            if isinstance(spec, str):
                raw_specs.append(("project.dependencies", spec))
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group, values in optional.items():
                if isinstance(values, list):
                    raw_specs.extend(
                        (f"project.optional-dependencies.{group}", value)
                        for value in values
                        if isinstance(value, str)
                    )
    poetry = document.get("tool", {}).get("poetry", {}) if isinstance(document.get("tool", {}), dict) else {}
    if isinstance(poetry, dict):
        dependencies = poetry.get("dependencies", {})
        if isinstance(dependencies, dict):
            for name, value in dependencies.items():
                if isinstance(value, str):
                    raw_specs.append((f"tool.poetry.dependencies.{name}", value))
                elif isinstance(value, dict):
                    if isinstance(value.get("git"), str):
                        issues.append(
                            {
                                "path": str(path.relative_to(root)),
                                "dependency": f"tool.poetry.dependencies.{name}.git",
                                "message": "network VCS dependency is not allowed",
                            }
                        )
                    if isinstance(value.get("url"), str):
                        raw_specs.append((f"tool.poetry.dependencies.{name}.url", value["url"]))
                    if isinstance(value.get("path"), str):
                        raw_specs.append((f"tool.poetry.dependencies.{name}.path", f"path:{value['path']}"))
        sources = poetry.get("source", [])
        if isinstance(sources, list):
            for index, source in enumerate(sources):
                if isinstance(source, dict) and isinstance(source.get("url"), str):
                    raw_specs.append((f"tool.poetry.source.{index}.url", source["url"]))
    for location, spec in raw_specs:
        issue = _local_dependency_issue(spec, path, root)
        if issue:
            issues.append(
                {"path": str(path.relative_to(root)), "dependency": location, "message": issue}
            )
    return issues


def _scan_pipfile(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [{"path": str(path.relative_to(root)), "message": f"invalid Pipfile: {exc}"}]
    issues: list[dict[str, str]] = []
    sources = document.get("source", [])
    if isinstance(sources, list):
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if isinstance(url, str):
                issue = _local_dependency_issue(url, path, root)
                if issue:
                    issues.append(
                        {
                            "path": str(path.relative_to(root)),
                            "dependency": f"source.{index}.url",
                            "message": issue,
                        }
                    )
            if source.get("verify_ssl") is False:
                issues.append(
                    {
                        "path": str(path.relative_to(root)),
                        "dependency": f"source.{index}.verify_ssl",
                        "message": "Pipfile source must verify TLS",
                    }
                )
    for section in ("packages", "dev-packages"):
        values = document.get(section, {})
        if isinstance(values, dict):
            for name, value in values.items():
                if isinstance(value, dict):
                    for key in ("git", "path", "file"):
                        if isinstance(value.get(key), str):
                            spec = value[key] if key == "git" else f"path:{value[key]}"
                            issue = _local_dependency_issue(spec, path, root)
                            if issue:
                                issues.append(
                                    {
                                        "path": str(path.relative_to(root)),
                                        "dependency": f"{section}.{name}.{key}",
                                        "message": issue,
                                    }
                                )
    return issues


def _scan_registry_config(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [{"path": str(path.relative_to(root)), "message": f"cannot read registry config: {exc}"}]
    issues: list[dict[str, str]] = []
    if re.search(r"(?i)\bhttp://", text):
        issues.append(
            {"path": str(path.relative_to(root)), "message": "insecure HTTP registry is not allowed"}
        )
    if re.search(r"(?i)(?:_auth|authToken|password|username|credential)\s*[:=]", text):
        issues.append(
            {"path": str(path.relative_to(root)), "message": "credential-bearing registry config is not allowed"}
        )
    if re.search(r"(?i)https?://[^\s/:]+:[^\s/@]+@", text):
        issues.append(
            {"path": str(path.relative_to(root)), "message": "credential-bearing registry URL is not allowed"}
        )
    return issues


def _scan_package_manager_hook_config(
    path: pathlib.Path, root: pathlib.Path, allow_build_hooks: bool
) -> list[dict[str, str]]:
    if allow_build_hooks:
        return []
    relative = str(path.relative_to(root))
    if path.name in {".pnpmfile.cjs", "pnpmfile.cjs"}:
        return [
            {
                "path": relative,
                "message": "pnpm hook files require current-prompt build-hook authorization",
            }
        ]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [{"path": relative, "message": f"cannot read package-manager config: {exc}"}]
    patterns = {
        "custom Yarn executables/plugins": r"(?im)^\s*(?:yarnPath|yarn-path|plugins)\s*[:=]",
        "custom npm script shell/loading": (
            r"(?im)^\s*(?:script-shell|shell|node-options|onload-script)\s*[:=]"
        ),
        "custom pnpm hook configuration": r"(?im)^\s*(?:pnpmfile|hooks)\s*[:=]",
    }
    return [
        {
            "path": relative,
            "message": f"{label} require current-prompt build-hook authorization",
        }
        for label, pattern in patterns.items()
        if re.search(pattern, text)
    ]


def _scan_dependency_lock(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [{"path": str(path.relative_to(root)), "message": f"cannot read lockfile: {exc}"}]
    issues: list[dict[str, str]] = []
    relative = str(path.relative_to(root))
    if re.search(r"(?i)(?:git\+|git://|ssh://|git@|github:|gitlab:)", text):
        issues.append({"path": relative, "message": "network VCS reference in lockfile is not allowed"})
    for url in re.findall(r"https?://[^\s\"'<>]+", text):
        issue = _local_dependency_issue(url.rstrip("\\,]}"), path, root)
        if issue:
            issues.append({"path": relative, "message": issue})
    if re.search(r"(?i)(?:_auth|authToken|password|credential)\s*[:=]\s*[^$\s]", text):
        issues.append({"path": relative, "message": "embedded lockfile credential is not allowed"})
    for local in re.findall(r"(?i)(?:file|link|portal|path):[^\s\"',}\]]+", text):
        issue = _local_dependency_issue(local, path, root)
        if issue:
            issues.append({"path": relative, "message": issue})
    local_value_pattern = re.compile(
        r'''(?ix)
        ["']?(?:directory|path|editable|resolved|file|url)["']?\s*[:=]\s*["']
        ((?:\.\.?[/\\]|[/\\]|~[/\\]|(?:file|link|portal|path):)[^"']+)
        ["']
        '''
    )
    for match in local_value_pattern.finditer(text):
        local = match.group(1)
        issue = _local_dependency_issue(local, path, root)
        if issue:
            issues.append({"path": relative, "message": issue})
    # Keep output deterministic and avoid duplicate messages from repeated URLs.
    unique = {tuple(sorted(issue.items())) for issue in issues}
    return [dict(item) for item in sorted(unique)]


def _scan_go_mod(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return [{"path": str(path.relative_to(root)), "message": f"cannot read go.mod: {exc}"}]
    issues: list[dict[str, str]] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if "=>" not in line:
            continue
        replacement = line.split("=>", 1)[1].strip().split()[0]
        if replacement.startswith(("./", "../", "/")):
            issue = _local_dependency_issue(f"path:{replacement}", path, root)
            if issue:
                issues.append(
                    {"path": str(path.relative_to(root)), "line": str(number), "message": issue}
                )
    return issues


def _scan_go_work(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return [{"path": str(path.relative_to(root)), "message": f"cannot read go.work: {exc}"}]
    issues: list[dict[str, str]] = []
    in_use_block = False
    for number, raw in enumerate(lines, start=1):
        line = raw.split("//", 1)[0].strip()
        if line == "use (":
            in_use_block = True
            continue
        if in_use_block and line == ")":
            in_use_block = False
            continue
        candidate = None
        if in_use_block and line:
            candidate = line.split()[0]
        elif line.startswith("use "):
            candidate = line[4:].strip().split()[0]
        if candidate and candidate.startswith((".", "/")):
            issue = _local_dependency_issue(f"path:{candidate}", path, root)
            if issue:
                issues.append(
                    {"path": str(path.relative_to(root)), "line": str(number), "message": issue}
                )
    return issues


def validate_dependencies(
    root: pathlib.Path, allow_build_hooks: bool = False
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    issues: list[dict[str, str]] = []
    for path in _iter_scan_files(root, {"package.json"}):
        issues.extend(_scan_package_json(path, root))
    for path in _iter_scan_files(
        root,
        {"requirements.txt", "requirements-dev.txt", "requirements-test.txt", "constraints.txt"},
    ):
        issues.extend(_scan_requirements(path, root))
    for path in _iter_scan_files(root, {"pyproject.toml"}):
        issues.extend(_scan_pyproject(path, root))
    for path in _iter_scan_files(root, {"Pipfile"}):
        issues.extend(_scan_pipfile(path, root))
    config_names = {
        ".npmrc",
        ".yarnrc",
        ".yarnrc.yml",
        "pip.conf",
        "poetry.toml",
        "pnpm-workspace.yaml",
        ".pnpmfile.cjs",
        "pnpmfile.cjs",
    }
    for path in _iter_scan_files(root, config_names):
        issues.extend(_scan_registry_config(path, root))
        issues.extend(_scan_package_manager_hook_config(path, root, allow_build_hooks))
    for path in _iter_scan_files(root, {"go.mod"}):
        issues.extend(_scan_go_mod(path, root))
    for path in _iter_scan_files(root, {"go.work"}):
        issues.extend(_scan_go_work(path, root))
    lock_names = {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
    }
    for path in _iter_scan_files(root, lock_names):
        issues.extend(_scan_dependency_lock(path, root))
    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "status": "blocked" if issues else "valid",
        "issues": issues,
    }


def command_validate_dependencies(args: argparse.Namespace) -> int:
    result = validate_dependencies(pathlib.Path(args.root), args.allow_build_hooks)
    if args.output:
        atomic_write_json(pathlib.Path(args.output), result)
    print(json.dumps(result, sort_keys=True))
    return 2 if result["issues"] else 0


def _requirements_are_hashed(path: pathlib.Path) -> bool:
    entries: list[str] = []
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not current and line.startswith("--"):
            continue
        current = f"{current} {line}".strip()
        if line.endswith("\\"):
            continue
        entries.append(current)
        current = ""
    if current:
        entries.append(current)
    return bool(entries) and all("==" in line and "--hash=" in line for line in entries)


def _python_test_extras(root: pathlib.Path) -> tuple[str, ...]:
    """Select only the narrow, conventional PEP 621 test extra when declared."""

    path = root / "pyproject.toml"
    if not path.is_file():
        return ()
    try:
        with path.open("rb") as handle:
            pyproject = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CriticError(f"invalid pyproject.toml: {exc}") from exc
    project = pyproject.get("project", {})
    if not isinstance(project, dict):
        return ()
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict) or "test" not in optional:
        return ()
    dependencies = optional["test"]
    if not isinstance(dependencies, list) or not all(
        isinstance(dependency, str) for dependency in dependencies
    ):
        raise CriticError("project.optional-dependencies.test must be a string array")
    return ("test",) if dependencies else ()


def _python_restore_plan(root: pathlib.Path, allow_unlocked: bool, hooks: bool) -> dict[str, Any]:
    environment = {
        "PIP_ONLY_BINARY": "" if hooks else ":all:",
        "POETRY_INSTALLER_ONLY_BINARY": "" if hooks else ":all:",
    }
    no_build = [] if hooks else ["--no-build"]
    reset_venv = list(PYTHON_VENV_RESET_COMMAND)
    test_extras = _python_test_extras(root)
    uv_test_extra_args = [item for extra in test_extras for item in ("--extra", extra)]
    poetry_test_extra_args = [item for extra in test_extras for item in ("--extras", extra)]
    if (root / "uv.lock").is_file():
        return {
            "manager": "uv",
            "manager_version": "0.12.9",
            "mode": "locked",
            "lockfile": "uv.lock",
            "commands": [
                reset_venv,
                [
                    "uv",
                    "sync",
                    "--locked",
                    "--no-install-project",
                    *uv_test_extra_args,
                    *no_build,
                ]
            ],
            "environment": environment,
        }
    if (root / "poetry.lock").is_file():
        return {
            "manager": "poetry",
            "manager_version": "2.4.2",
            "mode": "locked",
            "lockfile": "poetry.lock",
            "commands": [
                reset_venv,
                [
                    "poetry",
                    "install",
                    "--no-root",
                    "--no-interaction",
                    "--no-ansi",
                    *poetry_test_extra_args,
                ]
            ],
            "environment": environment,
        }
    if (root / "Pipfile.lock").is_file():
        if test_extras:
            raise CriticError(
                "Pipfile.lock and a separate project test extra require a unified dependency lock",
                kind="blocked_dependency_restore",
            )
        return {
            "manager": "pipenv",
            "manager_version": "2026.8.0",
            "mode": "locked",
            "lockfile": "Pipfile.lock",
            "commands": [
                reset_venv,
                ["pipenv", "sync", "--dev"],
                ["pipenv", "requirements", "--dev"],
            ],
            "environment": {**environment, "PIPENV_VENV_IN_PROJECT": "1"},
        }
    requirements = root / "requirements.txt"
    requirements_are_hashed = requirements.is_file() and _requirements_are_hashed(requirements)
    if requirements_are_hashed and not test_extras:
        command = [
            "uv",
            "pip",
            "install",
            "--python",
            ".venv/bin/python",
            "--require-hashes",
            "--only-binary=:all:",
            "--requirement",
            "requirements.txt",
        ]
        return {
            "manager": "pip",
            "manager_version": "bundled-with-python-3.12.14",
            "mode": "locked",
            "lockfile": "requirements.txt",
            "commands": [
                reset_venv,
                command,
                ["uv", "pip", "freeze", "--python", ".venv/bin/python"],
            ],
            "environment": environment,
        }
    if requirements_are_hashed and test_extras:
        raise CriticError(
            "hashed requirements and a separate project test extra cannot be combined without an explicit unified lock",
            kind="blocked_dependency_restore",
        )
    if not any(
        (root / name).is_file()
        for name in ("pyproject.toml", "requirements.txt", "Pipfile", "Pipfile.lock")
    ):
        return {
            "manager": "python-venv",
            "manager_version": "3.12.14",
            "mode": "locked",
            "lockfile": "dependency-free",
            "commands": [reset_venv],
            "environment": environment,
        }
    if not allow_unlocked:
        raise CriticError("Python project has no supported locked dependency input", kind="blocked_dependency_restore")
    if (root / "Pipfile").is_file():
        if test_extras:
            raise CriticError(
                "Pipfile and a separate project test extra require a unified dependency lock",
                kind="blocked_dependency_restore",
            )
        return {
            "manager": "pipenv",
            "manager_version": "2026.8.0",
            "mode": "resolved_unlocked",
            "lockfile": "Pipfile.lock (generated in scratch)",
            "commands": [
                reset_venv,
                ["pipenv", "lock"],
                ["pipenv", "sync", "--dev"],
                ["pipenv", "requirements", "--dev"],
            ],
            "environment": {**environment, "PIPENV_VENV_IN_PROJECT": "1"},
        }
    if (root / "pyproject.toml").is_file():
        try:
            with (root / "pyproject.toml").open("rb") as handle:
                pyproject = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CriticError(f"invalid pyproject.toml: {exc}") from exc
        tool = pyproject.get("tool", {})
        if isinstance(tool, dict) and isinstance(tool.get("poetry"), dict):
            return {
                "manager": "poetry",
                "manager_version": "2.4.2",
                "mode": "resolved_unlocked",
                "lockfile": "poetry.lock (generated in scratch)",
                "commands": [
                    reset_venv,
                    ["poetry", "lock"],
                    [
                        "poetry",
                        "install",
                        "--no-root",
                        "--no-interaction",
                        "--no-ansi",
                        *poetry_test_extra_args,
                    ],
                    ["poetry", "show", "--tree"],
                ],
                "environment": environment,
            }
    if (root / "pyproject.toml").is_file():
        return {
            "manager": "uv",
            "manager_version": "0.12.9",
            "mode": "resolved_unlocked",
            "lockfile": "uv.lock (generated in scratch)",
            "commands": [
                reset_venv,
                ["uv", "lock", *no_build],
                [
                    "uv",
                    "sync",
                    "--locked",
                    "--no-install-project",
                    *uv_test_extra_args,
                    *no_build,
                ],
                ["uv", "pip", "freeze"],
            ],
            "environment": environment,
        }
    if requirements.is_file():
        compile_command = [
            "uv",
            "pip",
            "compile",
            "requirements.txt",
            "--python-version",
            "3.12",
            "--generate-hashes",
            "--output-file",
            ".repository-critic.requirements.lock",
            *no_build,
        ]
        install_command = [
            "uv",
            "pip",
            "install",
            "--python",
            ".venv/bin/python",
            "--require-hashes",
            "--requirement",
            ".repository-critic.requirements.lock",
            *no_build,
        ]
        return {
            "manager": "pip",
            "manager_version": "bundled-with-python-3.12.14",
            "mode": "resolved_unlocked",
            "lockfile": ".repository-critic.requirements.lock (generated in scratch)",
            "commands": [
                reset_venv,
                compile_command,
                install_command,
                ["uv", "pip", "freeze", "--python", ".venv/bin/python"],
            ],
            "environment": environment,
        }
    return {
        "manager": "python-venv",
        "manager_version": "3.12.14",
        "mode": "locked",
        "lockfile": "dependency-free",
        "commands": [reset_venv],
        "environment": environment,
    }


def _package_manager(root: pathlib.Path) -> tuple[str, str]:
    package_path = root / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CriticError(f"invalid package.json: {exc}") from exc
    declared = package.get("packageManager")
    if declared is None:
        if (root / "package-lock.json").is_file() or (root / "npm-shrinkwrap.json").is_file():
            return "npm", "10.9.3"
        if (root / "pnpm-lock.yaml").is_file() or (root / "yarn.lock").is_file():
            raise CriticError(
                "pnpm and Yarn lockfiles require an exact packageManager declaration",
                kind="unsupported_toolchain",
            )
        return "npm", "10.9.3"
    if not isinstance(declared, str) or not re.fullmatch(r"(?:npm|pnpm|yarn)@[0-9][0-9A-Za-z.+-]*", declared):
        raise CriticError("packageManager must be an exact npm, pnpm, or yarn version")
    manager, version = declared.split("@", 1)
    if version not in SUPPORTED_PACKAGE_MANAGERS[manager]:
        raise CriticError(
            f"package manager is not preloaded: {manager}@{version}",
            kind="unsupported_toolchain",
        )
    return manager, version


def _javascript_restore_plan(root: pathlib.Path, allow_unlocked: bool, hooks: bool) -> dict[str, Any]:
    manager, version = _package_manager(root)
    ignore_scripts = [] if hooks else ["--ignore-scripts"]
    if manager == "npm":
        lock = "npm-shrinkwrap.json" if (root / "npm-shrinkwrap.json").is_file() else "package-lock.json"
        if (root / lock).is_file():
            commands = [["npm", "ci", *ignore_scripts, "--no-audit", "--no-fund"]]
            mode = "locked"
        elif allow_unlocked:
            commands = [
                ["npm", "install", "--package-lock-only", *ignore_scripts, "--no-audit", "--no-fund"],
                ["npm", "ci", *ignore_scripts, "--no-audit", "--no-fund"],
                ["npm", "ls", "--all", "--json"],
            ]
            lock = "package-lock.json (generated in scratch)"
            mode = "resolved_unlocked"
        else:
            raise CriticError("npm project has no lockfile", kind="blocked_dependency_restore")
    elif manager == "pnpm":
        lock = "pnpm-lock.yaml"
        if (root / lock).is_file():
            commands = [["pnpm", "install", "--frozen-lockfile", *ignore_scripts]]
            mode = "locked"
        elif allow_unlocked:
            commands = [
                ["pnpm", "install", "--lockfile-only", *ignore_scripts],
                ["pnpm", "install", "--frozen-lockfile", *ignore_scripts],
                ["pnpm", "list", "--depth", "Infinity", "--json"],
            ]
            lock = "pnpm-lock.yaml (generated in scratch)"
            mode = "resolved_unlocked"
        else:
            raise CriticError("pnpm project has no lockfile", kind="blocked_dependency_restore")
    else:
        lock = "yarn.lock"
        is_classic = version == "1.22.22"
        immutable = ["--frozen-lockfile"] if is_classic else ["--immutable"]
        skip_builds = [] if hooks or is_classic else ["--mode=skip-build"]
        yarn_ignore_scripts = ignore_scripts if is_classic else []
        inventory = (
            ["yarn", "list", "--json"]
            if is_classic
            else ["yarn", "info", "-A", "-R", "--json"]
        )
        if (root / lock).is_file():
            commands = [["yarn", "install", *immutable, *yarn_ignore_scripts, *skip_builds]]
            mode = "locked"
        elif allow_unlocked:
            commands = [
                ["yarn", "install", *yarn_ignore_scripts, *skip_builds],
                inventory,
            ]
            lock = "yarn.lock (generated in scratch)"
            mode = "resolved_unlocked"
        else:
            raise CriticError("Yarn project has no lockfile", kind="blocked_dependency_restore")
    return {
        "manager": manager,
        "manager_version": version,
        "mode": mode,
        "lockfile": lock,
        "commands": commands,
        "environment": {
            "NPM_CONFIG_IGNORE_SCRIPTS": "false" if hooks else "true",
            "YARN_ENABLE_SCRIPTS": "true" if hooks else "false",
        },
    }


def _go_restore_plan(root: pathlib.Path, allow_unlocked: bool) -> dict[str, Any]:
    go_mod = root / "go.mod"
    if not go_mod.is_file():
        raise CriticError("no go.mod in project root", kind="unsupported_toolchain")
    text = go_mod.read_text(encoding="utf-8")
    go_match = re.search(r"(?m)^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", text)
    toolchain_match = re.search(r"(?m)^toolchain\s+go(\d+\.\d+(?:\.\d+)?)\s*$", text)
    required = toolchain_match.group(1) if toolchain_match else (go_match.group(1) if go_match else None)
    required_parts = tuple(int(part) for part in required.split(".")) if required else ()
    required_parts = (*required_parts, *(0 for _ in range(3 - len(required_parts))))
    if required and required_parts > (1, 27, 1):
        raise CriticError(
            f"Go toolchain {required} is newer than the baked Go 1.27.1",
            kind="unsupported_toolchain",
        )
    if (root / "vendor").is_dir():
        return {
            "manager": "go",
            "manager_version": "1.27.1",
            "mode": "locked",
            "lockfile": "vendor/ with go.mod/go.sum",
            "commands": [["go", "list", "-mod=vendor", "-m", "all"]],
            "environment": {"GOTOOLCHAIN": "local"},
        }
    if (root / "go.sum").is_file():
        return {
            "manager": "go",
            "manager_version": "1.27.1",
            "mode": "locked",
            "lockfile": "go.sum",
            "commands": [["go", "mod", "download"], ["go", "mod", "verify"]],
            "environment": {"GOTOOLCHAIN": "local"},
        }
    if not allow_unlocked:
        raise CriticError("Go project has no go.sum or vendor directory", kind="blocked_dependency_restore")
    return {
        "manager": "go",
        "manager_version": "1.27.1",
        "mode": "resolved_unlocked",
        "lockfile": "go.sum (generated in scratch)",
        "commands": [["go", "mod", "download"], ["go", "list", "-m", "-json", "all"]],
        "environment": {"GOTOOLCHAIN": "local"},
    }


def command_restore_plan(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.root).resolve(strict=True)
    validation = validate_dependencies(root, args.allow_build_hooks)
    if validation["issues"]:
        raise CriticError("dependency references failed validation", kind="blocked_dependency_restore")
    ecosystem = args.ecosystem
    if ecosystem == "auto":
        detected = []
        if any((root / name).is_file() for name in ("pyproject.toml", "requirements.txt", "Pipfile")):
            detected.append("python")
        if (root / "package.json").is_file():
            detected.append("javascript-typescript")
        if (root / "go.mod").is_file():
            detected.append("go")
        if len(detected) != 1:
            raise CriticError(
                f"ecosystem auto-detection requires exactly one match; found: {', '.join(detected) or 'none'}"
            )
        ecosystem = detected[0]
    if ecosystem == "python":
        plan = _python_restore_plan(root, args.allow_unlocked, args.allow_build_hooks)
    elif ecosystem == "javascript-typescript":
        plan = _javascript_restore_plan(root, args.allow_unlocked, args.allow_build_hooks)
    elif ecosystem == "go":
        plan = _go_restore_plan(root, args.allow_unlocked)
    else:
        raise CriticError(f"unsupported ecosystem: {ecosystem}", kind="unsupported_toolchain")
    manifest_candidates = {
        "python": ("pyproject.toml", "requirements.txt", "Pipfile"),
        "javascript-typescript": ("package.json",),
        "go": ("go.mod",),
    }[ecosystem]
    source_manifest = next((root / name for name in manifest_candidates if (root / name).is_file()), None)
    manifest_digest = None
    if source_manifest:
        manifest_digest = hashlib.sha256(source_manifest.read_bytes()).hexdigest()
    result = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "ecosystem": ecosystem,
        "build_hooks_enabled": bool(args.allow_build_hooks),
        "dependency_manifest_sha256": manifest_digest,
        **plan,
    }
    if args.output:
        atomic_write_json(pathlib.Path(args.output), result)
    print(json.dumps(result, sort_keys=True))
    return 0


def _test_argv(args: argparse.Namespace, default: list[str]) -> list[str]:
    if not args.test_argv_json:
        return default
    try:
        value = json.loads(args.test_argv_json)
    except json.JSONDecodeError as exc:
        raise CriticError(f"invalid --test-argv-json: {exc}") from exc
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise CriticError("--test-argv-json must be a non-empty JSON string array")
    return value


def _instrument_go_test_argv(argv: Sequence[str], profile: pathlib.Path) -> list[str]:
    if list(argv[:2]) != ["go", "test"]:
        raise CriticError(
            "generic Go coverage accepts only a go test argv; use a documented coverage command otherwise"
        )
    replaced_flags = {"-count", "-covermode", "-coverpkg", "-coverprofile", "-json"}
    retained: list[str] = []
    index = 2
    while index < len(argv):
        item = argv[index]
        flag = item.split("=", 1)[0]
        if flag in replaced_flags:
            if "=" not in item and flag != "-json":
                if index + 1 >= len(argv):
                    raise CriticError(f"Go test flag {flag} is missing its value")
                index += 2
            else:
                index += 1
            continue
        if item in {"-c", "-i"}:
            raise CriticError(
                "generic Go coverage cannot instrument compile-only go test commands"
            )
        retained.append(item)
        index += 1
    return [
        "go",
        "test",
        "-count=1",
        "-covermode=atomic",
        "-coverpkg=./...",
        f"-coverprofile={profile}",
        "-json",
        *retained,
    ]


def command_coverage_plan(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.root).resolve(strict=True)
    scratch_root = pathlib.Path(args.scratch_root).resolve(strict=True)
    ensure_within(root, scratch_root, "coverage project root")
    coverage_dir = pathlib.Path(args.coverage_dir).resolve(strict=False)
    ensure_within(coverage_dir, scratch_root, "coverage output directory")
    coverage_dir.mkdir(parents=True, exist_ok=True)
    if args.ecosystem == "python":
        interpreter = root / ".venv" / "bin" / "python"
        if not interpreter.is_file():
            raise CriticError(
                "Python project environment .venv is absent; execute the restore plan first",
                kind="blocked_dependency_restore",
            )
        data_file = coverage_dir / ".coverage"
        json_file = coverage_dir / "coverage.json"
        xml_file = coverage_dir / "coverage.xml"
        supplied_tests = _test_argv(args, ["pytest"])
        executable = pathlib.Path(supplied_tests[0]).name
        if executable.startswith("python") and supplied_tests[1:2] == ["-m"] and len(supplied_tests) >= 3:
            coverage_target = supplied_tests[1:]
        elif executable in {"pytest", "py.test"}:
            coverage_target = ["-m", "pytest", *supplied_tests[1:]]
        else:
            raise CriticError(
                "generic Python coverage accepts only python -m MODULE or pytest argv; use the repository's documented coverage command otherwise"
            )
        commands = [
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(interpreter),
                "--offline",
                "--no-index",
                "--find-links",
                "/opt/remoteagent/agent/python-wheelhouse",
                "--require-hashes",
                "--requirement",
                "/opt/remoteagent/agent/coverage-tools.lock",
            ],
            [str(interpreter), "-m", "coverage", "erase", f"--data-file={data_file}"],
            [
                str(interpreter),
                "-m",
                "coverage",
                "run",
                "--branch",
                "--source=.",
                f"--data-file={data_file}",
                *coverage_target,
            ],
            [str(interpreter), "-m", "coverage", "json", f"--data-file={data_file}", "-o", str(json_file)],
            [str(interpreter), "-m", "coverage", "xml", f"--data-file={data_file}", "-o", str(xml_file)],
        ]
        native = [str(json_file), str(xml_file)]
        normalizer = {"format": "coveragepy", "input": str(json_file)}
    elif args.ecosystem == "javascript-typescript":
        manager, version = _package_manager(root)
        tests = _test_argv(args, [manager, "test"])
        json_file = coverage_dir / "coverage-final.json"
        commands = [
            [
                "c8",
                "--all",
                "--reporter=json",
                "--reporter=cobertura",
                "--reports-dir",
                str(coverage_dir),
                *tests,
            ]
        ]
        native = [str(json_file), str(coverage_dir / "cobertura-coverage.xml")]
        normalizer = {"format": "istanbul", "input": str(json_file)}
        normalizer["package_manager"] = f"{manager}@{version}"
    elif args.ecosystem == "go":
        profile = coverage_dir / "coverage.out"
        requested_tests = _test_argv(
            args,
            [
                "go",
                "test",
                "-count=1",
                "-covermode=atomic",
                "-coverpkg=./...",
                f"-coverprofile={profile}",
                "-json",
                "./...",
            ],
        )
        tests = _instrument_go_test_argv(requested_tests, profile)
        commands = [tests, ["go", "tool", "cover", f"-func={profile}"]]
        native = [str(profile)]
        normalizer = {"format": "go", "input": str(profile)}
    else:
        raise CriticError(f"unsupported ecosystem: {args.ecosystem}", kind="unsupported_toolchain")
    result = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "ecosystem": args.ecosystem,
        "commands": commands,
        "normalizer": normalizer,
        "native_artifacts": native,
    }
    if args.output:
        atomic_write_json(pathlib.Path(args.output), result)
    print(json.dumps(result, sort_keys=True))
    return 0


def _bounded_remove_tree(
    parent: pathlib.Path, directory_name: str, timeout: int
) -> tuple[bool, int, str | None]:
    """Remove an exact child using no-follow, directory-relative operations."""

    deadline = time.monotonic() + min(max(1, timeout), 30)
    removed_entries = 0
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | no_follow

    def remove_contents(directory_fd: int) -> tuple[bool, str | None]:
        nonlocal removed_entries
        if time.monotonic() >= deadline:
            return False, "cleanup exceeded its 30-second bound"
        try:
            os.fchmod(directory_fd, os.fstat(directory_fd).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
        for name in sorted(os.listdir(directory_fd)):
            if time.monotonic() >= deadline:
                return False, "cleanup exceeded its 30-second bound"
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                try:
                    complete, error = remove_contents(child_fd)
                finally:
                    os.close(child_fd)
                if not complete:
                    return False, error
                os.rmdir(name, dir_fd=directory_fd)
            else:
                os.unlink(name, dir_fd=directory_fd)
            removed_entries += 1
        return True, None

    try:
        parent_fd = os.open(parent, directory_flags)
        try:
            root_fd = os.open(directory_name, directory_flags, dir_fd=parent_fd)
            try:
                complete, error = remove_contents(root_fd)
            finally:
                os.close(root_fd)
            if not complete:
                return False, removed_entries, error
            os.rmdir(directory_name, dir_fd=parent_fd)
            return True, removed_entries + 1, None
        finally:
            os.close(parent_fd)
    except OSError as exc:
        return False, removed_entries, redact_text(str(exc))


def _cleanup_prepared_job(
    prepare: Mapping[str, Any], workspace: pathlib.Path, job_id: str, timeout: int
) -> dict[str, Any]:
    record_job_id = str(prepare.get("job_id", ""))
    if not JOB_ID_RE.fullmatch(record_job_id):
        raise CriticError("prepare record has an invalid job id")
    if record_job_id != job_id:
        raise CriticError("prepare record job id does not match the trusted current job id")
    scratch_parent = workspace / ".repository-critic"
    if scratch_parent.is_symlink() or not scratch_parent.is_dir():
        raise CriticError("scratch parent must be a real directory beneath the workspace")
    expected = scratch_parent / job_id
    if expected.is_symlink() or not expected.is_dir():
        raise CriticError("job scratch must be a real directory")
    scratch_root = pathlib.Path(str(prepare.get("scratch_root", "")))
    if scratch_root.is_symlink() or scratch_root.resolve(strict=True) != expected.resolve(strict=True):
        raise CriticError("prepare record scratch root is not the exact job-specific directory")
    companion_name = str(prepare.get("companion_name", ""))
    if not COMPANION_NAME_RE.fullmatch(companion_name):
        raise CriticError("prepare record companion name is invalid")
    candidates = list_companions(workspace)
    source = candidates.get(companion_name)
    if source is None or source != pathlib.Path(str(prepare.get("source_path", ""))).resolve(strict=True):
        raise CriticError("prepare record source no longer matches the selected workspace companion")
    source_digest = snapshot_sha256(source)
    source_unchanged = source_digest == prepare.get("source_snapshot_sha256")
    bytes_before = directory_size(scratch_root)
    started = time.monotonic()
    removed, entries, error = _bounded_remove_tree(scratch_parent, job_id, timeout)
    result = {
        "schema_version": SCHEMA_VERSION,
        "job_id": record_job_id,
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started, 6),
        "source_unchanged": source_unchanged,
        "source_snapshot_sha256": source_digest,
        "scratch_removed": removed,
        "scratch_bytes_before": bytes_before,
        "entries_removed": entries,
        "status": "complete" if removed and source_unchanged else "partial",
        "error": error,
    }
    return result


def command_cleanup(args: argparse.Namespace) -> int:
    workspace = pathlib.Path(args.workspace).resolve(strict=True)
    if not JOB_ID_RE.fullmatch(args.job_id):
        raise CriticError("trusted current job id is invalid")
    supplied_prepare_path = pathlib.Path(args.prepare_record)
    prepare_path = supplied_prepare_path.resolve(strict=True)
    expected_prepare = workspace / ".repository-critic" / args.job_id / "prepare.json"
    if prepare_path != expected_prepare.resolve(strict=True) or supplied_prepare_path.is_symlink():
        raise CriticError("prepare record is not the exact current job record")
    prepare = read_json_bounded(prepare_path, PREPARE_RECORD_LIMIT_BYTES, "prepare record")
    if prepare.get("job_id") != args.job_id or not JOB_ID_RE.fullmatch(args.job_id):
        raise CriticError("prepare record job id does not match the trusted current job id")
    result = _cleanup_prepared_job(prepare, workspace, args.job_id, args.timeout)
    artifacts = pathlib.Path(args.artifacts).resolve(strict=True)
    artifact_root = artifacts / f"repository-review-{args.job_id}"
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise CriticError("artifact root must be the exact real job artifact directory")
    expected_recorded_artifacts = pathlib.Path(str(prepare.get("artifact_root", ""))).resolve(strict=True)
    if artifact_root.resolve(strict=True) != expected_recorded_artifacts:
        raise CriticError("prepare record artifact root does not match the trusted artifact root")
    run_manifest_path = artifact_root / "run-manifest.json"
    run_manifest = read_json_bounded(
        run_manifest_path, PRIMARY_ARTIFACT_LIMIT_BYTES, "run manifest"
    )
    if not isinstance(run_manifest, dict) or run_manifest.get("job_id") != args.job_id:
        raise CriticError("run-manifest.json does not match the current job")
    run_manifest["cleanup"] = result
    run_manifest["finished_at"] = result["finished_at"]
    atomic_write_json(run_manifest_path, run_manifest)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "complete" else 2


def metric(covered: int, total: int) -> dict[str, int | float | None]:
    return {
        "covered": covered,
        "total": total,
        "percent": round(100.0 * covered / total, 4) if total else None,
    }


def _display_path(value: str, root: pathlib.Path) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise CriticError("coverage source path must be a non-empty POSIX path")
    candidate = pathlib.Path(value)
    if candidate.is_absolute():
        try:
            return candidate.resolve(strict=False).relative_to(root).as_posix()
        except ValueError as exc:
            raise CriticError(f"coverage source path is outside the reviewed project: {value}") from exc
    pure = pathlib.PurePosixPath(value)
    if ".." in pure.parts:
        raise CriticError(f"coverage source path escapes the reviewed project: {value}")
    return pure.as_posix()


def _coverage_count(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CriticError(f"coverage.py {label} must be a non-negative integer")
    return value


def _native_count(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CriticError(f"{label} must be a non-negative integer")
    return value


def _coveragepy(input_path: pathlib.Path, root: pathlib.Path) -> dict[str, Any]:
    document = read_json(input_path)
    if not isinstance(document, dict):
        raise CriticError("coverage.py JSON root must be an object")
    totals = document.get("totals", {})
    if not isinstance(totals, dict):
        raise CriticError("coverage.py totals must be an object")
    line_covered = _coverage_count(totals.get("covered_lines", 0), "covered_lines")
    line_total = _coverage_count(totals.get("num_statements", 0), "num_statements")
    if line_covered > line_total:
        raise CriticError("coverage.py covered lines exceed total statements")
    branch_total = totals.get("num_branches")
    branches = None
    if branch_total is not None:
        branch_total = _coverage_count(branch_total, "num_branches")
        branch_covered = _coverage_count(
            totals.get("covered_branches", 0), "covered_branches"
        )
        if branch_covered > branch_total:
            raise CriticError("coverage.py covered branches exceed total branches")
        branches = metric(branch_covered, branch_total)
    files_present = "files" in document
    files = document.get("files", {})
    if not isinstance(files, dict):
        raise CriticError("coverage.py files must be an object")
    zero_files = []
    file_line_covered = file_line_total = 0
    file_branch_covered = file_branch_total = 0
    saw_branch_summary = False
    for filename, value in files.items():
        if not isinstance(filename, str) or not isinstance(value, dict):
            raise CriticError("coverage.py file entries must map paths to objects")
        summary = value.get("summary", {})
        if not isinstance(summary, dict):
            raise CriticError("coverage.py file summary must be an object")
        covered_here = _coverage_count(
            summary.get("covered_lines", 0), "file covered_lines"
        )
        total_here = _coverage_count(
            summary.get("num_statements", 0), "file num_statements"
        )
        if covered_here > total_here:
            raise CriticError("coverage.py file covered lines exceed its statements")
        file_line_covered += covered_here
        file_line_total += total_here
        if "num_branches" in summary or "covered_branches" in summary:
            saw_branch_summary = True
            file_branches = _coverage_count(
                summary.get("num_branches", 0), "file num_branches"
            )
            file_covered_branches = _coverage_count(
                summary.get("covered_branches", 0), "file covered_branches"
            )
            if file_covered_branches > file_branches:
                raise CriticError("coverage.py file covered branches exceed its branches")
            file_branch_total += file_branches
            file_branch_covered += file_covered_branches
        display_path = _display_path(filename, root)
        if total_here and not covered_here:
            zero_files.append(display_path)
    if files_present and (file_line_covered, file_line_total) != (line_covered, line_total):
        raise CriticError("coverage.py totals disagree with per-file line summaries")
    if files_present and saw_branch_summary and (
        branches is None
        or (file_branch_covered, file_branch_total)
        != (branches["covered"], branches["total"])
    ):
        raise CriticError("coverage.py totals disagree with per-file branch summaries")
    return {
        "lines": metric(line_covered, line_total),
        "branches": branches,
        "functions": None,
        "metric_basis": "executable_lines",
        "zero_coverage_files": sorted(set(zero_files)),
    }


def _istanbul(input_path: pathlib.Path, root: pathlib.Path) -> dict[str, Any]:
    document = read_json(input_path)
    if not isinstance(document, dict):
        raise CriticError("Istanbul JSON root must be an object")
    line_covered = line_total = 0
    branch_covered = branch_total = 0
    function_covered = function_total = 0
    zero_files: list[str] = []
    for filename, value in document.items():
        if not isinstance(filename, str) or not isinstance(value, dict):
            raise CriticError("Istanbul file entries must map paths to objects")
        statements = value.get("s", {})
        statement_map = value.get("statementMap", {})
        if not isinstance(statements, dict) or not isinstance(statement_map, dict):
            raise CriticError("Istanbul statement maps and counts must be objects")
        if set(statements) != set(statement_map):
            raise CriticError("Istanbul statement counts disagree with statementMap entries")
        line_hits: dict[int, int] = {}
        for statement_id, count in statements.items():
            count = _native_count(count, "Istanbul statement count")
            location = statement_map.get(statement_id)
            if not isinstance(location, dict) or not isinstance(location.get("start"), dict):
                raise CriticError("Istanbul statement location is invalid")
            line = location["start"].get("line")
            if not isinstance(line, int) or isinstance(line, bool) or line < 1:
                raise CriticError("Istanbul statement line must be a positive integer")
            line_hits[line] = max(line_hits.get(line, 0), count)
        covered_here = sum(1 for count in line_hits.values() if count > 0)
        total_here = len(line_hits)
        line_covered += covered_here
        line_total += total_here
        display_path = _display_path(filename, root)
        if total_here and not covered_here:
            zero_files.append(display_path)
        functions = value.get("f", {})
        if not isinstance(functions, dict):
            raise CriticError("Istanbul function counts must be an object")
        function_counts = [
            _native_count(count, "Istanbul function count") for count in functions.values()
        ]
        function_total += len(function_counts)
        function_covered += sum(1 for count in function_counts if count > 0)
        branches = value.get("b", {})
        if not isinstance(branches, dict):
            raise CriticError("Istanbul branch counts must be an object")
        for counts in branches.values():
            if not isinstance(counts, list):
                raise CriticError("Istanbul branch record must be an array")
            parsed_counts = [
                _native_count(count, "Istanbul branch count") for count in counts
            ]
            branch_total += len(parsed_counts)
            branch_covered += sum(1 for count in parsed_counts if count > 0)
    return {
        "lines": metric(line_covered, line_total),
        "branches": metric(branch_covered, branch_total),
        "functions": metric(function_covered, function_total),
        "metric_basis": "instrumented_lines",
        "zero_coverage_files": sorted(set(zero_files)),
    }


def _lcov(input_path: pathlib.Path, root: pathlib.Path) -> dict[str, Any]:
    totals = {"LF": 0, "LH": 0, "BRF": 0, "BRH": 0, "FNF": 0, "FNH": 0}
    current_file: str | None = None
    current_lf = current_lh = 0
    zero_files: list[str] = []

    def complete_file() -> None:
        nonlocal current_file, current_lf, current_lh
        if current_file and current_lf and not current_lh:
            zero_files.append(_display_path(current_file, root))
        current_file = None
        current_lf = current_lh = 0

    for raw in input_path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not raw:
            continue
        if raw.startswith("SF:"):
            complete_file()
            current_file = raw[3:]
            if not current_file:
                raise CriticError("LCOV SF record has an empty source path")
            _display_path(current_file, root)
        elif raw == "end_of_record":
            complete_file()
        elif raw.startswith(("TN:", "VER:")):
            continue
        elif raw.startswith("FN:"):
            match = re.fullmatch(r"FN:(\d+),(.+)", raw)
            if not match or int(match.group(1)) < 1:
                raise CriticError("LCOV FN record is malformed")
        elif raw.startswith("FNDA:"):
            match = re.fullmatch(r"FNDA:(\d+),(.+)", raw)
            if not match:
                raise CriticError("LCOV FNDA record is malformed")
        elif raw.startswith("DA:"):
            match = re.fullmatch(r"DA:(\d+),(\d+)(?:,[^,\s]+)?", raw)
            if not match or int(match.group(1)) < 1:
                raise CriticError("LCOV DA record is malformed")
        elif raw.startswith("BRDA:"):
            match = re.fullmatch(r"BRDA:(\d+),(\d+),(\d+),(-|\d+)", raw)
            if not match or int(match.group(1)) < 1:
                raise CriticError("LCOV BRDA record is malformed")
        elif ":" in raw:
            key, value = raw.split(":", 1)
            if key in totals:
                if not re.fullmatch(r"\d+", value):
                    raise CriticError(f"LCOV {key} record is malformed")
                parsed = int(value)
                totals[key] += parsed
                if key == "LF":
                    current_lf = parsed
                elif key == "LH":
                    current_lh = parsed
            else:
                raise CriticError(f"unsupported LCOV record: {key}")
        else:
            raise CriticError("malformed LCOV record")
    complete_file()
    for covered_key, total_key in (("LH", "LF"), ("BRH", "BRF"), ("FNH", "FNF")):
        if totals[covered_key] > totals[total_key]:
            raise CriticError(f"LCOV {covered_key} exceeds {total_key}")
    return {
        "lines": metric(totals["LH"], totals["LF"]),
        "branches": metric(totals["BRH"], totals["BRF"]),
        "functions": metric(totals["FNH"], totals["FNF"]),
        "metric_basis": "executable_lines",
        "zero_coverage_files": sorted(set(zero_files)),
    }


def _go_cover(input_path: pathlib.Path, root: pathlib.Path) -> dict[str, Any]:
    covered = total = 0
    by_file: dict[str, list[int]] = {}
    lines = input_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or not re.fullmatch(r"mode: (?:set|count|atomic)", lines[0]):
        raise CriticError("Go cover profile is missing its mode header")
    pattern = re.compile(r"^(.*?):\d+\.\d+,\d+\.\d+\s+(\d+)\s+(\d+)$")
    for raw in lines[1:]:
        if not raw:
            continue
        match = pattern.match(raw)
        if not match:
            raise CriticError("Go cover profile contains a malformed record")
        filename, statements_text, count_text = match.groups()
        statements = int(statements_text)
        count = int(count_text)
        if statements <= 0:
            raise CriticError("Go cover profile statement counts must be positive")
        total += statements
        if count > 0:
            covered += statements
        file_counts = by_file.setdefault(_display_path(filename, root), [0, 0])
        file_counts[1] += statements
        if count > 0:
            file_counts[0] += statements
    zero_files = sorted(name for name, counts in by_file.items() if counts[1] and not counts[0])
    return {
        "lines": metric(covered, total),
        "branches": None,
        "functions": None,
        "metric_basis": "go_statement_blocks",
        "zero_coverage_files": zero_files,
    }


def command_normalize(args: argparse.Namespace) -> int:
    if args.status not in COVERAGE_STATUSES:
        raise CriticError(f"invalid coverage status: {args.status}")
    root = pathlib.Path(args.root).resolve(strict=True)
    input_path = pathlib.Path(args.input).resolve(strict=True)
    scratch_root = pathlib.Path(args.scratch_root).resolve(strict=True)
    ensure_within(root, scratch_root, "coverage project root")
    ensure_within(input_path, scratch_root, "native coverage input")
    if input_path.stat().st_size > NATIVE_COVERAGE_LIMIT_BYTES:
        raise CriticError(
            f"native coverage exceeds {NATIVE_COVERAGE_LIMIT_BYTES} bytes",
            kind="resource_limited",
        )
    if args.format == "coveragepy":
        normalized = _coveragepy(input_path, root)
    elif args.format == "istanbul":
        normalized = _istanbul(input_path, root)
    elif args.format == "lcov":
        normalized = _lcov(input_path, root)
    elif args.format == "go":
        normalized = _go_cover(input_path, root)
    else:
        raise CriticError(f"unsupported coverage format: {args.format}")
    tests = {"passed": None, "failed": None, "skipped": None, "total": None}
    if args.tests:
        supplied = json.loads(args.tests)
        if not isinstance(supplied, dict):
            raise CriticError("--tests must be a JSON object")
        for key in tests:
            value = supplied.get(key)
            if value is not None and (not isinstance(value, int) or value < 0):
                raise CriticError(f"test count {key} must be a non-negative integer or null")
            tests[key] = value
    project = {
        "id": args.project_id,
        "root": args.project_root,
        "ecosystem": args.ecosystem,
        "status": args.status,
        "tests": tests,
        **normalized,
        "exclusions": list(args.exclusion),
        "limitations": list(args.limitation),
        "native_artifacts": list(args.native_artifact),
    }
    validate_project_coverage(project)
    atomic_write_json(pathlib.Path(args.output), project)
    print(json.dumps(project, sort_keys=True))
    return 0


def _validate_metric(value: Any, name: str, *, nullable: bool) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, dict):
        raise CriticError(f"coverage metric {name} must be an object" + (" or null" if nullable else ""))
    covered = value.get("covered")
    total = value.get("total")
    percent = value.get("percent")
    if covered is None and total is None and percent is None:
        return
    if (
        not isinstance(covered, int)
        or isinstance(covered, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or covered < 0
        or total < 0
        or covered > total
    ):
        raise CriticError(f"coverage metric {name} has invalid counts")
    expected = round(100.0 * covered / total, 4) if total else None
    if expected is None:
        if percent is not None:
            raise CriticError(f"coverage metric {name} must use null percent for a zero denominator")
    elif (
        not _is_finite_number(percent)
        or abs(float(percent) - expected) > 0.0001
    ):
        raise CriticError(f"coverage metric {name} percent does not match its counts")


def validate_project_coverage(project: Any) -> None:
    if not isinstance(project, dict):
        raise CriticError("normalized coverage project must be an object")
    required = {
        "id",
        "root",
        "ecosystem",
        "status",
        "tests",
        "lines",
        "branches",
        "functions",
        "metric_basis",
        "zero_coverage_files",
        "exclusions",
        "limitations",
        "native_artifacts",
    }
    missing = sorted(required - set(project))
    extras = sorted(set(project) - required)
    if missing or extras:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extras:
            detail.append("unsupported " + ", ".join(extras))
        raise CriticError("normalized coverage project fields are invalid: " + "; ".join(detail))
    if not isinstance(project["id"], str) or not project["id"]:
        raise CriticError("coverage project id must be non-empty text")
    if not isinstance(project["root"], str) or not project["root"]:
        raise CriticError("coverage project root must be non-empty text")
    if project["ecosystem"] not in {"python", "javascript-typescript", "go"}:
        raise CriticError("coverage project ecosystem is invalid")
    if project["status"] not in COVERAGE_STATUSES:
        raise CriticError(f"normalized project has invalid coverage status: {project['status']}")
    tests = project["tests"]
    if not isinstance(tests, dict) or set(tests) != {"passed", "failed", "skipped", "total"}:
        raise CriticError("coverage project tests must contain exact count fields")
    for key, value in tests.items():
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise CriticError(f"coverage project test count {key} is invalid")
    if all(tests[key] is not None for key in ("passed", "failed", "skipped", "total")):
        if tests["passed"] + tests["failed"] + tests["skipped"] != tests["total"]:
            raise CriticError("coverage project test counts do not add up to total")
    _validate_metric(project["lines"], "lines", nullable=False)
    _validate_metric(project["branches"], "branches", nullable=True)
    _validate_metric(project["functions"], "functions", nullable=True)
    if project["metric_basis"] not in {
        "executable_lines",
        "instrumented_lines",
        "go_statement_blocks",
    }:
        raise CriticError("coverage project metric_basis is invalid")
    if project["ecosystem"] == "go" and project["metric_basis"] != "go_statement_blocks":
        raise CriticError("Go coverage must use the go_statement_blocks metric basis")
    if project["ecosystem"] == "python" and project["metric_basis"] != "executable_lines":
        raise CriticError("Python coverage must use the executable_lines metric basis")
    if project["ecosystem"] == "go" and (
        project["branches"] is not None or project["functions"] is not None
    ):
        raise CriticError("Go coverage must use null branch and function metrics")
    if project["status"] in {"complete", "tests_failed_partial"}:
        if (
            not isinstance(project["lines"], dict)
            or project["lines"].get("covered") is None
            or project["lines"].get("total", 0) <= 0
        ):
            raise CriticError("measured coverage status requires a positive executable-line total")
    for key in ("zero_coverage_files", "exclusions", "limitations", "native_artifacts"):
        if not isinstance(project[key], list) or not all(isinstance(item, str) for item in project[key]):
            raise CriticError(f"coverage project {key} must be a string array")
    _validate_relative_reference(project["root"], "coverage project root", allow_dot=True)
    for reference in project["zero_coverage_files"]:
        _validate_relative_reference(reference, "zero-coverage file")
    for reference in project["native_artifacts"]:
        _validate_relative_reference(reference, "native coverage artifact")


def _validate_relative_reference(value: str, label: str, *, allow_dot: bool = False) -> None:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise CriticError(f"{label} must be a non-empty POSIX relative path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or (value == "." and not allow_dot):
        raise CriticError(f"{label} must stay within its artifact or repository root: {value}")


def command_aggregate_coverage(args: argparse.Namespace) -> int:
    projects = []
    project_ids: set[str] = set()
    for path_text in args.project:
        project = read_json(pathlib.Path(path_text))
        validate_project_coverage(project)
        if project["id"] in project_ids:
            raise CriticError(f"duplicate coverage project id: {project['id']}")
        project_ids.add(project["id"])
        projects.append(project)
    if len(projects) > MAX_PROJECT_ROOTS:
        raise CriticError(f"coverage summary exceeds {MAX_PROJECT_ROOTS} project roots")
    precedence = (
        "resource_limited",
        "timed_out",
        "blocked_dependency_restore",
        "unsupported_toolchain",
        "tests_failed_partial",
        "no_tests",
        "complete",
    )
    present = {str(project["status"]) for project in projects}
    aggregate_status = next((status for status in precedence if status in present), "no_tests")
    result = {
        "schema_version": SCHEMA_VERSION,
        "review_mode": REVIEW_MODE,
        "status": aggregate_status,
        "projects": projects,
        "high_priority_gaps": list(args.high_priority_gap),
    }
    atomic_write_json(pathlib.Path(args.output), result)
    print(json.dumps(result, sort_keys=True))
    return 0


def _write_coverage_archive(
    directories: Sequence[pathlib.Path], scratch_root: pathlib.Path, output: pathlib.Path
) -> dict[str, Any]:
    if len(directories) > MAX_PROJECT_ROOTS:
        raise CriticError(f"coverage archive exceeds {MAX_PROJECT_ROOTS} project directories")
    checked_directories: list[pathlib.Path] = []
    seen_directories: set[pathlib.Path] = set()
    for directory in directories:
        directory = directory.resolve(strict=True)
        ensure_within(directory, scratch_root, "coverage archive input")
        if directory in seen_directories:
            raise CriticError("coverage archive contains a duplicate project directory")
        seen_directories.add(directory)
        validate_snapshot_tree(directory)
        size = directory_size(directory)
        if size > NATIVE_COVERAGE_LIMIT_BYTES:
            raise CriticError(
                f"coverage directory exceeds {NATIVE_COVERAGE_LIMIT_BYTES} bytes: {directory}",
                kind="resource_limited",
            )
        checked_directories.append(directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    entry_count = 0
    try:
        with temporary.open("wb") as raw_output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    for index, directory in enumerate(checked_directories, start=1):
                        for path in _walk_entries(directory):
                            metadata = path.lstat()
                            if stat.S_ISDIR(metadata.st_mode):
                                continue
                            if not stat.S_ISREG(metadata.st_mode):
                                raise CriticError(f"coverage archive input is not a regular file: {path}")
                            entry_count += 1
                            if entry_count > 50_000:
                                raise CriticError("coverage archive exceeds 50,000 entries", kind="resource_limited")
                            relative = path.relative_to(directory)
                            archive.add(
                                path,
                                arcname=(pathlib.PurePosixPath(f"project-{index}") / relative).as_posix(),
                                recursive=False,
                                filter=lambda info: _normalized_tar_info(info),
                            )
        if temporary.stat().st_size > 100 * 1024 * 1024:
            raise CriticError("compressed coverage archive exceeds 100 MiB", kind="resource_limited")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "output": str(output),
        "coverage_directories": len(checked_directories),
        "entries": entry_count,
        "bytes": output.stat().st_size,
    }


def command_archive_coverage(args: argparse.Namespace) -> int:
    scratch_root = pathlib.Path(args.scratch_root).resolve(strict=True)
    directories = [pathlib.Path(value) for value in args.coverage_dir]
    result = _write_coverage_archive(directories, scratch_root, pathlib.Path(args.output))
    print(json.dumps(result, sort_keys=True))
    return 0


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode &= 0o777
    info.pax_headers = {}
    return info


def _sanitize_json_strings(
    value: Any,
    inherited_proxy: str | None = None,
    endpoint: _ManagedProxyEndpoint | None = None,
) -> Any:
    if isinstance(value, str):
        return _redact_managed_proxy_details(value, inherited_proxy, endpoint)
    if isinstance(value, list):
        return [_sanitize_json_strings(item, inherited_proxy, endpoint) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _sanitize_json_strings(item, inherited_proxy, endpoint)
            for key, item in value.items()
        }
    return value


def validate_repository_review(review: Any, prepare: Mapping[str, Any]) -> None:
    if not isinstance(review, dict):
        raise CriticError("repository review must be a JSON object")
    review_fields = {
        "schema_version",
        "review_mode",
        "status",
        "repository",
        "summary",
        "traceability",
        "findings",
        "dynamic_analysis",
        "limitations",
    }
    if set(review) != review_fields:
        raise CriticError("repository review has missing or unsupported fields")
    if review.get("schema_version") != SCHEMA_VERSION or review.get("review_mode") != REVIEW_MODE:
        raise CriticError("repository review schema_version/review_mode is invalid")
    if review.get("status") not in {"complete", "partial", "blocked"}:
        raise CriticError("repository review status is invalid")
    repository = review.get("repository")
    if not isinstance(repository, dict):
        raise CriticError("repository review provenance is missing")
    expected_repository = {
        "companion_name": prepare.get("companion_name"),
        "source_path": prepare.get("source_path"),
        "scratch_path": prepare.get("repository_path"),
        "snapshot_sha256": prepare.get("source_snapshot_sha256"),
        "git_commit": prepare.get("git_commit"),
        "git_dirty": prepare.get("git_dirty"),
    }
    if repository != expected_repository:
        raise CriticError("repository review provenance does not match the prepared snapshot")
    summary = review.get("summary")
    summary_fields = {"verdict", "p0_count", "p1_count", "p2_count", "p3_count"}
    if (
        not isinstance(summary, dict)
        or set(summary) != summary_fields
        or not isinstance(summary.get("verdict"), str)
    ):
        raise CriticError("repository review summary is invalid")
    findings = review.get("findings")
    if not isinstance(findings, list):
        raise CriticError("repository review findings must be an array")
    counts = {priority: 0 for priority in ("P0", "P1", "P2", "P3")}
    finding_ids: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            raise CriticError("each repository finding must be an object")
        required = {
            "id",
            "priority",
            "confidence",
            "category",
            "title",
            "description",
            "evidence",
            "impact",
            "recommendation",
        }
        if set(finding) != required:
            raise CriticError("repository finding has missing or unsupported fields")
        if not isinstance(finding["id"], str) or not finding["id"] or finding["id"] in finding_ids:
            raise CriticError("repository finding ids must be non-empty and unique")
        finding_ids.add(finding["id"])
        if finding["priority"] not in counts:
            raise CriticError("repository finding priority is invalid")
        counts[finding["priority"]] += 1
        if finding["confidence"] not in {"high", "medium", "low"}:
            raise CriticError("repository finding confidence is invalid")
        for key in ("category", "title", "description", "impact", "recommendation"):
            if not isinstance(finding[key], str) or not finding[key]:
                raise CriticError(f"repository finding {key} must be non-empty text")
        if not isinstance(finding["evidence"], list) or not finding["evidence"]:
            raise CriticError("repository finding evidence must be a non-empty array")
        for evidence in finding["evidence"]:
            if not isinstance(evidence, dict) or set(evidence) != {"kind", "path", "line", "url"}:
                raise CriticError("finding evidence must contain exact kind/path/line/url fields")
            if not isinstance(evidence["kind"], str) or not evidence["kind"]:
                raise CriticError("finding evidence kind must be non-empty text")
            if evidence["path"] is not None:
                _validate_relative_reference(evidence["path"], "finding evidence path")
            if evidence["line"] is not None and (
                not isinstance(evidence["line"], int)
                or isinstance(evidence["line"], bool)
                or evidence["line"] < 1
            ):
                raise CriticError("finding evidence line must be a positive integer or null")
            if evidence["url"] is not None:
                parsed = urllib.parse.urlsplit(evidence["url"])
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise CriticError("finding evidence URL must be credential-free HTTPS")
    for priority, count in counts.items():
        if summary.get(f"{priority.lower()}_count") != count:
            raise CriticError(f"repository review summary {priority} count is inconsistent")
    traceability = review.get("traceability")
    if not isinstance(traceability, list):
        raise CriticError("repository review traceability must be an array")
    if review["status"] != "blocked" and not traceability:
        raise CriticError("non-blocked repository review requires traceability evidence")
    claim_ids: set[str] = set()
    for row in traceability:
        if not isinstance(row, dict):
            raise CriticError("traceability row must be an object")
        required = {
            "claim_id",
            "claim",
            "authority",
            "status",
            "documentation_evidence",
            "implementation_evidence",
            "test_evidence",
        }
        if set(row) != required:
            raise CriticError("traceability row has missing or unsupported fields")
        if not isinstance(row["claim_id"], str) or not row["claim_id"] or row["claim_id"] in claim_ids:
            raise CriticError("traceability claim ids must be non-empty and unique")
        claim_ids.add(row["claim_id"])
        if row["status"] not in {"verified", "partial", "contradicted", "unverifiable"}:
            raise CriticError("traceability status is invalid")
        for key in ("claim", "authority"):
            if not isinstance(row[key], str) or not row[key]:
                raise CriticError(f"traceability {key} must be non-empty text")
        for key in ("documentation_evidence", "implementation_evidence", "test_evidence"):
            if not isinstance(row[key], list) or not all(isinstance(item, str) for item in row[key]):
                raise CriticError(f"traceability {key} must be a string array")
    dynamic = review.get("dynamic_analysis")
    if not isinstance(dynamic, dict) or set(dynamic) != {
        "status",
        "project_count",
        "test_run_count",
        "coverage_run_count",
    }:
        raise CriticError("repository review dynamic_analysis is invalid")
    if not isinstance(dynamic["status"], str) or not dynamic["status"]:
        raise CriticError("dynamic analysis status must be non-empty text")
    for key in ("project_count", "test_run_count", "coverage_run_count"):
        if not isinstance(dynamic[key], int) or isinstance(dynamic[key], bool) or dynamic[key] < 0:
            raise CriticError(f"dynamic analysis {key} must be non-negative")
    limitations = review.get("limitations")
    if not isinstance(limitations, list) or not all(isinstance(item, str) for item in limitations):
        raise CriticError("repository review limitations must be a string array")


def validate_coverage_summary(summary: Any) -> None:
    if not isinstance(summary, dict) or set(summary) != {
        "schema_version",
        "review_mode",
        "status",
        "projects",
        "high_priority_gaps",
    }:
        raise CriticError("coverage summary has missing or unsupported fields")
    if summary["schema_version"] != SCHEMA_VERSION or summary["review_mode"] != REVIEW_MODE:
        raise CriticError("coverage summary schema_version/review_mode is invalid")
    if not isinstance(summary["projects"], list) or len(summary["projects"]) > MAX_PROJECT_ROOTS:
        raise CriticError(f"coverage summary must contain at most {MAX_PROJECT_ROOTS} projects")
    ids: set[str] = set()
    for project in summary["projects"]:
        validate_project_coverage(project)
        if project["id"] in ids:
            raise CriticError(f"duplicate coverage project id: {project['id']}")
        ids.add(project["id"])
    if not isinstance(summary["high_priority_gaps"], list) or not all(
        isinstance(item, str) for item in summary["high_priority_gaps"]
    ):
        raise CriticError("coverage high_priority_gaps must be a string array")
    precedence = (
        "resource_limited",
        "timed_out",
        "blocked_dependency_restore",
        "unsupported_toolchain",
        "tests_failed_partial",
        "no_tests",
        "complete",
    )
    present = {project["status"] for project in summary["projects"]}
    expected = next((status for status in precedence if status in present), "no_tests")
    if summary["status"] != expected:
        raise CriticError("coverage aggregate status is inconsistent with its projects")


def _contains_forbidden_manifest_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in {"env", "environment", "stdout", "stderr", "raw_output"}:
                return True
            if _contains_forbidden_manifest_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_manifest_field(item) for item in value)
    return False


def _validate_sha256_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "possibly empty " if allow_empty else "non-empty "
        raise CriticError(f"{label} must be a {qualifier}SHA-256 string array")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CriticError(f"{label} contains a non-string digest")
        digest = item.removeprefix("sha256:").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CriticError(f"{label} contains an invalid SHA-256 digest")
        normalized.append(digest)
    if len(normalized) != len(set(normalized)):
        raise CriticError(f"{label} contains duplicate SHA-256 digests")
    return normalized


def _validate_https_source_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CriticError(f"{label} must be a credential-free HTTPS URL")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CriticError(f"{label} must be a credential-free HTTPS URL")
    return value


def _baked_toolchain_manifest() -> Any:
    return read_json(pathlib.Path(__file__).resolve().parents[1] / "toolchain-manifest.json")


def validate_run_manifest(manifest: Any, prepare: Mapping[str, Any], job_id: str) -> None:
    if not isinstance(manifest, dict):
        raise CriticError("run manifest must be an object")
    required = {
        "schema_version",
        "review_mode",
        "job_id",
        "started_at",
        "finished_at",
        "total_duration_seconds",
        "repository",
        "policy",
        "toolchains",
        "commands",
        "dependency_restores",
        "limits",
        "cleanup",
        "limitations",
    }
    optional = {"log_truncated"}
    if required - set(manifest) or set(manifest) - required - optional:
        raise CriticError("run manifest is missing required fields")
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["review_mode"] != REVIEW_MODE:
        raise CriticError("run manifest schema_version/review_mode is invalid")
    if manifest["job_id"] != job_id:
        raise CriticError("run manifest job id is invalid")
    _parse_timestamp(manifest["started_at"], "run manifest started_at")
    if manifest["finished_at"] is not None:
        _parse_timestamp(manifest["finished_at"], "run manifest finished_at")
    if (
        not _is_finite_number(manifest["total_duration_seconds"])
        or manifest["total_duration_seconds"] < 0
    ):
        raise CriticError("run manifest total_duration_seconds must be non-negative")
    repository = manifest["repository"]
    expected_repository = {
        "companion_name": prepare.get("companion_name"),
        "source_path": prepare.get("source_path"),
        "scratch_path": prepare.get("repository_path"),
        "snapshot_sha256": prepare.get("source_snapshot_sha256"),
        "git_commit": prepare.get("git_commit"),
        "git_dirty": prepare.get("git_dirty"),
    }
    if repository != expected_repository:
        raise CriticError("run manifest repository provenance is invalid")
    policy = manifest["policy"]
    if not isinstance(policy, dict) or set(policy) != {
        "network",
        "build_hooks",
        "locked_restore_required",
    }:
        raise CriticError("run manifest policy must contain exact network/hook/lock fields")
    if not isinstance(policy["network"], str) or not policy["network"]:
        raise CriticError("run manifest network policy must be non-empty text")
    if not isinstance(policy["build_hooks"], bool) or not isinstance(
        policy["locked_restore_required"], bool
    ):
        raise CriticError("run manifest hook/lock policy fields must be booleans")
    if not isinstance(manifest["toolchains"], dict) or manifest["toolchains"].get(
        "schema_version"
    ) != SCHEMA_VERSION:
        raise CriticError("run manifest toolchains must embed toolchain-manifest.json")
    for key in ("runtimes", "coverage_tools", "package_managers", "source_images", "runtime_policy"):
        if not isinstance(manifest["toolchains"].get(key), dict):
            raise CriticError(f"run manifest toolchains.{key} must be an object")
    if manifest["toolchains"] != _baked_toolchain_manifest():
        raise CriticError("run manifest toolchains do not match the baked toolchain manifest")
    if not isinstance(manifest["commands"], list):
        raise CriticError("run manifest commands are invalid")
    command_ids: set[str] = set()
    command_required = {
        "id",
        "cwd",
        "argv",
        "ecosystem",
        "phase",
        "restore_mode",
        "started_at",
        "duration_seconds",
        "exit_code",
        "signal",
        "status",
        "stdout_truncated",
        "stderr_truncated",
        "build_hooks_enabled",
        "limit_reason",
    }
    command_optional = {"managed_proxy_state"}
    scratch_root = pathlib.Path(str(prepare.get("scratch_root", ""))).resolve(strict=False)
    for command in manifest["commands"]:
        if (
            not isinstance(command, dict)
            or command_required - set(command)
            or set(command) - command_required - command_optional
        ):
            raise CriticError("run manifest command must contain only supported fields")
        if _contains_inherited_managed_proxy_details(command):
            raise CriticError("run manifest command contains managed proxy endpoint details")
        if not isinstance(command["id"], str) or not command["id"] or command["id"] in command_ids:
            raise CriticError("run manifest command ids must be non-empty and unique")
        command_ids.add(command["id"])
        if not isinstance(command["argv"], list) or not command["argv"] or not all(
            isinstance(item, str) for item in command["argv"]
        ):
            raise CriticError("run manifest command argv must be a non-empty string array")
        try:
            pathlib.Path(command["cwd"]).resolve(strict=False).relative_to(scratch_root)
        except (TypeError, ValueError) as exc:
            raise CriticError("run manifest command cwd escapes job scratch") from exc
        if command["phase"] not in {"restore", "test", "coverage", "static"}:
            raise CriticError("run manifest command phase is invalid")
        if (
            command["phase"] == "restore"
            and command["restore_mode"] not in {"locked", "resolved_unlocked"}
        ) or (command["phase"] != "restore" and command["restore_mode"] is not None):
            raise CriticError("run manifest command restore_mode is invalid")
        if command["ecosystem"] not in {"python", "javascript-typescript", "go", "other"}:
            raise CriticError("run manifest command ecosystem is invalid")
        _parse_timestamp(command["started_at"], "run manifest command started_at")
        if (
            not _is_finite_number(command["duration_seconds"])
            or command["duration_seconds"] < 0
        ):
            raise CriticError("run manifest command duration is invalid")
        if command["exit_code"] is not None and (
            not isinstance(command["exit_code"], int) or isinstance(command["exit_code"], bool)
        ):
            raise CriticError("run manifest command exit_code is invalid")
        if command["status"] not in {
            "success",
            "failed",
            "timed_out",
            "resource_limited",
            "start_failed",
        }:
            raise CriticError("run manifest command status is invalid")
        for key in ("stdout_truncated", "stderr_truncated", "build_hooks_enabled"):
            if not isinstance(command[key], bool):
                raise CriticError(f"run manifest command {key} must be boolean")
        for key in ("signal", "limit_reason"):
            if command[key] is not None and not isinstance(command[key], str):
                raise CriticError(f"run manifest command {key} must be text or null")
        managed_proxy_state = command.get("managed_proxy_state")
        if managed_proxy_state is not None and (
            not isinstance(managed_proxy_state, str)
            or managed_proxy_state not in {"available", "unavailable"}
        ):
            raise CriticError("run manifest command managed_proxy_state is invalid")
        if command["phase"] != "restore" and managed_proxy_state is not None:
            raise CriticError("non-restore command managed_proxy_state must be null")
        if managed_proxy_state == "unavailable" and (
            command["status"] != "failed"
            or command["limit_reason"] != MANAGED_PROXY_UNAVAILABLE
        ):
            raise CriticError("unavailable managed proxy state has inconsistent failure evidence")
        if (
            command["limit_reason"] == MANAGED_PROXY_UNAVAILABLE
            and managed_proxy_state != "unavailable"
        ):
            raise CriticError("managed proxy failure reason requires unavailable state")
    restores = manifest["dependency_restores"]
    if not isinstance(restores, list):
        raise CriticError("run manifest dependency_restores must be an array")
    restore_required = {
        "project",
        "ecosystem",
        "manager",
        "mode",
        "reproducible",
        "dependency_manifest_sha256",
        "source_url",
        "resolved_dependencies",
        "freeze",
        "generated_lock",
    }
    for restore in restores:
        if not isinstance(restore, dict) or set(restore) != restore_required:
            raise CriticError("dependency restore must contain the exact evidence fields")
        _validate_relative_reference(
            restore["project"], "dependency restore project", allow_dot=True
        )
        if restore["ecosystem"] not in {"python", "javascript-typescript", "go"}:
            raise CriticError("dependency restore ecosystem is invalid")
        allowed_managers = {
            "python": {"pip", "uv", "poetry", "pipenv", "python-venv"},
            "javascript-typescript": {"npm", "pnpm", "yarn"},
            "go": {"go"},
        }
        if restore["manager"] not in allowed_managers[restore["ecosystem"]]:
            raise CriticError("dependency restore manager does not match its ecosystem")
        if restore["mode"] not in {"locked", "resolved_unlocked"}:
            raise CriticError("dependency restore mode is invalid")
        if not isinstance(restore["reproducible"], bool) or restore["reproducible"] is not (
            restore["mode"] == "locked"
        ):
            raise CriticError("dependency restore reproducibility disagrees with its mode")
        digest = restore["dependency_manifest_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CriticError("dependency restore manifest digest is invalid")
        source_url = _validate_https_source_url(
            restore["source_url"], "dependency restore source_url"
        )
        dependencies = restore["resolved_dependencies"]
        if not isinstance(dependencies, list):
            raise CriticError("dependency restore resolved_dependencies must be an array")
        dependency_names: set[str] = set()
        resolved_integrities: set[str] = set()
        expected_freeze: list[str] = []
        dependency_required = {"name", "version", "source_url", "integrity_sha256"}
        for dependency in dependencies:
            if not isinstance(dependency, dict) or set(dependency) != dependency_required:
                raise CriticError("resolved dependency must contain exact provenance fields")
            name = dependency["name"]
            version = dependency["version"]
            if not isinstance(name, str) or not name or name in dependency_names:
                raise CriticError("resolved dependency names must be non-empty and unique")
            if not isinstance(version, str) or not version or any(
                marker in version for marker in ("*", "<", ">", "~", "^", " ")
            ):
                raise CriticError("resolved dependency versions must be exact non-empty values")
            dependency_names.add(name)
            if _validate_https_source_url(
                dependency["source_url"], "resolved dependency source_url"
            ) != source_url:
                raise CriticError("resolved dependency source_url disagrees with restore source_url")
            hashes = _validate_sha256_list(
                dependency["integrity_sha256"], "resolved dependency integrity_sha256"
            )
            resolved_integrities.update(hashes)
            expected_freeze.append(f"{name}=={version}")
        freeze = restore["freeze"]
        if not isinstance(freeze, list) or not all(
            isinstance(item, str) and item for item in freeze
        ):
            raise CriticError("dependency restore freeze must be an exact string array")
        if restore["ecosystem"] == "python" and freeze != expected_freeze:
            raise CriticError("Python dependency restore freeze disagrees with resolved dependencies")
        generated = restore["generated_lock"]
        generated_required = {
            "generated",
            "path",
            "sha256",
            "requirements",
            "integrity_sha256",
        }
        if not isinstance(generated, dict) or set(generated) != generated_required:
            raise CriticError("dependency restore generated_lock has invalid fields")
        if not isinstance(generated["generated"], bool) or generated["generated"] is not (
            restore["mode"] == "resolved_unlocked"
        ):
            raise CriticError("generated_lock.generated disagrees with dependency restore mode")
        _validate_relative_reference(generated["path"], "dependency restore lock path")
        if not isinstance(generated["sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", generated["sha256"]
        ):
            raise CriticError("dependency restore lock SHA-256 is invalid")
        if generated["requirements"] != freeze:
            raise CriticError("generated lock requirements disagree with freeze evidence")
        lock_integrities = set(
            _validate_sha256_list(
                generated["integrity_sha256"],
                "generated lock integrity_sha256",
                allow_empty=not dependencies,
            )
        )
        if lock_integrities != resolved_integrities:
            raise CriticError("generated lock integrity disagrees with resolved dependencies")
        if not any(
            command["phase"] == "restore"
            and command["ecosystem"] == restore["ecosystem"]
            and command["restore_mode"] == restore["mode"]
            for command in manifest["commands"]
        ):
            raise CriticError("dependency restore has no matching helper command evidence")
    limits = manifest["limits"]
    if limits != _enforced_limits():
        raise CriticError("run manifest limits do not match the enforced defaults")
    if not isinstance(manifest["cleanup"], dict):
        raise CriticError("run manifest cleanup must be an object")
    _validate_cleanup(manifest["cleanup"], job_id)
    if not isinstance(manifest["limitations"], list) or not all(
        isinstance(item, str) for item in manifest["limitations"]
    ):
        raise CriticError("run manifest limitations must be a string array")
    if "log_truncated" in manifest and not isinstance(manifest["log_truncated"], bool):
        raise CriticError("run manifest log_truncated must be boolean")
    if _contains_forbidden_manifest_field(manifest):
        raise CriticError("run manifest contains raw environment or process-output fields")


def _parse_timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CriticError(f"{label} must be a UTC RFC 3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CriticError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise CriticError(f"{label} must be UTC")
    return parsed


def _validate_cleanup(cleanup: Mapping[str, Any], job_id: str) -> None:
    if cleanup == {"status": "pending"}:
        return
    required = {
        "schema_version",
        "job_id",
        "finished_at",
        "duration_seconds",
        "source_unchanged",
        "source_snapshot_sha256",
        "scratch_removed",
        "scratch_bytes_before",
        "entries_removed",
        "status",
        "error",
    }
    if set(cleanup) != required or cleanup.get("schema_version") != SCHEMA_VERSION:
        raise CriticError("run manifest cleanup result has invalid fields")
    if cleanup.get("job_id") != job_id or cleanup.get("status") not in {"complete", "partial"}:
        raise CriticError("run manifest cleanup result has invalid job/status")
    _parse_timestamp(cleanup.get("finished_at"), "run manifest cleanup finished_at")
    if (
        not _is_finite_number(cleanup.get("duration_seconds"))
        or cleanup["duration_seconds"] < 0
    ):
        raise CriticError("run manifest cleanup duration is invalid")
    for key in ("source_unchanged", "scratch_removed"):
        if not isinstance(cleanup.get(key), bool):
            raise CriticError(f"run manifest cleanup {key} must be boolean")
    digest = cleanup.get("source_snapshot_sha256")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise CriticError("run manifest cleanup source digest is invalid")
    for key in ("scratch_bytes_before", "entries_removed"):
        value = cleanup.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise CriticError(f"run manifest cleanup {key} is invalid")
    if cleanup.get("error") is not None and not isinstance(cleanup.get("error"), str):
        raise CriticError("run manifest cleanup error must be text or null")


def _checked_scratch_file(path_text: str, scratch_root: pathlib.Path, label: str) -> pathlib.Path:
    supplied = pathlib.Path(path_text)
    if supplied.is_symlink():
        raise CriticError(f"{label} must not be a symlink")
    path = supplied.resolve(strict=True)
    ensure_within(path, scratch_root, label)
    if not path.is_file():
        raise CriticError(f"{label} must be a regular scratch file")
    return path


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_restore_evidence_files(
    manifest: Mapping[str, Any],
    prepare: Mapping[str, Any],
    scratch_root: pathlib.Path,
    coverage_directories: Sequence[pathlib.Path],
) -> None:
    repository = pathlib.Path(str(prepare.get("repository_path", ""))).resolve(strict=True)
    ensure_within(repository, scratch_root, "prepared repository")
    checked_coverage_directories: list[pathlib.Path] = []
    for directory in coverage_directories:
        resolved = directory.resolve(strict=True)
        ensure_within(resolved, scratch_root, "coverage provenance directory")
        if resolved.is_symlink() or not resolved.is_dir():
            raise CriticError("coverage provenance input must be a real directory")
        checked_coverage_directories.append(resolved)

    manifest_names = {
        "python": ("pyproject.toml", "requirements.txt", "Pipfile"),
        "javascript-typescript": ("package.json",),
        "go": ("go.mod",),
    }
    for restore in manifest["dependency_restores"]:
        project_root = ensure_within(
            repository / restore["project"], repository, "dependency restore project"
        )
        if not project_root.is_dir():
            raise CriticError("dependency restore project is not a repository directory")
        expected_manifest_digest = restore["dependency_manifest_sha256"]
        matching_manifests = [
            project_root / name
            for name in manifest_names[restore["ecosystem"]]
            if (project_root / name).is_file()
            and not (project_root / name).is_symlink()
            and _sha256_file(project_root / name) == expected_manifest_digest
        ]
        if not matching_manifests:
            raise CriticError(
                "dependency restore manifest digest does not match a supported project manifest"
            )

        generated = restore["generated_lock"]
        lock_path = ensure_within(
            project_root / generated["path"], project_root, "dependency restore lock"
        )
        if lock_path.is_symlink() or not lock_path.is_file():
            raise CriticError("dependency restore lock evidence is not a regular project file")
        lock_digest = _sha256_file(lock_path)
        if lock_digest != generated["sha256"]:
            raise CriticError("dependency restore lock digest disagrees with the project lock file")
        if generated["generated"]:
            published_copies = []
            for directory in checked_coverage_directories:
                candidate = ensure_within(
                    directory / generated["path"], directory, "archived generated lock copy"
                )
                if candidate.is_file() and not candidate.is_symlink():
                    published_copies.append(candidate)
            if not published_copies or not any(
                _sha256_file(candidate) == lock_digest for candidate in published_copies
            ):
                raise CriticError(
                    "generated dependency lock must be copied unchanged into a coverage/provenance directory"
                )
            if restore["manager"] == "pip":
                text = lock_path.read_text(encoding="utf-8")
                for requirement in generated["requirements"]:
                    if requirement not in text:
                        raise CriticError(
                            "generated pip lock content disagrees with its requirement inventory"
                        )
                for digest in generated["integrity_sha256"]:
                    if digest.removeprefix("sha256:") not in text:
                        raise CriticError(
                            "generated pip lock content disagrees with its integrity inventory"
                        )


def _planned_coverage_archive_members(
    directories: Sequence[pathlib.Path], scratch_root: pathlib.Path
) -> set[str]:
    if len(directories) > MAX_PROJECT_ROOTS:
        raise CriticError(f"coverage archive exceeds {MAX_PROJECT_ROOTS} project directories")
    members: set[str] = set()
    seen: set[pathlib.Path] = set()
    for index, directory in enumerate(directories, start=1):
        resolved = directory.resolve(strict=True)
        ensure_within(resolved, scratch_root, "coverage archive input")
        if resolved in seen:
            raise CriticError("coverage archive contains a duplicate project directory")
        seen.add(resolved)
        validate_snapshot_tree(resolved)
        for path in _walk_entries(resolved):
            if stat.S_ISREG(path.lstat().st_mode):
                relative = path.relative_to(resolved).as_posix()
                members.add(f"coverage-details.tar.gz#project-{index}/{relative}")
    return members


def _validate_coverage_artifact_links(
    coverage: Mapping[str, Any], archive_members: set[str]
) -> None:
    for project in coverage["projects"]:
        native_artifacts = project["native_artifacts"]
        if project["status"] in {"complete", "tests_failed_partial"} and not native_artifacts:
            raise CriticError("measured coverage project must link at least one native artifact")
        for reference in native_artifacts:
            if reference not in archive_members:
                raise CriticError(
                    f"native coverage artifact does not name an archived regular file: {reference}"
                )


def _validate_command_ledger(manifest: Mapping[str, Any], scratch_root: pathlib.Path) -> None:
    ledger_path = scratch_root / ".critic-budget.json"
    if ledger_path.is_symlink():
        raise CriticError("dynamic command ledger must not be a symlink")
    if not ledger_path.exists():
        if manifest["commands"]:
            raise CriticError("run manifest commands exist without the helper command ledger")
        return
    inherited_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    managed_proxy_endpoint = _managed_loopback_proxy_endpoint(inherited_proxy)
    ledger = _sanitize_json_strings(
        read_json(ledger_path), inherited_proxy, managed_proxy_endpoint
    )
    if not isinstance(ledger, dict) or set(ledger) != {
        "schema_version",
        "spent_seconds",
        "runs",
    }:
        raise CriticError("dynamic command ledger has invalid fields")
    if ledger["schema_version"] != SCHEMA_VERSION or not isinstance(ledger["runs"], list):
        raise CriticError("dynamic command ledger schema is invalid")
    spent = ledger["spent_seconds"]
    if (
        not _is_finite_number(spent)
        or spent < 0
    ):
        raise CriticError("dynamic command ledger spent_seconds is invalid")
    if len(ledger["runs"]) != sum(isinstance(run, dict) for run in ledger["runs"]):
        raise CriticError("dynamic command ledger contains a non-object run")
    run_durations = [run.get("duration_seconds") for run in ledger["runs"]]
    if any(
        not _is_finite_number(duration)
        or duration < 0
        for duration in run_durations
    ):
        raise CriticError("dynamic command ledger run durations are invalid")
    recorded_total = sum(float(duration) for duration in run_durations)
    if not math.isfinite(recorded_total):
        raise CriticError("dynamic command ledger run durations are invalid")
    recorded_spent = round(recorded_total, 6)
    if abs(float(spent) - recorded_spent) > 0.000001:
        raise CriticError("dynamic command ledger duration disagrees with its runs")
    if manifest["commands"] != ledger["runs"]:
        raise CriticError("run manifest commands do not exactly match the helper command ledger")


def command_finalize(args: argparse.Namespace) -> int:
    workspace = pathlib.Path(args.workspace).resolve(strict=True)
    if not JOB_ID_RE.fullmatch(args.job_id):
        raise CriticError("trusted current job id is invalid")
    supplied_prepare_path = pathlib.Path(args.prepare_record)
    prepare_path = supplied_prepare_path.resolve(strict=True)
    expected_prepare_path = workspace / ".repository-critic" / args.job_id / "prepare.json"
    if (
        supplied_prepare_path.is_symlink()
        or prepare_path != expected_prepare_path.resolve(strict=True)
    ):
        raise CriticError("prepare record is not the exact current job record")
    prepare = read_json_bounded(prepare_path, PREPARE_RECORD_LIMIT_BYTES, "prepare record")
    if prepare.get("job_id") != args.job_id:
        raise CriticError("prepare record does not match the current job")
    scratch_root = pathlib.Path(str(prepare.get("scratch_root", ""))).resolve(strict=True)
    expected_scratch = workspace / ".repository-critic" / args.job_id
    if scratch_root.is_symlink() or scratch_root != expected_scratch.resolve(strict=True):
        raise CriticError("finalizer scratch root is not the exact current job directory")
    artifacts_base = pathlib.Path(args.artifacts).resolve(strict=True)
    artifact_root = artifacts_base / f"repository-review-{args.job_id}"
    if artifact_root.is_symlink() or artifact_root.resolve(strict=True) != pathlib.Path(
        str(prepare.get("artifact_root", ""))
    ).resolve(strict=True):
        raise CriticError("finalizer artifact root does not match the prepared job")
    existing = sorted(path.name for path in artifact_root.iterdir())
    if existing:
        raise CriticError("artifact root must be empty before finalization: " + ", ".join(existing))

    report_path = _checked_scratch_file(args.report, scratch_root, "Markdown report")
    review_path = _checked_scratch_file(args.review_json, scratch_root, "review JSON")
    coverage_path = _checked_scratch_file(args.coverage_json, scratch_root, "coverage JSON")
    manifest_path = _checked_scratch_file(args.run_manifest, scratch_root, "run manifest")
    if len(args.log) > MAX_PROJECT_ROOTS:
        raise CriticError(f"finalizer accepts at most {MAX_PROJECT_ROOTS} bounded project logs")
    if len(args.coverage_dir) > MAX_PROJECT_ROOTS:
        raise CriticError(
            f"finalizer accepts at most {MAX_PROJECT_ROOTS} project coverage directories"
        )
    log_paths = [_checked_scratch_file(value, scratch_root, "test log") for value in args.log]
    for label, path in (
        ("Markdown report", report_path),
        ("review JSON", review_path),
        ("coverage JSON", coverage_path),
        ("run manifest", manifest_path),
    ):
        if path.stat().st_size > PRIMARY_ARTIFACT_LIMIT_BYTES:
            raise CriticError(
                f"{label} exceeds the 16 MiB primary-artifact limit",
                kind="resource_limited",
            )
    for path in log_paths:
        if path.stat().st_size > LOG_LIMIT_BYTES:
            raise CriticError("input project log exceeds the 5 MiB limit", kind="resource_limited")

    inherited_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    managed_proxy_endpoint = _managed_loopback_proxy_endpoint(inherited_proxy)
    report = _redact_managed_proxy_details(
        report_path.read_text(encoding="utf-8"), inherited_proxy, managed_proxy_endpoint
    )
    review = _sanitize_json_strings(
        read_json_bounded(review_path, PRIMARY_ARTIFACT_LIMIT_BYTES, "review JSON"),
        inherited_proxy,
        managed_proxy_endpoint,
    )
    coverage = _sanitize_json_strings(
        read_json_bounded(coverage_path, PRIMARY_ARTIFACT_LIMIT_BYTES, "coverage JSON"),
        inherited_proxy,
        managed_proxy_endpoint,
    )
    manifest = _sanitize_json_strings(
        read_json_bounded(manifest_path, PRIMARY_ARTIFACT_LIMIT_BYTES, "run manifest"),
        inherited_proxy,
        managed_proxy_endpoint,
    )
    validate_repository_review(review, prepare)
    validate_coverage_summary(coverage)
    validate_run_manifest(manifest, prepare, args.job_id)
    _validate_command_ledger(manifest, scratch_root)
    coverage_directories = [pathlib.Path(value) for value in args.coverage_dir]
    archive_members = _planned_coverage_archive_members(coverage_directories, scratch_root)
    _validate_coverage_artifact_links(coverage, archive_members)
    _validate_restore_evidence_files(
        manifest, prepare, scratch_root, coverage_directories
    )
    dynamic = review["dynamic_analysis"]
    if dynamic["project_count"] != len(coverage["projects"]):
        raise CriticError("review project_count does not match coverage-summary projects")
    test_commands = sum(1 for command in manifest["commands"] if command["phase"] == "test")
    coverage_commands = sum(1 for command in manifest["commands"] if command["phase"] == "coverage")
    if dynamic["test_run_count"] != test_commands:
        raise CriticError("review test_run_count does not match run-manifest test commands")
    if dynamic["coverage_run_count"] != coverage_commands:
        raise CriticError("review coverage_run_count does not match run-manifest coverage commands")
    manifest["cleanup"] = {"status": "pending"}
    manifest["finished_at"] = None

    for label, value in (
        ("repository-review.json", review),
        ("coverage-summary.json", coverage),
        ("run-manifest.json", manifest),
    ):
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > PRIMARY_ARTIFACT_LIMIT_BYTES:
            raise CriticError(f"{label} exceeds the 16 MiB primary-artifact limit", kind="resource_limited")

    combined_log = ""
    log_truncated = False
    for path in log_paths:
        section = _redact_managed_proxy_details(
            path.read_text(encoding="utf-8", errors="replace"),
            inherited_proxy,
            managed_proxy_endpoint,
        )
        encoded = (combined_log + section + "\n").encode("utf-8")
        if len(encoded) > LOG_LIMIT_BYTES:
            combined_log = encoded[:LOG_LIMIT_BYTES].decode("utf-8", "ignore") + "\n[LOG TRUNCATED]\n"
            log_truncated = True
            break
        combined_log += section + "\n"
    manifest["log_truncated"] = log_truncated

    staging_name = f".repository-review-{args.job_id}.finalizing"
    staging = artifacts_base / staging_name
    if staging.exists() or staging.is_symlink():
        raise CriticError("artifact finalization staging directory already exists")
    staging.mkdir(mode=0o700)
    cleanup_attempted = False
    try:
        atomic_write_text(staging / "repository-review.md", report)
        atomic_write_json(staging / "repository-review.json", review)
        atomic_write_json(staging / "coverage-summary.json", coverage)
        atomic_write_text(staging / "test-run.log", combined_log)
        archive_result = _write_coverage_archive(
            coverage_directories,
            scratch_root,
            staging / "coverage-details.tar.gz",
        )
        cleanup_attempted = True
        try:
            cleanup_result = _cleanup_prepared_job(prepare, workspace, args.job_id, 30)
        except CriticError as exc:
            cleanup_result = {
                "schema_version": SCHEMA_VERSION,
                "job_id": args.job_id,
                "finished_at": utc_now(),
                "duration_seconds": 0.0,
                "source_unchanged": False,
                "source_snapshot_sha256": None,
                "scratch_removed": False,
                "scratch_bytes_before": None,
                "entries_removed": 0,
                "status": "partial",
                "error": redact_text(str(exc)),
            }
        manifest["cleanup"] = cleanup_result
        manifest["finished_at"] = cleanup_result["finished_at"]
        started_at = _parse_timestamp(manifest["started_at"], "run manifest started_at")
        finished_at = _parse_timestamp(manifest["finished_at"], "run manifest finished_at")
        manifest["total_duration_seconds"] = round(
            max(0.0, (finished_at - started_at).total_seconds()), 6
        )
        if cleanup_result["status"] != "complete":
            limitation = "Job scratch cleanup or source-integrity verification was incomplete."
            if limitation not in manifest["limitations"]:
                manifest["limitations"].append(limitation)
            if limitation not in review["limitations"]:
                review["limitations"].append(limitation)
            review["status"] = "partial"
            report += "\n\n## Finalization limitation\n\n" + limitation + "\n"
        validate_repository_review(review, prepare)
        validate_run_manifest(manifest, prepare, args.job_id)
        if len(report.encode("utf-8")) > PRIMARY_ARTIFACT_LIMIT_BYTES:
            raise CriticError("repository-review.md exceeds the 16 MiB limit", kind="resource_limited")
        atomic_write_text(staging / "repository-review.md", report)
        atomic_write_json(staging / "repository-review.json", review)
        atomic_write_json(staging / "run-manifest.json", manifest)
        for label in ("repository-review.json", "coverage-summary.json", "run-manifest.json"):
            if (staging / label).stat().st_size > PRIMARY_ARTIFACT_LIMIT_BYTES:
                raise CriticError(f"{label} exceeds the 16 MiB primary-artifact limit", kind="resource_limited")
        artifact_root.rmdir()
        os.replace(staging, artifact_root)
    except BaseException:
        if not cleanup_attempted and staging.is_dir() and not staging.is_symlink():
            _bounded_remove_tree(artifacts_base, staging_name, 30)
        raise
    expected_names = {
        "repository-review.md",
        "repository-review.json",
        "coverage-summary.json",
        "run-manifest.json",
        "test-run.log",
        "coverage-details.tar.gz",
    }
    actual_names = {path.name for path in artifact_root.iterdir()}
    if actual_names != expected_names:
        raise CriticError("final artifact set does not match the six-file contract")
    result = {
        "schema_version": SCHEMA_VERSION,
        "artifact_root": str(artifact_root),
        "artifacts": sorted(actual_names),
        "coverage_archive": {
            **archive_result,
            "output": str(artifact_root / "coverage-details.tar.gz"),
        },
        "log_truncated": log_truncated,
        "cleanup": cleanup_result,
    }
    print(json.dumps(result, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    prepare = subparsers.add_parser("prepare", help="select, validate, and copy a repository companion")
    prepare.add_argument("--workspace", default="/workspace")
    prepare.add_argument("--job-id", required=True)
    prepare.add_argument("--artifacts", required=True)
    prepare.add_argument("--companion-name")
    prepare.add_argument("--output", required=True)
    prepare.set_defaults(handler=command_prepare)

    verify = subparsers.add_parser("verify-source", help="prove that the source companion was not changed")
    verify.add_argument("--prepare-record", required=True)
    verify.add_argument("--output")
    verify.set_defaults(handler=command_verify_source)

    run = subparsers.add_parser("run", help="run argv without a shell under fixed limits")
    run.add_argument("--id", required=True)
    run.add_argument("--scratch-root", required=True)
    run.add_argument("--cwd", required=True)
    run.add_argument("--phase", choices=("restore", "test", "coverage", "static"), required=True)
    run.add_argument("--ecosystem", choices=("python", "javascript-typescript", "go", "other"), required=True)
    run.add_argument("--timeout", type=int)
    run.add_argument("--total-budget", type=int, default=TOTAL_DYNAMIC_SECONDS)
    run.add_argument("--disk-limit", type=int, default=SCRATCH_LIMIT_BYTES)
    run.add_argument("--log-limit", type=int, default=LOG_LIMIT_BYTES)
    run.add_argument("--ledger")
    run.add_argument("--log")
    run.add_argument("--output")
    run.add_argument("--allow-build-hooks", action="store_true")
    run.add_argument(
        "--restore-mode",
        choices=("locked", "resolved_unlocked"),
        default="locked",
        help="controls read-only dependency semantics in the bounded environment",
    )
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(handler=command_run)

    validate = subparsers.add_parser("validate-dependencies", help="reject unsafe dependency references")
    validate.add_argument("--root", required=True)
    validate.add_argument("--allow-build-hooks", action="store_true")
    validate.add_argument("--output")
    validate.set_defaults(handler=command_validate_dependencies)

    restore = subparsers.add_parser("restore-plan", help="emit a deterministic restore plan")
    restore.add_argument("--root", required=True)
    restore.add_argument(
        "--ecosystem",
        choices=("auto", "python", "javascript-typescript", "go"),
        default="auto",
    )
    restore.add_argument("--allow-unlocked", action="store_true")
    restore.add_argument("--allow-build-hooks", action="store_true")
    restore.add_argument("--output")
    restore.set_defaults(handler=command_restore_plan)

    coverage = subparsers.add_parser(
        "coverage-plan", help="emit generic coverage argv using only baked tooling"
    )
    coverage.add_argument("--root", required=True)
    coverage.add_argument("--scratch-root", required=True)
    coverage.add_argument("--coverage-dir", required=True)
    coverage.add_argument("--ecosystem", choices=("python", "javascript-typescript", "go"), required=True)
    coverage.add_argument("--test-argv-json")
    coverage.add_argument("--output")
    coverage.set_defaults(handler=command_coverage_plan)

    cleanup = subparsers.add_parser("cleanup", help="verify the source and remove exact job scratch")
    cleanup.add_argument("--workspace", default="/workspace")
    cleanup.add_argument("--artifacts", required=True)
    cleanup.add_argument("--job-id", required=True)
    cleanup.add_argument("--prepare-record", required=True)
    cleanup.add_argument("--timeout", type=int, default=30)
    cleanup.set_defaults(handler=command_cleanup)

    normalize = subparsers.add_parser("normalize", help="normalize a native coverage file")
    normalize.add_argument("--format", choices=("coveragepy", "istanbul", "lcov", "go"), required=True)
    normalize.add_argument("--input", required=True)
    normalize.add_argument("--root", required=True)
    normalize.add_argument("--scratch-root", required=True)
    normalize.add_argument("--project-id", required=True)
    normalize.add_argument("--project-root", required=True)
    normalize.add_argument("--ecosystem", choices=("python", "javascript-typescript", "go"), required=True)
    normalize.add_argument("--status", choices=sorted(COVERAGE_STATUSES), default="complete")
    normalize.add_argument("--tests", help="JSON test counts")
    normalize.add_argument("--exclusion", action="append", default=[])
    normalize.add_argument("--limitation", action="append", default=[])
    normalize.add_argument("--native-artifact", action="append", default=[])
    normalize.add_argument("--output", required=True)
    normalize.set_defaults(handler=command_normalize)

    aggregate = subparsers.add_parser("aggregate-coverage", help="assemble coverage-summary.json")
    aggregate.add_argument("--project", action="append", default=[])
    aggregate.add_argument("--high-priority-gap", action="append", default=[])
    aggregate.add_argument("--output", required=True)
    aggregate.set_defaults(handler=command_aggregate_coverage)

    archive = subparsers.add_parser("archive-coverage", help="create a bounded safe native coverage archive")
    archive.add_argument("--scratch-root", required=True)
    archive.add_argument("--coverage-dir", action="append", default=[])
    archive.add_argument("--output", required=True)
    archive.set_defaults(handler=command_archive_coverage)

    finalize = subparsers.add_parser("finalize", help="validate and atomically publish the six artifacts")
    finalize.add_argument("--workspace", default="/workspace")
    finalize.add_argument("--artifacts", required=True)
    finalize.add_argument("--job-id", required=True)
    finalize.add_argument("--prepare-record", required=True)
    finalize.add_argument("--report", required=True)
    finalize.add_argument("--review-json", required=True)
    finalize.add_argument("--coverage-json", required=True)
    finalize.add_argument("--run-manifest", required=True)
    finalize.add_argument("--log", action="append", default=[])
    finalize.add_argument("--coverage-dir", action="append", default=[])
    finalize.set_defaults(handler=command_finalize)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except CriticError as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "review_mode": REVIEW_MODE,
                    "status": "blocked",
                    "kind": exc.kind,
                    "message": redact_text(str(exc)),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "review_mode": REVIEW_MODE,
                    "status": "blocked",
                    "kind": "io_or_parse_error",
                    "message": redact_text(str(exc)),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (ValueError, TypeError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "review_mode": REVIEW_MODE,
                    "status": "blocked",
                    "kind": "invalid_data",
                    "message": redact_text(str(exc)),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
