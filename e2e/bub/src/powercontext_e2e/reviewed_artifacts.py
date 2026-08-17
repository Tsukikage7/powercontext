"""Run the deterministic reviewed Artifact acceptance journey through the public Client."""

from __future__ import annotations

from powercontext.client import PowerContextClient, ServerResponseError
from powercontext.client.settings import ClientSettings
from powercontext.http import (
    ApproveArtifactCandidateRequest,
    ArtifactCandidate,
    ArtifactReference,
    CandidateFamily,
    CandidateStatus,
    CaptureContentSourceRequest,
    ExperienceProposal,
    GetArtifactCandidateRequest,
    GetExperienceRequest,
    GetSkillRequest,
    ListArtifactCandidatesRequest,
    PrepareContextRequest,
    ProposeExperienceRequest,
    ProposeSkillRequest,
    RejectArtifactCandidateRequest,
    ReviseArtifactCandidateRequest,
    SkillProposal,
    SkillValidationItem,
)

from .models import (
    ClientErrorSnapshot,
    PreparedContextSnapshot,
    ReviewedArtifactsObservation,
)

EXPERIENCE_PENDING = "experience-pending"
EXPERIENCE_REVISED = "experience-revised"
EXPERIENCE_APPROVED = "experience-approved"
EXPERIENCE_APPROVED_EXACT = "experience-approved-exact"
EXPERIENCE_STALE_APPROVAL = "experience-stale-approval"
SKILL_PENDING = "skill-pending"
SKILL_APPROVED = "skill-approved"
SKILL_REPLACEMENT_PENDING = "skill-replacement-pending"
SKILL_REPLACEMENT_APPROVED = "skill-replacement-approved"
SKILL_REJECTED = "skill-rejected"
SKILL_REJECTED_EXACT = "skill-rejected-exact"
CONTEXT_BEFORE_APPROVAL = "before-experience-approval"
CONTEXT_AFTER_APPROVAL = "after-experience-approval"
EXPERIENCE_INBOX = "experience-pending"
SKILL_INBOX = "skill-pending"
SOURCE_ID = "contract-validation"
SOURCE_CONTENT = "api-generate and contract-test passed"
REPLACEMENT_SOURCE_ID = "skill-validation"
REPLACEMENT_SOURCE_CONTENT = "The managed Skill was used and validation passed."
RECALL_QUERY = "How should the Client be updated after an OpenAPI contract change?"
REJECTION_REASON = "The proposal deletes unrelated artifacts."

EXPERIENCE_PROPOSAL = ExperienceProposal(
    situation="The public OpenAPI contract changes.",
    action="Regenerate the Client and run contract tests.",
    outcome="The generated transport matches the contract.",
    lesson="Regenerate the Client before contract tests.",
)
REVISED_EXPERIENCE_PROPOSAL = EXPERIENCE_PROPOSAL.model_copy(
    update={"lesson": "Regenerate and inspect the Client before contract tests."}
)
SKILL_PROPOSAL = SkillProposal(
    name="powercontext-openapi-change",
    description="Use when changing PowerContext's public HTTP contract.",
    instructions="Regenerate the Client, inspect the diff, and run contract tests.",
    validation=[
        SkillValidationItem("make api-generate-check passes"),
        SkillValidationItem("make contract-test passes"),
    ],
)
REPLACEMENT_SKILL_PROPOSAL = SKILL_PROPOSAL.model_copy(
    update={
        "instructions": "Regenerate the Client, inspect the diff, run generation checks, and then run contract tests."
    }
)
REJECTED_SKILL_PROPOSAL = SkillProposal(
    name="unsafe-contract-cleanup",
    description="An intentionally rejected contract workflow.",
    instructions="Delete unrelated generated artifacts before reviewing the contract.",
    validation=[SkillValidationItem("Unrelated generated artifacts are absent.")],
)


