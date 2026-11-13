# -*- coding: utf-8 -*-
"""网关依赖：来源 IP 解析、限流、API Key 鉴权、管理会话、CSRF。

本模块集中所有**与请求处理顺序强相关**的安全逻辑，
路由函数只负责"拿到数据、组装响应"，不重复实现安全判定。
"""
import ipaddress
import time
from typing import List, Optional, Tuple

from fastapi import Request

from .. import config_center as cc
from .. import setting
from ..core.errors import (AuthError, CsrfError, RateLimitError,
                         ValidationError)
from ..core.security import constant_time_compare
from ..core.validators import validate_cidr_list

# ---------- 复用限制 ----------
#: 单 IP 冷却窗口内最多分发次数
REUSE_MAX = 3
#: 复用计数 Key
REUSE_KEY = "fpp:reuse:%s"
#: 一次最多清理多少个复用计数 Key（避免大池下一次 DEL 上万 key 阻塞 Redis）
CLEAR_REUSE_BATCH = 500

# ---------- 限流 ----------
RATE_KEY = "fpp:ratelimit:%s:%d"


# ================= 来源 IP 解析 =================

def _peer_ip(request: Request) -> str:
    """直连来源地址（TCP 层对端）。"""
    return request.client.host if request.client else ""


def is_trusted_proxy(request: Request) -> bool:
    """判断直连来源是否属于 ``TRUSTED_PROXIES`` 配置的网段。"""
    try:
        networks = validate_cidr_list(cc.get("TRUSTED_PROXIES", []))
    except ValidationError:
        return False
    if not networks:
        return False
    peer = _peer_ip(request)
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in networks)


def client_ip(request: Request) -> str:
    """获取可信的客户端 IP。

    v0.1.0 安全修复：旧版无条件信任 ``X-Forwarded-For`` 的第一个值。
    由于 Nginx 使用 ``$proxy_add_x_forwarded_for``（**追加**而非覆盖），
    攻击者只要自带一个 XFF 头，就能让每次请求的"来源 IP"都不一样 ——
    登录失败锁定因此形同虚设，可无限次爆破管理密码。

    现在只有当直连来源落在 ``TRUSTED_PROXIES`` 内时才采信 XFF，
    默认该配置为空 ⇒ **完全不采信**，一律使用 TCP 对端地址。
    """
    if not is_trusted_proxy(request):
        return _peer_ip(request)
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        try:
            ipaddress.ip_address(real_ip)
            return real_ip
        except ValueError:
            pass
    return _peer_ip(request)


# ================= 限流 =================

def identity_of(request: Request) -> str:
    """限流标识：优先 API Key（即便校验未通过），其次客户端 IP。"""
    key = request.headers.get("API-KEY") or request.query_params.get("key")
    if key:
        return "key:%s" % key[:128]
    return "ip:%s" % client_ip(request)


def check_rate_limit(request: Request) -> bool:
    """QPS 限流。返回 True 表示应当拒绝。

    v0.1.0 变更：Redis 异常时**不再直接放行**，而是退回进程内限流。
    旧版 ``return False``（放行）意味着 Redis 一抖动，整个网关就变成
    无限制状态，等于把限流这个安全边界也一起关掉了。

    v0.1.0 正式版补充修复：``get_secure`` 读不到配置时旧代码把 qps 置 0，
    ``qps <= 0`` 分支直接 ``return False`` —— **恰好把限流整个关掉**，
    与上面那句注释想要的行为正好相反。现在读不到就回落到 schema 默认值，
    后面的 INCR 失败也会走进程内限流，任何路径上都有上界。
    """
    try:
        qps = int(cc.get_secure("RATE_LIMIT_QPS"))
    except Exception:
        qps = int(setting.RATE_LIMIT_QPS)
    if qps <= 0:
        return False

    identity = identity_of(request)
    bucket = int(time.time())
    key = RATE_KEY % (identity, bucket)
    try:
        conn = cc.get_redis()
        pipe = conn.pipeline()
        pipe.incr(key)
        pipe.expire(key, 2)
        current, _ = pipe.execute()
        return int(current) > qps
    except Exception:
        return _local_rate_limited(identity, qps, bucket)


_local_buckets: dict = {}


def _local_rate_limited(identity: str, qps: int, bucket: int) -> bool:
    """进程内降级限流（Redis 不可用时兜底，保证仍有上界）。"""
    _prune_local_buckets(bucket)
    count = _local_buckets.get(identity, 0) + 1
    _local_buckets[identity] = count
    return count > qps


