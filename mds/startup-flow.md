# kama-core 启动流程详解

本文档梳理从执行 `kama-core` 命令到服务就绪的完整启动链路，包括文件加载顺序和方法调用顺序。

---

## 一、入口映射（pyproject.toml → Python 函数）

```
终端命令: kama-core
    ↓  (pyproject.toml:21  [project.scripts])
    ↓  kama-core = "kama_claude.core.app:run"
    ↓
Python 入口: src/kama_claude/core/__main__.py → 调用 app.run()
```

### 1.1 文件: `pyproject.toml`

声明了三个 CLI 入口，其中 `kama-core` 指向 `kama_claude.core.app:run`。

### 1.2 文件: `src/kama_claude/core/__main__.py`

```python
from kama_claude.core.app import run
run()
```

这是 `python -m kama_claude.core` 的入口，仅一行：导入并调用 `run()`。

---

## 二、`app.py` 启动全流程

### 2.1 文件: `src/kama_claude/core/app.py` — `run()` 函数 (第 58 行)

```
run()                                  ← 同步入口
  │
  └─ asyncio.run(CoreApp().run())     ← 创建 CoreApp 实例，进入异步事件循环
       │
       ├── 1. CoreApp.__init__()      ← 仅记录 self._start_time
       │
       └── 2. CoreApp.run()           ← 异步主流程 (第 34 行)
              │
              ├── ① self._start_time = time.monotonic()
              │
              ├── ② config = get_config()           ← 加载配置（见第三章）
              │
              ├── ③ setup_logging(config)            ← 初始化日志（见第四章）
              │
              ├── ④ server = SocketServer(config.host, config.port)
              │      └── SocketServer.__init__()     ← 仅保存 host/port，初始化空 handlers 字典
              │
              ├── ⑤ server.register("core.ping", self._ping_handler)
              │      └── 将方法名 "core.ping" 映射到 _ping_handler 回调
              │
              ├── ⑥ addr = await server.start()     ← 启动 TCP 服务器（见第五章）
              │
              ├── ⑦ 注册 SIGINT / SIGTERM 信号处理器
              │      └── loop.add_signal_handler(signal.SIGINT, shutdown.set)
              │      └── loop.add_signal_handler(signal.SIGTERM, shutdown.set)
              │
              ├── ⑧ await shutdown.wait()           ← 阻塞等待退出信号
              │
              └── ⑨ await server.stop()             ← 收到信号后优雅关闭
```

---

## 三、配置加载流程: `get_config()` 

### 3.1 文件: `src/kama_claude/core/config.py` — `get_config()` (第 34 行)

配置采用四层优先级，**后者覆盖前者**：

```
默认值 → TOML 文件 → .env 文件 → 系统环境变量
```

### 3.2 详细步骤

```
get_config()
  │
  ├── 1. config = KamaConfig()                    ← 使用 dataclass 默认值
  │      ├── host    = "127.0.0.1"
  │      ├── port    = 7437
  │      └── logging = LoggingConfig(
  │             level  = "INFO"
  │             file   = "~/.kama/logs/core.log"
  │             format = "text"
  │          )
  │
  ├── 2. load_dotenv(".env", override=False)      ← 加载项目根目录 .env 到 os.environ
  │      (override=False: .env 不覆盖已有的系统环境变量)
  │
  ├── 3. 读取 KAMA_CONFIG 环境变量（或默认 ~/.kama/config.toml）
  │      └── 若文件存在 → tomllib.load() 解析 TOML
  │           └── _apply_toml(config, data)        ← 将 TOML 值写入 config
  │                ├── [core] → host, port
  │                └── [logging] → level, file, format
  │                (未知 key 会导致 SystemExit)
  │
  ├── 4. _apply_env(config)                       ← 系统环境变量覆盖
  │      ├── KAMA_HOST       → config.host
  │      ├── KAMA_PORT       → config.port
  │      ├── KAMA_LOG_LEVEL  → config.logging.level
  │      ├── KAMA_LOG_FILE   → config.logging.file
  │      └── KAMA_LOG_FORMAT → config.logging.format
  │
  └── 5. return config
```

### 3.3 关键文件和数据类

| 文件 | 类/函数 | 作用 |
|------|---------|------|
| `config.py:19-23` | `LoggingConfig` | 日志相关配置 dataclass |
| `config.py:26-30` | `KamaConfig` | 顶层配置 dataclass |
| `config.py:34-51` | `get_config()` | 配置加载主函数 |
| `config.py:55-90` | `_apply_toml()` | TOML → dataclass |
| `config.py:94-116` | `_apply_env()` | 环境变量 → dataclass |
| `~/.kama/config.toml` | (用户文件) | 可选的 TOML 配置文件 |

---

## 四、日志初始化: `setup_logging()`

### 4.1 文件: `src/kama_claude/core/logging_setup.py` — `setup_logging()` (第 15 行)

```
setup_logging(config)
  │
  ├── 1. level = getattr(logging, config.logging.level.upper())  ← 字符串 → logging 常量
  │
  ├── 2. fmt = 根据 config.logging.format 选择格式
  │      ├── "json" → '{"level":"%(levelname)s","ts":"%(asctime)s",...}'
  │      └── 其他   → 'level=%(levelname)s ts=%(asctime)s source=%(name)s msg="%(message)s"'
  │
  ├── 3. root logger 设置
  │      ├── root.setLevel(level)
  │      └── root.handlers.clear()          ← 清除已有 handler，防止重复
  │
  ├── 4. 添加 stderr handler (始终启用)
  │
  └── 5. 若 config.logging.file 非空 → 添加 RotatingFileHandler
         ├── maxBytes = 10MB
         ├── backupCount = 5
         └── 自动创建父目录
```

