# -*- coding: utf-8 -*-
"""MySQL 冷存储归档层。

## 定位

**只做归档，不参与实时业务**。有效代理的实时读写全在 Redis；
MySQL 存的是"历史快照"，用于事后追溯：这个代理什么时候被采到、
什么时候失效、来自哪个源、被谁分发过。

## v0.1.0 变更

旧版本模块有 8 个对外方法，其中 6 个（``record_proxy_history``、
``record_access_log``、``save_source_config``、``load_source_configs``、
``set_system_config``、``get_system_config``）**从未被任何代码调用** ——
是一整块死代码，同时 MySQL 容器却一直在跑、白白占用资源。

v0.1.0 的选择是**把它们接进业务**而不是删掉，因为它们承载的
"访问审计 + 代理历史"恰恰是安全审计报告需要的数据来源：

* :func:`archive_proxy` —— 代理入池 / 更新时写一条历史快照；
* :func:`archive_access` —— 网关分发请求写一条访问日志；
* :func:`archive_source_config` / :func:`load_source_configs` —— 采集源配置归档；
* :func:`save_system_config` / :func:`load_system_config` —— 系统配置快照。

所有方法在未启用或异常时返回 ``None``/``False``/``[]``，绝不阻塞主流程。
"""
from datetime import datetime
from typing import List, Optional

from .. import config_center as cc
from ..core.logging import get_logger
from .models import AccessLog, Base, ProxyHistory, SourceConfig, SystemConfig

log = get_logger("archive", to_file=False)

_engine = None
_session_factory = None


def archive_enabled() -> bool:
    """MySQL 归档是否启用。"""
    try:
        return bool(cc.get("MYSQL_ENABLED", False))
    except Exception:
        return False


def _build_url() -> str:
    """拼接连接串（账号密码做 URL 编码，避免特殊字符歧义）。"""
    from urllib.parse import quote_plus
    user = quote_plus(str(cc.get("MYSQL_USER", "fpp")))
    password = quote_plus(str(cc.get("MYSQL_PASSWORD", "")))
    host = str(cc.get("MYSQL_HOST", "127.0.0.1"))
    port = int(cc.get("MYSQL_PORT", 3306))
    database = str(cc.get("MYSQL_DB", "fpp"))
    return "mysql+pymysql://%s:%s@%s:%d/%s?charset=utf8mb4" % (
        user, password, host, port, database)


def engine():
    """惰性创建并缓存 SQLAlchemy engine（未启用返回 ``None``）。"""
    global _engine
    if not archive_enabled():
        return None
    if _engine is None:
        try:
            from sqlalchemy import create_engine
            _engine = create_engine(_build_url(), pool_pre_ping=True,
                                    pool_recycle=1800, pool_size=5, max_overflow=10)
        except Exception as exc:
            log.error("创建 MySQL engine 失败：%s", type(exc).__name__)
            _engine = None
    return _engine


def _session():
    global _session_factory
    if _session_factory is None:
        eng = engine()
        if eng is None:
            return None
        try:
            from sqlalchemy.orm import sessionmaker
            _session_factory = sessionmaker(bind=eng, expire_on_commit=False)
        except Exception as exc:
            log.error("创建 MySQL session 失败：%s", type(exc).__name__)
            return None
    try:
        return _session_factory()
    except Exception as exc:
        log.error("获取 MySQL session 失败：%s", type(exc).__name__)
        return None


def ensure_tables() -> bool:
    """幂等建表。"""
    eng = engine()
    if eng is None:
        return False
    try:
        Base.metadata.create_all(eng)
        return True
    except Exception as exc:
        log.error("建表失败：%s", type(exc).__name__)
        return False


# ================= 归档写入 =================

