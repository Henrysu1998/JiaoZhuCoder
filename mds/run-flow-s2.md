# kama run 调用链详解（S2：双进程 IPC 架构）

本文档梳理 S2 阶段 `kama run --goal "..."` 的完整调用链。S2 相比 S1 的根本变化：**从单进程改为双进程 IPC** —— `kama` 客户端通过 TCP + JSON-RPC 连接 `kama-core` 守护进程，agent 循环在 daemon 里跑，进度事件通过 socket 推回客户端。

> S1 版本（单进程直连）见 `run-flow.md`，已过时。

---

## 零、S1 vs S2 架构变化（先看这个）

| | S1（单进程） | S2（双进程 IPC） |
|---|---|---|
| agent 循环在哪跑 | CLI 进程内 | **daemon 进程内** |
| `kama run` 做什么 | 自己组装 AgentRunner 直接跑 | **连 daemon，发命令，收事件** |
| 需要先启动 daemon | 否 | **是**（`kama core start`） |
| 事件怎么到终端 | 进程内直接调 StdoutPrinter | **daemon 通过 socket 推送** |
| 进程数 | 1 | 2（CLI + daemon） |

S2 的关键：agent 的执行逻辑（LLM 调用、工具调用、事件总线）**全部搬到了 daemon**，CLI 退化成一个「瘦客户端」，只负责：连上 daemon → 发命令 → 打印 daemon 推来的事件。

---

## 一、入口映射（pyproject.toml → Python 函数）

```
终端命令: kama run --goal "整理代码"
    ↓  (pyproject.toml:21)
    ↓  kama = "kama_claude.cli.main:main"
    ↓
Python 入口: src/kama_claude/cli/__main__.py → 调用 main()
```

---

## 二、命令分发: `cli/main.py` — `main()`

```python
# 第 22-23 行：注册 run 子命令
run_parser = subparsers.add_parser("run", help="Run an agent task")
run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")

# 第 37-38 行：加载配置、初始化日志
config = get_config()
setup_logging(config)

# 第 42-43 行：分发到 run
elif args.command == "run":
    cmd_run(args.goal, config)
```

---

## 三、客户端核心: `cli/commands/run.py` — `_run_async()`

这是 S2 客户端的灵魂，一次完整生命周期分五步：

```python
# 第 66 行
async def _run_async(goal: str, config: KamaConfig) -> int:
    # 第一步：连接 daemon
    client = SocketClient(config.host, config.port)
    await client.connect()                       # ← TCP 连接（open_connection）

    # 第二步：准备回调
    printer = StdoutPrinter()                    # 终端打印器
    finished = asyncio.Event()                   # 用于等 run.finished 的开关
    exit_code = 0

    async def on_event(event: dict) -> None:     # 事件回调（跑在后台 task）
        nonlocal exit_code
        await printer.handle(event)              # 格式化打印
        if event.get("type") == "run.finished":  # 收到结束事件
            if event.get("status") != "success":
                exit_code = 1
            finished.set()                       # 唤醒主协程

    client.on_event(on_event)                    # 注册回调
    # 第三步：后台 task 死循环读 socket 事件
    loop_task = asyncio.create_task(client.run_event_loop())

    # 第四步：先订阅事件，再触发 run（顺序不能反）
    await client.send_command("event.subscribe",
        {"topics": ["run.*", "step.*", "tool.*", "llm.token", "llm.usage"],
         "scope": "global"})
    await client.send_command("agent.run", {"goal": goal})

    # 第五步：挂起，等 daemon 推来 run.finished
    await finished.wait()

    # 清理：取消后台读循环、关连接
    loop_task.cancel()
    await client.close()
    return exit_code
```

### 五步总结

| 步骤 | 动作 | 代码 |
|------|------|------|
| ① 连接 | `SocketClient.connect()` | `run.py:71` |
| ② 准备 | 建 printer + finished 开关 + on_event 回调 | `run.py:79-97` |
| ③ 后台读 | `create_task(run_event_loop())` | `run.py:102` |
| ④ 发命令 | `event.subscribe` 然后 `agent.run` | `run.py:107-117` |
| ⑤ 等待 | `await finished.wait()` 直到 run.finished | `run.py:127` |

