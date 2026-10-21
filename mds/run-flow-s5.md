# kama 工具执行安全调用链详解（S5：权限审批 + 失败分类 + 持久化策略）

本文档梳理 S5 阶段的**主要实现逻辑**与**完整执行流程**。S5 相对 S4 保持**双进程 IPC 架构**与**会话/记忆模型**不变，变化集中在"工具真正执行之前和失败之后"：

1. **权限审批** —— 新增 `core/permissions/` 子系统：静态策略评估 + 用户交互审批 + session 级缓存 + 跨会话持久化策略；bash / write_file 默认需要审批。
2. **失败分类与重试** —— `tool.call_failed` 事件把 `error_type` 改为 `error_class` 并新增 `attempt`；`runtime_error` / `rate_limited` 自动指数退避重试，其余错误直接失败。
3. **参数 schema 校验内化** —— `BaseTool` 新增 `params_model`，每个工具用 pydantic 模型校验参数，校验失败统一归为 `schema_error`，且**在权限审批之前**发生。
4. **IPC 并发化（关键改造）** —— `SocketServer._read_loop` 从"逐行 await"改为"每行一个 task"，否则审批响应 `permission.respond` 会被正在运行的 `session.send_message` 堵在 socket 缓冲里。

> S4 版本（会话 + 分层语义记忆 + TUI 输入框）见 `run-flow-s4.md`。本文档是它的 S5 演进版。

---

## 零、S4 vs S5 变化（先看这个）

| | S4（会话 + 记忆 + 输入框） | S5（工具执行安全） |
|---|---|---|
| 工具执行 | LLM 决定后直接执行 | **先过权限闸门，再执行** |
| 权限模型 | 无 | **静态策略 4 层 + 运行时缓存 2 层** |
| 审批交互 | 无 | **`permission.requested` / `permission.respond` 反向请求** |
| 审批超时 | 无 | **默认 60s，超时=拒绝一次**（`0` 表示不超时） |
| 持久化 | 会话记忆（thread / notes / tasks） | **`~/.kama/policy.toml` 的 `[always]` 决策** |
| 失败事件 | `error_type`，无重试 | **`error_class` + `attempt`，可重试错误自动重试 2 次** |
| 参数校验 | 各工具内部 `str(...)` 强转 | **pydantic `params_model` 统一校验 → `schema_error`** |
| 同一连接的命令 | **串行**（`await _handle_line`） | **并发**（`asyncio.create_task(_handle_line)`） |
| TUI 审批 | 无 | **内联 `PermissionSelect` 选择器 + `PermissionBlock` 摘要** |
| TUI 日志 | 无（只有屏幕） | **`~/.kama/logs/tui.log` 滚动文件** |
| IPC 命令 | 7 个 | **8 个**（+`permission.respond`） |
| IPC 事件 | 17 个 | **20 个**（+3 个 permission 事件） |

一句话概括 S5：**把"工具调用"从"LLM 说执行就执行"改造成"策略判断 → 用户确认 → 执行 → 分类失败 → 必要时重试"的完整流水线。**

---

## 一、入口与用户可见行为

入口没有变化（`kama-tui` / `kama chat` / `kama run` → 同一个 daemon），但行为变了：

| 场景 | S4 | S5 |
|---|---|---|
| `read_file` / `list_dir` / `note_save` | 直接执行 | 仍然**直接执行**（默认 ALLOW，无噪音） |
| `bash` / `write_file` | 直接执行 | **弹审批**，默认 60s 内未响应则拒绝 |
| 未在策略表登记、但已注册的工具（如四个 task 工具） | 直接执行 | **按 ASK 处理**（未登记兜底） |
| 工具参数类型错误 | 可能抛异常或静默强转 | **`schema_error`**，不弹审批、不执行 |
| 工具运行时失败 | 直接回填错误 | **分类后决定是否重试**（最多 3 次尝试） |

对应配置：

```toml
# ~/.kama/config.toml（可选）
[permission]
timeout_s = 60      # 审批超时秒数；0 表示不超时
```

环境变量 `KAMA_PERMISSION_TIMEOUT_S` 优先级更高（`core/config.py:246`）。

---

## 二、权限策略模型：`permissions/policy.py`

### 2.1 三种静态决策

```python
# policy.py:9
class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
```

### 2.2 工具默认策略

```python
# policy.py:40-49
DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    "bash":       ToolPolicy(default=PermissionDecision.ASK),
    "write_file": ToolPolicy(default=PermissionDecision.ASK),
    "read_file":  ToolPolicy(default=PermissionDecision.ALLOW),
    "list_dir":   ToolPolicy(default=PermissionDecision.ALLOW),
    "note_save":  ToolPolicy(default=PermissionDecision.ALLOW),
}

# 未登记的工具一律 ASK
_UNKNOWN_TOOL_DEFAULT = PermissionDecision.ASK
```

