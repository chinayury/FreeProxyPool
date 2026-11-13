# -*- coding: utf-8 -*-
"""ASN 推导 IP 机型（机房 / 住宅 / 未知）。

v0.1.0 B-07：基于 GeoLite2-ASN.mmdb 读取 ``autonomous_system_organization``，
按已知云厂商关键字判定机房 IP。读取失败或库未配置时回退到 ``unknown``。

设计约束：

* **绝不阻塞主流程**：ASN 解析失败只保留 ``ip_type="unknown"``，不影响代理入池；
* **文件级单例**：每个进程只打开一次 mmdb，避免重复文件句柄与内存映射；
* **零外部依赖**：复用 B-06 引入的 ``maxminddb``，不额外增加依赖。
"""
import os
from typing import Optional

from .. import config_center as cc
from ..core.logging import get_logger

log = get_logger("asn", to_file=False)

#: 判定为机房/云厂商的 ASN org 关键字（小写匹配）
#: 注意：匹配是对 org.lower() 做子串包含，因此关键字必须全小写、不能带首尾空格，
#: 否则永远不会命中（历史上 " CenturyLink" / "Lumen" 两条就是这样失效的）。
_DATACENTER_KEYWORDS = (
    "amazon", "google", "alibaba", "tencent", "digitalocean",
    "microsoft", "azure", "oracle", "huawei", "baidu",
    "ovh", "hetzner", "linode", "vultr", "rackspace",
    "softlayer", "centurylink", "lumen",
    "chinanet", "cernet", "drpeng",
)

#: 判定为住宅/移动的 ASN org 关键字（小写匹配）
_RESIDENTIAL_KEYWORDS = (
    "residential", "home", "broadband", "cable", "dsl",
    "fios", "spectrum", "cox", "comcast", "att", "verizon",
    "sbc", "telstra", "sky", "virgin", "talktalk",
    "bt ", "ee ", "vodafone", "three", "o2",
)


def _match_keyword(org: str, keywords: tuple) -> bool:
    org_lower = org.lower()
    return any(kw in org_lower for kw in keywords)


class AsnProvider:
    """ASN 解析器（单例，随验证器进程生命周期复用）。"""

    def __init__(self, db_path: str = ""):
        self._db_path = ""
        self._reader = None
        self.set_path(db_path)

    def set_path(self, db_path: str) -> None:
        """设置 mmdb 路径；路径变化时关闭旧 reader（支持 ASN_DB 热更新）。"""
        path = (db_path or "").strip()
        if path == self._db_path:
            return
        self.close()
        self._db_path = path

    def _ensure_reader(self):
        if self._reader is not None:
            return self._reader
        if not self._db_path or not os.path.isfile(self._db_path):
            return None
        try:
            import maxminddb
            self._reader = maxminddb.open_database(self._db_path)
            return self._reader
        except Exception:
            return None

    def lookup(self, ip: str) -> str:
        """推断 IP 类型：``datacenter`` / ``residential`` / ``unknown``。"""
        reader = self._ensure_reader()
        if reader is None:
            return "unknown"
        try:
            data = reader.get(ip)
        except Exception:
            return "unknown"
        if not isinstance(data, dict):
            return "unknown"
        org = str(data.get("autonomous_system_organization") or "").strip()
        if not org:
            return "unknown"
        if _match_keyword(org, _DATACENTER_KEYWORDS):
            return "datacenter"
        if _match_keyword(org, _RESIDENTIAL_KEYWORDS):
            return "residential"
        # 无法明确归类时保持 unknown，宁可漏标也不误标
        return "unknown"

    def close(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:
                pass
            self._reader = None


# 模块级单例（延迟初始化）
_provider: Optional[AsnProvider] = None


def get_provider() -> AsnProvider:
    global _provider
    if _provider is None:
        _provider = AsnProvider(cc.get("ASN_DB", ""))
    return _provider


def lookup_asn(ip: str) -> str:
    """便捷函数：返回 IP 类型，失败或不启用时返回 ``unknown``。"""
    if not ip or not bool(cc.get("ASN_ENABLED", True)):
        return "unknown"
    try:
        provider = get_provider()
        # ASN_DB 可能被管理后台热更新，同步一次路径（只在 __init__ 读一次的话，
        # 改完配置不重启进程就永远不生效）
        provider.set_path(cc.get("ASN_DB", ""))
        return provider.lookup(ip)
    except Exception:
        return "unknown"


__all__ = ["AsnProvider", "get_provider", "lookup_asn"]
