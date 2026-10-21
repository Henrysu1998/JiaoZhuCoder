# kama 多智能体与扩展能力调用链详解（S7：Subagents + Skills + MCP + 编排）

本文档梳理 S7 阶段的**主要实现逻辑**与**完整执行流程**。S7 相对 S6 保持双进程 IPC、会话、权限、压缩与分层记忆不变，新增了四类"把能力装进来"的机制：

1. **Subagents** —— `spawn_agent` / `agent_result` 工具，支持**冷启动上下文**的子 agent、前台阻塞与后台并行、两级嵌套。
2. **Agent Profiles** —— `planner` / `executor` / `reviewer` 三种角色配置（TOML），控制 system prompt 与工具白名单。
3. **Skills** —— Markdown 定义的可复用工作流，通过 `/name args` 触发，覆盖 system prompt 并收窄工具集；TUI 提供斜杠自动补全。
4. **MCP** —— 通过 stdio / TCP 连接外部 MCP server，自动发现工具并包装为 `BaseTool`，与内置工具共用注册表、权限与事件体系。

另有三处配套增强：**extended thinking blocks 原样回传**、**IPC 64MB 帧上限与连接关闭修复**、**TUI ASCII banner**。

> S4 版本（会话 + 记忆 + TUI 输入框）见 `run-flow-s4.md`；S5（权限审批）见 `run-flow-s5.md`；S6（分层记忆 + 压缩 + 流重试）见 `run-flow-s6.md`。

---

## 零、S6 vs S7 变化（先看这个）

| | S6（记忆 + 压缩 + 流重试） | S7（多智能体 + 扩展） |
|---|---|---|
| 执行单元 | 一个 session 内串行 run | **run 内可派生多个 child run（subagent）** |
| 子 agent 上下文 | 无 | **冷启动：只给 prompt，不继承父对话** |
| 并行 | 无 | **后台 subagent + `agent_result` 轮询** |
| 角色配置 | 无 | **`.toml` Agent Profile，可覆盖 system prompt / 工具白名单** |
| 斜杠命令 | 只有 `/compact`（客户端） | **任意 skill：`/review`、`/init`、`/summarize`、`/orchestrate`** |
| 外部工具 | 仅内置 9 个 | **MCP server 工具动态发现并注册** |
| system prompt | base / global / project / notes | **+ `system_prompt_override`（skill / profile 优先）** |
| 思考块 | 丢弃 | **`thinking` blocks 原样保留并回传** |
| IPC 帧上限 | 1 MB | **64 MB（兼容 MCP 大结果）** |
| 配置来源 | 全局 `~/.kama/config.toml` | **+ 项目本地 `.kama/config.toml` 分层覆盖** |
| TUI | 输入框 + 权限 | **banner + 斜杠补全 + subagent 树形渲染** |
| 新事件 | `context.compacted` | **`subagent.started` / `subagent.finished` / `skill.invoked`** |

一句话概括：**S6 让一个 agent 跑得更久，S7 让多个 agent 分工、让外部能力接进来、让用户用一行命令调用整套工作流。**

---

## 一、能力总览

```
                      ┌──────────────────────────────┐
                      │         Root AgentLoop        │
                      │  (session 的当前 run)          │
                      └──────────────┬───────────────┘
                                     │ tool calls
          ┌──────────────────────────┼──────────────────────────┐
          │                          │                          │
   spawn_agent                skill(/xxx)               MCP tools
   (subagent)                 (system prompt            (server__tool)
    ├─ foreground              override + whitelist)     ├─ stdio
    └─ background              └─ AgentLoop              └─ tcp
         │                          │
         ├─ planner profile         └─ 只保留白名单工具
         ├─ executor profile
         └─ reviewer profile
```

三种能力最终都汇入同一个执行内核：`AgentLoop + ToolRegistry + EventBus + PermissionManager`。S7 没有新增"第二种 agent 引擎"，只是**在 registry 和 context 上做组合**。

---

## 二、用户可见行为

| 能力 | 入口 | 表现 |
|---|---|---|
| 启动横幅 | TUI `on_mount` | ASCII `KAMACLAUDE` + 操作提示 |
| Skill 补全 | 输入 `/` | 弹出 `SlashCompleteWidget`，↑↓ 导航、Tab/Enter 选择、Esc 关闭 |
| Skill 调用 | 输入 `/review src/` + Enter | 发 `skill.invoked` 事件，日志显示 `/review src/` |
| Subagent | LLM 调 `spawn_agent` | 日志出现 `┌─ 描述 run_id` / `└─ ✓ 描述 3.2s` |
| 后台 subagent | `run_in_background: true` | 工具立即返回 `run_id`，之后用 `agent_result` 查询 |
| MCP | `config.toml` 的 `[mcp]` | daemon 启动时连接、发现工具，工具名 `server__tool` |