注意**任务类工具**（`task_create` / `task_update` / `task_list` / `task_get`）不在表里——按"未登记 → ASK"处理，也就是说 S5 里它们默认也需要审批。这是一个容易被忽略的行为变化。

> 区分两类"未知"：**注册表里根本不存在**的工具在 `invoke_tool` 的 `registry.get()` 处直接返回 `runtime_error: unknown tool`，根本走不到权限层；这里说的 ASK 只针对"工具存在但策略表里没登记"的情况。

| 工具 | 默认 | 理由 |
|---|---|---|
| `read_file` / `list_dir` / `note_save` | ALLOW | 只读或会话内安全写，避免权限疲劳 |
| `bash` / `write_file` | ASK | 有副作用 / 可执行任意命令 |
| 四个 task 工具 | ASK（未登记兜底） | 保守策略 |

### 2.3 `OUTSIDE_CWD` 强制审批

```python
# policy.py:15-23
OUTSIDE_CWD_HEURISTICS = [
    r"(^|\s)/[^\s]",              # 绝对路径
    r"(^|\s)~",                   # ~ 家目录
    r"(^|\s)\.\.(/|$|\s)",        # 父目录穿越
    r"\$\{?HOME\b",               # $HOME
    r"\$\{?PWD\b",                # $PWD
    r"(^|\s|;|&&|\|\|)cd(\s|$)",  # 显式 cd
]
```

这组规则只作用于 `bash` 的 `command` 参数，命中的含义是：**即使之前选过 always_allow，也必须重新问用户**。它是整个策略里优先级第二高的闸门（仅次于 `deny_patterns`）。

### 2.4 参数摘要：`param_preview()`

```python
# policy.py:63-73
_PREVIEW_KEY = {"bash": "command", "read_file": "path",
                "write_file": "path", "list_dir": "path",
                "note_save": "content"}
# 生成 key='value'，超过 60 字符截断加 "…"
```

审批卡片只展示最有信息量的一个字段，而不是整段 JSON：

```
[permission] bash  command='rm -rf build'
```

---

## 三、`PermissionManager`：审批状态机

`PermissionManager`（`permissions/manager.py:37`）是整个 S5 的核心运行时组件，持有四份状态：

| 字段 | 类型 | 作用 | 生命周期 |
|---|---|---|---|
| `_pending` | `dict[tool_use_id, _PendingRequest]` | 每个挂起审批对应的 `Future` + session/tool 元数据 | 请求创建 → respond/超时 |
| `_session_always` | `dict[(session_id, tool_name), "allow"/"deny"]` | 本会话内"总是允许/拒绝" | daemon 进程内，重启丢失 |
| `_persistent_always` | `dict[tool_name, "allow"/"deny"]` | 跨会话持久决策 | 进程内 + `policy.toml` |
| `_timeout_s` | `float` | 审批超时；`0` = 不超时 | 启动配置 |

### 3.1 六层决策流水线（`check_and_wait`，`manager.py:65`）

```python
# ① deny_patterns：bash 专用，最高优先级，不可绕过
if command and policy:
    for pat in policy.deny_patterns:
        if re.search(pat, command):
            return False, "auto_deny"

# ② OUTSIDE_CWD：命中即强制 ASK，跳过所有缓存与 allow_patterns
outside_cwd = bool(command and matches_outside_cwd(command))

if not outside_cwd:
    # ③ session always 缓存
    # ④ persistent always 缓存
    # ⑤ allow_patterns
    # ⑥ tool default（ALLOW / DENY / ASK）

# ⑦ 走到这里说明需要问用户：挂 Future + 发 permission.requested
```

这条流水线的关键在于**顺序本身就是安全语义**：

```
deny_patterns  →  OUTSIDE_CWD  →  session always  →  persistent always
     (拒)            (强制问)          (缓存)              (缓存)
                                                              ↓
                                    allow_patterns  →  tool default  →  ASK（占位等待）
```

`always_allow` 只能跳过 ⑤⑥，**跳不过 ①②**；所以"总是允许 bash"之后，`rm -rf`（若配置了 deny 规则）照样被拒，`cd ~ && ...` 照样要重新确认。

> 注：`manager.py:59` 的 `evaluate()` 是对 `policy.evaluate()` 的委托（4 层静态评估）；但 `check_and_wait()` 内部并没有调用它，而是自己重写了一遍 6 层逻辑。当前 `evaluate()` 只被单测覆盖，生产路径不使用。

### 3.2 ASK 路径：Future + 事件

```python
# manager.py:114-143
future: asyncio.Future[str] = loop.create_future()
self._pending[tool_use_id] = _PendingRequest(future, session_id, tool_name)

await event_emitter({                      # → PermissionRequestedEvent
    "type": "permission.requested",
    "tool_use_id": tool_use_id,
    "tool_name": tool_name,
    "params": params,
    "param_preview": param_preview(tool_name, params),
    "session_id": session_id,
    ...
})

if self._timeout_s > 0:
    raw = await asyncio.wait_for(future, timeout=self._timeout_s)
else:
    raw = await future
```

