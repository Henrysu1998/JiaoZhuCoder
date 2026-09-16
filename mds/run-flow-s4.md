# kama 会话与记忆调用链详解（S4：会话系统内化 + 分层语义记忆 + TUI 输入框）

本文档梳理 S4 阶段的**主要实现逻辑**与**完整执行流程**。S4 相对 S3 保持**双进程 IPC 架构**不变（`kama-tui` / `kama` 客户端 → `kama-core` 守护进程，TCP + JSON-RPC NDJSON），但把「一次性 run」升级成「可持续对话的 session」，并补上记忆与交互的最后一块拼图：

1. **会话系统内化** —— 新增 `SessionManager` / `SessionStore` / `Session`，会话落盘到 `~/.kama/sessions/<sess-id>/`；**一条用户消息 = 一次 agent run**，run 目录挂在 session 之下。
2. **分层语义记忆** —— 三层记忆各司其职：`thread.jsonl`（精确层，全量对话回放）、`notes.md`（语义层，`note_save` 主动写入的持久事实）、`runs/<id>/.tasks/`（任务层，沿用 S3）。
3. **TUI 输入框** —— 新增 `ChatTextArea` 多行输入框，Enter 提交、⌘/⇧/⌥+Enter 换行，由 `session.waiting_for_input` 事件驱动的 busy 状态机控制可用性。
4. **IPC 契约扩展** —— 新增 4 个 session 命令与 5 个 session 事件；`agent.run` 改为复用 session 路径执行。

> S3 版本（任务系统 + 八工具体系 + 系统级 trace）见 `run-flow-s3.md`。本文档是它的 S4 演进版。

---

## 零、S3 vs S4 变化（先看这个）

| | S3（任务 + trace） | S4（会话 + 记忆 + 输入框） |
|---|---|---|
| 执行单元 | run（一次性，跑完即结束） | **session**（可多轮），一条消息触发一次 run |
| run 落盘位置 | `runs/<run_id>/` | **`~/.kama/sessions/<sess-id>/runs/<run_id>/`** |
| 会话状态 | 无 | **`Session` 状态机**（`active` / `waiting_for_input` / `closed`） |
| 对话历史 | 每次 run 从 goal 重新开始 | **`thread.jsonl` 全量回放为 messages** |
| 长时记忆 | 无 | **`notes.md` + `note_save` 工具，注入 system prompt** |
| 工具数量 | 8 个 | **9 个**（session 模式下多注册 `note_save`） |
| 任务系统 | `TaskManager` + 4 任务工具 | 不变（run 级，`<run>/ .tasks/`） |
| system prompt | 硬编码在 provider 内 | **由 `ExecutionContext.system_prompt()` 生成，逐次传入** |
| LLM 接口 | `chat(messages, tools, bus, run_id, step)` | **多一个 `system` 关键字参数**（provider 与 trace 均透传） |
| 前端输入 | 无（TUI 只读展示） | **`ChatTextArea` 多行输入框 + 提交/换行键位** |
| 前端并发保护 | 无 | 客户端 `_busy` 标志 + 服务端 `SESSION_BUSY` 双重保护 |
| 断线处理 | 无 | TUI `_socket_loop` **重连循环 + 重连后重建 session** |
| IPC 命令 | 3 个 | **7 个**（+4 个 session 命令） |
| IPC 事件 | 12 个 | **17 个**（+5 个 session 事件） |

架构骨架（双进程、命令/事件双通道、先订阅后触发）在 S4 完全保留，变化集中在**执行单元的粒度**与**上下文的组装方式**上。

---

## 一、入口映射（pyproject.toml → Python 函数）

S4 有三个用户入口，前两个是产品面，第三个是脚本/调试面：

```
kama-tui  →  kama_claude.tui.__main__:main   →  KamaTuiApp(...).run()   ← 主前端（会话式）
kama chat →  kama_claude.cli.main:main       →  cmd_chat(config)         ← 行式会话（脚本用）
kama run  →  kama_claude.cli.main:main       →  cmd_run(goal, config)    ← 一次性 run
kama-core →  kama_claude.core.__main__       →  CoreApp().run()          ← daemon
```

`kama chat` 的分发在 `cli/main.py` 新增一行：

```python
# 第 20 行：注册 chat 子命令（无参数，进入交互输入循环）
subparsers.add_parser("chat", help="Start a multi-turn chat session")

# 第 52-53 行：分发
elif args.command == "chat":
    cmd_chat(config)
```

---

## 二、会话数据模型与落盘布局

### 2.1 目录布局

```
~/.kama/sessions/
  └── sess-3f9a1c04be27/            ← SessionManager.create() 生成（uuid4 前 12 位）
       ├── meta.json                ← Session 元数据（状态、标题、run_ids）
       ├── thread.jsonl             ← 对话历史（精确层记忆，一行一条 Anthropic 消息）
       ├── notes.md                 ← 会话笔记（语义层记忆，note_save 追加）
       └── runs/
            └── 20260915-101500-a1b2c3/
                 ├── events.jsonl   ← 该次 run 的事件日志（沿用 S3 EventWriter）
                 └── .tasks/        ← 该次 run 的任务库（沿用 S3 TaskManager）
                      └── task_1.json
```

与 S3 的差异只有一处但影响全局：**run 不再是顶层目录，而是挂在 session 之下**（`SessionStore.runs_dir()` → `<session>/runs/`），事件回放也随之新增了 session 目录的 glob 回退路径（见第五节）。

### 2.2 `Session`（`core/session/model.py:11`）

