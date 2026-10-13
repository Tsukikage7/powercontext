---
title: Kimi Code
description: Minimal project memory for Kimi Code through a native plugin, HTTP MCP and a prompt Hook.
status: community
---

# Kimi Code

The integration is experimental and supports the minimal profile: Memory read/write, automatic Source capture and context injection. One configured Scope is shared across projects and sessions in the selected Kimi home. Use another Kimi home or explicitly reconfigure the Scope when work needs isolation.

## Install

Install Kimi Code and Node 24+, and install PowerContext with CLI/Server extras from the same checkout used for the plugin:

```text
uv pip install -e ".[cli,server]"
powercontext server run --env-file .env
```

For an authenticated Server, set POWERCONTEXT_KIMI_TOKEN before setup so native MCP authentication is enabled. For an unauthenticated Server, leave it unset. An existing endpoint keeps its established authentication mode; a missing runtime token reports an error.

In another terminal, create or select an existing Scope using the Server/CLI/Dashboard, then run:

```text
powercontext setup kimi-code --source /path/to/powercontext --server-url http://127.0.0.1:8000 --scope-id <existing-scope-id>
powercontext doctor kimi-code
kimi
```

The source must be a local checkout or the unconfigured plugin directory from it; the installed managed copy is not an update source. Kimi and Node must be on PATH; KIMI_CLI_BIN can point to a real binary or built main.mjs for diagnostics and acceptance. KIMI_CODE_HOME selects the Kimi home, defaulting to ~/.kimi-code.

Setup installs a managed copy at <Kimi home>/plugins/managed/powercontext and registers it in plugins/installed.json. It leaves model configuration and other plugins intact, preserves plugin/MCP disablement, and restores previous registration and plugin contents if publication fails. If rollback itself is denied, setup retains the previous plugin at plugins/managed/.powercontext-kimi-backup and reports the recovery path; restore it before retrying. Repeat setup to update; omitted endpoint, Scope and capture options preserve installed settings. Reinstall from the source to change files in the managed copy.

Open a **new session** after installation or reconfiguration; an existing session may retain its old Skill or MCP configuration. The plugin is user-wide, not repository-local. Setup does not create a Scope or modify bindings. HTTPS and loopback HTTP are supported; non-loopback plaintext HTTP is rejected.

## Authentication and first use

For an authenticated Server, export the raw token in the terminal that launches Kimi:

```powershell
$env:POWERCONTEXT_KIMI_TOKEN = '<raw-server-token>'
```

On Bash use export POWERCONTEXT_KIMI_TOKEN='<raw-server-token>'. Hook and MCP consume the same variable; no token is stored in the plugin. Omit it for an unauthenticated Server. Setup does not load a separate environment file for Kimi.

Ask Kimi to save a project decision, then search it in a fresh session. The plugin exposes search_memory, remember_memory and get_artifact_revision. Current Memory results carry Atomic Memory Artifact references; exact readback uses the returned family, artifact_id and revision. Automatic Source capture is separate from an explicit save.

Ordinary text prompts attempt context preparation and Source capture. Mixed prompts retain text only; unsupported or pure-media input is skipped. Capture skips oversize/sensitive input and can be disabled:

```text
powercontext setup kimi-code --source /path/to/powercontext --no-capture-prompts
```

The Hook has a 4-second total budget and returns context through Kimi's message field. Failures continue ordinary work and emit redacted stderr diagnostics. A capture response lost after sending is unknown and is not retried automatically. Separate submissions, including identical text, get separate Source IDs.

Explicit Memory operations need no Generation API. Automatic Memory requires the Server's Generation configuration and scheduled processing; Source acceptance alone does not prove Memory creation, and the plugin does not flush. Kimi credentials do not supply Server Generation credentials.

## Diagnose and remove

doctor is readonly: it checks registration/enablement, Node, MCP/Hook declarations, Server readiness and Scope access. It does not prove that a real MCP call or model injection succeeded. Missing/wrong tokens surface as authentication_failed; a disabled plugin/MCP stays disabled after setup. Incorrect endpoints or inaccessible Scopes require explicit correction and a new session.

Use Kimi's /plugins remove powercontext to remove registration. Kimi keeps the managed copy on disk. Delete only the PowerContext-owned managed directory after verifying its .powercontext-owned marker and removal of its registration. No global Hook or project configuration is added.

## Verification

Windows, Kimi 2.1.1 (npm CLI), Node 24.15.0 passed the real CLI + real Server + SQLite chain, including native MCP save/read/search, actual prompt injection, scheduler worker Generation with exact Source evidence, capture disablement, incorrect credentials and Server outage. Both model services in this repeatable acceptance test used controlled inference.

The same Windows, Kimi 2.1.1 and Node 24.15.0 environment also passed a manual live acceptance run against the official DeepSeek OpenAI Chat Completions API (model = deepseek-v4-flash): the live model called native MCP to save, read the exact revision and search in a fresh session; live Generation created Memory from Hook-captured Source with provenance; a fresh session answered from automatically injected Memory without calling tools. This run used an isolated Kimi home, Scope and SQLite database. It is separate from the automated tests and does not establish correct tool selection for arbitrary prompts. Other versions/platforms, HTTPS ingress and TUI reload remain unverified.

Run the focused checks from the checkout:

```text
node --test integrations/kimi-code/plugins/powercontext/tests/plugin.test.mjs
uv run pytest tests/test_cli_kimi_code.py
```

Set KIMI_CLI_BIN to the installed real binary or built main.mjs, then run uv run pytest tests/e2e/test_kimi_code_host.py. This opt-in test uses disposable Kimi homes, workspaces, Server database and local inference; it needs no live model key and does not modify the user's Kimi profile. An unset variable skips host acceptance and is not a passing qualification.
