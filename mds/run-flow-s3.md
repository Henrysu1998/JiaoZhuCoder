# kama run 调用链详解（S3：任务系统内化 + 八工具体系 + 系统级 trace）

本文档梳理 S3 阶段 `kama run --goal "..."` 的完整调用链。S3 相对 S2 保持**双进程 IPC 架构**不变（`kama` 客户端 → `kama-core` 守护进程，TCP + JSON-RPC NDJSON），但内化了三件大事：

1. **任务系统内化** —— 新增 `TaskManager` + 四个任务工具（`task_create` / `task_update` / `task_list` / `task_get`），任务以 JSON 文件形式落盘在 `runs/<id>/.tasks/`。
2. **八工具体系** —— 工具注册表从 S2 的少数几个扩展到八个：`read_file`、`bash`、`write_file`、`list_dir` + 上述四个任务工具。
3. **系统级统一时间线 trace** —— 新增 `TraceWriter` / `TracingProvider`，把 IPC、事件、LLM 三个层面的活动统一写入一条 NDJSON 时间线，供 `kama trace` 查询。

> S2 版本（双进程 IPC 骨架）见 `run-flow-s2.md`。本文档是它的 S3 演进版。

---

## 零、S2 vs S3 架构变化（先看这个）

| | S2（双进程 IPC 骨架） | S3（任务 + trace） |
|---|---|---|
| 单 run 互斥 | 是（`_current_run_task`，同时只允许一个） | **否**（`_running_runs: set[Task]`，支持并发多个 run） |
| 任务系统 | 无 | **`TaskManager` + 4 个任务工具** |
| 工具数量 | 少数几个 | **8 个**（文件/命令/任务四类） |
| trace | 无 | **系统级统一时间线**（ipc / event / llm 三层） |
| LLM 调用记录 | 无 | `TracingProvider` 包裹 provider，记录请求/响应/延迟 |
| AgentRunner 返回值 | 无（`run()` 返回 None） | **`run_and_capture()` 返回 `RunOutcome`**（含最终文本结果） |
| 循环状态载体 | 散落的局部变量 | **`ExecutionContext` dataclass** 统一承载 |
| daemon 事件落盘 | `EventWriter` → events.jsonl | 不变 |
| 事件 → trace | 无 | `_trace_event_handler` 订阅 EventBus，事件同时进 trace |

架构骨架（双进程、两条通道、先订阅后触发）在 S3 完全保留，只是 daemon 内部的执行细节更丰富、可观测性更强。

---

## 一、入口映射（pyproject.toml → Python 函数）

```
终端命令: kama run --goal "整理代码"
    ↓  (pyproject.toml)
    ↓  kama = "kama_claude.cli.main:main"
    ↓
Python 入口: src/kama_claude/cli/__main__.py → 调用 main()
```

---

## 二、命令分发: `cli/main.py` — `main()`

```python
# 第 23-24 行：注册 run 子命令，--goal 必填
run_parser = subparsers.add_parser("run", help="Run an agent task")
run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")

# 第 45-46 行：加载配置、初始化日志
config = get_config()
setup_logging(config)

# 第 50-51 行：分发到 run
elif args.command == "run":
    cmd_run(args.goal, config)
```

与 S2 完全一致——CLI 仍是「瘦客户端」，本身不执行 agent 逻辑，只负责连 daemon、发命令、打印事件。

---

## 三、客户端核心: `cli/commands/run.py` — `_run_async()`

客户端生命周期仍是 S2 那五步，代码结构几乎没变：

