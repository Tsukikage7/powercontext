"""Run every workload as a Harbor job."""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from harbor.job import Job
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import DatasetConfig, JobConfig
from harbor.models.trial.config import (
    AgentConfig,
    EnvironmentConfig,
    ResourceMode,
    ServiceVolumeConfig,
    VerifierConfig,
)
from powercontext.client import PowerContextClient
from powercontext.client.settings import ClientSettings
from powercontext.http import ListMemoryEntriesRequest, PrepareContextRequest

from .acceptance import SCOPE_ID_ENV
from .artifacts import write_artifacts
from .catalog import E2ETask, MemoryEvaluationSpec, runtime_requirement
from .codex_agent import seed_codex_experiences
from .evaluation import evaluate_observation
from .evidence import fingerprint, load_resolved_instructions, redact
from .harbor_agent import BUB_ACP_SERVER_VERSION
from .models import (
    CaptureRecord,
    HarborTrialObservation,
    MemoryEntrySnapshot,
    MemorySnapshot,
    NativeArtifact,
    PreparedContextSnapshot,
    RecallProbeObservation,
    RunEnvironment,
    SourceReferenceSnapshot,
    TaskObservation,
)
from .settings import (
    CodexNotConfiguredError,
    HarnessSettings,
    ModelNotConfiguredError,
    bub_environment,
    codex_auth_path,
    powercontext_bub_environment,
)

NATIVE_ARTIFACT_NAMES = frozenset({
    "acceptance.json",
    "acp-events.jsonl",
    "acp-summary.json",
    "reviewed-artifacts.json",
    "trajectory.json",
})


async def evaluate_task(task: E2ETask, *, output_dir: Path, settings: HarnessSettings) -> bool:
    observation = await run_task(task, output_dir=output_dir, settings=settings)
    report = evaluate_observation(observation, experiment=f"e2e:{task.id}")
    write_artifacts(observation, report, output_dir, settings=settings)
    return report.accepted


async def run_tasks(
    tasks: tuple[E2ETask, ...],
    *,
    output_dir: Path,
    settings: HarnessSettings,
) -> bool:
    bub_workload_ids = tuple(task.id for task in tasks if runtime_requirement(task) == "bub-model")
    if bub_workload_ids and "BUB_MODEL" not in bub_environment():
        raise ModelNotConfiguredError(bub_workload_ids)

    codex_workload_ids = tuple(task.id for task in tasks if runtime_requirement(task) == "codex")
    if codex_workload_ids and not os.environ.get("CODEX_MODEL"):
        raise CodexNotConfiguredError(codex_workload_ids, "CODEX_MODEL is unset")
    if codex_workload_ids and not codex_auth_path().is_file() and not os.environ.get("OPENAI_API_KEY"):
        raise CodexNotConfiguredError(codex_workload_ids, "neither Codex auth.json nor OPENAI_API_KEY is available")

    accepted = True
    for task in tasks:
        task_accepted = await evaluate_task(task, output_dir=output_dir / task.id, settings=settings)
        accepted = task_accepted and accepted
    return accepted


async def run_task(task: E2ETask, *, output_dir: Path, settings: HarnessSettings) -> TaskObservation:
    started_at = datetime.now(UTC)
    run_id = f"{task.id}-{uuid4().hex[:12]}"
    scope_id = f"e2e:{run_id}"
    errors: list[str] = []
    capture_records: tuple[CaptureRecord, ...] = ()
    native_artifacts: tuple[NativeArtifact, ...] = ()
    resolved_instructions = ()
    harbor_observation = HarborTrialObservation()
    memory_before = MemorySnapshot()
    memory_after = MemorySnapshot()
    probes: tuple[RecallProbeObservation, ...] = ()
    client_settings = ClientSettings()
    client_token = None if client_settings.api_token is None else client_settings.api_token.get_secret_value()

    async with PowerContextClient(
        client_settings.server_url,
        token=client_token,
        timeout=client_settings.timeout,
    ) as client:
        try:
            await client.get_readiness()
            if isinstance(task.evaluation, MemoryEvaluationSpec):
                memory_before = await memory_snapshot(client, scope_id)
            if task.agent == "codex":
                await seed_codex_experiences(client, scope_id)

            output_dir.mkdir(parents=True, exist_ok=True)
            job = await Job.create(_job_config(task, run_id, scope_id, output_dir, settings))
            result = await job.run()
            harbor_observation, trial_dir = _harbor_observation(result, settings)
            if harbor_observation.exception_type is not None:
                errors.append(
                    f"{harbor_observation.exception_type}: {harbor_observation.exception_message or ''}".strip()
                )
            if trial_dir is not None:
                capture_records = _load_capture_records(trial_dir)
                native_artifacts = _native_artifacts(trial_dir)
                resolved_instructions = load_resolved_instructions(trial_dir, settings)

            if isinstance(task.evaluation, MemoryEvaluationSpec):
                memory_after = await memory_snapshot(client, scope_id)
                probes = await _probe_observations(client, scope_id, task.evaluation)
        except Exception as exc:
            errors.append(redact(f"{type(exc).__name__}: {exc}", settings))
            if isinstance(task.evaluation, MemoryEvaluationSpec):
                with suppress(Exception):
                    memory_after = await memory_snapshot(client, scope_id)

    return TaskObservation(
        run_id=run_id,
        environment=RunEnvironment(
            commit=settings.commit_id(),
            database=settings.database,
            adapter_version=version("harbor"),
            adapter_protocol_version=_adapter_protocol(task),
            agent_model=_agent_model(task),
            started_at=started_at,
            finished_at=datetime.now(UTC),
        ),
        task=task,
        status="completed" if not errors else "failed",
        errors=tuple(errors),
        harbor=harbor_observation,
        capture_records=capture_records,
        native_artifacts=native_artifacts,
        resolved_instructions=resolved_instructions,
        memory_before=memory_before,
        memory_after=memory_after,
        probes=probes,
    )


