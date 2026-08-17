# End-to-end workload harness

This directory contains PowerContext's Harbor workload catalog. Harbor is the single execution boundary. It owns
datasets, tasks, steps, agents, timeouts, container lifecycles, verifiers, and native evidence. The harness selects
workloads, provides PowerContext scopes, adds Memory observations, and generates a unified report.

Each manifest has three orthogonal dimensions:

- `dataset`: the Harbor task source, ID, and checksum;
- `agent`: `acceptance`, `bub`, or `codex`;
- `evaluation`: either the Harbor-native verifier or Memory capture and recall evaluation in addition to the native
  reward.

Categories describe suite and capability membership. They do not select an execution adapter or imply model and
credential requirements.

```yaml
schema: powercontext.e2e-task/v1
id: acceptance-suite
categories:
  - acceptance
  - smoke
dataset:
  path: e2e/bub/harbor-tasks
  task_id: acceptance-suite
  checksum: <harbor-task-checksum>
agent: acceptance
evaluation:
  type: native
```

## Workloads

| ID | Agent | Evaluation | Purpose |
| --- | --- | --- | --- |
| `acceptance-suite` | Model-free acceptance | Harbor native | Validate the LoCoMo sample, project database decision, and reviewed Artifact lifecycle in one Harbor multi-step trial |
| `terminal-bench-db-wal-recovery` | Bub ACP | Memory | Exercise model-driven, long-horizon Terminal-Bench capture and recall |
| `codex-experience-recall` | Harbor Codex | Harbor native | Validate three-step recall of approved Experiences with a real Codex agent |

The three `acceptance-suite` steps share one Harbor task environment and one agent setup, but each step uses an
independent PowerContext scope. The acceptance agent runs only fixed Bub tool commands or public Client calls and does
not require an LLM. The required SQLite and OceanBase CI jobs use the same `acceptance` selector.

Terminal-Bench requires a model and corresponding credentials, so it is not part of acceptance. Codex recall is also
explicitly opt-in. It uses Harbor's native Codex agent, and the Harbor task definition owns its 600-second agent
timeout. Acceptance CI does not enable the Terminal workload or request its credentials.

## Run acceptance

Against an existing PowerContext Server:

```bash
export POWERCONTEXT_CLIENT_SERVER_URL=http://127.0.0.1:8000
export POWERCONTEXT_BUB_BASE_URL=http://host-gateway:8000
make harness-acceptance
```

With the fixed Compose harness:

```bash
make harness-compose-acceptance ARGS='--category acceptance'

POWERCONTEXT_E2E_DATABASE=oceanbase \
make harness-compose-acceptance ARGS='--category acceptance'
```

Selection accepts repeatable workload IDs and categories. ID and category selectors are additive:

```bash
make harness-compose-acceptance ARGS='--id acceptance-suite'
make harness-compose-acceptance ARGS='--category acceptance --category smoke'
```

Every workload produces the same output structure. Harbor trial, agent, verifier, and environment evidence is stored
under `harbor-jobs/`:

```text
<output>/<workload-id>/
  replay.json
  eval-report.json
  report.md
  harbor-jobs/
```

`replay.json` is a self-contained Pydantic observation. It records the dataset checksum, agent identity, database
identity, Harbor reward, native artifacts, and any Memory workload snapshots, captures, and probes.
`eval-report.json` uses `powercontext.e2e-evaluation/v1`; `report.md` is a human-readable projection of the same result.

## Model-backed workloads

Terminal-Bench retains its registry task, native verifier, and isolation boundary. The Bub agent installs the local
PowerContext plugin and ACP server through the supported `uv tool install` path. Provide `BUB_MODEL` and the
corresponding native Bub authentication settings at runtime:

```bash
export BUB_MODEL=openrouter:openai/gpt-5.4
export BUB_API_KEY=replace-me
make harness-compose-acceptance ARGS='--category long-horizon'
```

The task definition owns Harbor task and environment timeouts; Bub continues to own `BUB_MODEL_TIMEOUT_SECONDS`. The
Memory evaluator independently checks checkpoints, grounded captures, and recall probes. The Harbor-native reward is
diagnostic only.

Codex recall requires `CODEX_MODEL` and uses either a native Codex `auth.json` file or `OPENAI_API_KEY`:

```bash
export CODEX_MODEL=gpt-5.4
make harness-compose-acceptance ARGS='--id codex-experience-recall'
```

The harness does not translate provider settings. The Client consumes `POWERCONTEXT_CLIENT_*`, Bub consumes `BUB_*`,
the PowerContext Bub integration consumes `POWERCONTEXT_BUB_*`, and Codex uses its own authentication file or
`OPENAI_API_KEY`.

## Rescore evidence

All workloads use the same offline entry point:

```bash
REPLAY=.powercontext-e2e/bub/sqlite/acceptance/terminal-bench-db-wal-recovery/replay.json \
make harness-rescore
```

The final evidence sink removes configured Client, Bub, and Codex secrets. CI scans evidence with TruffleHog before
publishing artifacts. Harbor-native artifacts can still contain arbitrary task output and require manual review before
publication.