项目本地配置首次支持覆盖全局配置（`core/config.py:85-110`）：

```
内置默认 → ~/.kama/config.toml → <cwd>/.kama/config.toml → .env → 环境变量
```

若显式设置 `KAMA_CONFIG`，则**只读该文件**，不再叠加全局/项目两份。

---

## 三、Agent Profiles：角色配置

### 3.1 数据结构（`core/agents/loader.py:9`）

```python
@dataclass
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    model: str = ""
```

### 3.2 查找优先级（`loader.py:18`）

```python
[ ".kama/agents/<name>.toml",          # 项目本地
  "~/.kama/agents/<name>.toml",        # 用户全局
  <package>/agents/builtin/<name>.toml # 内建
]
```

`load()` 返回**第一个存在且可解析**的文件；解析异常返回 `None`（静默失败，不阻断 run）。

### 3.3 三个内建角色

| 角色 | 职责 | `allowed_tools` |
|---|---|---|
| `planner` | 分析目标并拆解为有序步骤，禁止修改 | `read_file`、`list_dir`、`task_create`、`task_update` |
| `executor` | 严格按计划执行并汇报 | `bash`、`read_file`、`write_file`、`list_dir`、`task_update`、`task_list` |
| `reviewer` | 核查产出是否达成目标 | `read_file`、`list_dir`、`bash` |

TOML 形态：

```toml
[agent]
description = "规划 agent：分析目标并拆解为有序子任务，禁止执行任何修改操作"
system_prompt = """
你是规划专家。...
"""
allowed_tools = ["read_file", "list_dir", "task_create", "task_update"]
model = "claude-sonnet-4-6"
```

`allowed_tools` 为空时表示**不限制**；非空时子 registry 只注册列出的工具。

---

## 四、Subagent：`spawn_agent` / `agent_result`

### 4.1 参数（`core/subagent/tool.py:40`）

```python
class SpawnAgentParams(BaseModel):
    description: str           # 3-5 词，TUI 进度展示
    prompt: str                # 子 agent 能看到的全部上下文
    run_in_background: bool = False
    subagent_type: str = ""    # planner / executor / reviewer，空=默认
```

工具描述里明确写了：**子 agent 从干净上下文启动，看不到父对话**，所以 `prompt` 必须自包含。

### 4.2 冷启动上下文（`tool.py:124-130`）

```python
child_run_id = new_run_id()
child_context = ExecutionContext(
    run_id=child_run_id,
    goal=p.prompt,
    max_steps=self._max_steps,
    system_prompt_override=profile.system_prompt if profile else None,
)
```

与父 run 的区别：

| 项 | 父 run | 子 run |
|---|---|---|
| messages 初始值 | `thread.jsonl` 全量回放 | **只有 `goal` 一条 user 消息** |
| global / project context | 注入 | **不注入** |
| session notes | 注入 | **不注入** |
| system prompt | base + 记忆层 | **profile override（若有），否则 base** |
| 工具 | 全部（含 note_save / MCP） | **文件 + 任务 + spawn/agent_result（按 profile 过滤）** |

这是刻意的"隔离"：子 agent 不会被父对话的长上下文污染，也拿不到父 session 的笔记。代价是调用方必须把必要信息完整写进 `prompt`。

### 4.3 事件桥接（`tool.py:132-138`）

```python
child_bus = EventBus()

async def _bridge(event: BaseModel) -> None:
    await self._parent_bus.publish(event)

child_bus.subscribe(_bridge)
```

子 agent 的所有 `run.* / step.* / tool.* / llm.*` 事件都会**转发到父 bus**，因此 TUI 能用嵌套缩进展示子 agent 的工具调用，而 `events.jsonl` 同时保留子 run 自己的完整日志。

### 4.4 前台 vs 后台（`tool.py:108-198`）

```python
if p.run_in_background:
    task = asyncio.create_task(self._run_background(...))
    self._task_registry.register(child_run_id, task, child_context)
    return ToolResult(content=f"Subagent started in background. run_id={child_run_id}. ...")

async with EventWriter(child_run_path / "events.jsonl") as writer:
    writer.subscribe(child_bus)
    await child_loop.run(child_context)
```