这里用的是**反向请求**模式：`check_and_wait` 不是轮询，而是在一个 Future 上挂起；客户端稍后通过 `permission.respond` 命令把决策 `set_result()` 进来。

### 3.3 `respond()`：唯一能唤醒 Future 的入口

```python
# manager.py:149-155
def respond(self, tool_use_id: str, decision: str) -> None:
    req = self._pending.pop(tool_use_id, None)
    if req is None:
        logger.warning("permission.respond: unknown tool_use_id=%s", tool_use_id)
        return
    if not req.future.done():
        req.future.set_result(decision)
```

对未知 / 已过期的 `tool_use_id` 只打 warning、不抛异常——这是为了容忍"超时之后迟到的响应"和"客户端重复发送"这两类竞态。

### 3.4 四种用户决策与缓存写入（`_apply_response`，`manager.py:158`）

| 决策 | 本次是否放行 | session 缓存 | 持久化 `policy.toml` |
|---|---|---|---|
| `allow_once` | ✅ | 不写 | 不写 |
| `always_allow` | ✅ | 写 `allow` | 写 `allow` |
| `deny_once` | ❌ | 不写 | 不写 |
| `always_deny` | ❌ | 写 `deny` | 写 `deny` |
| 超时（内部） | ❌ | 不写 | 不写 |

注意 `always_allow` / `always_deny` 是**按工具名**持久化的，不区分参数；参数级的安全由 ①② 两层兜底。

### 3.5 超时分支

```python
except asyncio.TimeoutError:
    self._pending.pop(tool_use_id, None)
    logger.info("permission: timeout tool_use_id=%s tool=%s", tool_use_id, tool_name)
    return False, "timeout"
```

超时的语义是"**拒绝这一次**"，不是"终止整个 run"——上层会把它变成一条 `permission_denied` 的工具结果回填给模型，循环继续。

### 3.6 `cancel_session()`：设计上存在，当前未接线

```python
# manager.py:193-204
def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
    # 把该 session 的所有 pending Future 设为 deny_once
```

它的目的是客户端断连时防止 Future 永久挂起。但 S5 的 `SocketServer._handle_connection` / `CoreApp` **没有任何地方调用它**——全仓只有定义和单测。所以客户端在审批中途断开的实际行为是：等 60s 超时兜底（若 `timeout_s=0`，则真正永久挂起）。这一点在第十五节还会展开。

---

## 四、持久化策略：`permissions/storage.py`

### 4.1 文件格式

```toml
# ~/.kama/policy.toml
# 由 kama-core 自动管理，手动编辑生效但格式须正确

[always]
bash = "allow"
write_file = "deny"
```

### 4.2 读写实现

```python
# storage.py:9-30：逐行解析，只认 [always] 节下的 `key = "allow"/"deny"`
def load_policy_file(path: Path | None = None) -> dict[str, str]:

# storage.py:33-51：整体覆盖重写 [always] 节（按工具名排序）
def save_policy_file(always: dict[str, str], path: Path | None = None) -> None:
```

选择手写解析而不是引入 TOML 库，原因很明确：**只需要一个极小的子集**（一个节 + 字符串键值），手写 20 行就能完成，且错误格式会被静默忽略而不是让 daemon 起不来。

写入时机：`_apply_response()` 里收到 `always_allow` / `always_deny` 且 `policy_file` 非空时（`manager.py:167-184`），写失败只记日志、不影响本次决策。

---

## 五、IPC 契约增量

### 5.1 命令（`core/bus/commands.py:81`）

```python
class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    decision: str   # allow_once | always_allow | deny_once | always_deny
```

daemon 侧 handler（`app.py:126-133`）只做一件事：`PermissionManager.respond(tool_use_id, decision)`。

### 5.2 事件（`core/bus/events.py:143-169`）

| 事件 | 触发条件 | 关键字段 |
|---|---|---|
| `permission.requested` | 策略判定为 ASK | `run_id`、`tool_use_id`、`tool_name`、`params`、`param_preview`、`session_id` |
| `permission.granted` | 用户放行且不是 `auto_allow` | `decision`（`allow_once`/`always_allow`） |
| `permission.denied` | 用户拒绝 / 超时，且不是 `auto_deny` | `decision`（`deny_once`/`always_deny`/`timeout`） |

### 5.3 事件矩阵：哪些情况会发 permission 事件

| 决策来源 | `permission.requested` | `permission.granted` | `permission.denied` |
|---|---|---|---|
| 默认 ALLOW / 缓存 allow / allow_patterns（`auto_allow`） | ❌ | ❌（刻意静默） | ❌ |
| deny_patterns / 缓存 deny（`auto_deny`） | ❌ | ❌ | ❌（只有 `tool.call_failed`） |
| 用户 `allow_once` / `always_allow` | ✅ | ✅ | ❌ |
| 用户 `deny_once` / `always_deny` | ✅ | ❌ | ✅ |
| 超时 | ✅ | ❌ | ✅（`decision="timeout"`） |

