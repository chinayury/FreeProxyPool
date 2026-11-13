# -*- coding: utf-8 -*-
"""代理三层递进校验。

    第一层  TCP 连通      —— asyncio.open_connection 探测端口，不通直接拉黑
    第二层  代理功能      —— 经代理请求 HTTP 目标，记录延迟与出口 IP
                             再探测 CONNECT 隧道（HTTPS 能力）
    第三层  多轮复检      —— raw 阶段要求连续 N 轮全通过才允许入池

并发由 ``CHECK_CONCURRENCY`` 控制，全部异步。

v0.1.0 评分模型优化（B-01/B-02/B-14）：
- 引入 EWMA 延迟 + error_rate 复合分；
- ``_check_one`` 改为 ``try/finally`` + 单一 ``_record()`` 收口埋点；
- Redis 侧新增 speed history 定长 List。

v0.1.0 地域/机型/声誉增强（B-06/B-07/B-08）：
- 地域解析优先走 GeoIP mmdb（B-06），失败回落 HTTP；
- 校验通过后异步填充 ASN ip_type（B-07）；
- 校验通过后异步填充 DNSBL 声誉标记（B-08）。

v0.1.0 SOCKS4/5 支持（P0-1）：
- 按 :attr:`helper.proxy.Proxy.protocol` 选择探针会话 ——
  HTTP 代理复用全局会话（请求带 ``proxy=`` 参数），
  SOCKS 代理用 ``aiohttp_socks.ProxyConnector`` 绑定该代理地址
  （请求**不能**再带 ``proxy=``，否则与连接器冲突）；
- 每个 SOCKS 代理一个独立会话，用完即关（连接器不可复用）；
- 缺少 ``aiohttp-socks`` 依赖时记 ``fail_unsupported``（硬失败），
  不静默把 SOCKS 代理当成"没通过"混进软失败里。
"""
import asyncio
import atexit
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from aiohttp import ClientSession, ClientTimeout, TCPConnector

from .. import config_center as cc
from ..core.iputil import parse_host_port
from ..core.logging import get_logger
from .asn import lookup_asn
from .proxy import Proxy, keep_first, keep_known
from .region import get_resolver, judge_anonymous, parse_origin_ip
from .reputation import get_checker

log = get_logger("checker", to_file=False)

# ---------- B-02: EWMA 超参 ----------
EWMA_ALPHA: float = 0.3
SPEED_HISTORY_MAX: int = 100

_LOOP: Optional[asyncio.AbstractEventLoop] = None
_SESSION: Optional[ClientSession] = None


def origin_probe_url(raw: str) -> str:
    """把校验目标规整到"能返回出口 IP + 响应头"的路径。

    httpbin 的站点根路径返回 HTML 而非 JSON，``/ip`` 只返回 origin，
    ``/get`` 同时返回 origin 与 headers，用于匿名度判定。
    自建目标则原样使用。
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        return base
    if base.lower().endswith("httpbin.org"):
        return base + "/get"
    return raw


def get_loop() -> asyncio.AbstractEventLoop:
    """获取（并复用）进程内的事件循环。

    v0.1.0 修复：旧版每轮校验都 ``new_event_loop()`` + ``run_until_complete``
    + ``close()``，既无法复用连接，又会在 close 时对未完成 task 打出
    "Task was destroyed but it is pending" 告警。现在整个进程共用一个 loop。
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP


async def _get_session(concurrency: int) -> ClientSession:
    """复用全局 aiohttp 会话（连接池跨批次共享，避免反复 TLS 握手）。"""
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        connector = TCPConnector(limit=concurrency, ttl_dns_cache=300, ssl=False)
        _SESSION = ClientSession(
            connector=connector,
            timeout=ClientTimeout(total=60),
            trust_env=False,   # 不走环境代理，避免校验流量被绕出去
        )
    return _SESSION


async def close_session() -> None:
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        try:
            await _SESSION.close()
        except Exception:
            pass
    _SESSION = None


def _shutdown() -> None:
    """进程退出时关闭复用中的 aiohttp 会话。

    不注册的话，每次进程结束都会打出 ``Unclosed client session`` 告警，
    在长时间反复重启的容器编排里会持续刷屏，掩盖真正的告警。
    """
    try:
        if _LOOP is not None and not _LOOP.is_closed() and _SESSION is not None:
            _LOOP.run_until_complete(close_session())
    except Exception:
        pass


