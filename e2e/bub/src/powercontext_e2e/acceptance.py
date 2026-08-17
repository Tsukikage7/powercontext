"""Harbor-native agents and verification for the shared model-free acceptance task."""

from __future__ import annotations

import json
import shlex
from typing import Annotated, Literal, override

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier
from powercontext.client import PowerContextClient
from powercontext.client.settings import ClientSettings
from powercontext.http import PrepareContextRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .harbor_agent import (
    BUB_VERSION,
    REMOTE_BIN_DIR,
    REMOTE_BUB_HOME,
    REMOTE_BUB_PROJECT,
    REMOTE_SOURCE,
    REMOTE_TOOL_DIR,
)
from .models import ReviewedArtifactsObservation
from .reviewed_artifacts import reviewed_artifacts_passed, run_reviewed_artifacts_scenario

SCOPE_ID_ENV = "POWERCONTEXT_E2E_SCOPE_ID"
REVIEWED_ARTIFACTS_LOG = "reviewed-artifacts.json"


class InstructionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BubInstruction(InstructionModel):
    type: Literal["bub"]
    scope: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    messages: tuple[str, ...] = Field(min_length=1)


class ClientInstruction(InstructionModel):
    type: Literal["client"]
    scope: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    scenario: Literal["reviewed-artifacts"]


INSTRUCTION_ADAPTER = TypeAdapter(Annotated[BubInstruction | ClientInstruction, Field(discriminator="type")])


class PowerContextAcceptanceAgent(BaseAgent):
    """Execute model-free Bub commands and Client scenarios in one Harbor trial."""

    @staticmethod
    @override
    def name() -> str:
        return "powercontext-acceptance"

    @override
    def version(self) -> str:
        return BUB_VERSION

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        command = (
            "set -eu; "
            f"mkdir -p {REMOTE_BIN_DIR} {REMOTE_BUB_HOME} {REMOTE_BUB_PROJECT} {REMOTE_TOOL_DIR}; "
            f"UV_TOOL_BIN_DIR={REMOTE_BIN_DIR} UV_TOOL_DIR={REMOTE_TOOL_DIR} "
            f"uv tool install --force --with {REMOTE_SOURCE} --with {REMOTE_SOURCE}/integrations/bub "
            f"{shlex.quote(f'bub=={BUB_VERSION}')}"
        )
        result = await environment.exec(command=command, env=self.extra_env, user="root")
        if result.return_code != 0:
            raise RuntimeError(f"Bub installation failed: {result.stderr or result.stdout}")  # noqa: TRY003

    @override
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        parsed = INSTRUCTION_ADAPTER.validate_json(instruction)
        scope_id = f"{self._scope_prefix()}:{parsed.scope}"
        if isinstance(parsed, BubInstruction):
            await self._run_bub_messages(parsed.messages, scope_id, environment)
            return

        evidence = await run_reviewed_artifacts_scenario(scope_id)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / REVIEWED_ARTIFACTS_LOG).write_text(
            evidence.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _scope_prefix(self) -> str:
        try:
            return self.extra_env[SCOPE_ID_ENV]
        except KeyError as exc:
            raise ValueError(f"{SCOPE_ID_ENV} is required") from exc  # noqa: TRY003

    async def _run_bub_messages(
        self,
        messages: tuple[str, ...],
        scope_id: str,
        environment: BaseEnvironment,
    ) -> None:
        bub_env = {
            **self.extra_env,
            "BUB_API_KEY": "null",
            "BUB_FALLBACK_MODELS": "null",
            "BUB_HOME": REMOTE_BUB_HOME,
            "BUB_PROJECT": REMOTE_BUB_PROJECT,
            "POWERCONTEXT_BUB_SCOPE_ID": scope_id,
        }
        for index, message in enumerate(messages, start=1):
            session_id = f"{scope_id}:{index}"
            command = shlex.join((
                f"{REMOTE_BIN_DIR}/bub",
                "--workspace",
                "/workspace",
                "run",
                message,
                "--chat-id",
                session_id,
                "--session-id",
                session_id,
            ))
            result = await environment.exec(command=command, env=bub_env)
            if result.return_code != 0:
                raise RuntimeError(  # noqa: TRY003
                    f"Bub command {index} failed: {result.stderr or result.stdout}"
                )


class PowerContextAcceptanceVerifier(BaseVerifier):
    """Verify each shared acceptance step through public PowerContext evidence."""

    @override
    async def verify(self) -> VerifierResult:
        step_name = self.step_name
        if step_name is None:
            raise ValueError("The acceptance verifier requires a Harbor step name")  # noqa: TRY003
        scope_id = f"{self._scope_prefix()}:{step_name}"
        if step_name == "reviewed-artifact-lifecycle":
            passed = self._reviewed_artifacts_passed()
        else:
            passed = await self._memory_passed(scope_id, step_name)
        self.trial_paths.verifier_dir.mkdir(parents=True, exist_ok=True)
        (self.trial_paths.verifier_dir / "acceptance.json").write_text(
            json.dumps({"scope_id": scope_id, "step": step_name, "passed": passed}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return VerifierResult(rewards={"reward": int(passed)})

    def _scope_prefix(self) -> str:
        value = self.override_env.get(SCOPE_ID_ENV)
        if not value:
            raise ValueError(f"{SCOPE_ID_ENV} is required")  # noqa: TRY003
        return value

    async def _memory_passed(self, scope_id: str, step_name: str) -> bool:
        query, expected = MEMORY_EXPECTATIONS[step_name]
        client_settings = ClientSettings()
        token = None if client_settings.api_token is None else client_settings.api_token.get_secret_value()
        async with PowerContextClient(
            client_settings.server_url,
            token=token,
            timeout=client_settings.timeout,
        ) as client:
            prepared = await client.prepare_context(PrepareContextRequest(scope_id=scope_id, query=query))
        content = prepared.content or ""
        return prepared.status.value == "ready" and all(
            fragment.casefold() in content.casefold() for fragment in expected
        )

    def _reviewed_artifacts_passed(self) -> bool:
        path = self.trial_paths.agent_dir / REVIEWED_ARTIFACTS_LOG
        if not path.is_file():
            return False
        evidence = ReviewedArtifactsObservation.model_validate_json(path.read_text(encoding="utf-8"))
        return reviewed_artifacts_passed(evidence)


MEMORY_EXPECTATIONS = {
    "locomo-support-group": (
        "When did Caroline go to the LGBTQ support group?",
        ("LGBTQ support group", "7 May 2023"),
    ),
    "project-database-decision": (
        "Which project decision selected multi-node persistent storage?",
        ("OceanBase", "MySQL-compatible", "multi-node persistent storage"),
    ),
}