```python
# 第 66 行
async def _run_async(goal: str, config: KamaConfig) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()                       # ① TCP 连接
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = StdoutPrinter()                        # ② 终端打印器
    finished = asyncio.Event()                       #    等待 run.finished 的信号灯
    exit_code = 0

    async def on_event(event: dict[str, Any]) -> None:  # ② 事件回调（跑在后台 task）
        nonlocal exit_code
        await printer.handle(event)
        if event.get("type") == "run.finished":
            if event.get("status") != "success":
                exit_code = 1
            finished.set()

    client.on_event(on_event)                        # ② 注册回调
    loop_task = asyncio.create_task(client.run_event_loop())  # ③ 后台死循环读事件

    try:
        # ④ 先订阅 topic，再触发 agent.run（顺序不能反）
        await client.send_command(
            "event.subscribe",
            {"topics": ["run.*", "step.*", "tool.*", "llm.token", "llm.usage"],
             "scope": "global"},
        )
        await client.send_command("agent.run", {"goal": goal})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        loop_task.cancel()
        await client.close()
        return 1

    await finished.wait()                            # ⑤ 挂起，等 run.finished

    loop_task.cancel()                               # 清理
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
    await client.close()
    return exit_code
```

### 五步总结

| 步骤 | 动作 | 代码 |
|------|------|------|
| ① 连接 | `SocketClient.connect()` | `run.py:69` |
| ② 准备 | printer + `finished` 信号灯 + `on_event` 回调 | `run.py:74-86` |
| ③ 后台读 | `create_task(run_event_loop())` | `run.py:87` |
| ④ 发命令 | `event.subscribe` 然后 `agent.run` | `run.py:90-97` |
| ⑤ 等待 | `await finished.wait()` 直到 run.finished | `run.py:104` |

### `StdoutPrinter.handle()`（客户端侧事件渲染）

```python
# 第 26-62 行：按事件 type 分发格式化打印
if t == "run.started":      print(f"[run] {run_id}")
elif t == "step.started":   print(f"[step {step}] planning...")
elif t == "llm.token":      print(token, end="", flush=True)   # token 流式内联打印
elif t == "tool.call_started":    print(f"[tool] {name} {params}")
elif t == "tool.call_finished":   print(f"[tool] {name} ✓ {elapsed_ms}ms")
elif t == "tool.call_failed":     print(f"[tool] {name} ✗ {err}", file=sys.stderr)
elif t == "step.finished":  print(f"[step {step}] done")
elif t == "run.finished":   print(f"[run] {status} {steps} steps {elapsed:.1f}s")
```

> **注意**：`llm.token` 用 `end=""` 内联打印，靠 `_ensure_newline()` 在下一个非 token 事件前补换行。这是「流式打字机」效果的关键。

---

## 四、客户端传输层: `transport/socket_client.py` — `SocketClient`

与 S2 一致，两条通道靠消息字段区分：

### 4.1 命令通路：`send_command()`（一发一收）

```python
# 第 51-60 行
async def send_command(self, method, params) -> dict:
    req_id = str(uuid.uuid4())
    request = JsonRpcRequest(id=req_id, method=method, params=params)
    fut = asyncio.get_running_loop().create_future()
    self._pending[req_id] = fut                    # 记下：这个 ID 等哪个 future
    self._writer.write(request.model_dump_json().encode() + b"\n")
    await self._writer.drain()
    return await fut                               # 挂起，等响应填进 future
```

### 4.2 事件通路：`run_event_loop()`（死循环读推送）

```python
# 第 63-79 行
async def run_event_loop(self):
    try:
        while True:
            try:
                line = await self._reader.readline()
            except (ConnectionResetError, OSError):
                break
            if not line:
                break
            await self._dispatch(line)
    finally:
        for fut in self._pending.values():         # 连接断开时清理未完成的请求
            if not fut.done():
                fut.cancel()
        self._pending.clear()
```

### 4.3 分发逻辑：`_dispatch()`

```python
# 第 82-103 行
if "jsonrpc" in msg:                               # 命令响应
    fut = self._pending.pop(req_id)
    if "error" in msg: fut.set_exception(IpcError(...))
    else: fut.set_result(msg.get("result") or {})
elif msg.get("kind") == "event":                   # 服务器推送事件
    for handler in self._event_handlers:
        await handler(event_data)
```

---

## 五、服务器端: `transport/socket_server.py` — `SocketServer`

### 5.1 连接处理与读循环

```python
# 第 92-109 行：每个连接一个协程，断开时清理订阅
async def _handle_connection(self, reader, writer):
    try:
        await self._read_loop(reader, writer)
    finally:
        if self._broadcaster is not None:
            self._broadcaster.unsubscribe(writer)   # ★ 断开时移除该连接的订阅
        writer.close()
```

