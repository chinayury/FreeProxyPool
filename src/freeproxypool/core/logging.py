# -*- coding: utf-8 -*-
"""日志器工厂。

v0.1.0 修复的两个真实缺陷：

1. **日志在 Redis 中重复写入**。
   旧版 :class:`handler.logHandler.LogHandler` 在每次实例化时给自己挂一个
   ``RedisLogHandler``，而 :func:`log_sink.attach_log_sink` 又往 root logger
   挂了一个。同一条日志既走自己的 handler 又冒泡到 root handler，
   在管理后台日志面板里表现为**每条日志出现两遍**。
   → 现在 Redis 汇聚 handler **只允许由** :func:`attach_redis_sink` **挂载一次**
     （挂到 root），业务 logger 一律不带 Redis handler。

2. **Logger / 文件句柄泄漏**。
   旧版每次 ``LogHandler("fetcher")`` 都会新建一个 ``logging.Logger``
   并打开一个新的 ``TimedRotatingFileHandler``；而采集链路每轮请求都会
   ``WebRequest()`` → 新建 logger，长跑进程会不断堆积 handler 与文件描述符。
   → 现在 logger **按名字缓存复用**，同名多次获取返回同一实例。
"""
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
from typing import Optional

from .redact import redact

_LOCK = threading.RLock()
_LOGGERS = {}

#: 日志目录：环境变量 FPP_LOG_DIR 可覆盖，缺省为当前工作目录下的 logs/
LOG_DIR = os.environ.get("FPP_LOG_DIR") or os.path.join(os.getcwd(), "logs")

DEFAULT_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(filename)s:%(lineno)d %(message)s"

#: 配置中心未就绪时的日志级别（配置中心可用后由 set_log_level 校正）
_level = logging.INFO


class _RedactFilter(logging.Filter):
    """对所有 handler 统一做凭据脱敏。

    旧实现把 ``record.msg`` 就地改写并清空 ``record.args``，
    由于同一个 LogRecord 会被多个 handler 依次消费，
    第一个 handler 的改写会影响后续 handler 的输出。
    这里改为"只在输出阶段替换"，不污染 LogRecord 本身。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:      # 脱敏失败绝不能影响业务日志输出
            pass
        return True


def set_log_level(level: str) -> None:
    """设置全局日志级别（由配置中心启动后调用）。"""
    global _level
    resolved = getattr(logging, str(level).upper(), None)
    if isinstance(resolved, int):
        _level = resolved
        for logger in _LOGGERS.values():
            logger.setLevel(resolved)


def get_log_level() -> str:
    return logging.getLevelName(_level)


def _ensure_log_dir() -> Optional[str]:
    """确保日志目录存在；不可写时返回 ``None``（降级为只输出到控制台）。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        return LOG_DIR
    except OSError:
        return None


def get_logger(name: str = "fpp", *, to_file: Optional[bool] = None,
               stream: bool = True) -> logging.Logger:
    """获取（并缓存）一个 logger。

    :param name: logger 名称，通常是模块名或角色名（``gateway`` / ``validator`` ...）
    :param to_file: 是否写文件；``None`` 表示按平台自动决定（Windows 不写文件）
    :param stream: 是否输出到标准输出
    """
    key = (name, bool(to_file), bool(stream))
    with _LOCK:
        existing = _LOGGERS.get(key)
        if existing is not None:
            return existing

        logger = logging.getLogger(name)
        logger.setLevel(_level)
        # propagate 保持 True：日志冒泡到 root，由 root 上的统一 sink 负责汇聚。
        # 业务 logger 自身**不挂** RedisLogHandler，避免重复写入。
        logger.propagate = True
        logger.handlers.clear()

        redactor = _RedactFilter()
        formatter = logging.Formatter(DEFAULT_FORMAT)

        if stream:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(formatter)
            handler.addFilter(redactor)
            logger.addHandler(handler)

        if to_file is None:
            # 平台自动决定：Windows 开发机默认不落文件（避免日志目录权限噪音）；
            # 是否写文件最终由 LOG_TO_FILE 配置说了算 —— 该配置项此前已声明却
            # 没有任何一处读取，属于"配了不生效"。
            to_file = platform.system() != "Windows"
            if to_file:
                try:
                    from .. import config_center as cc
                    to_file = bool(cc.get("LOG_TO_FILE", True))
                except Exception:
                    pass
        if to_file:
            log_dir = _ensure_log_dir()
            if log_dir:
                file_handler = logging.handlers.TimedRotatingFileHandler(
                    filename=os.path.join(log_dir, "%s.log" % name.replace("/", "_")),
                    when="D", interval=1, backupCount=15,
                )
                file_handler.suffix = "%Y%m%d"
                file_handler.setFormatter(formatter)
                file_handler.addFilter(redactor)
                logger.addHandler(file_handler)

        # 避免同一日志被 root 与自身重复输出
        logger.propagate = True
        _LOGGERS[key] = logger
        return logger