| | 前台 | 后台 |
|---|---|---|
| 工具何时返回 | 子 agent 完成后 | **立即**返回 `run_id` |
| 结果获取 | 直接作为 `tool_result` | `agent_result(run_id)` 轮询 |
| 失败语义 | `is_error=True` + `runtime_error` | 事件里 `status=failed`，查询时返回错误 |
| 适合场景 | 需要结果才能继续 | 并行探索 / 长任务 |

### 4.5 后台任务注册表（`subagent/registry.py:9`）

```python
class BackgroundTaskRegistry:
    _tasks: dict[str, tuple[asyncio.Task[None], ExecutionContext]]
    def register(run_id, task, context) -> None
    def get(run_id) -> tuple[...] | None
    def all() -> list[tuple[...]]
```

`AgentResultTool.invoke()`（`tool.py:305`）：

```
未知 run_id      → is_error=True（只能查后台任务）
task 未完成      → content="still running"（不是错误）
task 被取消      → is_error=True
task 抛异常      → is_error=True
task 完成        → context.result 或 "completed with no text result"
```

### 4.6 嵌套限制与子 registry（`tool.py:221-275`）

```python
if self._depth >= 2:
    return ToolResult("Subagent nesting limit (2) reached...", is_error=True)
...
if self._depth < 1:
    registry.register(SpawnAgentTool(..., depth=self._depth + 1))
    registry.register(AgentResultTool(...))
```

实际层级：

```
root agent
  └─ spawn (depth=0) → child agent            ← 第 1 层
        └─ spawn (depth=1) → grandchild agent ← 第 2 层
              └─ registry 里没有 spawn（depth<1 不成立）
```

子 registry 的组成：

| 工具组 | 是否注册 |
|---|---|
| `read_file` / `bash` / `write_file` / `list_dir` | 按 `allowed_tools` |
| 四个 task 工具（独立 `TaskManager`） | 按 `allowed_tools` |
| `spawn_agent` / `agent_result` | 仅 `depth < 1` 且白名单允许 |
| `note_save` | **不注册**（子 agent 无 session notes） |
| MCP 工具 | **不注册**（只有根 registry 注入） |

子 run 目录：`<session>/runs/<child_run_id>/`，与父 run 同级；事件文件独立。

---

## 五、Skills：Markdown 工作流

### 5.1 两种文件形态（`skills/loader.py:81`）

```
.kama/skills/review.md            ← 扁平文件
.kama/skills/review/SKILL.md      ← 目录式（S7 修复支持）
```

查找优先级：**项目本地 `.kama/skills` → `~/.kama/skills` → 内建**；同名时前面的覆盖后面的。

### 5.2 Frontmatter 解析（`skills/loader.py:20`）

```markdown
---
name: review
description: >
  对指定路径做代码审查，
  输出三级分类
allowed_tools:
  - read_file
  - list_dir
  - bash
---
你是一位严格的代码审查员。请审查：$ARGUMENTS
```

解析器支持：

- `name` / `description` 的引号与裸值；
- YAML 块标量 `>`（折叠为一行）与 `|`（保留换行）；
- `allowed_tools` 的 `- item` 列表；
- 无 frontmatter 时用文件名作 `name`，正文全部作为 system prompt。

### 5.3 触发链路（`core/session/manager.py:101-130`）

```python
if content.startswith("/"):
    parts = content[1:].split(None, 1)
    skill_name, arguments = parts[0], parts[1] if len(parts) > 1 else ""
    skill = self._skill_loader.resolve(skill_name)
    if skill is not None:
        goal = self._skill_loader.render_prompt(skill, arguments)  # $ARGUMENTS 替换
        system_prompt_override = skill.system_prompt_template
        tool_whitelist = skill.allowed_tools or None
        await self._bus.publish(SkillInvokedEvent(...))

runner.run_and_capture(goal, ..., system_prompt_override=..., tool_whitelist=...)
```

三件套：

| 产物 | 作用 |
|---|---|
| `system_prompt_override` | 整个 skill 正文替代 base system prompt（记忆层仍会追加） |
| `tool_whitelist` | 根 registry 只保留列出的工具 |
| `SkillInvokedEvent` | 前端展示 `/name args`，trace/events 可追溯 |

### 5.4 内建 Skills

| Skill | 用途 | 允许工具 |
|---|---|---|
| `init` | 分析项目并生成 `.kama/context.md` | `read_file`、`list_dir`、`write_file`、`bash` |
| `review` | 代码审查，输出严重/建议/可选 | `read_file`、`list_dir`、`bash` |
| `summarize` | 把当前对话总结进 `notes.md` | `note_save` |
| `orchestrate` | planner → executor → reviewer 三阶段 | `spawn_agent`、`agent_result`、`task_*` |