async def run_reviewed_artifacts_scenario(scope_id: str) -> ReviewedArtifactsObservation:
    """Execute one model-free Experience and Skill lifecycle against an existing Server."""

    candidates = {}
    experiences = {}
    skills = {}
    contexts = {}
    inboxes = {}
    client_errors = {}
    client_settings = ClientSettings()
    client_token = None if client_settings.api_token is None else client_settings.api_token.get_secret_value()

    async with PowerContextClient(
        client_settings.server_url,
        token=client_token,
        timeout=client_settings.timeout,
    ) as client:
        await client.get_readiness()
        capabilities = await client.get_capabilities()
        source = await client.capture_content_source(
            CaptureContentSourceRequest(
                scope_id=scope_id,
                source_id=SOURCE_ID,
                content=SOURCE_CONTENT,
            )
        )
        pending_experience = await client.propose_experience(
            ProposeExperienceRequest(
                scope_id=scope_id,
                proposal=EXPERIENCE_PROPOSAL,
                source_refs=[source.source],
                artifact_refs=[],
            )
        )
        candidates[EXPERIENCE_PENDING] = pending_experience
        inboxes[EXPERIENCE_INBOX] = await _candidate_ids(client, scope_id, CandidateFamily.EXPERIENCE)
        contexts[CONTEXT_BEFORE_APPROVAL] = await _prepared_context(client, scope_id, RECALL_QUERY)

        revised_experience = await client.revise_artifact_candidate(
            ReviseArtifactCandidateRequest(
                scope_id=scope_id,
                candidate_id=pending_experience.candidate_id,
                expected_version=pending_experience.version,
                proposal=REVISED_EXPERIENCE_PROPOSAL,
                source_refs=[source.source],
                artifact_refs=[],
                reason="Clarify the reusable lesson before approval.",
            )
        )
        candidates[EXPERIENCE_REVISED] = revised_experience
        try:
            await client.approve_artifact_candidate(
                ApproveArtifactCandidateRequest(
                    scope_id=scope_id,
                    candidate_id=revised_experience.candidate_id,
                    expected_version=pending_experience.version,
                )
            )
        except ServerResponseError as exc:
            client_errors[EXPERIENCE_STALE_APPROVAL] = ClientErrorSnapshot(
                status_code=exc.status_code,
                code=exc.code,
            )
        approved_experience = await client.approve_artifact_candidate(
            ApproveArtifactCandidateRequest(
                scope_id=scope_id,
                candidate_id=revised_experience.candidate_id,
                expected_version=revised_experience.version,
            )
        )
        candidates[EXPERIENCE_APPROVED] = approved_experience
        candidates[EXPERIENCE_APPROVED_EXACT] = await client.get_artifact_candidate(
            GetArtifactCandidateRequest(
                scope_id=scope_id,
                candidate_id=approved_experience.candidate_id,
            )
        )
        experience_ref = _result_artifact(approved_experience, "Experience approval")
        experience = await client.get_experience(GetExperienceRequest(scope_id=scope_id, artifact=experience_ref))
        experiences[EXPERIENCE_APPROVED] = experience
        contexts[CONTEXT_AFTER_APPROVAL] = await _prepared_context(client, scope_id, RECALL_QUERY)

        pending_skill = await client.propose_skill(
            ProposeSkillRequest(
                scope_id=scope_id,
                proposal=SKILL_PROPOSAL,
                source_refs=[],
                artifact_refs=[experience.artifact],
                reason="Incubated from the approved Experience.",
            )
        )
        candidates[SKILL_PENDING] = pending_skill
        inboxes[SKILL_INBOX] = await _candidate_ids(client, scope_id, CandidateFamily.SKILL)
        approved_skill = await client.approve_artifact_candidate(
            ApproveArtifactCandidateRequest(
                scope_id=scope_id,
                candidate_id=pending_skill.candidate_id,
                expected_version=pending_skill.version,
            )
        )
        candidates[SKILL_APPROVED] = approved_skill
        skill_ref = _result_artifact(approved_skill, "Skill approval")
        skill = await client.get_skill(GetSkillRequest(scope_id=scope_id, artifact=skill_ref))
        skills[SKILL_APPROVED] = skill

        usage = await client.capture_content_source(
            CaptureContentSourceRequest(
                scope_id=scope_id,
                source_id=REPLACEMENT_SOURCE_ID,
                content=REPLACEMENT_SOURCE_CONTENT,
            )
        )
        replacement = await client.propose_skill(
            ProposeSkillRequest(
                scope_id=scope_id,
                proposal=REPLACEMENT_SKILL_PROPOSAL,
                source_refs=[usage.source],
                artifact_refs=[skill.artifact],
                target=skill.artifact,
                reason="Usage evidence updates the managed procedure.",
            )
        )
        candidates[SKILL_REPLACEMENT_PENDING] = replacement
        approved_replacement = await client.approve_artifact_candidate(
            ApproveArtifactCandidateRequest(
                scope_id=scope_id,
                candidate_id=replacement.candidate_id,
                expected_version=replacement.version,
            )
        )
        candidates[SKILL_REPLACEMENT_APPROVED] = approved_replacement
        replacement_ref = _result_artifact(approved_replacement, "Skill replacement approval")
        skills[SKILL_REPLACEMENT_APPROVED] = await client.get_skill(
            GetSkillRequest(scope_id=scope_id, artifact=replacement_ref)
        )
        skills["skill-historical-revision"] = await client.get_skill(
            GetSkillRequest(scope_id=scope_id, artifact=skill.artifact)
        )

        rejected_pending = await client.propose_skill(
            ProposeSkillRequest(
                scope_id=scope_id,
                proposal=REJECTED_SKILL_PROPOSAL,
                source_refs=[],
                artifact_refs=[experience.artifact],
                reason="Exercise the deterministic rejection path.",
            )
        )
        rejected = await client.reject_artifact_candidate(
            RejectArtifactCandidateRequest(
                scope_id=scope_id,
                candidate_id=rejected_pending.candidate_id,
                expected_version=rejected_pending.version,
                reason=REJECTION_REASON,
            )
        )
        candidates[SKILL_REJECTED] = rejected
        candidates[SKILL_REJECTED_EXACT] = await client.get_artifact_candidate(
            GetArtifactCandidateRequest(scope_id=scope_id, candidate_id=rejected.candidate_id)
        )

    return ReviewedArtifactsObservation(
        capability_families=tuple(capabilities.artifact_families),
        candidates=candidates,
        experiences=experiences,
        skills=skills,
        contexts=contexts,
        inboxes=inboxes,
        client_errors=client_errors,
    )


