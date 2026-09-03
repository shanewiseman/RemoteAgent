from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .schemas import AgentDefinition


@dataclass(frozen=True, slots=True)
class ConversationPaths:
    root: Path
    workspace: Path
    sessions: Path
    artifacts: Path
    control: Path
    jobs: Path
    inputs: Path
    companion_objects: Path
    companion_worktrees: Path
    companion_aliases: Path

    def job_output(self, job_id: str) -> Path:
        return self.jobs / job_id / "final.txt"


class WorkspaceManager:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "conversations"

    def paths(self, conversation_key: str) -> ConversationPaths:
        root = self.root / conversation_key
        workspace = root / "workspace"
        return ConversationPaths(
            root=root,
            workspace=workspace,
            sessions=root / "sessions",
            artifacts=root / "artifacts",
            control=root / "control",
            jobs=workspace / ".remoteagent" / "jobs",
            inputs=root / "inputs",
            companion_objects=root / "inputs" / "objects",
            companion_worktrees=workspace / ".remoteagent" / "companions",
            companion_aliases=workspace / "companions",
        )

    def ensure_conversation(self, conversation_key: str) -> ConversationPaths:
        paths = self.paths(conversation_key)
        for path in (
            paths.root,
            paths.workspace,
            paths.sessions,
            paths.artifacts,
            paths.control,
            paths.workspace / ".remoteagent",
            paths.jobs,
            paths.inputs,
            paths.companion_objects,
            paths.companion_worktrees,
            paths.companion_aliases,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        # The artifact directory is mounted over this location by Compose. A
        # host symlink would cross the router's storage boundary, so keep the
        # host workspace location as a harmless directory.
        (paths.workspace / "artifacts").mkdir(mode=0o700, exist_ok=True)
        return paths

    def materialize_turn(
        self,
        conversation_key: str,
        job_id: str,
        definition: AgentDefinition,
    ) -> tuple[ConversationPaths, Path]:
        paths = self.ensure_conversation(conversation_key)
        context_path = paths.control / "AGENTS.md"
        context_path.write_text(definition.base_context, encoding="utf-8")
        os.chmod(context_path, 0o600)
        config = definition.config_toml.rstrip()
        if "cli_auth_credentials_store" not in tomllib.loads(config or ""):
            config = f'{config}\ncli_auth_credentials_store = "file"'.lstrip()
        config_path = paths.control / "config.toml"
        config_path.write_text(config + "\n", encoding="utf-8")
        os.chmod(config_path, 0o600)
        job_directory = paths.jobs / job_id
        job_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        output = job_directory / "final.txt"
        output.unlink(missing_ok=True)
        return paths, output

    def remove_conversation(self, conversation_key: str) -> None:
        target = self.paths(conversation_key).root.resolve()
        root = self.root.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("conversation cleanup escaped storage root") from exc
        if target.exists():
            import shutil

            shutil.rmtree(target)
