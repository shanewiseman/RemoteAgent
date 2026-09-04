from __future__ import annotations

import hashlib
import importlib.util
import io
import pathlib
import tarfile
from copy import deepcopy
from types import ModuleType

import pytest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_smoke() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "smoke_repository_critic.py"
    spec = importlib.util.spec_from_file_location("remoteagent_repository_critic_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coverage_archive(*, name: str = "project-1/coverage.json", kind: str = "file") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        else:
            payload = b"{}"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def test_coverage_archive_accepts_only_safe_regular_members() -> None:
    smoke = _load_smoke()

    assert smoke.validate_coverage_archive(_coverage_archive()) == 1
    with pytest.raises(smoke.SmokeFailure, match="unsafe path"):
        smoke.validate_coverage_archive(_coverage_archive(name="../coverage.json"))
    with pytest.raises(smoke.SmokeFailure, match="non-regular"):
        smoke.validate_coverage_archive(_coverage_archive(kind="symlink"))
    with pytest.raises(smoke.SmokeFailure, match="lifecycle sentinel"):
        smoke.validate_coverage_archive(_coverage_archive(name="project-1/LIFECYCLE_RAN"))


def test_unlocked_restore_provenance_is_exact_and_cross_consistent() -> None:
    smoke = _load_smoke()
    manifest_digest = "1" * 64
    integrity = "2" * 64
    manifest = {
        "dependency_restores": [
            {
                "project": ".",
                "ecosystem": "python",
                "manager": "pip",
                "mode": "resolved_unlocked",
                "reproducible": False,
                "dependency_manifest_sha256": manifest_digest,
                "source_url": "https://pypi.org/simple",
                "resolved_dependencies": [
                    {
                        "name": "six",
                        "version": "1.17.0",
                        "source_url": "https://pypi.org/simple",
                        "integrity_sha256": [integrity],
                    }
                ],
                "freeze": ["six==1.17.0"],
                "generated_lock": {
                    "generated": True,
                    "path": ".repository-critic.requirements.lock",
                    "sha256": "3" * 64,
                    "requirements": ["six==1.17.0"],
                    "integrity_sha256": [integrity],
                },
            }
        ]
    }

    assert smoke.validate_unlocked_restore(manifest, manifest_digest) == "1.17.0"
    manifest["dependency_restores"][0]["freeze"] = ["six==1.16.0"]
    with pytest.raises(smoke.SmokeFailure, match="pip-freeze evidence"):
        smoke.validate_unlocked_restore(manifest, manifest_digest)


def test_snapshot_review_requires_state_framing_traceability_and_architecture() -> None:
    smoke = _load_smoke()
    review = {
        "summary": {"verdict": "The current repository snapshot contradicts its contract."},
        "traceability": [
            {
                "claim": (
                    "authorize_sensitive_operation permits only fixture-admin and denies "
                    "every other token, including empty"
                ),
                "status": "contradicted",
                "documentation_evidence": ["README.md"],
                "implementation_evidence": ["review_target/access.py"],
                "test_evidence": ["tests/test_access.py"],
            }
        ],
        "findings": [
            {
                "priority": "P0",
                "title": "Authorization denial is violated",
                "description": (
                    "The implementation allows every token, including empty and "
                    "non-admin values"
                ),
                "evidence": [{"path": "review_target/access.py"}],
            },
            {
                "priority": "P2",
                "title": "Policy-module boundary is absent",
                "description": (
                    "ARCHITECTURE.md requires review_target/policy.py, but policy remains "
                    "in review_target/access.py"
                ),
            },
        ],
    }

    assert len(smoke.validate_snapshot_review("Current-state assessment.", review)) == 1

    missing_traceability = deepcopy(review)
    missing_traceability["traceability"] = []
    with pytest.raises(smoke.SmokeFailure, match="traceability row"):
        smoke.validate_snapshot_review("Current-state assessment.", missing_traceability)

    with pytest.raises(smoke.SmokeFailure, match="forbidden diff framing"):
        smoke.validate_snapshot_review("Diff review of the repository.", review)


def test_high_priority_gap_accepts_semantic_authorization_evidence() -> None:
    smoke = _load_smoke()

    assert smoke.has_high_priority_authorization_coverage_gap(
        [
            "Authorization denial path for non-admin and empty tokens is uncovered "
            "and currently allows access."
        ]
    )
    assert not smoke.has_high_priority_authorization_coverage_gap(
        ["A low-risk utility line is uncovered."]
    )


def test_fixture_test_counts_accept_contractual_nulls_or_consistent_counts() -> None:
    smoke = _load_smoke()

    smoke.validate_optional_fixture_test_counts(
        {"total": None, "passed": None, "failed": None, "skipped": None}
    )
    smoke.validate_optional_fixture_test_counts(
        {"total": 1, "passed": 1, "failed": 0, "skipped": 0}
    )
    with pytest.raises(smoke.SmokeFailure, match="inconsistent nullable"):
        smoke.validate_optional_fixture_test_counts(
            {"total": 1, "passed": None, "failed": 0, "skipped": 0}
        )


def test_execution_contract_requires_exact_toolchains_argv_and_hook_suppression(
    tmp_path: pathlib.Path,
) -> None:
    smoke = _load_smoke()
    scratch = tmp_path / ".repository-critic" / "job"
    repository = scratch / "repository"
    coverage = scratch / "coverage" / "python"
    interpreter = str(repository / ".venv" / "bin" / "python")
    data_file = coverage / ".coverage"

    def command(
        phase: str, argv: list[str], *, restore_mode: str | None = None
    ) -> dict[str, object]:
        return {
            "phase": phase,
            "argv": argv,
            "status": "success",
            "restore_mode": restore_mode,
            "build_hooks_enabled": False,
        }

    commands = [
        command(
            "restore",
            ["uv", "pip", "compile", "requirements.txt"],
            restore_mode="resolved_unlocked",
        ),
        command("test", smoke.DOCUMENTED_TEST_ARGV),
        command(
            "coverage",
            [
                "uv",
                "pip",
                "install",
                "--python",
                interpreter,
                "--offline",
                "--no-index",
                "--find-links",
                "/opt/remoteagent/agent/python-wheelhouse",
                "--require-hashes",
                "--requirement",
                "/opt/remoteagent/agent/coverage-tools.lock",
            ],
        ),
        command(
            "coverage",
            [interpreter, "-m", "coverage", "erase", f"--data-file={data_file}"],
        ),
        command(
            "coverage",
            [
                interpreter,
                "-m",
                "coverage",
                "run",
                "--branch",
                "--source=.",
                f"--data-file={data_file}",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ],
        ),
        command(
            "coverage",
            [
                interpreter,
                "-m",
                "coverage",
                "json",
                f"--data-file={data_file}",
                "-o",
                str(coverage / "coverage.json"),
            ],
        ),
        command(
            "coverage",
            [
                interpreter,
                "-m",
                "coverage",
                "xml",
                f"--data-file={data_file}",
                "-o",
                str(coverage / "coverage.xml"),
            ],
        ),
    ]
    manifest = {
        "toolchains": smoke.checked_in_toolchains(),
        "policy": {
            "network": "public HTTPS restore only",
            "build_hooks": False,
            "locked_restore_required": False,
        },
        "repository": {"scratch_path": str(repository)},
        "commands": commands,
    }

    smoke.validate_execution_contract(manifest)

    hooks_enabled = deepcopy(manifest)
    hooks_enabled["commands"][0]["build_hooks_enabled"] = True
    with pytest.raises(smoke.SmokeFailure, match="enabled lifecycle/build hooks"):
        smoke.validate_execution_contract(hooks_enabled)

    stale_toolchains = deepcopy(manifest)
    stale_toolchains["toolchains"]["runtimes"]["python"] = "0.0.0"
    with pytest.raises(smoke.SmokeFailure, match="toolchains do not exactly match"):
        smoke.validate_execution_contract(stale_toolchains)


def test_fixture_archive_digest_matches_private_admitted_tree(tmp_path: pathlib.Path) -> None:
    smoke = _load_smoke()
    fixture = REPOSITORY_ROOT / "scripts" / "fixtures" / "repository-critic"
    archive = tmp_path / "fixture.tar.gz"
    reference = tmp_path / "reference"

    archive_digest, snapshot_digest = smoke.create_fixture_archive(fixture, archive, reference)

    assert archive_digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert snapshot_digest == smoke.snapshot_sha256(reference)
    assert {path.stat().st_mode & 0o777 for path in reference.rglob("*") if path.is_file()} == {
        0o600
    }
    assert {path.stat().st_mode & 0o777 for path in reference.rglob("*") if path.is_dir()} == {
        0o700
    }


def test_artifact_index_enforces_the_exact_bounded_output_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_smoke()
    job_id = "job-123"

    def item(name: str) -> dict[str, object]:
        return {
            "id": f"artifact-{name}",
            "relative_path": f"repository-review-{job_id}/{name}",
            "size_bytes": 2,
            "sha256": "a" * 64,
        }

    response = [item(name) for name in smoke.EXPECTED_ARTIFACTS]
    monkeypatch.setattr(smoke, "request_json", lambda *args, **kwargs: response)
    assert set(smoke.artifact_index("http://router", "token", job_id)) == (smoke.EXPECTED_ARTIFACTS)

    response.append(item("unexpected.txt"))
    with pytest.raises(smoke.SmokeFailure, match="exactly 6 artifacts"):
        smoke.artifact_index("http://router", "token", job_id)

    response.pop()
    oversized = next(
        entry for entry in response if str(entry["relative_path"]).endswith("/repository-review.md")
    )
    oversized["size_bytes"] = smoke.MAX_PRIMARY_ARTIFACT_BYTES + 1
    with pytest.raises(smoke.SmokeFailure, match="exceeds"):
        smoke.artifact_index("http://router", "token", job_id)


def test_discovery_requires_deployed_detail_to_match_checked_in_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_smoke()
    contract = smoke.checked_in_agent_contract()
    detail = {key: deepcopy(value) for key, value in contract.items() if key != "compose_suffix"}
    detail["compose_file"] = f"/deployment{contract['compose_suffix']}"
    detail["revision"] = 3

    def request_json(
        _base_url: str,
        _token: str,
        _method: str,
        path: str,
        *_args: object,
        **_kwargs: object,
    ) -> object:
        if path == "/api/v1/agents":
            return [
                {
                    "id": "repository-critic",
                    "enabled": True,
                    "revision": 3,
                }
            ]
        assert path == "/api/v1/agents/repository-critic"
        return detail

    monkeypatch.setattr(smoke, "request_json", request_json)
    smoke.discover_agent("http://router", "token")

    detail["metadata"]["toolchain_revision"] = "stale"
    with pytest.raises(smoke.SmokeFailure, match="metadata does not match"):
        smoke.discover_agent("http://router", "token")


def test_failed_smoke_reports_the_uploaded_stage_identifier(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke = _load_smoke()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REMOTEAGENT_SMOKE_URL", "http://router")
    monkeypatch.setenv("REMOTEAGENT_SMOKE_TOKEN", "token")
    monkeypatch.setattr(smoke, "discover_agent", lambda *args: None)
    monkeypatch.setattr(smoke, "upload_fixture", lambda *args: "stage-retained")

    def fail_submission(*args: object) -> dict[str, object]:
        raise smoke.SmokeFailure("submission failed")

    monkeypatch.setattr(smoke, "submit_review", fail_submission)

    with pytest.raises(smoke.SmokeFailure, match="submission failed"):
        smoke.main()
    assert "stage=stage-retained" in capsys.readouterr().err
