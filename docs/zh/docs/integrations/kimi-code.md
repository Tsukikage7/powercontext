---
title: Kimi Code
description: 通过原生插件、HTTP MCP 和提示词 Hook 为 Kimi Code 接入最小项目记忆。
status: community
---

# Kimi Code

本接入为 experimental，支持 minimal profile：记忆读写、自动 Source 采集和上下文注入。同一 Kimi home 内所有项目和会话共用一个配置好的 Scope；需要隔离工作时，使用另一 Kimi home 或显式修改 Scope。

## 安装

安装 Kimi Code 和 Node 24+，从与插件相同的 PowerContext checkout 安装 CLI/Server 并启动 Server：

```text
uv pip install -e ".[cli,server]"
powercontext server run --env-file .env
```

Server 开启鉴权时，先设置 POWERCONTEXT_KIMI_TOKEN 再运行 setup，才能为原生 MCP 配置鉴权；无鉴权 Server 保持变量未设置。已有 endpoint 保留既定鉴权模式，缺失运行时 token 会报错。

在另一终端，通过 Server、CLI 或 Dashboard 创建或选择已有 Scope，然后执行：

```text
powercontext setup kimi-code --source /path/to/powercontext --server-url http://127.0.0.1:8000 --scope-id <已有 Scope ID>
powercontext doctor kimi-code
kimi
```

source 仅支持本地 checkout 或其中未配置的插件目录，不能将已配置的受管理副本当作更新源。Kimi 和 Node 需在 PATH；KIMI_CLI_BIN 可指向真实二进制或构建后的 main.mjs，用于诊断和验收。KIMI_CODE_HOME 指定宿主目录，默认 ~/.kimi-code。

安装器将副本放到 <Kimi home>/plugins/managed/powercontext，注册到 plugins/installed.json；保留模型配置、其他插件和已有插件/MCP 禁用状态，发布失败时恢复原插件与注册记录。回滚本身被拒绝时，会将原插件保留在 plugins/managed/.powercontext-kimi-backup 并报告恢复路径，需先恢复再重试。重复 setup 更新插件，省略的 endpoint、Scope 和采集参数保留已安装设置。修改源文件后需要重新安装，宿主运行的是受管理副本。

安装或改配置后开启**新会话**；现有会话可能保留旧 Skill/MCP 配置。插件对用户全局生效，不限当前仓库。setup 不创建 Scope、不修改绑定。支持 HTTPS 和环回 HTTP，拒绝非环回明文 HTTP。

## 鉴权与使用

Server 开启鉴权时，在启动 Kimi 的终端设置原始 token：

```powershell
$env:POWERCONTEXT_KIMI_TOKEN = '<原始 Server token>'
```

Bash 使用 export POWERCONTEXT_KIMI_TOKEN='<原始 Server token>'。Hook 与 MCP 共用此变量，不将 token 写入插件；无鉴权 Server 可以不设置。setup 不替 Kimi 加载环境文件。

让 Kimi 保存一条项目决策，然后在新会话中搜索。插件开放 search_memory、remember_memory 和 get_artifact_revision。当前记忆返回 Atomic Memory Artifact 引用，读回时使用实际返回的 family、artifact_id、revision。自动采集 Source 不等于完成显式保存。

普通文本请求自动尝试准备上下文、采集 Source；混合输入仅提取文本，纯媒体或不支持的输入跳过。过长或敏感内容不采集，也可关闭采集：

```text
powercontext setup kimi-code --source /path/to/powercontext --no-capture-prompts
```

Hook 总预算 4 秒，通过 Kimi 的 message 字段注入。失败时普通对话继续，stderr 输出脱敏诊断。已发送采集请求但丢失响应时，结果为 unknown，不自动重试；独立提交即使文字相同，也分配独立 Source ID。

显式记忆读写不需要 Generation API。自动生成记忆需要 Server 配置 Generation 和定时处理；Source 接受不证明记忆已生成，插件不调用 flush。Kimi 登录凭据不等于 Server 的模型凭据。

## 诊断与移除

doctor 只读检查注册/启用状态、Node、MCP/Hook 声明、Server readiness 和 Scope 可访问性，不证明真实 MCP 调用或模型注入已成功。缺失/错误 token 显示 authentication_failed，已禁用的插件/MCP 不会被 setup 自动开启。错误 endpoint 或不可访问的 Scope 需显式修正后开启新会话。

使用 Kimi 的 /plugins remove powercontext 移除注册。宿主保留受管理副本；确认注册已移除并检查 .powercontext-owned 标记后，才删除 PowerContext 管理目录。接入不添加全局 Hook 或项目配置。

## 验证

Windows 上 Kimi 2.1.1（npm CLI）、Node 24.15.0 已通过真实 CLI + 真实 Server + SQLite 验收，覆盖原生 MCP 保存/读回/搜索、实际模型请求注入、后台 Worker 生成记忆及精确 Source 证据、关闭采集、错误 token 和 Server 停机。该可重复验收的两处推理服务均为受控模型边界。

同一 Windows、Kimi 2.1.1 和 Node 24.15.0 环境还通过了 DeepSeek 官方 OpenAI Chat Completions 接口的人工在线验收（model = deepseek-v4-flash）：真实模型调用原生 MCP 保存、精确读回及新会话搜索；真实 Generation 从 Hook 采集的 Source 生成记忆并保留来源证据；新会话在不调用工具的情况下使用自动注入的记忆回答。测试使用独立 Kimi home、Scope 和 SQLite 数据库。该单次在线验收不属于自动化测试，也不证明模型在任意提示词下都能正确选择工具。其他版本/平台、HTTPS 入口和 TUI reload 尚未验证。

在 checkout 执行：

```text
node --test integrations/kimi-code/plugins/powercontext/tests/plugin.test.mjs
uv run pytest tests/test_cli_kimi_code.py
```

设置 KIMI_CLI_BIN 指向真实二进制或构建后的 main.mjs，再运行 uv run pytest tests/e2e/test_kimi_code_host.py。该 opt-in 验收使用隔离 Kimi home、workspace、Server 数据库与本地推理，不需要线上模型 key，也不修改用户宿主配置。未设置变量时跳过宿主验收，跳过不代表通过。