```python
SessionStatus = Literal["active", "waiting_for_input", "closed"]
SessionMode = Literal["one_shot", "chat"]

@dataclass
class Session:
    id: str                 # "sess-<12位hex>"
    mode: SessionMode       # chat：跑完继续等输入；one_shot：跑完自动 closed
    status: SessionStatus
    title: str              # 空标题时用首条用户消息前 40 字
    created_at: str
    updated_at: str
    run_ids: list[str] = field(default_factory=list)
```

配套的 `to_dict()` / `from_dict()`（`model.py:21` / `model.py:34`）是 `meta.json` 的序列化契约：**只存元数据，不存消息**——消息单独放 `thread.jsonl`，避免每轮对话重写整个文件。

---

## 三、`SessionStore`：三层记忆的读写实现

`SessionStore`（`core/session/store.py:21`）是纯文件层的薄封装，不持任何运行时状态，所有方法接收 `sid` 现算路径，因此 daemon 重启后数据天然还在（内存里的 `SessionManager._sessions` 索引才是易失的那一层）。

### 3.1 精确层：`thread.jsonl`

```python
# 第 50-64 行：追加一条消息，带 ts 与 run_id 记账
def append_message(self, sid, role, content, run_id=None) -> None:
    row = {"ts": _now(), "role": role, "content": content}
    if run_id is not None:
        row["run_id"] = run_id
    with (path / "thread.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

# 第 81-107 行：读回时剥离 ts/run_id，只留 Anthropic API 需要的 role/content
def read_messages(self, sid) -> list[dict[str, Any]]:
    ...
    messages.append({"role": role, "content": row.get("content", "")})
    return self._trim_orphan_tool_use(messages)
```

写盘时多两个字段（`ts`、`run_id`）是为了**可审计**；读盘时剥掉，是为了让同一份文件既能被 `kama` 直接读，又能原样喂给 Anthropic API。

### 3.2 `_trim_orphan_tool_use`（`store.py:109`）—— 防止 `messages.invalid`

```python
# 扫描整条 thread：assistant 的 tool_use 入待配对集合，user 的 tool_result 出集合；
# 记录「最后一次集合为空」的下标，若最终仍有余项则裁掉其后的消息
last_balanced = 0
for idx, msg in enumerate(messages, start=1):
    ...
    if not pending:
        last_balanced = idx
if pending:
    logger.warning("trim orphan tool_use blocks from thread")
    return messages[:last_balanced]
```

这是 S4 最容易踩坑的地方：进程被杀、run 取消都会让 `thread.jsonl` 尾部留下**只有 `tool_use` 没有 `tool_result`** 的半截记录，下一次请求会被 Anthropic API 直接拒绝（`messages.invalid`）。读取时统一裁尾，比在写入端做补偿更稳。

### 3.3 语义层：`notes.md`

```python
# 第 131-135 行：读全文，文件不存在返回空串
def read_notes(self, sid) -> str: ...

# 第 138-143 行：追加一条带时间与 run_id 的笔记
def append_note(self, sid, content, run_id) -> None:
    f.write(f"## Note ({_now()}, {run_id})\n{content}\n\n")
```

固定使用 Markdown 二级标题分块，好处是**人可读、模型可读、diff 友好**——追加写不产生全文件重写，多个 run 的笔记天然按时间累积。

---

## 四、`SessionManager`：会话状态机与并发语义

`SessionManager`（`core/session/manager.py:35`）持有两个进程内字典：

| 字段 | 类型 | 作用 |
|------|------|------|
| `_sessions` | `dict[str, Session]` | 内存索引，`_get_session()` 未命中即抛 `SESSION_NOT_FOUND` |
| `_locks` | `dict[str, asyncio.Lock]` | 每 session 一把锁，保证「一个会话同时只跑一个 run」 |

### 4.1 状态机

```
                 create(mode="chat")                    send_message 完成
   (不存在) ───────────────────────────▶ active ─────────────────────────▶ waiting_for_input
                                          ▲                                     │
                                          └────────── send_message ◀────────────┘
                                                                                │
   create(mode="one_shot") ──▶ active ── send_message 完成 ──▶ closed           │
                                                               ▲                │
                                              close() ─────────┴────────────────┘
```

### 4.2 `send_message()`（`manager.py:69`）—— S4 的核心方法

```python
async def send_message(self, sid, content, *, run_id=None) -> str:
    session = self._get_session(sid)
    lock = self._locks[sid]
    if lock.locked():                                   # ① 快速失败，不排队
        raise HandlerError(SESSION_BUSY, "session busy")

    async with lock:
        if session.status == "closed":                  # ② 终态拒绝
            raise HandlerError(SESSION_CLOSED, "session already closed")

        if session.status == "waiting_for_input":       # ③ 从等待态恢复
            await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

        self._store.append_message(sid, "user", content)          # ④ 用户消息先落盘
        await self._bus.publish(
            SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
        )

        if not session.title:
            session.title = content[:40]                # ⑤ 首条消息补标题

        run_id = run_id or new_run_id()
        session.run_ids.append(run_id)
        session.updated_at = _now()
        self._store.write_meta(session)

        runner = self._runner_factory()                 # ⑥ 每次 run 新建 runner
        await runner.run_and_capture(content, run_id=run_id, session=session, store=self._store)

        session.updated_at = _now()                     # ⑦ 按 mode 决定终态
        if session.mode == "one_shot":
            session.status = "closed"
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
        else:
            session.status = "waiting_for_input"
            await self._bus.publish(SessionWaitingForInputEvent(..., last_run_id=run_id, ...))
        self._store.write_meta(session)
        return run_id
```