atexit.register(_shutdown)


async def tcp_check(ip: str, port: int, timeout: float) -> bool:
    """第一层：TCP 连通性。"""
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout)
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


async def http_probe(session: ClientSession, proxy_str: str,
                      url: str, timeout: ClientTimeout,
                      use_proxy_arg: bool = True) -> Tuple[bool, float, str, dict]:
    """第二层：经代理请求 HTTP 目标。返回 ``(是否成功, 延迟秒, 出口IP, 响应头)``。

    ``use_proxy_arg=False`` 时**不传** ``proxy=`` 参数 —— SOCKS 走的是会话
    自身的连接器（``ProxyConnector`` 构造时就绑定了代理地址），
    再叠一个 ``proxy=`` 会与之冲突。
    """
    kwargs = {"proxy": "http://%s" % proxy_str} if use_proxy_arg else {}
    started = time.perf_counter()
    try:
        async with session.get(url, timeout=timeout,
                               allow_redirects=True, **kwargs) as response:
            if response.status != 200:
                return False, 0.0, "", {}
            body = await response.text()
            headers = {k.lower(): v for k, v in response.headers.items()}
            return True, time.perf_counter() - started, parse_origin_ip(body), headers
    except Exception:
        return False, 0.0, "", {}


async def https_probe(session: ClientSession, proxy_str: str,
                       url: str, timeout: ClientTimeout,
                       use_proxy_arg: bool = True) -> bool:
    """第二层补充：探测代理是否支持 CONNECT 隧道。

    对 SOCKS5 而言这个能力是**天然具备**的（SOCKS5 能连任意 TCP 目标），
    但探测方式不同 —— 走 SOCKS 连接器而非 HTTP CONNECT，
    因此 ``use_proxy_arg`` 必须为 False。
    """
    kwargs = {"proxy": "http://%s" % proxy_str} if use_proxy_arg else {}
    try:
        async with session.get(url, timeout=timeout,
                               allow_redirects=True, **kwargs) as response:
            return response.status == 200
    except Exception:
        return False


# ================= SOCKS 会话 =================

#: 硬失败：代理本身不可用（地址非法 / 端口不通 / 协议不受支持）
HARD_FAILURES = ("fail_format", "fail_tcp", "fail_unsupported")


class UnsupportedProtocolError(Exception):
    """运行环境不支持该代理协议（例如缺少 ``aiohttp-socks`` 依赖）。

    这是**部署问题**而不是代理本身的问题，但它同样意味着这条代理
    当前不可用 —— 单独用 ``fail_unsupported`` 标记，区别于"探到了但没通"，
    便于运维一眼看出是依赖没装而不是源站被墙。
    """


async def new_socks_session(proxy_str: str, protocol: str,
                            timeout: int = 15) -> ClientSession:
    """为单个 SOCKS 代理建立专用 aiohttp 会话。

    ``aiohttp_socks.ProxyConnector`` 在构造时就绑定了代理地址，
    **无法像 HTTP 代理那样用 ``session.get(proxy=...)`` 逐请求切换**，
    因此每个 SOCKS 代理需要一个独立会话 —— 用完即关（见
    :func:`probe_session`），避免连接池无限增长。

    ``rdns`` 保持各协议的库默认：

    * SOCKS5 → ``True``，域名交给代理远程解析（不泄漏本地 DNS）；
    * SOCKS4 → ``False``，本地解析后按 IP 连接（普通 SOCKS4 服务器
      不支持 4a 域名扩展，强行发域名会被拒）。
    """
    try:
        from aiohttp_socks import ProxyConnector
    except ImportError as exc:
        raise UnsupportedProtocolError(
            "缺少 aiohttp-socks 依赖，无法校验 SOCKS 代理") from exc

    scheme = "socks5" if protocol == "socks5" else "socks4"
    connector = ProxyConnector.from_url(
        "%s://%s" % (scheme, proxy_str),
        limit=4, ttl_dns_cache=300, ssl=False,
    )
    return ClientSession(
        connector=connector,
        timeout=ClientTimeout(total=timeout),
        trust_env=False,   # 不走环境代理，避免校验流量被绕出去
    )


