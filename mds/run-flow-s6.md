# kama 分层记忆与上下文压缩调用链详解（S6：三层记忆 + 上下文压缩 + 流重试）

本文档梳理 S6 阶段的**主要实现逻辑**与**完整执行流程**。S6 相对 S5 保持双进程 IPC、会话模型和权限体系不变，重点解决"跑得久之后上下文越来越贵、越来越容易断"的问题：

1. **分层记忆** —— 在原有 `notes.md` 之外，新增 **全局上下文** `~/.kama/context.md` 与 **项目上下文** `.kama/context.md`，与 session notes 一起按固定顺序注入 system prompt。
2. **上下文预算与压缩** —— `tool_result` 超长自动截断；支持手动 `/compact` 与可选的**按上下文占比自动压缩**，把整段对话压成结构化交接摘要。
3. **流式重试与 token 上限** —— LLM 流式调用遇到网络中断最多尝试 3 次（退避 1s、2s）；`max_tokens` 从 4096 提到 8192，并处理"输出被截断在 tool_use 中间"的不完整消息。
4. **可观测性** —— 新增 `llm.usage.context_pct`、`context.compacted` 事件与 TUI 上下文占用进度条。

> S4 版本（会话 + 分层语义记忆 + TUI 输入框）见 `run-flow-s4.md`；S5 版本（权限审批 + 失败分类 + 持久化策略）见 `run-flow-s5.md`。本文档是 S6 演进版。

---

## 零、S5 vs S6 变化（先看这个）

| | S5（工具执行安全） | S6（分层记忆 + 压缩 + 流重试） |
|---|---|---|
| system prompt 注入 | 仅 `notes.md` | **Global Context → Project Context → Session Notes** |
| 项目级指令 | 无 | **`.kama/context.md`（随仓库）** |
| 用户级指令 | 无 | **`~/.kama/context.md`（跨项目）** |
| tool_result 预算 | 全量进上下文 | **超过 8000 字符截断为前 4000 + 省略说明** |
| 上下文压缩 | 无 | **手动 `session.compact` + 可选自动阈值** |
| 压缩产物 | 无 | **`summary_<ts>.md` / `thread_<ts>.jsonl.bak`** |
| LLM 流式调用 | 单次 | **最多 3 次尝试，退避 1s / 2s / 4s** |
| `max_tokens` | 4096 | **8192** |
| 不完整 tool_use | 可能触发 `messages.invalid` | **补齐合成 tool_result，保持消息配平** |
| 上下文占用率 | 无 | **`llm.usage.context_pct` + TUI 进度条** |
| TUI 交互 | 权限选择器 | **新增 `/compact` 与压缩横幅** |
| IPC 命令 | 8 个 | **9 个**（+`session.compact`） |
| IPC 事件 | 20 个 | **21 个**（+`context.compacted`） |
| 配置 | `[permission]` | **新增 `[compaction]`** |

一句话概括：**S5 保证"工具敢不敢执行"，S6 保证"对话长了还能继续跑、网络抖了还能接上"。**

---

## 一、入口与用户可见行为

入口没有变化，新增两个用户可见能力：

| 操作 | 位置 | 行为 |
|---|---|---|
| `/compact` | TUI 输入框 | 手动把当前 session 的 thread 压缩成摘要，成功后显示 `⚡ Context compacted summary=… saved≈…` |
| 上下文占用条 | 每次 `llm.usage` 事件 | `ctx:42.3% ████████░░░░░░░░░░░░`，≥70% 黄色，≥85% 红色 |
| `context.md` | 项目根 `.kama/context.md` / 用户目录 `~/.kama/context.md` | 每次 run 前读取，注入 system prompt |

项目里自带的 `.kama/context.md`：

```markdown
# 行为规范

## 创建文件准则

需要创建文件时，所有的文件都需要在 ./workspace 下创建。
```

配套改动是 `.gitignore` 增加 `workspace/`——**项目上下文约定的产物目录不进入版本管理**。

---

## 二、分层记忆：四层上下文

S6 之后，模型看到的上下文由四层组成，**前两层是 system prompt，后两层是消息/记忆文件**：