def _job_config(
    task: E2ETask,
    run_id: str,
    scope_id: str,
    output_dir: Path,
    settings: HarnessSettings,
) -> JobConfig:
    repository = settings.repository_path()
    mounts: list[ServiceVolumeConfig] | None = None
    if task.agent != "codex":
        mounts = [
            {
                "type": "bind",
                "source": str(repository),
                "target": "/opt/powercontext/source",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ]
    agent = _agent_config(task, scope_id, settings)
    verifier = (
        VerifierConfig(
            import_path="powercontext_e2e.acceptance:PowerContextAcceptanceVerifier",
            env={SCOPE_ID_ENV: scope_id},
        )
        if task.agent == "acceptance"
        else VerifierConfig()
    )
    return JobConfig(
        job_name=run_id,
        jobs_dir=output_dir / "harbor-jobs",
        n_attempts=1,
        n_concurrent_trials=1,
        quiet=True,
        environment=EnvironmentConfig(
            type=EnvironmentType.DOCKER,
            delete=True,
            cpu_enforcement_policy=ResourceMode.IGNORE,
            memory_enforcement_policy=ResourceMode.IGNORE,
            extra_docker_compose=[repository / "e2e" / "bub" / "harbor-task-overlay.yaml"],
            mounts=mounts,
        ),
        verifier=verifier,
        agents=[agent],
        datasets=[_dataset_config(task, repository)],
    )


def _agent_config(task: E2ETask, scope_id: str, settings: HarnessSettings) -> AgentConfig:
    if task.agent == "acceptance":
        return AgentConfig(
            import_path="powercontext_e2e.acceptance:PowerContextAcceptanceAgent",
            env={SCOPE_ID_ENV: scope_id, **powercontext_bub_environment(), **_agent_proxy_environment(settings)},
        )
    if task.agent == "codex":
        env = {SCOPE_ID_ENV: scope_id, **_agent_proxy_environment(settings)}
        if codex_auth_path().is_file():
            env["CODEX_AUTH_JSON_PATH"] = str(codex_auth_path())
        elif api_key := os.environ.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = api_key
        return AgentConfig(
            import_path="powercontext_e2e.codex_agent:PowerContextCodexAgent",
            model_name=os.environ["CODEX_MODEL"],
            env=env,
        )

    evaluation = task.evaluation
    if not isinstance(evaluation, MemoryEvaluationSpec):
        raise TypeError("The Bub agent requires Memory evaluation")  # noqa: TRY003
    agent_env = {**powercontext_bub_environment(), **bub_environment()}
    agent_env.update({
        "BUB_HOME": "/installed-agent/bub-home",
        "BUB_MAX_STEPS": os.environ.get("BUB_MAX_STEPS", "200"),
        "BUB_MAX_TOKENS": os.environ.get("BUB_MAX_TOKENS", "16384"),
        "CODEX_HOME": "/installed-agent/codex",
        "POWERCONTEXT_BUB_CAPTURE_CHECKPOINT_EVERY": str(evaluation.checkpoint_every_events),
        "POWERCONTEXT_BUB_CAPTURE_EVENTS": str(evaluation.capture_events).lower(),
        "POWERCONTEXT_BUB_CAPTURE_LOG": "/logs/agent/powercontext-capture.jsonl",
        "POWERCONTEXT_BUB_CAPTURE_MAX_BYTES": str(evaluation.max_event_bytes),
        "POWERCONTEXT_BUB_SCOPE_ID": scope_id,
    })
    agent_env.update(_agent_proxy_environment(settings))
    return AgentConfig(
        import_path="powercontext_e2e.harbor_agent:PowerContextBubAcpAgent",
        env=agent_env,
    )


def _agent_proxy_environment(settings: HarnessSettings) -> dict[str, str]:
    if settings.agent_proxy_url is None:
        return {}
    proxy_url = settings.agent_proxy_url.get_secret_value()
    no_proxy = "127.0.0.1,localhost,host-gateway,powercontext"
    return {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "NO_PROXY": no_proxy,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "no_proxy": no_proxy,
    }


def _dataset_config(task: E2ETask, repository: Path) -> DatasetConfig:
    dataset = task.dataset
    if dataset.path is not None:
        return DatasetConfig(path=repository / dataset.path, task_names=[dataset.task_id])
    return DatasetConfig(name=dataset.name, version=dataset.version, task_names=[dataset.task_id])


def _adapter_protocol(task: E2ETask) -> str:
    if task.agent == "bub":
        return BUB_ACP_SERVER_VERSION
    return "harbor.agent/v1"


def _agent_model(task: E2ETask) -> str | None:
    if task.agent == "bub":
        return bub_environment().get("BUB_MODEL")
    if task.agent == "codex":
        return os.environ.get("CODEX_MODEL")
    return None


async def memory_snapshot(client: PowerContextClient, scope_id: str) -> MemorySnapshot:
    response = await client.list_memory_entries(ListMemoryEntriesRequest(scope_id=scope_id))
    return MemorySnapshot(
        entries=tuple(
            MemoryEntrySnapshot(
                entry_id=entry.citation.entry_id,
                entry_version_id=entry.citation.entry_version_id,
                version=entry.version,
                kind=entry.kind,
                text=entry.text,
                state=entry.state.value,
                source_refs=tuple(
                    SourceReferenceSnapshot(name=source.name, source_id=source.source_id)
                    for source in entry.source_refs
                ),
            )
            for entry in response.entries
        )
    )


async def prepared_context(client: PowerContextClient, scope_id: str, query: str) -> PreparedContextSnapshot:
    prepared = await client.prepare_context(PrepareContextRequest(scope_id=scope_id, query=query))
    return PreparedContextSnapshot(status=prepared.status.value, content=prepared.content or "")


async def _probe_observations(
    client: PowerContextClient,
    scope_id: str,
    evaluation: MemoryEvaluationSpec,
) -> tuple[RecallProbeObservation, ...]:
    observations: list[RecallProbeObservation] = []
    for probe in evaluation.probes:
        observations.append(
            RecallProbeObservation(
                id=probe.id,
                query=probe.query,
                prepared_context=await prepared_context(client, scope_id, probe.query),
            )
        )
    return tuple(observations)


def _harbor_observation(result: Any, settings: HarnessSettings) -> tuple[HarborTrialObservation, Path | None]:
    if not result.trial_results:
        return HarborTrialObservation(job_id=str(result.id)), None
    trial = result.trial_results[0]
    rewards = trial.verifier_result.rewards if trial.verifier_result is not None else {}
    exception = trial.exception_info
    return (
        HarborTrialObservation(
            job_id=str(result.id),
            trial_name=trial.trial_name,
            trial_uri=trial.trial_uri,
            task_checksum=trial.task_checksum,
            rewards=rewards or {},
            exception_type=None if exception is None else exception.exception_type,
            exception_message=None if exception is None else redact(exception.exception_message, settings),
            started_at=trial.started_at,
            finished_at=trial.finished_at,
        ),
        _trial_dir(trial.trial_uri),
    )


def _trial_dir(trial_uri: str) -> Path | None:
    parsed = urlparse(trial_uri)
    return Path(unquote(parsed.path)) if parsed.scheme == "file" else None


def _load_capture_records(trial_dir: Path) -> tuple[CaptureRecord, ...]:
    records: list[CaptureRecord] = []
    for path in sorted(trial_dir.rglob("powercontext-capture.jsonl")):
        records.extend(
            CaptureRecord.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return tuple(records)


def _native_artifacts(trial_dir: Path) -> tuple[NativeArtifact, ...]:
    return tuple(
        fingerprint(path, relative_to=trial_dir)
        for path in sorted(trial_dir.rglob("*"))
        if path.name in NATIVE_ARTIFACT_NAMES
    )
