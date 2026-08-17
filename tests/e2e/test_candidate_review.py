from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import powercontext.client.cli as client_cli
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.cli.app import create_cli
from powercontext.client import PowerContextClient
from powercontext.http import (
    ExperienceProposal,
    SkillProposal,
    SkillValidationItem,
)
from powercontext.server.factory import create_server_app
from powercontext.server.settings import McpConfig, ServerSettings


def _settings(database: Path) -> ServerSettings:
    return ServerSettings(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"),
        mcp=McpConfig(enabled=False),
    )


def _proposal(lesson: str) -> ExperienceProposal:
    return ExperienceProposal(
        situation="The public OpenAPI contract changes.",
        action="Regenerate the Client and run contract tests.",
        outcome="The generated transport matches the contract.",
        lesson=lesson,
    )


def _skill_proposal(
    instructions: str = "Regenerate the Client, inspect the diff, and run contract tests.",
) -> SkillProposal:
    return SkillProposal(
        name="powercontext-openapi-change",
        description="Use when changing PowerContext's public HTTP contract.",
        instructions=instructions,
        validation=[
            SkillValidationItem("make api-generate-check passes"),
            SkillValidationItem("make contract-test passes"),
        ],
    )


def test_candidate_cli_lists_shows_revises_approves_and_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_server_app(settings=_settings(tmp_path / "cli-review.db"))

    class InProcessClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self._transport: httpx.AsyncClient | None = None
            self._client: PowerContextClient | None = None

        async def __aenter__(self):
            self._transport = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )
            self._client = PowerContextClient("http://testserver", http_client=self._transport)
            return self

        async def __aexit__(self, *_args) -> None:
            assert self._transport is not None
            await self._transport.aclose()

        async def list_artifact_candidates(self, request):
            assert self._client is not None
            return await self._client.list_artifact_candidates(request)

        async def get_artifact_candidate(self, request):
            assert self._client is not None
            return await self._client.get_artifact_candidate(request)

        async def approve_artifact_candidate(self, request):
            assert self._client is not None
            return await self._client.approve_artifact_candidate(request)

        async def reject_artifact_candidate(self, request):
            assert self._client is not None
            return await self._client.reject_artifact_candidate(request)

        async def revise_artifact_candidate(self, request):
            assert self._client is not None
            return await self._client.revise_artifact_candidate(request)

        async def get_skill(self, request):
            assert self._client is not None
            return await self._client.get_skill(request)

    monkeypatch.setattr(client_cli, "PowerContextClient", InProcessClient)
    cli = create_cli([])
    runner = CliRunner()
    with TestClient(app) as transport:
        captured = transport.post(
            "/v1/sources/content",
            json={"scope_id": "project", "source_id": "task-1", "content": "bounded evidence"},
        ).json()
        proposal = _proposal("Initial lesson.").model_dump(mode="json")
        first = transport.post(
            "/v1/experience/propose",
            json={
                "scope_id": "project",
                "proposal": proposal,
                "source_refs": [captured["source"]],
                "artifact_refs": [],
            },
        ).json()
        second = transport.post(
            "/v1/experience/propose",
            json={
                "scope_id": "project",
                "proposal": proposal,
                "source_refs": [captured["source"]],
                "artifact_refs": [],
            },
        ).json()
        listed = runner.invoke(cli, ["candidate", "list", "--scope-id", "project"])
        shown = runner.invoke(
            cli,
            ["candidate", "show", "--scope-id", "project", first["candidate_id"]],
        )
        revised = runner.invoke(
            cli,
            [
                "candidate",
                "revise",
                "experience",
                "--scope-id",
                "project",
                "--expected-version",
                "1",
                "--situation",
                "The public OpenAPI contract changes.",
                "--action",
                "Regenerate the Client and run contract tests.",
                "--outcome",
                "The generated transport matches the contract.",
                "--lesson",
                "Revised lesson.",
                "--source-ref",
                "content/task-1",
                first["candidate_id"],
            ],
        )
        approved = runner.invoke(
            cli,
            [
                "candidate",
                "approve",
                "--scope-id",
                "project",
                "--expected-version",
                "2",
                first["candidate_id"],
            ],
        )
        rejected = runner.invoke(
            cli,
            [
                "candidate",
                "reject",
                "--scope-id",
                "project",
                "--expected-version",
                "1",
                "--reason",
                "unsupported",
                second["candidate_id"],
            ],
        )
        approved_head = transport.post(
            "/v1/artifact-candidates/get",
            json={"scope_id": "project", "candidate_id": first["candidate_id"]},
        ).json()
        experience_ref = approved_head["result_artifact"]
        skill_candidate = transport.post(
            "/v1/skill/propose",
            json={
                "scope_id": "project",
                "proposal": _skill_proposal().model_dump(mode="json"),
                "source_refs": [],
                "artifact_refs": [experience_ref],
            },
        ).json()
        skill_approved = transport.post(
            "/v1/artifact-candidates/approve",
            json={
                "scope_id": "project",
                "candidate_id": skill_candidate["candidate_id"],
                "expected_version": 1,
            },
        ).json()
        skill_ref = skill_approved["result_artifact"]
        projection = tmp_path / "repo" / ".agents" / "skills" / "powercontext-openapi-change"
        skill_shown = runner.invoke(
            cli,
            [
                "skill",
                "show",
                "--scope-id",
                "project",
                "--revision",
                str(skill_ref["revision"]),
                skill_ref["artifact_id"],
            ],
        )
        skill_projected = runner.invoke(
            cli,
            [
                "skill",
                "export",
                "--target",
                "codex",
                "--scope-id",
                "project",
                "--revision",
                str(skill_ref["revision"]),
                "--destination",
                str(projection),
                skill_ref["artifact_id"],
            ],
        )

    assert all(
        result.exit_code == 0
        for result in (
            listed,
            shown,
            revised,
            approved,
            rejected,
            skill_shown,
            skill_projected,
        )
    )
    assert first["candidate_id"] in listed.output
    assert first["candidate_id"] in shown.output
    assert '"version": 2' in revised.output
    assert '"status": "approved"' in approved.output
    assert '"status": "rejected"' in rejected.output
    assert skill_ref["artifact_id"] in skill_shown.output
    assert "Exported" in skill_projected.output
    assert (projection / "SKILL.md").is_file()
