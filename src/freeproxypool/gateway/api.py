# -*- coding: utf-8 -*-
"""FreeProxyPool 网关（FastAPI）。

对外提供三类端点：

* **代理分发**（需 API Key）：``/get/`` ``/batch/`` ``/pop/`` ``/all/`` ``/count/``
* **运维观测**：``/health`` ``/metrics`` ``/version`` ``/stats/``
* **管理后台**：``/admin`` 页面 + ``/admin/api/*`` JSON 接口

## 响应约定

管理后台接口统一使用信封结构 ``{"code":0,"message":"...","data":{...}}``；
代理分发端点为兼容历史客户端，**直接返回代理对象本身**（不套信封）。
"""

from contextlib import asynccontextmanager

import hashlib

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import config_center as cc
from .. import metrics
from ..core.errors import FreeProxyPoolError, ValidationError
from ..core.logging import attach_redis_sink, get_logger, set_log_level
from ..core.validators import (parse_area, parse_anon, parse_bool_param, parse_format,
                             parse_ntl, parse_protocol, parse_strategy, parse_tier,
                             parse_type, require_proxy)
from ..core.version import VERSION
from ..db.dbClient import get_store
from . import deps
from .deps import (annotate, client_ip, enforce_rate_limit, is_pool_low,
                          pick_candidates, require_admin_auth,
                          require_admin_csrf, require_admin_enabled,
                          require_api_key, select_many, select_one)

log = get_logger("gateway", to_file=False)

