# asyncio 与网络基础概念

本文档补充解释 kama-core 里涉及的几个基础概念：`asyncio` 异步模型、监听与连接的区别、信号处理与优雅关闭。与 `startup-flow.md`（启动流程）互为补充。

---

## 一、asyncio 是什么

`asyncio` 是 Python **标准库**中的一个模块，用来写**异步并发**程序。核心思想是「单线程 + 事件循环」，而不是「多线程」。

普通程序一行行顺序执行，遇到耗时操作（等网络、等文件 IO）就卡住干等。`asyncio` 让你在等待的时候**去做别的事**，等结果回来再回来处理。

### 1.1 三个核心概念

| 概念 | 写法 | 含义 |
|------|------|------|
| 协程 coroutine | `async def` | 可以暂停和恢复的函数 |
| 挂起 | `await` | 暂停当前协程，把控制权交回事件循环，等操作完成再继续 |
| 事件循环 event loop | `asyncio.run()` | 调度器，管理所有协程：谁在等、谁可以继续跑 |

### 1.2 用项目代码举例

```python
async def _read_loop(self, reader, writer):
    line = await reader.readline()   # 读一行，可能等很久
    await self._handle_line(line, writer)
```

当某个客户端连接在 `await reader.readline()` 等数据时，事件循环不会傻等，而是切去处理**其他客户端的连接**。这就是一个单线程 daemon 能同时服务多个 `kama` 客户端的原因。

### 1.3 对比多线程

| | 多线程 | asyncio |
|---|---|---|
| 并发单位 | 线程（操作系统调度） | 协程（程序自己调度） |
| 切换开销 | 较大（内核级） | 很小（用户态） |
| 适合场景 | CPU 密集 | **IO 密集**（网络、文件） |

kama-core 是网络 IPC 服务（等 socket 数据），属于 IO 密集，所以选 `asyncio` 很合适——不用为每个连接开线程，省资源、易管理。

### 1.4 在项目里的体现

- `app.py` 的 `asyncio.run(CoreApp().run())` — 启动事件循环
- `socket_server.py` 的 `await asyncio.start_server(...)` — 在事件循环里监听端口
- `app.py` 的 `await shutdown.wait()` — 挂起等退出信号，期间事件循环还能处理连接

---

## 二、监听 vs 连接

### 2.1 术语对照

| 动作 | 谁发起 | 对应 API |
|------|--------|----------|
| 监听 listen | 服务器 | `asyncio.start_server()` |
| 连接 connect | 客户端 | `asyncio.open_connection()` |

### 2.2 SocketServer 是「创建监听」，不是「连接」

`SocketServer(config.host, config.port)` 只是**实例化对象**（构造函数），只保存 host/port、初始化空 handler 字典、把 `_server` 置为 `None`，没有任何网络操作。

真正的监听发生在 `await server.start()`：

```python
self._server = await asyncio.start_server(
    self._handle_connection,
    host=self._host,
    port=self._port,
)
```

`asyncio.start_server(...)` 在**当前进程里新建一个监听 socket**，绑定到 `host:port` 并开始等待连接。底层就是操作系统层面的 `socket()` → `bind()` → `listen()`。绑定成功后，客户端只要连接 `host:port`，操作系统就把连接交给 `_handle_connection` 回调处理。

关键点：**这个 daemon 进程本身就是服务器**，不是去连接一个别人已经启动的服务器。

### 2.3 为什么不用标准库 socketserver

标准库的 `socketserver` 是**同步 / 多线程**模型。kama-core 没用它，而是自己写了 `SocketServer` 类，底层用 `asyncio` 的流式 API（`start_server` / `StreamReader` / `StreamWriter`）。

原因：整个项目是 `asyncio` 异步架构，用 asyncio 的流式 API 才能和 daemon 的 `async` 事件循环协同，不会像标准库那样为每个连接开线程。

### 2.4 注意 start() 里的「探活」是连接

```python
_r, w = await asyncio.open_connection(self._host, self._port)
```

这行是**主动发起一次出站连接**，但它只是「探活」——假装客户端去连一下，如果连上了说明已有别的进程在监听该端口，于是 `SystemExit` 退出；连不上（`ConnectionRefusedError`）才继续往下 `bind`。这是防多实例的检查，和「创建监听服务器」是两码事。

---

## 三、信号处理与优雅关闭

对应 `app.py` 里的代码：

```python
loop = asyncio.get_running_loop()
shutdown = asyncio.Event()
loop.add_signal_handler(signal.SIGINT, shutdown.set)
loop.add_signal_handler(signal.SIGTERM, shutdown.set)
```

### 3.1 逐行解释

```python
loop = asyncio.get_running_loop()
```
拿到**当前正在运行的事件循环**。`run()` 是 `async` 函数，被 `asyncio.run()` 调用时已经在事件循环里，所以是「获取」而不是「创建」。

```python
shutdown = asyncio.Event()
```
创建一个 asyncio 的**事件标志**，相当于一个可跨协程共享的「开关」。初始状态是「未设置」，后面用 `await shutdown.wait()` 卡在这里等它被触发。

```python
loop.add_signal_handler(signal.SIGINT, shutdown.set)
loop.add_signal_handler(signal.SIGTERM, shutdown.set)
```
把**操作系统信号**和 `shutdown.set()` 绑定：

| 信号 | 含义 | 谁触发 |
|------|------|--------|
| `SIGINT` | 中断 | 用户按 `Ctrl+C` |
| `SIGTERM` | 终止 | `kill <pid>`（默认信号） |

一旦收到这两个信号之一，就调用 `shutdown.set()` 打开事件开关。

### 3.2 完整流程

```
服务器启动完毕
  └─ await shutdown.wait()        ← 挂起，等 shutdown 被 set
       ↓ (收到 Ctrl+C / kill)
       ↓ shutdown.set()
       ↓
await server.stop()               ← 优雅关闭服务器
```

### 3.3 为什么用信号处理器而不是 try/except

如果直接 `asyncio.run()` 裸跑，`Ctrl+C` 会抛 `KeyboardInterrupt`，可能让连接处理到一半被粗暴打断。用 `add_signal_handler` 的好处是：**信号不会打断正在处理的协程**，而是通过事件标志通知，让程序在合适时机（当前连接处理完后）主动走关闭流程——这就是「优雅关闭」。

> 补充：`add_signal_handler` 只在主线程、且事件循环跑在 Unix 系（Linux/macOS）时才可用。Windows 上 asyncio 对信号支持有限，这也是这类 daemon 通常部署在 Linux 上的原因。
