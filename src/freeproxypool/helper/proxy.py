# -*- coding: utf-8 -*-
"""代理数据模型。

v0.1.0 变更：旧版 :class:`helper.proxy.Proxy` 用 13 个手写 property
的普通类实现 ``to_dict`` / ``createFromJson``，字段一多就极易漏写，
且构造参数与序列化化字段两处维护，容易漂移。这里改为
:class:`dataclasses.dataclass` + 显式的 :meth:`from_dict` / :meth:`to_dict`。

v0.1.0 评分模型优化（B-01/B-02/B-14）：
- 废弃手工累加 score 模型，改为 ``error_rate + ewma_speed`` 复合分；
- 新增 ``error_count`` / ``ewma_speed`` 字段；
- ``mark_pass`` / ``mark_fail`` 保留原签名以兼容外部调用，内部忽略 score 参数；
- ``should_evict`` 增加基于错误率的淘汰维度。

行为保持兼容：
- ``source`` 对外仍是 ``"a/b"`` 形式的字符串，内部存 list；
- ``to_dict`` 的键名与旧版完全一致，不影响已有 Redis 数据与前端。
"""
import json
from dataclasses import asdict, dataclass, field
from typing import List, Optional

#: 代理传输协议。
#:
#: - ``http``：普通 HTTP 代理（历史数据一律按这个处理，向后兼容）；
#: - ``socks4``：SOCKS4 代理（域名在本地解析后按 IP 连接）；
#: - ``socks5``：SOCKS5 代理（域名由代理远程解析）。
#:
#: 注意与 :attr:`Proxy.https` 的区别：``protocol`` 表达**传输协议**
#: （这条代理用什么协议跟客户端说话），``https`` 表达**能力**
#: （这条代理能不能用于 HTTPS 目标）。两者正交，不可合并。
PROTOCOLS = ("http", "socks4", "socks5")


def normalize_protocol(value) -> str:
    """归一化协议名。

    空值 / 未知值一律按 ``http`` 处理 —— 历史数据没有这个字段，
    缺省必须是 HTTP，否则升级瞬间全池代理都会被判成"协议不受支持"。
    """
    text = str(value or "").strip().lower()
    return text if text in PROTOCOLS else "http"


@dataclass
class Proxy:
    """一条代理记录。"""

    proxy: str
    fail_count: int = 0
    region: str = ""
    anonymous: str = ""
    ip_type: str = "unknown"      # B-07: datacenter / residential / unknown
    dnsbl: bool = False           # B-08: 是否被 DNSBL 列入
    source: List[str] = field(default_factory=list)
    check_count: int = 0
    error_count: int = 0
    ewma_speed: Optional[float] = None
    streak: int = 0
    last_status: bool = False
    last_time: str = ""
    https: bool = False
    score: int = 0
    speed: float = 0.0
    ttl: int = 0
    #: 传输协议（http / socks4 / socks5），见 :data:`PROTOCOLS`
    protocol: str = "http"

    def __post_init__(self):
        if isinstance(self.source, str):
            self.source = [s for s in self.source.split("/") if s]
        self.protocol = normalize_protocol(self.protocol)

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        data = asdict(self)
        data["source"] = "/".join(self.source)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "Proxy":
        if not isinstance(data, dict):
            raise TypeError("代理数据必须是 dict")
        return cls(
            proxy=str(data.get("proxy", "")),
            fail_count=_as_int(data.get("fail_count")),
            region=str(data.get("region") or ""),
            anonymous=str(data.get("anonymous") or ""),
            ip_type=str(data.get("ip_type") or "unknown"),
            dnsbl=bool(data.get("dnsbl")),
            source=str(data.get("source") or ""),
            check_count=_as_int(data.get("check_count")),
            error_count=_as_int(data.get("error_count")),
            ewma_speed=_as_opt_float(data.get("ewma_speed")),
            streak=_as_int(data.get("streak")),
            last_status=bool(data.get("last_status")),
            last_time=str(data.get("last_time") or ""),
            https=bool(data.get("https")),
            score=_as_int(data.get("score")),
            speed=_as_float(data.get("speed")),
            ttl=_as_int(data.get("ttl")),
            protocol=normalize_protocol(data.get("protocol")),
        )

    @classmethod
    def from_json(cls, raw: str) -> "Proxy":
        return cls.from_dict(json.loads(raw))

    # ---------- 行为 ----------

    def add_source(self, name: str) -> None:
        """追加采集来源并去重（保持稳定顺序，便于前后端展示一致）。"""
        if name and name not in self.source:
            self.source.append(name)

    def mark_pass(self, https_ok: bool, speed: float,
                  score_max: int = 0, score_step: int = 0) -> None:
        """标记一次校验通过。

        v0.1.0 变更：保留 ``score_max`` / ``score_step`` 参数仅为了兼容外部调用，
        内部不再用于手工累加评分（改由 :meth:`_recalc_score` 派生）。
        """
        self.check_count += 1
        self.last_status = True
        self.https = https_ok
        self.speed = round(float(speed or 0.0), 4)
        if self.fail_count > 0:
            self.fail_count -= 1

    def mark_fail(self, score_min: int = 0, score_step: int = 0) -> None:
        """标记一次校验失败。

        v0.1.0 变更：保留 ``score_min`` / ``score_step`` 参数仅为了兼容外部调用，
        内部不再用于手工扣分。
        """
        self.check_count += 1
        self.last_status = False
        self.fail_count += 1
        self.error_count += 1

    def should_evict(self, max_fail_count: int,
                     max_error_rate: float = 0.5,
                     min_checks: int = 10) -> bool:
        """是否应当淘汰。

        v0.1.0 变更：新增基于 ``error_count / check_count`` 的错误率淘汰维度；
        不再依赖 ``score <= score_min``（复合分模型下该条件已无意义）。
        """
        if max_fail_count > 0 and self.fail_count > max_fail_count:
            return True
        if min_checks > 0 and self.check_count >= min_checks:
            return (self.error_count / self.check_count) > max_error_rate
        return False

    def _recalc_score(self) -> int:
        """基于错误率 + EWMA 延迟重新计算复合分。"""
        if self.check_count == 0:
            return 0
        error_rate = self.error_count / self.check_count
        avg_speed = self.ewma_speed if self.ewma_speed is not None else 0.0
        return int(max(0.0, round(
            (1.0 - error_rate) * 100.0 - min(avg_speed, 30.0)
        )))


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_opt_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def new_proxy(proxy_str: str, source: str, score_init: int = 0) -> Proxy:
    """构造一条新入池代理（应用 PROXY_SCORE_INIT）。"""
    return Proxy(proxy=proxy_str, source=[source], score=score_init)