`auto_allow` 不发 `permission.granted` 是刻意的：只读工具每一步都发一条 granted 会把事件流刷爆。

---

## 六、关键并发改造：`SocketServer` 读循环

这是 S5 里**最容易被忽略、但决定审批能否工作**的一处改动。

### 6.1 改了什么

```python
# S4：一条连接上的命令严格串行
await self._handle_line(line, writer)

# S5（socket_server.py:127-129）：
# 每条命令独立作为 task 执行，避免长时间运行的 handler（如 session.send_message）
# 阻塞读循环，使 permission.respond 等并发命令能被及时处理
asyncio.create_task(self._handle_line(line, writer))
```

### 6.2 为什么必须改

`session.send_message` 的 handler 要**等整轮 run 结束**才返回（这是 S4 就确定的设计）。而审批的流程是：

```
客户端 await send_command("session.send_message")   ← 挂起
   └─ daemon: _session_send_handler → send_message → run → tool → 等审批 Future
                                                                      ▲
客户端 send_command("permission.respond") ────────────────────────────┘
```

如果读循环还是串行，第二条命令（respond）根本没机会被读出来，只能等第一条 handler 返回——而第一条要等 respond。**必然死锁，最后只能靠 60s 超时打破**。改成 `create_task` 后，同一连接上的命令可以并发处理，审批链路才真正闭合。

### 6.3 并发化带来的三个副作用（都已处理或可接受）

| 副作用 | 分析 |
|---|---|
| 响应可能乱序到达 | 无影响：`SocketClient._dispatch` 按 JSON-RPC `id` 匹配各自的 Future |
| 多个 task 同时 `_send` 写同一 `writer` | 无影响：`_send` 里 `write(完整一行)` 是同步完成的，没有 await 插在编码和写入之间，字节不会交错 |
| `session.close` 可能与 run 并发 | 安全：`SessionManager` 的 per-session 锁会抛 `SESSION_BUSY`，不会破坏状态 |

还有两个值得记录的小点：

- `_writer_var.set(writer)` 发生在每个 per-line task 自己的 context 里，`create_task` 会复制上下文，因此不同连接/任务之间不会串写。
- 这些 per-line task **没有被集中跟踪**，daemon 关闭时不会主动 `await` 它们；不过 `_handle_line` 内部已捕获 handler/发送异常，正常路径下不会留下未处理异常。

---

## 七、daemon 装配：`CoreApp`

```python
# app.py:205-209
policy_file = Path("~/.kama/policy.toml").expanduser()
self._permission_manager = PermissionManager(
    policy_file=policy_file,
    timeout_s=self._config.permission.timeout_s,
)

# app.py:220-229：runner 工厂把同一个 manager 实例注入每次 run
self._sessions = SessionManager(
    store,
    runner_factory=lambda: AgentRunner(
        self._config, bus=self._bus, trace=self._trace,
        permission_manager=self._permission_manager,
    ),
    bus=self._bus,
)

# app.py:244
server.register("permission.respond", self._permission_respond_handler)
```

**一个 daemon 只有一个 `PermissionManager`**，它同时服务所有 session：因此 `_session_always` 按 `(session_id, tool_name)` 隔离，而 `_persistent_always` 全局共享——这正是"会话内缓存 / 跨会话持久化"两层语义的实现点。

---

## 八、执行链：`AgentLoop` → `invoke_tool`

### 8.1 依赖注入

```python
# loop.py:24-37
class AgentLoop:
    def __init__(self, provider, registry, bus, *,
                 permission_manager: PermissionManager | None = None,
                 session_id: str = "") -> None:
```

`AgentRunner` 在构造 `AgentLoop` 时传入 `permission_manager` 和 `session_id`（`runner.py:153-157`）。`session_id` 是给缓存键用的；没有 session（单测里的裸 run）时传空串。

### 8.2 工具调用点

```python
# loop.py:80-88
for tc in response.tool_calls:
    result = await invoke_tool(
        self._registry, tc, self._bus, context.run_id,
        permission_manager=self._permission_manager,
        session_id=self._session_id,
    )
    context.add_tool_result(tc.id, result.content, is_error=result.is_error)
```

多个工具调用仍然是**顺序执行**（同一个 step 里 `for` + `await`），因此同一时刻一个 run 最多只有一个待审批请求。

### 8.3 `invoke_tool` 的五个阶段