### 5.2 单行处理：`_handle_line()`（S3 新增 trace 埋点）

```python
# 第 130-182 行
async def _handle_line(self, line, writer):
    raw = json.loads(line)
    req = JsonRpcRequest.model_validate(raw)

    # ★ S3 新增：把收到的命令写进 trace（CLIENT→CORE / ipc / command）
    if self._trace is not None:
        self._trace.emit(TraceRecord(
            direction="CLIENT→CORE", layer="ipc", kind="command",
            client_id=str(writer.get_extra_info("peername")),
            data={"method": req.method, "id": req.id, "params": req.params},
        ))

    handler = self._handlers.get(req.method)
    if handler is None:
        return await self._send(writer, make_error(req.id, METHOD_NOT_FOUND, ...))

    _writer_var.set(writer)                        # 绑定当前连接到协程上下文
    result = await handler(req.params)
    result_data = result.model_dump() if isinstance(result, BaseModel) else result
    await self._send(writer, JsonRpcSuccess(id=req.id, result=result_data))
```

### 5.3 响应写回：`_send()`（S3 新增 trace 埋点）

```python
# 第 185-200 行
async def _send(self, writer, msg):
    writer.write(msg.model_dump_json().encode() + b"\n")
    await writer.drain()
    if self._trace is not None:                    # ★ 把响应也写进 trace
        kind = "error" if isinstance(msg, JsonRpcError) else "response"
        self._trace.emit(TraceRecord(
            direction="CORE→CLIENT", layer="ipc", kind=kind,
            client_id=str(writer.get_extra_info("peername")),
            data=msg.model_dump(),
        ))
```

> S3 的核心增强之一：`_handle_line` 和 `_send` 分别记录「命令进来」「响应出去」，这样一条完整的 IPC 往返在 trace 时间线上可追溯。

---

## 六、daemon 注册与启动: `core/app.py` — `CoreApp`

### 6.1 `run()` 启动流程（S3 新增 trace 初始化）

```python
# 第 133-159 行
async def run(self):
    self._config = get_config()
    setup_logging(self._config)

    # ★ ① 启动 TraceWriter，并订阅 EventBus（事件 → trace）
    if self._config.trace.enabled:
        self._trace = TraceWriter(trace_path)
        await self._trace.start()
        self._bus.subscribe(self._trace_event_handler)

    # ② 广播器：事件 → 客户端（也带 trace）
    self._broadcaster = IpcEventBroadcaster(trace=self._trace)
    self._bus.subscribe(self._broadcaster.handle)

    # ③ 启动 TCP server，注册三个 handler
    server = SocketServer(self._config.host, self._config.port,
                          self._broadcaster, trace=self._trace)
    server.register("core.ping", self._ping_handler)
    server.register("agent.run", self._agent_run_handler)
    server.register("event.subscribe", self._subscribe_handler)

    addr = await server.start()
    ...
```

### 6.2 `agent.run` → `_agent_run_handler()`（S3 支持并发）

```python
# 第 77-85 行
async def _agent_run_handler(self, params) -> AgentRunResult:
    cmd = AgentRunCommand.model_validate(params)
    run_id = new_run_id()
    runner = AgentRunner(self._config, bus=self._bus, trace=self._trace)  # ★ 传 trace
    run_task = asyncio.create_task(runner.run(cmd.goal, run_id=run_id))
    self._running_runs.add(run_task)               # ★ 记录运行中的 task
    run_task.add_done_callback(self._running_runs.discard)  # 结束自动移除
    return AgentRunResult(run_id=run_id)           # 立即返回 run_id
```

**与 S2 的区别**：S2 用 `_current_run_task` 做单 run 互斥（并发会报错），S3 改成 `_running_runs: set[Task]`——**允许多个 run 并发执行**，关闭时统一 cancel（`app.py:169-172`）。

### 6.3 `event.subscribe` → `_subscribe_handler()`

