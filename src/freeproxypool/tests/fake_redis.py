# -*- coding: utf-8 -*-
"""内存版 Redis 替身（仅供自检使用）。

覆盖本项目实际用到的命令子集，实现**与 redis-py 一致的返回类型约定**：

* ``decode_responses=True`` → 全部返回 ``str``；
* ``hget`` 命中返回字符串、未命中返回 ``None``；
* ``rpop(key, count)`` 在 key 不存在时返回 ``[]``（Redis 6.2+ 语义）；
* ``expire`` 在 key 不存在时返回 ``False``；
* ``pipeline()`` 返回可链式调用并支持 ``execute()`` 的上下文。

存在意义：让"鉴权链路 / 参数校验 / 分发策略 / 队列背压"这些
**不依赖真实 Redis 也能验证的逻辑**有可执行的回归测试，
部署到任何环境前先跑 ``python proxyPool.py selftest``。
"""
import fnmatch
import time


class FakePipeline:
    """命令缓冲队列。"""

    def __init__(self, store: "FakeRedis"):
        self._store = store
        self._queue = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self._queue.append((name, args, kwargs))
            return self
        return _record

    def execute(self):
        results = []
        for name, args, kwargs in self._queue:
            results.append(self._store.execute(name, args, kwargs))
        self._queue.clear()
        return results

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRedis:
    """内存 Redis。线程安全由自检的单线程模型保证。"""

    def __init__(self):
        self._hash = {}        # key -> {field: str}
        self._zset = {}        # key -> {member: float}
        self._set = {}         # key -> set
        self._list = {}        # key -> list
        self._string = {}      # key -> (value, expire_at)

    # ---------- 直通命令（redis-py 风格） ----------

    def __getattr__(self, name):
        """把 ``hset`` / ``zadd`` 之类的公开命令转发到 ``_cmd_`` 实现。

        同时兼容 pipeline 场景：``fake.hset(...)`` 与
        ``fake.pipeline().hset(...)`` 走的是同一套实现。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        command = "_cmd_" + name
        if not hasattr(type(self), command):
            raise AttributeError(
                "FakeRedis 未实现命令：%s" % name)

        def _call(*args, **kwargs):
            return self.execute(name, args, kwargs)
        return _call

    # ---------- 生命周期 ----------

    def close(self):
        self._closed = True

    _closed = False

    def ping(self):
        if self._closed:
            raise ConnectionError("FakeRedis closed")
        return True

    def info(self, section=None):
        return {"used_memory": 1024 * 1024}

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    # ---------- 过期 ----------

    def _alive(self, key) -> bool:
        item = self._string.get(key)
        if item and item[1] and item[1] < time.time():
            self._string.pop(key, None)
            return False
        return True

    # ---------- 命令分发 ----------

    def execute(self, name, args, kwargs):
        handler = getattr(self, "_cmd_" + name, None)
        if handler is None:
            raise NotImplementedError("FakeRedis 未实现命令：%s" % name)
        return handler(*args, **kwargs)

    # ---- String ----

    def _cmd_get(self, key):
        if not self._alive(key):
            return None
        item = self._string.get(key)
        return None if item is None else str(item[0])

    def _cmd_set(self, key, value, ex=None):
        self._string[key] = (str(value), time.time() + ex if ex else 0)
        return True

    def _cmd_setex(self, key, ttl, value):
        return self._cmd_set(key, value, ex=ttl)

    def _cmd_incr(self, key):
        current = int(self._cmd_get(key) or 0) + 1
        self._string[key] = (str(current), self._string.get(key, (None, 0))[1])
        return current

    # ---- Key ----

    def _cmd_delete(self, *keys):
        removed = 0
        for key in keys:
            hit = False
            for bucket in (self._hash, self._zset, self._set, self._list, self._string):
                if key in bucket:
                    bucket.pop(key)
                    hit = True
            if hit:
                removed += 1
        return removed

    def _cmd_exists(self, key):
        """判断 key 是否存在。

        旧实现写成 ``... or self._alive(key)``，而 ``_alive()`` 对
        **不存在的 key 返回 True**（它只负责判断"是否已过期"），
        于是 ``exists`` 对任何 key 都返回 1 —— 靠它做门控的逻辑
        （如地域索引完整性标记）会被静默放行。
        """
        if key in self._hash or key in self._zset \
                or key in self._set or key in self._list:
            return 1
        item = self._string.get(key)
        if item is None:
            return 0
        if item[1] and item[1] < time.time():
            self._string.pop(key, None)
            return 0
        return 1

    def _cmd_expire(self, key, ttl):
        return key in self._hash or key in self._zset or key in self._string

    def _cmd_llen(self, key):
        return len(self._list.get(key, []))

    # ---- Hash ----

    def _cmd_hset(self, key, field=None, value=None, mapping=None):
        bucket = self._hash.setdefault(key, {})
        added = 0
        if field is not None:
            added = 0 if field in bucket else 1
            bucket[field] = str(value)
        for k, v in (mapping or {}).items():
            added += 0 if k in bucket else 1
            bucket[k] = str(v)
        return added

    def _cmd_hget(self, key, field):
        return self._hash.get(key, {}).get(field)

    def _cmd_hdel(self, key, *fields):
        bucket = self._hash.get(key, {})
        return sum(1 for f in fields if bucket.pop(f, None) is not None)

    def _cmd_hgetall(self, key):
        return dict(self._hash.get(key, {}))

    def _cmd_hkeys(self, key):
        return list(self._hash.get(key, {}).keys())

    def _cmd_hvals(self, key):
        return list(self._hash.get(key, {}).values())

    def _cmd_hlen(self, key):
        return len(self._hash.get(key, {}))

    def _cmd_hexists(self, key, field):
        return field in self._hash.get(key, {})

    def _cmd_hmget(self, key, fields):
        bucket = self._hash.get(key, {})
        return [bucket.get(f) for f in fields]

    # ---- ZSet ----

    def _cmd_zadd(self, key, mapping):
        bucket = self._zset.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in bucket:
                added += 1
            bucket[member] = float(score)
        return added

    def _cmd_zrem(self, key, *members):
        bucket = self._zset.get(key, {})
        return sum(1 for m in members if bucket.pop(m, None) is not None)

    def _cmd_zscore(self, key, member):
        return self._zset.get(key, {}).get(member)

    def _cmd_zcard(self, key):
        return len(self._zset.get(key, {}))

    def _cmd_zcount(self, key, minimum, maximum):
        return len(self._zset.get(key, {}))

    def _sorted(self, key, reverse=False):
        items = sorted(self._zset.get(key, {}).items(),
                       key=lambda kv: kv[1], reverse=reverse)
        return items

    def _cmd_zrange(self, key, start, end, withscores=False, **kwargs):
        items = self._sorted(key)
        sliced = items[start:end + 1] if end >= 0 else items[start:]
        return sliced if withscores else [m for m, _ in sliced]

    def _cmd_zrevrange(self, key, start, end, withscores=False, **kwargs):
        items = self._sorted(key, reverse=True)
        sliced = items[start:end + 1] if end >= 0 else items[start:]
        return sliced if withscores else [m for m, _ in sliced]

    def _cmd_zrangebyscore(self, key, minimum, maximum, start=None, num=None):
        """按分数区间取成员，支持 ``LIMIT offset count``。

        ``start=0`` 是合法偏移，而且生产代码就在这么用
        （``RedisStore.evict_expired`` 传 ``start=0, num=batch``）。
        旧实现用 ``if start:`` 判断，``0`` 被当成"没传"而跳过整个分片逻辑，
        批量回收的 ``num`` 上限静默失效 —— 自检因此永远发现不了
        "一次回收远超一批"的问题。
        """
        items = [(m, s) for m, s in self._sorted(key)
                 if float(minimum) <= s <= float(maximum)]
        if start is not None:
            offset = int(start)
            if num is None or int(num) < 0:
                items = items[offset:]
            else:
                items = items[offset:offset + int(num)]
        return [m for m, _ in items]

    def _cmd_zremrangebyscore(self, key, minimum, maximum):
        bucket = self._zset.get(key, {})
        drop = [m for m, s in bucket.items() if float(minimum) <= s <= float(maximum)]
        for member in drop:
            bucket.pop(member)
        return len(drop)

    # ---- Set ----

    def _cmd_sadd(self, key, *members):
        bucket = self._set.setdefault(key, set())
        added = 0
        for member in members:
            if member not in bucket:
                bucket.add(member)
                added += 1
        return added

    # ---- List ----

    def _cmd_lpush(self, key, *values):
        bucket = self._list.setdefault(key, [])
        for value in values:
            bucket.insert(0, str(value))
        return len(bucket)

    def _cmd_rpush(self, key, *values):
        bucket = self._list.setdefault(key, [])
        bucket.extend(str(v) for v in values)
        return len(bucket)

    def _cmd_lrange(self, key, start, end):
        bucket = self._list.get(key, [])
        return bucket[start:end + 1] if end >= 0 else bucket[start:]

    def _cmd_ltrim(self, key, start, end):
        bucket = self._list.get(key, [])
        self._list[key] = bucket[start:end + 1] if end >= 0 else bucket[start:]

    def _cmd_rpop(self, key, count=None):
        bucket = self._list.get(key, [])
        if count is None:
            return bucket.pop() if bucket else None
        taken = []
        while bucket and len(taken) < count:
            taken.append(bucket.pop())
        return taken


__all__ = ["FakeRedis"]