```python
# invocation.py:64 起
① 发 tool.call_started                      ← TUI 先看到"工具开始"
② 查工具 + 校验 params_model                ← 失败 → schema_error（不弹审批）
③ 权限闸门 check_and_wait                   ← ALLOW / DENY / ASK(等待)
④ 限时执行 tool.invoke(timeout=120)         ← 失败分类 + 重试
⑤ 发 tool.call_finished / tool.call_failed
```

注意 ① 在权限判断之前：所以用户在审批弹窗出现时，日志里已经能看到 `[tool] bash ...` 这一行。

---

## 九、失败分类与重试：`invoke_tool`

### 9.1 错误分类表

```python
# invocation.py:28-30
_MAX_RETRIES = 2
_RETRY_BASE_S = 2.0
_RETRYABLE = frozenset({"runtime_error", "rate_limited"})
```

| `error_class` | 来源 | 是否重试 | 是否发 `tool.call_failed` |
|---|---|---|---|
| `schema_error` | pydantic 参数校验失败 | ❌ | ✅（attempt=1） |
| `permission_denied` | 用户拒绝 / 超时 / auto_deny | ❌ | ✅ |
| `timeout` | `tool.invoke` 超过 120s | ❌ | ✅ |
| `runtime_error` | 工具内部异常或 `ToolResult(is_error=True, error_type="runtime_error")` | ✅ 最多 2 次 | 每次尝试都发 |
| `rate_limited` | 工具抛 `RateLimitedError` | ✅ 最多 2 次 | 每次尝试都发 |
| `runtime_error`（unknown tool） | 注册表查不到工具 | ❌（立即返回） | ✅ |

### 9.2 重试循环

```python
# invocation.py:144-205
for attempt in range(1, _MAX_RETRIES + 2):        # 1,2,3 → 最多 3 次尝试
    try:
        result = await asyncio.wait_for(
            tool.invoke(dict(tool_call.input)), timeout=timeout
        )
        ...
    except RateLimitedError:
        error_class = "rate_limited"
    except TimeoutError:
        return await _fail(..., "timeout", ...)     # 不重试
    except Exception:
        error_class = "runtime_error"

    if error_class in _RETRYABLE and attempt <= _MAX_RETRIES:
        await bus.publish(ToolCallFailedEvent(..., attempt=attempt))
        await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))   # 2s, 4s
        continue
```

时间预算：`2s + 4s` 的退避 + 最多 `3 × 120s` 的工具执行；**审批等待（默认 60s）在这个预算之外**，因为它发生在 ④ 之前。

### 9.3 事件字段变化

```python
# events.py:63-73
class ToolCallFailedEvent(BaseModel):
    ...
    error_class: str      # 原 error_type 改名
    error_message: str
    elapsed_ms: int
    attempt: int = 1      # 1=首次, 2=第一次重试, 3=第二次重试
```

`ToolResult.error_type` 这个**内部**字段没有改名，只有 IPC 事件字段改成了 `error_class`；`_fail()` 负责把内部 `error_type` 映射成事件的 `error_class`。`WIRE_PROTOCOL.md` 已同步重新生成。

---

## 十、参数 schema 校验：`BaseTool.params_model`

```python
# tools/base.py:18-27
class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    params_model: ClassVar[type[BaseModel] | None] = None
```

每个内置工具都声明自己的模型：

| 工具 | 模型 | 关键约束 |
|---|---|---|
| `bash` | `BashParams` | `command: str` 必填；`timeout` 默认 60，范围 1..120 |
| `read_file` | `ReadFileParams` | `path: str` 必填 |
| `write_file` | `WriteFileParams` | `path` / `content` 必填 |
| `list_dir` | `ListDirParams` | `path` 默认 `"."`；`max_depth` 默认 2，范围 1..4 |
| `note_save` | `NoteSaveParams` | `content: str` 必填 |

三个设计要点：

1. **全部 `extra="ignore"`**：LLM 多传字段不会失败，只忽略。
2. **上限写进模型**（`le=120` / `le=4`），工具实现里不再需要 `min(...)` 兜底。
3. **校验发生在权限审批之前**：参数非法时直接 `schema_error`，不会用一个明显错误的命令去打扰用户。

校验调用点在 `invoke_tool` 里（`invocation.py:96-103`）：

```python
if tool.params_model is not None:
    try:
        tool.params_model.model_validate(dict(tool_call.input))
    except ValidationError as exc:
        return await _fail(..., "schema_error", str(exc), elapsed())
```

---

## 十一、TUI 审批交互：`PermissionSelect` / `PermissionBlock`

### 11.1 两个新控件

| 控件 | 位置 | 职责 |
|---|---|---|
| `PermissionBlock`（`tui/app.py:247`） | 追加到 `#log-view` | 审批摘要，解决后原地变成一行结论（`✓ permission bash command='...' allowed (once)` / `⏱ timed out`） |
| `PermissionSelect`（`tui/app.py:141`） | 挂到 Screen 顶层、`#prompt` 之前 | 可聚焦的选择器，↑↓/jk 导航，Enter 确认，y/a/n/d 或 1/2/3/4 快捷键 |