```python
# 第 88-100 行
async def _subscribe_handler(self, params) -> EventSubscribeResult:
    cmd = EventSubscribeCommand.model_validate(params)
    writer = get_connection_writer()               # 拿到当前连接

    replayed_count = 0
    if cmd.replay_from_run is not None:            # 可选：回放历史事件
        replayed_count = await self._replay_events(cmd.replay_from_run, writer, cmd.topics)

    sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
    return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)
```

---

## 七、agent 执行编排: `core/runner.py` — `AgentRunner`

这是 S3 执行层的核心，`run_and_capture()` 组装所有运行时依赖：

```python
# 第 81-141 行
async def run_and_capture(self, goal, *, run_id=None) -> RunOutcome:
    run_id = run_id or new_run_id()
    run_path = self._runs_dir / run_id
    run_path.mkdir(parents=True, exist_ok=True)

    task_manager = TaskManager(run_path / ".tasks")   # ★ 每个 run 独立的任务存储

    bus = self._bus if self._bus is not None else EventBus()
    for h in self._extra_handlers:
        bus.subscribe(h)

    context = ExecutionContext(run_id=run_id, goal=goal,
                               max_steps=self._config.agent.max_steps)

    async with EventWriter(run_path / "events.jsonl") as writer:
        writer.subscribe(bus)                          # 事件落盘
        await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

        provider = self._provider or AnthropicProvider(self._config.llm.default_model)
        if self._trace is not None:
            provider = TracingProvider(provider, self._trace,       # ★ 包裹 trace
                                       include_payload=...)

        registry = self._build_registry(task_manager)              # ★ 八工具体系
        loop = AgentLoop(provider, registry, bus)

        try:
            await loop.run(context)
        except asyncio.CancelledError:
            cancelled = True
            if not context.is_done():
                context.mark_failed("cancelled")

        await bus.publish(RunFinishedEvent(
            run_id=run_id, status=context.status, reason=context.reason,
            steps=context.step, ts=_now()))

    if cancelled:
        raise asyncio.CancelledError()

    return RunOutcome(status=context.status, result=context.result, reason=context.reason)
```

### 7.1 八工具体系：`_build_registry()`

```python
# 第 64-74 行
def _build_registry(self, task_manager) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(BashTool())
    registry.register(WriteFileTool())
    registry.register(ListDirTool())
    registry.register(TaskCreateTool(task_manager))   # ★ 四个任务工具共享同一 TaskManager
    registry.register(TaskUpdateTool(task_manager))
    registry.register(TaskListTool(task_manager))
    registry.register(TaskGetTool(task_manager))
    return registry
```

| 工具 | 类别 | 说明 |
|------|------|------|
| `read_file` | 文件 | 读取文件内容 |
| `write_file` | 文件 | 写文件 |
| `list_dir` | 文件 | 列目录 |
| `bash` | 命令 | 执行 shell 命令 |
| `task_create` | 任务 | 新建任务 |
| `task_update` | 任务 | 更新任务状态/依赖 |
| `task_list` | 任务 | 列出所有任务 |
| `task_get` | 任务 | 读取单个任务 |

---

## 八、任务系统: `core/task/manager.py` — `TaskManager`

S3 内化的任务系统，用**文件即数据库**的方式持久化任务：

```
runs/<run_id>/
  ├── events.jsonl          ← 事件日志（EventWriter）
  └── .tasks/
       ├── task_1.json       ← 每个任务一个 JSON 文件
       ├── task_2.json
       └── ...
```

### 关键方法

```python
# 第 43-64 行：创建任务，校验 blocked_by 依赖存在
def create(self, subject, description="", blocked_by=None) -> Task:
    for dep_id in (blocked_by or []):
        if not (self._dir / f"task_{dep_id}.json").exists():
            raise ValueError(f"blocked_by task {dep_id} not found")
    task = Task(id=self._next_id, subject=subject, ...)
    self._save(task)          # 写 task_<id>.json
    self._next_id += 1
    return task

# 第 71-92 行：更新任务；status="completed" 时自动清理其他任务的 blocked_by
def update(self, task_id, *, status=None, add_blocked_by=None, remove_blocked_by=None):
    ...
    if status == "completed":
        self._clear_dependency(task_id)   # 移除其他任务对它的依赖引用

# 第 105-115 行：把 completed_id 从所有其他任务的 blocked_by 里删掉
def _clear_dependency(self, completed_id): ...
```