| 层 | 载体 | 读取者 | 注入位置 | 生命周期 |
|---|---|---|---|---|
| **Global Context** | `~/.kama/context.md` | `AgentRunner` | system prompt | 跨项目、跨 session |
| **Project Context** | `<cwd>/.kama/context.md` | `AgentRunner` | system prompt | 随仓库 |
| **Session Notes** | `<session>/notes.md` | `SessionStore.read_notes()` → `ExecutionContext` | system prompt | 单会话，`note_save` 写入 |
| **Conversation** | `<session>/thread.jsonl`（或压缩后的摘要对） | `SessionStore.read_messages()` → `messages` | 消息列表 | 单会话，可压缩 |

### 2.1 加载（`core/memory/loader.py:7`）

```python
def load_context_file(path: Path) -> str:
    p = path.expanduser()
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()
```

三个约定：路径不存在返回空串；内容统一 `strip()`；编码固定 UTF-8。**文件缺失不是错误**，这让 context.md 完全可选。

### 2.2 装配（`core/runner.py:118-119`）

```python
global_ctx = load_context_file(Path("~/.kama/context.md").expanduser())
project_ctx = load_context_file(Path(".kama/context.md"))

context = ExecutionContext(
    ...,
    prefill_messages=history,
    session_notes=notes,
    global_context=global_ctx,
    project_context=project_ctx,
)
```

项目上下文用的是**相对路径**，指向 daemon 启动时的工作目录——因此"项目"的边界实际上由 `kama-core` 在哪个目录启动决定。

### 2.3 注入顺序（`core/context.py:30`）

```python
def system_prompt(self, base: str) -> str:
    parts = [base]
    if self.global_context.strip():
        parts.append("\n\n## Global Context\n" + self.global_context.strip())
    if self.project_context.strip():
        parts.append("\n\n## Project Context\n" + self.project_context.strip())
    if self.session_notes.strip():
        parts.append(
            "\n\n## Session Notes\n"
            + self.session_notes.strip()
            + "\n\nRemember important durable facts by calling note_save."
        )
    return "".join(parts)
```

最终结构：

```
<base system prompt>

## Global Context
<~/.kama/context.md>

## Project Context
<.kama/context.md>

## Session Notes
<notes.md>

Remember important durable facts by calling note_save.
```

**顺序即优先级提示**：越靠近前面的内容越"通用且稳定"，越靠后的越"具体且易变"。四层空三层时不会产生空 section（每个 `if` 都有 `strip()` 判断）。

---

## 三、上下文预算：`tool_result` 截断

### 3.1 规则（`core/compact/budget.py`）

```python
TOOL_RESULT_LIMIT = 8_000
TOOL_RESULT_KEEP = 4_000

if block.get("type") == "tool_result" and isinstance(block.get("content"), str):
    text = block["content"]
    if len(text) > limit:
        omitted = len(text) - keep
        block["content"] = (
            text[:keep]
            + f"\n[... {omitted} chars omitted. Full output in run events.]"
        )
```

四条边界行为都有单测覆盖：

- `len <= limit`：原文不动（恰好 8000 也不动）；
- `len > limit`：保留前 4000 字符 + 省略提示；
- 只处理 `role == "user"` 里的 `type == "tool_result"` 字符串内容；
- `text` block、assistant 消息、同一条消息里的其他 tool_result 都各自独立处理。

### 3.2 应用点（`core/session/store.py:107-108`）

```python
messages = self._trim_orphan_tool_use(messages)
from kama_claude.core.compact.budget import truncate_tool_results
return truncate_tool_results(messages)
```

截断发生在**读 thread 时**，而不是写 thread 时：

- `thread.jsonl` 保留完整原始输出（审计 / 事后排查不受影响）；
- 每次 run 组装 `messages` 时才做投影，**只有进模型的那一份被裁剪**；
- 与 S4 的"裁孤块"串在同一个读路径上，顺序是先配平 tool_use/tool_result，再压缩体积。

---

## 四、手动压缩：`session.compact` 全链路

### 4.1 IPC 契约（`core/bus/commands.py:92-100`）

```python
class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""          # 可选：告诉压缩器重点关注什么

class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int
```

`focus` 会拼进压缩提示词（`IMPORTANT: Pay special attention to: ...`），当前 TUI 的 `/compact` 传空串。

