from __future__ import annotations

from pathlib import Path

import pytest

from powercontext_e2e.catalog import E2ETask, load_tasks, runtime_requirement, select_tasks
from powercontext_e2e.runner import _job_config
from powercontext_e2e.settings import HarnessSettings


def test_catalog_uses_harbor_as_the_single_execution_boundary() -> None:
    repository = Path(__file__).resolve().parents[3]
    tasks = load_tasks(repository / "e2e" / "bub" / "tasks")

    assert [task.id for task in tasks] == [
        "acceptance-suite",
        "codex-experience-recall",
        "terminal-bench-db-wal-recovery",
    ]
    assert {task.agent for task in tasks} == {"acceptance", "bub", "codex"}
    assert {task.id: runtime_requirement(task) for task in tasks} == {
        "acceptance-suite": "none",
        "codex-experience-recall": "codex",
        "terminal-bench-db-wal-recovery": "bub-model",
    }

    acceptance = select_tasks(tasks, categories=("acceptance",))
    assert [task.id for task in acceptance] == ["acceptance-suite"]


def test_task_contract_has_no_execution_evaluation_pairing_matrix() -> None:
    repository = Path(__file__).resolve().parents[3]
    task = load_tasks(repository / "e2e" / "bub" / "tasks" / "acceptance-suite.yaml")[0]
    payload = task.model_dump(mode="json", by_alias=True)

    assert set(payload) == {"schema", "id", "categories", "provenance", "dataset", "agent", "evaluation"}


def test_workload_requires_explicit_evaluation_type() -> None:
    repository = Path(__file__).resolve().parents[3]
    task = load_tasks(repository / "e2e" / "bub" / "tasks" / "acceptance-suite.yaml")[0]
    payload = task.model_dump(mode="json", by_alias=True)
    del payload["evaluation"]["type"]

    with pytest.raises(ValueError, match="union_tag_not_found"):
        E2ETask.model_validate(payload)


def test_codex_tasks_do_not_mount_the_local_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = Path(__file__).resolve().parents[3]
    tasks = {task.id: task for task in load_tasks(repository / "e2e" / "bub" / "tasks")}
    settings = HarnessSettings(repository=repository)
    monkeypatch.setenv("CODEX_MODEL", "gpt-test")

    acceptance_config = _job_config(tasks["acceptance-suite"], "run", "scope", tmp_path, settings)
    codex_config = _job_config(tasks["codex-experience-recall"], "run", "scope", tmp_path, settings)
    bub_config = _job_config(tasks["terminal-bench-db-wal-recovery"], "run", "scope", tmp_path, settings)

    assert codex_config.environment.mounts is None
    expected_mounts = [
        {
            "type": "bind",
            "source": str(repository),
            "target": "/opt/powercontext/source",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    assert acceptance_config.environment.mounts == expected_mounts
    assert bub_config.environment.mounts == expected_mounts