**任务依赖模型**：任务通过 `blocked_by` 列表形成 DAG，`task_update` 把某任务标记为 `completed` 时，自动从其他任务的 `blocked_by` 中移除该 ID——这是让 Agent「拆解目标 → 逐个完成 → 依赖自动解锁」的机制基础。

---

## 九、agent 循环: `core/loop.py` — `AgentLoop`

plan → act → observe 循环，与 S2 结构一致，但状态从散落变量收拢到 `ExecutionContext`：

```python
# 第 31-81 行
async def run(self, context: ExecutionContext):
    while not context.is_done():
        context.step += 1
        await self._bus.publish(StepStartedEvent(run_id=..., step=context.step))

        # [plan] 调 LLM；API 错误终止 run
        try:
            response = await self._provider.chat(
                messages=context.messages,
                tool_schemas=self._registry.tool_schemas(),
                bus=self._bus, run_id=context.run_id, step=context.step)
        except asyncio.CancelledError:
            context.mark_failed("cancelled"); raise
        except Exception:
            context.mark_failed("llm_error"); break

        # [observe] 把 assistant content blocks 追加进消息历史
        blocks = [{"type": "text", "text": response.text}]
        for tc in response.tool_calls:
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
        context.add_assistant_message(blocks)

        # [act] 执行工具；错误变成 tool_result 继续循环
        if response.stop_reason == "tool_use":
            for tc in response.tool_calls:
                result = await invoke_tool(self._registry, tc, self._bus, context.run_id)
                context.add_tool_result(tc.id, result.content, is_error=result.is_error)

        # 终止判定：end_turn 优先于 max_steps
        if response.stop_reason == "end_turn":
            context.result = response.text or ""
            context.mark_success()
        elif context.step >= context.max_steps:
            context.mark_failed("exceeded_max_steps")

        await self._bus.publish(StepFinishedEvent(run_id=..., step=context.step))
```

### `ExecutionContext`（循环状态载体）

```python
# context.py 第 7-62 行
@dataclass
class ExecutionContext:
    run_id: str
    goal: str
    max_steps: int
    messages: list[dict] = field(default_factory=list)   # 完整对话历史
    step: int = 0
    status: str = "running"   # "running" | "success" | "failed"
    reason: str | None = None
    result: str = ""

    def __post_init__(self):           # goal 作为第一条 user 消息
        self.messages.append({"role": "user", "content": self.goal})

    def add_assistant_message(self, content): ...   # 追加 assistant 消息
    def add_tool_result(self, tool_use_id, content, is_error=False): ...
    def is_done(self): return self.status != "running"
    def mark_success(self): self.status = "success"
    def mark_failed(self, reason): self.status = "failed"; self.reason = reason
```

---

## 十、工具调用: `core/tools/invocation.py` — `invoke_tool()`

工具执行不抛异常，所有失败都收敛为 `ToolResult(is_error=True)` 回填给 LLM：

```python
# 第 49-114 行
async def invoke_tool(registry, tool_call, bus, run_id, timeout=120.0):
    await bus.publish(ToolCallStartedEvent(run_id=..., tool_name=..., params=...))

    tool = registry.get(tool_call.name)
    if tool is None:
        return await _fail(bus, ..., "runtime_error", f"unknown tool: {tool_call.name}")

    # 校验必填参数
    required = tool.input_schema.get("required", [])
    missing = [p for p in required if p not in tool_call.input]
    if missing:
        return await _fail(bus, ..., "schema_error", f"missing required parameters: ...")

    try:
        result = await asyncio.wait_for(tool.invoke(tool_call.input), timeout=timeout)
        if result.is_error:
            return await _fail(bus, ..., result.error_type or "runtime_error", ...)
        await bus.publish(ToolCallFinishedEvent(run_id=..., tool_name=..., elapsed_ms=..., output=...))
        return result
    except TimeoutError:
        return await _fail(bus, ..., "timeout", f"tool timed out after {timeout}s")
    except Exception as exc:
        return await _fail(bus, ..., "runtime_error", str(exc))
```

