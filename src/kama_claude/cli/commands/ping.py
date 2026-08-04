from __future__ import annotations

import asyncio
import json
import sys
import time

import kama_claude
from kama_claude.core.bus.commands import PongResult
from kama_claude.core.bus.envelope import JsonRpcError, JsonRpcSuccess
from kama_claude.core.config import KamaConfig


# 同步入口：运行 ping 协程，连接失败时打印错误并退出
def cmd_ping(config: KamaConfig) -> None:
    try:
        asyncio.run(_ping(config))
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        sys.exit(1)


# 向 core 守护进程发送 ping 请求，打印 pong 响应及延迟
async def _ping(config: KamaConfig) -> None:
    t0 = time.monotonic()  # 开始计时，用于计算往返延迟

    # 建立到 daemon 的 TCP 连接
    reader, writer = await asyncio.open_connection(config.host, config.port)

    # 构建 JSON-RPC 2.0 请求体（应用层协议：规定消息内容长什么样）
    req = {
        "jsonrpc": "2.0",
        "id": "cli-1",
        "method": "core.ping",
        "params": {"client": f"cli/{kama_claude.__version__}"},
    }
    # NDJSON 定界：json.dumps() 将 dict 序列化为一行 JSON 文本，末尾追加 \n 作为帧边界
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()  # 确保数据从写缓冲区刷出到 TCP 发送队列

    # NDJSON 解帧：readline() 按 \n 读取完整一行，自动处理半包/粘包
    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
    latency_ms = int((time.monotonic() - t0) * 1000)  # 毫秒级往返延迟

    # 关闭发送端（不等待 server 主动关闭，直接结束）
    writer.close()
    await writer.wait_closed()

    # 将 NDJSON 行反序列化回 dict，再按 JSON-RPC 结构校验分发
    raw = json.loads(line)
    # 响应可能是错误：走 JsonRpcError 校验
    if "error" in raw:
        err = JsonRpcError.model_validate(raw)
        print(f"error: {err.error.code} {err.error.message}", file=sys.stderr)
        sys.exit(1)

    # 响应是成功：先校验外层 JSON-RPC 信封，再校验内层 PongResult
    resp = JsonRpcSuccess.model_validate(raw)
    result = PongResult.model_validate(resp.result)
    print(f"pong server={result.server_version} uptime={result.uptime_ms}ms latency={latency_ms}ms")
