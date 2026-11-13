# -*- coding: utf-8 -*-
"""MySQL 冷存储归档层 ORM 模型（SQLAlchemy 2.0 声明式）。

四张表全部只服务"事后追溯"，不参与实时代理分发与校验：

=========================  ==========================================
表                          用途
=========================  ==========================================
``proxy_history``          代理历史快照：何时入池、何时淘汰、来源、评分
``access_log``             对外接口访问日志（安全审计来源）
``source_config``          采集源配置归档
``system_config``          系统配置快照（键值对）
=========================  ==========================================
"""
from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, Float, Index, Integer,
                        String, Text)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class SourceConfig(Base):
    """采集源配置表。"""

    __tablename__ = "source_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), unique=True, nullable=False)
    urls = Column(Text, nullable=False, default="")
    enabled = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ProxyHistory(Base):
    """代理历史快照表。

    加上 ``(proxy, check_time)`` 联合索引 —— 审计查询几乎总是
    "某个代理最近发生了什么"，没有索引时全表扫描会越来越慢。
    """

    __tablename__ = "proxy_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proxy = Column(String(128), nullable=False, index=True)
    protocol = Column(String(16), default="http")
    anonymous = Column(String(32), default="")
    region = Column(String(64), default="", index=True)
    speed = Column(Float, default=0.0)
    status = Column(String(16), default="validated")
    score = Column(Integer, default=0)
    source = Column(String(128), default="")
    check_time = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (Index("ix_proxy_history_proxy_time", "proxy", "check_time"),)


class AccessLog(Base):
    """对外接口访问日志表。"""

    __tablename__ = "access_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String(128), default="")
    endpoint = Column(String(256), default="")
    proxy = Column(String(128), nullable=True)
    status_code = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now, index=True)


class SystemConfig(Base):
    """系统配置快照表。"""

    __tablename__ = "system_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(128), unique=True, nullable=False)
    value = Column(Text, nullable=True)


__all__ = ["Base", "SourceConfig", "ProxyHistory", "AccessLog", "SystemConfig"]