---

## 四、客户端传输层: `transport/socket_client.py` — `SocketClient`

客户端有两个关键机制：

### 4.1 命令通路：`send_command()`（一发一收）

```python
# 第 51-60 行
async def send_command(self, method, params) -> dict:
    req_id = str(uuid.uuid4())                    # 每次请求一个随机 ID
    request = JsonRpcRequest(id=req_id, method=method, params=params)
    fut = asyncio.get_running_loop().create_future()  # 创建一个"等待结果的箱子"
    self._pending[req_id] = fut                   # 记下：这个 ID 等哪个 future
    self._writer.write(request.model_dump_json().encode() + b"\n")
    await self._writer.drain()
    return await fut                              # 挂起，等响应回来填进 future
```

### 4.2 事件通路：`run_event_loop()`（死循环读推送）

```python
# 第 63-79 行
async def run_event_loop(self):
    while True:
        line = await self._reader.readline()      # 读一行
        if not line:
            break
        await self._dispatch(line)                # 分发这行
```

### 4.3 分发逻辑：`_dispatch()`（区分两种消息）

```python
# 第 82-103 行
async def _dispatch(self, line: bytes):
    msg = json.loads(line)

    if "jsonrpc" in msg:                          # 有 jsonrpc 字段 → 是命令的响应
        req_id = msg.get("id")
        fut = self._pending.pop(req_id)           # 找到对应的 future
        if "error" in msg:
            fut.set_exception(IpcError(...))      # 出错 → future 抛异常
        else:
            fut.set_result(msg.get("result"))     # 成功 → future 填结果

    elif msg.get("kind") == "event":              # kind=event → 是服务器推送的事件
        event_data = msg.get("event", {})
        for handler in self._event_handlers:      # 逐个回调（即 on_event）
            await handler(event_data)
```

**核心区分**：一条 socket 线路上跑两种消息 —— 命令的「响应」和服务器「主动推送的事件」。靠 `jsonrpc` 字段 vs `kind="event"` 字段来区分。

---

## 五、服务器端: `transport/socket_server.py` — `SocketServer`

daemon 侧收到请求后，解析并调用已注册的 handler：

```python
# 第 116-152 行
async def _handle_line(self, line, writer):
    raw = json.loads(line)
    req = JsonRpcRequest.model_validate(raw)      # 解析 JSON-RPC 请求

    handler = self._handlers.get(req.method)      # 按方法名找 handler
    if handler is None:
        return self._send(writer, make_error(...))  # 方法不存在

    _writer_var.set(writer)                       # ★ 把当前连接存进 context var
    result = await handler(req.params)            # 调用 handler（如 _agent_run_handler）

    result_data = result.model_dump() if isinstance(result, BaseModel) else result
    await self._send(writer, JsonRpcSuccess(id=req.id, result=result_data))
```

> **`_writer_var`（ContextVar）的作用**：每个连接在自己的协程里处理，`_writer_var.set(writer)` 把「当前是哪个连接」存进协程上下文。这样 handler 里调用 `get_connection_writer()` 就能拿到「正在处理这个请求的连接」—— 用于把订阅和具体的 socket 连接绑定起来。

---

## 六、daemon 注册的三个 handler: `core/app.py`

```python
# 第 116-119 行
server = SocketServer(self._config.host, self._config.port, self._broadcaster)
server.register("core.ping", self._ping_handler)
server.register("agent.run", self._agent_run_handler)
server.register("event.subscribe", self._subscribe_handler)
```

### 6.1 `agent.run` → `_agent_run_handler()`（触发任务）

```python
# 第 52-64 行
async def _agent_run_handler(self, params) -> AgentRunResult:
    cmd = AgentRunCommand.model_validate(params)  # 校验 goal

    if self._current_run_task is not None and not self._current_run_task.done():
        raise RuntimeError("a run is already in progress")  # 同时只允许一个 run

    run_id = new_run_id()
    runner = AgentRunner(self._config, bus=self._bus)  # ★ 复用 daemon 的 EventBus
    self._current_run_task = asyncio.create_task(
        runner.run(cmd.goal, run_id=run_id)            # ★ 后台 task 执行，不阻塞
    )
    return AgentRunResult(run_id=run_id)               # 立即返回 run_id
```

