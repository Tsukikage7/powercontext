# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""One Source window: prepare all Atomic Memory decisions, then publish once."""

from __future__ import annotations

import logging
import math
from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, NoReturn

from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from powercontext._logging import log_safely
from powercontext.artifacts import ArtifactLineage, ArtifactRef
from powercontext.artifacts.search import ArtifactSearchExecutionContext
from powercontext.builtin.artifacts.atomic_memory.errors import AtomicMemoryConflictError
from powercontext.builtin.artifacts.atomic_memory.extraction import (
    AtomicMemoryEvidence,
    AtomicMemoryExtractionInput,
    project_atomic_memory_evidence,
    require_atomic_memory_pipeline,
)
from powercontext.builtin.artifacts.atomic_memory.models import AtomicMemoryContent, AtomicMemoryStateValue
from powercontext.builtin.artifacts.atomic_memory.reconciliation import (
    ATOMIC_MEMORY_RECONCILIATION_INSTRUCTIONS,
    AtomicMemoryArtifactEvidence,
    AtomicMemoryReconciliationOutput,
    AtomicMemoryWindowWorkset,
    AtomicMemoryWorkingItem,
)
from powercontext.builtin.artifacts.memory.canonical import canonical_embedding
from powercontext.builtin.artifacts.memory.protocols import (
    MemoryWriteGate,
    MemoryWriteGateRequest,
    MemoryWriteVerdict,
)
from powercontext.builtin.artifacts.prompt.errors import PromptError
from powercontext.builtin.artifacts.prompt.service import PromptService, current_prompt
from powercontext.builtin.decision_observations import DecisionObservation
from powercontext.builtin.inference import (
    InferenceTimeoutError,
    InferenceUnavailableError,
    InvalidInferenceOutputError,
    embed_query,
)
from powercontext.builtin.persistence.atomic_memory_index import AtomicMemoryIndexError, AtomicMemoryRelatedRequest
from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.decision_observations import DecisionObservationRepository
from powercontext.builtin.persistence.errors import GenerationConflictError
from powercontext.builtin.persistence.memory_windows import MemorySourceWindowRepository
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.tables import SOURCE_JOURNAL_HEADS_TABLE
from powercontext.builtin.runtime.atomic_memory import AtomicMemoryApplication, deferred_decision_audit
from powercontext.builtin.runtime.decision_policy import emit_decision_observation
from powercontext.builtin.runtime.models import MemoryFlushResult
from powercontext.builtin.runtime.processing_execution import ScopeInvocation
from powercontext.builtin.runtime.protocols import RuntimeTracing
from powercontext.builtin.source_eligibility import is_generation_eligible
from powercontext.builtin.triggers import SOURCE_WINDOW_TRIGGER_NAME, SourceHighWatermark, SourceWindowTrigger
from powercontext.errors import RevisionConflictError

if TYPE_CHECKING:
    from powercontext.builtin.runtime.config import RuntimeConfig


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AtomicMemoryProcessingConfig:
    related_mode: Literal["auto", "fts", "vector", "hybrid"] = "auto"
    related_max_distance: float = 1.0
    related_fts_fallback: bool = False
    comparison_batch_size: int = 20
    input_tokens_limit: int = 24_000

    def __post_init__(self) -> None:
        if (
            self.related_mode not in {"auto", "fts", "vector", "hybrid"}
            or not math.isfinite(self.related_max_distance)
            or self.related_max_distance < 0
            or self.comparison_batch_size < 1
            or self.input_tokens_limit < 1
        ):
            raise ValueError("Atomic Memory processing requires a valid mode, threshold and positive input budgets")  # noqa: TRY003

    @classmethod
    def from_runtime(cls, runtime: RuntimeConfig) -> AtomicMemoryProcessingConfig:
        return cls(
            related_mode=runtime.atomic_memory_related_mode,
            related_max_distance=runtime.atomic_memory_related_max_distance,
            related_fts_fallback=runtime.atomic_memory_related_fts_fallback,
            comparison_batch_size=runtime.atomic_memory_comparison_batch_size,
            input_tokens_limit=runtime.atomic_memory_input_tokens_limit,
        )


