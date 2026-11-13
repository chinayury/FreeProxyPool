# -*- coding: utf-8 -*-
"""Redis 待验代理队列。

采集器 LPUSH 生产、验证器 RPOP 消费。

## v0.1.0 修复：队列载荷丢失采集来源

早期实现只把 ``"ip:port"`` 裸字符串推进队列，**采集来源在入队那一刻就丢了**。
后果是全池代理的 ``source`` 字段恒为空，管理后台"各源贡献了多少可用代理"
与 ``/count/`` 的 source 维度彻底失效 —— 采集源的好坏无法评估。

现在载荷为紧凑 JSON ``{"p":"ip:port","s":"srcA/srcB"}``：

* 省带宽：约 20~60 字节，比原字符串只多几十字节；
* **向后兼容**：验证端遇到不带 ``{`` 的元素仍按裸字符串处理，
  因此升级期间队列里已有的旧数据不会丢。

v0.1.0（P0-1）新增紧凑键 ``"o"``（传输协议）。**只有非 HTTP 才写**，
HTTP 载荷与旧版逐字节一致；缺 ``"o"`` 的旧载荷按 HTTP 处理。

## v0.1.0 新增：队列长度上限

旧版队列**没有任何背压机制**：一旦验证服务停摆（Redis 抖动、进程崩溃、
网络分区），采集器会持续 LPUSH，而队列里的元素只增不减，最终
``OOM command not allowed`` 把整个 Redis 打死 —— 连带网关一起不可用。

现在 :meth:`push_many` 在批量写入前检查长度，超上限时用
``LTRIM`` 丢弃最旧的待验元素，保证"验证服务长时间停摆"不会拖垮存储。
"""
import json
import time
from typing import Iterable, List, Optional, Tuple

from .. import config_center as cc
from .. import setting
from ..helper.proxy import normalize_protocol

QUEUE_KEY = "fpp:queue"
DEDUP_KEY = "fpp:queue:dedup"


def encode(item) -> Tuple[str, str]:
    """把采集结果编码为 ``(去重键, 入队载荷)``。

    :param item: ``"ip:port"`` 字符串，或 ``{"proxy":..., "source":...}`` 字典

    v0.1.0（P0-1）：载荷新增紧凑键 ``"o"`` 存**传输协议**（socks4 / socks5）。
    只有非 HTTP 才写这个键 —— HTTP 载荷与旧版**逐字节一致**，
    升级期间队列里新旧数据可以混存，验证端遇到缺 ``"o"`` 的载荷按 HTTP 处理。
    """
    if isinstance(item, str):
        return item, item
    if isinstance(item, dict):
        proxy = str(item.get("proxy") or "")
        source = item.get("source") or ""
        if isinstance(source, (list, tuple)):
            source = "/".join(str(part) for part in source if part)
        payload = {"p": proxy, "s": str(source)}
        protocol = normalize_protocol(item.get("protocol"))
        if protocol != "http":
            payload["o"] = protocol
        return proxy, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = str(item)
    return text, text


def decode(raw: str):
    """解码队列载荷为 :class:`helper.proxy.Proxy`。

    兼容两种形态：紧凑 JSON（带来源）与裸 ``ip:port``（旧数据）。
    """
    from ..helper.proxy import Proxy

    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return Proxy.from_dict({"proxy": data.get("p", ""),
                                    "source": data.get("s", ""),
                                    "protocol": data.get("o", "http")})
        except (TypeError, ValueError):
            pass
    return Proxy(text)


class ProxyQueue:
    """待验代理队列。"""

    def __init__(self, conn=None):
        self._conn = conn

    @property
    def conn(self):
        return self._conn if self._conn is not None else cc.get_redis()

    # ---------- 生产者 ----------

    def push_many(self, proxies: Iterable) -> int:
        """批量入队，返回实际入队条数。

        流程：编码 → 去重集合过滤 → 长度保护 → 一次性 LPUSH。
        全部走 pipeline，避免逐条往返。
        """
        encoded = []
        seen = set()
        for item in proxies or []:
            proxy, payload = encode(item)
            if not proxy or proxy in seen:
                continue
            seen.add(proxy)
            encoded.append((proxy, payload))
        if not encoded:
            return 0

        conn = self.conn
        dedup_ttl = int(cc.get("QUEUE_DEDUP_TTL", setting.QUEUE_DEDUP_TTL))
        max_len = int(cc.get("QUEUE_MAX_LEN", setting.QUEUE_MAX_LEN))

        try:
            pipe = conn.pipeline()
            for proxy, _payload in encoded:
                pipe.sadd(DEDUP_KEY, proxy)
            added = pipe.execute()
            fresh = [pair for pair, flag in zip(encoded, added) if flag]
        except Exception:
            # 去重不可用时不做静默丢弃，退化为直接入队，保证采集不丢数据
            fresh = encoded

        if not fresh:
            return 0

        try:
            pipe = conn.pipeline()
            if dedup_ttl > 0:
                pipe.expire(DEDUP_KEY, dedup_ttl)
            for _proxy, payload in fresh:
                pipe.lpush(QUEUE_KEY, payload)
            pipe.llen(QUEUE_KEY)
            results = pipe.execute()
            depth = int(results[-1] or 0)
            if depth > max_len:
                # 丢弃最旧的 depth - max_len 条（队列头部），保留最新数据
                self.conn.ltrim(QUEUE_KEY, depth - max_len, -1)
                depth = max_len
        except Exception:
            # 队列写入失败：整体回退为逐条写入，尽量不丢
            count = 0
            for _proxy, payload in fresh:
                try:
                    self.conn.lpush(QUEUE_KEY, payload)
                    count += 1
                except Exception:
                    break
            return count
        return len(fresh)

    # ---------- 消费者 ----------

    def pop_batch(self, batch_size: int = 200) -> List[str]:
        """原子批量弹出最多 ``batch_size`` 条（一次 RTT，不再逐条往返）。

        返回原始载荷字符串，由 :func:`decode` 解析。
        """
        batch_size = max(1, int(batch_size))
        try:
            # RPOP key count：Redis 6.2+ 支持，一次往返取回多条
            items = self.conn.rpop(QUEUE_KEY, batch_size)
        except Exception:
            items = None
        if items is None:
            # 兼容不支持 RPOP COUNT 的旧版本
            items = []
            for _ in range(batch_size):
                try:
                    item = self.conn.rpop(QUEUE_KEY)
                except Exception:
                    break
                if item is None:
                    break
                items.append(item)
        return [str(item).strip() for item in items or [] if item and str(item).strip()]

    # ---------- 观测 ----------

    def depth(self) -> int:
        """当前积压深度。"""
        try:
            return int(self.conn.llen(QUEUE_KEY) or 0)
        except Exception:
            return 0

    def clear(self) -> None:
        try:
            self.conn.delete(QUEUE_KEY, DEDUP_KEY)
        except Exception:
            pass


__all__ = ["ProxyQueue", "encode", "decode", "QUEUE_KEY", "DEDUP_KEY"]