`/compact` 是例外：它在 **TUI 客户端**被拦截并转为 `session.compact`，不会进入 skill 解析，也不会作为用户消息发给模型。

### 5.5 TUI 斜杠补全（`tui/app.py:297-460`）

```
输入 "/"            → SlashCompleteWidget(items = 内建 compact + SkillLoader.list_all_skills())
继续输入 "re"       → 按名称子串过滤
↑ / ↓               → 移动光标
Tab / Enter         → 选中，输入框填入 "/review "
Esc                 → 收起弹窗
```

`ChatTextArea.on_text_area_changed()` 只在"以 `/` 开头且不含空格"时发 `SlashChanged`，避免正常输入文本时频繁弹窗。

---

## 六、Multi-agent 编排：`/orchestrate`

`orchestrate` 不是一个新引擎，而是**用 skill 固定住调用顺序**：

```
用户: /orchestrate 实现登录功能
  │
  ├─ SessionManager: system_prompt_override = orchestrate 正文
  │                  tool_whitelist = [spawn_agent, agent_result, task_*]
  │
  └─ Root AgentLoop（只能编排，不能直接读写文件）
       ├─ spawn_agent(subagent_type="planner",
       │              prompt="目标 + 要求输出有序步骤")
       │     └─ 前台等待 → planner 的完整计划作为 tool_result
       │
       ├─ spawn_agent(subagent_type="executor",
       │              prompt="原始目标 + planner 计划")
       │     └─ 前台等待 → executor 的执行汇报
       │
       └─ spawn_agent(subagent_type="reviewer",
                      prompt="原始目标 + executor 汇报")
             └─ 前台等待 → reviewer 的审查结论

最终由 root agent 汇总：计划摘要 + 执行摘要 + 审查结论 + 是否成功
```

关键设计：**root 只负责协调**，工具白名单里没有 `read_file` / `write_file` / `bash`，避免协调者越权直接改代码；每个子 agent 按 profile 拿到各自的最小工具集。

---

## 七、MCP：外部工具接入

### 7.1 配置（`core/config.py:60`）

```toml
[mcp]
servers = [
  { name = "filesystem", transport = "stdio",
    command = "npx", args = ["-y", "@modelcontextprotocol/server-filesystem", "."],
    env = { DEBUG = "0" } },
  { name = "remote", transport = "tcp", host = "127.0.0.1", port = 3000 },
]
```

| 字段 | 说明 |
|---|---|
| `name` | 必填，工具名前缀 |
| `transport` | `stdio` / `tcp` |
| `command` / `args` / `env` | stdio 专用 |
| `host` / `port` | tcp 专用 |

### 7.2 daemon 生命周期（`core/mcp/server.py:14`）

```python
# 启动：在 CoreApp.run() 中、server.start() 之前
self._mcp_manager = McpServerManager()
await self._mcp_manager.start_all(self._config.mcp.servers)

# 关闭：在 CoreApp.run() 的 shutdown 分支
await self._mcp_manager.stop_all()
```

`start_all()` **逐个连接、逐个失败跳过**：某个 server 起不来只记日志，不影响 daemon 与其他 server。

### 7.3 协议与稳定性（`core/mcp/client.py`）

```python
connect_stdio/connect_tcp
  └─ initialize（protocolVersion 2024-11-05）
       └─ notifications/initialized
            └─ tools/list → tools/call
```

关键实现：

- `_call()` 用 `asyncio.Lock` **串行化同一 server 的请求**，按 `id`（字符串化比较）匹配响应；
- 忽略服务端主动通知（`id is None`）；
- `_read_line()` 有 **30s 超时**，EOF 才判定断连，空行跳过；
- 流上限 **64MB**，避免大文件结果触发 `LimitOverrunError`；
- stdio 的 stderr 由后台任务持续读走，防止管道写满导致子进程阻塞；
- `close()` 先取消 stderr 任务，再 `terminate` → 5s → `kill`。

### 7.4 工具包装（`core/mcp/tool.py:10`）

```python
self.name = f"{server_name}__{tool_def.name}"     # 防命名冲突
self.input_schema = tool_def.input_schema or {"type": "object", "properties": {}}
params_model = None                                # 不做 pydantic 二次校验
```

调用结果：