**事件粒度**：每个工具调用发三个事件——`tool.call_started`（含 params）、`tool.call_finished`（含 elapsed_ms、output）或 `tool.call_failed`（含 error_type、error_message）。这是客户端和 TUI 展示工具进度的数据来源。

---

## 十一、LLM 调用: `core/llm/provider.py` + `trace/provider.py`

### 11.1 `AnthropicProvider.chat()`（真实 LLM 调用）

```python
# 第 38-110 行
async def chat(self, messages, tool_schemas, bus, run_id, *, step=0):
    await bus.publish(LlmModelSelectedEvent(run_id=..., model=..., strategy="static"))

    # system prompt + 最后一个 tool schema 加 cache_control（prompt caching）
    system = [{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    tools = list(tool_schemas)
    if tools:
        last = dict(tools[-1]); last["cache_control"] = {"type": "ephemeral"}
        tools = tools[:-1] + [last]

    text_parts = []
    async with self._client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:
            await bus.publish(LlmTokenEvent(run_id=..., token=text))   # ★ 逐 token 推送
            text_parts.append(text)
        final_message = await stream.get_final_message()

    await bus.publish(LlmUsageEvent(run_id=..., input_tokens=..., output_tokens=..., ...))

    # 抽取 tool_use blocks → ToolCallBlock 列表
    tool_calls = [ToolCallBlock(id=b.id, name=b.name, input=dict(b.input))
                  for b in final_message.content if b.type == "tool_use"]
    return LlmResponse(stop_reason=..., tool_calls=..., text="".join(text_parts), usage=...)
```

### 11.2 `TracingProvider.chat()`（trace 包裹层）

```python
# trace/provider.py 第 33-95 行
async def chat(self, messages, tool_schemas, bus, run_id, *, step=0):
    self._trace.emit(TraceRecord(direction="CORE→LLM", layer="llm",
                                 kind="api_call", run_id=run_id, step=step,
                                 data={"messages": messages, ...}))
    t0 = time.monotonic()
    result = await self._inner.chat(...)             # 调真实 provider
    latency_ms = int((time.monotonic() - t0) * 1000)
    self._trace.emit(TraceRecord(direction="LLM→CORE", layer="llm",
                                 kind="api_response", run_id=run_id, step=step,
                                 data={"stop_reason": ..., "usage": ..., "latency_ms": ...}))
    return result
```

**装饰器模式**：`TracingProvider` 包住 `AnthropicProvider`，不改变 `chat()` 签名，只在调用前后各写一条 trace 记录（`CORE→LLM` / `LLM→CORE`），让每次 LLM 往返的请求、响应、延迟都落在时间线上。`include_payload=False` 时可只记元数据不记正文。

---

## 十二、事件如何推回客户端（EventBus 的多个订阅者）

daemon 内 `self._bus`（`app.py:45`）在启动时挂了 3 个订阅者：

| 订阅者 | 注册位置 | 作用 |
|--------|---------|------|
| `_trace_event_handler` | `app.py:142` | 事件 → trace（layer=event） |
| `IpcEventBroadcaster.handle` | `app.py:145` | 事件 → 客户端 socket |
| `EventWriter` | `runner.py:101` | 事件 → `runs/<id>/events.jsonl` |

`EventBus.publish()`（`events/bus.py:19-21`）按注册顺序**依次 `await` 所有订阅者**。当 `AgentLoop` 里 `bus.publish(StepStartedEvent(...))` 时，一条事件同时流到 trace、客户端、落盘文件三个去处。

### `_trace_event_handler`（事件 → trace）

```python
# app.py 第 62-74 行
async def _trace_event_handler(self, event):
    event_dict = event.model_dump()
    self._trace.emit(TraceRecord(
        ts=_now(), direction="CORE", layer="event", kind="event",
        run_id=event_dict.get("run_id"), data=event_dict))
```

---

## 十三、trace 时间线: `core/trace/writer.py` — `TraceWriter`

S3 的系统级 trace 用「队列 + 后台 drain task」异步落盘，不阻塞事件主流程：