四个选项：

```
  ❯ Allow once        y / 1
    Always allow      a / 2
    Deny              n / 3
    Always deny       d / 4
```

### 11.2 为什么要有 `App.on_key` 兜底

Textual 里控件焦点可能被滚动容器抢走。`KamaTuiApp.on_key`（`tui/app.py:391-416`）在检测到有 pending permission 时，如果 `PermissionSelect` 没拿到焦点，就在 App 层重新解释 y/a/n/d、↑↓、Enter，保证无论焦点在哪都能完成审批。

### 11.3 发送必须放在 worker 里

```python
# tui/app.py:444
self.run_worker(self._do_send_message(content), name="send_message", exclusive=False)
```

S4 里是直接 `await send_command("session.send_message")`。S5 改成 worker 后，`await` 不再阻塞 Textual 的消息泵，**审批弹窗的键盘事件才能被处理**。这是 TUI 侧必须改的一处配套改动。

### 11.4 状态机

```
用户提交消息
  ├─ _busy=True，输入框 disabled，标题 "agent is working..."
  └─ run_worker(_do_send_message)  ← 不阻塞 UI

permission.requested
  ├─ PermissionBlock 追加到日志
  ├─ PermissionSelect 挂到 #prompt 之前并 focus
  └─ 输入框 disabled，标题 "permission required"

用户按 y/a/n/d/Enter
  ├─ PermissionSelect.Decided 消息
  ├─ 移除选择器，PermissionBlock 原地更新为结论
  ├─ send_command("permission.respond", {tool_use_id, decision})
  └─ 无其他 pending → 输入框重新 enabled + focus

permission.denied（超时 / 断连）
  ├─ PermissionBlock 标记 "⏱ timed out"
  ├─ 移除选择器
  └─ 输入框恢复（工具将被拒绝，run 继续）

session.waiting_for_input
  └─ _busy=False，输入框恢复
```

`permission.granted` 事件 TUI **没有分支**——因为用户点击时本地已经完成了 UI 收尾，不需要再渲染一次。

### 11.5 TUI 文件日志

```python
# tui/__main__.py:16-33
_DEFAULT_TUI_LOG = "~/.kama/logs/tui.log"
RotatingFileHandler(maxBytes=5MB, backupCount=3)
```

Textual 占满终端，往 stderr 打日志会破坏渲染，所以 S5 把日志统一写滚动文件；重点关注焦点转移（`PermissionSelect.on_mount/on_focus/on_blur`）这类难以复现的 UI 问题。

---

## 十二、CLI 侧的审批行为

### 12.1 `kama chat`

```python
# chat.py:10-15
_DECISION_MAP = {"y": "allow_once", "a": "always_allow",
                 "n": "deny_once",  "d": "always_deny"}
```

`ChatPrinter` 收到 `permission.requested` 时打印：

```
[permission] bash  command='echo hi'
  y=allow once  a=always allow  n=deny once  d=always deny
```

然后把 `pending_permission_id` 置上；`while` 循环下一轮读到的输入如果命中决策映射，就发 `permission.respond`。

**已知限制**：主协程此时仍卡在 `await send_command("session.send_message")` 上，并没有回到 `_readline()`，所以实际上用户无法在审批窗口内输入。详见第十四节。

### 12.2 `kama run`

S5 没有修改 `run.py`：它既不订阅 `permission.*`，也没有审批回复路径。因此任何默认 ASK 的工具都会：

```
等待 60s → 超时 → permission_denied → 模型收到错误继续 → run 结束
```

纯读取任务（`read_file` / `list_dir`）不受影响。

---

## 十三、完整执行流程（TUI 触发一次 bash 工具）

