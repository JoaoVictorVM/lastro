"""Projeto Codex real em disco e Codex falso como processo, sem inferência."""

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from lastro.codex.models import CodexInstall, CodexResult, LaunchMode
from lastro.config.loader import ProjectContext, load_project
from lastro.history import HistoryStore, RunRecord, TextMission, open_history
from lastro.scaffold.files import InitAnswers, write_scaffold

FAKE = Path(__file__).resolve().parents[1] / "fake_codex.py"
PROJECT_ID = "3f0c9a52-6b1e-4d8e-9a77-2a4f1c0d5e61"


def codex_block(record: RunRecord) -> dict[str, Any]:
    return cast(dict[str, Any], record.config_snapshot["codex"])


@dataclass
class CodexProject:
    directory: Path
    bin_dir: Path
    outputs: list[str]

    def behave(self, **values: Any) -> None:
        (self.bin_dir / "behavior.json").write_text(json.dumps(values), encoding="utf-8")

    @property
    def install(self) -> CodexInstall:
        return CodexInstall(
            command=(sys.executable, str(self.bin_dir / "fake_codex.py")), version="0.166.0"
        )

    def context(self) -> ProjectContext:
        return load_project(self.directory / "lastro.toml", "local")

    def history(self) -> HistoryStore:
        return open_history(self.context())

    def prepared(self) -> Any:
        from lastro.codex.launcher import prepare_codex

        return prepare_codex(self.context(), probe=lambda: self.install)

    def create_run(self) -> str:
        from lastro.codex.launcher import executor_snapshot
        from lastro.history import ExecutorInfo

        prepared = self.prepared()
        return (
            self.history()
            .create_run(
                self.context(),
                TextMission("Conferir a lista"),
                ExecutorInfo(name="codex", version=prepared.install.version),
                skill_bundle_version=prepared.bundle_sha256,
                executor_snapshot=executor_snapshot(prepared),
            )
            .record.id
        )

    def launch(
        self, run_id: str, mode: LaunchMode = LaunchMode.HEADLESS, **kwargs: Any
    ) -> CodexResult:
        from lastro.codex.launcher import launch

        return launch(
            self.context(),
            self.history(),
            run_id,
            mode,
            write=self.outputs.append,
            probe=lambda: self.install,
            **kwargs,
        )

    def call(self, mode: str = "exec") -> dict[str, Any]:
        return json.loads((self.bin_dir / f"{mode}-call.json").read_text(encoding="utf-8"))


def make_project(tmp_path: Path, write_env: bool = True) -> CodexProject:
    directory = tmp_path / "project"
    directory.mkdir()
    write_scaffold(
        directory,
        InitAnswers(
            profile_name="local",
            target_url="http://127.0.0.1:9",
            login_url="http://127.0.0.1:9/login",
            tenant_id="t1",
            project_id=PROJECT_ID,
        ),
        "codex",
    )
    config = directory / "lastro.toml"
    root = (tmp_path / "private").as_posix()
    config.write_text(
        f"private_data_root = '{root}'\n" + config.read_text(encoding="utf-8"), encoding="utf-8"
    )
    if write_env:
        (directory / ".env").write_text(
            "LASTRO_LOCAL_USERNAME=u\nLASTRO_LOCAL_PASSWORD=canary-password\n", encoding="utf-8"
        )
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    shutil.copy(FAKE, bin_dir / "fake_codex.py")
    project = CodexProject(directory, bin_dir, [])
    project.behave()
    return project