```python
# 第 11-44 行
class TraceWriter:
    def __init__(self, path):
        self._queue = asyncio.Queue()           # 非阻塞队列
        self._task = None

    async def start(self):                       # 建目录 + 启动后台 drain task
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._drain())

    def emit(self, record):                      # ★ 非阻塞：put_nowait 进队列
        self._queue.put_nowait(record)

    async def _drain(self):                      # 后台 task 逐行 append 写文件
        with open(self._path, "a") as f:
            while True:
                record = await self._queue.get()
                try:
                    f.write(record.model_dump_json() + "\n"); f.flush()
                finally:
                    self._queue.task_done()
```

**关键设计**：`emit()` 是**非阻塞**的（`put_nowait`），真正的磁盘写入在后台 `_drain` task 里串行进行。这样在 agent 循环里随手 `emit` 一条 trace 不会阻塞事件推送或 LLM 调用，trace 的开销被隔离到独立的写协程。

### trace 的三层覆盖

| layer | direction | 记录点 |
|-------|-----------|--------|
| `ipc` | `CLIENT→CORE` / `CORE→CLIENT` | `socket_server.py:143` / `:185` |
| `event` | `CORE` | `app.py:62`（每个 EventBus 事件） |
| `llm` | `CORE→LLM` / `LLM→CORE` | `trace/provider.py:51` / `:83` |

这构成了 `kama trace` 命令（`cli/commands/trace.py`）可查询的「系统级统一时间线」。

---

## 十四、完整调用链总览

```
终端 A:  kama core start                          ← 启动 daemon（监听 127.0.0.1:7437）
           └─ CoreApp.run(): 启动 TraceWriter + EventBus(3订阅者) + SocketServer

终端 B:  kama run --goal "整理代码"
  │
  ├─ cli/main.py: main() → cmd_run(goal, config)
  │
  └─ cli/commands/run.py: _run_async(goal, config)
       │
       │  ┌────────────── 客户端进程 (kama) ──────────────┐
       │  ├─ ① SocketClient.connect()  ── TCP ────────────┼──→ daemon
       │  ├─ ② printer + finished + on_event 回调         │
       │  ├─ ③ create_task(run_event_loop()) 后台读       │
       │  ├─ ④ send_command("event.subscribe", {...})     │
       │  ├─ ④ send_command("agent.run", {"goal": ...})   │
       │  └─ ⑤ await finished.wait()                     │
       └──────────────────────────────────────────────────┘
                        ▲                              │
            事件(推送)   │                              │ 命令(请求-响应)
                        │                              ▼
       ┌──────────────────────────────────────────────────────────────┐
       │                daemon 进程 (kama-core)                        │
       │                                                               │
       │  SocketServer._handle_line()                                  │
       │    ├─ agent.run → _agent_run_handler()                       │
       │    │      └─ create_task(runner.run(goal))  ──后台执行─────┐ │
       │    └─ event.subscribe → _subscribe_handler()                │ │
       │           └─ broadcaster.subscribe(writer, topics)          │ │
       │                                                              │ │
       │  AgentRunner.run_and_capture() (后台 task)                   │ │
       │    ├─ TaskManager(runs/<id>/.tasks)  ← 任务落盘              │ │
       │    ├─ ExecutionContext(goal, max_steps)                      │ │
       │    ├─ EventWriter(events.jsonl)                             │ │
       │    ├─ provider = TracingProvider(AnthropicProvider)          │ │
       │    ├─ registry = 八工具体系                                  │ │
       │    └─ AgentLoop.run(context)                                 │ │
       │         └─ 每步: plan(chat) → observe(add msg) → act(invoke_tool)│
       │              └─ bus.publish(各种事件)                        │ │
       │                   ├─ _trace_event_handler → TraceWriter      │ │
       │                   ├─ EventWriter → events.jsonl              │ │
       │                   └─ IpcEventBroadcaster → 匹配topic → 写回客户端 socket ─┘
       │                                                               │
       │  TraceWriter._drain() (后台 task)                             │
       │    └─ ipc / event / llm 三层记录 → trace 文件 (NDJSON)        │
       └───────────────────────────────────────────────────────────────┘
```

---

