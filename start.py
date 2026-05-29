# -*- coding: utf-8 -*-
r"""网关启动入口。

用法：
    D:\APP\conda\envs\llm\python.exe start.py
    D:\APP\conda\envs\llm\python.exe start.py --config config.yaml

职责：
- 读取并校验配置文件。
- 初始化 Starlette 应用。
- 使用 uvicorn 启动 MCP 聚合网关。
"""
from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from src.app import create_app
from src.config import DEFAULT_CONFIG_PATH, load_config


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="启动 MCP 聚合网关")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="配置文件路径，默认使用项目根目录下的 config.yaml",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="日志级别",
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    """配置日志输出。"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main() -> int:
    """程序入口。"""
    args = parse_args()
    setup_logging(args.log_level)

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - 启动器应直接给出可读错误
        logging.getLogger("mcp_gateway.start").error("加载配置失败：%s", exc)
        return 1

    app = create_app(config)
    logging.getLogger("mcp_gateway.start").info(
        "启动网关：0.0.0.0:%s，服务=%s",
        config.gateway.port,
        [svc.name for svc in config.services if svc.enabled],
    )
    uvicorn.run(app, host="0.0.0.0", port=config.gateway.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