def _prune_local_buckets(current_bucket: int) -> None:
    if len(_local_buckets) < 4096:
        return
    for key in [k for k, v in list(_local_buckets.items()) if v < current_bucket - 2]:
        _local_buckets.pop(key, None)


# ================= 依赖：API Key 鉴权 =================

def require_api_key(request: Request) -> None:
    """API Key 鉴权。

    三种情形：

    * 配置了 Key → 必须匹配；
    * 未配置 Key 且读取成功 → 放行（内网部署的显式选择），但会打告警日志；
    * **配置读取失败（Redis 不可用）→ 抛 503 拒绝**。

    最后一条是 v0.1.0 修复的核心：旧版读取失败会回落到空默认值，
    等于自动关闭鉴权。
    """
    try:
        configured = cc.get_secure("API_KEY")
    except Exception as exc:
        raise _unavailable("API Key 配置读取失败，已拒绝请求", exc)

    configured = str(configured or "")
    if not configured:
        return

    provided = request.headers.get("API-KEY") or request.query_params.get("key")
    if not provided or not constant_time_compare(provided, configured):
        raise AuthError("无效的 API Key", detail="api key mismatch")


def _unavailable(message: str, exc: Exception) -> Exception:
    from ..core.errors import BackendUnavailable
    return BackendUnavailable(message, detail=type(exc).__name__)


def metrics_api_key_ok(request: Request) -> bool:
    """``/metrics`` 鉴权：未开启 METRICS_AUTH 或已配置 API Key 时要求校验。"""
    try:
        required = bool(cc.get_secure("METRICS_AUTH"))
    except Exception:
        # 读不到就按"需要鉴权"处理，宁可挡住监控也不能泄露池规模
        required = True
    if not required:
        return True
    try:
        configured = str(cc.get_secure("API_KEY") or "")
    except Exception:
        return False
    if not configured:
        # 压根没配 API Key，开启 metrics 鉴权没有意义，放行但不再校验
        return True
    provided = request.headers.get("API-KEY") or request.query_params.get("key")
    return bool(provided) and constant_time_compare(provided, configured)


# ================= 依赖：管理会话 =================

def session_token(request: Request) -> str:
    """提取会话令牌（请求头优先，回退 query 以兼容旧前端）。"""
    return (request.headers.get("X-Admin-Token")
            or request.query_params.get("token")
            or request.cookies.get("fpp_admin_token")
            or "")


def require_admin_auth(request: Request) -> str:
    """管理会话鉴权，返回会话令牌。

    v0.1.0 修复：旧版未登录时抛的是 ``HTTPException(status_code=400)``，
    对外表现为 400 Bad Request —— 语义错误（应为 401），
    且会让网关侧的审计/告警把"未登录"误统计成"客户端参数错误"。
    """
    token = session_token(request)
    if not token or not cc.verify_session(token):
        raise AuthError("未登录或会话已过期")
    return token


def require_admin_csrf(request: Request) -> None:
    """写操作校验 CSRF 令牌（fail-closed）。"""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    token = session_token(request)
    csrf = request.headers.get("X-CSRF-Token", "")
    if not token or not csrf:
        raise CsrfError("缺少 CSRF Token")
    try:
        expected = cc.csrf_for(token)
    except Exception:
        expected = ""
    if not expected or not constant_time_compare(csrf, expected):
        raise CsrfError("CSRF 校验失败")


def require_admin_enabled() -> None:
    """管理后台开关校验。

    v0.1.0 安全修复：旧版只在 ``/admin`` 页面检查 ``ADMIN_ENABLED``，
    ``/admin/api/*`` 全部接口**不检查** —— 关掉后台后，接口依然完全可用，
    开关形同虚设。现在所有后台接口统一走这里。
    """
    try:
        enabled = bool(cc.get_secure("ADMIN_ENABLED"))
    except Exception as exc:
        raise _unavailable("配置服务不可用", exc)
    if not enabled:
        raise AuthError("管理后台未启用", detail="ADMIN_ENABLED is off")


# ================= 依赖：调用 =================

def enforce_rate_limit(request: Request) -> None:
    """限流依赖（供路由以 ``Depends`` 方式挂载）。"""
    if check_rate_limit(request):
        # 计数器必须有真实写入方，否则 Grafana 上这条曲线恒为 0、
        # 告警规则 FppRateLimitedHeavy 永远不触发（旧版定义未埋点）。
        from .. import metrics
        metrics.inc_counter(metrics.gateway_rate_limited)
        raise RateLimitError()


# ================= 分发策略 =================

