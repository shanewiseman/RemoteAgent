from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
import venv


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = PACKAGE_ROOT / "tools" / "repository_critic.py"
SPEC = importlib.util.spec_from_file_location("repository_critic_tools", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
critic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = critic
SPEC.loader.exec_module(critic)


def namespace(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


class PreparationTests(unittest.TestCase):
    def test_relative_link_stays_in_scratch_and_source_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            artifacts = base / "artifacts"
            repository = workspace / "companions" / "repository"
            repository.mkdir(parents=True)
            artifacts.mkdir()
            (repository / "target.txt").write_text("source\n", encoding="utf-8")
            (repository / "link.txt").symlink_to("target.txt")
            output = workspace / ".repository-critic" / "job-1" / "prepare.json"
            result = critic.command_prepare(
                namespace(
                    workspace=str(workspace),
                    artifacts=str(artifacts),
                    job_id="job-1",
                    companion_name=None,
                    output=str(output),
                )
            )
            self.assertEqual(result, 0)
            prepared = critic.read_json(output)
            scratch_link = Path(prepared["repository_path"]) / "link.txt"
            scratch_link.write_text("scratch\n", encoding="utf-8")
            self.assertEqual((repository / "target.txt").read_text(encoding="utf-8"), "source\n")
            self.assertEqual(critic.command_verify_source(namespace(prepare_record=str(output), output=None)), 0)

    def test_absolute_link_is_rejected_before_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            artifacts = base / "artifacts"
            repository = workspace / "companions" / "repository"
            repository.mkdir(parents=True)
            artifacts.mkdir()
            target = repository / "target.txt"
            target.write_text("source\n", encoding="utf-8")
            (repository / "absolute").symlink_to(target)
            with self.assertRaisesRegex(critic.CriticError, "absolute symlink"):
                critic.command_prepare(
                    namespace(
                        workspace=str(workspace),
                        artifacts=str(artifacts),
                        job_id="job-2",
                        companion_name=None,
                        output=str(workspace / ".repository-critic" / "job-2" / "prepare.json"),
                    )
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "source\n")
            self.assertFalse((workspace / ".repository-critic" / "job-2").exists())

    def test_selection_prefers_repository_then_only_companion(self) -> None:
        first = Path("/tmp/first")
        second = Path("/tmp/second")
        self.assertEqual(critic.select_companion({"other": first}, None), ("other", first))
        self.assertEqual(
            critic.select_companion({"other": first, "repository": second}, None),
            ("repository", second),
        )
        with self.assertRaises(critic.CriticError):
            critic.select_companion({"first": first, "second": second}, None)

    def test_ambiguous_selection_publishes_complete_blocked_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            artifacts = base / "artifacts"
            for name in ("alpha", "beta"):
                companion = workspace / "companions" / name
                companion.mkdir(parents=True)
                (companion / "source.txt").write_text(name, encoding="utf-8")
            artifacts.mkdir()
            result = critic.command_prepare(
                namespace(
                    workspace=str(workspace),
                    artifacts=str(artifacts),
                    job_id="ambiguous",
                    companion_name=None,
                    output=str(workspace / ".repository-critic" / "ambiguous" / "prepare.json"),
                )
            )
            self.assertEqual(result, 2)
            published = artifacts / "repository-review-ambiguous"
            self.assertEqual(
                {path.name for path in published.iterdir()},
                {
                    "repository-review.md",
                    "repository-review.json",
                    "coverage-summary.json",
                    "run-manifest.json",
                    "test-run.log",
                    "coverage-details.tar.gz",
                },
            )
            review = critic.read_json(published / "repository-review.json")
            manifest = critic.read_json(published / "run-manifest.json")
            self.assertEqual(review["status"], "blocked")
            self.assertEqual(review["repository"]["companion_name"], None)
            self.assertEqual(manifest["limits"]["max_artifacts"], 6)
            self.assertEqual(manifest["cleanup"]["status"], "complete")


class DependencyPlanTests(unittest.TestCase):
    def test_unsafe_dependency_sources_are_rejected(self) -> None:
        samples = {
            "requirements.txt": (
                "-r child/../../../outside.txt\n--find-links file:///etc\n"
                "escape @ ../outside\n/absolute/dependency\n"
            ),
            "pyproject.toml": (
                "[project]\nname='fixture'\nversion='1'\n"
                "dependencies=['escape @ ../outside']\n"
                "[[tool.poetry.source]]\nname='bad'\nurl='http://packages.example/simple'\n"
            ),
            "Pipfile": "[[source]]\nurl='http://packages.example/simple'\nverify_ssl=false\n",
            "package.json": json.dumps(
                {
                    "dependencies": {
                        "escape": "../outside",
                        "absolute": "/absolute/dependency",
                        "shorthand": "owner/repository",
                    }
                }
            ),
            "package-lock.json": json.dumps(
                {"packages": {"node_modules/x": {"resolved": "http://example.test/x.tgz"}}}
            ),
            "uv.lock": '[[package]]\nname="escape"\nsource={directory="../../outside"}\n',
            "go.work": "go 1.27\nuse ../../outside\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            for name, content in samples.items():
                (root / name).write_text(content, encoding="utf-8")
            result = critic.validate_dependencies(root)
            self.assertEqual(result["status"], "blocked")
            messages = "\n".join(issue["message"] for issue in result["issues"])
            self.assertIn("escapes repository", messages)
            self.assertIn("insecure HTTP", messages)
            self.assertIn("verify TLS", messages)

    def test_package_manager_code_hooks_require_explicit_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".yarnrc.yml").write_text(
                "yarnPath: .yarn/releases/repository-controlled.cjs\nplugins:\n  - path: .yarn/plugin.cjs\n",
                encoding="utf-8",
            )
            (root / ".pnpmfile.cjs").write_text("module.exports = {}\n", encoding="utf-8")
            blocked = critic.validate_dependencies(root)
            self.assertEqual(blocked["status"], "blocked")
            self.assertTrue(any("authorization" in issue["message"] for issue in blocked["issues"]))
            allowed = critic.validate_dependencies(root, allow_build_hooks=True)
            self.assertEqual(allowed["status"], "valid")

    def test_multiline_hashed_requirements_are_locked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text(
                "example==1.2.3 \\\n+    --hash=sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
            plan = critic._python_restore_plan(root, False, False)
            self.assertEqual(plan["mode"], "locked")
            self.assertEqual(plan["lockfile"], "requirements.txt")
            self.assertEqual(plan["commands"][0], ["python3.12", "-m", "venv", ".venv"])

    def test_dependency_free_python_creates_only_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan = critic._python_restore_plan(Path(temporary), False, False)
            self.assertEqual(plan["mode"], "locked")
            self.assertEqual(plan["manager"], "python-venv")
            self.assertEqual(plan["commands"], [["python3.12", "-m", "venv", ".venv"]])

    def test_unlocked_pipenv_and_yarn4_suppress_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Pipfile").write_text("[packages]\nrequests='*'\n", encoding="utf-8")
            pipenv = critic._python_restore_plan(root, True, False)
            self.assertEqual(pipenv["mode"], "resolved_unlocked")
            self.assertEqual(pipenv["commands"][0], ["pipenv", "lock"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                json.dumps({"packageManager": "yarn@4.18.0"}), encoding="utf-8"
            )
            (root / "yarn.lock").write_text("__metadata:\n  version: 8\n", encoding="utf-8")
            yarn = critic._javascript_restore_plan(root, False, False)
            command = yarn["commands"][0]
            self.assertIn("--immutable", command)
            self.assertIn("--mode=skip-build", command)
            self.assertNotIn("--ignore-scripts", command)


class ExecutionTests(unittest.TestCase):
    def test_command_ledger_cannot_be_relocated_or_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            with self.assertRaisesRegex(critic.CriticError, "custom dynamic budget"):
                critic.command_run(
                    namespace(
                        id="custom-ledger",
                        scratch_root=str(scratch),
                        cwd=str(scratch),
                        phase="test",
                        ecosystem="other",
                        timeout=1,
                        total_budget=1,
                        disk_limit=critic.SCRATCH_LIMIT_BYTES,
                        log_limit=critic.LOG_LIMIT_BYTES,
                        ledger=str(scratch / "other-ledger.json"),
                        log=None,
                        output=None,
                        allow_build_hooks=False,
                        restore_mode="locked",
                        command=["--", "true"],
                    )
                )
            critic.atomic_write_json(
                scratch / ".critic-budget.json",
                {
                    "schema_version": 1,
                    "spent_seconds": 1.0,
                    "runs": [{"duration_seconds": 1.0}],
                },
            )
            with self.assertRaisesRegex(critic.CriticError, "do not exactly match"):
                critic._validate_command_ledger({"commands": []}, scratch)

    def test_test_phase_resolves_python_from_confined_project_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            venv.EnvBuilder(with_pip=False).create(scratch / ".venv")
            log = scratch / "venv.log"
            output = scratch / "venv.json"
            result = critic.command_run(
                namespace(
                    id="venv",
                    scratch_root=str(scratch),
                    cwd=str(scratch),
                    phase="test",
                    ecosystem="python",
                    timeout=10,
                    total_budget=30,
                    disk_limit=critic.SCRATCH_LIMIT_BYTES,
                    log_limit=critic.LOG_LIMIT_BYTES,
                    ledger=None,
                    log=str(log),
                    output=str(output),
                    allow_build_hooks=False,
                    restore_mode="locked",
                    command=["--", "python", "-c", "import sys; print(sys.prefix)"],
                )
            )
            self.assertEqual(result, 0)
            self.assertIn(str(scratch / ".venv"), log.read_text(encoding="utf-8"))
            self.assertIsNone(critic.read_json(output)["restore_mode"])

    def test_test_phase_clears_proxy_and_redacts_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            log = scratch / "command.log"
            output = scratch / "record.json"
            previous_proxy = os.environ.get("HTTPS_PROXY")
            previous_secret = os.environ.get("EXAMPLE_TOKEN")
            os.environ["HTTPS_PROXY"] = "http://proxy.example:1234"
            os.environ["EXAMPLE_TOKEN"] = "do-not-copy"
            try:
                rc = critic.command_run(
                    namespace(
                        id="env",
                        scratch_root=str(scratch),
                        cwd=str(scratch),
                        phase="test",
                        ecosystem="python",
                        timeout=10,
                        total_budget=30,
                        disk_limit=critic.SCRATCH_LIMIT_BYTES,
                        log_limit=critic.LOG_LIMIT_BYTES,
                        ledger=None,
                        log=str(log),
                        output=str(output),
                        allow_build_hooks=False,
                        restore_mode="locked",
                        command=[
                            "--",
                            sys.executable,
                            "-c",
                            (
                                "import os; print(os.getenv('HTTPS_PROXY')); "
                                "print(os.getenv('EXAMPLE_TOKEN')); print('password=visible-secret')"
                            ),
                        ],
                    )
                )
            finally:
                if previous_proxy is None:
                    os.environ.pop("HTTPS_PROXY", None)
                else:
                    os.environ["HTTPS_PROXY"] = previous_proxy
                if previous_secret is None:
                    os.environ.pop("EXAMPLE_TOKEN", None)
                else:
                    os.environ["EXAMPLE_TOKEN"] = previous_secret
            self.assertEqual(rc, 0)
            text = log.read_text(encoding="utf-8")
            self.assertNotIn("visible-secret", text)
            self.assertNotIn("do-not-copy", text)
            self.assertNotIn("proxy.example", text)
            self.assertIn("[REDACTED]", text)

    def test_term_ignoring_descendant_is_killed_and_run_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            pid_path = scratch / "child.pid"
            old_grace = critic.TERMINATE_GRACE_SECONDS
            critic.TERMINATE_GRACE_SECONDS = 0.2
            try:
                rc = critic.command_run(
                    namespace(
                        id="daemon",
                        scratch_root=str(scratch),
                        cwd=str(scratch),
                        phase="test",
                        ecosystem="python",
                        timeout=5,
                        total_budget=30,
                        disk_limit=critic.SCRATCH_LIMIT_BYTES,
                        log_limit=critic.LOG_LIMIT_BYTES,
                        ledger=None,
                        log=None,
                        output=str(scratch / "record.json"),
                        allow_build_hooks=False,
                        restore_mode="locked",
                        command=[
                            "--",
                            sys.executable,
                            "-c",
                            (
                                "import os,signal,subprocess; "
                                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                "p=subprocess.Popen(['sleep','30']); "
                                f"open({str(pid_path)!r},'w').write(str(p.pid)); os._exit(0)"
                            ),
                        ],
                    )
                )
            finally:
                critic.TERMINATE_GRACE_SECONDS = old_grace
            self.assertEqual(rc, 1)
            record = critic.read_json(scratch / "record.json")
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["signal"], "SIGKILL_DESCENDANTS")
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)


class CoverageAndArtifactTests(unittest.TestCase):
    def test_structured_artifact_validators_reject_unknown_fields(self) -> None:
        project = {
            "id": "python",
            "root": ".",
            "ecosystem": "python",
            "status": "complete",
            "tests": {"passed": 1, "failed": 0, "skipped": 0, "total": 1},
            "lines": critic.metric(1, 1),
            "branches": None,
            "functions": None,
            "metric_basis": "executable_lines",
            "zero_coverage_files": [],
            "exclusions": [],
            "limitations": [],
            "native_artifacts": ["coverage-details.tar.gz#project-1/coverage.json"],
            "stdout": "must not be smuggled",
        }
        with self.assertRaisesRegex(critic.CriticError, "unsupported stdout"):
            critic.validate_project_coverage(project)
        project.pop("stdout")
        with self.assertRaisesRegex(critic.CriticError, "does not name an archived"):
            critic._validate_coverage_artifact_links({"projects": [project]}, set())
        project["native_artifacts"] = []
        with self.assertRaisesRegex(critic.CriticError, "at least one native"):
            critic._validate_coverage_artifact_links({"projects": [project]}, set())

    def test_coverage_plans_include_python_sources_and_instrument_custom_go_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            python_root = scratch / "python"
            interpreter = python_root / ".venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")
            python_plan_path = scratch / "python-plan.json"
            critic.command_coverage_plan(
                namespace(
                    root=str(python_root),
                    scratch_root=str(scratch),
                    coverage_dir=str(scratch / "coverage" / "python"),
                    ecosystem="python",
                    test_argv_json=None,
                    output=str(python_plan_path),
                )
            )
            python_plan = critic.read_json(python_plan_path)
            self.assertIn("--source=.", python_plan["commands"][2])

            go_root = scratch / "go"
            go_root.mkdir()
            (go_root / "go.mod").write_text("module example.test/fixture\n\ngo 1.27\n")
            go_plan_path = scratch / "go-plan.json"
            critic.command_coverage_plan(
                namespace(
                    root=str(go_root),
                    scratch_root=str(scratch),
                    coverage_dir=str(scratch / "coverage" / "go"),
                    ecosystem="go",
                    test_argv_json=json.dumps(["go", "test", "./pkg"]),
                    output=str(go_plan_path),
                )
            )
            go_command = critic.read_json(go_plan_path)["commands"][0]
            self.assertEqual(go_command.count("-coverpkg=./..."), 1)
            self.assertTrue(any(item.startswith("-coverprofile=") for item in go_command))
            self.assertEqual(go_command[-1], "./pkg")

    def test_normalizers_reject_external_empty_and_inconsistent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            root = scratch / "repository"
            root.mkdir()
            inconsistent = scratch / "inconsistent.json"
            inconsistent.write_text(
                json.dumps(
                    {
                        "totals": {"covered_lines": 1, "num_statements": 2},
                        "files": {
                            "inside.py": {
                                "summary": {"covered_lines": 1, "num_statements": 1}
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(critic.CriticError, "totals disagree"):
                critic._coveragepy(inconsistent, root)

            outside = scratch / "outside.json"
            outside.write_text(
                json.dumps(
                    {
                        "totals": {"covered_lines": 1, "num_statements": 1},
                        "files": {
                            "/tmp/outside.py": {
                                "summary": {"covered_lines": 1, "num_statements": 1}
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(critic.CriticError, "outside the reviewed project"):
                critic._coveragepy(outside, root)

            for payload, normalizer in (
                (
                    json.dumps(
                        {
                            "/tmp/outside.js": {
                                "statementMap": {"0": {"start": {"line": 1}}},
                                "s": {"0": 1},
                            }
                        }
                    ),
                    critic._istanbul,
                ),
                ("SF:/tmp/outside.js\nLF:1\nLH:1\nend_of_record\n", critic._lcov),
                ("mode: atomic\n/tmp/outside.go:1.1,2.1 1 1\n", critic._go_cover),
            ):
                path = scratch / f"external-{normalizer.__name__}"
                path.write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(critic.CriticError, "outside the reviewed project"):
                    normalizer(path, root)

            empty_project = {
                "id": "empty",
                "root": ".",
                "ecosystem": "python",
                "status": "complete",
                "tests": {"passed": 0, "failed": 0, "skipped": 0, "total": 0},
                "lines": critic.metric(0, 0),
                "branches": None,
                "functions": None,
                "metric_basis": "executable_lines",
                "zero_coverage_files": [],
                "exclusions": [],
                "limitations": [],
                "native_artifacts": [],
            }
            with self.assertRaisesRegex(critic.CriticError, "positive executable-line"):
                critic.validate_project_coverage(empty_project)

    def test_normalizers_keep_metric_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coverage = root / "coverage.json"
            coverage.write_text(
                json.dumps(
                    {
                        "totals": {
                            "covered_lines": 3,
                            "num_statements": 4,
                            "covered_branches": 1,
                            "num_branches": 2,
                        },
                        "files": {
                            "src/hit.py": {"summary": {"covered_lines": 3, "num_statements": 3}},
                            "src/zero.py": {"summary": {"covered_lines": 0, "num_statements": 1}},
                        },
                    }
                ),
                encoding="utf-8",
            )
            normalized = critic._coveragepy(coverage, root)
            self.assertEqual(normalized["lines"], {"covered": 3, "total": 4, "percent": 75.0})
            self.assertEqual(normalized["branches"]["percent"], 50.0)
            self.assertEqual(normalized["zero_coverage_files"], ["src/zero.py"])

            go = root / "coverage.out"
            go.write_text(
                "mode: atomic\nexample/a.go:1.1,2.1 2 1\nexample/b.go:1.1,2.1 1 0\n",
                encoding="utf-8",
            )
            go_result = critic._go_cover(go, root)
            self.assertEqual(go_result["lines"], {"covered": 2, "total": 3, "percent": 66.6667})
            self.assertIsNone(go_result["branches"])
            self.assertIsNone(go_result["functions"])

    def test_archive_is_repeatable_and_refuses_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            coverage = scratch / "coverage"
            coverage.mkdir()
            (coverage / "coverage.json").write_text('{"ok":true}\n', encoding="utf-8")
            first = scratch / "first.tar.gz"
            second = scratch / "second.tar.gz"
            critic._write_coverage_archive([coverage], scratch, first)
            critic._write_coverage_archive([coverage], scratch, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            (coverage / "escape").symlink_to("/etc/passwd")
            with self.assertRaises(critic.CriticError):
                critic._write_coverage_archive([coverage], scratch, scratch / "bad.tar.gz")

    def test_finalize_publishes_exact_contract_and_embeds_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            artifacts = base / "artifacts"
            repository = workspace / "companions" / "repository"
            repository.mkdir(parents=True)
            artifacts.mkdir()
            (repository / "README.md").write_text("documented\n", encoding="utf-8")
            requirements_text = "example\n"
            (repository / "requirements.txt").write_text(
                requirements_text, encoding="utf-8"
            )
            prepare_path = workspace / ".repository-critic" / "job-final" / "prepare.json"
            critic.command_prepare(
                namespace(
                    workspace=str(workspace),
                    artifacts=str(artifacts),
                    job_id="job-final",
                    companion_name=None,
                    output=str(prepare_path),
                )
            )
            prepare = critic.read_json(prepare_path)
            scratch = Path(prepare["scratch_root"])
            integrity = "a" * 64
            lock_text = f"example==1.2.3 \\\n    --hash=sha256:{integrity}\n"
            lock_name = ".repository-critic.requirements.lock"
            scratch_lock = Path(prepare["repository_path"]) / lock_name
            scratch_lock.write_text(lock_text, encoding="utf-8")
            provenance_dir = scratch / "coverage" / "python"
            provenance_dir.mkdir(parents=True)
            (provenance_dir / lock_name).write_text(lock_text, encoding="utf-8")
            restore_log = scratch / "restore.log"
            restore_record = scratch / "restore.json"
            self.assertEqual(
                critic.command_run(
                    namespace(
                        id="python-restore",
                        scratch_root=str(scratch),
                        cwd=prepare["repository_path"],
                        phase="restore",
                        ecosystem="python",
                        timeout=10,
                        total_budget=30,
                        disk_limit=critic.SCRATCH_LIMIT_BYTES,
                        log_limit=critic.LOG_LIMIT_BYTES,
                        ledger=None,
                        log=str(restore_log),
                        output=str(restore_record),
                        allow_build_hooks=False,
                        restore_mode="resolved_unlocked",
                        command=["--", "true"],
                    )
                ),
                0,
            )
            drafts = scratch / "drafts"
            drafts.mkdir()
            report_path = drafts / "report.md"
            report_path.write_text("# Review\n\npassword=must-not-publish\n", encoding="utf-8")
            repository_record = {
                "companion_name": prepare["companion_name"],
                "source_path": prepare["source_path"],
                "scratch_path": prepare["repository_path"],
                "snapshot_sha256": prepare["source_snapshot_sha256"],
                "git_commit": prepare["git_commit"],
                "git_dirty": prepare["git_dirty"],
            }
            review = {
                "schema_version": 1,
                "review_mode": "repository_snapshot",
                "status": "complete",
                "repository": repository_record,
                "summary": {
                    "verdict": "No material findings.",
                    "p0_count": 0,
                    "p1_count": 0,
                    "p2_count": 0,
                    "p3_count": 0,
                },
                "traceability": [
                    {
                        "claim_id": "DOC-1",
                        "claim": "The README is repository documentation.",
                        "authority": "README.md",
                        "status": "verified",
                        "documentation_evidence": ["README.md:1"],
                        "implementation_evidence": ["README.md:1"],
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
            }
            coverage = {
                "schema_version": 1,
                "review_mode": "repository_snapshot",
                "status": "no_tests",
                "projects": [],
                "high_priority_gaps": [],
            }
            toolchains = json.loads((PACKAGE_ROOT / "toolchain-manifest.json").read_text(encoding="utf-8"))
            run_manifest = {
                "schema_version": 1,
                "review_mode": "repository_snapshot",
                "job_id": "job-final",
                "started_at": prepare["prepared_at"],
                "finished_at": None,
                "total_duration_seconds": 0,
                "repository": {
                    "companion_name": prepare["companion_name"],
                    "source_path": prepare["source_path"],
                    "scratch_path": prepare["repository_path"],
                    "snapshot_sha256": prepare["source_snapshot_sha256"],
                    "git_commit": prepare["git_commit"],
                    "git_dirty": prepare["git_dirty"],
                },
                "policy": {
                    "network": "restore-only managed public-host proxy",
                    "build_hooks": False,
                    "locked_restore_required": False,
                },
                "toolchains": toolchains,
                "commands": critic.read_json(scratch / ".critic-budget.json")["runs"],
                "dependency_restores": [
                    {
                        "project": ".",
                        "ecosystem": "python",
                        "manager": "pip",
                        "mode": "resolved_unlocked",
                        "reproducible": False,
                        "dependency_manifest_sha256": hashlib.sha256(
                            requirements_text.encode("utf-8")
                        ).hexdigest(),
                        "source_url": "https://pypi.org/simple",
                        "resolved_dependencies": [
                            {
                                "name": "example",
                                "version": "1.2.3",
                                "source_url": "https://pypi.org/simple",
                                "integrity_sha256": [integrity],
                            }
                        ],
                        "freeze": ["example==1.2.3"],
                        "generated_lock": {
                            "generated": True,
                            "path": lock_name,
                            "sha256": hashlib.sha256(lock_text.encode("utf-8")).hexdigest(),
                            "requirements": ["example==1.2.3"],
                            "integrity_sha256": [integrity],
                        },
                    }
                ],
                "limits": {
                    "max_project_roots": 6,
                    "restore_timeout_seconds": 600,
                    "test_timeout_seconds": 1200,
                    "total_dynamic_seconds": 2700,
                    "scratch_limit_bytes": 2147483648,
                    "log_limit_bytes": 5242880,
                    "native_coverage_limit_bytes": 26214400,
                    "max_artifacts": 6,
                },
                "cleanup": {"status": "pending"},
                "limitations": [],
            }
            tampered_manifest = json.loads(json.dumps(run_manifest))
            tampered_manifest["toolchains"]["runtimes"]["python"] = "0.0.0"
            with self.assertRaisesRegex(critic.CriticError, "baked toolchain"):
                critic.validate_run_manifest(tampered_manifest, prepare, "job-final")
            tampered_review = {**review, "environment": {"TOKEN": "unsafe"}}
            with self.assertRaisesRegex(critic.CriticError, "unsupported fields"):
                critic.validate_repository_review(tampered_review, prepare)
            with self.assertRaisesRegex(critic.CriticError, "requires traceability"):
                critic.validate_repository_review({**review, "traceability": []}, prepare)
            review_path = drafts / "review.json"
            coverage_path = drafts / "coverage.json"
            manifest_path = drafts / "run.json"
            log_path = drafts / "run.log"
            critic.atomic_write_json(review_path, review)
            critic.atomic_write_json(coverage_path, coverage)
            critic.atomic_write_json(manifest_path, run_manifest)
            log_path.write_text("token=must-not-publish\n", encoding="utf-8")
            result = critic.command_finalize(
                namespace(
                    workspace=str(workspace),
                    artifacts=str(artifacts),
                    job_id="job-final",
                    prepare_record=str(prepare_path),
                    report=str(report_path),
                    review_json=str(review_path),
                    coverage_json=str(coverage_path),
                    run_manifest=str(manifest_path),
                    log=[str(restore_log), str(log_path)],
                    coverage_dir=[str(provenance_dir)],
                )
            )
            self.assertEqual(result, 0)
            self.assertFalse(scratch.exists())
            published = artifacts / "repository-review-job-final"
            self.assertEqual(
                {path.name for path in published.iterdir()},
                {
                    "repository-review.md",
                    "repository-review.json",
                    "coverage-summary.json",
                    "run-manifest.json",
                    "test-run.log",
                    "coverage-details.tar.gz",
                },
            )
            self.assertNotIn("must-not-publish", (published / "repository-review.md").read_text())
            self.assertNotIn("must-not-publish", (published / "test-run.log").read_text())
            final_manifest = critic.read_json(published / "run-manifest.json")
            self.assertEqual(final_manifest["cleanup"]["status"], "complete")
            self.assertTrue(final_manifest["cleanup"]["source_unchanged"])
            self.assertIsNotNone(final_manifest["finished_at"])


class BuildInputsTests(unittest.TestCase):
    def test_tool_locks_are_hashed_and_node_lock_has_integrities(self) -> None:
        self.assertTrue(critic._requirements_are_hashed(PACKAGE_ROOT / "python-tools.lock"))
        self.assertTrue(critic._requirements_are_hashed(PACKAGE_ROOT / "coverage-tools.lock"))
        node_lock = json.loads(
            (PACKAGE_ROOT / "node-tools" / "package-lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(node_lock["lockfileVersion"], 3)
        missing = [
            name
            for name, value in node_lock["packages"].items()
            if name and not value.get("link") and "integrity" not in value
        ]
        self.assertEqual(missing, [])
        dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("--require-hashes", dockerfile)
        self.assertIn("ci --engine-strict --ignore-scripts", dockerfile)
        self.assertIn("@sha256:", dockerfile)

    def test_manifest_docker_and_agent_toolchain_metadata_are_exactly_aligned(self) -> None:
        manifest = json.loads(
            (PACKAGE_ROOT / "toolchain-manifest.json").read_text(encoding="utf-8")
        )
        with (PACKAGE_ROOT / "agent.toml").open("rb") as handle:
            agent = tomllib.load(handle)
        dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")
        node_package = json.loads(
            (PACKAGE_ROOT / "node-tools" / "package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(agent["metadata"]["toolchain_revision"], manifest["revision"])
        for image in manifest["source_images"].values():
            self.assertIn(image, dockerfile)
        for version in manifest["runtimes"].values():
            self.assertIn(str(version), dockerfile)
        for version in manifest["support_runtimes"].values():
            self.assertIn(str(version), dockerfile)
        for version in manifest["coverage_tools"].values():
            if str(version).startswith("go"):
                continue
            self.assertIn(str(version), dockerfile)
        for manager, versions in manifest["package_managers"].items():
            for version in versions:
                self.assertIn(str(version), dockerfile, manager)
        self.assertEqual(
            node_package["dependencies"], {"c8": "12.0.0", "corepack": "0.36.0"}
        )
        self.assertIn("COREPACK_ENABLE_NETWORK=0", dockerfile)
        self.assertIn("corepack-shim", dockerfile)
        self.assertIn("--source=.", dockerfile)
        self.assertIn("-covermode=atomic", dockerfile)


if __name__ == "__main__":
    unittest.main()
