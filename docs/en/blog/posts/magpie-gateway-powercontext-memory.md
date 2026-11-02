---
slug: magpie-gateway-powercontext-memory
title: "Long-term memory for agents at the gateway: designing the Magpie PowerContext plugin"
description: "Every coding agent's requests already pass through Magpie, so I connected PowerContext there: recall before inference, capture after the reply, with no client MCP or skill. This post covers the async hooks, the recall and capture paths, and the boundaries tightened during review."
cover: assets/blog/magpie-gateway-powercontext-memory/cover.png
cover_alt: Paper ribbons from three workbenches pass through one archway and merge into a blue ribbon, while an archive cabinet beneath the arch places memory cards on it.
date: 2026-11-02
authors:
  - tsukikage7
categories:
  - Engineering
draft: false
pin: false
---

I recently contributed a PowerContext plugin to the [Magpie](https://github.com/yetone/magpie) community plugin repository. The package is `@magpie-community/gateway-powercontext`, currently at version 0.2.1, and it can be installed directly from Magpie's plugin discovery page. This post walks through its design, its implementation details and the problems I ran into along the way.

It is a long post. The first half covers why the plugin exists and how to use it; the second half covers the implementation. If you only want to get started, the first three sections are enough.

## Where the problem comes from

I use several coding agents every day: Claude Code, Codex, and occasionally Gemini CLI. Over time, one concrete annoyance stood out: project conventions do not carry across tools. A repository deploys only with Helm, tests need a temporary HOME, generated files in some directory must not be edited by hand. Once these are explained in one session, switching to another client, or simply opening a new session the next day, means explaining them again.

There are many long-term memory services, and PowerContext is one of the more complete ones. It is an open-source agent memory service from OceanBase. Beyond basic Memory, it provides user Profiles, cross-session Handoffs, Topic Memory organized by subject, and Experience distilled from task outcomes.

The difficulty is integration. The usual approach configures each client separately: install an MCP server, write a skill or add a paragraph to the system prompt, and let the model call memory tools when it sees fit. This has some practical drawbacks.

First, every client needs its own configuration. Settings end up scattered, and a new machine or a new client means starting over.

Second, whether memory is consulted depends on the model's judgment. If the model forgets to call the tool, calls it at the wrong time, or writes a poor query, recall suffers.

Third, an MCP tool call itself takes an inference round and adds a tool-call record to the conversation.

I have used Magpie for a while. It is a local model gateway: Claude Code can run on Kimi, Codex on DeepSeek, and models and accounts can be switched from the menu bar. Since every client's requests already pass through Magpie, recall and conversation capture can be handled once at the gateway, and the clients need not know that memory exists at all. That is where this plugin started.

## What it does

The plugin does two things during a request's lifecycle.

**Recall before inference.** When a request enters Magpie and before it is sent to the model, the plugin takes the latest user turn and asks PowerContext for related Memory and Profile. On the first turn of a conversation, it also fetches the latest Handoff hint so that a new session can pick up earlier work. The result is wrapped as tagged reference text and appended to the end of the latest user message, and Magpie then sends the request to the model as usual.

**Capture after the reply.** This is off by default and must be enabled explicitly. After the model's reply finishes, Magpie notifies the plugin, which asynchronously writes the complete conversation back to PowerContext as a content Source. The complete conversation includes user messages, assistant replies, and the tool calls and results that the client executed itself. Following the schedule configured on the Server, PowerContext uses its generation model in the background to turn these Sources into new Memory and Topic Memory.

Together these form a loop: a conversation in Codex today is captured and distilled, and can be recalled when you ask a question in Claude Code tomorrow.

For the client, the only change is pointing its model address at Magpie. It needs no MCP, no skill, and no prompt reminding the model to consult memory.

The plugin supports the four protocols Magpie's gateway serves: OpenAI Chat Completions, OpenAI Responses, Anthropic Messages and Gemini. A request leaves in the protocol it arrived in. The plugin only appends text within that protocol's user message structure and performs no protocol conversion.

## How to use it

**Deploy PowerContext.** The plugin does not install or start PowerContext. Deploy a Server independently by following the PowerContext [quickstart](https://powercontext.oceanbase.io/en/docs/get-started/quickstart/). For captured conversations to be distilled into memory automatically, configure a generation model and a Memory schedule on the Server; for better semantic search, configure an embedding model as well. This configuration belongs to the PowerContext service, not to the plugin.

**Install the plugin.** Magpie 0.1.1273 or newer is required. Open Discover on Magpie's plugin page and search for PowerContext, or use the command line:

```sh
magpie plugin add @magpie-community/gateway-powercontext
magpie plugin options @magpie-community/gateway-powercontext '{"serverUrl":"http://127.0.0.1:8000"}'
```

**Enter the connection details.** The plugin includes a configuration page for the Server address, an optional Scope ID and the service key, and for turning conversation capture on or off. Enter the token directly; the page adds the `Bearer` prefix. The Scope ID can be left empty, in which case the session and workspace binding, or the Server's default Scope, is used.

One version detail: Magpie 0.1.1531 fixed a problem where a package with hooks was treated as a page-only plugin, so settings saved from the page took effect only after restarting Magpie. Use 0.1.1531 or newer.

After that, clients send requests through Magpie as usual. Captured Sources and the Memory distilled from them appear in the PowerContext dashboard, and Magpie's plugin diagnostics show each hook outcome, such as recalled, empty, skipped or unavailable.

## First, async hooks for Magpie

The first obstacle was in Magpie itself.

Magpie's existing gateway middleware runs synchronously in an embedded JS engine. It suits pure computation such as rewriting requests, but it cannot make a network request and wait for the result before inference. Memory recall needs exactly that step: query the memory service first, then send the request to the model.

So I opened an issue in the Magpie repository ([yetone/magpie#2118](https://github.com/yetone/magpie/issues/2118)) proposing two opt-in capabilities.

The first is an async hook that can be awaited before inference. It has an explicit deadline, is cancelled together with the client request, and keeps the original request on failure.

The second is a notification after the reply completes. It is best effort and does not block the client's streaming output.

A key point agreed with the maintainer during the discussion was that the gateway makes exactly one model inference per client request. A hook may only add context to the request; it cannot register tools or drive its own multi-round inference loop. Tool execution and subsequent inference remain the client's responsibility. This boundary matters: it keeps Magpie a gateway rather than turning it into another agent runtime just because memory was added.

I implemented the mechanism in my fork. After review, the maintainer ported it into the main line, and it shipped in Magpie v0.1.1273 with the entry name `magpie.hooks`. The port made one adjustment: the entry name I had used, `magpie.gateway`, was already an alias for synchronous middleware, so a hook file importing `node:fs` was also loaded as middleware and failed in the embedded engine. The released version separates hook and middleware names completely.

The hook contract now looks like this. A plugin declares a `magpie.hooks` entry in `package.json` and exports `async onRequest(body, ctx)` and an optional `onResponse(reply, ctx)`. Both run in the Bun plugin host and can use Node-style APIs for network requests. `ctx` carries the protocol, model, whether the request streams, session, working directory, the request origin classification, plugin options, an `AbortSignal`, and an interface for reporting diagnostics. Client request headers and credentials are not passed to hooks.

The host enforces a few hard constraints, which I think are well restrained:

- When a hook throws, times out or returns invalid data, all its changes to the request are rolled back and the original is sent to the model.
- Changes to the client's declared tools, tool choice, `model` or `stream` are also rolled back. These fields concern authorization and what the client expects back, and a plugin should not touch them.
- A rolled-back hook receives no `onResponse` for that request.
- The whole hook chain has a ten-second limit, and each plugin's own limit is configurable from 100 to 10000 ms.

These hooks are not only for memory. Any plugin that needs to wait for external I/O before inference, such as querying a knowledge base, auditing content or connecting an internal context service, can build on them.

## The recall path

The core of the plugin is `powercontext.hooks.mjs`, a little over six hundred lines with no dependencies, plus `conversation.mjs`, which parses each protocol's conversation format. Here are the notable points, in the order a request meets them.

### Deciding whether to act

The plugin passes several checks before sending any request. Without a configured `serverUrl` it does nothing. There is no default address, so without configuration there is no outbound request at all.

Next is the request origin. The Magpie host classifies every request: a request sent directly from the local loopback address, without forwarding headers or a named gateway key, is `local`; others are `magpie-key`, `gateway-key` or `remote`. The host derives this from the connection, and clients cannot forge it. By default the plugin recalls and captures only for `local` requests.

The reason is that Magpie supports LAN sharing and can hand out gateway keys. If every request were recalled, someone using your gateway could read your memory, and their conversations would be stored in your memory library. Recall and capture for non-local origins therefore require separate opt-ins, `recallRemote` and `captureRemote`. Permission to capture does not grant access to recalled memory; the code checks the two independently. This was corrected after the maintainer pointed it out in review, which I come back to below.

Finally, the plugin checks whether the service is backing off. If the same Server address, credential and Scope recently failed to connect, timed out or reported itself unavailable, the plugin skips during the backoff interval (30 seconds by default), neither querying the service nor replaying the cache. When PowerContext is down, each request does not wait out a timeout for nothing.

### Resolving the Scope

PowerContext isolates the memory of different projects or users with Scopes. The plugin calls `v1/scope-bindings/resolve`, sending the session ID and working directory provided by Magpie as binding keys, and the Server decides which Scope the request belongs to. A Scope ID set in the options is sent as an explicit value.

The working directory is normalized and reduced to a SHA-256 digest before it is sent, so the Server never sees the raw path. When no binding exists, the plugin does not create a Scope; it gives up on recall for that request and the request proceeds. The plugin also gives the model no ability to change Scopes; the injected reference text states that the Scope ID only identifies where the recalled evidence came from and does not grant permission to change Scope.

### Fetching context and Handoff in parallel

Once the Scope is known, the plugin calls `v1/context/prepare` with the latest user turn as the query, limited by `maxBytes` (8000 bytes by default). On the first user message of a conversation it also calls `v1/handoff/hint` for the latest committed Handoff. The two requests run in parallel with `Promise.all`, each with its own timeout (1000 ms by default, including reading the response body).

Choosing the query has one subtlety. Clients such as Codex put environment information, instruction file contents and various system notices into the user message, and querying with the whole message dilutes recall. The Magpie host provides `ctx.query`, containing only what the user actually typed. The plugin queries with it while leaving the original message sent to the model untouched.

The Handoff is optional. When none has been committed, the Server returns 404 or 503; this is normal and does not trigger backoff. If the Handoff endpoint itself fails to connect or times out, only that endpoint backs off, and normal memory recall continues.

There is also a compatibility fallback. The plugin first asks for results organized as sectioned Markdown. If an older Server does not support the parameter and returns 422, the plugin falls back to requests without the format parameter for the next ten minutes.

### Injecting the reference text

The recalled content is wrapped in a pair of `<powercontext-reference>` tags, preceded by a notice telling the model that this is reference material, not a user instruction, and appended to the end of the latest user message.

Prompt injection has to be considered here. Memory content ultimately comes from past conversations and can contain any text, including a forged closing tag. The plugin escapes any `<powercontext-reference` and `</powercontext-reference` in the recalled content, so recalled text cannot close the block early and pose as content outside it.

### Keeping prompt cache hits

This is the part of the implementation that took the most thought.

A model provider's prompt cache depends on the request prefix staying the same. In a multi-turn conversation, the client resends the full history every time. If the plugin injected reference text only into the latest turn, the previous user message would lack that text on the next turn, the prefix would change and the cache would miss. If every turn were queried again, an updated memory library could return different results and the prefix would change just the same.

The plugin keeps an in-process cache per conversation that records the reference text injected into each user turn. For a new request, it first restores, unchanged, the text each earlier user turn was originally sent with, and only queries for the latest turn. The history stays byte-for-byte identical, so the cache keeps hitting.

The same user turn can also be sent several times. After the agent calls a tool and gets the result, the client sends another request carrying the same user message to continue inference. Once the plugin recognizes the same turn, it reuses the cached reference text without querying the service.

The cache has explicit limits: 8 MiB across all conversations, at most 256 conversations and 128 turns per conversation, with whole conversations evicted least recently used first. Cache keys include origin and protocol, so requests from different origins are isolated, even with remote recall enabled. Note that this limit bounds cached content, not the Bun process's total memory. Evicted turns are queried again later, so prefix retention under memory pressure is best effort.

### Failing open

If any step of the recall path fails, whether a timeout, an unavailable service, an authorization failure, an unrecognized response or a missing Scope, the plugin skips injection and the request goes to the model unchanged. When the client cancels a request, in-flight service requests are aborted too, and cancellation does not trigger backoff.

For the user, the memory service's state never affects whether the model can answer. The worst case is a turn without recalled context.

## The capture path

### When to write

Capture happens in `onResponse`, after the reply finishes. Failed, truncated or incomplete replies are not captured.

Writing on every turn of a multi-turn conversation would send the Server large amounts of repeated prefix. The plugin merges per conversation: after a reply, it holds the conversation and starts an idle timer, ten minutes by default. If the same conversation gets a new turn within those ten minutes and the history continues the held one, the new complete conversation replaces it and the timer restarts. If the history diverges, for example because the user went back and edited an earlier message, the held conversation is written first. The held conversation is actually written when the timer expires.

The write goes to `v1/sources/content`, with metadata marking the origin as `magpie`, the event as `conversation_completed`, and the protocol. The session ID and working directory in metadata are again kept only as SHA-256 digests. The write has its own ten-second timeout and is not retried on failure.

### Reconstructing Responses history

The OpenAI Responses protocol lets a client reference the previous turn stored on the server with `previous_response_id` instead of resending the full history. The plugin then sees only the increment, and capturing it directly would produce a fragmentary conversation.

The plugin records the full input and output of every response it has handled, and reconstructs the full history from them when it sees `previous_response_id`. This is used only for capture; the request sent to the model is not changed. If the referenced history was not handled by this process, for example because Magpie restarted, the plugin skips the capture rather than storing an incomplete conversation as if it were complete.

### Credential checks

Captured content includes tool arguments and results, which contain files the agent read and output of commands it ran, so secrets are bound to appear. Before holding a conversation, the plugin checks user message text and tool arguments and results. Coverage includes common credential assignments, JSON fields, prefixed environment variables (including `PGPASSWORD` and `MYSQL_PWD`), `user:password@` in URLs, Stripe `sk_live_` and `rk_live_` keys, Google `AIza` keys and more. The checks run on structured arguments before JSON serialization, so escaping cannot hide a match.

On a match, the whole conversation is skipped instead of masking only the matched part. Masking is hard to do cleanly, and the remaining context can itself leak information.

This check is only a safeguard. It cannot identify secrets in arbitrary formats, such as a password written in prose or passed as a command-line flag. That is why capture stays off by default, and users should confirm that these conversations may be stored in the Server they configured before turning it on.

### Where captured content goes

Captured conversations are processed by PowerContext's Memory and Topic Memory schedules. Notably, they do not feed Experience. PowerContext's Experience considers only Sources whose metadata marks them as task outcomes, and those must be recorded by the agent with PowerContext MCP's `record_task_outcome` tool when a task actually completes or is interrupted.

The plugin does not infer task outcomes from conversations: an assistant reply claiming "done" does not mean the task succeeded, and recording it as fact would pollute the experience library. For Experience, connect PowerContext's MCP server to the client separately; it works alongside this plugin. Likewise, when an agent needs to search or write memory explicitly, that goes through MCP; the plugin itself provides no tools.

## The review

The plugin's PR is [magpie-community/plugins#117](https://github.com/magpie-community/plugins/pull/117). It was opened on October 14 and merged on October 29 after several rounds of review. At merge it added about 2,900 lines: roughly 1,100 lines of tests, 650 lines for the configuration page, 800 lines of plugin code, and the rest documentation. Looking back, much of the plugin's design took shape during review. Here are the key turning points.

**Removing automatic installation.** The earliest version aimed to work out of the box: on installation it downloaded uv's install script, installed Python and PowerContext, and then started and supervised the service process. The maintainer's first review pointed out that downloading and running a shell script and installing packages from PyPI at run time is too risky for a community plugin, and that a gateway plugin should not manage a service's lifecycle. I removed all of it, and the plugin now only connects to an already deployed service. It became a few plain `.mjs` files with no dependencies, no executable entry and no lockfile, which made it much easier to review.

**Capture off by default.** The second round focused on capture: no default service address, capture off by default, and even when enabled, only local requests.

**Restricting recall by origin too.** After the capture fixes, the maintainer found a boundary I had missed: the previous version restricted capture by origin but not recall. Someone connecting with a shared key could still send queries to my service and receive my memory. After the fix, recall checks the origin before resolving the Scope, touching the cache or sending any request, and capture and recall permissions are fully separate.

**Credential checks and test timing.** Later rounds mainly broadened credential coverage and fixed timing in tests. Initially I gave every test request a 50 ms timeout, which failed from transport latency when the maintainer's machine was under heavy load. The tests now keep a 1000 ms timeout for normal requests, shorten it only for a deliberately hung request after the fixture receives it, and advance a stubbed `Date.now()` for backoff instead of waiting in real time.

**Following the released host entry.** When the async hooks were released under the `magpie.hooks` entry name, the plugin renamed its entry file to `powercontext.hooks.mjs` and updated its manifest and check script.

The plugin now has more than sixty test cases that use a fake HTTP service to cover recall, capture, timeouts, backoff, origin restrictions, credential checks and cache behavior, without starting PowerContext. End-to-end validation against a real service runs separately in my local environment, with results posted in the PR.

## The configuration page

The plugin also includes a configuration page. It uses only Magpie's released `magpie.options.get/set` and theme bridge, and needed no new host API.

The page shows the current connection summary, opens a dialog for the address, Scope ID and key, toggles capture, and explains what recall and capture each send to the service. Saving preserves other advanced options set in the JSON editor. The page follows Magpie's light, dark and system themes, using PowerContext's official reverse icon in dark mode. Its language follows Magpie's setting, with Simplified Chinese, Traditional Chinese, English, Japanese and German, and switching languages does not discard unsaved form edits.

One note: the key is stored in Magpie's local plugin configuration, not the system keychain. If credentials were previously configured through the `POWERCONTEXT_AUTHORIZATION` environment variable, that variable is still read when no key is entered on the page.

## Known limitations

To help you judge whether the plugin fits your setup, here are its current limitations in one place:

- Capture is best effort. Held conversations not yet written are lost when the Magpie process exits. Failed writes are not retried, and when a conversation is written in several fragments, the remaining fragments stop after a failure.
- The recall cache is in-process. After Magpie restarts, turns are queried again, and the first request's prefix may differ from before.
- Credential checks cannot cover sensitive information in every format.
- The plugin does not deploy or operate PowerContext, and the quality of distilled memory depends on the generation model configured on the Server.
- Captured conversations do not feed Experience.

## Closing

The plugin took a little over two weeks from issue to merge. More time went into getting the boundaries right than into writing code: what a gateway should and should not do, which data may be sent by default and which requires explicit consent, and how a service failure can avoid affecting normal use. Review kept pressing on these questions, and the result is far more restrained than my original design, and more trustworthy for it.

Thanks to Magpie's maintainer yetone for reading the code line by line in review, running it to verify, and porting the async hooks into the main line. Please try the plugin; if you run into problems, open an issue in the plugin repository or join Magpie's [Discord](https://discord.gg/vGSnD3ZKQF). And if there is something else you want to do at the gateway, you can now build it on `magpie.hooks`.

## Links

- Plugin source and documentation: [magpie-community/plugins · packages/powercontext](https://github.com/magpie-community/plugins/tree/main/packages/powercontext)
- Magpie async hook documentation: [gateway-request-hooks.md](https://github.com/yetone/magpie/blob/main/docs/subsystems/gateway-request-hooks.md)
- Magpie: [usemagpie.ai](https://usemagpie.ai) · [GitHub](https://github.com/yetone/magpie)
- PowerContext: [GitHub](https://github.com/oceanbase/powercontext) · [Documentation](https://powercontext.oceanbase.io/en/docs/)
