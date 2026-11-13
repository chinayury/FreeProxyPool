# -*- coding: utf-8 -*-
"""管理后台 JSON API。

全部接口统一经过四道关卡（顺序固定）：

    ADMIN_ENABLED 开关 → 管理会话鉴权 → （写操作）CSRF 校验 → 参数校验

v0.1.0 安全修复要点：

* ``ADMIN_ENABLED`` 此前**只作用于 HTML 页面**，
  ``/admin/api/*`` 全部接口不检查 —— 关掉后台后接口依然可用，开关形同虚设。
* 未登录时返回的 HTTP 状态码从 400 纠正为 **401**。
* 黑名单 ``ttl`` 参数从 ``int(body.get('ttl'))`` 改为安全解析，
  旧版传 ``"abc"`` 会直接抛 ValueError 变成 **500**。
* ``config/reset`` 传字符串时旧版会被逐字符迭代，
  结果"什么都没重置"却返回成功 —— 现在明确报 400。
* 采集源 ``name`` 与 URL 覆盖都做校验（防越权写入任意 Key、防 SSRF）。
"""
import time
from importlib import resources

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import config_center as cc
from .. import metrics
from .. import setting
from ..core.errors import FreeProxyPoolError, ValidationError
from ..core.logging import get_logger, read_logs, clear_logs
from ..core.validators import (parse_bool_param, parse_page, parse_protocol, parse_size,
                             parse_tier, parse_ttl, require_proxy, safe_int,
                             validate_fetch_urls, validate_name)
from ..core.version import VERSION
from ..db.dbClient import get_store
from ..db.queue import ProxyQueue
from ..fetcher import baseFetcher as breaker
from .deps import (client_ip, require_admin_auth, require_admin_csrf,
                          require_admin_enabled)
from ..helper.fetch import enabled_fetchers
from ..util.alert import clear_alerts, read_alerts

log = get_logger("admin", to_file=False)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    # v0.1.0 安全修复：开关作用于**整个** /admin 前缀。
    # 旧版只在 HTML 页面检查 ADMIN_ENABLED，/admin/api/* 全部接口不检查，
    # 关掉后台后接口依然完全可用，开关形同虚设。
    dependencies=[Depends(require_admin_enabled)],
)

#: 管理后台 HTML 模板在 admin 包内的位置（package-data）
ADMIN_HTML_REL = "templates/admin.html"


def _admin_html_path() -> str:
    """定位包内 admin/templates/admin.html（兼容源码运行与安装后运行）。"""
    return str(resources.files("freeproxypool.admin").joinpath(ADMIN_HTML_REL))


async def body_of(request: Request) -> dict:
    """宽容解析请求体（失败返回空 dict）。"""
    from .api import json_body
    return await json_body(request)


# ================= 页面 =================

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def admin_page():
    """管理后台页面（ADMIN_ENABLED 由路由级依赖统一校验）。"""
    return FileResponse(_admin_html_path(), media_type="text/html; charset=utf-8")


# ================= 认证 =================

@router.post("/api/login")
async def login(request: Request):
    """管理登录。

    失败次数按**可信来源 IP** 计数（``gateway.deps.client_ip``，
    仅当直连来源命中 ``TRUSTED_PROXIES`` 时才采信 X-Forwarded-For），
    并对计数 Key 做哈希归一化 —— 旧版直接拼接不可信的 XFF，
    攻击者每次换一个 XFF 即可无限次爆破。

    路由级依赖 ``require_admin_enabled`` 已经保证了开关关闭时
    这里根本不会被执行到业务逻辑。
    """
    identifier = client_ip(request)
    try:
        max_fail = int(cc.get_secure("LOGIN_MAX_FAIL"))
    except Exception:
        max_fail = 5

    if cc.login_fails(identifier) >= max_fail:
        # 提示的秒数必须是**锁定时长**（LOGIN_FAIL_TTL），不是失败次数。
        # 旧代码把 max_fail 塞进"%d 秒"，用户看到"请 5 秒后再试"，
        # 实际要等 600 秒，反复重试反而延长锁定。
        try:
            lock_ttl = int(cc.get_secure("LOGIN_FAIL_TTL"))
        except Exception:
            lock_ttl = setting.LOGIN_FAIL_TTL
        return JSONResponse(
            status_code=429,
            content={"code": 1, "message": "尝试次数过多，请 %d 秒后再试" % lock_ttl})

    body = await body_of(request)
    password = body.get("password")
    if not isinstance(password, str) or not cc.verify_password(password):
        cc.record_login_failure(identifier)
        log.warning("管理登录失败，来源 %s", identifier)
        return JSONResponse(status_code=400,
                            content={"code": 1, "message": "密码错误或账号未启用"})

    cc.clear_login_failures(identifier)
    try:
        token, ttl = cc.create_session()
    except Exception:
        return JSONResponse(status_code=503,
                            content={"code": 1, "message": "服务暂不可用"})

    need_change = bool(cc.get("ADMIN_FORCE_PASSWORD_CHANGE", True)) and cc.is_default_password()
    response = JSONResponse(content={
        "code": 0, "message": "登录成功",
        "data": {"token": token, "ttl": ttl, "csrf_token": cc.csrf_for(token),
                 "must_change_password": need_change,
                 "version": VERSION},
    })
    # 供 Nginx auth_request 校验 Grafana / Prometheus 免密访问
    response.set_cookie("fpp_admin_token", token, httponly=True,
                        max_age=ttl, path="/", samesite="lax")
    return response


