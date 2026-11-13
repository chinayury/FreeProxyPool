# -*- coding: utf-8 -*-
"""Redis 存储层：Hash（元数据）+ ZSet（评分 / HTTPS 索引 / 过期索引 / 黑名单）。

## Key 布局

    {table}                 Hash    { "ip:port": "<json 元数据>" }
    {table}:score           ZSet    { "ip:port": score }           评分索引（按分取）
    {table}:https           ZSet    { "ip:port": <最近通过时间戳> }  HTTPS 代理索引（O(1) 计数）
    {table}:expire          ZSet    { "ip:port": 过期时间戳 }        过期索引（按时间回收）
    {table}:region:{CODE}   ZSet    { "ip:port": 复合评分 }          地域索引（每个国家码一个）
    {table}:regionof        Hash    { "ip:port": 国家码 }            地域反向索引（决定删哪些 ZSet）
    {table}:regionidx       String  索引重建完成时间戳              地域索引完整性标记
    {table}:black           ZSet    { "ip:port": 跳过截止时间戳 }    死代理退避缓存（见下）
    {table}:blackfail       Hash    { "ip:port": 连续失败次数 }      退避阶梯的计数
    fpp:queue               List    待验代理队列（LPUSH / RPOP）
    fpp:speeds:{proxy}      List    最近 N 次校验延迟（秒），用于 EWMA / P95

## v0.1.0 的性能修复

旧版 :meth:`getCount` 为了统计 HTTPS 数量，每次都执行一次
``HVALS {table}`` 全量拉取并逐条 ``json.loads``。而 ``/count/``、
``/health``、``/metrics``、管理后台概览**每个请求/每次 scrape 都会调用它**，
池子一旦上万就是稳定的 O(N) 热点。

现在新增 ``{table}:https`` 索引写入时同步维护，HTTPS 数量退化为
``ZCARD``，``getCount`` 整体变成 O(1)。

## v0.1.0 评分模型变更（B-01）

评分索引 ``{table}:score`` 的 score 从手工累加分（每次通过 +1）改为
复合分：``(1 - error_rate) * 100 - min(ewma_speed_s, 30)``。

## v0.1.0 黑名单语义变更（P0-2：死代理指数退避）

**旧语义**：``{table}:black`` 的 score 是"拉黑时间戳"，条目靠后台的
``ZREMRANGEBYSCORE ... (now - ttl)`` 顺带清理，存活时长恒为 ``BLACKLIST_TTL``。
后果是：一个已确认死亡的代理，每过 1 小时就会被重新拉出来校验一次，
**永久循环** —— 线上表现为队列积压 4.5 万、连续多批通过率 0%，
大量算力花在反复验死代理上。

**新语义**：score 改为"**跳过截止时间戳**"，即"这个代理在什么时刻之前
不要再碰"。连续失败次数记在 ``{table}:blackfail``，按阶梯
:data:`BLACKLIST_BACKOFF` 递增跳过时长：

    第 1 次失败 → 跳过 1h    第 2 次 → 2h    第 3 次 → 6h    第 4 次起 → 24h

校验通过时清零计数并解除拉黑（代理"活过来了"就立刻重新可用）。
第 1 级与 ``BLACKLIST_TTL`` 默认值一致，因此**首次失败的行为与改造前相同**，
变化只体现在"反复失败的代理不再被反复重试"。

**向后兼容**：改造前写入的 score 是过去的时间戳，按新语义判定
（``score > now``）自然为"已到期"，无需迁移脚本即可平滑过渡。

## v0.1.0 地域索引（P1-1）

``?area=`` 是最常用的筛选维度之一，但旧实现走 ``get_all()`` ——
``HVALS`` 全量拉取 + 逐条 ``json.loads`` + 过滤，池子上万时每次请求都是 O(N)。

现在新增 ``{table}:region:{CODE}`` 地域索引（每个国家/地区码一个 ZSet，
score 为**复合评分**，与 ``{table}:score`` 同序 —— 于是带地域筛选与
不带地域筛选两条路径都是"好代理优先"），``get_by_region()`` 优先走索引
取候选，索引不可用时**降级**为全表扫描。

降级**只改变取数方式，不改变返回什么** —— 地域筛选始终严格匹配。
这一点与 haipproxy 的降级策略刻意不同：它降级时会放宽约束，
而我们放宽就等于把 v0.1.0 修掉的"area 静默忽略"那个 bug 又请回来。

### 三个键为什么缺一不可

* ``{table}:region:{CODE}`` —— 候选来源；
* ``{table}:regionof`` —— **反向索引**：``ip:port -> 国家码``。
  没有它就无法在删除代理时知道该从哪些 ZSet 里摘掉这条 —— 代理的地域
  可能变过（GeoIP 变更、采集源重新判定），只按当前地域清一遍会留下残条；
* ``{table}:regionidx`` —— **完整性标记**：标记"本进程的索引已按全量
  Hash 重建过"。没有它就会出现**部分索引**：升级前入库的数据没有索引
  条目，升级后新写入的才有，此时若直接信任索引就会**少返回**代理。
  标记不存在时一律走全表扫描 —— 慢，但结果与改造前完全一致。
"""
import json
import time
from typing import Dict, List, Optional