**关键**：handler 不直接 `await runner.run()`，而是用 `asyncio.create_task` 丢到后台。这样 handler 能**立即返回** `run_id` 给客户端，而 agent 循环在后台慢慢跑，进度靠事件通道推给客户端。

### 6.2 `event.subscribe` → `_subscribe_handler()`（订阅事件）

```python
# 第 67-78 行
async def _subscribe_handler(self, params) -> EventSubscribeResult:
    cmd = EventSubscribeCommand.model_validate(params)
    writer = get_connection_writer()              # ★ 拿到当前连接的 writer

    if cmd.replay_from_run is not None:
        replayed_count = await self._replay_events(...)  # 可选：回放历史事件

    sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
    return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)
```

把「当前 socket 连接」注册为事件订阅者，指定关心的 `topics`（如 `run.*`、`tool.*`）和 `scope`。

---

## 七、事件如何从 daemon 推到客户端: `transport/ipc_broadcaster.py`

daemon 内部有个全局 `EventBus`（`app.py:35`），它有两个订阅者：

1. **`IpcEventBroadcaster`**（`app.py:36-37`）—— 把事件推给所有订阅的客户端。
2. **`EventWriter`**（`runner.py:57-58`）—— 把事件写入 `runs/<id>/events.jsonl`。

当 `AgentRunner` 在后台跑时，每发一个事件（`StepStartedEvent`、`LlmTokenEvent`...），`EventBus.publish()` 会同时通知这两个订阅者。

### `IpcEventBroadcaster.handle()`（广播）

```python
# 第 45-66 行
async def handle(self, event: BaseModel):
    event_dict = event.model_dump()
    event_type = event_dict.get("type", "")
    run_id = event_dict.get("run_id")

    for sub in list(self._subscriptions):
        if not self._matches_topic(event_type, sub.topics):   # topic 匹配（fnmatch）
            continue
        if not self._matches_scope(run_id, sub.scope):        # scope 匹配
            continue
        envelope = EventPushEnvelope(event=event_dict)        # 包装成推送信封
        sub.writer.write(envelope.model_dump_json().encode() + b"\n")
        await sub.writer.drain()                              # 推给客户端
```

topic 匹配用 `fnmatch`（glob 通配符），如 `run.*` 匹配 `run.started`、`run.finished`；scope `global` 表示接收所有 run 的事件。

---

## 八、agent 循环（daemon 内部，与 S1 相同）

`AgentRunner.run()`（`runner.py:42`）在 daemon 后台 task 里执行，流程与 S1 一致：

```python
async def run(self, goal, *, run_id=None):
    bus = self._bus                    # ★ 复用 daemon 的 bus（不再新建）
    ...
    async with EventWriter(...) as writer:
        writer.subscribe(bus)          # 事件落盘
        await bus.publish(RunStartedEvent(...))   # → 广播到客户端

        loop = AgentLoop(provider, registry, bus)
        await loop.run(context)        # plan→act→observe 循环，事件不断广播

        await bus.publish(RunFinishedEvent(...))  # → 广播 run.finished
```

唯一区别：S1 里 `bus` 是新建的，S2 里 `bus` 是 daemon 全局的（`app.py:35`），这样事件才能通过 broadcaster 流到客户端。

---

## 九、两条通道（S2 的核心设计）

| | 命令通路 | 事件通路 |
|---|---|---|
| 方向 | 客户端 → daemon → 客户端 | daemon → 客户端 |
| 模式 | 一发一收（请求-响应） | 服务器主动推（server push） |
| 客户端 API | `send_command()` | `run_event_loop()` + `on_event()` |
| 服务器 API | handler 返回结果 | `IpcEventBroadcaster.handle()` |
| 消息区分 | 有 `jsonrpc` 字段 | `kind == "event"` |
| 例子 | `agent.run`、`event.subscribe` | `run.started`、`llm.token`、`run.finished` |

**为什么分两条通道**：命令是「同步的请求-响应」（发个命令等结果）；而 agent 运行期间会持续产生大量进度事件（每个 token、每个工具调用），这些是「异步的、源源不断的」。用一条通道做请求-响应、另一条通道做推送，各司其职。