@router.post("/api/logout")
def logout(request: Request, _: str = Depends(require_admin_auth),
           _c: None = Depends(require_admin_csrf)):
    from .deps import session_token
    cc.destroy_session(session_token(request))
    response = JSONResponse(content={"code": 0, "message": "已退出", "data": None})
    response.delete_cookie("fpp_admin_token", path="/")
    return response


@router.get("/auth/verify")
def auth_verify(request: Request):
    """内部认证端点：供 Nginx ``auth_request`` 子请求使用。

    通过时返回 200 并带 ``X-WEBAUTH-USER`` 响应头（Grafana auth.proxy SSO 用），
    未通过返回 401。
    """
    from .deps import session_token
    token = session_token(request)
    try:
        authed = bool(token) and cc.verify_session(token)
    except Exception:
        authed = False
    if not authed:
        return JSONResponse(status_code=401, content={"authenticated": False})
    response = JSONResponse(content={"authenticated": True})
    response.headers["X-WEBAUTH-USER"] = "admin"
    return response


# ================= 概览 =================

@router.get("/api/overview")
def overview(_: str = Depends(require_admin_auth)):
    """概览驾驶舱数据。"""
    from .api import ok
    from ..helper.proxy import (PROTOCOLS, TIERS, normalize_protocol, tier_of_proxy,
                              tier_thresholds)

    store = get_store()
    try:
        count = store.get_count()
    except Exception as exc:
        return JSONResponse(status_code=503,
                            content={"code": 1, "message": "存储不可用：%s" % type(exc).__name__})

    by_source, by_region = {}, {}
    by_tier = {t: 0 for t in TIERS}
    by_protocol = {p: 0 for p in PROTOCOLS}
    fast_ms, medium_ms = tier_thresholds()
    for item in store.get_all():
        for src in str(item.get("source") or "").split("/"):
            if src:
                by_source[src] = by_source.get(src, 0) + 1
        region = str(item.get("region") or "")
        if region:
            by_region[region] = by_region.get(region, 0) + 1
        label = tier_of_proxy(item, fast_ms, medium_ms)
        by_tier[label] = by_tier.get(label, 0) + 1
        transport = normalize_protocol(item.get("protocol"))
        by_protocol[transport] = by_protocol.get(transport, 0) + 1

    try:
        fetchers = cc.list_fetchers()
    except Exception:
        fetchers = []

    try:
        redis_ok = bool(cc.get_redis().ping())
    except Exception:
        redis_ok = False

    try:
        blacklist_total = store.black_count()
    except Exception:
        blacklist_total = 0

    try:
        depth = ProxyQueue().depth()
    except Exception:
        depth = -1

    try:
        mysql_ok = _mysql_health()
    except Exception:
        mysql_ok = False

    # v0.1.0（P1-3）：长任务进度
    try:
        from ..helper.progress import read as read_progress
        progress = read_progress()
    except Exception:
        progress = {}

    return ok({
        "version": VERSION,
        "pool": {"total": count["total"], "https": count["https"]},
        "sources": by_source,
        "regions": by_region,
        "tiers": by_tier,
        "protocols": by_protocol,
        "progress": progress,
        "fetchers": {
            "total": len(fetchers),
            "enabled": sum(1 for f in fetchers if f.get("enabled")),
        },
        "fetchers_list": fetchers,
        "fetch": {
            "last_run": cc.get_stat("fetch:last_run", ""),
            "last_total": cc.get_stat("fetch:last_total", 0),
            "interval": cc.get("FETCH_INTERVAL", 5),
            "paused": bool(cc.get("FETCHER_PAUSED", False)),
        },
        "check": {
            "last_run": cc.get_stat("check:last_run", ""),
            "last_total": cc.get_stat("check:last_total", 0),
            "interval": cc.get("CHECK_INTERVAL", 5),
        },
        "health": {
            "redis": redis_ok,
            "mysql": mysql_ok,
            "pool_total": count["total"],
            "blacklist_total": blacklist_total,
            "queue_depth": depth,
            "tripped_sources": breaker.tripped_names(),
        },
    })


