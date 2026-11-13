# -*- coding: utf-8 -*-
"""采集源基类与熔断器。

## v0.1.0 的三处行为修复

1. **"抓到 0 条"不再算成功**。
   旧版 :meth:`BaseFetcher.fetch` 只判断"响应体是否非空"：

       r = WebRequest().get(url, ...)
       text = r.text
       if text:              # ← 只要有 HTML 就算成功
           for proxy in self.parse(text): yield proxy
           break

   源站改版（返回验证码页 / 改版空页）时，正文非空但解析不出任何代理，
   于是 :func:`fetcher_mark_success` 被调用，熔断计数清零 ——
   **坏源永远不会熔断**，垃圾流量持续涌入。
   现在判定条件改为"解析出 ≥1 条合法代理"才算成功。

2. **响应状态码参与判定**。
   旧版不检查 ``status_code``，404 / 500 的错误页正文会被当成数据解析。
   现在非 2xx 一律按失败处理并退避重试。

3. **熔断状态写入 Redis**。
   旧版熔断状态只存在采集进程的模块级 dict 里，管理后台和 Prometheus
   永远读到 0；重启即丢失。现在同步写入 ``fpp:breaker`` Hash，
   网关/调度器可直接读取。
"""
import re
import threading
import time
from typing import List, Optional

from .. import config_center as cc
from ..core.iputil import is_valid_proxy, strip_proxy_prefix

#: 熔断状态内存缓存：{name: {'fails': n, 'until': ts}}
_breaker: dict = {}
_breaker_lock = threading.RLock()

#: 形如 ip:port 的通用匹配（允许中间有空格或冒号两侧空白）
IP_PORT_PATTERN = re.compile(
    r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?:\s*:\s*|\s+)(\d{1,5})(?![\d.])"
)


# ================= 熔断器 =================

def breaker_status(name: str) -> dict:
    """返回某源当前熔断状态。"""
    with _breaker_lock:
        state = _breaker.get(name)
        return dict(state) if state else {"fails": 0, "until": 0}


def is_tripped(name: str) -> bool:
    """源是否处于熔断中（冷却到期后自动复位）。

    v0.1.0 关键修复：旧实现只要发现 ``until <= now`` 就把整条状态
    ``pop`` 掉 —— 而"从未熔断"的源 ``until`` 本来就是 0，于是
    **每次查询都会把累计失败次数清零**。

    由于 ``helper.fetch.enabled_fetchers()`` 在每轮采集开始时都会对
    所有源调用一次本函数，实际效果是：任何源的单轮失败计数都被抹掉，
    永远攒不到 ``FETCHER_MAX_FAIL`` 阈值，**熔断机制形同虚设**，
    坏源可以无限期地拖慢每一轮采集。

    现在区分两种情况：

    * ``until == 0``：从未熔断 → 只查询、**不动失败计数**；
    * ``until > 0`` 且已到期：熔断冷却结束 → 清零并给一次重新计数的机会。
    """
    with _breaker_lock:
        state = _breaker.get(name)
        if not state:
            return False
        if state["until"] > time.time():
            return True
        if state["until"] > 0:
            # 冷却结束：清零计数与熔断标记，允许重新尝试
            _breaker.pop(name, None)
            cc.set_breaker(name, 0)
        return False


def mark_success(name: str) -> None:
    """请求成功：清零连续失败计数。"""
    with _breaker_lock:
        _breaker.pop(name, None)
    cc.set_breaker(name, 0)


def mark_failure(name: str) -> dict:
    """请求失败：累加连续失败计数，达到阈值进入熔断。"""
    threshold = int(cc.get("FETCHER_MAX_FAIL", 5))
    cooldown = int(cc.get("FETCHER_COOLDOWN", 600))
    now = time.time()
    with _breaker_lock:
        state = _breaker.get(name)
        if state and state["until"] > 0 and state["until"] <= now:
            state = None  # 冷却已结束，重新计数
        if state is None:
            state = {"fails": 0, "until": 0}
        _breaker[name] = state
        state["fails"] += 1
        if state["fails"] >= threshold:
            state["until"] = now + cooldown
        snapshot = dict(state)
    cc.set_breaker(name, snapshot["until"])
    return snapshot


def tripped_names() -> List[str]:
    """当前熔断中的源名列表。"""
    now = time.time()
    with _breaker_lock:
        return [name for name, state in _breaker.items() if state["until"] > now]