**四个关键决策**：

1. **用户消息在调 LLM 之前落盘**（④）——即使 daemon 中途崩溃，用户输入也不会丢。
2. **`run_id` 在 session 内生成并记账**（`session.run_ids`），使「会话 → 多个 run」的关系持久化。
3. **`one_shot` 与 `chat` 只差在终态**（⑦）——这让 `kama run` 无需另写一套执行逻辑，复用了完整会话路径。
4. **锁是"快速失败"而非"排队"**：`lock.locked()` 直接抛错，避免用户连按 Enter 时排队堆叠出一串 run。

### 4.3 错误码（`manager.py:25-27`）

| 常量 | 值 | 触发条件 |
|------|----|----------|
| `SESSION_NOT_FOUND` | `-32010` | `_get_session()` 内存索引未命中 |
| `SESSION_CLOSED` | `-32011` | 向已关闭 session 发消息 |
| `SESSION_BUSY` | `-32012` | 该 session 已有 run 在跑 |

三者都是 S4 自定义的业务码，通过 `HandlerError` 抛出后由 `SocketServer` 转成结构化 JSON-RPC 错误响应——与 `-32601 METHOD_NOT_FOUND` 这类传输层/方法级错误区分开，客户端可以据此区分"会话状态不对"和"daemon 不认识这个方法"。

---

## 五、daemon 侧：handler 注册与三条 session 通路

### 5.1 注册（`core/app.py:179` 起）

```python
sessions_root = Path("~/.kama/sessions").expanduser()
store = SessionStore(sessions_root)
self._sessions = SessionManager(
    store,
    runner_factory=lambda: AgentRunner(self._config, bus=self._bus, trace=self._trace),
    bus=self._bus,
)

server.register("core.ping",            self._ping_handler)
server.register("agent.run",            self._agent_run_handler)      # 兼容入口（一次性会话）
server.register("event.subscribe",      self._subscribe_handler)
server.register("session.create",       self._session_create_handler)
server.register("session.send_message", self._session_send_handler)
server.register("session.get_history",  self._session_history_handler)
server.register("session.close",        self._session_close_handler)
```

注意 `runner_factory` 是**每次调用新建** `AgentRunner`：runner 本身无跨 run 状态，真正沉淀状态的是 `SessionStore`（磁盘）+ `SessionManager`（内存）。

### 5.2 `agent.run` → `_agent_run_handler()`（`app.py:87`）

```python
cmd = AgentRunCommand.model_validate(params)
session = await self._sessions.create(mode="one_shot", title=cmd.goal[:40])   # ★ S4：包成一个会话
run_id = new_run_id()
run_task = asyncio.create_task(
    self._sessions.send_message(session.id, cmd.goal, run_id=run_id)
)
self._running_runs.add(run_task)
run_task.add_done_callback(self._running_runs.discard)
return AgentRunResult(run_id=run_id)                                          # 立即返回
```

**行为变化**：`kama run` 仍然是「发命令 → 立即拿到 run_id → 等 `run.finished` → 退出」，但对 daemon 而言它现在**就是一个标题等于 goal 的一次性会话**。好处是 `kama run` 与 TUI 会话共用同一条执行链、同一套落盘布局，不再有第二条代码路径。

### 5.3 `session.send_message` → `_session_send_handler()`（`app.py:107`）

```python
cmd = SessionSendMessageCommand.model_validate(params)
run_id = await self._sessions.send_message(cmd.session_id, cmd.content)   # ★ 同步等待 run 结束
return SessionSendMessageResult(run_id=run_id)
```

这里**故意让 handler 阻塞到 run 结束**才回响应：客户端因此不需要轮询，请求的返回本身就是"这一轮跑完了"。而过程中的进度信息（token、工具调用、步骤）并没有被这个阻塞挡住——它们走的是**另一条通道**（`IpcEventBroadcaster` 推送），两条通路互不干扰。

### 5.4 事件回放兼容 session 目录（`app.py:143`）

```python
path = events_file(run_id)                      # 先试 S1-S3 的 runs/<run_id>/events.jsonl
if not path.exists():
    for candidate in Path("~/.kama/sessions").expanduser().glob(f"*/runs/{run_id}/events.jsonl"):
        path = candidate                        # ★ S4：回退到会话目录
        break
```

`kama-tui --replay <run_id>` 因此对 S3 旧数据与 S4 新数据都能工作。

---

## 六、`AgentRunner`：会话化的 run 执行

### 6.1 记忆装配（`core/runner.py:93-125`）

```python
run_id = run_id or new_run_id()
if session is not None and store is not None:              # ★ session 模式
    run_path = store.runs_dir(session.id) / run_id         #   run 落在 session 目录下
    history = store.read_messages(session.id)              #   精确层：全量线程（已裁尾）
    notes = store.read_notes(session.id)                   #   语义层：笔记全文
else:                                                       #   无 session 模式（单测/遗留路径）
    run_path = self._runs_dir / run_id
    history = [{"role": "user", "content": goal}]
    notes = ""
run_path.mkdir(parents=True, exist_ok=True)

context = ExecutionContext(
    run_id=run_id,
    goal=goal,
    max_steps=self._config.agent.max_steps,
    prefill_messages=history,     # ★ 整个 thread 成为本轮 messages
    session_notes=notes,          # ★ 笔记进 system prompt
)
prefill_len = len(history)        # ★ 记住边界，回写时只追加本轮新增
```