def _mysql_health() -> bool:
    """MySQL 冷存储可用性（未启用时返回 False）。"""
    try:
        if not cc.get("MYSQL_ENABLED", False):
            return False
        from ..storage.mysql_client import engine
        eng = engine()
        if eng is None:
            return False
        with eng.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        return False


# ================= 配置 =================

@router.get("/api/config")
def get_config(_: str = Depends(require_admin_auth)):
    """读取全部配置项（密码类只返回 ``has_value``，不回明文）。"""
    from .api import ok
    return ok(cc.describe())


@router.post("/api/config")
async def set_config(request: Request,
                     _: str = Depends(require_admin_auth),
                     _c: None = Depends(require_admin_csrf)):
    """批量更新配置。"""
    from .api import ok
    body = await body_of(request)
    updates = body.get("updates")
    if not isinstance(updates, dict) or not updates:
        raise ValidationError("请提供 updates 字典")

    failed = cc.set_many(updates)
    if failed:
        detail = "；".join("%s(%s)" % (f["key"], f["error"]) for f in failed)
        raise ValidationError("配置校验未通过：%s" % detail)

    if "ADMIN_PASSWORD" in updates:
        cc.destroy_other_sessions(cc_session_token(request))
        log.warning("管理密码已更新，其他会话已全部登出")

    from ..core.logging import set_log_level
    set_log_level(cc.get("LOG_LEVEL", "INFO"))
    return ok(message="配置已更新")


@router.post("/api/config/reset")
async def reset_config(request: Request,
                       _: str = Depends(require_admin_auth),
                       _c: None = Depends(require_admin_csrf)):
    """重置配置项。``keys`` 省略或为 null 表示全部重置。"""
    from .api import ok
    body = await body_of(request)
    cc.reset(body.get("keys"))
    return ok(message="已重置")


# ================= 采集源 =================

@router.get("/api/fetchers")
def list_fetchers(_: str = Depends(require_admin_auth)):
    from .api import ok
    try:
        return ok(cc.list_fetchers())
    except Exception as exc:
        raise FreeProxyPoolError("读取采集源列表失败：%s" % type(exc).__name__)


@router.post("/api/fetchers")
async def set_fetcher(request: Request,
                      _: str = Depends(require_admin_auth),
                      _c: None = Depends(require_admin_csrf)):
    """启用/停用采集源，或覆盖其抓取 URL。"""
    from .api import ok
    body = await body_of(request)
    name = validate_name(body.get("name"), name="采集源名")

    if "enabled" in body:
        cc.set_fetcher(name, bool(body["enabled"]))
    if "urls" in body:
        cc.set_fetcher_urls(name, validate_fetch_urls(body["urls"]))
    return ok(message="已更新")


@router.post("/api/fetchers/reset")
async def reset_fetcher(request: Request,
                        _: str = Depends(require_admin_auth),
                        _c: None = Depends(require_admin_csrf)):
    from .api import ok
    body = await body_of(request)
    cc.reset_fetcher(validate_name(body.get("name"), name="采集源名"))
    return ok(message="已重置")


# ================= 代理池 =================