@asynccontextmanager
async def probe_session(proxy_str: str, protocol: str,
                        shared: ClientSession, timeout: int = 15):
    """按协议准备探针会话，产出 ``(session, use_proxy_arg)``。

    * **HTTP 代理**：共用全局会话，请求时带 ``proxy=`` 参数；
    * **SOCKS 代理**：用该代理自己的连接器，请求时**不带** ``proxy=``
      （连接器本身已完成路由，再叠 ``proxy=`` 会冲突）。
    """
    if protocol in ("socks4", "socks5"):
        session = await new_socks_session(proxy_str, protocol, timeout)
        try:
            yield session, False
        finally:
            try:
                await session.close()
            except Exception:
                pass
    else:
        yield shared, True


# ================= B-14: 单一 _record 收口埋点 =================


def _record(proxy: Proxy, outcome: str, speed: float,
            https_ok: bool, opts: dict) -> None:
    """统一记录校验结果（在 ``finally`` 块中调用，确保任何代码路径都落账）。

    outcome 取值：
    - ``pass``：HTTP 多轮全通过
    - ``fail_format``：地址格式异常
    - ``fail_tcp``：TCP 端口不通
    - ``fail_http``：HTTP 探针失败
    - ``fail_unsupported``：运行环境不支持该协议（缺 aiohttp-socks）
    """
    store = opts["store"]
    # 供 _run 判断本次是硬失败（已移出池）还是软失败（可能仍留在池内）。
    # 这是个临时属性，不属于序列化字段，不会进 Redis。
    proxy._check_outcome = outcome

    if outcome == "pass":
        proxy.mark_pass(https_ok, speed)
        # B-05: streak 连续存活天数（check_count 近似为"被检验次数"）
        proxy.streak = int(proxy.streak or 0) + 1
        # B-02: EWMA 延迟（超时样本 speed=0，不更新均值）
        if speed > 0:
            if proxy.ewma_speed is None:
                proxy.ewma_speed = speed
            else:
                proxy.ewma_speed = (
                    EWMA_ALPHA * speed + (1.0 - EWMA_ALPHA) * proxy.ewma_speed
                )
        # B-02: 延迟样本写入 Redis 定长 List（实现收敛到 db.redisClient）
        store.update_speed_history(proxy.proxy, speed, SPEED_HISTORY_MAX)
        # P0-2: 校验通过 → 清零连续失败计数并解除退避。
        # 代理"活过来了"就该立刻重新可用，否则它会被退避窗口挡住，
        # 直到窗口自然过期才有机会重新参与分发。
        store.clear_black_fail(proxy.proxy)
    else:
        proxy.mark_fail()
        proxy.streak = 0
        # P0-2: 任何校验失败都进入死代理退避窗口，跳过时长相随连续失败
        # 次数递增（1h → 2h → 6h → 24h）。
        # 关键是 **fail_http 也要拉黑** —— 改造前只有 TCP/格式失败才拉黑，
        # 于是"端口通但请求不通"的代理（免费代理里的绝大多数）既不在池里
        # 也不在黑名单，采集侧每轮都会重新收一遍、验一遍，**永远循环**。
        # 这正是线上队列积压 4.5 万、通过率 0% 的根因。
        store.add_black(proxy.proxy, backoff=True)
        if outcome in HARD_FAILURES:
            # 硬失败 = 代理本身坏了（地址非法 / 端口不通 / 协议不受支持），
            # 把它移出有效池：死代理留在池里只会被继续分发出去给用户踩坑。
            # 软失败（fail_http）不动池成员资格 —— 校验目标自身故障时
            # 可能整池都不通过，此时大刀阔斧清池比留着空池更糟，
            # 因此软失败仍由"错误率淘汰"这条既有规则来决定去留。
            store.delete(proxy.proxy)

    # B-01: 基于 error_rate + ewma_speed 重新计算复合分
    proxy.score = proxy._recalc_score()


# ================= 单条校验 =================