def pick_candidates(store, https: bool, region: Optional[str],
                    strategy: str, limit: int, anon: Optional[str] = None,
                    exclude_dnsbl: bool = False,
                    tier: Optional[str] = None,
                    protocol: Optional[str] = None) -> list:
    """按策略取候选代理（已含 https / region / 匿名度 / 延迟分层 / 协议过滤）。

    v0.1.0 性能修复：旧版对 ``getAll()`` 返回的**全池**做排序，
    池子上万时每次 ``/get/`` 都要拉全量 + 排序 + 反序列化。
    现在先用 ZSet 按评分取前 ``limit * 2`` 条候选，
    过滤后截断为 ``limit``，把单次请求的复杂度从 O(N log N) 降到 O(limit)。

    v0.1.0 新增：``weighted`` 策略按 ``(1 - error_rate) / (1 + speed)`` 加权随机；
    ``anon`` 过滤支持三段式匿名度；``exclude_dnsbl`` 排除声誉黑名单代理；
    ``tier`` 按延迟分层（fast / medium / slow / unknown）过滤 —— 借鉴 docs/09 P0-3；
    ``protocol`` 按传输协议（http / socks4 / socks5）过滤 —— 借鉴 docs/09 P0-1。

    注意：tier 与 protocol 都是在**候选集内**后置过滤的，不是索引级过滤。
    候选集是按评分取的前 ``limit * 2`` 条，因此过滤可能把候选掏空。
    这是刻意的取舍 —— 为 tier 单独建索引成本更高，
    当前池规模下后置过滤的召回损失可以接受。

    v0.1.0（P1-1）：带 ``region`` 时走**地域索引**
    （``{table}:region:{CODE}``），取候选从 O(全池) 降到 O(该地域)。
    索引窗口被后置过滤掏空时**显式全表兜底一次** —— 旧实现直接取全量
    地域候选，不存在这个问题；索引化后若不补偿，
    ``?area=CN&tier=fast`` 会因为窗口里恰好没有 fast 代理而返回空。
    """
    import random

    limit = max(1, int(limit))

    def _filter(candidates):
        """在候选集内做后置过滤（匿名度 / DNSBL / 延迟分层 / 传输协议）。"""
        if anon:
            candidates = [c for c in candidates if c.get("anonymous") == anon]
        # B-08: 排除 DNSBL 列入代理
        if exclude_dnsbl:
            candidates = [c for c in candidates if not c.get("dnsbl")]
        # 延迟分层过滤（借鉴 docs/09 P0-3）
        if tier:
            from ..helper.proxy import tier_of_proxy, tier_thresholds
            fast_ms, medium_ms = tier_thresholds()
            candidates = [c for c in candidates
                          if tier_of_proxy(c, fast_ms, medium_ms) == tier]
        # 传输协议过滤（借鉴 docs/09 P0-1）
        if protocol:
            from ..helper.proxy import normalize_protocol
            candidates = [c for c in candidates
                          if normalize_protocol(c.get("protocol")) == protocol]
        return candidates

    if region:
        # 地区筛选走地域索引（借鉴 docs/09 P1-1）：旧实现是全表扫描，
        # 池子上万时每次请求都是 O(N)。索引不可用时由 get_by_region 降级全表。
        candidates = _filter(store.get_by_region(region, https=https,
                                                limit=limit * 10))
        if not candidates:
            # 候选窗口被后置过滤掏空 → 全表兜底一次（严格匹配，绝不放宽地域）。
            # 旧实现直接取全量地域候选，不存在这个问题；索引化后必须显式补偿，
            # 否则 "?area=CN&tier=fast" 会因为窗口里恰好没有 fast 代理而返回空。
            candidates = _filter(store.get_all(https, region))[:limit * 10]
    else:
        candidates = _filter(store.top_by_score(limit=limit * 2, https=https))

    if not candidates:
        return []

    if strategy == "random":
        random.shuffle(candidates)
    elif strategy == "roundrobin":
        index = _roundrobin_index(len(candidates))
        candidates = candidates[index:] + candidates[:index]
    elif strategy == "weighted":
        # B-10: 按 (1 - error_rate) / (1 + speed) 加权随机
        weights = []
        for c in candidates:
            check_count = max(1, int(c.get("check_count") or 0))
            error_count = int(c.get("error_count") or 0)
            speed = float(c.get("ewma_speed") or c.get("speed") or 0.5)
            error_rate = error_count / check_count
            w = max(0.01, (1.0 - error_rate) / (1.0 + speed))
            weights.append(w)
        # random.choices 可能重复，去重保序
        chosen = random.choices(candidates, weights=weights, k=len(candidates))
        seen = set()
        result = []
        for item in chosen:
            key = item["proxy"]
            if key not in seen:
                seen.add(key)
                result.append(item)
        candidates = result
    else:  # latency：已按评分降序，无需再排
        pass

    return candidates[:limit]