# ---------- 延迟分层（借鉴 Thordata，见 docs/09 P0-3） ----------

#: 三档延迟标签 + unknown（从未通过校验、没有延迟样本）
TIERS = ("fast", "medium", "slow", "unknown")


# ---------- 属性稳定合并（借鉴 Thordata，见 docs/09 P2-3） ----------


def keep_first(current: str, incoming: str) -> str:
    """属性稳定合并（其一）：**已有值优先**。

    借鉴 Thordata 的"第一个提供该 hostport 的源胜出"（docs/09 P2-3）：
    国家码在代理存活期内基本不变，一旦有值就不再被后来的结果改写 ——
    避免同一代理的属性在源之间反复横跳。

    用于 **region**：国家码同时也是地域索引的分组键，横跳会让
    ``?area=CN`` 的结果集忽大忽小。
    """
    return current if current else incoming


def keep_known(current: str, incoming: str) -> str:
    """属性稳定合并（其二）：**空值不覆盖非空值**。

    匿名度、机型这类字段是"能力"，会真实变化（代理被换成透明的、
    IP 段重新划分），因此以**最新一次有效测量**为准；但一次探测失败
    （出口 IP 取不到、ASN 库读不出来）返回的空值**不能**把上一轮
    已经知道的值抹成空 —— 否则代理会因为一次网络抖动就从
    ``?area=CN`` 的结果里消失、或丢掉已判定的匿名度。

    与 :func:`keep_first` 的区别：这里空值让位、非空值覆盖；
    那里已有值一律不让位。
    """
    return incoming if incoming else current


def tier_thresholds(fast_ms=None, medium_ms=None):
    """取当前生效的分层阈值（毫秒）。

    阈值是**可热更新的配置项**（``TIER_FAST_MS`` / ``TIER_MEDIUM_MS``），
    因此运行时一律以配置中心为准；``setting`` 只是默认值。
    配置中心不可用时回落 ``setting`` —— 此时分层退化为固定口径，
    不影响可用性（延迟分层只是筛选维度，不是安全边界）。
    """
    from .. import setting

    fast = medium = None
    try:
        from .. import config_center as cc
        fast = cc.get("TIER_FAST_MS")
        medium = cc.get("TIER_MEDIUM_MS")
    except Exception:
        fast = medium = None

    if fast is None:
        fast = setting.TIER_FAST_MS
    if medium is None:
        medium = setting.TIER_MEDIUM_MS
    if fast_ms is not None:
        fast = fast_ms
    if medium_ms is not None:
        medium = medium_ms

    try:
        fast = int(fast)
    except (TypeError, ValueError):
        fast = setting.TIER_FAST_MS
    try:
        medium = int(medium)
    except (TypeError, ValueError):
        medium = setting.TIER_MEDIUM_MS
    # 阈值顺序被配反（fast >= medium）时，fast 档会吞掉 medium 档，
    # 出现"medium 永远为空"的畸形分布。这里兜底夹紧，保证两档都可命中。
    if fast >= medium:
        fast = max(1, medium - 1)
    return fast, medium


def tier_of(speed_seconds, fast_ms: int = None, medium_ms: int = None) -> str:
    """把延迟（**秒**）映射为分层标签。

    - ``< fast_ms``   → ``fast``
    - ``< medium_ms`` → ``medium``
    - 其余            → ``slow``
    - 无样本 / 非正数 → ``unknown``

    阈值单位为毫秒（与配置项一致），内部换算；缺省读配置中心。
    """
    fast, medium = tier_thresholds(fast_ms, medium_ms)
    if speed_seconds is None:
        return "unknown"
    try:
        seconds = float(speed_seconds)
    except (TypeError, ValueError):
        return "unknown"
    if seconds <= 0:
        return "unknown"
    ms = seconds * 1000.0
    if ms < fast:
        return "fast"
    if ms < medium:
        return "medium"
    return "slow"


def effective_speed(proxy_dict: dict):
    """取代理的有效延迟：优先 EWMA，回落最近一次延迟。

    EWMA 平滑了单次抖动，作为分层依据比 ``speed`` 稳定。
    """
    if not isinstance(proxy_dict, dict):
        return None
    ewma = proxy_dict.get("ewma_speed")
    if ewma is not None:
        try:
            value = float(ewma)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    speed = proxy_dict.get("speed")
    if speed is None:
        return None
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def tier_of_proxy(proxy_dict: dict, fast_ms: int = None, medium_ms: int = None) -> str:
    """按代理元数据判定延迟分层。"""
    return tier_of(effective_speed(proxy_dict), fast_ms, medium_ms)


__all__ = ["Proxy", "new_proxy", "tier_of", "tier_of_proxy", "effective_speed",
           "tier_thresholds", "TIERS", "PROTOCOLS", "normalize_protocol",
           "keep_first", "keep_known"]
