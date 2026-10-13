---
status: community
title: OpenCode
description: Install the PowerContext OpenCode plugin and control its local behavior.
---

# OpenCode

`community`

## Install or refresh the plugin

OpenCode 1.18.21 or newer in the 1.x line is required. Install the plugin from the same PowerContext Git ref as the
Server and CLI:

```bash
powercontext setup opencode
```

The setup command registers the native plugin globally and installs its owned `powercontext-project-context` Skill under the
OpenCode config directory. It refuses to replace an existing same-name Skill that is not owned by PowerContext. A
local checkout is also supported:

```bash
powercontext setup opencode --source .
```

Start the Server, then open a new OpenCode session:

```bash
powercontext server run
opencode
```

## Understand the behavior

For each normal user turn, the plugin asks `POST /v1/context/prepare` for one bounded context value and independently
captures eligible prompt text through `POST /v1/sources/content`. Prepared context is labelled as untrusted history
and inserted transiently before model dispatch; it is not stored in the OpenCode transcript.

Named `pc_*` tools expose curated Memory, Handoff, Experience, Skill, and read-only Candidate operations. OpenCode
asks for confirmation before a durable mutation. Candidate approval and rejection remain explicit human CLI actions.

## Configure the connection

Set variables before starting OpenCode:

```bash
export POWERCONTEXT_OPENCODE_BASE_URL=http://127.0.0.1:8000
export POWERCONTEXT_OPENCODE_CAPTURE_PROMPTS=true
opencode
```

Each OpenCode session resolves an explicit `POWERCONTEXT_OPENCODE_SCOPE_ID`, its durable Session binding, its
workspace binding, then the Server default. The resolved Scope is fixed to the Session so resume keeps the same
boundary. Set the explicit variable only to an existing Server-owned Scope.

For a Server using optional bearer authentication, set the complete header in
`POWERCONTEXT_OPENCODE_AUTHORIZATION`. Never put credentials in the URL. Plain HTTP is allowed on loopback by default;
for non-loopback HTTP, explicitly set `POWERCONTEXT_OPENCODE_ALLOW_INSECURE_HTTP=true`. HTTPS certificate validation
stays enabled. See [Connect to a remote Server](../operate/connect-remote-server.md) for setup and saved consent.
Set `POWERCONTEXT_OPENCODE_CAPTURE_PROMPTS=false` when prompts must not be persisted as Source evidence.

## Verify the installation

### Recover a failed refresh

If setup reports `recovery failed`, keep the reported backups. Setup refuses further installation writes while a
`.powercontext-install-*` path or a `.powercontext-project-context.*.bak` path remains; a missing active Skill is
not treated as a fresh installation in this state.

Restore the failed file destinations named in the error from the installation backup directory; file backups use their
original filenames. If `skills/powercontext-project-context` is absent, restore its retained Skill directory to that
location. If an active Skill or multiple backups exist, compare and reconcile their contents before choosing a version;
setup does not choose or merge them automatically. Preserve personal files and notes outside managed regions.

Only after restoring or reconciling the contents, move remaining recovery paths out of the OpenCode config directory
and rerun the setup command, then run the checks below. Removing backups just to bypass the check can discard user notes.
These safeguards cover caught filesystem failures; they do not guarantee recovery from process crashes or concurrent setups.

### Check the plugin

```bash
powercontext doctor
powercontext doctor opencode
```

The integration-specific doctor checks the OpenCode version, resolved plugin configuration, and the owned Skill.
An unavailable Server does not block OpenCode and is checked separately by the default doctor command.