def _roundrobin_index(length: int) -> int:
    try:
        return int(cc.get_redis().incr("fpp:rr:index")) % length
    except Exception:
        return 0


def acquire_reuse(proxy_str: str) -> bool:
    """尝试占用一次分发配额。返回 True 表示允许分发。

    v0.1.0 修复：旧版在遍历候选时就对**每一个被跳过的候选**执行了 ``INCR``，
    导致计数被"扫描行为"污染 —— 一个热门代理即使一次都没被分发出去，
    也会因为被别人反复扫描而迅速耗尽配额。
    现在只在**真正选中**某个代理时才 +1。
    """
    window = int(cc.get("IP_REUSE_WINDOW", 60))
    if window <= 0:
        return True
    key = REUSE_KEY % proxy_str
    try:
        conn = cc.get_redis()
        pipe = conn.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        current, _ = pipe.execute()
        return int(current) <= REUSE_MAX
    except Exception:
        return True


def release_reuse(proxy_str: str) -> None:
    """释放一次分发配额（批量场景回滚已占用但未使用的计数）。"""
    try:
        cc.get_redis().delete(REUSE_KEY % proxy_str)
    except Exception:
        pass


def clear_reuse_batch(proxy_list) -> int:
    """批量清理复用计数，返回清理条数（按批次上限裁剪）。"""
    keys = [REUSE_KEY % p for p in proxy_list[:CLEAR_REUSE_BATCH]]
    if not keys:
        return 0
    try:
        return int(cc.get_redis().delete(*keys) or 0)
    except Exception:
        return 0


def select_one(store, https: bool, region: Optional[str], strategy: str,
               limit: int, anon: Optional[str] = None,
               exclude_dnsbl: bool = False,
               tier: Optional[str] = None,
               protocol: Optional[str] = None) -> Optional[dict]:
    """选出一个可分发的代理（遵守复用限制）。"""
    candidates = pick_candidates(store, https, region, strategy, limit,
                                 anon, exclude_dnsbl, tier, protocol)
    if not candidates:
        return None
    for item in candidates:
        if acquire_reuse(item["proxy"]):
            return item
    # 全部超限：清理一批计数后重试一次
    clear_reuse_batch([c["proxy"] for c in candidates])
    for item in candidates:
        if acquire_reuse(item["proxy"]):
            return item
    return None


def select_many(store, https: bool, region: Optional[str], strategy: str,
                limit: int, count: int, anon: Optional[str] = None,
                exclude_dnsbl: bool = False,
                tier: Optional[str] = None,
                protocol: Optional[str] = None) -> List[dict]:
    """批量选取多个互不相同的代理。"""
    candidates = pick_candidates(store, https, region, strategy, limit,
                                 anon, exclude_dnsbl, tier, protocol)
    picked: List[dict] = []
    taken = set()
    for item in candidates:
        if item["proxy"] in taken:
            continue
        if acquire_reuse(item["proxy"]):
            picked.append(item)
            taken.add(item["proxy"])
        if len(picked) >= count:
            break
    if not picked and candidates:
        clear_reuse_batch([c["proxy"] for c in candidates])
        for item in candidates:
            if item["proxy"] not in taken and acquire_reuse(item["proxy"]):
                picked.append(item)
                taken.add(item["proxy"])
            if len(picked) >= count:
                break
    return picked


def is_pool_low(store) -> bool:
    """池规模是否低于枯竭阈值。"""
    try:
        threshold = int(cc.get("POOL_LOW_THRESHOLD", 50))
        return store.get_count()["total"] < threshold
    except Exception:
        return False


def annotate(data: dict, pool_low: bool) -> dict:
    """在响应中标注池枯竭状态。"""
    if pool_low:
        data["pool_low"] = True
    return data


__all__ = [
    "REUSE_MAX", "client_ip", "is_trusted_proxy", "identity_of",
    "check_rate_limit", "enforce_rate_limit",
    "require_api_key", "metrics_api_key_ok",
    "session_token", "require_admin_auth", "require_admin_csrf", "require_admin_enabled",
    "pick_candidates", "acquire_reuse", "release_reuse", "clear_reuse_batch",
    "select_one", "select_many", "is_pool_low", "annotate",
]