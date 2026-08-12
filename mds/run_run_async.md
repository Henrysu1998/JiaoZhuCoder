# `_run_async` 知识点详解

> 对应源文件：`src/kama_claude/cli/commands/run.py` → `_run_async(goal, config)`
>
> 函数职责：一次完整对话生命周期——连接 daemon → 订阅事件 → 触发 run → 等 `run.finished` → 清理退出。

---

## 源码 + 注释

```python
# 一次完整对话生命周期：连接 daemon → 订阅事件 → 触发 run → 等 run.finished → 清理退出
async def _run_async(goal: str, config: KamaConfig) -> int:
    # 第一步：连接 kama-core 守护进程
    # TCP 客户端，目标地址即守护进程监听的 host:port（默认 127.0.0.1:7437）
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        # 连接被拒绝或端口未监听 → 守护进程没启动或已崩溃
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    # 第二步：准备事件处理回调
    # StdoutPrinter 负责将守护进程推送的事件（token、工具调用、步骤进度等）格式化打印到终端
    printer = StdoutPrinter()
    # asyncio.Event 是协程世界的同步工具：当前协程会在 finished.wait() 处挂起，
    # 直到守护进程发来 run.finished 事件后调用 finished.set() 才被唤醒
    finished = asyncio.Event()
    exit_code = 0

    # 事件回调：守护进程每推送一个事件，就调用这个函数
    # 它运行在后台 loop_task 协程中，不是当前协程
    async def on_event(event: dict[str, Any]) -> None:
        nonlocal exit_code
        await printer.handle(event)  # 格式化输出事件到终端
        if event.get("type") == "run.finished":
            # 守护进程通知：本轮 agent run 已完成（成功、失败、或用户取消）
            if event.get("status") != "success":
                exit_code = 1  # 非成功状态记为退出码 1
            finished.set()    # 唤醒 await finished.wait() 的当前协程

    # 注册回调，但不直接调用 run_event_loop()——它是死循环，会阻塞当前协程
    client.on_event(on_event)
    # 第三步：启动后台 task 持续读取 socket 上的推送事件（NDJSON 行）
    # 这里形成了两条通道：
    #   命令通路 — send_command() 一发一收，同步等响应（当前协程）
    #   事件通路 — run_event_loop() 死循环读 socket，收到事件调 callback（后台 task）
    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        # 第四步：先订阅 topic，再触发 agent.run
        # 顺序不能反——先订阅再触发，否则可能漏掉守护进程在订阅之前就发出的事件
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["run.*", "step.*", "tool.*", "llm.token", "llm.usage"],
                "scope": "global",
            },
        )
        # 告诉守护进程：开始执行这个 goal
        # send_command 走命令通路：发 JSON-RPC 请求，等响应回来才继续
        # 守护进程收到后就开始跑 agent 循环，并通过事件通路持续推送进度
        await client.send_command("agent.run", {"goal": goal})
    except IpcError as e:
        # 命令层面出错（topic 非法、goal 为空等）→ 直接清理退出
        print(f"error: {e}", file=sys.stderr)
        loop_task.cancel()
        await client.close()
        return 1

    # 第五步：挂起当前协程，等待守护进程发来 run.finished
    # 这里 await 不占 CPU，事件循环继续调度后台 loop_task 读消息、调 on_event
    await finished.wait()

    # agent.run 已完成 → 取消后台消息读取 task，做清理
    loop_task.cancel()
    try:
        # 必须 await 被取消的 task，让它在抛出 CancelledError 后正常结束
        await loop_task
    except asyncio.CancelledError:
        pass  # CancelledError 是预期内的，吞掉即可

    # 关闭 TCP 连接，释放底层 socket
    await client.close()
    return exit_code
```

---

## 涉及的核心概念

### 1. `async def` — 协程函数

用 `async def` 定义的函数叫**协程函数**。调用它不会执行函数体，而是返回一个**协程对象**——可以理解成"还没开始跑的任务说明书"，你得用 `await` 或 `create_task` 才能真正启动它。

