# -*- coding: utf-8 -*-
"""代理池订阅输出（Clash/Mihomo 与 V2Ray/V2RayN 两种格式）。

把池内 HTTP / SOCKS5 代理格式化为客户端订阅 —— 用户在 Clash / v2rayN
等客户端填一个订阅 URL 即可自动同步整个代理池（借鉴 docs/06 调研记录
中 vpn 类项目的标准分发形态）。

设计取舍：

* **SOCKS4 不输出** —— Clash/Mihomo 的节点类型没有 socks4，v2rayN 的
  ``socks://`` 分享同样按 SOCKS5 解析；硬塞会生成连不上的死节点，
  如实跳过而不是产出坏数据。
* **不引入 YAML 依赖** —— Clash 订阅的节点段是平铺键值结构，手工拼接
  即可；host/port 逐项校验，name 由本模块生成的 ``fpp-NNN`` 固定模式，
  无注入面。符合依赖最小化。
* 输出内容与 ``/all/`` 同源同权 —— 全池列表属敏感数据，鉴权与限流
  在端点层完成（与 ``/all/`` 完全一致），本模块只做纯格式化。
"""
import base64

#: Clash/Mihomo 支持的节点类型映射（socks4 等其余协议跳过）
_CLASH_TYPES = {"http": "http", "socks5": "socks5"}


def _pool_items(items):
    """过滤出可订阅节点（http / socks5），返回节点字典列表。

    节点字段：``name``（fpp-NNN 固定模式）/ ``type`` / ``server`` / ``port``。
    地址解析用 ``rpartition(':')`` 以容忍 IPv6 形态（订阅场景主用 IPv4，
    IPv6 无括号形式也能取到最后的端口段）。
    """
    from .proxy import normalize_protocol
    out = []
    for idx, item in enumerate(items, 1):
        proxy = str(item.get("proxy") or "")
        host, _, port = proxy.rpartition(":")
        if not host or not port.isdigit():
            continue
        clash_type = _CLASH_TYPES.get(normalize_protocol(item.get("protocol")))
        if not clash_type:
            continue
        out.append({
            "name": "fpp-%03d" % idx,
            "type": clash_type,
            "server": host,
            "port": int(port),
        })
    return out


def build_clash_yaml(items):
    """把池内代理格式化为 Clash/Mihomo 订阅 YAML 文本。

    输出一个 select 型 proxy-group（FreeProxyPool）包含全部节点，
    客户端可在组内手动切换；空池时组回落到 DIRECT，保证配置始终合法。
    """
    nodes = _pool_items(items)
    lines = [
        "port: 7890",
        "socks-port: 7891",
        "allow-lan: false",
        "mode: rule",
        "log-level: info",
        "proxies:",
    ]
    for node in nodes:
        lines += [
            "  - name: %s" % node["name"],
            "    type: %s" % node["type"],
            "    server: %s" % node["server"],
            "    port: %d" % node["port"],
        ]
    lines += [
        "proxy-groups:",
        "  - name: FreeProxyPool",
        "    type: select",
        "    proxies:",
    ]
    if nodes:
        lines += ["      - %s" % n["name"] for n in nodes]
    else:
        lines.append("      - DIRECT")
    lines += [
        "rules:",
        "  - MATCH,FreeProxyPool",
    ]
    return "\n".join(lines) + "\n"


def build_v2ray_b64(items):
    """把池内代理格式化为 V2Ray/V2RayN 订阅：``base64(逐行分享 URI)``。

    * HTTP 代理 → ``http://host:port#name``
    * SOCKS5 代理 → ``socks://host:port#name``（v2rayN 按 SOCKS5 解析）
    """
    nodes = _pool_items(items)
    uris = ["%s://%s:%d#%s" % ("socks" if n["type"] == "socks5" else "http",
                               n["server"], n["port"], n["name"])
            for n in nodes]
    return base64.b64encode("\n".join(uris).encode("utf-8")).decode("ascii")


__all__ = ["build_clash_yaml", "build_v2ray_b64"]