### 4.2 daemon handler（`core/app.py:139`）

```python
async def _session_compact_handler(self, params):
    cmd = SessionCompactCommand.model_validate(params)
    return await self._sessions.compact(cmd.session_id, cmd.focus)
```

### 4.3 `SessionManager.compact()`（`core/session/manager.py:135`）

```python
async def compact(self, sid: str, focus: str = "") -> Any:
    session = self._get_session(sid)
    lock = self._locks[sid]
    if lock.locked():
        raise HandlerError(SESSION_BUSY, "session busy")
    if self._provider is None:
        raise HandlerError(-32020, "provider not available for compaction")

    async with lock:
        messages = self._store.read_messages(sid)          # 已截断、已配平
        compactor = Compactor(self._bus, session_dir, sid)
        result = await compactor.compact_messages(messages, self._provider, focus=focus)
        if result is None:
            raise HandlerError(-32021, "compaction failed or not beneficial")
        self._store.write_compacted(sid, [
            {"role": "user", "content": result.summary_text},
            {"role": "assistant", "content": "Understood, I'll continue from this summary."},
        ])
        return SessionCompactResult(
            summary_tokens=result.summary_tokens,
            saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
        )
```

四条约束：

1. **加 session 锁**：run 进行中手动压缩会被 `SESSION_BUSY` 拒绝，避免读到半截对话。
2. **用独立 provider**：`CoreApp` 启动时创建一个 `AnthropicProvider` 专门给手动压缩用（`app.py:231`），与 run 内被 `TracingProvider` 包裹的 provider 分开。
3. **失败不改数据**：`compact_messages` 返回 `None` 时直接抛 `-32021`，thread 原样保留。
4. **失败不静默**：错误通过 JSON-RPC 返回给客户端，TUI 显示 `compact error: ...`。

### 4.4 覆盖写入与备份（`core/session/store.py:133`）

```python
def write_compacted(self, sid, messages):
    path = self.session_dir(sid) / "thread.jsonl"
    bak = self.session_dir(sid) / f"thread_{ts}.jsonl.bak"
    if path.exists():
        path.rename(bak)                 # 原 thread 备份
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps({"ts": _now(), "role": msg["role"],
                                "content": msg["content"]}) + "\n")
```

`thread.jsonl` 是唯一会被**重写**的记忆文件（S4 以来一直是纯追加）。所以这里采用"先改名为 `.bak`，再写新文件"的两步法：即使进程在写新文件时崩溃，原数据仍在备份里。

压缩后的 thread 只有两条消息：

```jsonl
{"ts": "...", "role": "user", "content": "## 1. Original Goal\n..."}
{"ts": "...", "role": "assistant", "content": "Understood, I'll continue from this summary."}
```

这是合法的 `user → assistant` 交替序列，下一轮 run 直接把这两条作为 `prefill_messages` 即可继续。

### 4.5 TUI `/compact`（`tui/app.py:431-471`）

```python
if content == "/compact":
    event.text_area.text = ""
    if self._client is not None and self._session_id is not None and not self._busy:
        self.run_worker(self._do_compact(), name="compact", exclusive=False)
    return
```

`/compact` 是**客户端侧拦截**的指令，不会作为聊天消息发给模型。实际 IPC 调用：

```python
result = await self._client.send_command(
    "session.compact", {"session_id": self._session_id, "focus": ""}
)
```

成功后把 `_last_context_pct` 归零并打印摘要/节省 token 数。

---

## 五、自动压缩：基于 `context_pct` 的阈值触发

### 5.1 上下文占比从哪来

`AnthropicProvider` 维护一张模型 → context window 表（`provider.py:13-16`）：

```python
_MODEL_CONTEXT_WINDOWS = {
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-4-7": 200_000,
}

context_pct = usage.input_tokens / _context_window(self._model)
```

`context_pct` 同时写入 `UsageStats`（供 loop 判断）和 `LlmUsageEvent`（供 TUI 展示）。

### 5.2 触发条件（`core/loop.py:119-127`）

```python
if (
    not context.is_done()
    and response.stop_reason == "tool_use"
    and self._compactor is not None
    and self._compact_threshold > 0
    and response.usage is not None
    and response.usage.context_pct >= self._compact_threshold
):
    await self._compactor.compact(context, self._provider)
```