```python
async def _run_async(goal, config):  # 这是一个协程函数
    ...

# 调用它返回协程对象，不会执行函数体
coro = _run_async("test", config)  # 什么都没做，只是个"待执行"的包装

# 必须 await 或 create_task 才能启动
await coro                           # 方式一：直接 await
asyncio.create_task(coro)            # 方式二：扔给事件循环后台跑
```

### 2. `await` — 挂起 + 让路

```python
await client.connect()         # 等连接完成
await client.send_command(...)  # 等命令响应回来
await finished.wait()          # 等别人通知我"结束了"
await loop_task                # 等后台 task 退出
```

`await` 做两件事：
- **挂起**当前协程："我先不动了，等右边那个东西有结果再说"
- **让路**给事件循环："这段时间你去跑别的协程，别浪费着等我"

核心类比：

```
传统线程（阻塞 IO）    → 线程傻等在电话旁边，什么也不干
异步（非阻塞 IO + await）→ 有人排队等电话，但可以帮别人结账，电话响了再回来
```

这就是 Python 异步的**单线程并发**：不是多线程同时跑，而是一个线程在多个协程之间来回切换——**"谁等 IO 谁就让路"**。

### 3. `asyncio.Event()` — 协程间的"信号灯"

```python
finished = asyncio.Event()    # 灯初始灭
await finished.wait()          # 等灯亮（协程挂起）
finished.set()                 # 点亮灯（唤醒等待者）
```

类比：两个人在协调——A 说"我在这儿等着"，B 到了终点挥旗说"走"。旗子就是 `Event`，挥旗就是 `set()`，看到旗号继续跑就是 `wait()` 返回。

对比线程世界的同类工具：

| 任务世界 | 示例 |
|---------|------|
| 线程 | `threading.Event`, `threading.Lock`, `threading.Semaphore` |
| 协程 | `asyncio.Event`, `asyncio.Lock`, `asyncio.Semaphore` |

核心区别在于"等"的方式不同：线程的 `wait()` 把线程卡住不动了；异步的 `await ...wait()` 只是把当前协程挂起，事件循环继续跑其他协程，**不占线程，不浪费资源**。

在这个函数里，`finished` 是"命令通路"和"事件通路"之间的同步点：

```
事件通路（后台 task）         命令通路（当前协程）
收到 run.finished
  → finished.set() ──────→  唤醒 await finished.wait()
                               ↓
                            继续清理、关闭连接、返回退出码
```

### 4. `asyncio.create_task()` — 提交后台任务

```python
loop_task = asyncio.create_task(client.run_event_loop())
```

Task 类比：就像餐厅的**点餐小票**——你把菜名（协程）告诉服务员（事件循环），服务员拿走去后厨做，返给你一张小票。你可以凭小票**取消订单**（`task.cancel()`）或**在座位上等菜做好**（`await task`）。小票本身不干活，它只是跟踪后厨进度的"凭证"。

为什么必须用 `create_task` 而不能直接 `await`：

```python
# 错误写法
await client.run_event_loop()  # run_event_loop 是死循环，这辈子都不会返回
# → 后续的 send_command("agent.run") 永远执行不到

# 正确写法
loop_task = asyncio.create_task(client.run_event_loop())  # 返回 Task 句柄，立即继续
# → 死循环在后台跑，当前协程继续往下 send_command
```

这形成了这个函数最核心的设计——**双通道**：

| 通道 | 方向 | 实现 | 特点 |
|------|------|------|------|
| 命令通路 | 客户端 → 守护 → 客户端 | `send_command()` 一发一收 | 同步等待响应，短、快 |
| 事件通路 | 守护 → 客户端（推送） | 后台 task 死循环读 socket | 持续推送，长、流式 |

### 5. 回调模式 — "有事就调我"

```python
client.on_event(on_event)   # 注册回调
```

把函数当参数传进去，告诉对方"有事件来就调这个函数"。这是一种**控制反转**——你不用自己轮询"有新消息吗"，新消息来了框架会自动调你。

注意这里的命名巧合：`client.on_event` 是 `SocketClient` 的方法，`on_event` 是上面定义的协程函数。它们恰好同名，但不是一个东西：