async def _check_one(semaphore: asyncio.Semaphore, session: ClientSession,
                     proxy: Proxy, opts: dict) -> Proxy:
    """校验单条代理，结果通过 ``_record`` 统一写回。

    v0.1.0（P0-1）：按 ``proxy.protocol`` 选择探针会话 ——
    HTTP 代理复用全局会话（请求带 ``proxy=``），
    SOCKS 代理使用绑定了该代理地址的专用连接器（请求**不带** ``proxy=``）。
    """
    outcome = "fail_unknown"
    speed = 0.0
    https_ok = False
    try:
        async with semaphore:
            ip, port = parse_host_port(proxy.proxy)
            if ip is None:
                outcome = "fail_format"
                return proxy

            # 第一层：TCP 连通
            if not await tcp_check(ip, port, opts["tcp_timeout"]):
                outcome = "fail_tcp"
                return proxy

            # 第二层：代理功能（连续 N 轮全通过才算有效）
            try:
                async with probe_session(proxy.proxy, proxy.protocol, session,
                                        opts["verify_timeout"]) as (sess, use_proxy):
                    total_speed = 0.0
                    origin = ""
                    resp_headers = {}
                    passed = True
                    for _ in range(opts["multi_round"]):
                        ok, sp, out, headers = await http_probe(
                            sess, proxy.proxy, opts["http_url"], opts["timeout"],
                            use_proxy_arg=use_proxy)
                        if not ok:
                            passed = False
                            break
                        total_speed += sp
                        origin = origin or out
                        resp_headers = headers or resp_headers

                    if passed:
                        speed = round(total_speed / opts["multi_round"], 4)
                        # 属性稳定（借鉴 docs/09 P2-3）：判定不出匿名度时
                        # **不覆盖**已有值 —— 出口 IP 取不到只是一次探测失败，
                        # 不该把上一轮已判定的匿名度抹成空（抹空之后这条代理
                        # 就再也筛不出 `anon=elite` 了）。
                        proxy.anonymous = keep_known(
                            proxy.anonymous,
                            judge_anonymous(ip, origin, resp_headers))
                        # SOCKS5/SOCKS4 隧道天然可以承载 TLS，
                        # 这里仍然实测而不是直接置 True ——
                        # 代理可能只支持 TCP 转发而不支持 TLS 握手。
                        https_ok = await https_probe(
                            sess, proxy.proxy, opts["https_url"], opts["timeout"],
                            use_proxy_arg=use_proxy)
                        outcome = "pass"
                    else:
                        outcome = "fail_http"
            except UnsupportedProtocolError:
                outcome = "fail_unsupported"
                return proxy
            return proxy
    finally:
        proxy.last_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _record(proxy, outcome, speed, https_ok, opts)


# ================= 地域填充 =================


async def _fill_regions(proxies: List[Proxy]) -> None:
    """给通过校验的代理补齐地域。失败不影响主流程。

    v0.1.0（P2-3）：**已有地域的代理不重新解析**（首源胜出）——
    国家码在代理存活期内基本不变，而它同时是地域索引的分组键，
    横跳会让 ``?area=CN`` 的结果集忽大忽小。
    """
    if not bool(cc.get("PROXY_REGION", True)):
        return
    targets = [p for p in proxies
               if p.last_status and not p.region and parse_host_port(p.proxy)[0]]
    if not targets:
        return
    try:
        resolver = get_resolver()
        mapping = await resolver.resolve_many(
            [parse_host_port(p.proxy)[0] for p in targets])
        for proxy in targets:
            region = mapping.get(parse_host_port(proxy.proxy)[0])
            if region:
                proxy.region = keep_first(proxy.region, region)
    except Exception as exc:
        log.warning("地域解析失败，跳过本轮：%s", type(exc).__name__)


# ================= B-07: ASN 机型填充 =================


async def _fill_asn(proxies: List[Proxy]) -> None:
    """给通过校验的代理补齐 IP 类型（机房/住宅）。失败不影响主流程。"""
    if not proxies or not bool(cc.get("ASN_ENABLED", True)):
        return
    targets = [p for p in proxies
               if p.last_status and p.ip_type == "unknown"
               and parse_host_port(p.proxy)[0]]
    if not targets:
        return
    try:
        for proxy in targets:
            ip = parse_host_port(proxy.proxy)[0]
            if not ip:
                continue
            # 属性稳定（借鉴 docs/09 P2-3）：ASN 查不出来时**不覆盖**已有值 ——
            # "unknown" 只是"这次没查出来"，不是"确认是 unknown"，
            # 拿它覆盖会把已经判好的机房/住宅属性抹掉。
            value = lookup_asn(ip)
            if value and value != "unknown":
                proxy.ip_type = keep_known(proxy.ip_type, value)
    except Exception as exc:
        log.warning("ASN 解析失败，跳过本轮：%s", type(exc).__name__)