五个条件缺一不可：

| 条件 | 原因 |
|---|---|
| `not context.is_done()` | 已经成功/失败的 run 不需要再压缩 |
| `stop_reason == "tool_use"` | 此时工具结果刚追加完，messages 末尾是 `user(tool_result)`，压缩后替换成 `user(summary) → assistant(ack)` 是合法的下一次输入 |
| `_compactor is not None` | 允许不启用 |
| `threshold > 0` | **默认 0 表示禁用自动压缩** |
| `context_pct >= threshold` | 达到配置占比 |

配置：

```toml
[compaction]
auto_threshold = 0.0        # 0 表示禁用；推荐手动 /compact
tool_result_limit = 8000
tool_result_keep = 4000
```

环境变量：`KAMA_COMPACT_THRESHOLD` / `KAMA_COMPACT_TOOL_LIMIT` / `KAMA_COMPACT_TOOL_KEEP`。

### 5.3 压缩动作（`core/compact/compactor.py:73`）

```python
result = await self.compact_messages(context.messages, provider, focus)
if result is None:
    return None                       # 失败 → 保留原 messages

context.messages = [
    {"role": "user", "content": result.summary_text},
    {"role": "assistant", "content": "Understood, I'll continue from this summary."},
]
self._write_summary(result.summary_text)          # summary_<ts>.md
await self._bus.publish(ContextCompactedEvent(...))  # 实时通知 TUI / trace
```

与手动压缩的三点区别：

| | 手动 `/compact` | 自动压缩 |
|---|---|---|
| 触发者 | 用户 | `AgentLoop` 阈值判断 |
| 影响范围 | 重写 `thread.jsonl`（含 `.bak`） | 只替换**内存中的** `context.messages` |
| 摘要落盘 | 写入 `thread.jsonl` | 写入 `summary_<ts>.md` |
| 事件 | 无（RPC 结果直接返回） | 发 `context.compacted` |

---

## 六、压缩器内部：六段式交接摘要

### 6.1 提示词（`compactor.py:18`）

要求模型严格输出六段：

```
## 1. Original Goal
## 2. Completed Steps
## 3. Key Constraints & Discoveries
## 4. Current File State
## 5. Remaining TODOs
## 6. Critical Data
```

设计意图是**让另一个 LLM 实例只靠摘要就能接手**：不仅要有"做了什么"，还要有"文件现在是什么状态""还差什么""哪些值必须逐字保留（ID、错误信息、配置）"。提示词明确要求省略推理过程和中间尝试，只保留结论。

### 6.2 消息 → 纯文本（`compactor.py:164`）

```python
[USER]
...
[ASSISTANT]
<tool_call name=read_file id=toolu_1>
{'path': 'x'}
</tool_call>
<tool_result id=toolu_1>
...
</tool_result>
```

把结构化的 content blocks 摊平成模型易读的文本；tool_use 的输入用 Python dict 表示（不是 JSON），对压缩任务足够。

### 6.3 一次独立的 LLM 调用（`compactor.py:105`）

```python
silent_bus = EventBus()                 # 独立总线
response = await provider.chat(
    messages=[{"role": "user", "content": prompt + history_text}],
    tool_schemas=[],
    bus=silent_bus,                     # 不污染 run 的事件流
    run_id="compact",
    step=0,
    system="You are a helpful assistant that summarizes conversations.",
)
```

关键点：

- **空工具表**：压缩调用不允许模型再调工具；
- **独立的 `EventBus`**：压缩过程产生的 `llm.token` / `llm.usage` 不会写进本次 run 的 `events.jsonl`，也不会推给前端；
- **失败即放弃**：异常或空摘要返回 `None`，绝不把半截摘要写进上下文；
- 粗略 token 估算用"字符数 / 4"，真实摘要 token 优先取 `usage.output_tokens`。

---

## 七、LLM 流式重试

### 7.1 重试策略（`core/llm/provider.py:22-23, 98-121`）