# ---------- Redis 日志汇聚（单例，仅挂 root 一次） ----------

class RedisLogHandler(logging.Handler):
    """把日志批量汇聚到 Redis，供管理后台日志面板读取。

    采用**进程内缓冲 + 批量 flush**，而不是每条日志一次网络往返：
    采集高峰期每轮可产生数千条日志，逐条 LPUSH 会让日志链路成为瓶颈。
    """

    def __init__(self, redis_key: str, max_logs: int = 2000,
                 buffer_size: int = 200, flush_interval: float = 0.5):
        super().__init__()
        self.redis_key = redis_key
        self.max_logs = max_logs
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self._buffer = []
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact(self.format(record))
        except Exception:
            try:
                message = redact(record.getMessage())
            except Exception:
                return
        # 注意：formatTime 是 logging.Formatter 的方法，不是 logging.Handler 的。
        # 曾经误用 self.formatTime(record, ...) 导致所有挂载了本 handler 的
        # 进程（网关 / 采集 / 验证 / 调度）在第一条日志上就抛 AttributeError。
        line = "%s %-8s %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
            record.levelname,
            message,
        )
        with self._lock:
            self._buffer.append(line)
            if len(self._buffer) >= self.buffer_size:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        pending, self._buffer = self._buffer, []
        try:
            from .. import config_center as cc
            conn = cc.get_redis()
            pipe = conn.pipeline()
            for line in pending:
                pipe.lpush(self.redis_key, line)
            pipe.ltrim(self.redis_key, 0, self.max_logs - 1)
            pipe.execute()
        except Exception:
            # 日志汇聚失败静默丢弃，绝不影响业务（这是观测链路，不是控制链路）
            pass

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()


_sink_state = {"handler": None, "thread": None, "running": False}


def attach_redis_sink(redis_key: str, max_logs: int = 2000) -> bool:
    """把 Redis 日志汇聚 handler 挂到 root logger（幂等）。

    :return: True 表示本次调用真正启动了汇聚；False 表示此前已启动（幂等返回）
    """
    with _LOCK:
        if _sink_state["running"]:
            return False

        handler = RedisLogHandler(redis_key=redis_key, max_logs=max_logs)
        root = logging.getLogger()
        # root 自身的 level 会拦截子 logger 冒泡上来的 DEBUG/INFO，
        # 这里显式设为最低，确保不丢日志。
        if root.level > logging.DEBUG:
            root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        _sink_state["handler"] = handler
        _sink_state["running"] = True

        def _loop():
            import time
            while _sink_state["running"]:
                time.sleep(handler.flush_interval)
                try:
                    handler.flush()
                except Exception:
                    pass

        thread = threading.Thread(target=_loop, name="fpp-log-sink", daemon=True)
        thread.start()
        _sink_state["thread"] = thread
        return True


def detach_redis_sink() -> None:
    """摘除 Redis 汇聚 handler（测试用）。"""
    with _LOCK:
        if not _sink_state["running"]:
            return
        _sink_state["running"] = False
        handler = _sink_state.pop("handler", None)
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.flush()
        _sink_state.pop("thread", None)


def read_logs(limit: int = 200, level: Optional[str] = None) -> list:
    """读取最近的日志（管理后台日志面板使用）。"""
    from .. import config_center as cc
    from ..log_sink import LOG_KEY
    limit = max(1, min(int(limit or 200), 1000))
    try:
        lines = cc.get_redis().lrange(LOG_KEY, 0, limit - 1)
    except Exception:
        return []
    if level and level != "ALL":
        wanted = str(level).upper()
        lines = [line for line in lines if _level_matches(line, wanted)]
    return lines


def _level_matches(line: str, level: str) -> bool:
    """日志行是否匹配级别。

    行格式为 ``日期 时间 级别      消息``（见 :meth:`RedisLogHandler.emit`），
    级别是第 3 个以空白分隔的 token。

    v0.1.0 正式版修复：旧实现用 ``split(" ", 2)`` 后取 ``parts[1]``，
    拿到的是**时间**（``00:00:00``），于是管理后台按级别筛选永远返回空列表，
    且不报任何错 —— 用户只会以为"这个级别没有日志"。
    """
    parts = line.split(" ", 3)
    return len(parts) >= 3 and parts[2].strip() == level


def clear_logs() -> None:
    """清空 Redis 中的日志列表。"""
    from .. import config_center as cc
    from ..log_sink import LOG_KEY
    try:
        cc.get_redis().delete(LOG_KEY)
    except Exception:
        pass


__all__ = [
    "get_logger", "set_log_level", "get_log_level",
    "attach_redis_sink", "detach_redis_sink", "RedisLogHandler",
    "read_logs", "clear_logs", "LOG_DIR",
]