# ================= B-08: DNSBL 声誉填充 =================


async def _fill_reputation(proxies: List[Proxy]) -> None:
    """给通过校验的代理做 DNSBL 声誉检查。失败不影响主流程。"""
    if not proxies or not bool(cc.get("DNSBL_ENABLED", True)):
        return
    targets = [p for p in proxies
               if p.last_status and not p.dnsbl
               and parse_host_port(p.proxy)[0]]
    if not targets:
        return
    try:
        checker = get_checker()
        mapping = await checker.check_many(
            [parse_host_port(p.proxy)[0] for p in targets])
        for proxy in targets:
            ip = parse_host_port(proxy.proxy)[0]
            result = mapping.get(ip)
            if result:
                proxy.dnsbl = bool(result.get("listed"))
    except Exception as exc:
        log.warning("DNSBL 检查失败，跳过本轮：%s", type(exc).__name__)


# ================= 批次执行 =================


async def _run(proxy_list: List[Proxy], work_type: str,
               on_progress=None) -> dict:
    """执行一轮校验。``work_type`` 取 ``raw``（新代理）或 ``use``（存量复检）。

    ``on_progress`` 是可选的进度回调，签名 ``(checked, total)``，
    每处理完一批调用一次 —— 长任务（存量复检动辄几万条）需要向用户
    反馈进度，否则就是一段没有反馈的黑盒等待。
    回调异常不影响校验主流程（进度只是观测）。
    """
    from ..db.dbClient import get_store

    store = get_store()
    concurrency = max(5, int(cc.get("CHECK_CONCURRENCY", 50)))
    opts = {
        "store": store,
        "score_max": int(cc.get("PROXY_SCORE_MAX", 100)),
        "score_min": int(cc.get("PROXY_SCORE_MIN", 0)),
        "score_step": int(cc.get("PROXY_SCORE_STEP", 1)),
        "tcp_timeout": float(cc.get("TCP_CHECK_TIMEOUT", 3)),
        "multi_round": max(1, int(cc.get("CHECK_MULTI_ROUND", 2))),
        # P0-2：黑名单时长不再由调用方给定 —— 退避阶梯由存储层按连续失败
        # 次数决定（BLACKLIST_BACKOFF），这里保留配置读取仅供观测/兼容。
        "black_ttl": int(cc.get("BLACKLIST_TTL", 3600)),
        "http_url": origin_probe_url(cc.get("HTTP_URL", "http://httpbin.org")),
        "https_url": cc.get("HTTPS_URL", "https://www.qq.com"),
        "timeout": ClientTimeout(total=int(cc.get("VERIFY_TIMEOUT", 10))),
        # P0-1: SOCKS 专用会话的整体超时（秒），与 HTTP 探针同一口径
        "verify_timeout": int(cc.get("VERIFY_TIMEOUT", 10)),
        "ttl": int(cc.get("PROXY_TTL", 86400)),
        # B-01: 新增错误率淘汰阈值
        "max_error_rate": float(cc.get("PROXY_MAX_ERROR_RATE", 0.5)),
        "evict_min_checks": int(cc.get("PROXY_EVICT_MIN_CHECKS", 10)),
    }
    max_fail = int(cc.get("MAX_FAIL_COUNT", 0))

    semaphore = asyncio.Semaphore(concurrency)
    session = await _get_session(concurrency)
    batch_size = max(concurrency * 4, 200)

    stats = {"checked": 0, "passed": 0, "failed": 0, "added": 0, "removed": 0}

    for start in range(0, len(proxy_list), batch_size):
        batch = proxy_list[start:start + batch_size]
        tasks = [_check_one(semaphore, session, p, opts) for p in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        passed_items: List[Proxy] = []
        for item in results:
            # checked 统计**全部**待验条目（含执行异常），failed = 异常 + 校验失败，
            # 始终维持 passed + failed == checked 的不变式。
            # 旧实现里 checked 只数非异常条目、failed 又数一遍失败条目，
            # 调用方再拿 checked + failed 当分母，失败条数被重复计入，
            # 通过率被稀释约一半，`FppCheckPassRateLow` 告警也会误判。
            stats["checked"] += 1
            if isinstance(item, BaseException):
                stats["failed"] += 1
                continue
            if item.last_status:
                stats["passed"] += 1
                passed_items.append(item)
            else:
                stats["failed"] += 1

        await _fill_regions(passed_items)
        # B-07/B-08: 异步填充 ASN + DNSBL（失败不影响主流程）
        await _fill_asn(passed_items)
        await _fill_reputation(passed_items)

        for proxy in passed_items:
            try:
                if work_type == "raw":
                    if not store.exists(proxy.proxy):
                        # v0.1.0: 不再手工维护 score，复合分由 _record 计算
                        store.put(proxy.to_dict(), ttl=opts["ttl"])
                        stats["added"] += 1
                        _archive(proxy.to_dict(), "added")
                else:
                    store.update(proxy.to_dict(), ttl=opts["ttl"])
            except Exception as exc:
                log.error("写入代理 %s 失败：%s", proxy.proxy, type(exc).__name__)

        for item in results:
            if isinstance(item, BaseException) or item.last_status:
                continue
            try:
                if work_type != "use":
                    continue
                outcome = getattr(item, "_check_outcome", "")
                if outcome in HARD_FAILURES:
                    # P0-2: 硬失败已在 _record 中移出有效池并写入退避条目，
                    # 这里绝不能把它写回去 —— 否则死代理立刻回到池里继续被分发。
                    # P0-1: 硬失败集合新增 fail_unsupported（协议不受支持）。
                    stats["removed"] += 1
                    _archive(item.to_dict(), "removed")
                elif item.should_evict(
                    max_fail,
                    opts.get("max_error_rate", 0.5),
                    opts.get("evict_min_checks", 10),
                ):
                    # 错误率超限 = 确认已死。退避条目 _record 已写入（计数只加一次），
                    # 这里只负责把它移出有效池。
                    store.delete(item.proxy)
                    stats["removed"] += 1
                    _archive(item.to_dict(), "removed")
                else:
                    # 软失败但尚未触发淘汰：留在池里继续观察，
                    # 退避窗口会阻止它被重复采集。
                    store.update(item.to_dict(), ttl=opts["ttl"])
            except Exception as exc:
                log.error("淘汰代理 %s 失败：%s", item.proxy, type(exc).__name__)

        if on_progress is not None:
            try:
                on_progress(stats["checked"], len(proxy_list))
            except Exception:
                # 进度回调异常不能影响校验主流程
                pass

    return stats


def _archive(proxy_dict: dict, event: str) -> None:
    """写入 MySQL 冷存储归档（未启用时是空操作）。"""
    try:
        from ..storage.mysql_client import archive_enabled, archive_proxy
        if archive_enabled():
            archive_proxy(proxy_dict, event)
    except Exception:
        # 归档永远不能影响实时业务
        pass


def check(proxy_list: List[Proxy], work_type: str = "raw", on_progress=None) -> dict:
    """同步入口：在进程内共享的事件循环上执行一轮校验。

    :param on_progress: 可选进度回调 ``(checked, total)``，每批处理完调用一次。
    """
    if not proxy_list:
        return {"checked": 0, "passed": 0, "failed": 0, "added": 0, "removed": 0}
    loop = get_loop()
    try:
        return loop.run_until_complete(_run(proxy_list, work_type,
                                             on_progress=on_progress))
    except Exception as exc:
        log.error("校验执行异常：%s", exc, exc_info=True)
        return {"checked": 0, "passed": 0, "failed": 0, "added": 0, "removed": 0}


__all__ = ["check", "Checker", "get_loop", "close_session", "origin_probe_url",
           "tcp_check", "http_probe", "https_probe",
           "probe_session", "new_socks_session", "HARD_FAILURES",
           "UnsupportedProtocolError"]


class Checker:
    """兼容旧调用方式的门面类。"""

    @staticmethod
    def run(proxy_list: List[Proxy], work_type: str = "raw") -> dict:
        return check(proxy_list, work_type)

    @staticmethod
    def pre_validate(proxy_str: str) -> bool:
        """入池前格式预校验。"""
        from ..core.iputil import is_valid_proxy
        return is_valid_proxy(proxy_str)