```python
_MAX_STREAM_RETRIES = 3
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)

for attempt in range(1, _MAX_STREAM_RETRIES + 1):
    text_parts = []
    try:
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if attempt == 1:
                    await bus.publish(LlmTokenEvent(...))
                text_parts.append(text)
            final_message = await stream.get_final_message()
        break
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
        if attempt == _MAX_STREAM_RETRIES:
            raise
        await asyncio.sleep(_RETRY_BACKOFF_S[attempt - 1])
```

| 项 | 值 |
|---|---|
| 最多尝试次数 | 3 |
| 退避间隔 | 实际 sleep 1s、2s（常量表里的 4s 在第 3 次失败前不会被用到，因为第 3 次直接抛出） |
| 捕获异常 | `RemoteProtocolError` / `ReadError` / `ConnectError` |
| 不捕获 | 认证错误、限流错误、请求非法等（交给上层 `llm_error`） |
| token 事件 | **只在第一次尝试时发**（避免 TUI 重复） |

每次尝试都会重置 `text_parts`，因此最终返回给 AgentLoop 的是**最后一次成功尝试的完整文本**；失败尝试里已经推给客户端的 token 不会被撤回。

### 7.2 输出上限与"半截 tool_use"

```python
"max_tokens": 8192,        # provider.py:88，由 4096 提升
```

输出被 `max_tokens` 截断时，Anthropic 可能已经产生了完整的 `tool_use` block 但没有执行。S5 及以前这类消息会留在上下文里导致下一次请求配平失败；S6 在 `AgentLoop` 中补齐：

```python
elif response.stop_reason == "max_tokens" and response.tool_calls:
    for tc in response.tool_calls:
        context.add_tool_result(
            tc.id,
            "Error: output token limit reached before this tool call could be completed. "
            "Please break the task into smaller steps and try again.",
            is_error=True,
        )
```

这不是"重试工具"，而是**保证 `tool_use` 一定有配对的 `tool_result`**，让对话保持合法，模型下一步能看到错误并拆分任务。

---

## 八、IPC 契约增量

### 8.1 新命令

| 方法 | 参数 | 结果 |
|---|---|---|
| `session.compact` | `session_id`、`focus`（可选） | `summary_tokens`、`saved_tokens` |

注册位置：`CoreApp.run()` → `server.register("session.compact", ...)`（`app.py:258`）。

### 8.2 新事件与字段

| 事件/字段 | 变化 | 内容 |
|---|---|---|
| `context.compacted` | 新增事件 | `session_id`、`run_id`、`original_tokens`、`summary_tokens`、`ts` |
| `LlmUsageEvent.context_pct` | 新增字段（默认 0.0） | 本次输入占模型 context window 的比例 |

`context.compacted` 只在**自动压缩**时发布；手动压缩的统计信息通过 RPC 返回值给出。

---

## 九、TUI 增量

### 9.1 上下文占用条（`tui/app.py:545`）

```python
def _render_ctx_bar(self, pct: float) -> str:
    filled = int(pct * 20)
    bar = "█" * filled + "░" * (20 - filled)
    label = f"ctx:{pct * 100:.1f}%"
    if pct >= 0.85:   color = "bold red"
    elif pct >= 0.70: color = "yellow"
    else:             color = "dim"
    return f"[{color}]{label} {bar}[/{color}]"
```

追加在每次 `llm.usage` 那一行的末尾：

```
  tokens  in=12000 out=800 cache=0  ctx:6.0% █░░░░░░░░░░░░░░░░░░░
```

### 9.2 压缩提示

| 来源 | 显示 |
|---|---|
| `/compact` 开始 | `⚡ compacting context...` |
| RPC 成功 | `⚡ Context compacted summary=… tokens saved≈… tokens` |
| RPC 失败 | `compact error: ...` |
| 自动压缩事件 | `⚡ Context compacted original≈… tokens → summary=… tokens` |

两种成功提示文本不同（一个带 `saved`，一个带 `original → summary`），正好对应手动/自动两条路径。

订阅的 topic 新增 `context.*`（`tui/app.py:613-616`）。

---

## 十、完整执行流程

### 10.1 一次 run 的记忆装配

