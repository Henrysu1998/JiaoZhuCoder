# kama run 命令调用链详解

本文档梳理从执行 `kama run --goal "..."` 到一次 agent 任务完成（或失败）的完整调用链，覆盖文件加载顺序与方法调用顺序。

> **重要区别**：`kama run` 与 `kama ping` 走完全不同的路。
> - `kama ping` 是**客户端连 daemon**（走 TCP socket，见 `client-server.md`）。
> - `kama run` 是 **CLI 进程本地直接执行**，不连 daemon。它在 `kama` 进程内自己组装 `AgentRunner` → `AgentLoop` → 调 Anthropic API → 执行工具。

---

## 一、入口映射（pyproject.toml → Python 函数）

```
终端命令: kama run --goal "整理代码"
    ↓  (pyproject.toml:21  [project.scripts])
    ↓  kama = "kama_claude.cli.main:main"
    ↓
Python 入口: src/kama_claude/cli/__main__.py → 调用 main()
```

### 1.1 文件: `pyproject.toml` (第 21 行)

```toml
kama = "kama_claude.cli.main:main"
```

声明了 `kama` 命令指向 `kama_claude.cli.main` 模块的 `main` 函数。

### 1.2 文件: `src/kama_claude/cli/__main__.py`

```python
from kama_claude.cli.main import main
main()
```

`python -m kama_claude.cli` 的入口，仅一行：导入并调用 `main()`。

---

## 二、命令分发: `cli/main.py` — `main()`

```python
# 第 21-22 行：注册 run 子命令，声明必需的 --goal 参数
run_parser = subparsers.add_parser("run", help="Run an agent task")
run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")

# 第 30-31 行：加载配置、初始化日志（与 ping 相同的前置步骤）
config = get_config()
setup_logging(config)

# 第 33-36 行：按子命令分发
if args.command == "ping":
    cmd_ping(config)
elif args.command == "run":
    cmd_run(args.goal, config)      # ← 进入 run 命令
```

流程：`argparse` 解析出 `command="run"` 和 `goal="..."` → 加载配置 → 初始化日志 → 调用 `cmd_run(goal, config)`。

---

## 三、run 命令入口: `cli/commands/run.py` — `cmd_run()`

```python
# 第 73-79 行
def cmd_run(goal: str, config: KamaConfig) -> None:
    printer = StdoutPrinter()                                # ① 终端打印器
    runner = AgentRunner(config, extra_handlers=[printer.handle])  # ② 组装 runner
    try:
        asyncio.run(runner.run(goal))                        # ③ 启动事件循环执行
    except KeyboardInterrupt:
        sys.exit(130)                                        # Ctrl+C → 退出码 130
```

三个动作：

1. **`StdoutPrinter()`**（第 24-69 行）—— 订阅事件总线的打印器，把 run 的进度（step、token、tool 调用等）格式化打印到终端。
2. **`AgentRunner(config, extra_handlers=[printer.handle])`** —— 组装运行时，把打印器作为「额外事件处理器」传进去。
3. **`asyncio.run(runner.run(goal))`** —— 启动事件循环，执行一次完整 run。

---

## 四、核心装配: `core/runner.py` — `AgentRunner.run()`

这是把「运行一次 agent 所需的所有零件」拼装起来的地方。

```python
# 第 40-80 行
async def run(self, goal: str) -> None:
    run_id = new_run_id()                                   # ① 生成唯一 ID
    run_path = self._runs_dir / run_id
    run_path.mkdir(parents=True, exist_ok=True)             # ② 建 runs/<run_id>/ 目录

    bus = EventBus()                                        # ③ 事件总线
    for h in self._extra_handlers:
        bus.subscribe(h)                                    # ④ 订阅打印器

    provider = self._provider or AnthropicProvider(self._config.llm.default_model)
    registry = ToolRegistry()                               # ⑤ LLM provider + 工具注册表
    registry.register(ReadFileTool())                       #    目前只注册 read_file 工具

    loop = AgentLoop(provider, registry, bus)               # ⑥ 核心循环

    context = ExecutionContext(                             # ⑦ 执行上下文
        run_id=run_id, goal=goal,
        max_steps=self._config.agent.max_steps,             #    默认 20 步
    )

    async with EventWriter(run_path / "events.jsonl") as writer:  # ⑧ 事件落盘
        writer.subscribe(bus)                               #    把 writer 也注册为订阅者
        await bus.publish(RunStartedEvent(...))             # ⑨ 发「run 开始」事件

        try:
            await loop.run(context)                         # ⑩ 驱动核心循环
        except asyncio.CancelledError:
            ...

        await bus.publish(RunFinishedEvent(...))            # ⑪ 发「run 结束」事件
```