@router.get("/api/proxies")
def list_proxies(request: Request, _: str = Depends(require_admin_auth)):
    """分页查询代理（支持协议 / 采集源 / 地域 / 评分区间 / 延迟分层筛选）。"""
    from .api import ok
    from ..helper.proxy import normalize_protocol, tier_of_proxy, tier_thresholds
    store = get_store()

    page = parse_page(request.query_params.get("page"))
    size = parse_size(request.query_params.get("size"), default=50, max_size=500)
    source = (request.query_params.get("source") or "").strip()
    region = (request.query_params.get("region") or "").strip().upper()
    https_only = parse_bool_param(request.query_params.get("https"))
    score_min = safe_int(request.query_params.get("score_min"), -1, low=-1)
    score_max = safe_int(request.query_params.get("score_max"), -1, low=-1)
    tier = parse_tier(request.query_params.get("tier"))
    protocol = parse_protocol(request.query_params.get("protocol"))

    items = store.get_all()
    if https_only:
        items = [i for i in items if i.get("https")]
    if source:
        items = [i for i in items if source in str(i.get("source") or "")]
    if region:
        items = [i for i in items
                 if str(i.get("region") or "").upper().startswith(region)]
    if score_min >= 0:
        items = [i for i in items if int(i.get("score") or 0) >= score_min]
    if score_max >= 0:
        items = [i for i in items if int(i.get("score") or 0) <= score_max]
    if tier:
        fast_ms, medium_ms = tier_thresholds()
        items = [i for i in items if tier_of_proxy(i, fast_ms, medium_ms) == tier]
    if protocol:
        items = [i for i in items
                 if normalize_protocol(i.get("protocol")) == protocol]

    total = len(items)
    start = (page - 1) * size
    page_items = items[start:start + size]
    # 列表带上延迟分层标签与传输协议，前端直接展示，不必自己算阈值
    fast_ms, medium_ms = tier_thresholds()
    for item in page_items:
        item["tier"] = tier_of_proxy(item, fast_ms, medium_ms)
        item["protocol"] = normalize_protocol(item.get("protocol"))
    return ok({"items": page_items, "total": total,
               "page": page, "size": size})


@router.post("/api/proxies/delete")
async def delete_proxy(request: Request,
                       _: str = Depends(require_admin_auth),
                       _c: None = Depends(require_admin_csrf)):
    from .api import ok
    body = await body_of(request)
    proxy_str = require_proxy(body.get("proxy"))
    get_store().delete(proxy_str)
    return ok(message="已删除")


@router.post("/api/proxies/batch_delete")
async def batch_delete(request: Request,
                       _: str = Depends(require_admin_auth),
                       _c: None = Depends(require_admin_csrf)):
    from .api import ok
    body = await body_of(request)
    proxies = body.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        raise ValidationError("请提供 proxies 数组")
    if len(proxies) > 1000:
        raise ValidationError("单次最多删除 1000 条")

    store = get_store()
    failed = []
    for raw in proxies:
        try:
            store.delete(require_proxy(raw))
        except FreeProxyPoolError:
            failed.append(raw)
    return ok({"failed": failed}, "已删除 %d 条" % (len(proxies) - len(failed)))


@router.post("/api/pool/clear")
def clear_pool(_: str = Depends(require_admin_auth),
               _c: None = Depends(require_admin_csrf)):
    from .api import ok
    get_store().clear()
    return ok(message="代理池已清空")


# ================= 黑名单 =================

@router.get("/api/blacklist")
def get_blacklist(request: Request, _: str = Depends(require_admin_auth)):
    """分页列出黑名单（死代理退避缓存）。

    v0.1.0（P0-2）：条目语义从"拉黑时刻"变为"**解禁时刻**"
    （``blocked_until``），并附 ``fail_count``（连续失败次数 = 退避档位）。
    """
    from .api import ok
    page = parse_page(request.query_params.get("page"))
    size = parse_size(request.query_params.get("size"), default=50, max_size=500)
    return ok(get_store().list_black(page, size))


@router.post("/api/blacklist/add")
async def add_blacklist(request: Request,
                        _: str = Depends(require_admin_auth),
                        _c: None = Depends(require_admin_csrf)):
    """管理员手动拉黑（显式 TTL，不参与退避阶梯计数）。

    手动拉黑是运维的**显式意图**：时长由管理员指定，且不改动
    "连续失败次数"这个退避档位计数器 —— 否则一次手动操作就会
    把代理的退避阶梯顶到 24 小时。
    """
    from .api import ok
    body = await body_of(request)
    proxy_str = require_proxy(body.get("proxy"))
    # v0.1.0 修复：旧版 int(body.get('ttl')) 遇非数字直接 500
    ttl = parse_ttl(body.get("ttl"), int(cc.get("BLACKLIST_TTL", 3600)))
    get_store().add_black(proxy_str, ttl)
    return ok(message="已拉黑 %s（TTL %ds）" % (proxy_str, ttl))