def archive_proxy(proxy_dict: dict, event: str = "validated") -> bool:
    """记录一条代理历史快照。

    :param proxy_dict: 代理元数据
    :param event: ``validated``（入池/复检通过）或 ``removed``（淘汰）
    """
    if not archive_enabled() or not proxy_dict:
        return False
    session = _session()
    if session is None:
        return False
    try:
        source = proxy_dict.get("source")
        if isinstance(source, (list, tuple)):
            source = "/".join(source)
        session.add(ProxyHistory(
            proxy=str(proxy_dict.get("proxy", ""))[:128],
            protocol="https" if proxy_dict.get("https") else "http",
            anonymous=str(proxy_dict.get("anonymous") or "")[:32],
            region=str(proxy_dict.get("region") or "")[:64],
            speed=float(proxy_dict.get("speed") or 0.0),
            status=event,
            score=int(proxy_dict.get("score") or 0),
            source=str(source or "")[:128],
            check_time=datetime.now(),
        ))
        session.commit()
        return True
    except Exception:
        _rollback(session)
        return False
    finally:
        _close(session)


def archive_access(client_id: str, endpoint: str,
                   proxy: Optional[str], status_code: int) -> bool:
    """记录一条接口访问日志（安全审计用）。"""
    if not archive_enabled():
        return False
    session = _session()
    if session is None:
        return False
    try:
        session.add(AccessLog(
            client_id=str(client_id or "")[:128],
            endpoint=str(endpoint or "")[:256],
            proxy=str(proxy)[:128] if proxy else None,
            status_code=int(status_code or 0),
            created_at=datetime.now(),
        ))
        session.commit()
        return True
    except Exception:
        _rollback(session)
        return False
    finally:
        _close(session)


def archive_source_config(name: str, urls, enabled: bool, **extra) -> bool:
    """归档一条采集源配置（按 name 唯一 upsert）。"""
    if not archive_enabled() or not name:
        return False
    session = _session()
    if session is None:
        return False
    try:
        row = session.query(SourceConfig).filter(SourceConfig.name == name).first()
        if row is None:
            row = SourceConfig(name=str(name)[:128])
            session.add(row)
        if isinstance(urls, (list, tuple)):
            urls = "\n".join(str(u) for u in urls)
        row.urls = str(urls or "")
        row.enabled = bool(enabled)
        row.updated_at = datetime.now()
        session.commit()
        return True
    except Exception:
        _rollback(session)
        return False
    finally:
        _close(session)


def save_system_config(key: str, value) -> bool:
    """写入系统配置快照（按 key 唯一 upsert）。"""
    if not archive_enabled() or not key:
        return False
    session = _session()
    if session is None:
        return False
    try:
        row = session.query(SystemConfig).filter(SystemConfig.key == key).first()
        if row is None:
            row = SystemConfig(key=str(key)[:128])
            session.add(row)
        row.value = str(value)
        session.commit()
        return True
    except Exception:
        _rollback(session)
        return False
    finally:
        _close(session)


# ================= 归档读取 =================

def load_source_configs() -> List[dict]:
    """读取全部采集源配置归档。"""
    if not archive_enabled():
        return []
    session = _session()
    if session is None:
        return []
    try:
        return [{"name": r.name, "urls": r.urls, "enabled": r.enabled,
                 "updated_at": r.updated_at}
                for r in session.query(SourceConfig).all()]
    except Exception:
        return []
    finally:
        _close(session)


def load_system_config(key: str) -> Optional[str]:
    """读取一条系统配置快照。"""
    if not archive_enabled() or not key:
        return None
    session = _session()
    if session is None:
        return None
    try:
        row = session.query(SystemConfig).filter(SystemConfig.key == key).first()
        return row.value if row else None
    except Exception:
        return None
    finally:
        _close(session)


def recent_access(limit: int = 100) -> List[dict]:
    """读取最近的访问日志（管理后台审计视图）。"""
    if not archive_enabled():
        return []
    session = _session()
    if session is None:
        return []
    try:
        rows = session.query(AccessLog).order_by(
            AccessLog.id.desc()).limit(max(1, min(limit, 1000))).all()
        return [{"id": r.id, "client_id": r.client_id, "endpoint": r.endpoint,
                 "proxy": r.proxy, "status_code": r.status_code,
                 "created_at": r.created_at} for r in rows]
    except Exception:
        return []
    finally:
        _close(session)


def _rollback(session) -> None:
    try:
        session.rollback()
    except Exception:
        pass


def _close(session) -> None:
    try:
        session.close()
    except Exception:
        pass


__all__ = [
    "archive_enabled", "engine", "ensure_tables",
    "archive_proxy", "archive_access", "archive_source_config",
    "save_system_config", "load_source_configs", "load_system_config",
    "recent_access",
]