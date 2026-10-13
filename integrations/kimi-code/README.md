# PowerContext for Kimi Code

A minimal native Kimi plugin: explicit Memory search/save and exact Artifact readback through HTTP MCP, plus bounded prompt recall and Source capture through UserPromptSubmit. All operations use one configured, existing Scope. Server owns memory generation; the Hook does not flush.

Install from a local checkout with powercontext setup kimi-code --source /path/to/powercontext --scope-id <existing-id>. Keep Node on PATH, start Server, and open a fresh Kimi session afterwards.

See the [English guide](../../docs/en/docs/integrations/kimi-code.md) or [中文指南](../../docs/zh/docs/integrations/kimi-code.md).

Real Kimi 2.1.1 on Windows with Node 24.15.0 passed native MCP save, exact-revision readback, fresh-session search, Source capture, scheduled Memory generation with Source evidence, actual model-request context injection, disabled capture, incorrect credentials and Server outage. Inference for both Kimi and the real background worker was controlled through a local OpenAI-compatible service. This establishes real host/Server interoperability, not live-model routing, other versions/platforms, HTTPS ingress, or TUI reload behavior.