```python
# SocketClient 的方法定义
def on_event(self, handler: EventHandler) -> None:
    self._event_handlers.append(handler)  # 把回调存起来，以后有事件就调

# 类型定义
type EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
# 即：接收一个 dict、返回一个可 await 的东西的可调用对象
```

`on_event` 回调运行在**后台 loop_task 协程**中，不是 `_run_async` 主协程中。所以需要通过 `asyncio.Event` 跨协程通信。

### 6. Task 生命周期管理 — 创建 → 取消 → 等退出

```python
# 创建
loop_task = asyncio.create_task(...)

# 取消（向 task 内部的 await 点注入 CancelledError）
loop_task.cancel()

# 等 task 真正退出
try:
    await loop_task
except asyncio.CancelledError:
    pass  # cancel 抛出的异常，预期内，吞掉即可
```

关键点：
- `cancel()` 不是暴力杀协程，而是在协程内部**下一个 `await` 点**抛 `CancelledError`
- cancel 后**必须 await**，否则 task 可能还没清理完（socket 没关、资源没释放）
- `CancelledError` 是正常关闭方式，不是 bug，所以 catch 并忽略

### 7. `nonlocal` — 修改外层变量

```python
async def on_event(event):
    nonlocal exit_code    # 声明：我要改外层的 exit_code，不是创建本地变量
    ...
    exit_code = 1         # 如果不加 nonlocal，这行会在 on_event 内部创建新局部变量
```

`nonlocal` 让嵌套函数可以修改外层函数（但不是全局）的变量。没有它的话，`exit_code = 1` 只会在 `on_event` 内部创建一个新的局部变量，外层 `exit_code` 不变，最终返回的还是 `0`。

---

## 整体心智模型

把事件循环想象成**一个收银员**，协程是排队办事的人：

```
事件循环（单线程，就一个人）

  ┌─ 顾客 _run_async：
  │    "连接 daemon（await connect → 让位）"
  │    "创建后台 task（create_task → 让位）"
  │    "发订阅命令（await send_command → 让位）"
  │    "发 agent.run（await send_command → 让位）"
  │    "等 run.finished 信号（await finished.wait → 让位，这一次等很久）"
  │    "信号来了 → 取消后台 task → 关闭连接 → 返回退出码"
  │
  ├─ 顾客 loop_task (后台)：
  │    "死循环读 socket（await readline → 让位）"
  │    "收到事件 → 调 on_event 回调"
  │    "继续读下一行..."
  │    （直到被 cancel）
  │
  └─ 回调 on_event（在 loop_task 里被调用）：
       "打印事件到终端"
       "如果是 run.finished → finished.set()，唤醒 _run_async"
```

一个收银员同一时刻只能服务一个人。但只要有人开始等 IO（`await`），收银员就转身去帮另一个。所有人都在**等 IO 时主动让路**，谁也不占着收银员不放。所以一个线程就能撑起整个通信服务。

这也是为什么异步特别适合 IO 密集型场景——大部分时间都在等网络、等磁盘，CPU 实际干活的时间很少，一个线程绰绰有余。

---

## 概念速查表

| 概念 | 一句话 | 在本函数中的出现 |
|------|--------|-----------------|
| `async def` | 定义协程函数，调用返回协程对象 | `_run_async`, `on_event` |
| `await` | 挂起当前协程，让事件循环去跑别人 | 连接、发命令、等事件、等 task |
| `asyncio.Event` | 协程间的旗语，一方等一方通知 | `finished` 同步两个通路 |
| `asyncio.create_task` | 把协程交给事件循环后台跑，返回 Task 句柄 | 启动 `run_event_loop` |
| Task 对象 | 后台协程的"跟踪凭证"，可取消、可等待 | `loop_task` |
| `task.cancel()` | 向 task 注入 CancelledError，在它的下一个 await 点抛出 | 清理后台 task |
| `CancelledError` | cancel 的预期产物，catch 并忽略 | 末尾的 `except` |
| 回调模式 | 把函数当参数传，对方有事就调 | `client.on_event(on_event)` |
| `nonlocal` | 嵌套函数修改外层变量 | `exit_code` |
| `EventHandler` | 类型别名，约束回调签名为 `(dict) → Awaitable[None]` | `on_event` 的隐式类型 |