---

## 十、完整调用链总览

```
终端 A:  kama core start                     ← 先启动 daemon（后台，见 core.py）
           └─ subprocess 启动 kama-core，监听 127.0.0.1:7437

终端 B:  kama run --goal "..."               ← 再运行客户端
  │
  ├─ cli/main.py: main() → cmd_run(goal, config)
  │
  └─ cli/commands/run.py: _run_async(goal, config)
       │
       │  ┌──────────────── 客户端进程 (kama) ────────────────┐
       │  │
       │  ├─ ① SocketClient.connect()  ── TCP 连接 ──────────┼──→ daemon
       │  ├─ ② 准备 printer + finished + on_event 回调        │
       │  ├─ ③ create_task(run_event_loop())  后台读消息      │
       │  ├─ ④ send_command("event.subscribe", {...})         │
       │  ├─ ④ send_command("agent.run", {"goal": ...})       │
       │  └─ ⑤ await finished.wait()  挂起等结束              │
       │                                                     │
       └─────────────────────────────────────────────────────┘
                        ▲                              │
            事件(推送)   │                              │ 命令(请求-响应)
                        │                              ▼
       ┌─────────────────────────────────────────────────────────┐
       │               daemon 进程 (kama-core)                    │
       │                                                          │
       │  SocketServer._handle_line()                             │
       │    ├─ agent.run → _agent_run_handler()                  │
       │    │      └─ create_task(runner.run(goal))  ──后台执行──┐ │
       │    └─ event.subscribe → _subscribe_handler()             │ │
       │           └─ broadcaster.subscribe(writer, topics)       │ │
       │                                                          │ │
       │  AgentRunner.run() (后台 task)                           │ │
       │    └─ EventBus.publish(各种事件)                          │ │
       │         ├─ EventWriter → events.jsonl                    │ │
       │         └─ IpcEventBroadcaster.handle()                  │ │
       │              └─ 匹配 topic/scope → 写回客户端 socket ────┼─┘
       │                                                          │
       └─────────────────────────────────────────────────────────┘
```

---

## 十一、文件加载顺序（import 链）

```
1.  pyproject.toml                          ← kama = cli.main:main
2.  cli/__main__.py                         → main()
3.  cli/main.py                             ← 导入 cmd_run
4.  cli/commands/run.py                     ← 导入 SocketClient, StdoutPrinter
5.  core/transport/socket_client.py         ← SocketClient, IpcError
6.  core/bus/envelope.py                    ← JsonRpcRequest, EventPushEnvelope

[daemon 侧，kama-core 启动时]
7.  core/app.py                             ← 导入 AgentRunner, IpcEventBroadcaster, SocketServer
8.  core/bus/commands.py                    ← AgentRunCommand, EventSubscribeCommand
9.  core/transport/socket_server.py         ← SocketServer, get_connection_writer
10. core/transport/ipc_broadcaster.py       ← IpcEventBroadcaster
11. core/runner.py                          ← AgentRunner（内部再 import loop/provider/tools）
```

---

## 十二、关键设计要点

1. **两条通道分离**：命令走「请求-响应」（`send_command` + pending future），事件走「服务器推送」（`broadcaster` + `run_event_loop`）。靠 `jsonrpc` vs `kind="event"` 字段区分。
2. **先订阅后触发**：客户端必须先 `event.subscribe` 再 `agent.run`，否则会漏掉订阅前发出的事件（`run.py:105-106`）。
3. **异步返回 run_id**：`_agent_run_handler` 用 `create_task` 把 run 丢后台，立即返回 `run_id`，不阻塞命令通路。
4. **单 run 互斥**：daemon 同时只允许一个 run 在跑（`app.py:56-57`），避免并发混乱。
5. **`ContextVar` 绑定连接**：`_writer_var` 让 handler 能拿到「当前请求对应的 socket 连接」，从而把订阅绑定到正确的客户端。
6. **事件落盘 + 实时推送并存**：同一个事件既写 `events.jsonl`（可回放）又推给客户端（实时显示）。
7. **`core` 子命令管理生命周期**：`kama core start/stop/status` 用 PID 文件 + 信号管理 daemon 的启停（`cli/commands/core.py`）。