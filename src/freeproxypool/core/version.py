# -*- coding: utf-8 -*-
"""FreeProxyPool 版本单一来源。

全项目的版本号只允许来自根目录的 VERSION 文件，避免 setting.py、
FastAPI app、/version 端点、容器镜像标签、文档各写各的导致漂移。

读取顺序：
    1. 环境变量 FREEPROXYPool_VERSION（CI / 打包时可覆盖）
    2. 从源码运行时读取本项目根目录的 VERSION 文件
    3. 安装包或缺少源码版本文件时使用随包发布的 FALLBACK_VERSION
"""
import os
import tomllib
from pathlib import Path

# wheel 内嵌版本与根目录 VERSION 的一致性由自检校验。
FALLBACK_VERSION = "0.2.3"


def read_version() -> str:
    """返回语义化版本号字符串，始终非空。"""
    env = os.environ.get("FREEPROXYPool_VERSION")
    if env and env.strip():
        return env.strip()

    src = Path(__file__).resolve().parents[2]
    if src.name == "src":
        try:
            with (src.parent / "pyproject.toml").open("rb") as fp:
                project = tomllib.load(fp).get("project", {})
            if isinstance(project, dict) and project.get("name") == "freeproxypool":
                return (src.parent / "VERSION").read_text(encoding="utf-8").strip() or FALLBACK_VERSION
        except (OSError, ValueError):
            pass

    return FALLBACK_VERSION


VERSION = read_version()

__all__ = ["VERSION", "FALLBACK_VERSION", "read_version"]