```
用户: "跑一下 pytest"
  │
  ├─ TUI: run_worker(_do_send_message) → send_command("session.send_message")
  │
  ▼
daemon: SocketServer._read_loop
  └─ asyncio.create_task(_handle_line)          ← S5：命令并发
       └─ session.send_message → SessionManager.send_message
            ├─ 锁住 session，追加 user 消息到 thread.jsonl
            └─ AgentRunner.run_and_capture → AgentLoop.run
                 ├─ LLM: 返回 tool_use(bash, {"command": "pytest"})
                 └─ invoke_tool
                      ├─ ① 发 tool.call_started
                      │     └─ EventBus → trace / events.jsonl / IPC → TUI 工具块
                      ├─ ② 校验 BashParams（command 存在、timeout 合法）
                      ├─ ③ PermissionManager.check_and_wait
                      │     ├─ deny_patterns?  否
                      │     ├─ OUTSIDE_CWD?    否
                      │     ├─ session always? 否
                      │     ├─ persistent?     否
                      │     ├─ allow_patterns? 否
                      │     ├─ default = ASK
                      │     ├─ 创建 Future，登记 _pending[tool_use_id]
                      │     └─ 发 permission.requested
                      │           └─ EventBus → trace / events.jsonl / IPC
                      │                 └─ TUI: PermissionBlock + PermissionSelect
                      │
                      │   （run 协程挂起在 await future；读循环仍在跑）
                      │
                      ├─ 用户按 y
                      │    └─ TUI: send_command("permission.respond")
                      │         └─ daemon 读循环 create_task → _permission_respond_handler
                      │              └─ manager.respond(tool_use_id, "allow_once")
                      │                   └─ future.set_result("allow_once")
                      │
                      ├─ ④ _apply_response → allowed=True（不写缓存）
                      │    └─ 发 permission.granted
                      ├─ ⑤ asyncio.wait_for(tool.invoke(...), timeout=120)
                      │    ├─ 成功 → 发 tool.call_finished(output)
                      │    └─ 失败 → 分类；可重试则 2s/4s 退避后再来
                      │
                      └─ 返回 ToolResult → context.add_tool_result
                 └─ 下一步 plan → 直到 end_turn
            ├─ thread.jsonl 批量追加本轮模型/工具消息
            └─ status → waiting_for_input，发 session.waiting_for_input
  │
  ▼
TUI: run.finished + session.waiting_for_input → 输入框恢复
```

超时分支只改变中间一段：

```
Future 60s 未被 set_result
  → asyncio.TimeoutError → 返回 (False, "timeout")
  → 发 permission.denied(decision="timeout")
  → tool.call_failed(error_class="permission_denied")
  → 模型看到"权限被拒绝"，可以换方案或向用户解释
  → run 正常收尾，客户端照常收到 JSON-RPC 响应
```

---

## 十四、边界情况与当前实现现状

这些不是设计目标，而是**代码当前真实行为**，排查问题时很有用。

### 14.1 超时是"拒绝一次"，不是"卡死"

默认 `timeout_s=60` 时，最坏情况是每个 ASK 工具多等 60 秒，然后 run 继续。`session.send_message` 的 JSON-RPC 响应最终一定会返回，输入框也会恢复。

### 14.2 `timeout_s=0` 才是真正的无界等待

```python
if self._timeout_s > 0:
    raw = await asyncio.wait_for(future, timeout=self._timeout_s)
else:
    raw = await future      # 永远等下去
```

配置成 0 表示"不超时"，而不是"关闭审批"。如果此时客户端不在（`kama run`、断连、TUI 被杀），Future 永远不完成 → run 永远不结束 → session 锁永远被占。**无人值守场景不要用 0。**

### 14.3 迟到的审批响应会被忽略

超时后 `_pending` 已被 pop；之后到达的 `permission.respond` 命中 `respond()` 的 `req is None` 分支，只打 warning。不会崩溃，也不会"复活"已拒绝的调用。

### 14.4 客户端断连不会主动取消审批

`cancel_session()` 已实现但**没有接入** `_handle_connection` 的断连清理。后果：

- `timeout_s>0`：最多 60s 后自愈；
- `timeout_s=0`：pending Future 永久悬挂，run 任务与 session 锁泄漏。

### 14.5 `kama chat` 实际上无法在审批窗口内回复

`ChatPrinter` 会打印选项、设置 `pending_permission_id`，但 `_chat_async` 主协程此时正 `await send_command("session.send_message")`，没有回到 `_readline()`。用户在 60s 内敲的 `y` 会停留在终端驱动缓冲里；等超时 → run 结束 → `session.waiting_for_input` 清空 `pending_permission_id` → 下一次 `input("> ")` 把缓冲的 `y` 当成**聊天消息**提交。

结论：S5 的 `kama chat` 遇到 ASK 工具必然走超时拒绝路径。

### 14.6 `kama run` 看不到审批请求

`run.py` 的订阅列表没有 `permission.*`，也没有回复逻辑。默认 ASK 的 `bash` / `write_file` / 四个 task 工具都会等满 60s 后以 `permission_denied` 失败。

### 14.7 策略模式只能改代码

`PermissionManager._policies` 支持注入自定义 `ToolPolicy`，但 `config.py` 只暴露 `timeout_s`；`policy.toml` 只存 `[always]` 决策，不存 `allow_patterns` / `deny_patterns`。想改规则需要改代码或构造自定义 manager（目前只用于单测）。

---

## 十五、文件改动清单（S5）