---

## 五、TCP 服务器启动: `SocketServer`

### 5.1 文件: `src/kama_claude/core/transport/socket_server.py`

```
SocketServer(host, port)
  │
  ├── __init__()                     ← 保存 host/port，初始化 self._handlers = {}
  │
  ├── register(method, handler)      ← 注册命令处理方法
  │
  ├── start()                        ← 启动 TCP 服务器
  │    ├── 1. asyncio.open_connection(host, port)   ← 探活：尝试连接
  │    │      ├── 连接成功 → SystemExit("core already running...")
  │    │      └── ConnectionRefusedError → 端口空闲，继续
  │    │
  │    ├── 2. asyncio.start_server(                  ← 绑定端口，启动监听
  │    │        _handle_connection,                  ← 每个连接的回调
  │    │        host=host,
  │    │        port=port,
  │    │        limit=1MB,                           ← 单行最大 1MB
  │    │    )
  │    │
  │    └── 3. return f"{host}:{port}"               ← 返回实际监听地址
  │
  └── stop()                         ← 关闭服务器，最多等待 2 秒
```

### 5.2 连接处理流程

```
客户端连接到达
  │
  └── _handle_connection(reader, writer)
       │
       └── _read_loop(reader, writer)         ← 循环读取 NDJSON 行
            │
            └── _handle_line(line, writer)    ← 逐行处理
                 │
                 ├── 1. json.loads(line)      ← 解析 JSON
                 ├── 2. JsonRpcRequest.model_validate(raw)  ← pydantic 校验
                 ├── 3. 查找 handler
                 ├── 4. await handler(req.params)  ← 调用注册的处理函数
                 └── 5. _send(writer, response)   ← 返回 JSON-RPC 响应
```

---

## 六、完整文件加载顺序（import 链）

从 `kama-core` 命令执行开始，Python 解释器按以下顺序加载模块：

```
1.  pyproject.toml                     ← 入口映射: kama-core → kama_claude.core.app:run
2.  src/kama_claude/__init__.py        ← __version__ = "0.0.1"
3.  src/kama_claude/core/__init__.py   ← 包标记
4.  src/kama_claude/core/app.py        ← run() 函数所在
    ├── kama_claude                    ← (已加载)
    ├── kama_claude.core.bus.commands  ← PongResult
    │   └── kama_claude.core.bus.envelope (间接)
    ├── kama_claude.core.config        ← get_config, KamaConfig
    │   └── python-dotenv (第三方)
    ├── kama_claude.core.logging_setup ← setup_logging
    └── kama_claude.core.transport.socket_server  ← SocketServer
        └── kama_claude.core.bus.envelope ← JsonRpcRequest, JsonRpcSuccess, make_error
```

---

## 七、方法调用时间线总结

```
时间线 (t=0 为执行 kama-core 命令)

t≈0ms    run()                                    [app.py:58]
t≈0ms    └─ asyncio.run(CoreApp().run())          [app.py:59]
t≈0ms         ├─ CoreApp.__init__()               [app.py:20]  记录启动时间戳
t≈0ms         └─ CoreApp.run()                    [app.py:34]
t≈1ms              ├─ _start_time 重置             [app.py:35]
t≈5ms              ├─ get_config()                [config.py:34]  四层配置加载
t≈5ms              │    ├─ KamaConfig()           [config.py:35]  默认值
t≈5ms              │    ├─ load_dotenv()          [config.py:38]  加载 .env
t≈5ms              │    ├─ tomllib.load()         [config.py:45]  解析 TOML (可选)
t≈5ms              │    └─ _apply_env()           [config.py:50]  环境变量覆盖
t≈10ms             ├─ setup_logging(config)       [logging_setup.py:15]
t≈10ms             │    ├─ root logger 级别设置
t≈10ms             │    ├─ stderr handler 挂载
t≈15ms             │    └─ RotatingFileHandler 挂载 (可选)
t≈15ms             ├─ SocketServer(host, port)    [socket_server.py:29]
t≈15ms             ├─ server.register("core.ping", ...)  [socket_server.py:36]
t≈20ms             ├─ await server.start()        [socket_server.py:40]
t≈20ms             │    ├─ 探活连接 (防多实例)
t≈25ms             │    └─ asyncio.start_server() ← TCP 端口绑定
t≈30ms             ├─ 注册 SIGINT/SIGTERM 信号
t≈30ms             └─ await shutdown.wait()       ← 阻塞，等待退出信号

        ... 服务运行中，处理客户端连接 ...

收到信号            shutdown.set()
                    ├─ await server.stop()        [socket_server.py:58]
                    └─ run() 返回，进程退出
```

---

## 八、关键设计要点

1. **防多实例**: `SocketServer.start()` 先尝试连接目标端口，若成功则直接 `SystemExit`，防止重复启动守护进程。
2. **四层配置优先级**: 默认值 < TOML < .env < 环境变量，后者覆盖前者。`.env` 在读取 `KAMA_CONFIG` 之前加载，因此 `.env` 中可以设置 `KAMA_CONFIG` 来改变 TOML 路径。
3. **JSON-RPC 2.0 over NDJSON**: 每行一个完整的 JSON 对象，用换行符分隔，最大单帧 1MB。
4. **优雅关闭**: 通过 `asyncio.Event` + 信号处理器实现，收到 SIGINT/SIGTERM 后调用 `server.stop()` 并最多等待 2 秒让现有连接完成。
5. **日志双通道**: stderr 始终输出（适合 systemd/journald），文件日志可选且自动轮转（10MB × 5）。