```
SessionManager.send_message()
  └─ AgentRunner.run_and_capture()
       ├─ history = store.read_messages(sid)
       │    ├─ 丢弃坏行 / 非法 role
       │    ├─ _trim_orphan_tool_use()     ← 配平
       │    └─ truncate_tool_results()     ← 8000/4000 截断
       ├─ notes      = store.read_notes(sid)
       ├─ global_ctx = ~/.kama/context.md
       ├─ project_ctx= .kama/context.md
       └─ ExecutionContext(prefill_messages=history + 三层记忆字段)
            └─ AgentLoop.run()
                 └─ provider.chat(system=context.system_prompt(base))
```

### 10.2 自动压缩发生在 step 内部

```
step N
  ├─ plan:    LLM 返回 stop_reason=tool_use + usage.context_pct
  ├─ observe: assistant content blocks 入 messages
  ├─ act:     执行工具 → tool_result 入 messages（末尾是 user）
  ├─ 压缩检查: pct >= auto_threshold ?
  │    ├─ 否 → 继续
  │    └─ 是 → Compactor.compact()
  │         ├─ 用 silent_bus 调一次 LLM 生成六段摘要
  │         ├─ 失败 → 保留原 messages，继续
  │         ├─ 成功 → messages = [user(summary), assistant(ack)]
  │         ├─ 写 summary_<ts>.md
  │         └─ publish context.compacted（trace / events.jsonl / IPC → TUI）
  └─ StepFinishedEvent → 下一步 LLM 看到的是压缩后的 messages
```

注意压缩发生在 `StepFinishedEvent` 之前，所以前端看到的事件顺序是：

```
tool.call_finished → context.compacted → step.finished
```

### 10.3 手动 `/compact` 的时序

```
TUI: 用户输入 /compact + Enter
  ├─ 客户端拦截，不发给模型
  └─ run_worker(_do_compact)
       └─ send_command("session.compact", {session_id, focus:""})
            ▼
daemon: _session_compact_handler
  └─ SessionManager.compact
       ├─ lock.locked() ? → SESSION_BUSY
       ├─ provider 缺失 ? → -32020
       ├─ read_messages()（已截断/配平）
       ├─ Compactor.compact_messages()（silent_bus 调 LLM）
       │    └─ 失败/空摘要 → -32021
       ├─ write_compacted()
       │    ├─ thread.jsonl → thread_<ts>.jsonl.bak
       │    └─ 写入 [user(summary), assistant(ack)]
       └─ 返回 {summary_tokens, saved_tokens}
            ▼
TUI: ⚡ Context compacted summary=… saved≈…；ctx 进度条归零
```

---

## 十一、落盘布局变化

```
~/.kama/
  ├── context.md                 ← 新增：全局上下文（跨项目）
  └── sessions/<sess-id>/
        ├── meta.json
        ├── thread.jsonl         ← 可能被手动压缩重写
        ├── thread_<ts>.jsonl.bak← 新增：手动压缩前的完整备份
        ├── summary_<ts>.md      ← 新增：自动压缩生成的摘要
        ├── notes.md
        └── runs/<run_id>/
              ├── events.jsonl   ← 自动压缩会多一条 context.compacted
              └── .tasks/

<project>/.kama/context.md       ← 新增：项目上下文（随仓库）
<project>/workspace/             ← .kama/context.md 约定的文件创建目录（已 gitignore）
```

**为什么 summary 文件要单独落盘**：自动压缩只改内存里的 `context.messages`，如果进程在 run 结束前崩溃，这个摘要不会出现在 thread 里；`summary_<ts>.md` 是它唯一的持久痕迹，也方便人工检查压缩质量。

---

## 十二、文件改动清单（S6）