### 6.2 本轮新增消息回写（`runner.py:169-170`）

```python
if session is not None and store is not None:
    store.append_messages(session.id, context.messages[prefill_len:], run_id=run_id)
```

这一行是「多轮对话可续」的闭环：`prefill_len` 之前的内容本来就在文件里，**只把本轮新增的 assistant / tool_result 消息追加进 `thread.jsonl`**，因此既不重复写入，也不需要全量重写。

### 6.3 工具注册表（`runner.py:67-88`）

```python
registry.register(ReadFileTool());  registry.register(BashTool())
registry.register(WriteFileTool()); registry.register(ListDirTool())
registry.register(TaskCreateTool(task_manager)); registry.register(TaskUpdateTool(task_manager))
registry.register(TaskListTool(task_manager));   registry.register(TaskGetTool(task_manager))
if session is not None and store is not None and run_id is not None:
    registry.register(NoteSaveTool(store, session.id, run_id))     # ★ 第 9 个工具
```

| 工具 | 类别 | S4 变化 |
|------|------|---------|
| `read_file` / `write_file` / `list_dir` / `bash` | 文件、命令 | 不变 |
| `task_create` / `task_update` / `task_list` / `task_get` | 任务 | 不变（run 级 `.tasks/`） |
| **`note_save`** | 记忆 | **新增，仅会话模式下注册** |

`note_save` 通过构造参数绑定 `(store, session_id, run_id)`，而不是让模型自己传 session_id —— **模型只负责决定"记什么"，"记到哪"由运行时注入**，避免模型伪造会话标识。

### 6.4 其余执行骨架

`EventWriter` 订阅、`RunStartedEvent` / `RunFinishedEvent` 发布、`TracingProvider` 包裹、`CancelledError` 转 `mark_failed("cancelled")` 等环节与 S3 完全一致（见 `run-flow-s3.md` 第七节），本阶段未做改动。

---

## 七、分层语义记忆：三层结构与其注入点

这是 S4 的设计核心——**不同精度的记忆用不同载体、注入到不同位置**：

| 层 | 载体 | 写入者 | 注入点 | 特点 |
|---|------|--------|--------|------|
| **精确层**（对话） | `<session>/thread.jsonl` | `SessionManager`（user 消息）+ `AgentRunner`（新增 assistant/tool 消息） | `ExecutionContext.prefill_messages` → `messages` | 全量、逐字、可回放；token 成本随轮数线性增长 |
| **语义层**（事实） | `<session>/notes.md` | `note_save` 工具 | `ExecutionContext.session_notes` → system prompt | 短、去噪、由模型判断"什么值得长期记住"；token 成本恒定 |
| **任务层**（进度） | `<run>/.tasks/task_*.json` | 四个 task 工具 | 工具返回值（不进 prompt） | run 级、结构化、可查询 |

```
                        session 生命周期
   ┌───────────────────────────────────────────────────────────┐
   │  thread.jsonl  ←── 每轮全量回放（精确层，越用越长）          │
   │       │                                                    │
   │       ├── 作为 messages 直接送给 LLM  ─────────────┐        │
   │       │                                            │        │
   │  notes.md      ←── note_save 主动摘录（语义层）     │        │
   │       │                                            ▼        │
   │       └── 拼进 system prompt ────────────▶  LLMProvider.chat │
   │                                                     │        │
   │  .tasks/*.json ←── 任务工具（任务层，只影响工具结果）  │        │
   └───────────────────────────────────────────────────────────┘
```

**为什么不让 `notes.md` 取代 `thread.jsonl`？** 两者解决的矛盾不同：裁剪 thread 省 token 但会丢细节（用户上文的具体要求、变量名、路径）；全量保留 thread 保精度但成本无上限。分层后，模型可以**只在"值得长期记住"时调用 `note_save`**，把精度交给 thread、把长期记忆交给 notes，二者互不替代。

---

## 八、`ExecutionContext`：上下文组装的变化

### 8.1 初始化（`core/context.py:21`）

```python
def __post_init__(self) -> None:
    if self.prefill_messages:                       # ★ 会话路径：整条 thread 就是初始 messages
        self.messages = [dict(m) for m in self.prefill_messages]
    elif not self.messages:                         #   兜底：无回放时用 goal 起头
        self.messages.append({"role": "user", "content": self.goal})
```

`prefill_messages` 的定位是「**可选的上下文起点**」，这让同一个 `ExecutionContext` 同时服务"有历史的会话 run"和"从零开始的一次性 run"。

### 8.2 system prompt 生成（`context.py:28`）

```python
def system_prompt(self, base: str) -> str:
    if not self.session_notes.strip():
        return base
    return (
        base
        + "\n\n## Session Notes\n" + self.session_notes.strip()
        + "\n\nRemember important durable facts by calling note_save."
    )
```

只在**有笔记时**才拼接（避免给无记忆的会话增加无用 token），并且**顺手把 `note_save` 的使用提示放在笔记旁边**——模型读完自己的历史笔记后，紧接着就看到"以后可以继续用 note_save 记录"，形成记忆的自我强化。

### 8.3 工具结果合并（`context.py:43`，S3 已具备）

同一步多个工具的结果会被合并进**同一条 user 消息**的 content 列表里，符合 Anthropic 对 `tool_result` 块的要求——这是多工具并行调用不触发 `messages.invalid` 的前提。

---

## 九、`AgentLoop`：唯一改动是传入 system prompt

plan → act → observe 三段式与 S3 完全一致，`loop.py:46` 处新增 `system=` 实参：