关键零件一览：

| 零件 | 类 | 作用 |
|------|-----|------|
| run_id | `new_run_id()` | 生成 `YYYYMMDD-HHMMSS-xxxxxx` 唯一 ID |
| 事件总线 | `EventBus` | 解耦进度上报，向所有订阅者广播事件 |
| LLM provider | `AnthropicProvider` | 封装 Anthropic API 调用（流式） |
| 工具注册表 | `ToolRegistry` | 注册并查找可调用的工具 |
| 核心循环 | `AgentLoop` | 驱动 plan→act→observe 直到结束 |
| 执行上下文 | `ExecutionContext` | 保存 goal、消息历史、step 数、状态 |
| 事件落盘 | `EventWriter` | 把每个事件写入 `runs/<id>/events.jsonl` |

---

## 五、核心循环: `core/loop.py` — `AgentLoop.run()`

这是 agent 的「大脑」，反复执行 **plan → act → observe** 直到任务结束。

```python
# 第 31-79 行
async def run(self, context: ExecutionContext) -> None:
    while not context.is_done():              # 只要没结束就一直循环
        context.step += 1
        await self._bus.publish(StepStartedEvent(...))   # 发「step 开始」

        # [plan] 调用 LLM，生成回复（可能要求调用工具）
        response = await self._provider.chat(
            messages=context.messages,         # 完整对话历史
            tool_schemas=self._registry.tool_schemas(),  # 可用工具的 schema
            bus=self._bus,
            run_id=context.run_id,
        )

        # [observe] 把 LLM 回复追加到对话历史
        blocks = []
        if response.text:
            blocks.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
        context.add_assistant_message(blocks)

        # [act] 如果 LLM 要求调工具，逐个执行，结果写回历史
        if response.stop_reason == "tool_use":
            for tc in response.tool_calls:
                result = await invoke_tool(self._registry, tc, self._bus, context.run_id)
                context.add_tool_result(tc.id, result.content, is_error=result.is_error)

        # 终止判断
        if response.stop_reason == "end_turn":
            context.mark_success()             # LLM 给出最终答案 → 成功
        elif context.step >= context.max_steps:
            context.mark_failed("exceeded_max_steps")  # 步数耗尽 → 失败

        await self._bus.publish(StepFinishedEvent(...))   # 发「step 结束」
```

### 5.1 plan-act-observe 循环

| 阶段 | 动作 | 代码位置 |
|------|------|----------|
| **plan** | 调 LLM，让它「想下一步」 | `provider.chat(...)` |
| **observe** | 把 LLM 的回复（文本/工具调用请求）记进对话历史 | `add_assistant_message(...)` |
| **act** | 若 LLM 要调工具，就真的执行工具，结果再记进历史 | `invoke_tool(...)` + `add_tool_result(...)` |

循环结束条件（二选一）：

- `stop_reason == "end_turn"` —— LLM 认为任务完成，给出最终答案 → `status = "success"`。
- `step >= max_steps` —— 超过最大步数（默认 20）→ `status = "failed"`。

---

## 六、LLM 调用: `core/llm/provider.py` — `AnthropicProvider.chat()`

封装 Anthropic SDK 的流式调用，把 API 结果转成内部 `LlmResponse`。

```python
# 第 38-108 行
async def chat(self, messages, tool_schemas, bus, run_id) -> LlmResponse:
    await bus.publish(LlmModelSelectedEvent(...))    # 发「模型已选定」事件

    system = [{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": ...}]  # 系统提示词（带 prompt caching）
    tools = list(tool_schemas)                       # 工具 schema（最后一个带 cache_control）

    kwargs = {"model": self._model, "max_tokens": 4096, "system": system, "messages": messages}
    if tools:
        kwargs["tools"] = tools

    text_parts = []
    async with self._client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:        # 流式逐 token
            await bus.publish(LlmTokenEvent(token=text, ...))  # 每个 token 都发事件（用于实时打印）
            text_parts.append(text)
        final_message = await stream.get_final_message()

    await bus.publish(LlmUsageEvent(...))            # 发「token 用量」事件

    tool_calls = [...]                               # 解析 LLM 返回的 tool_use 块

    return LlmResponse(stop_reason=..., tool_calls=..., text=..., usage=...)
```