| 情况 | 返回 |
|---|---|
| 成功 | 所有 `type == "text"` 内容用换行拼接 |
| server 不可用 | `is_error=True`，`runtime_error` |
| 工具应用层错误（`McpToolError`） | `is_error=True`，`runtime_error` |
| 其他异常 | `is_error=True`，`runtime_error` |

### 7.5 注册位置（`core/runner.py:80-137`）

```python
if self._mcp_manager is not None:
    for mcp_tool in self._mcp_manager.get_tools():
        if _ok(mcp_tool.name):
            registry.register(mcp_tool)
```

MCP 工具**只注入根 registry**，子 agent 的 registry 不包含它们（见第四节）。

---

## 八、Extended thinking blocks

Claude 的 extended thinking 会在响应里返回 `thinking` block，并带有 `signature`。多轮工具调用时，这些 block 必须**原样、按顺序**回传，否则 API 会拒绝。

### 8.1 提取（`core/llm/provider.py:143-157`）

```python
elif block.type == "thinking":
    thinking_blocks.append({
        "type": "thinking",
        "thinking": block.thinking,
        "signature": block.signature,
    })
```

### 8.2 回填（`core/loop.py:82`）

```python
# thinking blocks must come first and be preserved verbatim
blocks: list[dict[str, object]] = list(response.thinking_blocks)
if response.text:
    blocks.append({"type": "text", "text": response.text})
for tc in response.tool_calls:
    blocks.append({"type": "tool_use", ...})
```

顺序固定为 **thinking → text → tool_use**，与 Anthropic 的要求一致。

---

## 九、上下文与 registry 装配

### 9.1 system prompt 覆盖（`core/context.py:22,33`）

```python
system_prompt_override: str | None = None

parts = [self.system_prompt_override if self.system_prompt_override else base]
# 之后仍追加 Global / Project / Session Notes
```

优先级：**override（skill/profile）> base**；记忆层永远追加，不会被覆盖丢弃。

### 9.2 根 registry 组成（`core/runner.py:80-137`）

| 来源 | 条件 |
|---|---|
| 4 个文件工具 + 4 个 task 工具 + `note_save` | 按 `tool_whitelist` |
| `spawn_agent` / `agent_result` | 有 provider/bus/run_id，且白名单允许 |
| MCP tools | `mcp_manager` 已启动 |

### 9.3 `AgentRunner` 新增参数

```python
run_and_capture(
    ...,
    system_prompt_override: str | None = None,
    tool_whitelist: list[str] | None = None,
)
```

`SessionManager` 在 skill 解析后把两者传进来；普通消息两者均为 `None`，行为与 S6 完全一致。

---

## 十、配置与传输层变化

### 10.1 配置分层

```
~/.kama/config.toml      ← 全局
<cwd>/.kama/config.toml  ← 项目本地（后应用，覆盖同名键）
```

新增 `[mcp]` 小节；其余配置（permission / compaction / trace / llm）沿用。

### 10.2 IPC 稳定性修复

| 位置 | 变化 | 原因 |
|---|---|---|
| `socket_client.py:13` | 帧上限 1MB → **64MB** | MCP 大文件结果 |
| `socket_client.py:69` | 捕获 `ValueError` / `LimitOverrunError` 后继续读 | 单行超限不再杀死整个事件循环 |
| `socket_server.py:44` | 帧上限 1MB → **64MB** | 同上 |
| `socket_server.py:61,89,108` | 记录活跃 writer，`stop()` 时主动关闭 | 防止停 daemon 时被客户端连接卡住 |
| `socket_server.py:112` | 断连时不再 `await writer.wait_closed()` | 避免异常连接拖慢清理 |

---

## 十一、TUI 渲染

### 11.1 启动 banner（`tui/app.py:494`）

`self._BANNER` 是六行 ASCII `KAMACLAUDE` + 一行提示，`on_mount()` 时作为 `Static(id="banner")` 追加到 `#log-view` 顶部。

### 11.2 Skill 事件

```python
elif t == "skill.invoked":
    self._append(Static(f"[bold cyan]/{skill_name}[/bold cyan]{args_part}"))
```

### 11.3 Subagent 树形渲染（`tui/app.py:897-935`）

```
┌─ 分析代码 abc12345
    tool read_file ...        ← 子 agent 的工具块缩进 6 格
└─ ✓ 分析代码 3.2s
```

实现要点：

- `subagent.started` 记录 `run_id → description / start_time`；
- `tool.call_started` 时若 `run_id` 属于子 agent，则设置 `padding = (0,2,0,6)`；
- 子 agent 的 `step.started` / `llm.usage` 被**抑制**，避免刷屏；
- `subagent.finished` 输出 `✓/✗ + 描述 + 耗时`。