```python
response = await self._provider.chat(
    messages=context.messages,
    tool_schemas=self._registry.tool_schemas(),
    bus=self._bus,
    run_id=context.run_id,
    step=context.step,
    system=context.system_prompt(          # ★ 每步都带上（含 session notes 的）system prompt
        "You are a helpful AI assistant. "
        "Use the available tools to complete the user's goal. "
        "When the goal is fully achieved, respond with a final answer "
        "and do not call any more tools."
    ),
)
```

**注意**：system prompt 每步都重新生成，而不是在 run 开始时算一次。这样同一轮对话内模型调用 `note_save` 写入的新笔记，**在下一步就立刻生效**——记忆当轮即生效，无需等下一轮对话。

---

## 十、system prompt 的下行链路（loop → provider → trace）

```python
# core/llm/base.py：Protocol 增加一个关键字参数
async def chat(self, messages, tool_schemas, bus, run_id, *, step: int = 0,
               system: str | None = None) -> LlmResponse: ...
```

```python
# core/llm/provider.py:38-70：system 参数优先于内置默认值，且仍然带 prompt caching
system_blocks = [
    {"type": "text", "text": system or _SYSTEM_PROMPT,
     "cache_control": {"type": "ephemeral"}},
]
kwargs = {"model": self._model, "max_tokens": 4096, "system": system_blocks, "messages": messages}
```

```python
# core/trace/provider.py：装饰器透传 system，并按 include_payload 决定是否记录正文
if self._include_payload:
    call_data = {"messages": messages, "tool_schemas": tool_schemas, "system": system}
...
result = await self._inner.chat(messages, tool_schemas, bus, run_id, step=step, system=system)
```

三点值得注意：

1. **`system=None` 时行为与 S3 完全一致**（回落到 `_SYSTEM_PROMPT`），所以未接入 session 的调用方无需改动。
2. **`cache_control: ephemeral` 仍然保留**：system prompt 在多轮里基本稳定，是 prompt caching 收益最大的部分。
3. **trace 记录 system**：`include_payload=False` 时只记 `message_count`，不会把笔记正文写进 trace 文件。

---

## 十一、`note_save` 工具（`core/tools/builtin/note_save.py`）

```python
class NoteSaveTool(BaseTool):
    name = "note_save"
    description = ("Save a concise fact or decision to this session's notes. "
                   "These notes are visible in future turns of the same session.")
    input_schema = {"type": "object",
                    "properties": {"content": {"type": "string", ...}},
                    "required": ["content"]}

    async def invoke(self, params) -> ToolResult:      # 第 31 行
        content = str(params.get("content", "")).strip()
        if not content:
            return ToolResult(content="empty content", is_error=True, error_type="runtime_error")
        self._store.append_note(self._session_id, content, self._run_id)
        return ToolResult(content="saved")
```

- **描述即用法说明**：`description` 明确写了"这些笔记在同会话后续轮次可见"，让模型自己判断调用时机。
- **空内容视为错误**：走 `invoke_tool` 统一错误收敛路径（回填 `is_error=True`，循环继续），不会打断 run。
- **TUI 低噪声展示**：`ToolCallBlock._summary()` 对成功的 `note_save` 特判为 `remembered`，而不是打印一整段 note 内容（`tui/app.py:98`）。

---

## 十二、TUI：输入框与会话状态机

### 12.1 `ChatTextArea`（`tui/app.py:138`）—— 键位契约

```python
async def _on_key(self, event: events.Key) -> None:          # 第 166 行
    key = event.key
    if key == "enter":                                        # 提交
        event.stop(); event.prevent_default()
        if self.text.strip():
            self.post_message(self.Submitted(self))
        return
    if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):   # 换行
        event.stop(); event.prevent_default()
        if not self.read_only:
            self.insert("\n")
        return
    await super()._on_key(event)                              # 其余交回 TextArea
```

| 按键 | 行为 |
|------|------|
| `Enter` | 提交（内容为空则忽略，不发送） |
| `Alt/Shift/Super+Enter`、`Ctrl+J` | 插入换行 |
| 其他 | TextArea 默认编辑行为 |

`Submitted(Message)`（`app.py:159`）只携带 `text_area` 与 `value`，由宿主的 `on_chat_text_area_submitted` 处理——控件不碰网络，网络不碰控件，职责分离。

### 12.2 提交处理（`tui/app.py:245`）

```python
async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
    content = event.value.strip()
    if not content: return
    if self._client is None or self._session_id is None or self._busy:      # ① 双重保护
        self._append(Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line"))
        return
    self._busy = True                                                        # ② 置忙
    prompt.text = ""; prompt.disabled = True                                 # ③ 清空并禁用
    prompt.border_title = "agent is working..."
    self._append(Static(f"[bold]>[/bold] {content}", classes="user-turn"))   # ④ 立即回显用户输入
    self._update_header("running")
    try:
        await self._client.send_command(                                  # ⑤ 同步等待本轮 run
            "session.send_message", {"session_id": self._session_id, "content": content})
    except IpcError as e:
        ...  # 失败时恢复输入框并打印错误，不让 UI 卡死
```

**关键点**：⑤ 的 `await` 会一直挂到本轮 run 结束，但这**不会冻结 UI**——它在 Textual 的事件协程里，同时 `run_event_loop()` 在另一条 task 上持续消费推送事件并刷新界面。用户看到的是流式输出，而不是"卡住一秒再一次性吐出"。

### 12.3 busy 状态机（由事件驱动复位）