def reviewed_artifacts_passed(evidence: ReviewedArtifactsObservation) -> bool:
    candidates = evidence.candidates
    experiences = evidence.experiences
    skills = evidence.skills
    pending_experience = candidates.get(EXPERIENCE_PENDING)
    revised_experience = candidates.get(EXPERIENCE_REVISED)
    approved_experience = candidates.get(EXPERIENCE_APPROVED)
    experience = experiences.get(EXPERIENCE_APPROVED)
    pending_skill = candidates.get(SKILL_PENDING)
    approved_skill = candidates.get(SKILL_APPROVED)
    first_skill = skills.get(SKILL_APPROVED)
    replacement_skill = skills.get(SKILL_REPLACEMENT_APPROVED)
    historical_skill = skills.get("skill-historical-revision")
    rejected_skill = candidates.get(SKILL_REJECTED)
    context_before = evidence.contexts.get(CONTEXT_BEFORE_APPROVAL)
    context_after = evidence.contexts.get(CONTEXT_AFTER_APPROVAL)
    stale_approval = evidence.client_errors.get(EXPERIENCE_STALE_APPROVAL)

    checks = (
        {"experience", "skill"}.issubset(evidence.capability_families),
        pending_experience is not None
        and pending_experience.status is CandidateStatus.PENDING
        and evidence.inboxes.get(EXPERIENCE_INBOX) == (pending_experience.candidate_id,),
        context_before is not None and context_before.status == "empty" and not context_before.content,
        revised_experience is not None
        and revised_experience.version == 2
        and revised_experience.proposal == REVISED_EXPERIENCE_PROPOSAL,
        approved_experience is not None
        and approved_experience.status is CandidateStatus.APPROVED
        and candidates.get(EXPERIENCE_APPROVED_EXACT) == approved_experience,
        experience is not None
        and experience.content == REVISED_EXPERIENCE_PROPOSAL
        and len(experience.source_refs) == 1
        and experience.source_refs[0].source_id == SOURCE_ID,
        stale_approval is not None
        and stale_approval.status_code == 409
        and stale_approval.code == "candidate_conflict",
        context_after is not None
        and context_after.status == "ready"
        and experience is not None
        and experience.artifact.artifact_id in context_after.content,
        pending_skill is not None
        and pending_skill.status is CandidateStatus.PENDING
        and pending_skill.proposal == SKILL_PROPOSAL
        and evidence.inboxes.get(SKILL_INBOX) == (pending_skill.candidate_id,),
        approved_skill is not None
        and approved_skill.status is CandidateStatus.APPROVED
        and first_skill is not None
        and first_skill.content == SKILL_PROPOSAL
        and experience is not None
        and first_skill.artifact_refs == [experience.artifact],
        replacement_skill is not None
        and replacement_skill.artifact.revision == 2
        and replacement_skill.content == REPLACEMENT_SKILL_PROPOSAL
        and first_skill is not None
        and replacement_skill.artifact_refs == [first_skill.artifact]
        and len(replacement_skill.source_refs) == 1
        and replacement_skill.source_refs[0].source_id == REPLACEMENT_SOURCE_ID
        and historical_skill == first_skill,
        rejected_skill is not None
        and rejected_skill.status is CandidateStatus.REJECTED
        and rejected_skill.result_artifact is None
        and rejected_skill.decision_reason == REJECTION_REASON
        and candidates.get(SKILL_REJECTED_EXACT) == rejected_skill,
    )
    return all(checks)


def _result_artifact(candidate: ArtifactCandidate, operation: str) -> ArtifactReference:
    if candidate.result_artifact is None:
        raise ValueError(f"{operation} returned no Artifact")  # noqa: TRY003
    return candidate.result_artifact


async def _candidate_ids(
    client: PowerContextClient,
    scope_id: str,
    family: CandidateFamily,
) -> tuple[str, ...]:
    page = await client.list_artifact_candidates(ListArtifactCandidatesRequest(scope_id=scope_id, family=family))
    return tuple(candidate.candidate_id for candidate in page.candidates)


async def _prepared_context(client: PowerContextClient, scope_id: str, query: str) -> PreparedContextSnapshot:
    prepared = await client.prepare_context(PrepareContextRequest(scope_id=scope_id, query=query))
    return PreparedContextSnapshot(status=prepared.status.value, content=prepared.content or "")