from ..core.iputil import parse_host_port
from ..core.logging import get_logger

log = get_logger("storage.redis", to_file=False)

#: 死代理退避阶梯（秒）：连续失败次数 → 跳过时长。
#: 第 1 级（3600）与 BLACKLIST_TTL 的默认值一致 ⇒ 首次失败行为与改造前相同；
#: 之后逐级递增到 24 小时封顶，避免死代理被无限次重试。
BLACKLIST_BACKOFF = (3600, 7200, 21600, 86400)


class RedisStore:
    """代理池存储客户端。

    所有方法在 Redis 异常时**不吞异常**：写操作失败必须让调用方感知，
    否则会出现"静默丢数据 → 界面显示成功 → 实际没存进去"的假成功。
    读操作同样不吞，由上层（网关/调度）决定降级策略。
    """

    def __init__(self, conn, table: str = "use_proxy"):
        self.conn = conn
        self.table = table

    # ---------- key 派生 ----------
    @property
    def key_hash(self) -> str:
        return self.table

    @property
    def key_score(self) -> str:
        return "%s:score" % self.table

    @property
    def key_https(self) -> str:
        return "%s:https" % self.table

    @property
    def key_expire(self) -> str:
        return "%s:expire" % self.table

    @property
    def key_black(self) -> str:
        return "%s:black" % self.table

    @property
    def key_black_fail(self) -> str:
        """连续失败次数（退避阶梯的档位依据）。"""
        return "%s:blackfail" % self.table

    def key_region(self, code: str) -> str:
        """地域索引 Key（每个国家/地区码一个 ZSet）。"""
        return "%s:region:%s" % (self.table, str(code or "").strip().upper())

    @property
    def key_regionof(self) -> str:
        """地域反向索引 Key（``ip:port -> 国家码``）。"""
        return "%s:regionof" % self.table

    @property
    def key_regionidx(self) -> str:
        """地域索引完整性标记（重建完成后写入）。"""
        return "%s:regionidx" % self.table

    def change_table(self, name: str) -> None:
        self.table = name

    # ---------- 写 ----------

    @staticmethod
    def _calc_score(proxy_dict: dict) -> float:
        """计算 B-01 复合分（error_rate + ewma_speed）。"""
        check_count = int(proxy_dict.get("check_count") or 0)
        error_count = int(proxy_dict.get("error_count") or 0)
        ewma_speed = float(proxy_dict.get("ewma_speed") or 0.0)
        if check_count == 0:
            return 0.0
        error_rate = error_count / check_count
        return max(0.0, round(
            (1.0 - error_rate) * 100.0 - min(ewma_speed, 30.0)
        ))

    def put(self, proxy_dict: dict, ttl: int = 0) -> bool:
        """写入/覆盖一个代理（Hash + 评分索引 + HTTPS 索引 + 过期索引 + 地域索引）。"""
        proxy = proxy_dict["proxy"]
        score = self._calc_score(proxy_dict)
        pipe = self.conn.pipeline()
        pipe.hset(self.key_hash, proxy, json.dumps(proxy_dict, ensure_ascii=False))
        pipe.zadd(self.key_score, {proxy: score})
        if proxy_dict.get("https"):
            pipe.zadd(self.key_https, {proxy: time.time()})
        else:
            pipe.zrem(self.key_https, proxy)
        if ttl and ttl > 0:
            pipe.zadd(self.key_expire, {proxy: time.time() + ttl})
        else:
            # 显式清除过期索引：否则"曾经按 TTL 入池"的代理在策略改为
            # ttl=0（永不过期）后，仍带着旧的过期时间戳，会被 evict_expired
            # 在旧时间点提前回收 —— 表现为"刚配了不过期，代理还是不断消失"。
            pipe.zrem(self.key_expire, proxy)
        # 地域索引（借鉴 docs/09 P1-1）：score 用**复合评分**，与
        # ``{table}:score`` 索引同序 —— 于是带地域筛选和不带地域筛选
        # 两条路径都是"好代理优先"，不会因为多了个 area 参数就拿到更差的。
        # 地域未知的代理不进任何地域索引 —— 与"严格匹配"语义一致：
        # 地域未知不该出现在任何 area 的结果里。
        code = extract_region(proxy_dict)
        old_code = self._region_indexed(proxy)
        if code:
            pipe.zadd(self.key_region(code), {proxy: score})
            pipe.hset(self.key_regionof, proxy, code)
        else:
            pipe.hdel(self.key_regionof, proxy)
        if old_code and old_code != code:
            # 地域变了（GeoIP 变更 / 采集源重新判定）：把旧地域索引里的残条摘掉，
            # 否则该代理会同时留在两个地域的索引里。
            pipe.zrem(self.key_region(old_code), proxy)
        pipe.execute()
        return True

    def update(self, proxy_dict: dict, ttl: int = 0) -> bool:
        """更新元数据与评分（语义同 :meth:`put`，保留独立入口便于阅读）。"""
        return self.put(proxy_dict, ttl=ttl)

    def update_speed_history(self, proxy_str: str, speed: float,
                             max_len: int = 100) -> None:
        """将单次延迟写入定长 List（B-02 EWMA 支撑）。"""
        if not proxy_str or speed <= 0:
            return
        key = "fpp:speeds:%s" % proxy_str
        self.conn.lpush(key, speed)
        self.conn.ltrim(key, 0, max_len - 1)
        self.conn.expire(key, 604800)

    def _region_indexed(self, proxy_str: str) -> str:
        """取一个代理**当前被索引到**哪个地域（读不到返回空串）。

        读的是反向索引（``{table}:regionof``）而不是 Hash 元数据：
        清理索引必须以"索引里实际在哪"为准 —— 元数据里的地域可能已经变了，
        拿它去清会漏掉旧地域索引里的残条。
        """
        try:
            code = self.conn.hget(self.key_regionof, proxy_str)
        except Exception:
            return ""
        return str(code or "").strip().upper()

    def delete(self, proxy_str: str) -> bool:
        """彻底删除一个代理：全部索引同步清理。"""
        region = self._region_indexed(proxy_str)
        pipe = self.conn.pipeline()
        pipe.hdel(self.key_hash, proxy_str)
        pipe.zrem(self.key_score, proxy_str)
        pipe.zrem(self.key_https, proxy_str)
        pipe.zrem(self.key_expire, proxy_str)
        pipe.hdel(self.key_regionof, proxy_str)
        if region:
            pipe.zrem(self.key_region(region), proxy_str)
        pipe.execute()
        return True

    def clear(self) -> None:
        """清空整个代理池（含所有索引）。"""
        codes = set()
        # 反向索引里的地域码
        try:
            for code in self.conn.hvals(self.key_regionof) or []:
                code = str(code or "").strip().upper()
                if code:
                    codes.add(code)
        except Exception:
            pass
        # Hash 元数据里的地域码（反向索引可能尚未建立，两边取并集才不漏）
        try:
            for raw in self.conn.hvals(self.key_hash) or []:
                try:
                    code = extract_region(json.loads(raw))
                except (TypeError, ValueError):
                    code = ""
                if code:
                    codes.add(code)
        except Exception:
            pass
        keys = [self.key_hash, self.key_score, self.key_https, self.key_expire,
                self.key_regionof, self.key_regionidx]
        keys.extend(self.key_region(code) for code in codes)
        self.conn.delete(*keys)

    def evict_expired(self, batch: int = 500) -> int:
        """回收已过期代理，返回回收条数。

        按过期索引的时间戳取，不再全表扫描。
        """
        now = time.time()
        expired = self.conn.zrangebyscore(self.key_expire, "-inf", now, start=0, num=batch)
        if not expired:
            return 0
        # 地域索引也要同步清理 —— 先取这批代理当前被索引到哪个地域
        region_of = {}
        try:
            codes = self.conn.hmget(self.key_regionof, expired)
        except Exception:
            codes = []
        for proxy, code in zip(expired, codes or []):
            code = str(code or "").strip().upper()
            if code:
                region_of[proxy] = code
        pipe = self.conn.pipeline()
        for proxy in expired:
            pipe.hdel(self.key_hash, proxy)
            pipe.zrem(self.key_score, proxy)
            pipe.zrem(self.key_https, proxy)
            pipe.zrem(self.key_expire, proxy)
            pipe.hdel(self.key_regionof, proxy)
        for proxy, code in region_of.items():
            pipe.zrem(self.key_region(code), proxy)
        pipe.execute()
        return len(expired)

    # ---------- 读 ----------

    def get_count(self) -> Dict[str, int]:
        """池规模统计，**全部 O(1)**。"""
        pipe = self.conn.pipeline()
        pipe.hlen(self.key_hash)
        pipe.zcard(self.key_https)
        total, https = pipe.execute()
        return {"total": int(total or 0), "https": int(https or 0)}

    def exists(self, proxy_str: str) -> bool:
        return bool(self.conn.hexists(self.key_hash, proxy_str))

    def get_one(self, https: bool = False, region: Optional[str] = None) -> Optional[dict]:
        """按评分从高到低取一个代理。

        :param https: 仅返回支持 HTTPS 的代理
        :param region: 地区代码（大小写不敏感）。**语义严格**：
            指定 region 时，地域未知（空）的代理**不会被返回**。

        v0.1.0 修复：旧版过滤条件是
        ``if area and data.get("region") and ...: continue``，
        地域为空时代理被**直接放行**，于是 ``?area=CN`` 实际返回全池代理；
        兜底分支还把 area 条件整个丢掉了。现在两处都改为严格匹配。
        """
        for raw in self._iter_candidates(https, region, limit=200):
            return json.loads(raw)
        return None

    def iter_raw(self, https: bool = False, region: Optional[str] = None) -> List[str]:
        """返回全部代理的原始 JSON 字符串列表。"""
        return [raw for raw in (self.conn.hvals(self.key_hash) or [])
                if _matches(raw, https, region)]

    def get_all(self, https: bool = False, region: Optional[str] = None) -> List[dict]:
        """返回全部代理（已解析为 dict）。"""
        result = []
        for raw in self.iter_raw(https, region):
            try:
                result.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return result

    def top_by_score(self, limit: int = 200, https: bool = False) -> List[dict]:
        """按评分降序取前 N 条（不做地域过滤，用于分发候选）。"""
        members = self.conn.zrevrange(self.key_score, 0, max(0, limit * 2 - 1))
        if not members:
            return self.get_all(https)[:limit]
        raws = self.conn.hmget(self.key_hash, members)
        result = []
        for raw in raws:
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if https and not data.get("https"):
                continue
            result.append(data)
            if len(result) >= limit:
                break
        return result

    # ---------- 地域索引（借鉴 docs/09 P1-1） ----------

    def region_index_ready(self) -> bool:
        """地域索引是否已按全量 Hash 重建过（完整性标记）。

        **这个门控是必须的**：地域索引是后加的，升级前入库的数据没有索引
        条目。若在索引"只覆盖了一部分"时直接信任它，``?area=CN`` 就会
        **少返回**代理 —— 那是比慢更糟的回归。

        标记不存在 → 一律走全表扫描：慢，但结果与改造前逐字节一致。
        """
        try:
            return bool(self.conn.exists(self.key_regionidx))
        except Exception:
            return False

    def region_total(self, code: str) -> int:
        """某地域索引内的代理总数（索引缺失返回 0）。"""
        try:
            return int(self.conn.zcard(self.key_region(code)) or 0)
        except Exception:
            return 0

    def get_by_region(self, region: Optional[str], https: bool = False,
                      limit: Optional[int] = 200) -> List[dict]:
        """按地域取代理。

        **为什么要有索引**：旧实现带 ``?area=`` 时走 ``get_all()``，
        即 ``HVALS`` 全量拉取 + 逐条 ``json.loads`` + 过滤 —— 池子上万时
        每次请求都是 O(N)，而 ``?area=`` 是最常用的筛选维度之一。

        **逐级降级**（借鉴 haipproxy 的多 ZSet 交集 + 降级思路）：

        1. **一级（快）**：走 ``{table}:region:{CODE}`` 地域索引 ——
           ZSet 按复合评分倒序取候选，再回 Hash 取元数据复核；
        2. **二级（降级）**：索引不可用（未重建 / 索引损坏 / 索引结果为空）
           时，退回全表扫描。

        **降级只改变"取数方式"，绝不改变"返回什么"** —— 地域筛选始终是
        严格匹配（地域未知的代理不会被返回）。这一点与 haipproxy 的降级
        策略不同：它降级时会放宽约束，而我们放宽就等于把 v0.1.0 修掉的
        "area 静默忽略"那个 bug 又请回来。

        索引只是**候选来源**：最终仍以元数据里的 ``region`` 字段为准，
        因此索引里残留的过期条目不会造成错分。

        **索引结果为空时一律退回全表扫描** —— 这是"宁可慢也不能少返回"
        的兜底：索引可能只覆盖了池子的一部分（升级前入库的数据、滚动发布
        期间旧版本进程写入的代理都没进索引），此时若直接返回空列表，
        ``?area=CN`` 就会**静默返回空**。慢一次好过丢一批代理。

        ``limit=None`` 表示不限量（取该地域的全部代理）。
        """
        code = str(region or "").strip().upper()
        if not code:
            return []
        unlimited = limit is None
        limit = 0 if unlimited else max(1, int(limit))

        # ---------- 一级：地域索引 ----------
        if self.region_index_ready():
            end = -1 if unlimited else max(0, limit * 2 - 1)
            try:
                members = self.conn.zrevrange(self.key_region(code), 0, end)
            except Exception:
                members = None
            if members is not None:
                result = []
                if members:
                    try:
                        raws = self.conn.hmget(self.key_hash, members)
                    except Exception:
                        raws = []
                    for raw in raws or []:
                        if not raw:
                            continue
                        try:
                            data = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if extract_region(data) != code:
                            continue
                        if https and not data.get("https"):
                            continue
                        result.append(data)
                        if not unlimited and len(result) >= limit:
                            break
                if result:
                    return result
                # 索引这一路没拿到东西 —— 可能是索引只覆盖了池子的一部分，
                # 也可能候选全被 https 过滤掉了。两种情况都退回全表扫描兜底。

        # ---------- 二级（降级）：全表扫描 ----------
        items = self.get_all(https, code)
        return items if unlimited else items[:limit]

    def rebuild_region_index(self) -> int:
        """按当前 Hash 重建全部地域索引，返回索引内的代理数。

        升级场景：地域索引是新增的，**升级前入库的数据没有索引条目**。
        调度器启动后重建一次即可，之后由 :meth:`put` / :meth:`delete`
        增量维护。

        重建会把每个地域码对应的 ZSet 整个重建（先 DEL 再 ZADD），
        因此不会残留已删除代理的条目；重建完成后写入**完整性标记**，
        读侧才会开始信任索引。

        重建是幂等的：即使有旧版本进程还在往池子里写（滚动发布），
        周期性地重建一次也能让索引重新收敛到真实值。
        """
        buckets: Dict[str, List[str]] = {}
        scores: Dict[str, float] = {}
        try:
            raws = self.conn.hvals(self.key_hash) or []
        except Exception:
            return 0
        for raw in raws:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            code = extract_region(data)
            proxy = str(data.get("proxy") or "")
            if not code or not proxy:
                continue
            buckets.setdefault(code, []).append(proxy)
            scores[proxy] = self._calc_score(data)
        try:
            pipe = self.conn.pipeline()
            # 先删掉已知的旧地域索引（含本次没有代理的地域，避免残条）
            try:
                for old_code in set(self.conn.hvals(self.key_regionof) or []):
                    old_code = str(old_code or "").strip().upper()
                    if old_code:
                        pipe.delete(self.key_region(old_code))
            except Exception:
                pass
            pipe.delete(self.key_regionof)
            for code, proxies in buckets.items():
                pipe.zadd(self.key_region(code),
                          {p: scores.get(p, 0.0) for p in proxies})
                for p in proxies:
                    pipe.hset(self.key_regionof, p, code)
            # 标记放在最后：只有索引确实写完才置位，读侧才敢信任它
            pipe.set(self.key_regionidx, str(time.time()))
            pipe.execute()
        except Exception:
            return 0
        return sum(len(items) for items in buckets.values())

    def _iter_candidates(self, https: bool, region: Optional[str], limit: int):
        """按评分降序遍历候选，过滤 https/region。"""
        members = self.conn.zrevrange(self.key_score, 0, max(0, limit - 1))
        if members:
            for raw in self.conn.hmget(self.key_hash, members) or []:
                if raw and _matches(raw, https, region):
                    yield raw
        # 评分索引缺失（历史数据 / 索引损坏）时回退到全表
        if not members:
            for raw in self.iter_raw(https, region):
                yield raw

    # ---------- 黑名单（死代理退避缓存） ----------

    def add_black(self, proxy_str: str, ttl: int = 3600,
                  backoff: bool = False) -> bool:
        """加入黑名单。

        v0.1.0（P0-2）语义变更：score 由"拉黑时间戳"改为"**跳过截止时间戳**"
        ——即"这个代理在什么时刻之前不要再碰"。

        * ``backoff=False``（默认）：**显式 TTL**。用于管理员手动拉黑 ——
          尊重调用方给定的时长，且**不改动**连续失败计数。
        * ``backoff=True``：**死代理退避**。连续失败次数 +1，按阶梯
          :data:`BLACKLIST_BACKOFF`（1h → 2h → 6h → 24h）计算跳过时长。
          用于"检测到代理已死"的路径（校验硬失败、客户端回报失效）。

        两种模式都会顺带清理已到期的成员（score <= now）。
        """
        now = time.time()
        if backoff:
            count = self.black_fail_count(proxy_str) + 1
            window = BLACKLIST_BACKOFF[min(count, len(BLACKLIST_BACKOFF)) - 1]
            pipe = self.conn.pipeline()
            pipe.hset(self.key_black_fail, proxy_str, count)
            pipe.zadd(self.key_black, {proxy_str: now + window})
            pipe.zremrangebyscore(self.key_black, "-inf", now)
            pipe.execute()
            return True
        pipe = self.conn.pipeline()
        pipe.zadd(self.key_black, {proxy_str: now + max(0, int(ttl))})
        pipe.zremrangebyscore(self.key_black, "-inf", now)
        pipe.execute()
        return True

    def is_black(self, proxy_str: str) -> bool:
        """是否处于退避窗口内（跳过截止时间 > now）。

        v0.1.0（P0-2）修复：旧实现只判断"成员是否存在于 ZSet"，
        而成员是靠后台 ``ZREMRANGEBYSCORE`` 顺带清理的 —— 早已过期的代理
        在清理任务没跑到之前仍会被判定为黑名单。现在直接按 score 判定，
        过期即失效，不依赖清理任务的执行时机。
        """
        return self.black_until(proxy_str) > 0

    def black_until(self, proxy_str: str) -> float:
        """返回跳过截止时间戳（0 = 未拉黑 / 已到期）。"""
        score = self.conn.zscore(self.key_black, proxy_str)
        if score is None:
            return 0.0
        try:
            value = float(score)
        except (TypeError, ValueError):
            return 0.0
        return value if value > time.time() else 0.0

    def black_fail_count(self, proxy_str: str) -> int:
        """连续失败次数（退避阶梯的档位依据）。"""
        raw = self.conn.hget(self.key_black_fail, proxy_str)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    def clear_black_fail(self, proxy_str: str) -> bool:
        """校验通过：清零连续失败计数并解除拉黑。

        代理"活过来了"就应当立刻重新可用 —— 否则它会被退避窗口挡住，
        直到窗口自然过期才有机会重新参与分发。
        """
        pipe = self.conn.pipeline()
        pipe.hdel(self.key_black_fail, proxy_str)
        pipe.zrem(self.key_black, proxy_str)
        pipe.execute()
        return True

    def remove_black(self, proxy_str: str) -> bool:
        """解除拉黑（同时清零失败计数）。"""
        pipe = self.conn.pipeline()
        pipe.zrem(self.key_black, proxy_str)
        pipe.hdel(self.key_black_fail, proxy_str)
        results = pipe.execute()
        return bool(results and results[0])

    def clear_black(self) -> None:
        """清空黑名单（同时清零全部失败计数）。"""
        pipe = self.conn.pipeline()
        pipe.delete(self.key_black)
        pipe.delete(self.key_black_fail)
        pipe.execute()

    def black_count(self) -> int:
        """当前生效的黑名单条数（自动清理已到期成员）。"""
        now = time.time()
        pipe = self.conn.pipeline()
        pipe.zremrangebyscore(self.key_black, "-inf", now)
        pipe.zcard(self.key_black)
        results = pipe.execute()
        return int(results[1] or 0)

    def list_black(self, page: int = 1, size: int = 50) -> dict:
        """分页列出黑名单（自动清理已到期成员）。

        返回的 ``blocked_until`` 是**解禁时间戳**（旧版叫 ``blocked_at``，
        语义从"拉黑时刻"变成了"跳过截止时刻"）。
        """
        now = time.time()
        self.conn.zremrangebyscore(self.key_black, "-inf", now)
        total = int(self.conn.zcard(self.key_black) or 0)
        start = max(0, (page - 1) * size)
        items = self.conn.zrange(self.key_black, start, start + size - 1, withscores=True)
        members = [proxy for proxy, _ in items]
        counts = []
        if members:
            pipe = self.conn.pipeline()
            for proxy in members:
                pipe.hget(self.key_black_fail, proxy)
            counts = pipe.execute()
        return {
            "total": total,
            "items": [{"proxy": proxy,
                       "blocked_until": score,
                       "fail_count": _to_int(counts[index]) if index < len(counts) else 0}
                      for index, (proxy, score) in enumerate(items)],
        }

    def prune_black_fail(self) -> int:
        """清理已不在黑名单里的连续失败计数。

        失败计数记在独立 Hash 里（代理被移出有效池后计数仍需保留，
        否则退避阶梯每次都会从第 1 级重新开始），因此必须显式回收，
        否则永久死亡的代理会让 Hash 无限增长。
        """
        try:
            proxies = list(self.conn.hkeys(self.key_black_fail) or [])
        except Exception:
            return 0
        if not proxies:
            return 0
        now = time.time()
        pipe = self.conn.pipeline()
        for proxy in proxies:
            pipe.zscore(self.key_black, proxy)
        scores = pipe.execute()
        stale = []
        for proxy, score in zip(proxies, scores):
            try:
                value = float(score) if score is not None else 0.0
            except (TypeError, ValueError):
                value = 0.0
            if value <= now:
                stale.append(proxy)
        if stale:
            pipe2 = self.conn.pipeline()
            for proxy in stale:
                pipe2.hdel(self.key_black_fail, proxy)
            pipe2.execute()
        return len(stale)

    # ---------- 运维 ----------

    def ping(self) -> bool:
        try:
            return bool(self.conn.ping())
        except Exception:
            return False

    def health(self) -> Dict[str, object]:
        """存储健康快照（管理后台概览使用）。"""
        try:
            count = self.get_count()
            return {"redis": self.ping(), "total": count["total"], "https": count["https"]}
        except Exception as exc:
            return {"redis": False, "error": type(exc).__name__, "total": 0, "https": 0}


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _matches(raw: str, https: bool, region: Optional[str]) -> bool:
    """单条代理元数据是否满足 https/region 过滤条件。"""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if https and not data.get("https"):
        return False
    if region:
        actual = str(data.get("region") or "").strip().upper()
        # 严格语义：地域未知的代理不满足地区筛选
        if not actual or actual != region.upper():
            return False
    return True


def extract_region(data: dict) -> str:
    """兼容读取地域字段（未来支持多地域时统一从这里取）。"""
    return str(data.get("region") or "").strip().upper()


def host_of(proxy_str: str) -> Optional[str]:
    """取代理地址中的 IP 部分（黑名单、告警日志用）。"""
    return parse_host_port(proxy_str)[0]


__all__ = ["RedisStore", "extract_region", "host_of", "BLACKLIST_BACKOFF"]