| 事件 | UI 反应 |
|------|---------|
| 提交成功 | `_busy=True`、输入框 `disabled`、标题 `agent is working...`、header 变 `running` |
| `session.waiting_for_input` | `_busy=False`、输入框启用并 `focus()`、header 变 `ready`（`app.py:388`） |
| `session.closed` | `_busy=False`、输入框保持禁用、标题 `session closed`、header `disconnected`（`app.py:397`） |
| 发送失败（`IpcError`） | `_busy=False`、输入框启用、header 回 `ready` |

**复位由服务端事件驱动，而不是由 `send_command` 返回驱动**：即使响应包因网络抖动丢失，只要 `session.waiting_for_input` 到达，输入框就会解锁。

### 12.4 连接与重连（`tui/app.py:309`）

```python
async def _socket_loop(self) -> None:
    while True:                                          # ★ 永不退出的重连循环
        client = SocketClient(self._host, self._port)
        try:
            await client.connect()
        except (ConnectionRefusedError, OSError):
            self._update_header("disconnected")
            await asyncio.sleep(2)                        # daemon 没起 → 2 秒后重试
            continue
        ...
        await client.send_command("event.subscribe", params)     # ① 先订阅（含可选 replay_from_run）
        created = await client.send_command("session.create", {"mode": "chat"})   # ② 再建会话
        self._session_id = str(created["session_id"])
        prompt.disabled = False; prompt.focus()                  # ③ 解锁输入框
        self._update_header("ready")
        await loop_task                                          # ④ 挂起，直到连接断开
        ...
        self._update_header("disconnected"); await asyncio.sleep(2)
```

订阅的 topic 覆盖会话、run、step、tool、LLM 与日志：

```python
{"topics": ["session.*", "run.*", "step.*", "tool.*", "llm.token", "llm.usage", "log.*"],
 "scope": "global"}          # replay_run_id 存在时追加 "replay_from_run"
```

「**先订阅后建会话**」是 S2 就确立的顺序：反过来的话，创建会话与首轮 run 的事件会在订阅生效前发出，前端就看不到开头。

### 12.5 渲染细节（S4 顺手补齐的两处 polish）

```python
# 第 64-70 行：流式块结束（下一个 token 开启新块之前）把累积文本渲染成 Rich Markdown
def finalize_markdown(self) -> None:
    if self._finalized: return
    self._finalized = True
    if self._text.strip():
        self.update(Markdown(self._text, code_theme="monokai"))
```

```python
# 第 30-43 行：工具块摘要优先展示各工具最关键的字段，而不是整段 JSON
keys_by_tool = {"read_file": ("path",), "write_file": ("path",),
                "list_dir": ("path", "max_depth"), "bash": ("command",),
                "note_save": ("content",)}
```

配套的 `ToolCallBlock.on_click()`（`app.py:123`）支持点击展开完整 params / output / elapsed——**默认安静、需要时展开**，避免工具噪音淹没对话。

---

## 十三、IPC 契约增量（S4 新增）

### 命令（`core/bus/commands.py`）

| 方法 | 参数 | 结果 |
|------|------|------|
| `session.create` | `mode`（`chat` / `one_shot`）、`title` | `session_id`、`status` |
| `session.send_message` | `session_id`、`content` | `run_id`（本轮跑完才返回） |
| `session.get_history` | `session_id` | `messages`（已裁尾的 Anthropic messages） |
| `session.close` | `session_id` | `status="closed"` |

### 事件（`core/bus/events.py`）

| 事件 | 触发点 | 载荷要点 |
|------|--------|----------|
| `session.created` | `SessionManager.create()` | `session_id`、`mode` |
| `session.message_received` | `send_message()` 用户消息落盘后 | `session_id`、`content` |
| `session.resumed` | 从 `waiting_for_input` 恢复时 | `session_id` |
| `session.waiting_for_input` | 本轮 run 结束且 mode 为 chat | `session_id`、`last_run_id` |
| `session.closed` | `close()` 或 one_shot 收尾 | `session_id` |

`WIRE_PROTOCOL.md` 由 `scripts/gen_protocol_doc.py` 从这些 pydantic 模型生成，S4 同步重新生成（新增 Commands 段 4 组、Session Events 段 5 组）。

---

## 十四、完整执行流程（TUI 一条消息的时序）