关键点：

- **流式输出**：用 `stream.text_stream` 逐 token 读，每拿到一个 token 就发 `LlmTokenEvent`，`StdoutPrinter` 收到后立即打印，实现「打字机」效果。
- **Prompt caching**：system prompt 和最后一个 tool schema 加了 `cache_control: ephemeral`，重复调用时命中缓存省钱。
- 默认模型是 `claude-sonnet-4-6`（`config.py:18`），需要环境变量 `ANTHROPIC_API_KEY`。

---

## 七、工具调用: `core/tools/`

### 7.1 工具注册表 `core/tools/registry.py`

```python
class ToolRegistry:
    def register(self, tool): ...        # 注册工具，同名覆盖
    def get(self, name): ...             # 按名查找
    def tool_schemas(self): ...          # 生成 Anthropic 格式的 schema 列表
```

`tool_schemas()` 把每个工具转成 LLM 能懂的 `{name, description, input_schema}` 结构，传给 `chat()`。

### 7.2 调用入口 `core/tools/invocation.py` — `invoke_tool()`

```python
# 第 49-113 行
async def invoke_tool(registry, tool_call, bus, run_id, timeout=10.0) -> ToolResult:
    await bus.publish(ToolCallStartedEvent(...))       # 发「工具开始」

    tool = registry.get(tool_call.name)
    if tool is None:
        return await _fail(..., "unknown tool: ...")   # 未知工具 → 失败

    missing = [p for p in required if p not in tool_call.input]
    if missing:
        return await _fail(..., "missing required parameters: ...")  # 缺参数 → 失败

    try:
        result = await asyncio.wait_for(tool.invoke(...), timeout=10.0)  # 限时 10 秒
        ...
        await bus.publish(ToolCallFinishedEvent(...))  # 发「工具完成」
        return result
    except TimeoutError:
        return await _fail(..., "tool timed out ...")  # 超时 → 失败
    except Exception as exc:
        return await _fail(..., str(exc))              # 其他异常 → 失败
```

`invoke_tool` 保证**永不抛异常**：任何失败都被转成 `ToolResult(is_error=True)`，让 loop 继续跑（而不是整个 run 崩溃）。

### 7.3 具体工具 `core/tools/builtin/read_file.py` — `ReadFileTool`

```python
class ReadFileTool(BaseTool):
    name = "read_file"
    input_schema = {..., "required": ["path"]}

    async def invoke(self, params) -> ToolResult:
        if ".." in Path(path_str).parts:
            raise PermissionError("path traversal not allowed")  # 防路径穿越
        raw = path.read_bytes()
        text = raw[:_MAX_BYTES].decode(...)                      # 超 512KB 截断
        return ToolResult(content=text)
```

---

## 八、事件流（贯穿全程）

`kama run` 里有一个**事件总线**（`EventBus`），所有进度都以「事件」形式广播。有两个订阅者：

1. **`StdoutPrinter`**（`run.py`）—— 把事件格式化打印到终端，给用户看。
2. **`EventWriter`**（`events/writer.py`）—— 把事件序列化成 JSON 行写入 `runs/<id>/events.jsonl`，留作记录。

事件类型（`bus/events.py`）与触发时机：

| 事件 | 触发时机 |
|------|----------|
| `RunStartedEvent` | run 开始 |
| `RunFinishedEvent` | run 结束 |
| `StepStartedEvent` / `StepFinishedEvent` | 每个 step 的开始/结束 |
| `LlmTokenEvent` | LLM 流式输出的每个 token |
| `LlmModelSelectedEvent` | 每次调用 LLM 时选定模型 |
| `LlmUsageEvent` | LLM 返回后的 token 用量 |
| `ToolCallStartedEvent` / `ToolCallFinishedEvent` / `ToolCallFailedEvent` | 工具调用的开始/成功/失败 |

`StdoutPrinter.handle()`（`run.py:35-69`）根据事件类型打印不同格式：

- `RunStartedEvent` → `[run] <run_id>`
- `LlmTokenEvent` → 原样打印 token（不换行，实现流式）
- `ToolCallStartedEvent` → `[tool] read_file {"path": "..."}`
- `RunFinishedEvent` → `[run] success  3 steps  12.5s`

---

## 九、关键概念