API_LIST = [
    {"url": "/get/", "params": "type=https, area=CN, strategy=latency|random|roundrobin|weighted, anon=elite|anonymous|transparent, tier=fast|medium|slow, protocol=http|socks4|socks5, format=json|text",
     "desc": "获取一个代理"},
    {"url": "/batch/", "params": "n=10, type=https, area=CN, strategy=latency, anon=elite, tier=fast, protocol=socks5, format=json|text",
     "desc": "批量获取代理（最多 BATCH_MAX 条）"},
    {"url": "/pop/", "params": "type=https, anon=elite, protocol=socks5, format=json|text", "desc": "获取并从池中删除一个代理"},
    {"url": "/all/", "params": "type=https, area=CN, anon=elite, tier=fast, protocol=socks5, format=json|text", "desc": "获取代理列表"},
    {"url": "/subscribe/clash", "params": "key=API_KEY, type=https, area=CN, tier=fast, protocol=socks5", "desc": "Clash/Mihomo 订阅输出（YAML，含 http/socks5 节点）"},
    {"url": "/subscribe/v2ray", "params": "key=API_KEY, type=https, area=CN, tier=fast, protocol=socks5", "desc": "V2Ray/v2rayN 订阅输出（base64 分享 URI）"},
    {"url": "/count/", "params": "", "desc": "代理池统计（含 tier / protocol 分布）"},
    {"url": "/delete/", "params": "proxy=ip:port", "desc": "删除指定代理"},
    {"url": "/report/", "params": "proxies=[...]", "desc": "回报失效代理（快速淘汰）"},
    {"url": "/stats/", "params": "", "desc": "精简运行统计"},
    {"url": "/health", "params": "", "desc": "健康检查"},
    {"url": "/metrics", "params": "", "desc": "Prometheus 指标"},
]

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动钩子：对齐日志级别并挂载 Redis 日志汇聚（仅挂一次）。"""
    try:
        set_log_level(cc.get("LOG_LEVEL", "INFO"))
    except Exception:
        pass
    try:
        attach_redis_sink(cc.KEY_LOGS, max_logs=2000)
    except Exception:
        pass
    log.info("FreeProxyPool 网关已启动，版本 %s", VERSION)
    yield


app = FastAPI(
    title="FreeProxyPool Gateway",
    version=VERSION,
    description="免费代理 IP 池：采集 / 验证 / 分发",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)

# ---------- 安全响应头 ----------
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """统一注入安全响应头、遮蔽 Server 头，并做请求/错误指标埋点。

    同时对**改变状态的公开接口**（``/report/`` ``/delete/``）写一条访问审计日志。
    只读高频接口（``/get/`` ``/count/``）不写 —— 否则审计写入会成为
    分发链路的瓶颈，而它们本身没有审计价值。
    """
    metrics.inc_counter(metrics.gateway_requests)
    response = await call_next(request)
    if response.status_code >= 400:
        metrics.inc_counter(metrics.gateway_errors)
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
    # 遮蔽框架指纹，避免泄露 uvicorn / starlette 版本
    if "server" in response.headers:
        del response.headers["server"]

    if request.method in ("POST", "DELETE"):
        path = request.url.path
        if path.startswith(("/report/", "/delete/")):
            _write_audit(request, path, response.status_code)
    return response


def _write_audit(request: Request, endpoint: str, status_code: int) -> None:
    """写访问审计（未启用归档时为空操作）。"""
    try:
        from ..storage.mysql_client import archive_access, archive_enabled
        if not archive_enabled():
            return
        client = (request.headers.get("API-KEY")
                  or request.query_params.get("key")
                  or client_ip(request))
        # 只记录密钥的哈希前 8 位，绝不落明文
        digest = hashlib.sha256(str(client).encode("utf-8")).hexdigest()[:8]
        archive_access(digest, endpoint[:256], None, status_code)
    except Exception:
        pass


# ---------- 统一异常处理 ----------

@app.exception_handler(FreeProxyPoolError)
async def handle_domain_error(request: Request, exc: FreeProxyPoolError):
    """项目内主动抛出的异常：按声明的状态码返回稳定文案。"""
    log.warning("%s %s → %s：%s",
                request.method, request.url.path, exc.status_code, exc.log_text)
    return JSONResponse(status_code=exc.status_code,
                        content={"code": exc.status_code, "message": exc.public_message})


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return JSONResponse(status_code=404,
                            content={"code": 404, "message": "资源不存在"})
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code,
                        content={"code": exc.status_code, "message": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def handle_request_validation(request: Request, exc: RequestValidationError):
    """参数校验失败统一收敛为 400，且不把内部字段结构吐给调用方。"""
    return JSONResponse(status_code=400,
                        content={"code": 400, "message": "请求参数不合法"})


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    """兜底：记录完整堆栈，对外只返回通用文案。"""
    log.error("未处理异常 %s %s：%s", request.method, request.url.path, exc,
              exc_info=True)
    return JSONResponse(status_code=500,
                        content={"code": 500, "message": "服务器内部错误"})


# ---------- 通用响应辅助 ----------

def ok(data=None, message: str = "ok") -> dict:
    return {"code": 0, "message": message, "data": data}


async def json_body(request: Request) -> dict:
    """宽容解析 JSON 请求体：解析失败返回空 dict（不抛 400）。"""
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def with_tier(item: dict, fast_ms=None, medium_ms=None) -> dict:
    """给返回的代理对象补上延迟分层标签（借鉴 docs/09 P0-3）。

    分层是**现算**的派生值，不落 Redis：阈值是配置项、延迟是动态的，
    存字段意味着要额外维护一份"过期即失真"的数据。
    调用方因此不需要自己设阈值就能知道"这条代理快不快"。

    批量场景请把阈值取一次传进来（见 :func:`helper.proxy.tier_thresholds`），
    避免每条代理都重读一遍配置中心。
    """
    if isinstance(item, dict) and "proxy" in item:
        from ..helper.proxy import tier_of_proxy
        item["tier"] = tier_of_proxy(item, fast_ms, medium_ms)
    return item


def with_protocol(item: dict) -> dict:
    """给返回的代理对象补上传输协议（借鉴 docs/09 P0-1）。

    历史数据没有 ``protocol`` 字段（升级前入库的全是 HTTP 代理），
    这里统一补成 ``"http"``，调用方不必自己判断缺省值。
    """
    if isinstance(item, dict) and "proxy" in item:
        from ..helper.proxy import normalize_protocol
        item["protocol"] = normalize_protocol(item.get("protocol"))
    return item


def decorate(items, fast_ms=None, medium_ms=None) -> list:
    """批量补齐派生字段（延迟分层 + 传输协议）。"""
    if isinstance(items, dict):
        return with_protocol(with_tier(items, fast_ms, medium_ms))
    return [with_protocol(with_tier(i, fast_ms, medium_ms)) for i in items or []]


# ================= 代理分发端点 =================

public = APIRouter()


def text_response(lines) -> Response:
    """纯文本响应（借鉴 docs/09 P2-3）：一行一个 ``ip:port``。

    Content-Type 为 ``text/plain; charset=utf-8``，客户端可以直接
    ``curl -s ".../all/?format=text" > proxies.txt`` 或
    ``... | xargs -I{} curl -x {} <url>``。

    **纯文本模式没有信封**：没有"暂无可用代理"的提示、没有 ``pool_low``
    标记 —— 列表为空就是为空。需要这些信息请用 JSON 模式。
    """
    body = "\n".join([str(x) for x in lines if x])
    if body:
        body += "\n"
    return Response(content=body, media_type="text/plain")


@public.get("/")
def index():
    """服务信息与端点清单。"""
    return {"name": "FreeProxyPool", "version": VERSION, "endpoints": API_LIST}


@public.get("/favicon.ico", include_in_schema=False)
def favicon():
    """返回 204，避免浏览器默认请求站点图标产生 404 噪音。"""
    return Response(status_code=204)


@public.get("/get/", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
def get_proxy(request: Request):
    """获取一个代理。

    查询参数：

    * ``type``   —— ``https`` 表示只要支持 HTTPS 隧道的代理
    * ``area``   —— 两位国家码（严格匹配，地域未知的代理不会被返回）
    * ``strategy`` —— ``latency``（按评分，默认）/ ``random`` / ``roundrobin`` / ``weighted``
    * ``anon``   —— ``elite`` / ``anonymous`` / ``transparent`` 匿名度筛选
    * ``exclude_dnsbl`` —— ``1``/``true`` 表示排除已被 DNSBL 列入的代理
    * ``tier``   —— ``fast`` / ``medium`` / ``slow`` / ``unknown`` 延迟分层筛选
    * ``protocol`` —— ``http`` / ``socks4`` / ``socks5`` 传输协议筛选（P0-1）
    * ``format`` —— ``json``（默认）/ ``text`` 纯文本，一行一个 ``ip:port``（P2-3）
    """
    store = get_store()
    https = parse_type(request.query_params.get("type"))
    area = parse_area(request.query_params.get("area"))
    strategy = parse_strategy(request.query_params.get("strategy"))
    anon = parse_anon(request.query_params.get("anon"))
    exclude_dnsbl = parse_bool_param(request.query_params.get("exclude_dnsbl"), False)
    tier = parse_tier(request.query_params.get("tier"))
    protocol = parse_protocol(request.query_params.get("protocol"))
    fmt = parse_format(request.query_params.get("format"))
    limit = int(cc.get("DISPATCH_CANDIDATE_LIMIT", 200))

    item = select_one(store, https, area, strategy, limit, anon, exclude_dnsbl,
                      tier, protocol)
    pool_low = is_pool_low(store)
    if item:
        metrics.inc_counter(metrics.gateway_served)
        if fmt == "text":
            return text_response([item.get("proxy")])
        return annotate(decorate(item), pool_low)
    if fmt == "text":
        return text_response([])
    data = {"code": 0, "message": "暂无可用代理"}
    hints = []
    if area:
        hints.append("area=%s 无匹配代理（地域未知的代理不会被返回）" % area)
    if tier:
        hints.append("tier=%s 无匹配代理（延迟分层按 EWMA 判定）" % tier)
    if protocol:
        hints.append("protocol=%s 无匹配代理（按传输协议筛选）" % protocol)
    if hints:
        data["hint"] = "；".join(hints)
    return annotate(data, pool_low)


@public.get("/batch/", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
def get_batch(request: Request):
    """批量获取代理（默认 10 条，最多 ``BATCH_MAX`` 条）。

    返回 ``{"code":0,"count":n,"data":[...]}``。

    查询参数：

    * ``n``       —— 条数（默认 10，上限 BATCH_MAX）
    * ``type``    —— ``https`` 表示只要支持 HTTPS 隧道的代理
    * ``area``    —— 两位国家码
    * ``strategy`` —— ``latency`` / ``random`` / ``roundrobin`` / ``weighted``
    * ``anon``    —— ``elite`` / ``anonymous`` / ``transparent``
    * ``exclude_dnsbl`` —— ``1``/``true`` 排除 DNSBL 列入代理
    * ``tier``    —— ``fast`` / ``medium`` / ``slow`` / ``unknown``
    * ``protocol`` —— ``http`` / ``socks4`` / ``socks5`` 传输协议（P0-1）
    * ``format``  —— ``json``（默认）/ ``text`` 纯文本，一行一个 ``ip:port``（P2-3）
    """
    store = get_store()
    https = parse_type(request.query_params.get("type"))
    area = parse_area(request.query_params.get("area"))
    strategy = parse_strategy(request.query_params.get("strategy"))
    anon = parse_anon(request.query_params.get("anon"))
    exclude_dnsbl = parse_bool_param(request.query_params.get("exclude_dnsbl"), False)
    tier = parse_tier(request.query_params.get("tier"))
    protocol = parse_protocol(request.query_params.get("protocol"))
    fmt = parse_format(request.query_params.get("format"))
    max_items = int(cc.get("BATCH_MAX", 100))
    count = parse_ntl(request.query_params.get("n"), default=10, max_n=max_items)
    limit = int(cc.get("DISPATCH_CANDIDATE_LIMIT", 200))

    items = select_many(store, https, area, strategy, limit, count, anon,
                        exclude_dnsbl, tier, protocol)
    pool_low = is_pool_low(store)
    if items:
        metrics.inc_counter(metrics.gateway_served, len(items))
    if fmt == "text":
        return text_response([i.get("proxy") for i in items])
    from ..helper.proxy import tier_thresholds
    fast_ms, medium_ms = tier_thresholds()
    items = decorate(items, fast_ms, medium_ms)
    return annotate({"code": 0, "count": len(items), "data": items}, pool_low)


@public.get("/pop/", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
def pop_proxy(request: Request):
    """获取一个代理并从池中移除（用于"独占型"调用方）。

    查询参数：

    * ``type``   —— ``https`` 表示只要支持 HTTPS 隧道的代理
    * ``anon``   —— ``elite`` / ``anonymous`` / ``transparent`` 匿名度筛选
    * ``exclude_dnsbl`` —— ``1``/``true`` 排除 DNSBL 列入代理
    * ``tier``   —— ``fast`` / ``medium`` / ``slow`` / ``unknown`` 延迟分层筛选
    * ``protocol`` —— ``http`` / ``socks4`` / ``socks5`` 传输协议筛选（P0-1）
    * ``format`` —— ``json``（默认）/ ``text`` 纯文本，一行一个 ``ip:port``（P2-3）
    """
    store = get_store()
    https = parse_type(request.query_params.get("type"))
    anon = parse_anon(request.query_params.get("anon"))
    exclude_dnsbl = parse_bool_param(request.query_params.get("exclude_dnsbl"), False)
    tier = parse_tier(request.query_params.get("tier"))
    protocol = parse_protocol(request.query_params.get("protocol"))
    fmt = parse_format(request.query_params.get("format"))
    limit = int(cc.get("DISPATCH_CANDIDATE_LIMIT", 200))

    item = None
    for candidate in pick_candidates(store, https, None, "latency", limit,
                                     anon, exclude_dnsbl, tier, protocol):
        try:
            if store.exists(candidate["proxy"]):
                store.delete(candidate["proxy"])
                item = candidate
                break
        except Exception:
            continue

    pool_low = is_pool_low(store)
    if item:
        metrics.inc_counter(metrics.gateway_served)
        if fmt == "text":
            return text_response([item.get("proxy")])
        return annotate(decorate(item), pool_low)
    if fmt == "text":
        return text_response([])
    return annotate({"code": 0, "message": "暂无可用代理"}, pool_low)


def _filtered_pool(request: Request) -> list:
    """/all/ 与 /subscribe/ 共用的取数与过滤链路（多入口同一核心）。

    解析查询参数 → 取数（area 走地域索引，可用性降级全表）→ 逐层筛选
    → 按 ``ALL_MAX`` 截断。返回**原始** item 列表（未 decorate），
    展示层自行决定加不加 tier/protocol 标签。
    """
    store = get_store()
    https = parse_type(request.query_params.get("type"))
    area = parse_area(request.query_params.get("area"))
    anon = parse_anon(request.query_params.get("anon"))
    exclude_dnsbl = parse_bool_param(request.query_params.get("exclude_dnsbl"), False)
    tier = parse_tier(request.query_params.get("tier"))
    protocol = parse_protocol(request.query_params.get("protocol"))
    limit = int(cc.get("ALL_MAX", 200))
    if area:
        # 走地域索引（借鉴 docs/09 P1-1），索引不可用时自动降级全表扫描。
        # limit=None 取该地域的全部代理 —— 与改造前 get_all(https, area)
        # 的结果集完全一致，只是取数来源从"全池 HVALS + 过滤"变成
        # "该地域的 ZSet"，池子上万时省掉的是全池那一遍。
        items = store.get_by_region(area, https=https, limit=None)
    else:
        items = store.get_all(https)
    if anon:
        items = [i for i in items if i.get("anonymous") == anon]
    if exclude_dnsbl:
        items = [i for i in items if not i.get("dnsbl")]
    if tier:
        from ..helper.proxy import tier_of_proxy
        items = [i for i in items if tier_of_proxy(i) == tier]
    if protocol:
        from ..helper.proxy import normalize_protocol
        items = [i for i in items
                 if normalize_protocol(i.get("protocol")) == protocol]
    items = items[:limit]
    if len(items) >= limit:
        cc.mark_stat("api:all_truncated", 1)
    return items


@public.get("/all/", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
def get_all(request: Request):
    """获取代理列表（最多 ``ALL_MAX`` 条）。

    查询参数：

    * ``type``   —— ``https`` 表示只要支持 HTTPS 隧道的代理
    * ``area``   —— 两位国家码
    * ``anon``   —— ``elite`` / ``anonymous`` / ``transparent`` 匿名度筛选
    * ``exclude_dnsbl`` —— ``1``/``true`` 排除 DNSBL 列入代理
    * ``tier``   —— ``fast`` / ``medium`` / ``slow`` / ``unknown`` 延迟分层筛选
    * ``protocol`` —— ``http`` / ``socks4`` / ``socks5`` 传输协议筛选（P0-1）
    * ``format`` —— ``json``（默认）/ ``text`` 纯文本，一行一个 ``ip:port``（P2-3）
    """
    fmt = parse_format(request.query_params.get("format"))
    items = _filtered_pool(request)
    if fmt == "text":
        return text_response([i.get("proxy") for i in items])
    # 只给**返回的**这批打 tier / protocol 标签：池子上万时，
    # 先算全池再切片纯属浪费
    from ..helper.proxy import tier_thresholds
    fast_ms, medium_ms = tier_thresholds()
    items = decorate(items, fast_ms, medium_ms)
    return items


@public.get("/subscribe/clash", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
def subscribe_clash(request: Request):
    """Clash / Mihomo 订阅输出（YAML）。

    把池内 HTTP / SOCKS5 代理输出为 Clash 订阅 —— 客户端把
    ``/subscribe/clash?key=<API_KEY>`` 配成订阅 URL 即可同步整个
    代理池为一个可选节点组。筛选参数与 ``/all/`` 相同；
    **SOCKS4 代理不被输出**（Clash 无此节点类型，硬塞只会生成死节点）。
    """
    from ..helper.subscribe import build_clash_yaml
    items = _filtered_pool(request)
    if items:
        metrics.inc_counter(metrics.gateway_served, len(items))
    return Response(content=build_clash_yaml(items),
                    media_type="text/plain; charset=utf-8")


@public.get("/subscribe/v2ray", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
def subscribe_v2ray(request: Request):
    """V2Ray / v2rayN 订阅输出（base64 编码的逐行分享 URI）。

    HTTP 代理 → ``http://host:port#name``，SOCKS5 → ``socks://host:port#name``；
    筛选参数与 ``/all/`` 相同，SOCKS4 代理不被输出（v2rayN 按 SOCKS5 解析）。
    """
    from ..helper.subscribe import build_v2ray_b64
    items = _filtered_pool(request)
    if items:
        metrics.inc_counter(metrics.gateway_served, len(items))
    return Response(content=build_v2ray_b64(items),
                    media_type="text/plain; charset=utf-8")


@public.get("/count/", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
def get_count(request: Request):
    """代理池统计（按协议 / 采集源 / 地域 / 延迟分层聚合）。

    返回两个**互相正交**的协议维度，别混淆：

    * ``http_type`` —— 能力维度：``https``（可用于 HTTPS 目标）/ ``http``；
    * ``protocol``  —— 传输维度：``http`` / ``socks4`` / ``socks5``（P0-1）。
    """
    from ..helper.proxy import PROTOCOLS, TIERS, normalize_protocol, tier_of_proxy, tier_thresholds

    store = get_store()
    items = store.get_all()
    by_https_cap: dict = {}
    by_transport: dict = {p: 0 for p in PROTOCOLS}
    by_source: dict = {}
    by_region: dict = {}
    by_tier: dict = {t: 0 for t in TIERS}
    # 阈值取一次即可（配置中心有缓存，但逐条再取仍是无谓开销）
    fast_ms, medium_ms = tier_thresholds()
    for item in items:
        capability = "https" if item.get("https") else "http"
        by_https_cap[capability] = by_https_cap.get(capability, 0) + 1
        transport = normalize_protocol(item.get("protocol"))
        by_transport[transport] = by_transport.get(transport, 0) + 1
        for src in str(item.get("source") or "").split("/"):
            if src:
                by_source[src] = by_source.get(src, 0) + 1
        region = str(item.get("region") or "")
        if region:
            by_region[region] = by_region.get(region, 0) + 1
        label = tier_of_proxy(item, fast_ms, medium_ms)
        by_tier[label] = by_tier.get(label, 0) + 1
    count = store.get_count()
    return {"count": count["total"], "https": count["https"],
            "http_type": by_https_cap, "protocol": by_transport,
            "source": by_source, "region": by_region, "tier": by_tier}


@public.get("/delete/", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
@public.post("/delete/", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
async def delete_proxy(request: Request):
    """删除指定代理（兼容 GET；POST 时可从请求体传参）。"""
    body = await json_body(request)
    raw = request.query_params.get("proxy") or body.get("proxy")
    if not raw:
        raise ValidationError("缺少 proxy 参数")
    get_store().delete(require_proxy(raw))
    return {"code": 0, "message": "已删除"}


@public.post("/report/", dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
async def report_failure(request: Request):
    """回报失效代理 —— 补齐业务闭环的关键一环。

    客户端在使用中发现代理连不通时调用本接口，代理会被立即从池中剔除并
    拉黑（旧版只能等下一轮周期复检才发现，期间会持续把坏代理分发给
    所有调用方，浪费带宽与配额）。

    请求体：``{"proxies": ["1.2.3.4:8080", ...]}``
    """
    body = await json_body(request)
    raw_list = body.get("proxies")
    if isinstance(raw_list, str):
        raw_list = [raw_list]
    if not isinstance(raw_list, list) or not raw_list:
        raise ValidationError("请提供 proxies 数组")

    store = get_store()
    accepted, rejected = [], []
    for raw in raw_list[:200]:
        try:
            proxy_str = require_proxy(raw)
        except FreeProxyPoolError:
            rejected.append(raw)
            continue
        try:
            store.delete(proxy_str)
            # P0-2: 客户端回报 = 确认该代理已死 → 走死代理退避阶梯
            # （1h → 2h → 6h → 24h），而不是固定 TTL。
            # 多个客户端反复回报同一条 = 它确实反复失效，阶梯正该升档。
            store.add_black(proxy_str, backoff=True)
            accepted.append(proxy_str)
        except Exception:
            rejected.append(raw)

    metrics.inc_counter(metrics.gateway_reports, len(accepted))
    return {"code": 0, "message": "已回报 %d 条" % len(accepted),
            "accepted": accepted, "rejected": rejected}


@public.get("/auth/verify", include_in_schema=False)
def auth_verify(request: Request):
    """Nginx auth_request 子请求：校验管理会话并返回 SSO 用户名。

    Grafana/Prometheus 通过 Nginx 反向代理访问时，
    Nginx 会对每个请求先调用本接口做子请求校验：
    - 返回 200 + ``X-WEBAUTH-USER`` → 放行并注入 SSO 头
    - 返回 401/403 → 拦截

    本接口不走 API Key 鉴权，只检查管理会话 Cookie/Header。
    """
    from .deps import require_admin_auth
    try:
        token = deps.session_token(request)
        if not token or not cc.verify_session(token):
            return Response(status_code=401)
        # 会话有效：返回 200 并告知 Nginx 用户名（Grafana SSO 需要）
        resp = Response(status_code=200)
        resp.headers["X-WEBAUTH-USER"] = "admin"
        return resp
    except Exception:
        return Response(status_code=401)


@public.get("/stats/", dependencies=[Depends(require_api_key)])
def stats():
    """精简运行统计（单次 Redis 往返，适合做外部监控探针）。"""
    store = get_store()
    count = store.get_count()
    try:
        from ..db.queue import ProxyQueue
        depth = ProxyQueue().depth()
    except Exception:
        depth = -1
    return {
        "version": VERSION,
        "pool": count,
        "queue_depth": depth,
        "blacklist": cc.get_stat("pool:blacklist", 0),
        "fetch": {"last_run": cc.get_stat("fetch:last_run", ""),
                  "last_count": cc.get_stat("fetch:last_total", 0)},
        "check": {"last_run": cc.get_stat("check:last_run", ""),
                  "last_count": cc.get_stat("check:last_total", 0)},
    }


@public.get("/version")
def version():
    """版本信息。"""
    return {"name": "FreeProxyPool", "version": VERSION, "python_api": "fastapi"}


# ================= 运维端点 =================

@public.get("/health")
def health():
    """存活 + 就绪探针。

    ``status`` 为 ``ok`` 表示 Redis 可用；``degraded`` 表示进程活着但
    存储不可用（此时业务接口会返回 503，探针仍应保持存活）。
    """
    redis_ok = False
    pool = 0
    try:
        count = get_store().get_count()
        redis_ok = True
        pool = count["total"]
    except Exception as exc:
        log.warning("健康检查：存储不可用 %s", type(exc).__name__)
    return {"status": "ok" if redis_ok else "degraded",
            "redis": redis_ok, "pool": pool, "version": VERSION}


@public.get("/health/live")
def health_live():
    """纯存活探针，不触碰 Redis。"""
    return {"status": "alive", "version": VERSION}


@public.get("/metrics")
def prometheus_metrics(request: Request):
    """Prometheus 指标。``METRICS_AUTH=true`` 时需要 API Key。"""
    if not deps.metrics_api_key_ok(request):
        return Response("# unauthorized\n", status_code=401,
                        media_type="text/plain; charset=utf-8")
    try:
        metrics.update_metrics()
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
    except Exception:
        return Response("# metrics unavailable\n",
                        media_type="text/plain; charset=utf-8")


app.include_router(public)

__all__ = ["app", "ok", "json_body", "with_tier", "with_protocol", "decorate", "log"]