订阅 topic 新增 `subagent.*` 与 `skill.*`（`tui/app.py:798-800`）。

---

## 十二、完整执行流程

### 12.1 前台 subagent（planner 示例）

```
父 AgentLoop
  └─ tool_use(spawn_agent{description, prompt, subagent_type:"planner"})
       └─ SpawnAgentTool.invoke
            ├─ depth 检查（>=2 拒绝）
            ├─ profile = AgentProfileLoader.load("planner")
            ├─ child_context = ExecutionContext(goal=prompt,
            │       system_prompt_override=profile.system_prompt)
            ├─ child_bus + bridge → parent_bus
            ├─ child_registry = 文件/任务工具（按 allowed_tools）+ spawn/agent_result
            ├─ publish subagent.started
            ├─ EventWriter(<session>/runs/<child>/events.jsonl)
            ├─ await child_loop.run(child_context)
            │     └─ LLM 调用 / 工具调用（事件同时进 child_bus → parent_bus）
            ├─ publish subagent.finished(status)
            └─ return ToolResult(child_context.result)
       └─ 父 agent 收到 planner 计划，继续下一个 tool_use
```

### 12.2 后台 subagent

```
spawn_agent(run_in_background=true)
  ├─ create_task(_run_background)
  ├─ registry.register(child_run_id, task, context)
  └─ 立即返回 "Subagent started in background. run_id=..."

（子 agent 在后台继续跑，事件照常桥接到父 bus）

父 agent 稍后:
  agent_result(run_id)
    ├─ 未完成 → "still running"
    ├─ 完成   → context.result
    └─ 失败   → is_error=True
```

### 12.3 Skill 调用（`/review src/`）

```
TUI 输入 "/review src/" + Enter
  ├─ 输入框检查 "/compact"? 否
  └─ send_command("session.send_message", {content:"/review src/"})
       ▼
SessionManager.send_message
  ├─ thread.jsonl 追加 user: "/review src/"     ← 原始斜杠命令
  ├─ SkillLoader.resolve("review")
  ├─ goal = 模板.replace("$ARGUMENTS", "src/")
  ├─ system_prompt_override = 模板原文
  ├─ tool_whitelist = [read_file, list_dir, bash]
  ├─ publish skill.invoked
  └─ runner.run_and_capture(goal, override, whitelist)
       ├─ messages = thread 回放（含 "/review src/"）
       ├─ system = override + global/project/notes
       ├─ registry = 仅白名单工具
       └─ AgentLoop.run()
```

### 12.4 MCP 工具调用

```
daemon 启动
  └─ McpServerManager.start_all
       ├─ connect_stdio / connect_tcp
       ├─ initialize + notifications/initialized
       ├─ tools/list → [McpToolDef...]
       └─ 为每个工具构造 McpTool("server__tool")

run 开始
  └─ runner._build_registry
       └─ mcp_manager.get_tools() → registry.register(...)

LLM 调 tool_use("filesystem__read_file")
  └─ invoke_tool → 权限检查 → McpTool.invoke
       └─ McpClient.call_tool → JSON-RPC tools/call
            └─ 拼接 text 内容 → ToolResult
```

---

## 十三、落盘布局

```
<project>/
  ├── .kama/config.toml          ← 项目本地配置（S7 新增叠加）
  ├── .kama/context.md           ← 项目记忆（S6）
  ├── .kama/agents/*.toml        ← 项目本地角色（覆盖全局/内建）
  └── .kama/skills/*.md          ← 项目本地 skill

~/.kama/
  ├── config.toml
  ├── agents/*.toml              ← 用户全局角色
  ├── skills/**/SKILL.md         ← 用户全局 skill
  └── sessions/<sess-id>/
        ├── thread.jsonl
        ├── notes.md
        └── runs/
              ├── <parent-run>/events.jsonl
              └── <child-run>/events.jsonl    ← subagent 独立 run
```

---

## 十四、文件改动清单（S7）

