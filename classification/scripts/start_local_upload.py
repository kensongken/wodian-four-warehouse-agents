#!/usr/bin/env python3
"""启动无需 Docker 的本地一键图片上传页。"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

import uvicorn


def resolve_backend_source(root: Path) -> Path:
    """同时支持 Git 仓库布局和常驻服务的独立后端布局。"""

    repository_source = root / "services" / "backend" / "src"
    if repository_source.is_dir():
        return repository_source
    deployed_source = root / "src"
    if deployed_source.is_dir():
        return deployed_source
    raise RuntimeError("local_upload_backend_source_missing")


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = resolve_backend_source(REPOSITORY_ROOT)
sys.path.insert(0, str(BACKEND_SOURCE))


def configured_port(environment: Mapping[str, str] = os.environ) -> int:
    """读取本机页面端口；拒绝非法值，避免常驻服务启动到错误端口。"""

    raw = environment.get("LOCAL_UPLOAD_PORT", "8000")
    try:
        port = int(raw)
    except ValueError as error:
        raise ValueError("local_upload_port_invalid") from error
    if not 1 <= port <= 65535:
        raise ValueError("local_upload_port_invalid")
    return port


def main() -> None:
    from product_engine.services.image_recognition import (
        RecognitionFailure,
    )
    from product_engine.services.on_device_vision import (
        ensure_local_vision_executable,
    )

    source = BACKEND_SOURCE / "product_engine" / "native" / "local_vision.swift"
    output = REPOSITORY_ROOT / ".local-data" / "bin" / "product-image-local-vision"
    try:
        executable = ensure_local_vision_executable(source, output)
    except RecognitionFailure:
        print("\n本机视觉识别暂时不可用，页面仍可继续人工审核。")
    else:
        os.environ["LOCAL_VISION_EXECUTABLE"] = str(executable)
        print("\n本机视觉识别已准备。")

    # Settings 从后端 .env 读取本机 S3/签名配置；否则相册收件箱会被安全地关闭。
    os.chdir(BACKEND_SOURCE.parent)
    from product_engine.api.local_app import app

    port = configured_port()
    url = f"http://127.0.0.1:{port}/local/templates"
    print(f"\n本地图片上传页已准备：{url}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