## 十五、文件加载顺序（import 链）

```
1.  pyproject.toml                           ← kama = cli.main:main
2.  cli/__main__.py                          → main()
3.  cli/main.py                              ← 导入 cmd_run
4.  cli/commands/run.py                      ← 导入 SocketClient, StdoutPrinter
5.  core/transport/socket_client.py          ← SocketClient, IpcError
6.  core/bus/envelope.py                     ← JsonRpcRequest, EventPushEnvelope

[daemon 侧，kama-core 启动时]
7.  core/app.py                              ← CoreApp（导入 runner/trace/transport）
8.  core/runner.py                           ← AgentRunner（导入 loop/task/tools）
9.  core/loop.py                             ← AgentLoop（导入 context/invocation）
10. core/context.py                          ← ExecutionContext
11. core/task/manager.py                     ← TaskManager + model
12. core/tools/registry.py + builtin/*       ← 八工具体系
13. core/tools/invocation.py                 ← invoke_tool
14. core/llm/provider.py                     ← AnthropicProvider
15. core/trace/writer.py + provider.py       ← TraceWriter, TracingProvider
16. core/transport/socket_server.py          ← SocketServer
17. core/transport/ipc_broadcaster.py        ← IpcEventBroadcaster
18. core/events/bus.py + writer.py           ← EventBus, EventWriter
```

---

## 十六、关键设计要点

1. **双通道分离（沿用 S2）**：命令走「请求-响应」（`send_command` + pending future），事件走「服务器推送」（`broadcaster` + `run_event_loop`），靠 `jsonrpc` vs `kind="event"` 字段区分。

2. **先订阅后触发（沿用 S2）**：客户端必须 `event.subscribe` 再 `agent.run`，否则漏掉订阅前的事件（`run.py:90-97`）。

3. **并发 run 支持（S3 改变）**：`_running_runs: set[Task]` 取代 S2 的单 run 互斥，允许多个 run 同时跑，关闭时统一 cancel（`app.py:169-172`）。

4. **事件三路分发（S3 增强）**：一条 EventBus 事件同时流向 trace（`_trace_event_handler`）、客户端（`IpcEventBroadcaster`）、落盘（`EventWriter`），互不干扰。

5. **trace 三层统一时间线（S3 新增）**：ipc（命令进出）、event（事件流）、llm（API 往返）三种活动以 `TraceRecord` 统一格式写入一条 NDJSON，`emit()` 非阻塞、后台 `_drain` 串行落盘。

6. **TracingProvider 装饰器（S3 新增）**：不改变 `LLMProvider` 协议，包裹真实 provider 记录每次 `chat()` 的请求/响应/延迟。

7. **文件即任务库（S3 新增）**：`TaskManager` 用 `task_<id>.json` 文件持久化任务，`blocked_by` 建模依赖，`completed` 时自动清理依赖引用。

8. **工具错误不炸循环（S3 保持）**：`invoke_tool` 把所有失败（unknown tool / schema_error / timeout / runtime_error）收敛为 `ToolResult(is_error=True)` 回填 LLM，循环继续，而非抛出异常终止 run。

9. **ContextVar 绑定连接（沿用 S2）**：`_writer_var` 让 handler 拿到「当前请求对应的 socket 连接」，从而把订阅绑定到正确客户端。

10. **prompt caching（S3 新增）**：`AnthropicProvider.chat` 给 system prompt 和最后一个 tool schema 加 `cache_control: ephemeral`，降低多轮对话的 token 成本。

---

## 附：与 S2 文档的对照阅读

| 主题 | S2 文档 | 本文档 |
|------|---------|--------|
| 客户端五步 | `run-flow-s2.md` 三节 | 第三节（不变） |
| `_run_async` 异步概念 | `run_run_async.md` | 直接沿用，未重复展开 |
| 事件推送机制 | `run-flow-s2.md` 七节 | 第十二节（新增 trace 订阅者） |
| agent 循环 | `run-flow-s2.md` 八节 | 第九节（状态收拢到 ExecutionContext） |
| 任务系统 | 无 | 第八节（新增） |
| trace | 无 | 第十三节（新增） |