```
终端 A:  kama-core                                 ← 启动 daemon（127.0.0.1:7437）
           └─ CoreApp.run(): TraceWriter + EventBus(全局订阅者: trace + broadcaster)
                            + SessionStore/SessionManager（每个 run 另加 EventWriter）
                            + SocketServer(注册 7 个 handler)

终端 B:  kama-tui
  │
  ├─ KamaTuiApp.compose(): #header + #log-view + ChatTextArea
  ├─ on_mount(): 启动 _socket_loop worker，输入框先置 disabled
  │
  └─ _socket_loop()（重连循环）
       ├─ ① SocketClient.connect()  ── TCP ──▶ daemon
       ├─ ② 启动 run_event_loop()（后台读推送）
       ├─ ③ send_command("event.subscribe", {topics:[session.*, run.*, ...]})
       ├─ ④ send_command("session.create", {mode:"chat"})  ─▶ SessionManager.create()
       │       └─ 写入 meta.json，发布 session.created
       │       └─ 回包 {session_id}  ─▶ 输入框启用 + focus + header=ready
       └─ ⑤ await loop_task（挂起等事件；断开则回到 ① 重连）

用户输入 "帮我看看 pyproject.toml" 并按 Enter
  └─ ChatTextArea._on_key → post_message(Submitted)
       └─ KamaTuiApp.on_chat_text_area_submitted()
            ├─ _busy=True，清空并禁用输入框，标题 "agent is working..."
            ├─ _append(Static("> 帮我看看 pyproject.toml"))           ← 立即回显
            └─ await send_command("session.send_message", {...})      ← 挂起到本轮结束

       ┌───────────────────────── daemon 进程 (kama-core) ─────────────────────────┐
       │ SocketServer._handle_line()                                                │
       │   └─ session.send_message → _session_send_handler()                        │
       │        └─ SessionManager.send_message(sid, content)                        │
       │             ├─ lock.locked()? → SESSION_BUSY（快速失败）                    │
       │             ├─ status=="closed"? → SESSION_CLOSED                          │
       │             ├─ 若 waiting_for_input → publish session.resumed              │
       │             ├─ thread.jsonl 追加 user 消息                                  │
       │             ├─ publish session.message_received                           │
       │             ├─ 首条消息补 title，run_ids 追加，写 meta.json                  │
       │             └─ AgentRunner.run_and_capture(content, session, store) ──┐    │
       │                                                                      │    │
       │   AgentRunner.run_and_capture()                                      │    │
       │     ├─ run_path = <session>/runs/<run_id>/  ← run 挂在会话下            │    │
       │     ├─ history = store.read_messages(sid)        ← 精确层（已裁孤块）   │    │
       │     ├─ notes   = store.read_notes(sid)           ← 语义层              │    │
       │     ├─ ExecutionContext(prefill_messages=history, session_notes=notes) │    │
       │     ├─ prefill_len = len(history)      ← 回写边界                      │    │
       │     ├─ registry = 8 工具 + NoteSaveTool ← 第 9 个工具                   │    │
       │     ├─ provider = TracingProvider(AnthropicProvider)                  │    │
       │     └─ AgentLoop.run(context)                                         │    │
       │          └─ 每步:                                                      │    │
       │               plan: provider.chat(messages, tools, system=system_prompt(base))│
       │                     └─ system_prompt 注入 session notes + note_save 提示 │   │
       │               observe: add_assistant_message(blocks)                   │   │
       │               act: invoke_tool(...)  ← 可能是 note_save → notes.md       │   │
       │                     └─ 下一步的 system prompt 立刻带上新笔记（当轮生效）  │   │
       │          └─ bus.publish(step.* / tool.* / llm.token / llm.usage)        │   │
       │               ├─ _trace_event_handler   → TraceWriter（layer=event）    │   │
       │               ├─ EventWriter            → <run>/events.jsonl          │   │
       │               └─ IpcEventBroadcaster    → 匹配 topic → 写回客户端 socket │   │
       │          └─ 结束: RunFinishedEvent                                     │   │
       │                └─ store.append_messages(sid, messages[prefill_len:]) ← 只追加本轮新增
       │                                                                      │    │
       │   SessionManager 收尾                                                 │    │
       │     ├─ mode=="chat"       → status=waiting_for_input + publish 同事件  │    │
       │     └─ mode=="one_shot"   → status=closed      + publish session.closed│    │
       │     └─ 写 meta.json，返回 run_id                                       │    │
       └────────────────────────────────────────────────────────────────────────────┘
                                     │
       TUI 侧并行消费事件（run_event_loop → _handle_event）:
         run.started        → [dim]run[/dim] <run_id> <goal>
         step.started       → [dim]step N[/dim]
         llm.token          → 累积进 LLMStreamBlock（流式打字机）
         tool.call_started  → 追加 ToolCallBlock（可点击展开）
         tool.call_finished → 更新摘要 done/✓ + elapsed_ms
         llm.usage          → tokens in/out/cache 一行
         run.finished       → ✓ completed / ✗ failed
         session.waiting_for_input → _busy=False，输入框重新启用并 focus，header=ready

输入框再次可用 → 用户输入下一句 → 重复上述流程（messages 已带上完整历史与新笔记）
```

---

## 十五、`kama run` 的一次性会话路径（对照）

`kama run` 客户端代码未变（仍走「订阅 → `agent.run` → 等 `run.finished`」），但 daemon 侧的执行链已收敛到同一套 session 逻辑：

```
kama run --goal "整理代码"
  └─ SocketClient: event.subscribe(run.*, step.*, tool.*, llm.token, llm.usage)
  └─ send_command("agent.run", {"goal": "整理代码"})           ← 立即返回 run_id
       └─ CoreApp._agent_run_handler()
            ├─ session = SessionManager.create(mode="one_shot", title=goal[:40])
            ├─ run_task = create_task(SessionManager.send_message(session.id, goal, run_id))
            └─ 立即回 AgentRunResult(run_id)                   ← 客户端不需要等
       └─ 后台 send_message 内部:
            thread.jsonl 追加 goal → 执行 run（9 工具全可用）
            → 收尾 status=closed → publish session.closed → meta.json 落盘
       └─ 客户端等到 run.finished，退出（exit_code 由 status 决定）
```

差别只在**收尾状态**：`chat` 停在 `waiting_for_input`，`one_shot` 直接 `closed`。因而 `kama run` 天然获得了两项新能力：**落盘可追溯**（`~/.kama/sessions/<sid>/runs/<run_id>/events.jsonl`）与 **`--replay` 可回放**。

---

## 十六、文件改动清单（S4）