| 类别 | 文件 | 内容 |
|---|---|---|
| 新增（权限） | `core/permissions/policy.py` | 决策枚举、默认策略、OUTSIDE_CWD 规则、参数摘要 |
| | `core/permissions/manager.py` | 6 层决策、pending Future、缓存、超时、持久化写入 |
| | `core/permissions/storage.py` | `policy.toml` 的 `[always]` 读写 |
| | `core/permissions/errors.py`、`__init__.py` | 异常与导出 |
| 修改（执行） | `core/tools/invocation.py` | 权限闸门 + 参数校验 + 失败分类 + 重试 |
| | `core/tools/base.py` | `params_model` 声明 |
| | `core/tools/builtin/*.py` | 每个工具的参数模型（bash/read/write/list/note_save） |
| | `core/tools/errors.py` | `RateLimitedError` |
| | `core/loop.py`、`core/runner.py` | 注入 `PermissionManager` + `session_id` |
| 修改（传输） | `core/transport/socket_server.py` | **每行命令独立 create_task** + 发送失败容错 |
| 修改（协议） | `core/bus/commands.py`、`core/bus/events.py` | `permission.respond` + 3 个事件 + `error_class`/`attempt` |
| | `WIRE_PROTOCOL.md` | 重新生成 |
| 修改（daemon） | `core/app.py` | 初始化 PermissionManager、注册 handler、注入 runner |
| | `core/config.py` | `[permission] timeout_s` + `KAMA_PERMISSION_TIMEOUT_S` |
| 修改（前端） | `tui/app.py` | `PermissionSelect` / `PermissionBlock` / worker 发送 / 超时收尾 |
| | `tui/__main__.py` | 滚动文件日志 |
| | `cli/commands/chat.py` | 权限提示与 y/a/n/d 决策映射 |
| 调试 | `trace_permission_flow.py` | 端到端诊断脚本（自动回复 `allow_once`） |

测试增量：

- `tests/unit/test_permission_policy.py` —— 4 层静态策略、OUTSIDE_CWD 不可绕过、未知工具兜底、`param_preview`。
- `tests/unit/test_permission_manager.py` —— ASK/respond、四种决策缓存、超时、迟到响应、`cancel_session`、持久化落盘。
- `tests/unit/test_tool_params.py` —— 五个参数模型的必填/类型/范围/额外字段。
- `tests/unit/test_tool_retry.py` —— 可重试/不可重试分类、attempt 递增、退避。
- `tests/integration/test_s5_permission_flow.py` —— allow_once 执行、deny_once 不执行、always_allow 只问一次。

---

## 十六、关键设计要点

1. **权限闸门放在工具执行之前、参数校验之后**：既不执行非法参数，也不用错误参数打扰用户。
2. **策略顺序即安全语义**：`deny_patterns` 和 `OUTSIDE_CWD` 位于所有缓存之前，`always_allow` 无法绕过它们。
3. **反向请求而不是轮询**：`permission.requested` + `permission.respond` + Future，审批链路上没有任何 `sleep` 轮询。
4. **两层缓存**：`(session, tool)` 内存缓存解决"本轮别反复问"，`policy.toml` 解决"下次启动别再问"。
5. **超时即拒绝一次**：不终止 run，把决定权交回模型继续规划，避免一次不响应导致整个任务失败。
6. **IPC 必须并发化**：`create_task(_handle_line)` 是审批功能的前置条件；没有它，审批必然死锁到超时。
7. **失败可分类、可重试**：只有 `runtime_error` / `rate_limited` 重试，`schema_error` / `timeout` / `permission_denied` 立即失败，避免放大副作用。
8. **`tool.call_failed` 记录每次尝试**：`attempt` 字段让 trace 里能还原"重试了几次、每次多久"。
9. **参数模型是契约的一部分**：`input_schema` 给 LLM 看，`params_model` 给运行时校验，两者必须同步维护。
10. **TUI 的输入与审批必须非阻塞**：`run_worker` + 内联选择器 + App 层快捷键兜底，保证 agent 运行时 UI 仍然可交互。
11. **日志不能进终端**：TUI 使用 `~/.kama/logs/tui.log` 滚动文件，避免破坏 Textual 渲染。
12. **审批事件本身可观测**：三个 permission 事件与 tool 事件一样进入 EventBus → trace / events.jsonl / IPC，事后可以还原"谁在什么时候批准了什么"。

---

## 十七、与 S4 文档的对照阅读

| 主题 | S4 文档 | 本文档 |
|---|---|---|
| 双进程、双通道 | `run-flow-s4.md` 五、十四节 | 不变（第六节只讲并发化） |
| 会话状态机 | 第四节 | 不变；新增 `permission.respond` 走同一连接 |
| 工具注册表（9 个） | 6.3 节 | 不变；新增权限闸门 |
| `AgentLoop` | 第九节 | 第八节（多注入两个参数） |
| 工具调用与错误收敛 | 第十节 | 第八、九节（权限 + 分类 + 重试） |
| `ExecutionContext` / 记忆 | 第七、八节 | 不变 |
| TUI 输入框 | 第十二节 | 第十一节（新增审批控件与 worker 发送） |
| IPC 契约 | 第十三节 | 第五节（+1 命令 +3 事件） |
| 权限审批 | 无 | 第二、三、四、六、七节（新增） |
| 失败分类与重试 | 无 | 第九节（新增） |