class AtomicMemorySourceWindowProcessor:
    """Keep the existing cursor/binding while replacing collection Memory extraction."""

    def __init__(
        self,
        application: AtomicMemoryApplication,
        sources: SourceRepository,
        cursors: SourceCursorRepository,
        *,
        pipeline: object | None,
        prompts: PromptService | None = None,
        config: AtomicMemoryProcessingConfig | None = None,
        memory_write_gate: MemoryWriteGate | None = None,
        decision_observations: DecisionObservationRepository | None = None,
        decision_observation_retention_days: int | None = None,
        tracing: RuntimeTracing | None = None,
    ) -> None:
        self.application = application
        self.sources = sources
        self.cursors = cursors
        self.pipeline = None if pipeline is None else require_atomic_memory_pipeline(pipeline)
        self.prompts = prompts
        self.config = config or AtomicMemoryProcessingConfig()
        self.memory_write_gate = memory_write_gate
        self.decision_observations = decision_observations
        self.decision_observation_retention_days = decision_observation_retention_days
        self.windows = MemorySourceWindowRepository()
        self.tracing = tracing

    async def flush(
        self,
        scope_id: str,
        limit: int,
        *,
        processing: ScopeInvocation | None = None,
        context: ArtifactSearchExecutionContext | None = None,
    ) -> MemoryFlushResult:
        selected_context = context
        # Scope checks may retain a separate configured audit connection. Flush its
        # decision events after every business transaction in this operation closes.
        async with deferred_decision_audit(selected_context):
            with self._stage("memory.flush", {"powercontext.memory.flush.source_count": 0}) as span:
                if self.prompts is not None:
                    legacy = await self.prompts.read_configuration(scope_id, "memory.extract")
                    if legacy.mode == "custom":
                        raise PromptError("legacy_memory_prompt_unsupported", during_inference=True)
                    async with (
                        self.prompts.bind(scope_id, "atomic_memory.extract"),
                        self.prompts.bind(scope_id, "atomic_memory.reconcile"),
                    ):
                        return await self._retry_window(scope_id, limit, processing, selected_context, span)
                return await self._retry_window(scope_id, limit, processing, selected_context, span)

    async def _retry_window(self, scope_id, limit, processing, context, span) -> MemoryFlushResult:
        for attempt in range(3):
            try:
                result = await self._flush_once(scope_id, limit, processing, context, span)
            except (AtomicMemoryConflictError, GenerationConflictError, RevisionConflictError):
                if attempt == 2:
                    raise
            else:
                if span is not None:
                    span.set_attributes({"powercontext.memory.flush.source_count": result.source_count})
                    span.set_outcome("success" if result.processed else "noop")
                return result
        raise AssertionError("unreachable")

    async def _flush_once(self, scope_id, limit, processing, context, span) -> MemoryFlushResult:
        application = self.application
        trigger = SourceWindowTrigger()
        async with application.database.transaction() as connection:
            if processing is not None:
                await processing.start(connection)
            await application.security.authorize(connection, scope_id, context, "read")
            await application.security.authorize(connection, scope_id, context, "create")
            cursor = await self.cursors.load(connection, scope_id, SOURCE_WINDOW_TRIGGER_NAME)
            state = trigger.initial_state() if cursor is None else cursor.cursor
            high_watermark = await self.sources.journal_position(connection, scope_id)
            signal = SourceHighWatermark(sequence=high_watermark, limit=limit)
            limit = await self.windows.limit(connection, scope_id, state.sequence, signal.limit)
            transition = trigger.activate(signal.model_copy(update={"limit": limit}), state)
            if not transition.actions:
                if processing is not None:
                    await processing.complete(connection, remaining_work=False)
                return self._result(state.sequence, state.sequence, high_watermark, 0)
            action = transition.actions[0]
            window = await self.sources.list_window(connection, scope_id, after=action.after, through=action.through)
            self._require_window(window, action.after, action.through)
            eligible = tuple(item for item in window if is_generation_eligible(item.value))
            if span is not None:
                span.set_attributes({"powercontext.memory.flush.source_count": len(eligible)})
            await application.security.authorize_sources(
                connection, scope_id, context, tuple(item.ref for item in eligible)
            )
        if eligible and self.pipeline is None:
            raise InferenceUnavailableError(
                "atomic-memory-generation", "Source extraction and reconciliation are not configured"
            )
        pipeline = self.pipeline if eligible else None
        try:
            workset, consulted_artifacts = await self._prepare(scope_id, eligible, pipeline, context)
            prepared, decision, observation = await self._inspect_decisions(scope_id, workset, context)
        except (InferenceTimeoutError, InvalidInferenceOutputError) as error:
            reducible = (isinstance(error, InferenceTimeoutError) and error.operation == "generate") or (
                isinstance(error, InvalidInferenceOutputError) and error.operation == "atomic-memory-input-budget"
            )
            if reducible and action.through - action.after > 1:
                await self._reduce_window(scope_id, context, processing, cursor, state, action, high_watermark)
            raise
        try:
            finalized = await self._publish(
                scope_id,
                context,
                processing,
                cursor,
                transition,
                action,
                window,
                eligible,
                consulted_artifacts,
                workset,
                prepared,
                decision,
                observation,
                high_watermark,
            )
        except Exception:
            self._emit_failed_observation(observation)
            raise
        if finalized is not None:
            emit_decision_observation(finalized)
        return self._result(
            action.after,
            action.through,
            high_watermark,
            len(eligible),
            held=decision is not None and decision.verdict is MemoryWriteVerdict.HOLD,
            hold_codes=() if decision is None or decision.code is None else (decision.code.value,),
        )

    async def _reduce_window(self, scope_id, context, processing, cursor, state, action, high_watermark):
        async with self.application.database.transaction() as connection:
            if processing is not None:
                await processing.guard(connection)
            await self.cursors.save(
                connection,
                scope_id,
                SOURCE_WINDOW_TRIGGER_NAME,
                state,
                expected_generation=None if cursor is None else cursor.generation,
            )
            await self.windows.reduce(
                connection,
                scope_id,
                source_through=high_watermark,
                window_limit=max(1, (action.through - action.after) // 2),
            )

    async def _inspect_decisions(self, scope_id, workset, context):
        application = self.application
        plans = []
        async with self._read_transaction(context) as connection:
            for item in workset.changes():
                lineage = ArtifactLineage(sources=item.sources, artifacts=item.artifacts)
                if len(item.origins) >= 2:
                    plan = await application.service.inspect_merge(
                        connection,
                        scope_id,
                        application.id_factory("atomic-memory"),
                        item.origins,
                        item.content,
                        context,
                        lineage=lineage,
                    )
                else:
                    original = item.origins[0] if item.origins else None
                    plan = await application.service.inspect_change(
                        connection,
                        scope_id,
                        application.id_factory("atomic-memory") if original is None else original.ref.artifact_id,
                        item.content,
                        context,
                        expected_revision=None if original is None else original.ref.revision,
                        expected_state_version=None if original is None else original.state_version,
                        lineage=lineage,
                    )
                plans.append(plan)
        prepared = tuple([await application.service.prepare_change(plan) for plan in plans])
        decision, observation = await self._assess_write_gate(scope_id, workset, prepared)
        return prepared, decision, observation

    async def _publish(
        self,
        scope_id,
        context,
        processing,
        cursor,
        transition,
        action,
        window,
        eligible,
        consulted_artifacts,
        workset,
        prepared,
        decision,
        observation,
        high_watermark,
    ):
        application = self.application
        writes = tuple(write for item in prepared for write in item.plan.writes)
        finalized = None
        with self._stage(
            "memory.commit",
            {
                "powercontext.memory.commit.memory_changed": bool(writes),
                "powercontext.memory.commit.artifact_revision_count": sum(write.draft is not None for write in writes),
            },
        ):
            async with application.database.transaction() as connection:
                # Every Atomic write uses journal -> heads. Source capture's journal
                # reservation prevents the exact window changing under validation.
                await connection.execute(
                    update(SOURCE_JOURNAL_HEADS_TABLE)
                    .where(SOURCE_JOURNAL_HEADS_TABLE.c.scope_id == scope_id)
                    .values(position=SOURCE_JOURNAL_HEADS_TABLE.c.position)
                )
                if processing is not None:
                    await processing.guard(connection)
                actual = await self.cursors.load(connection, scope_id, SOURCE_WINDOW_TRIGGER_NAME, for_update=True)
                actual_generation = None if actual is None else actual.generation
                expected_generation = None if cursor is None else cursor.generation
                if (
                    0 if actual is None else actual.cursor.sequence
                ) != action.after or actual_generation != expected_generation:
                    raise GenerationConflictError(SOURCE_WINDOW_TRIGGER_NAME, expected_generation, actual_generation)
                current_window = await self.sources.list_window(
                    connection, scope_id, after=action.after, through=action.through
                )
                self._require_window(current_window, action.after, action.through)
                if current_window != window:
                    _conflict("Source window changed during preparation")
                if tuple(item for item in current_window if is_generation_eligible(item.value)) != eligible:
                    _conflict("Source generation eligibility changed during preparation")
                await application.security.authorize_sources(
                    connection, scope_id, context, tuple(item.ref for item in eligible)
                )
                for ref in consulted_artifacts:
                    await self._authorize_artifact(connection, scope_id, context, ref)
                results = ()
                if decision is None or decision.verdict is not MemoryWriteVerdict.HOLD:
                    results = await application.service.commit_window(
                        connection, scope_id, prepared, context, read_set=tuple(workset.reads.values())
                    )
                finalized = await self._finalize_observation(
                    connection,
                    observation,
                    final_action=(
                        "memory_write_hold"
                        if decision is not None and decision.verdict is MemoryWriteVerdict.HOLD
                        else "memory_write_applied"
                    ),
                    subject_refs=tuple(_artifact_reference(result.primary.ref) for result in results),
                )
                await self.cursors.save(
                    connection,
                    scope_id,
                    SOURCE_WINDOW_TRIGGER_NAME,
                    transition.state,
                    expected_generation=expected_generation,
                )
                await self.windows.clear_consumed(connection, scope_id, action.through)
                if processing is not None:
                    await processing.complete(connection, remaining_work=action.through < high_watermark)
        return finalized

    async def _prepare(self, scope_id, eligible, pipeline, context):
        workset = AtomicMemoryWindowWorkset()
        artifacts: dict[tuple[str, str, int], ArtifactRef] = {}
        if pipeline is None:
            return workset, ()
        evidence = tuple([
            await project_atomic_memory_evidence(item, self.sources, evidence_id=f"source:{item.journal_position}")
            for item in eligible
        ])
        extraction_input = AtomicMemoryExtractionInput(evidence=evidence)
        self._require_budget(pipeline, extraction_input, "atomic_memory.extract")
        extraction_prompt = current_prompt("atomic_memory.extract")
        prompt_refs = (
            () if extraction_prompt is None or extraction_prompt.artifact is None else (extraction_prompt.artifact,)
        )
        await self._authorize_model_input(
            scope_id,
            context,
            (),
            tuple(item.ref for item in eligible),
            prompt_refs,
            generation_sources=eligible,
        )
        candidates = await pipeline.extract(extraction_input)
        for ref in prompt_refs:
            artifacts[(ref.family, ref.artifact_id, ref.revision)] = ref
        by_id = {item.evidence_id: item for item in evidence}
        # This order groups candidates by their supporting Sources; the model still
        # receives real Source context, never candidate order as a fact timestamp.
        candidates = sorted(
            candidates,
            key=lambda candidate: tuple(
                sorted(by_id[identifier].journal_position for identifier in candidate.evidence_ids)
            ),
        )
        for ordinal, candidate in enumerate(candidates):
            selected = tuple(by_id[identifier] for identifier in dict.fromkeys(candidate.evidence_ids))
            key = workset.add(
                AtomicMemoryWorkingItem(
                    key=f"candidate:{ordinal}",
                    content=AtomicMemoryContent(kind=candidate.kind, text=candidate.text),
                    evidence=selected,
                    sources=tuple(item.source_ref for item in selected),
                    changed=True,
                )
            )
            prior = tuple(
                item.key for item in workset.items.values() if item.key != key and item.changed and item.retain
            )
            hits = await self._recall(scope_id, candidate.text, context)
            recalled = await self._load_related_items(scope_id, hits, context, workset, artifacts)
            pending = list(dict.fromkeys((*prior, *recalled)))
            compared = False
            while pending or not compared:
                related = workset.related(key, pending[: self.config.comparison_batch_size])
                selected_count = min(len(pending), self.config.comparison_batch_size)
                while related and not self._fits(pipeline, workset.request(key, related), "atomic_memory.reconcile"):
                    selected_count = max(1, selected_count // 2)
                    related = workset.related(key, pending[:selected_count])
                    if selected_count == 1:
                        self._require_budget(pipeline, workset.request(key, related), "atomic_memory.reconcile")
                value = workset.request(key, related)
                self._require_budget(pipeline, value, "atomic_memory.reconcile")
                reconciliation_prompt = current_prompt("atomic_memory.reconcile")
                if reconciliation_prompt is not None and reconciliation_prompt.artifact is not None:
                    ref = reconciliation_prompt.artifact
                    prompt_refs = tuple(
                        {
                            (value.family, value.artifact_id, value.revision): value for value in (*prompt_refs, ref)
                        }.values()
                    )
                    artifacts[(ref.family, ref.artifact_id, ref.revision)] = ref
                dependencies = tuple(
                    read for item in (workset.items[workset.resolve(key)], *related) for read in item.origins
                )
                await self._authorize_model_input(
                    scope_id,
                    context,
                    dependencies,
                    tuple(item.source_ref for item in value.evidence if isinstance(item, AtomicMemoryEvidence)),
                    tuple(artifacts.values()),
                    generation_sources=eligible,
                )
                result = await pipeline.reconciler.generate(value)
                key = workset.apply(key, related, AtomicMemoryReconciliationOutput.model_validate(result.output))
                del pending[:selected_count]
                compared = True
        for item in workset.changes():
            refs = {(ref.family, ref.artifact_id, ref.revision): ref for ref in (*item.artifacts, *prompt_refs)}
            workset.items[item.key] = replace(item, artifacts=tuple(refs.values()))
        return workset, tuple(artifacts.values())

    async def _load_related_items(self, scope_id, hits, context, workset, artifacts):
        from powercontext.server.authz import AccessDeniedError

        recalled = []
        for hit in hits:
            try:
                async with self._read_transaction(context) as connection:
                    record = await self.application.service.get(
                        connection, scope_id, hit.artifact_ref.artifact_id, context
                    )
                    await self.application.security.authorize(connection, scope_id, context, "write", record.ref)
                    if (
                        record.ref != hit.artifact_ref
                        or record.state.state_version != hit.state_version
                        or record.state.state is not AtomicMemoryStateValue.ACTIVE
                        or record.artifact.content.kind != hit.kind
                        or record.artifact.content.text != hit.text
                    ):
                        _conflict("Related memory changed before comparison")
            except AccessDeniedError:
                continue
            content = AtomicMemoryContent(kind=hit.kind, text=hit.text)
            supported = AtomicMemoryArtifactEvidence(
                evidence_id=f"artifact:{record.ref.artifact_id}@{record.ref.revision}",
                artifact_ref=record.ref,
                content=content,
            )
            artifacts[(record.ref.family, record.ref.artifact_id, record.ref.revision)] = record.ref
            recalled.append(
                workset.add(
                    AtomicMemoryWorkingItem(
                        key=f"memory:{hit.artifact_ref.artifact_id}",
                        content=content,
                        origins=(record.as_read(),),
                        evidence=(supported,),
                        artifacts=(record.ref,),
                    )
                )
            )
        return recalled

    async def _recall(self, scope_id, query, context):
        application = self.application
        async with self._read_transaction(context) as connection:
            filters = await application.security.filters(connection, scope_id, context)
        mode = self.config.related_mode
        profile = application.index.capabilities.embedding_profile
        if mode == "auto":
            mode = "fts" if profile is None else "hybrid"
        vector = None
        if mode in {"vector", "hybrid"}:
            try:
                vector = await self._query_vector(query)
            except (AtomicMemoryIndexError, InferenceUnavailableError, InferenceTimeoutError):
                if not self.config.related_fts_fallback:
                    raise
                mode = "fts"
        request = AtomicMemoryRelatedRequest(
            query,
            filters,
            mode=mode,
            query_vector=vector,
            embedding_profile=profile,
            max_distance=self.config.related_max_distance,
        )
        async with application.database.transaction(consistent_snapshot=True) as connection:
            try:
                return await application.index.enumerate_related(connection, scope_id, request)
            except AtomicMemoryIndexError as error:
                if error.code == "stale-recall":
                    _conflict("Related channels returned different memory versions")
                if not self.config.related_fts_fallback or error.code not in {"incomplete-vector", "embedding-profile"}:
                    raise
                return await application.index.enumerate_related(connection, scope_id, replace(request, mode="fts"))

    async def _query_vector(self, query):
        profile = self.application.index.capabilities.embedding_profile
        model = self.application.embedding_model
        if model is None or profile is None or model.profile != profile:
            raise AtomicMemoryIndexError("embedding-profile", "Related-memory vector profile is unavailable")
        result = await embed_query(model, (query,))
        if len(result.vectors) != 1:
            raise AtomicMemoryIndexError("embedding-result", "Related query requires one vector")
        return canonical_embedding(result.vectors[0], dimension=profile.dimension, normalization=profile.normalization)

    async def _authorize_model_input(
        self, scope_id, context, reads, source_refs, artifact_refs=(), *, generation_sources=()
    ):
        async with self._read_transaction(context) as connection:
            await self.application.security.authorize(connection, scope_id, context, "read")
            await self.application.security.authorize(connection, scope_id, context, "create")
            await self.application.security.authorize_sources(connection, scope_id, context, source_refs)
            current_sources = await self.sources.get_many(
                connection, scope_id, tuple(item.ref for item in generation_sources)
            )
            if current_sources != generation_sources or any(
                not is_generation_eligible(item.value) for item in current_sources
            ):
                _conflict("Source generation basis changed before model inference")
            for read in reads:
                current = await self.application.service.get(connection, scope_id, read.ref.artifact_id, context)
                if current.as_read() != read:
                    _conflict("Memory basis changed before model comparison")
                await self.application.security.authorize(connection, scope_id, context, "write", read.ref)
            for ref in artifact_refs:
                await self._authorize_artifact(connection, scope_id, context, ref)

    async def _authorize_artifact(self, connection, scope_id, context, ref):
        await self.application.security.authorize(connection, scope_id, context, "read", ref)

    @asynccontextmanager
    async def _read_transaction(self, context):
        # Builtin authority shares the preparation snapshot; configured providers
        # retain their own decision boundary. Audit writes flush after this read.
        async with (
            deferred_decision_audit(context),
            self.application.database.transaction(consistent_snapshot=True) as connection,
        ):
            yield connection

    def _stage(self, name, attributes):
        return nullcontext(None) if self.tracing is None else self.tracing.stage(name, attributes=attributes)

    def _fits(self, pipeline, value, key):
        prompt = current_prompt(key)
        instructions = (
            (
                pipeline.extraction_instructions
                if key == "atomic_memory.extract"
                else ATOMIC_MEMORY_RECONCILIATION_INSTRUCTIONS
            )
            if prompt is None
            else prompt.compiled_instructions
        )
        return (
            pipeline.estimator.estimate(f"{instructions}\n{value.model_dump_json()}") <= self.config.input_tokens_limit
        )

    def _require_budget(self, pipeline, value, key):
        if not self._fits(pipeline, value, key):
            raise InvalidInferenceOutputError(
                "atomic-memory-input-budget", "A complete comparison input exceeds its token budget"
            )

    @staticmethod
    def _require_window(window, after, through):
        if tuple(item.journal_position for item in window) != tuple(range(after + 1, through + 1)):
            _conflict("Source journal window is not an exact contiguous interval")

    async def _assess_write_gate(self, scope_id, workset, prepared):
        gate = self.memory_write_gate
        if gate is None or not prepared:
            return None, None
        items = workset.changes()
        collector = _PendingObservationSink()
        request = MemoryWriteGateRequest(
            candidates=tuple(item.content.text for item in items),
            evidence=tuple(_gate_evidence_text(item) for item in items for item in item.evidence),
            scope_id=scope_id,
            operation_id=_window_operation_id(prepared),
            subject_refs=tuple(f"candidate:{index}" for index in range(1, len(items) + 1)),
            evidence_refs=tuple(dict.fromkeys(_evidence_reference(value) for item in items for value in item.evidence)),
            observation_sink=collector,
            defer_observation_emission=True,
        )
        try:
            decision = await gate.assess(request)
        except Exception:
            return None, None
        return decision, collector.observation

    async def _finalize_observation(self, connection, observation, *, final_action, subject_refs):
        if observation is None or self.decision_observations is None:
            return None
        finalized = observation.model_copy(
            update={
                "final_action": final_action,
                "subject_refs": observation.subject_refs if final_action == "memory_write_hold" else subject_refs,
            }
        )
        try:
            async with connection.begin_nested():
                await self.decision_observations.append(connection, finalized)
                if self.decision_observation_retention_days is not None:
                    from datetime import UTC, datetime, timedelta

                    await self.decision_observations.prune_before(
                        connection,
                        finalized.scope_id,
                        datetime.now(UTC) - timedelta(days=self.decision_observation_retention_days),
                    )
        except Exception as error:
            # SQLite RAISE(ROLLBACK, ...) invalidates the owner transaction and
            # destroys this savepoint. Continuing would consume the source window
            # in a new transaction after the Memory write was lost.
            if (
                connection.dialect.name == "sqlite"
                and isinstance(error, OperationalError)
                and "no such savepoint" in str(error)
            ):
                raise
            log_safely(
                logger,
                logging.WARNING,
                "Decision observation persistence failed",
                extra={"event": "decision.observation_sink_failed", "exception_type": type(error).__name__},
            )
            return None
        return finalized

    @staticmethod
    def _emit_failed_observation(observation):
        if observation is not None:
            emit_decision_observation(
                observation.model_copy(update={"final_action": "memory_write_failed", "subject_refs": ()})
            )

    @staticmethod
    def _result(after, through, high_watermark, count, *, held=False, hold_codes=()):
        return MemoryFlushResult(
            previous_cursor=after,
            current_cursor=through,
            high_watermark=high_watermark,
            source_count=count,
            memory_ref=None,
            remaining_work=through < high_watermark,
            held_count=1 if held else 0,
            hold_codes=hold_codes,
        )


@dataclass(slots=True)
class _PendingObservationSink:
    observation: DecisionObservation | None = None

    async def record(self, observation: DecisionObservation, /) -> None:
        self.observation = observation


def _gate_evidence_text(value) -> str:
    if isinstance(value, AtomicMemoryEvidence):
        import json

        return json.dumps(value.content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return value.content.text


def _reference_component(value: str) -> str:
    return urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _artifact_reference(reference: ArtifactRef) -> str:
    return f"artifact:{_reference_component(reference.family)}:{_reference_component(reference.artifact_id)}@{reference.revision}"


def _evidence_reference(value) -> str:
    if isinstance(value, AtomicMemoryEvidence):
        reference = value.source_ref
        return f"source:{_reference_component(reference.source_type)}:{_reference_component(reference.source_id)}"
    return _artifact_reference(value.artifact_ref)


def _window_operation_id(prepared) -> str:
    identities = ":".join(item.plan.primary_artifact_id for item in prepared)
    return f"atomic-memory-window:{sha256(identities.encode('utf-8')).hexdigest()}"


def _conflict(detail: str) -> NoReturn:
    raise AtomicMemoryConflictError(detail)
