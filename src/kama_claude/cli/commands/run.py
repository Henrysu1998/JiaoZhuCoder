from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient


class StdoutPrinter:
    # 接收 dict 格式的事件并将运行进度格式化打印到终端
    def __init__(self) -> None:
        self._inline = False  # True while LLM tokens are mid-line
        self._run_start: float = 0.0

    # 若当前行有未换行的 token，补一个换行符
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 根据事件 type 字段分发并格式化打印到 stdout/stderr
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "run.started":
            self._run_start = time.monotonic()
            print(f"[run] {event.get('run_id', '')}")

        elif t == "step.started":
            self._ensure_newline()
            print(f"[step {event.get('step')}] planning...")

        elif t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True

        elif t == "tool.call_started":
            self._ensure_newline()
            params_str = json.dumps(event.get("params", {}), ensure_ascii=False)
            print(f"[tool] {event.get('tool_name', '')} {params_str}")

        elif t == "tool.call_finished":
            print(f"[tool] {event.get('tool_name', '')} ✓  {event.get('elapsed_ms')}ms")

        elif t == "tool.call_failed":
            print(
                f"[tool] {event.get('tool_name', '')} ✗  {event.get('error_message', '')}",
                file=sys.stderr,
            )

        elif t == "step.finished":
            self._ensure_newline()
            print(f"[step {event.get('step')}] done")

        elif t == "run.finished":
            self._ensure_newline()
            elapsed = time.monotonic() - self._run_start
            print(f"[run] {event.get('status', '')}  {event.get('steps')} steps  {elapsed:.1f}s")


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


# 执行 kama run --goal "..." 命令
def cmd_run(goal: str, config: KamaConfig) -> None:
    try:
        exit_code = asyncio.run(_run_async(goal, config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