| 类别 | 文件 | 内容 |
|---|---|---|
| 新增（角色） | `core/agents/loader.py`、`builtin/*.toml` | AgentProfile + 三级查找 + 三种角色 |
| 新增（子 agent） | `core/subagent/tool.py` | `spawn_agent` / `agent_result` |
| | `core/subagent/registry.py` | 后台任务注册表 |
| 新增（Skills） | `core/skills/loader.py`、`builtin/*.md` | frontmatter 解析、三级查找、四个内建 skill |
| 新增（MCP） | `core/mcp/client.py` | stdio/tcp JSON-RPC 客户端 |
| | `core/mcp/server.py` | 生命周期与工具发现 |
| | `core/mcp/tool.py` | `BaseTool` 包装 |
| 修改（执行） | `core/runner.py` | 注入 spawn/agent_result/MCP、tool_whitelist、override |
| | `core/context.py` | `system_prompt_override` |
| | `core/loop.py` | thinking blocks 前置 |
| | `core/llm/provider.py`、`types.py` | thinking 提取与返回 |
| 修改（会话） | `core/session/manager.py` | `/skill` 解析、`SkillInvokedEvent` |
| 修改（协议） | `core/bus/events.py` | SubagentStarted/Finished、SkillInvoked |
| 修改（daemon） | `core/app.py` | MCP manager 启动/关闭、注入 runner |
| | `core/config.py` | 项目本地 config 叠加 + `[mcp]` |
| 修改（传输） | `socket_client.py` / `socket_server.py` | 64MB 帧、关闭与超限修复 |
| 修改（前端） | `tui/app.py` | banner、斜杠补全、skill/subagent 渲染 |

测试：

- `tests/unit/test_agent_profile_loader.py` —— 内建角色、未知角色、TOML 解析、项目覆盖。
- `tests/unit/test_spawn_agent_tool.py` —— 前台/后台、`agent_result` 三态、嵌套限制、started 事件。
- `tests/unit/test_skill_loader.py` —— 四个内建 skill、`$ARGUMENTS`、frontmatter、项目覆盖。
- `tests/unit/test_mcp_tool.py` —— 名称前缀、schema 透传、三类错误包装。
- `tests/unit/test_session_manager.py` —— 新增 `SkillInvokedEvent` 断言。

---

## 十五、关键设计要点

1. **子 agent 是"冷启动"而不是"fork"**：只传 prompt，不继承父对话、记忆层与 note_save，保证隔离与可预测性；代价是调用方必须写清上下文。
2. **子 agent 复用同一套执行内核**：`AgentLoop + ToolRegistry + EventBus + PermissionManager`，没有第二套调度器。
3. **事件桥接而非日志拼接**：child bus 的每条事件都转发到 parent bus，TUI 能实时看到嵌套进度，trace 也能按 run_id 区分。
4. **前台/后台只差调度方式**：前台 `await child_loop.run()`，后台 `create_task` + 注册表；子 agent 内部逻辑完全一致。
5. **嵌套靠 registry 控制**：`depth < 1` 才注册下一层 spawn，形成自然的层级上限；`depth >= 2` 是防御性守卫。
6. **角色即最小权限**：profile 的 `allowed_tools` 直接裁剪 registry，planner 拿不到 bash，reviewer 拿不到 write_file。
7. **Skill 是"提示词 + 工具集"的组合**：`system_prompt_override` 定义行为，`tool_whitelist` 定义边界，`$ARGUMENTS` 提供参数。
8. **优先级自上而下**：项目本地 > 用户全局 > 内建，agent profile 与 skill 一致，方便项目内覆盖。
9. **MCP 是工具来源，不是执行路径**：发现后包装成 `BaseTool`，因此自动获得权限审批、失败分类、重试与事件。
10. **MCP 连接生命周期归 daemon**：启动时连接一次、所有 run 共享；`stop_all()` 统一终止子进程。
11. **思考块必须逐字回传**：`thinking → text → tool_use` 的顺序与 `signature` 是 Anthropic 多轮工具调用的硬约束。
12. **IPC 为大数据让路**：64MB 帧上限 + 超限后继续读，避免一个 MCP 大结果打挂整个客户端事件循环。
13. **TUI 用视觉层级表达多 agent**：`┌─/└─` + 缩进 + 抑制子 agent 的 step/usage，避免嵌套事件淹没有效信息。

---

## 十六、边界情况与当前实现现状

以下是代码当前的真实行为，排查问题时很有用。

### 16.1 后台 subagent 的注册表并不跨 run 共享

`AgentRunner.__init__` 里注释写着"跨 run 共享的后台 subagent 任务注册表"，但：

```python
self._task_registry = BackgroundTaskRegistry()   # 每个 AgentRunner 一个
```

而 `SessionManager.send_message()` 每次都调用 `self._runner_factory()` 创建**新的** `AgentRunner`。因此：