- **run**：一次完整的 agent 任务（一个 goal 从开始到结束），对应一个 `run_id` 和一个 `runs/<id>/` 目录。
- **step**：run 里的一次「plan→act→observe」迭代。一个 run 可能有多步（默认最多 20 步）。
- **plan**：调 LLM 生成下一步动作。
- **act**：执行 LLM 请求的工具调用。
- **observe**：把结果写回对话历史，供下一轮 plan 使用。
- **事件总线**：解耦进度上报，让「终端打印」和「文件落盘」两个订阅者互不干扰。

---

## 十、完整调用链总览

```
kama run --goal "..."                       [终端]
  │
  ├─ pyproject.toml → kama_claude.cli.main:main
  │
  └─ cli/main.py: main()
       ├─ argparse 解析: command="run", goal="..."
       ├─ get_config()                       ← 加载配置
       ├─ setup_logging(config)              ← 初始化日志
       └─ cmd_run(args.goal, config)         [cli/commands/run.py:73]
            ├─ StdoutPrinter()               ← 终端打印器
            ├─ AgentRunner(config, extra_handlers=[printer.handle])
            └─ asyncio.run(runner.run(goal))
                 │
                 └─ core/runner.py: AgentRunner.run(goal)   [第 40 行]
                      ├─ new_run_id()                        ← 唯一 ID
                      ├─ mkdir runs/<run_id>/                ← 建目录
                      ├─ EventBus()                          ← 事件总线
                      ├─ bus.subscribe(printer.handle)       ← 订阅打印器
                      ├─ AnthropicProvider(default_model)    ← LLM provider
                      ├─ ToolRegistry() + ReadFileTool()     ← 工具
                      ├─ AgentLoop(provider, registry, bus)  ← 核心循环
                      ├─ ExecutionContext(run_id, goal, max_steps)
                      ├─ EventWriter(events.jsonl)           ← 事件落盘
                      │    └─ writer.subscribe(bus)
                      ├─ bus.publish(RunStartedEvent)
                      ├─ await loop.run(context)             [core/loop.py:31]
                      │    │
                      │    └─ while not context.is_done():
                      │         ├─ bus.publish(StepStartedEvent)
                      │         ├─ [plan] provider.chat(...)      [provider.py:38]
                      │         │    └─ client.messages.stream()  ← 流式调 Anthropic API
                      │         │         └─ 逐 token publish(LlmTokenEvent)
                      │         ├─ [observe] context.add_assistant_message(...)
                      │         ├─ [act] invoke_tool(...)         [invocation.py:49]
                      │         │    └─ tool.invoke(...)          ← 如 ReadFileTool
                      │         ├─ 终止判断 → mark_success / mark_failed
                      │         └─ bus.publish(StepFinishedEvent)
                      │
                      └─ bus.publish(RunFinishedEvent)
```

---

## 十一、文件加载顺序（import 链）

从 `kama run` 命令执行开始，按以下顺序加载模块：

```
1.  pyproject.toml                          ← 入口映射: kama → cli.main:main
2.  cli/__main__.py                         ← 调用 main()
3.  cli/main.py                             ← main()，导入 cmd_run
4.  cli/commands/run.py                     ← cmd_run，导入 StdoutPrinter + AgentRunner
5.  core/runner.py                          ← AgentRunner
    ├── core/bus/events.py                  ← 各事件模型
    ├── core/config.py                      ← KamaConfig
    ├── core/context.py                     ← ExecutionContext
    ├── core/events/bus.py                  ← EventBus
    ├── core/events/writer.py               ← EventWriter
    ├── core/llm/base.py                    ← LLMProvider 协议
    ├── core/llm/provider.py                ← AnthropicProvider
    ├── core/loop.py                        ← AgentLoop
    ├── core/runs.py                        ← new_run_id, RUNS_DIR
    └── core/tools/...                      ← ToolRegistry, invoke_tool, ReadFileTool
```

---

## 十二、与 `kama ping` 的对比

| | `kama ping` | `kama run` |
|---|---|---|
| 是否连 daemon | 是（TCP socket） | **否**（本地直接执行） |
| 网络通信 | `open_connection(host, port)` | 无（同进程内函数调用） |
| 涉及进程 | 2 个（CLI + daemon） | 1 个（仅 CLI） |
| 需要先启动 `kama-core` | 是 | **否** |
| 调用 LLM | 否 | 是（Anthropic API） |