| 类别 | 文件 | 内容 |
|------|------|------|
| 新增（会话） | `core/session/model.py` | `Session` 数据类与 JSON 契约 |
| | `core/session/store.py` | 三层记忆的文件读写、`_trim_orphan_tool_use` |
| | `core/session/manager.py` | 状态机、并发锁、错误码、事件发布 |
| | `core/session/__init__.py` | 导出 `Session` / `SessionStore` / `SessionManager` |
| 新增（工具） | `core/tools/builtin/note_save.py` | 第 9 个工具 `note_save` |
| 修改（执行） | `core/runner.py` | session 分支、prefill、回写新增消息、注册 `note_save` |
| | `core/context.py` | `prefill_messages`、`session_notes`、`system_prompt()` |
| | `core/loop.py` | 传 `system=` |
| | `core/llm/base.py`、`core/llm/provider.py` | `chat()` 新增 `system` 参数 |
| | `core/trace/provider.py` | 透传并记录 `system` |
| 修改（协议） | `core/bus/commands.py`、`core/bus/events.py` | 4 命令 + 5 事件 |
| | `core/app.py` | 4 个 session handler、`SessionManager` 装配、回放 glob 回退 |
| 修改（前端） | `tui/app.py` | `ChatTextArea` 输入框、busy 状态机、重连循环、Markdown 渲染 |
| | `cli/commands/chat.py`、`cli/main.py` | `kama chat` 行式会话入口 |
| 文档 | `WIRE_PROTOCOL.md` | 由 `scripts/gen_protocol_doc.py` 重新生成 |

测试增量：`tests/unit/test_session_store.py`（meta/thread/notes 往返、孤块裁尾）、`tests/unit/test_session_manager.py`（状态机与错误码）、`tests/unit/test_note_save_tool.py`、`tests/unit/test_runner.py`（history+notes 注入、`note_save` 落盘）、`tests/unit/test_tui_app.py`（参数摘要、Markdown finalize、提交态迁移）、`tests/integration/test_s4_session_ipc.py`（create → get_history → close 端到端）。

---

## 十七、关键设计要点

1. **执行单元从 run 升为 session（S4 主线）**：`SessionManager` 负责状态与并发，`AgentRunner` 只负责"跑一次"，两者职责清晰。
2. **一条消息 = 一次 run**：让「多轮对话」与「一次性任务」共享同一条执行链，`mode` 只影响终态，避免两套代码路径。
3. **分层记忆（S4 核心设计）**：精确层 `thread.jsonl` 保真、语义层 `notes.md` 保久、任务层 `.tasks/` 保进度，三者载体不同、注入点不同、成本曲线不同。
4. **`note_save` 只记"值得长期记住的"**：把"什么值得记"的判断题交给模型，把"记到哪里"的定位题交给运行时注入的 session 绑定。
5. **system prompt 每步重算**：同轮内新写入的笔记立即生效，记忆无需等到下一轮。
6. **`system=None` 向后兼容**：未接入 session 的调用方行为与 S3 完全一致，改动范围被限制在签名增加一个可选参数。
7. **读时裁尾优于写时补偿**：`_trim_orphan_tool_use` 把"半截 tool_use"的容错集中在一处，任何写入路径的意外中断都能被下一次读取兜住。
8. **只追加本轮新增消息**：`prefill_len` 边界让 `thread.jsonl` 保持纯追加写，O(新增) 而不是 O(全量)。
9. **阻塞式 `send_message` + 推送式事件**：请求-响应告诉客户端"这轮结束了"，推送通道告诉客户端"这轮进行到哪了"，两条通路互不阻塞。
10. **客户端与服务端双重并发保护**：TUI 的 `_busy` 保证体验，服务端的 `SESSION_BUSY` 保证正确性——任何客户端都不可能在同会话并发触发两个 run。
11. **UI 复位由服务端事件驱动**：`session.waiting_for_input` / `session.closed` 才是输入框解锁/锁定的唯一依据，避免响应丢失导致 UI 卡在 busy。
12. **断线自动重连 + 重连后重建会话**：`_socket_loop` 永不退出，daemon 重启后 TUI 仍能恢复可用（新会话，历史消息保留在磁盘上）。
13. **输入框键位即契约**：Enter 提交、⌘/⇧/⌥+Enter 换行——多行输入与"回车发送"的冲突用组合键解决，并把提示写在边框标题上。
14. **记忆的三层都可人读**：`thread.jsonl` / `notes.md` / `task_*.json` 全是纯文本，`tail` / `cat` / `git diff` 即可审阅，不需要专用工具。

---

## 十八、与 S3 文档的对照阅读

| 主题 | S3 文档 | 本文档 |
|------|---------|--------|
| 双进程架构、双通道 | `run-flow-s3.md` 三、四节 | 沿用，未重复展开 |
| 客户端事件订阅顺序 | 三、5 节「先订阅后触发」 | 第十二节 12.4（TUI 版本） |
| daemon handler 注册 | 第六节 | 第五节（+4 个 session handler） |
| `AgentRunner` 编排 | 第七节 | 第六节（+session 记忆装配） |
| 任务系统 / `.tasks/` | 第八节 | 第七节表格第三行（不变） |
| `AgentLoop` | 第九节 | 第九节（唯一改动：传 `system`） |
| 工具调用与错误收敛 | 第十节 | 未变（`note_save` 走同一路径） |
| LLM 调用与 trace | 第十一、十三节 | 第十节（+`system` 透传） |
| 会话与记忆 | 无 | 第二、三、四、七、八节（新增） |
| TUI 输入框 | 无 | 第十二节（新增） |
