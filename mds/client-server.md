# 客户端如何连接服务器（双进程架构）

本文档解释 kama 的客户端（`kama` / `kama-tui`）如何连接服务器（`kama-core`），以及命令与进程的对应关系。与 `startup-flow.md`（服务器启动流程）、`asyncio-concepts.md`（asyncio 与网络基础）互为补充。

---

## 一、命令与进程的对应关系

**不是所有命令都启动客户端。** 项目里命令分两类：

| 命令 | 角色 | 生命周期 |
|------|------|---------|
| `kama-core` | **服务器**（daemon） | 长期运行，一直监听端口 |
| `kama ping` | **客户端** | 一次性：连一下、发请求、收到回复、立刻退出 |
| `kama-tui` | **客户端**（TUI） | 交互式，S2+ 阶段才有 |

从代码能直接看出来：

**服务器**（`app.py`）—— 卡住不退出：

```python
await shutdown.wait()    # 一直等到收到退出信号
```

**客户端**（`ping.py`）—— 干完活就结束：

```python
def cmd_ping(config):
    asyncio.run(_ping(config))   # 跑完 _ping 就返回，进程退出
```

配合方式：

```
终端 A:  uv run kama-core        ← 启动服务器，一直挂着（监听 7437）
终端 B:  uv run kama ping        ← 启动客户端，连上服务器，发 ping，收到 pong，打印后退出
```

`kama ping` 能工作，前提是**已经有一个 `kama-core` 在运行**。否则客户端 `open_connection` 连不上，会报 `error: core not running` 并退出（`ping.py:18-20`）。

---

## 二、客户端是「连接 host + port」，不是「调用函数」

关键代码在 `ping.py:28`：

```python
reader, writer = await asyncio.open_connection(config.host, config.port)
```

### 2.1 为什么不能「调用函数」

**客户端（`kama`）和服务器（`kama-core`）是两个完全独立的进程**，各自有独立的内存。

服务器里的 `_ping_handler` 函数存在于 `kama-core` 进程里，客户端的 `kama` 进程根本「看不到」它，也没法直接调用它——就像没法直接调用另一个正在运行的程序里的函数。

所以跨进程通信只能走「**连接 + 传消息**」这条路：

1. 客户端用 `open_connection(host, port)` 建立 TCP 连接 —— 这就是「连接 host 和 port」。
2. 客户端把请求变成一段文字（JSON），通过连接**写过去**。
3. 服务器**读到**这段文字后，在自己进程内部解析、找到并调用 `_ping_handler`。
4. 服务器把结果再写成文字**传回来**。
5. 客户端**读到**结果。

**「调用函数」这件事确实发生了，但只发生在服务器进程内部**，由服务器自己执行，不是客户端跨进程调用的。

### 2.2 术语对照

| 动作 | 谁发起 | 对应 API |
|------|--------|----------|
| 监听 listen | 服务器 | `asyncio.start_server()` |
| 连接 connect | 客户端 | `asyncio.open_connection()` |

`host` 和 `port` 就是服务器的「电话号码」：`config.host`（默认 `127.0.0.1`）是本机地址，`config.port`（默认 `7437`）是端口号。服务器 `bind` 在这上面「等电话」，客户端 `open_connection` 用同一个号码「拨号」。

---

## 三、ping 命令完整流程

对照 `ping.py` 的 `_ping()`：

```python
# ① 连接：拨号到 host:port（TCP 三次握手）
reader, writer = await asyncio.open_connection(config.host, config.port)

# ② 构造请求内容（一段 JSON 文字）
req = {
    "jsonrpc": "2.0",
    "id": "cli-1",
    "method": "core.ping",          # 告诉服务器「我要调哪个方法」
    "params": {"client": "cli/0.0.1"},
}

# ③ 写过去：把 JSON 变成一行文字，末尾加 \n，通过 socket 发送
writer.write((json.dumps(req) + "\n").encode())
await writer.drain()

# ④ 读回来：等服务器处理完，把结果作为一行文字传回
line = await asyncio.wait_for(reader.readline(), timeout=10.0)

# ⑤ 关闭连接
writer.close()
await writer.wait_closed()
```

服务器那边（`socket_server.py`）收到 `method: "core.ping"` 后，才在自己内部执行：

```python
handler = self._handlers.get(req.method)   # 找到 "core.ping" 对应的函数
result = await handler(req.params)         # 在服务器进程内真正调用 _ping_handler
```

---

## 四、类比：打电话

| 网络通信 | 打电话 |
|---------|--------|
| `open_connection(host, port)` | 拨号到对方号码 |
| `writer.write(...)` 发请求 | 开口说话 |
| 服务器 `await handler(...)` 处理 | 对方听到后动脑想答案 |
| `reader.readline()` 读响应 | 听对方回答 |
| `writer.close()` | 挂电话 |

你「打给某人的号码」（host+port），然后通过「说话-听回答」（写-读）交流。**不能直接调用对方大脑里的思考函数**，只能靠通话传递信息。跨进程通信同理。

---

## 五、一句话总结

**命令 ≠ 都启动客户端**。`kama-core` 启动服务器（常驻），`kama ping` / `kama-tui` 启动客户端（连服务器的）。它们是两个进程，靠 TCP + JSON-RPC 通信。
