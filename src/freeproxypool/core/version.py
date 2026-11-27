# -*- coding: utf-8 -*-
"""FreeProxyPool 版本单一来源。

全项目的版本号只允许来自根目录的 VERSION 文件，避免 setting.py、
FastAPI app、/version 端点、容器镜像标签、文档各写各的导致漂移。

读取顺序：
    1. 环境变量 FREEPROXYPool_VERSION（CI / 打包时可覆盖）
    2. 项目根目录 VERSION 文件（从本文件位置向上有界搜索）
    3. 本文件内嵌的 FALLBACK_VERSION（仅当上述都不可用时兜底）
"""
import os

# 内嵌兜底值：仅在 VERSION 文件缺失（例如以 zip 解压方式单文件运行）时使用。
# 正常部署路径永远读 VERSION 文件，这里存在的目的是"任何情况下都能返回合法版本"。
FALLBACK_VERSION = "0.2.2"


def _find_version_file() -> str:
    """从本文件位置向上有界搜索仓库根的 VERSION 文件。

    支持 src 布局：version.py 位于 ``src/freeproxypool/core/``，
    需向上穿越包目录找到含 VERSION 文件的仓库根。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(10):  # 有界向上搜索，最多 10 层
        candidate = os.path.join(cur, "VERSION")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


_VERSION_FILE = _find_version_file()


def read_version() -> str:
    """返回语义化版本号字符串，始终非空。"""
    env = os.environ.get("FREEPROXYPool_VERSION")
    if env and env.strip():
        return env.strip()

    if _VERSION_FILE:
        try:
            with open(_VERSION_FILE, "r", encoding="utf-8") as fp:
                value = fp.read().strip()
            if value:
                return value
        except OSError:
            pass

    return FALLBACK_VERSION


VERSION = read_version()

__all__ = ["VERSION", "FALLBACK_VERSION", "read_version"]