def trip_until_text(name: str) -> str:
    """熔断截止时间的可读文本。"""
    until = breaker_status(name).get("until", 0)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until)) if until else ""


# ================= 采集源基类 =================

class BaseFetcher:
    """采集源基类（声明式）。

    子类只需声明::

        name = "example"        # 唯一标识（仅允许字母/数字/下划线/中划线/点）
        url = "https://..."     # 站点首页（仅用于后台展示）
        urls = ["https://..."]  # 实际抓取的 URL 列表
        enabled = True          # 是否默认启用
        is_html = False         # 正文是否为 HTML 表格

    可选覆写 :meth:`parse` 实现自定义解析。
    """

    name: str = ""
    url: str = ""
    urls: List[str] = []
    enabled: bool = True
    is_html: bool = False
    #: 源站现状说明（改版 / 下线 / 需 Cookie 等），展示在管理后台
    status_note: str = ""
    #: 本源产出的代理的**传输协议**（http / socks4 / socks5）。
    #:
    #: v0.1.0（P0-1）新增。绝大多数源是 HTTP 代理列表，保持默认 ``http``；
    #: 专门的 SOCKS 列表把 ``protocol`` 覆盖为 ``socks5`` / ``socks4``，
    #: 采集时写进 :class:`helper.proxy.Proxy`，后续的校验与分发据此
    #: 选择对应的连接方式。
    protocol: str = "http"

    # ---------- URL ----------

    def get_urls(self) -> List[str]:
        """实际抓取 URL（优先配置中心覆盖）。"""
        overridden = cc.get_fetcher_urls(self.name)
        if overridden:
            return list(overridden)
        return list(self.urls or [])

    # ---------- 抓取 ----------

    def fetch(self):
        """抓取并逐条 yield 代理地址（已归一化为 ``ip:port``）。

        这是生成器：调用方消费完成后才会执行后面的熔断状态更新，
        因此**必须完整消费**本生成器，否则熔断状态不会被结算。
        """
        from ..util.webRequest import get_client

        if is_tripped(self.name):
            return

        urls = self.get_urls()
        if not urls:
            return

        client = get_client()
        timeout = int(cc.get("FETCH_TIMEOUT", 15))
        retries = int(cc.get("FETCH_RETRY", 2))
        total = 0

        for url in urls:
            backoff = 1
            for attempt in range(retries + 1):
                text = self._request(client, url, timeout)
                if text is None:
                    if attempt < retries:
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 8)
                    continue
                found = 0
                for proxy in self.parse(text):
                    normalized = strip_proxy_prefix(proxy)
                    if is_valid_proxy(normalized):
                        yield normalized
                        found += 1
                # 只有真的解析出代理才算这个源"活着"
                total += found
                if found:
                    break
                # 正文非空但解析为 0 条：视同抓取失败，进入退避重试
                if attempt < retries:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 8)

        if total:
            mark_success(self.name)
        else:
            mark_failure(self.name)

    def _request(self, client, url: str, timeout: int) -> Optional[str]:
        """发起一次请求；非 2xx 或异常都返回 ``None``。"""
        try:
            response = client.get(url, timeout=timeout, retry_time=1)
        except Exception:
            return None
        if response is None:
            return None
        status = getattr(response, "status_code", None)
        if status is not None and not (200 <= int(status) < 300):
            return None
        text = getattr(response, "text", "") or ""
        return text.strip() or None

    # ---------- 解析 ----------

    def parse(self, text: str):
        """解析正文，默认用正则提取 ``ip:port``。"""
        if self.is_html:
            text = strip_tags(text)
        for proxy in self.parse_from_text(text):
            yield proxy

    @staticmethod
    def parse_from_text(text: str) -> List[str]:
        """正则提取所有 ``ip:port``。"""
        if not text:
            return []
        seen = []
        known = set()
        for ip, port in IP_PORT_PATTERN.findall(text):
            item = "%s:%s" % (ip, port)
            if item not in known:
                known.add(item)
                seen.append(item)
        return seen


_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&nbsp;?", re.IGNORECASE)


def strip_tags(html: str) -> str:
    """剥离 HTML 标签并还原不换行空格，避免表格单元格被粘连成 ``1.2.3.48080``。"""
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    return _ENTITY_RE.sub(" ", text)


__all__ = [
    "BaseFetcher", "is_tripped", "mark_success", "mark_failure",
    "breaker_status", "tripped_names", "trip_until_text", "strip_tags",
    "IP_PORT_PATTERN",
]