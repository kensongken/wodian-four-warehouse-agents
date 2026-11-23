#!/usr/bin/env python3
"""从 Wikimedia Commons 导入开放许可的珠宝测试图片。"""

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = REPOSITORY_ROOT / "services" / "backend" / "src"
sys.path.insert(0, str(BACKEND_SOURCE))

from product_engine.importers.commons import (
    USER_AGENT,
    CommonsClient,
    CommonsImporter,
    ManifestStore,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="导入可追溯、可断点续传的 Commons 珠宝测试图片",
    )
    parser.add_argument("--target", type=int, default=10, help="累计成功目标数")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=REPOSITORY_ROOT / ".local-data" / "commons-3000",
        help="断点清单和报告目录",
    )
    parser.add_argument(
        "--upload-url",
        default="http://127.0.0.1:8000/local/api/templates/upload",
        help="本地图片上传接口",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        choices=range(1, 5),
        metavar="1-4",
        help="图片下载批次大小（默认 1，避免 Commons CDN 限流）",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=2.0,
        help="每批下载后的间隔秒数（默认 2.0）",
    )
    return parser.parse_args()


async def run() -> int:
    arguments = parse_arguments()
    if arguments.target < 1:
        raise SystemExit("--target 必须大于 0")
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    async with httpx.AsyncClient(
        base_url="https://commons.wikimedia.org",
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
        follow_redirects=True,
        limits=limits,
    ) as http:
        importer = CommonsImporter(
            CommonsClient(http),
            ManifestStore(arguments.data_root),
            arguments.upload_url,
            concurrency=arguments.concurrency,
            request_interval=arguments.request_interval,
        )
        summary = await importer.run(target=arguments.target)
    print(
        f"导入结束：成功 {summary.success_count}/{arguments.target} 张，"
        f"失败 {summary.failure_count} 条，状态 {summary.pause_reason}"
    )
    print(f"追溯清单：{arguments.data_root / 'manifest.jsonl'}")
    return 0 if summary.success_count >= arguments.target else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