- 同一个 run 内（含嵌套子 agent）`agent_result` 可以查到后台任务；
- **下一轮对话再查同一个 run_id 会得到 `Unknown run_id`**；
- 后台任务若在父 run 结束后仍在跑，其结果无法通过后续消息取回。

### 16.2 daemon 退出不会批量取消后台 subagent

`BackgroundTaskRegistry.all()` 的注释写着"用于 daemon 退出时批量清理"，但全仓没有调用点；`CoreApp` 只取消 `_running_runs`（`agent.run` 路径）。daemon 关闭时后台子 agent 任务可能被直接丢弃。

### 16.3 子 agent 拿不到记忆层与部分工具

- 不注入 global / project context 与 session notes；
- 没有 `note_save`；
- 没有 MCP 工具；
- `profile.model` 被解析但**从未使用**，子 agent 始终复用父 provider 的模型。

### 16.4 会话路径下 skill 渲染出的 `goal` 实际不进入 messages

`send_message()` 先把**原始** `/review src/` 写进 `thread.jsonl`（`manager.py:88`），再计算渲染后的 `goal`。而 `AgentRunner.run_and_capture()` 读取的 `history` 已包含这条原始消息，`ExecutionContext.__post_init__` 优先使用 `prefill_messages`，因此：

- 模型看到的是 `"/review src/"` 这条 user 消息；
- `system_prompt_override` 是**未替换** `$ARGUMENTS` 的模板原文；
- `render_prompt()` 生成的文本只在"无 prefill 的裸 run"里才会作为 goal 生效。

功能上 skill 仍然能用（正文在 system prompt，参数在 user 消息），但 `$ARGUMENTS` 占位符不会被替换。

### 16.5 未知 skill 静默退化为普通消息

`resolve()` 返回 `None` 时不会报错、不会发 `skill.invoked`，`/unknown xxx` 会原样发给模型。

### 16.6 子 agent 工具默认都要审批

`spawn_agent` / `agent_result` / MCP 工具都不在 `DEFAULT_POLICIES` 中，按 S5 的兜底规则是 **ASK**。因此默认配置下：

- 每次 spawn 都会弹权限审批；
- 每次 MCP 工具调用也会弹审批。

用 `always_allow` 或 `policy.toml` 可以避免重复打扰。

### 16.7 MCP 的应用层错误会被重试

`McpTool.invoke()` 把所有 `McpToolError`（连接正常、工具返回 error）都映射为 `runtime_error`，而 `runtime_error` 属于 S5 的可重试分类。于是"参数错误"这类确定性失败也会被重试 2 次（共 3 次），可能重复产生副作用。

### 16.8 `WIRE_PROTOCOL.md` 未收录 S7 新事件

S7 只重新生成了 `LlmUsageEvent.context_pct` 的 6 行变更；`subagent.started`、`subagent.finished`、`skill.invoked` 三个新事件在 `WIRE_PROTOCOL.md` 中**没有对应章节**（`core/bus/events.py` 里已加入 Event 联合）。

### 16.9 未使用的入口

- `McpServerManager.register_tools()` 没有调用点（runner 改用 `get_tools()` 逐工具注册）；
- `BackgroundTaskRegistry.all()` 没有调用点；
- `AgentProfile.model` 没有使用点。

---

## 十七、与 S4 / S5 / S6 文档的对照阅读

| 主题 | S4 | S5 | S6 | 本文档 |
|---|---|---|---|---|
| 双进程 / 双通道 | 第五、十四节 | 第六节 | 不变 | 第十节（传输修复） |
| 会话状态机 | 第四节 | 不变 | 不变 | 不变（skill 在 send_message 内解析） |
| 工具调用 | 第十节 | 第八、九节 | 不变 | 第四、七、九节（新增工具来源） |
| 权限审批 | 无 | 第二至七节 | 不变 | 新增工具同样走权限 |
| 分层记忆 | 第七节 | 不变 | 第二节 | 子 agent 不继承记忆层 |
| 上下文压缩 | 无 | 无 | 第四、五节 | 不变 |
| TUI | 第十二节 | 第十一节 | 第九节 | 第十一节（banner/补全/subagent） |
| IPC 契约 | 第十三节 | 第五节 | 第八节 | 第十节（事件新增，协议文档未同步） |
| Subagents | 无 | 无 | 无 | 第四、六节（新增） |
| Skills | 无 | 无 | 无 | 第五、六节（新增） |
| MCP | 无 | 无 | 无 | 第七节（新增） |
| Extended thinking | 无 | 无 | 无 | 第八节（新增） |