| 类别 | 文件 | 内容 |
|---|---|---|
| 新增（记忆） | `core/memory/loader.py`、`core/memory/__init__.py` | `context.md` 加载 |
| | `.kama/context.md` | 项目级行为规范（文件建在 `./workspace`） |
| 新增（压缩） | `core/compact/budget.py` | tool_result 截断预算 |
| | `core/compact/compactor.py` | 六段式摘要、压缩执行、摘要落盘、事件 |
| 修改（上下文） | `core/context.py` | `global_context` / `project_context` + 三层注入 |
| | `core/runner.py` | 读取两层 context.md、构造 Compactor、传入阈值 |
| | `core/loop.py` | 自动压缩触发、`max_tokens` 半截 tool_use 补齐 |
| 修改（会话） | `core/session/manager.py` | 手动 `compact()`、provider 注入 |
| | `core/session/store.py` | 读取时截断、`write_compacted()` 备份重写 |
| 修改（LLM） | `core/llm/provider.py` | 流式重试、8192 max_tokens、context_pct |
| | `core/llm/types.py` | `UsageStats.context_pct` |
| 修改（协议） | `core/bus/commands.py`、`core/bus/events.py` | `session.compact`、`context.compacted`、`context_pct` |
| 修改（daemon） | `core/app.py` | 压缩 provider、handler 注册 |
| | `core/config.py` | `[compaction]` 与三个环境变量 |
| 修改（前端） | `tui/app.py` | `/compact`、ctx 进度条、压缩事件渲染 |
| 修改（仓库） | `.gitignore` | 忽略 `workspace/` |
| 文档 | `WIRE_PROTOCOL.md` | 随模型变化重新生成 |

测试增量：

- `tests/unit/test_budget.py` —— 8000/4000 边界、text/assistant 不受影响、多 tool_result 独立截断。
- `tests/unit/test_compactor.py` —— 摘要调用、消息替换、summary 文件、事件发布、失败保留上下文。
- `tests/unit/test_context_system_prompt.py` —— 三层记忆顺序、空层省略、note_save 提示。
- `tests/unit/test_memory_loader.py` —— 存在/缺失/空白 context.md。

---

## 十三、关键设计要点

1. **记忆分层而不是堆在一起**：全局 / 项目 / 会话 / 对话四种生命周期不同的信息，各自有载体和注入位置，便于独立演进与清理。
2. **注入顺序固定**：global → project → session，越通用越靠前、越具体越靠后，符合模型对 system prompt 的阅读习惯。
3. **写完整、读裁剪**：`thread.jsonl` 永远是完整原始记录；8000/4000 截断只发生在读出来组装 `messages` 时，兼顾审计与成本。
4. **先配平再压缩**：`_trim_orphan_tool_use` 在 `truncate_tool_results` 之前，保证截断不会破坏 `tool_use` / `tool_result` 的配对关系。
5. **手动压缩可逆**：`thread.jsonl` 重写前先改名为 `.bak`，任何一步失败都还有原始对话。
6. **自动压缩只改内存**：不碰 thread 文件，失败直接跳过，属于"尽力而为"的优化；手动压缩才是"确定生效"的操作。
7. **压缩调用隔离**：独立 `silent_bus` + 空工具表 + `run_id="compact"`，压缩产生的 token 不会混进本轮事件流，也不会触发工具。
8. **六段式交接摘要**：不是泛泛总结，而是为"另一个 LLM 接手"设计的结构化 handoff（目标、完成、约束、文件状态、TODO、关键数据）。
9. **压缩时机选在 tool_use 之后**：此时 messages 末尾是 user 的 tool_result，替换成 `user(summary) → assistant(ack)` 后仍是合法交替，下一次 LLM 调用无缝继续。
10. **默认不自动压缩**：`auto_threshold=0.0`，把控制权交给用户 `/compact`，避免在用户不知情时改写上下文。
11. **流式重试只针对网络错误**：`RemoteProtocolError` / `ReadError` / `ConnectError` 才重试；认证、限流、参数错误立即失败，避免放大问题。
12. **token 事件不重复**：重试时不再推送 token，防止 TUI 出现重复文本；代价是失败尝试的部分输出会留在屏幕上（见第十四节）。
13. **输出上限翻倍 + 半截工具补齐**：8192 减少截断概率；万一截断在 tool_use 中间，合成错误 tool_result 保证对话合法。
14. **上下文占用可视化**：`context_pct` 同时进事件与 TUI 进度条，让用户在做 `/compact` 决策前有量化依据。

---

## 十四、边界情况与当前实现现状

以下不是设计目标，而是代码当前的真实行为，排查问题时很有用。

### 14.1 `[compaction]` 的两个预算参数尚未接线

```python
# store.py:108
return truncate_tool_results(messages)      # 使用模块默认 8000 / 4000
```