@router.post("/api/blacklist/remove")
async def remove_blacklist(request: Request,
                           _: str = Depends(require_admin_auth),
                           _c: None = Depends(require_admin_csrf)):
    from .api import ok
    body = await body_of(request)
    get_store().remove_black(require_proxy(body.get("proxy")))
    return ok(message="已移除")


@router.post("/api/blacklist/clear")
def clear_blacklist(_: str = Depends(require_admin_auth),
                    _c: None = Depends(require_admin_csrf)):
    from .api import ok
    get_store().clear_black()
    return ok(message="黑名单已清空")


@router.get("/api/audit")
def get_audit(request: Request, _: str = Depends(require_admin_auth)):
    """访问审计（需启用 MySQL 冷存储归档）。

    返回最近的接口访问记录：调用方标识（API Key 的 SHA-256 前 8 位，
    **不存明文密钥**）、端点、状态码、时间。
    """
    from .api import ok
    limit = parse_size(request.query_params.get("limit"), default=100, max_size=1000)
    try:
        from ..storage.mysql_client import archive_enabled, recent_access
        if not archive_enabled():
            return ok({"enabled": False, "items": [],
                       "hint": "未启用归档：请在 .env 中设置 MYSQL_ENABLED=true"})
        return ok({"enabled": True, "items": recent_access(limit)})
    except Exception:
        return ok({"enabled": False, "items": []})


# ================= 日志与告警 =================

@router.get("/api/logs")
def get_logs(request: Request, _: str = Depends(require_admin_auth)):
    from .api import ok
    limit = parse_size(request.query_params.get("limit"), default=200, max_size=1000)
    return ok(read_logs(limit, request.query_params.get("level")))


@router.post("/api/logs/clear")
def clear_logs_api(_: str = Depends(require_admin_auth),
                   _c: None = Depends(require_admin_csrf)):
    from .api import ok
    clear_logs()
    return ok(message="日志已清空")


@router.get("/api/alerts")
def get_alerts(request: Request, _: str = Depends(require_admin_auth)):
    from .api import ok
    limit = parse_size(request.query_params.get("limit"), default=100, max_size=500)
    return ok(read_alerts(limit))


@router.post("/api/alerts/clear")
def clear_alerts_api(_: str = Depends(require_admin_auth),
                     _c: None = Depends(require_admin_csrf)):
    from .api import ok
    clear_alerts()
    return ok(message="告警已清空")


# ================= 运行控制 =================

@router.post("/api/trigger")
async def trigger(request: Request,
                  _: str = Depends(require_admin_auth),
                  _c: None = Depends(require_admin_csrf)):
    """立即触发一轮采集或存量复检。

    v0.1.0 修复：旧版只是往 Redis 写了个标记，**没有任何进程消费它**，
    按钮点了没反应。现在调度器每秒轮询 ``consume_trigger()``，真的会执行。
    """
    from .api import ok
    body = await body_of(request)
    processor = body.get("processor")
    cc.trigger(processor)
    return ok(message="已触发 %s（调度器将在 1 秒内响应）" % processor)


@router.get("/api/process/status")
def process_status(_: str = Depends(require_admin_auth)):
    """各进程运行状态汇总。"""
    from .api import ok
    try:
        depth = ProxyQueue().depth()
    except Exception:
        depth = -1
    try:
        enabled = len(enabled_fetchers())
    except Exception:
        enabled = 0

    # v0.1.0（P1-3）：长任务进度（采集 / 校验 / 存量复检）
    try:
        from ..helper.progress import read as read_progress
        progress = read_progress()
    except Exception:
        progress = {}

    return ok({
        "version": VERSION,
        "fetch_last": cc.get_stat("fetch:last_run", ""),
        "fetch_count": cc.get_stat("fetch:last_total", 0),
        "check_last": cc.get_stat("check:last_run", ""),
        "check_count": cc.get_stat("check:last_total", 0),
        "fetch_interval": cc.get("FETCH_INTERVAL", 5),
        "check_interval": cc.get("CHECK_INTERVAL", 5),
        "fetcher_paused": bool(cc.get("FETCHER_PAUSED", False)),
        "fetcher_enabled": enabled,
        "queue_depth": depth,
        "tripped_list": breaker.tripped_names(),
        "uptime_seconds": int(time.time() - _PROCESS_START),
        "progress": progress,
    })


_PROCESS_START = time.time()


def cc_session_token(request: Request) -> str:
    from .deps import session_token
    return session_token(request)


__all__ = ["router"]