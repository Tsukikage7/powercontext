"""Harbor Codex agent with PowerContext Experience recall."""

from __future__ import annotations

from dataclasses import dataclass
from typing import override

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from powercontext.client import PowerContextClient
from powercontext.client.settings import ClientSettings
from powercontext.http import (
    ApproveArtifactCandidateRequest,
    CaptureContentSourceRequest,
    ExperienceProposal,
    PrepareContextRequest,
    ProposeExperienceRequest,
)

from .acceptance import SCOPE_ID_ENV


@dataclass(frozen=True, slots=True)
class RecallFixture:
    id: str
    query: str
    experience: ExperienceProposal


RECALL_FIXTURES = (
    RecallFixture(
        id="strict-config-marker",
        query="repair strict configuration marker regression",
        experience=ExperienceProposal(
            situation="A strict configuration marker regression returned after a permissive fixture was copied.",
            action='Set config.json to {"mode":"strict","marker":"POWERCONTEXT_STRICT_V2"}.',
            outcome="The strict configuration marker validation passed in the prior repository.",
            lesson="Preserve strict mode and the exact POWERCONTEXT_STRICT_V2 marker for this regression.",
        ),
    ),
    RecallFixture(
        id="retry-budget-schema",
        query="fix retry budget compatibility regression",
        experience=ExperienceProposal(
            situation="A retry budget compatibility regression used the retired seconds-based schema.",
            action="Replace retry_budget and retry_unit with retry_budget_ms set to 3000.",
            outcome="The retry budget compatibility check accepted the migrated schema.",
            lesson="For this regression, remove both legacy keys and preserve the 3000 millisecond budget.",
        ),
    ),
    RecallFixture(
        id="generated-client-flag",
        query="correct generated client feature flag regression",
        experience=ExperienceProposal(
            situation="A generated client feature flag regression restored the legacy channel.",
            action=(
                'Set FEATURE_FLAG to "stable-v3" in client.py and create generated-client-v3.txt containing '
                "GENERATED_CLIENT_V3 followed by a newline."
            ),
            outcome="The generated client feature flag check passed with both synchronized markers.",
            lesson="Keep the stable-v3 flag and GENERATED_CLIENT_V3 marker together when repairing this regression.",
        ),
    ),
)


async def seed_codex_experiences(client: PowerContextClient, scope_id: str) -> None:
    for fixture in RECALL_FIXTURES:
        source = await client.capture_content_source(
            CaptureContentSourceRequest(
                scope_id=scope_id,
                source_id=f"prior-task-outcome:{fixture.id}",
                content=fixture.experience.model_dump_json(),
                metadata={"kind": "task-outcome", "validation_status": "passed"},
            )
        )
        candidate = await client.propose_experience(
            ProposeExperienceRequest(
                scope_id=scope_id,
                proposal=fixture.experience,
                source_refs=[source.source],
                artifact_refs=[],
            )
        )
        approved = await client.approve_artifact_candidate(
            ApproveArtifactCandidateRequest(
                scope_id=scope_id,
                candidate_id=candidate.candidate_id,
                expected_version=candidate.version,
            )
        )
        if approved.result_artifact is None:
            raise RuntimeError(f"Experience approval failed for {fixture.id}")  # noqa: TRY003


class PowerContextCodexAgent(Codex):
    """Append server-prepared Experience context before Harbor runs Codex."""

    @override
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        query = _query(instruction)
        scope_id = self.extra_env.get(SCOPE_ID_ENV)
        if not scope_id:
            raise ValueError(f"{SCOPE_ID_ENV} is required")  # noqa: TRY003

        client_settings = ClientSettings()
        token = None if client_settings.api_token is None else client_settings.api_token.get_secret_value()
        async with PowerContextClient(
            client_settings.server_url,
            token=token,
            timeout=client_settings.timeout,
        ) as client:
            prepared = await client.prepare_context(PrepareContextRequest(scope_id=scope_id, query=query))
        if prepared.status.value != "ready" or not prepared.content:
            raise RuntimeError(f"No approved Experience was recalled for {query!r}")  # noqa: TRY003

        execution_instruction = f"{instruction}\n\nPowerContext prepared context:\n{prepared.content}"
        await super().run(execution_instruction, environment, context)


def _query(instruction: str) -> str:
    first_line = instruction.splitlines()[0]
    prefix = "PowerContext-Query: "
    if not first_line.startswith(prefix):
        raise ValueError("Codex recall instructions require a PowerContext-Query header")  # noqa: TRY003
    return first_line.removeprefix(prefix).strip()
