# 开启延迟求值注解，兼容 Python 3.12 前向引用
from __future__ import annotations

# 标准库：命令行参数解析与系统退出
import argparse
import sys

# 子命令的实现：ping（心跳探测）和 version（版本号输出）
from kama_claude.cli.commands.ping import cmd_ping
from kama_claude.cli.commands.version import cmd_version

# 核心模块：读取配置、初始化日志
from kama_claude.core.config import get_config
from kama_claude.core.logging_setup import setup_logging


# CLI 主入口：解析命令行参数并分发到对应子命令
def main() -> None:
    # 搭建命令行骨架：声明程序名、支持的参数和子命令
    parser = argparse.ArgumentParser(prog="kama", description="KamaClaude CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("ping", help="Ping the core daemon")

    # 解析用户在终端输入的内容
    args = parser.parse_args()

    # --version 独立处理，无需加载配置和日志
    if args.version:
        cmd_version()
        return

    # ping 子命令：读配置 → 开日志 → 发送心跳
    if args.command == "ping":
        config = get_config()
        setup_logging(config)
        cmd_ping(config)
    else:
        # 未匹配任何命令时打印帮助并以错误码退出
        parser.print_help()
        sys.exit(1)