`config.compaction.tool_result_limit` / `tool_result_keep` 能被解析、能通过环境变量设置，但**没有任何调用点把它们传给 `truncate_tool_results`**。改了这两个配置目前不生效，只有 `auto_threshold` 真正被 `AgentLoop` 使用。

### 14.2 自动压缩后 thread 回写可能丢失/错位

`AgentRunner` 在 run 开始时记录：

```python
prefill_len = len(history)
...
store.append_messages(session.id, context.messages[prefill_len:], run_id=run_id)
```

这个切片假设 `context.messages` 只增不减。但自动压缩会把它替换成 2 条消息：

- 若 `prefill_len >= 2`，`context.messages[prefill_len:]` 为空 → **本轮摘要和后续消息都不会写进 thread.jsonl**；
- 若压缩后又产生若干消息、总长度重新超过 `prefill_len`，切片取到的是"尾部若干条"，与预期的"本轮新增"不对齐。

手动 `/compact` 不走这条路径（它直接 `write_compacted`），因此不受影响。这也是配置注释里"推荐用手动 `/compact`"的现实原因之一。

### 14.3 `context_pct` 只统计非缓存输入

```python
context_pct = usage.input_tokens / _context_window(self._model)
```

Anthropic 的 `input_tokens` 不包含 `cache_read_input_tokens` / `cache_creation_input_tokens`，所以真实上下文占用会被**低估**，尤其在 prompt caching 命中率高的长会话里。阈值判断与进度条都基于这个偏小的值。

### 14.4 重试时界面会残留失败尝试的部分输出

token 事件只在 `attempt == 1` 推送。如果第一次流式在中途中失败：

- 已经推给 TUI 的半截文本不会撤回；
- 第二次成功的完整文本不会重新流式展示；
- 但 `text_parts` 已重置，**最终进入上下文的文本是正确的那一份**。

因此"屏幕看到的"和"模型实际收到的"在重试场景下可能不一致。

### 14.5 压缩调用是否进入 trace 取决于路径

- **自动压缩**：`Compactor` 拿到的是 runner 里可能已被 `TracingProvider` 包裹的 provider → 会在全局 trace 里出现 `run_id="compact"` 的 LLM 往返，但不会进 `events.jsonl`（silent_bus）。
- **手动压缩**：`SessionManager` 用的是 `CoreApp` 启动时创建的**原始** `AnthropicProvider`（未包裹 trace）→ 不会出现在 trace 里。

### 14.6 daemon 启动即创建压缩用 provider

`CoreApp.run()` 中 `compact_provider = AnthropicProvider(...)`（`app.py:231`）发生在 `server.start()` 之前。`AnthropicProvider.__init__` 在缺少 `ANTHROPIC_API_KEY` 时会 `SystemExit`，因此 S6 的 daemon **启动阶段**就要求 key 可用（S4/S5 是第一次 run 时才惰性创建）。

### 14.7 手动与自动压缩对 thread 的影响不同

| | 手动 | 自动 |
|---|---|---|
| `thread.jsonl` | 被摘要对覆盖（原文件备份） | 理论上不变（但受 14.2 影响） |
| `summary_<ts>.md` | 不生成 | 生成 |
| `context.compacted` | 不发 | 发 |
| 失败时 | RPC 返回错误 | 静默跳过、继续 run |

---

## 十五、与 S4 / S5 文档的对照阅读

| 主题 | S4 文档 | S5 文档 | 本文档 |
|---|---|---|---|
| 双进程 / 双通道 | 第五、十四节 | 第六节 | 不变 |
| 会话状态机 | 第四节 | 不变 | 不变（新增 `session.compact` 共享同一把锁） |
| 分层记忆 | 第七节（thread / notes / tasks） | 不变 | **第二节：global / project / session / conversation 四层** |
| 工具调用 | 第十节 | 第八、九节 | 不变；新增 `max_tokens` 半截工具补齐 |
| 权限审批 | 无 | 第二至七节 | 不变 |
| TUI | 第十二节 | 第十一节 | 第九节（`/compact` + ctx 进度条） |
| IPC 契约 | 第十三节 | 第五节 | 第八节（+1 命令 +1 事件 +1 字段） |
| 上下文压缩 | 无 | 无 | 第四、五、六节（新增） |
| 流式重试 | 无 | 无 | 第七节（新增） |

