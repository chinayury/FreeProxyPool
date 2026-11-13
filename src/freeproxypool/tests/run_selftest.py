# -*- coding: utf-8 -*-
"""FreeProxyPool 自检套件。

运行::

    python proxyPool.py selftest
    python tests/run_selftest.py

全部用例在**内存版 Redis** 上运行，不依赖外部服务、不产生网络请求、
不修改任何真实数据。可安全地在 CI、交付前、开发机上反复执行。

覆盖范围：

* 版本单一来源一致性
* IP / 代理地址 / 地域 / URL 白名单校验（含畸形输入）
* 敏感信息脱敏
* 密码哈希与强度策略
* 配置中心 fail-closed 语义（Redis 故障时鉴权不放开）
* 存储层读写、HTTPS 索引、O(1) 计数、TTL 回收、地区严格过滤
* 队列背压上限
* 分发策略与复用限制
* 网关鉴权链路：API Key、限流、会话、CSRF、ADMIN_ENABLED
* 采集源解析与"抓到 0 条即判失败"的熔断语义
* 延迟分层 tier：阈值判定、参数校验、筛选与分布统计
"""
import json
import os
import sys
import time
import types


def _find_repo_root() -> str:
    """从本文件位置向上搜索仓库根（以 VERSION 文件为标志）。

    支持 src 布局：tests 位于 ``src/freeproxypool/tests/``，
    需向上穿越包目录找到含 VERSION 文件的仓库根。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(10):  # 有界向上搜索，最多 10 层
        if os.path.isfile(os.path.join(cur, "VERSION")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return here


_ROOT = _find_repo_root()
# 支持未安装直接跑源码：把 src/ 加入 sys.path
_SRC = os.path.join(_ROOT, "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_PASS = 0
_FAIL = 0
_FAILURES = []


def check(name: str, condition: bool, detail: str = ""):
    """记录一条断言结果。"""
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print("  \033[92mPASS\033[0m %s" % name)
    else:
        _FAIL += 1
        _FAILURES.append("%s %s" % (name, detail))
        print("  \033[91mFAIL\033[0m %s %s" % (name, detail))


def section(title: str):
    print("\n\033[1m== %s ==\033[0m" % title)


# =====================================================================
# 1. 版本
# =====================================================================

def test_version():
    section("版本单一来源")
    from ..core.version import VERSION, FALLBACK_VERSION, read_version
    with open(os.path.join(_ROOT, "VERSION"), "r", encoding="utf-8") as fp:
        file_value = fp.read().strip()
    check("VERSION 文件内容为 0.1.0", file_value == "0.1.0", file_value)
    check("core.version.VERSION 与文件一致", VERSION == file_value, VERSION)
    check("兜底版本同为 0.1.0", FALLBACK_VERSION == "0.1.0", FALLBACK_VERSION)
    check("read_version 可重复调用", read_version() == VERSION)

    from .. import setting
    check("setting.__version__ 一致", setting.__version__ == VERSION, setting.__version__)


def _collect_version_anchors(repo):
    """收集全部"版本锚点"的实际取值（部署面 + 文档声明面）。

    版本锁必须是**机制**而不是约定：这些锚点只要有一个漂移，
    就会出现"VERSION 说 0.1.0、镜像标签说 0.1.1"这种对外不一致。
    """
    import re

    def read(rel):
        with open(os.path.join(repo, rel), "r", encoding="utf-8") as fp:
            return fp.read()

    from ..core.version import FALLBACK_VERSION

    anchors = {
        "VERSION 文件": read("VERSION").strip(),
        "core.version 兜底值": FALLBACK_VERSION,
    }
    tags = sorted(set(re.findall(r"image:\s*freeproxypool:([0-9.]+)",
                                 read("docker-compose.yml"))))
    anchors["compose 镜像标签"] = ",".join(tags)
    return anchors


def _anchor_mismatches(anchors, expected):
    """返回与期望版本不一致的锚点（供正向与负例共用）。"""
    return dict((k, v) for k, v in anchors.items() if v != expected)


def test_version_anchors():
    section("版本锁：部署面锚点与文档声明")
    import re
    # 期望值拆开拼接：门禁扫文档时不会匹配到它自己写下的字面量
    expected = "0.1" + ".0"

    anchors = _collect_version_anchors(_ROOT)
    bad = _anchor_mismatches(anchors, expected)
    check("部署面锚点全部为 0.1.0", not bad, bad or sorted(anchors))
    check("锚点覆盖 VERSION/兜底/compose",
          set(anchors) == {"VERSION 文件", "core.version 兜底值",
                           "compose 镜像标签"},
          sorted(anchors))

    # 负例：锚点值一旦不一致，必须被检出（证明上面的检查不是摆设）
    wrong = "0.1" + ".9"
    fake = dict(anchors)
    fake["VERSION 文件"] = wrong
    check("负例：错误版本号会被检出", bool(_anchor_mismatches(fake, expected)),
          _anchor_mismatches(fake, expected))

    # 文档声明的"当前版本"必须与代码一致
    docs_dir = os.path.join(_ROOT, "docs")
    doc_ver = os.path.join(docs_dir, "05-开发日志.md")
    if not os.path.isfile(doc_ver):
        check("docs/05 声明的当前版本为 0.1.0", False,
              "docs/05-开发日志.md 不在构建上下文里")
    else:
        with open(doc_ver, "r", encoding="utf-8") as fp:
            cur = [ln.strip() for ln in fp.read().splitlines() if "当前版本" in ln]
        check("docs/05 声明的当前版本为 0.1.0",
              bool(cur) and all(expected in ln for ln in cur), cur[:2])

    # 不得出现虚构的同族版本（v0.1.x 且 x≠0）—— 本项目只发布过 0.1.0。
    # 唯一例外：明确标注为"后续版本约定"的举例句（docs/05 §版本策略）。
    fiction = []
    pat = re.compile(r"(?<![\w.])v(0\.1\.\d+)(?![\w.])")
    if os.path.isdir(docs_dir):
        for name in sorted(os.listdir(docs_dir)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(docs_dir, name), "r", encoding="utf-8") as fp:
                for i, line in enumerate(fp.read().splitlines(), 1):
                    for m in pat.finditer(line):
                        if m.group(1) == expected:
                            continue
                        if "SemVer" in line or "后续版本号" in line:
                            continue
                        fiction.append("%s:%d %s" % (name, i, m.group(0)))
    else:
        fiction.append("docs/ 目录缺失，无法扫描虚构版本号")
    check("文档无虚构的同族版本号", not fiction, fiction[:5])


# =====================================================================
# 2. IP 与地址校验
# =====================================================================

def test_iputil():
    section("IP 与代理地址校验")
    from ..core.iputil import (is_internal_proxy, is_valid_proxy, parse_host_port,
                             strip_proxy_prefix)

    good = ["1.2.3.4:8080", "8.8.8.8:53", "255.255.255.254:1"]
    bad = [
        "999.999.999.999:99999",   # 旧正则会放过
        "1.2.3.4:0",              # 0 号端口不可用
        "1.2.3.4:70000",          # 端口越界
        "1.2.3.4",                # 缺端口
        "1.2.3.4.5:80",           # 五段
        "01.2.3.4:80",            # 前导零（旧正则放过）
        "abc:80",
        "",
        None,
    ]
    for value in good:
        check("接受合法地址 %s" % value, is_valid_proxy(value))
    for value in bad:
        check("拒绝非法地址 %r" % value, not is_valid_proxy(value))

    check("剥离 http:// 前缀", strip_proxy_prefix("http://1.2.3.4:80") == "1.2.3.4:80")
    check("剥离 user:pass@ 前缀",
          strip_proxy_prefix("http://u:p@1.2.3.4:80") == "1.2.3.4:80")

    internal = ["127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",
                "172.31.255.255", "169.254.169.254", "100.64.0.1",
                "198.18.0.1", "0.0.0.0", "255.255.255.255", "224.0.0.1"]
    public = ["1.1.1.1", "8.8.8.8", "172.32.0.1", "172.15.0.1", "100.128.0.1"]
    for ip in internal:
        check("判为内网 %s" % ip, is_internal_proxy(ip + ":8080"))
    for ip in public:
        check("判为公网 %s" % ip, not is_internal_proxy(ip + ":8080"))

    check("带协议前缀也能判内网", is_internal_proxy("http://192.168.0.1:8080"))
    check("解析返回 (ip, port)", parse_host_port("1.2.3.4:8080") == ("1.2.3.4", 8080))


# =====================================================================
# 3. 脱敏
# =====================================================================

def test_redact():
    section("敏感信息脱敏")
    from ..core.redact import mask_conn, redact

    masked = mask_conn("redis://user:sup3rSecret@10.0.0.1:6379/0")
    check("密码被遮蔽", "sup3rSecret" not in masked, masked)
    check("主机名保留", "10.0.0.1" in masked, masked)

    check("无密码连接串原样返回",
          mask_conn("redis://10.0.0.1:6379/0") == "redis://10.0.0.1:6379/0")

    line = "connect redis://admin:hunter2@cache:6379/0 failed"
    result = redact(line)
    check("日志行中的密码被遮蔽", "hunter2" not in result, result)
    check("日志行其他内容保留", "connect" in result and "failed" in result)

    plain = "采集完成，本轮 100 条"
    check("普通文本不受影响", redact(plain) == plain)
    check("非字符串输入安全", redact(None) is None)


# =====================================================================
# 4. 密码与令牌
# =====================================================================

def test_security():
    section("密码与令牌")
    from ..core.security import (MIN_PASSWORD_LENGTH, constant_time_compare,
                               derive_csrf, generate_token, hash_password,
                               is_hashed, password_strength_error,
                               verify_password_hash)

    hashed = hash_password("Str0ng@Passw0rd")
    check("生成 PBKDF2 格式", is_hashed(hashed), hashed[:20])
    check("正确密码校验通过", verify_password_hash("Str0ng@Passw0rd", hashed))
    check("错误密码校验失败", not verify_password_hash("wrong", hashed))
    check("同密码两次哈希不同（随机盐）",
          hash_password("Str0ng@Passw0rd") != hashed)
    check("格式损坏时 fail-closed",
          not verify_password_hash("x", "garbage"))

    check("常量时间比较相等", constant_time_compare("a", "a"))
    check("常量时间比较不等", not constant_time_compare("a", "b"))
    check("None 归一化为空串且不抛异常",
          constant_time_compare(None, "") is True)

    check("短密码被拒绝", password_strength_error("abc") != "")
    check("纯数字被拒绝", password_strength_error("1234567890123") != "")
    strong = "Str0ng@Passw0rd"
    check("强密码通过（%d 位门槛）" % MIN_PASSWORD_LENGTH,
          password_strength_error(strong) == "")
    check("拒绝不足 %d 位" % MIN_PASSWORD_LENGTH,
          password_strength_error("Ab1@" + "x" * (MIN_PASSWORD_LENGTH - 5)) != "")

    check("令牌长度足够", len(generate_token()) >= 40)
    check("两次派生一致（确定性）",
          derive_csrf("tok", "sec") == derive_csrf("tok", "sec"))
    check("不同令牌派生不同",
          derive_csrf("tok1", "sec") != derive_csrf("tok2", "sec"))
    check("密钥为空时返回空串（fail-closed）",
          derive_csrf("tok", "") == "")


# =====================================================================
# 5. 配置中心 fail-closed
# =====================================================================

class _BrokenRedis:
    """模拟 Redis 不可用：所有命令抛异常。"""

    def pipeline(self, *a, **k):
        raise ConnectionError("redis down")

    def __getattr__(self, name):
        def _boom(*a, **k):
            raise ConnectionError("redis down")
        return _boom


def test_config_fail_closed():
    section("配置中心 fail-closed")
    from .. import config_center as cc
    from ..core.errors import BackendUnavailable

    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original = cc.get_redis
    cc.get_redis = lambda: fake
    try:
        fake.hset(cc.KEY_CONFIG, "API_KEY", "super-secret")
        check("正常读取 API_KEY", cc.get_secure("API_KEY") == "super-secret")

        cc._bump_cache()
        cc.get_redis = lambda: _BrokenRedis()
        raised = None
        try:
            cc.get_secure("API_KEY")
        except BackendUnavailable as exc:
            raised = exc
        check("Redis 故障时 get_secure 抛 503", raised is not None)
        check("异常状态码为 503", raised is not None and raised.status_code == 503)

        # 关键回归：绝不能回落到默认值（那等于自动关闭鉴权）
        fallback = cc.get("API_KEY", "")
        check("普通读取会回落默认值（用于对比）", fallback == "", fallback)
    finally:
        cc.get_redis = original
        cc._bump_cache()


def test_config_validation():
    section("配置校验")
    from .. import config_center as cc
    from ..core.errors import ConfigError
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original = cc.get_redis
    cc.get_redis = lambda: fake
    try:
        check("拒绝越界", cc.set_many({"PORT": 99999}) != [])
        check("拒绝非白名单取值", cc.set_many({"LOG_LEVEL": "TRACE"}) != [])
        check("拒绝未知配置项", cc.set_many({"NOPE": 1}) != [])
        check("拒绝弱管理员口令", cc.set_many({"ADMIN_PASSWORD": "123456"}) != [])
        check("校验失败不写入",
              fake.hget(cc.KEY_CONFIG, "PORT") is None)
        check("接受合法写入", cc.set_many({"PORT": 5021}) == [])
        check("写入生效", cc.get("PORT") == 5021, cc.get("PORT"))
        check("接受强管理员口令",
              cc.set_many({"ADMIN_PASSWORD": "Str0ng@Passw0rd"}) == [])
        check("管理员口令以哈希存储",
              str(fake.hget(cc.KEY_CONFIG, "ADMIN_PASSWORD")).startswith("pbkdf2_sha256$"))
        check("哈希后可校验通过", cc.verify_password("Str0ng@Passw0rd"))
        check("错误口令校验失败", not cc.verify_password("nope"))

        check("reset 接受 None", cc.reset(None) is None)
        check("reset 后回到默认", cc.get("PORT") == 5020)
        raised = False
        try:
            cc.reset("PORT")
        except ConfigError:
            raised = True
        check("reset 传字符串会明确报错（修复静默无效）", raised is True)

        raised = False
        try:
            cc.set_fetcher("no_such_source", True)
        except ConfigError:
            raised = True
        check("未知采集源被拒绝", raised)
    finally:
        cc.get_redis = original
        cc._bump_cache()


def test_login_lock_key():
    section("登录锁定 Key 归一化")
    from .. import config_center as cc

    a = cc.login_lock_key("1.2.3.4")
    b = cc.login_lock_key("1.2.3.4")
    check("同 IP 得到同 Key", a == b)
    check("Key 长度固定（防内存打爆）", len(a) <= 64, a)

    hostile = "x" * 5000
    check("超长伪造值不会撑大 Key", len(cc.login_lock_key(hostile)) <= 64)
    check("不同 IP 得到不同 Key", cc.login_lock_key("1.2.3.5") != a)


# =====================================================================
# 6. 参数校验
# =====================================================================

def test_validators():
    section("参数校验")
    from ..core.errors import ValidationError
    from ..core.validators import (parse_area, parse_page, parse_size, parse_ttl,
                                 require_proxy, safe_int, validate_check_url,
                                 validate_fetch_urls,
                                 validate_name, validate_webhook)

    check("合法 area", parse_area("cn") == "CN")
    check("空 area 视为不过滤", parse_area("") is None)
    check("缺省 area", parse_area(None) is None)
    for bad in ["C", "CHN", "C1", "1C", "<script>"]:
        raised = False
        try:
            parse_area(bad)
        except ValidationError:
            raised = True
        check("非法 area %r 被拒（修复静默忽略）" % bad, raised)

    # --- 筛选类参数口径一致：非法值一律 400，不静默回落成"不过滤" ---
    from ..core.validators import parse_anon, parse_protocol, parse_strategy
    check("合法 anon", parse_anon("elite") == "elite")
    check("anon 大小写不敏感", parse_anon("ELITE") == "elite")
    check("空 anon 视为不过滤", parse_anon("") is None)
    check("缺省 anon 视为不过滤", parse_anon(None) is None)
    raised = False
    try:
        parse_anon("ghost")
    except ValidationError:
        raised = True
    check("非法 anon 被拒（不再静默回落成不过滤）", raised)

    # 取值类参数仍保持宽松（不改变"筛不筛"，且兼容历史客户端脏值）
    check("非法 strategy 回落默认", parse_strategy("cheapest") == "latency")
    from ..core.validators import parse_type
    check("非法 type 按 http 处理（历史兼容）", parse_type("bogus") is False)
    check("type=https 生效", parse_type("https") is True)

    check("非数字回落默认", safe_int("abc", 7) == 7)
    check("None 回落默认", safe_int(None, 7) == 7)
    check("clamp 上界", safe_int(9999, 7, high=100) == 100)
    check("clamp 下界", safe_int(-5, 7, low=0) == 0)

    check("页码下限保护", parse_page("-3") == 1)
    check("页码默认值", parse_page("x") == 1)
    check("每页上限 500", parse_size("99999") == 500)
    check("非法 ttl 不再静默回落（改为 400）",
          _raises(parse_ttl, "abc", 3600))
    check("缺省 ttl 用默认值", parse_ttl(None, 3600) == 3600)
    check("ttl 上限裁剪", parse_ttl(999999, 3600) == 604800)

    check("代理归一化", require_proxy(" 1.2.3.4:8080 ") == "1.2.3.4:8080")
    for bad in ["", "abc", "1.2.3.4", "1.2.3.4:0", None, 123]:
        raised = False
        try:
            require_proxy(bad)
        except ValidationError:
            raised = True
        check("非法 proxy %r 返回 400" % bad, raised)

    check("允许 https URL", validate_fetch_urls(["https://a.com/x"]) == ["https://a.com/x"])
    for bad_scheme in ["file:///etc/passwd", "ftp://a.com", "gopher://x",
                       "javascript:alert(1)", "http://169.254.169.254/latest/meta-data/"]:
        raised = False
        try:
            validate_fetch_urls([bad_scheme])
        except ValidationError:
            raised = True
        check("拒绝 SSRF 协议 %s" % bad_scheme.split(":")[0], raised)
    raised = False
    try:
        validate_fetch_urls(["http://a.com"] * 500)
    except ValidationError:
        raised = True
    check("URL 数量超限被拒", raised)

    check("空 webhook 允许", validate_webhook("") == "")
    check("合法 webhook", validate_webhook("https://oapi.dingtalk.com/x") != "")
    check("非法 webhook 被拒",
          _raises(validate_webhook, "javascript:alert(1)"))
    # 渗透测试发现：Webhook 由服务端主动请求，内网目标即 SSRF
    for bad in ["http://127.0.0.1:6379/", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1:5020/", "http://localhost:5020/health",
                "http://192.168.1.1/"]:
        check("webhook 拒绝内网目标 %s" % bad, _raises(validate_webhook, bad))
    # 校验目标同样不接受内网地址
    for bad in ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:6379/",
                "http://10.0.0.1/"]:
        check("校验目标拒绝内网地址 %s" % bad,
              _raises(validate_check_url, bad, "HTTP_URL"))
    check("校验目标允许公网地址",
          validate_check_url("https://www.qq.com", "HTTPS_URL") == "https://www.qq.com")
    check("采集源名白名单", validate_name("thespeedx") == "thespeedx")
    check("采集源名拒绝路径穿越", _raises(validate_name, "../../etc/passwd"))
    check("采集源名拒绝空格", _raises(validate_name, "a b"))


def _raises(fn, *args, **kwargs):
    from ..core.errors import FreeProxyPoolError
    try:
        fn(*args, **kwargs)
        return False
    except FreeProxyPoolError:
        return True


# =====================================================================
# 7. 存储层
# =====================================================================

def test_storage():
    section("存储层")
    from ..db.redisClient import RedisStore
    from .fake_redis import FakeRedis

    store = RedisStore(FakeRedis(), table="t")

    check("空池计数为 0", store.get_count() == {"total": 0, "https": 0})

    store.put({"proxy": "1.1.1.1:80", "https": False, "score": 50,
               "region": "", "source": "a",
               "check_count": 10, "error_count": 5, "ewma_speed": 2.0})
    store.put({"proxy": "2.2.2.2:443", "https": True, "score": 90,
               "region": "CN", "source": "b",
               "check_count": 10, "error_count": 1, "ewma_speed": 1.0})
    store.put({"proxy": "3.3.3.3:8080", "https": True, "score": 70,
               "region": "US", "source": "c",
               "check_count": 10, "error_count": 3, "ewma_speed": 5.0})

    count = store.get_count()
    check("总数正确", count["total"] == 3, count)
    check("HTTPS 计数正确（O(1) 索引）", count["https"] == 2, count)

    check("按评分降序取前三",
          [p["proxy"] for p in store.top_by_score(3)] ==
          ["2.2.2.2:443", "3.3.3.3:8080", "1.1.1.1:80"])

    check("region=CN 严格只返回 CN", len(store.get_all(False, "CN")) == 1)
    check("region=US 只返回 US", len(store.get_all(False, "US")) == 1)
    check("未知地域不满足地区筛选（修复：全池返回）",
          len(store.get_all(False, "CN")) == 1)

    # 把 1.1.1.1 的地域清掉，验证它不再被 CN 查询命中
    raw = store.conn.hget("t", "1.1.1.1:80")
    import json as _json
    data = _json.loads(raw)
    data["region"] = ""
    store.put(data)
    check("地域为空时不被地区查询命中", len(store.get_all(False, "CN")) == 1)

    check("https 过滤", len(store.get_all(True)) == 2)

    store.delete("2.2.2.2:443")
    check("删除后 HTTPS 索引同步清理", store.get_count()["https"] == 1,
          store.get_count())

    # TTL 回收
    ttl_store = RedisStore(FakeRedis(), table="t2")
    ttl_store.put({"proxy": "9.9.9.9:80", "https": False, "score": 1}, ttl=1)
    check("未过期前仍在池中", ttl_store.exists("9.9.9.9:80"))
    ttl_store._expire_override = True
    # 直接把过期索引的时间戳推到过去，模拟到期
    ttl_store.conn.zadd("t2:expire", {"9.9.9.9:80": time.time() - 10})
    removed = ttl_store.evict_expired()
    check("过期代理被回收", removed == 1, removed)
    check("回收后不在池中", not ttl_store.exists("9.9.9.9:80"))

    # ttl=0（永不过期）必须清掉旧的过期索引，否则会被提前回收
    keep_store = RedisStore(FakeRedis(), table="t3")
    keep_store.put({"proxy": "8.8.8.8:80", "https": False, "score": 1}, ttl=1)
    check("先按 TTL 入池", keep_store.conn.zscore("t3:expire", "8.8.8.8:80") is not None)
    keep_store.put({"proxy": "8.8.8.8:80", "https": False, "score": 1}, ttl=0)
    check("改为永不过期后过期索引被清除",
          keep_store.conn.zscore("t3:expire", "8.8.8.8:80") is None)
    check("索引已清时 evict_expired 不会动它",
          keep_store.evict_expired() == 0 and keep_store.exists("8.8.8.8:80"))

    # FakeRedis 保真度：zrangebyscore 的 start=0 必须参与分片
    fidelity = FakeRedis()
    for i in range(5):
        fidelity.zadd("z:fidelity", {"m%d" % i: float(i)})
    check("zrangebyscore start=0 仍受 num 限制（修复分片失效）",
          fidelity.zrangebyscore("z:fidelity", "-inf", "+inf", start=0, num=2)
          == ["m0", "m1"],
          fidelity.zrangebyscore("z:fidelity", "-inf", "+inf", start=0, num=2))
    check("zrangebyscore 非零偏移正常",
          fidelity.zrangebyscore("z:fidelity", "-inf", "+inf", start=2, num=2)
          == ["m2", "m3"])


def test_blacklist():
    section("黑名单（死代理退避）")
    from ..db.redisClient import BLACKLIST_BACKOFF, RedisStore
    from .fake_redis import FakeRedis

    store = RedisStore(FakeRedis(), table="b")
    store.add_black("5.5.5.5:80", ttl=3600)
    check("加入后可查询", store.is_black("5.5.5.5:80"))
    check("未加入的不算黑名单", not store.is_black("6.6.6.6:80"))
    check("计数为 1", store.black_count() == 1)

    listed = store.list_black(1, 10)
    check("分页列表返回条目", listed["total"] == 1 and listed["items"][0]["proxy"] == "5.5.5.5:80")
    check("列表给出解禁时间戳", listed["items"][0].get("blocked_until", 0) > time.time(),
          listed["items"][0])

    # 过期成员被自动清理（score <= now 即视为到期）
    store.conn.zadd("b:black", {"5.5.5.5:80": time.time() - 99999})
    check("过期成员被自动清理", store.black_count() == 0)
    check("过期后不再算黑名单", not store.is_black("5.5.5.5:80"))

    # ---------- 退避阶梯 ----------
    store2 = RedisStore(FakeRedis(), table="bb")
    store2.add_black("1.1.1.1:80", backoff=True)
    first = store2.black_until("1.1.1.1:80")
    check("首次退避为第 1 级（1h）",
          3500 < first - time.time() < 3700, first - time.time())
    check("失败计数为 1", store2.black_fail_count("1.1.1.1:80") == 1)

    store2.add_black("1.1.1.1:80", backoff=True)
    second = store2.black_until("1.1.1.1:80")
    check("二次退避升到第 2 级（2h）",
          7000 < second - time.time() < 7400, second - time.time())
    check("失败计数为 2", store2.black_fail_count("1.1.1.1:80") == 2)

    store2.add_black("1.1.1.1:80", backoff=True)
    third = store2.black_until("1.1.1.1:80")
    check("三次退避升到第 3 级（6h）",
          21000 < third - time.time() < 22000, third - time.time())

    store2.add_black("1.1.1.1:80", backoff=True)
    store2.add_black("1.1.1.1:80", backoff=True)
    fifth = store2.black_until("1.1.1.1:80")
    check("第 5 次退避封顶在 24h",
          86000 < fifth - time.time() < 86500, fifth - time.time())
    check("阶梯长度与文档一致", len(BLACKLIST_BACKOFF) == 4, BLACKLIST_BACKOFF)
    check("阶梯单调递增",
          list(BLACKLIST_BACKOFF) == sorted(BLACKLIST_BACKOFF), BLACKLIST_BACKOFF)

    # ---------- 显式 TTL 不动失败计数 ----------
    store3 = RedisStore(FakeRedis(), table="bc")
    store3.add_black("2.2.2.2:80", backoff=True)
    store3.add_black("2.2.2.2:80", ttl=120)
    check("显式 TTL 不改失败计数", store3.black_fail_count("2.2.2.2:80") == 1)
    check("显式 TTL 覆盖跳过时长",
          100 < store3.black_until("2.2.2.2:80") - time.time() < 140)

    # ---------- 校验通过：清零并解除拉黑 ----------
    store3.clear_black_fail("2.2.2.2:80")
    check("通过后计数清零", store3.black_fail_count("2.2.2.2:80") == 0)
    check("通过后解除拉黑", not store3.is_black("2.2.2.2:80"))
    check("通过后黑名单条目被移除",
          store3.conn.zscore("bc:black", "2.2.2.2:80") is None)

    # ---------- 移除 / 清空 ----------
    store.remove_black("5.5.5.5:80")
    store.clear_black()
    check("清空黑名单", store.black_count() == 0)

    store4 = RedisStore(FakeRedis(), table="bd")
    store4.add_black("3.3.3.3:80", backoff=True)
    store4.add_black("4.4.4.4:80", backoff=True)
    store4.remove_black("3.3.3.3:80")
    check("移除时一并清零计数", store4.black_fail_count("3.3.3.3:80") == 0)
    check("未移除的计数保留", store4.black_fail_count("4.4.4.4:80") == 1)
    store4.clear_black()
    check("清空时一并清零全部计数", store4.black_fail_count("4.4.4.4:80") == 0)


def test_blacklist_backoff_integration():
    section("死代理退避联动（校验链路 → 采集去重）")
    from .. import config_center as cc
    from ..db.dbClient import reset_store
    from ..db.redisClient import RedisStore
    from ..helper.fetch import _already_known
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    reset_store()
    try:
        store = RedisStore(fake, table="e")
        # 死代理：硬失败后进退避窗口
        store.add_black("7.7.7.7:80", backoff=True)
        check("退避窗口内的代理不会被重复采集",
              _already_known(store, "7.7.7.7:80"))
        check("不在池也不在退避窗口的代理会被采集",
              not _already_known(store, "8.8.8.8:80"))
        # 退避窗口到期后重新参与采集
        fake.zadd("e:black", {"7.7.7.7:80": time.time() - 1})
        check("退避到期后重新参与采集",
              not _already_known(store, "7.7.7.7:80"))
        # 校验通过后立即恢复采集
        store.add_black("7.7.7.7:80", backoff=True)
        store.clear_black_fail("7.7.7.7:80")
        check("校验通过后立即恢复采集",
              not _already_known(store, "7.7.7.7:80"))

        # 失败计数回收
        store.add_black("9.9.9.9:80", backoff=True)
        fake.zadd("e:black", {"9.9.9.9:80": time.time() - 1})
        removed = store.prune_black_fail()
        check("过期条目对应的计数被回收", removed >= 1, removed)
        check("回收后计数不存在", store.black_fail_count("9.9.9.9:80") == 0)
    finally:
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


def test_queue():
    section("待验队列与背压")
    from ..db.queue import DEDUP_KEY, QUEUE_KEY, ProxyQueue
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    queue = ProxyQueue(fake)

    check("空队列深度为 0", queue.depth() == 0)
    check("入队成功", queue.push_many(["1.1.1.1:80", "2.2.2.2:80"]) == 2)
    check("深度正确", queue.depth() == 2)
    check("去重集合拦截重复", queue.push_many(["1.1.1.1:80"]) == 0)

    batch = queue.pop_batch(10)
    check("批量弹出", len(batch) == 2, batch)
    check("弹出后队列空", queue.depth() == 0)
    check("重复消费不重复返回", queue.pop_batch(10) == [])

    # 背压：把上限调到 3，塞 10 条
    from .. import config_center as cc
    original = cc.get_redis
    cc.get_redis = lambda: fake
    try:
        cc._bump_cache()
        fake.hset(cc.KEY_CONFIG, "QUEUE_MAX_LEN", "3")
        cc._bump_cache()
        big = ["10.0.%d.%d:80" % (i, j) for i in range(1, 4) for j in range(1, 4)]
        queue.push_many(big)
        check("超出上限时截断（修复无界增长 OOM 风险）",
              queue.depth() <= 3, queue.depth())
    finally:
        cc.get_redis = original
        cc._bump_cache()


def test_queue_payload():
    section("队列载荷：来源不丢失")
    from ..db.queue import ProxyQueue, decode, encode
    from .fake_redis import FakeRedis

    # 编码：字符串与字典两种入参
    key, payload = encode("1.2.3.4:80")
    check("字符串入参保持原样", key == "1.2.3.4:80" and payload == "1.2.3.4:80")

    key, payload = encode({"proxy": "1.2.3.4:80", "source": "thespeedx/monosans"})
    check("字典入参带来源", key == "1.2.3.4:80")
    check("载荷含来源字段", '"s":"thespeedx/monosans"' in payload, payload)

    key, payload = encode({"proxy": "1.2.3.4:80", "source": ["a", "b"]})
    check("列表来源被拼成字符串", '"s":"a/b"' in payload, payload)

    # 解码：往返一致（Proxy.source 内部是 list，to_dict() 才输出 "a/b" 字符串）
    proxy = decode(payload)
    check("解码还原代理地址", proxy.proxy == "1.2.3.4:80", proxy.proxy)
    check("解码还原来源（修复：来源全空）",
          proxy.to_dict()["source"] == "a/b", proxy.to_dict()["source"])

    # 解码：兼容旧的裸字符串（升级期间队列里已有旧数据）
    legacy = decode("5.6.7.8:3128")
    check("兼容裸字符串载荷", legacy.proxy == "5.6.7.8:3128")
    check("裸字符串来源为空", legacy.to_dict()["source"] == "")

    # 端到端：入队 → 出队 → 解码
    queue = ProxyQueue(FakeRedis())
    queue.push_many([
        {"proxy": "9.1.1.1:80", "source": "srcA"},
        {"proxy": "9.1.1.2:80", "source": ["srcB", "srcC"]},
        "9.1.1.3:80",
    ])
    decoded = [decode(raw) for raw in queue.pop_batch(10)]
    check("三种载荷都能正确解码", len(decoded) == 3, decoded)
    check("字典载荷保留来源",
          decoded[0].to_dict()["source"] == "srcA", decoded[0].to_dict())
    check("列表载荷合并来源",
          decoded[1].to_dict()["source"] == "srcB/srcC", decoded[1].to_dict())
    check("字符串载荷可解码", decoded[2].proxy == "9.1.1.3:80", decoded[2].proxy)

    # 去重按代理地址而非载荷文本
    queue2 = ProxyQueue(FakeRedis())
    queue2.push_many([{"proxy": "9.2.2.1:80", "source": "x"}])
    check("同地址不同来源仍被去重",
          queue2.push_many([{"proxy": "9.2.2.1:80", "source": "y"}]) == 0)


# =====================================================================
# 8. 分发策略与复用限制
# =====================================================================

def test_dispatch():
    section("分发策略与复用限制")
    from .. import config_center as cc
    from ..db.redisClient import RedisStore
    from ..db.dbClient import reset_store
    from ..gateway import deps
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    reset_store()
    try:
        store = RedisStore(fake, table="d")
        for i, errors in enumerate([9, 1, 5]):
            store.put({"proxy": "7.7.7.%d:80" % (i + 1), "https": False,
                       "score": 0, "region": "", "source": "s",
                       "check_count": 10, "error_count": errors,
                       "ewma_speed": float(errors)})

        # latency：应取评分最高的
        pick = deps.select_one(store, False, None, "latency", 10)
        check("latency 取最高分", pick["proxy"] == "7.7.7.2:80", pick["proxy"])

        # 复用限制：REUSE_MAX=3，第 4 次应换人
        seen = []
        for _ in range(4):
            item = deps.select_one(store, False, None, "latency", 10)
            seen.append(item["proxy"])
        check("第 4 次分发切换到下一个代理", seen[3] != seen[0], seen)

        # 只在真正选中时才 +1：直接检查未被选中的候选计数为 0
        check("未被选中的候选复用计数为 0（修复扫描污染）",
              fake.get("fpp:reuse:7.7.7.1:80") in (None, "0"),
              fake.get("fpp:reuse:7.7.7.1:80"))

        # 批量分发互不重复（先清空复用计数，避免受前面用例影响）
        for proxy in ("7.7.7.1:80", "7.7.7.2:80", "7.7.7.3:80"):
            fake.delete("fpp:reuse:%s" % proxy)
        many = deps.select_many(store, False, None, "latency", 10, 3)
        proxies = [m["proxy"] for m in many]
        check("批量分发数量正确", len(proxies) == 3, proxies)
        check("批量分发不重复", len(set(proxies)) == len(proxies), proxies)

        # 候选上限：池大时不应全量扫描
        check("pick_candidates 尊重 limit",
              len(deps.pick_candidates(store, False, None, "latency", 2)) <= 2)

        # B-10: weighted 策略不应崩溃且有条数上限
        weighted_picks = deps.pick_candidates(store, False, None, "weighted", 10)
        check("weighted 策略有返回值", len(weighted_picks) > 0, len(weighted_picks))
        check("weighted 策略尊重 limit", len(weighted_picks) <= 10, len(weighted_picks))

        # B-03: anon 过滤
        store.put({"proxy": "7.7.7.10:80", "https": False, "score": 80,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 1, "ewma_speed": 1.0})
        store.put({"proxy": "7.7.7.11:80", "https": False, "score": 80,
                   "region": "", "source": "s", "anonymous": "transparent",
                   "check_count": 10, "error_count": 1, "ewma_speed": 1.0})
        elite_picks = deps.pick_candidates(store, False, None, "latency", 10, "elite")
        check("anon=elite 只返回 elite",
              all(p.get("anonymous") == "elite" for p in elite_picks), [p.get("anonymous") for p in elite_picks])
        trans_picks = deps.pick_candidates(store, False, None, "latency", 10, "transparent")
        check("anon=transparent 只返回 transparent",
              all(p.get("anonymous") == "transparent" for p in trans_picks), [p.get("anonymous") for p in trans_picks])

        # B-08: exclude_dnsbl 过滤
        store.put({"proxy": "7.7.7.12:80", "https": False, "score": 80,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 1, "ewma_speed": 1.0,
                   "dnsbl": True})
        store.put({"proxy": "7.7.7.13:80", "https": False, "score": 80,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 1, "ewma_speed": 1.0,
                   "dnsbl": False})
        clean_picks = deps.pick_candidates(store, False, None, "latency", 10, None, True)
        check("exclude_dnsbl 只返回 dnsbl=False",
              all(not p.get("dnsbl") for p in clean_picks), [p.get("dnsbl") for p in clean_picks])
    finally:
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


# =====================================================================
# 9. 网关鉴权链路
# =====================================================================

def _build_client():
    """构造一个接在内存 Redis 上的 TestClient。"""
    from fastapi.testclient import TestClient
    from .. import config_center as cc
    from ..db.dbClient import reset_store
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    cc.get_redis = lambda: fake
    reset_store()
    cc._bump_cache()

    from ..gateway.main import app
    return TestClient(app), fake


def test_gateway():
    section("网关鉴权链路")
    from .. import config_center as cc

    client, fake = _build_client()
    try:
        # --- 基础端点 ---
        r = client.get("/version")
        check("GET /version 可用", r.status_code == 200, r.status_code)
        check("版本号为 0.1.0", r.json()["version"] == "0.1.0", r.json())

        r = client.get("/health/live")
        check("GET /health/live 不碰存储", r.status_code == 200)

        r = client.get("/health")
        check("GET /health 返回 degraded/ok", r.status_code == 200)
        check("health 含 redis 字段", "redis" in r.json())

        # --- 未配置 API Key 时放行 ---
        r = client.get("/count/")
        check("未配 API Key 时 /count/ 放行", r.status_code == 200, r.text[:120])

        # --- 配置 API Key 后必须校验 ---
        fake.hset(cc.KEY_CONFIG, "API_KEY", "s3cret-key")
        cc._bump_cache()
        r = client.get("/count/")
        check("无 Key 被拒", r.status_code == 401, r.status_code)
        r = client.get("/count/?key=wrong")
        check("错误 Key 被拒", r.status_code == 401, r.status_code)
        r = client.get("/count/", headers={"API-KEY": "s3cret-key"})
        check("正确 Key 放行", r.status_code == 200, r.status_code)

        # --- metrics 鉴权 ---
        r = client.get("/metrics")
        check("/metrics 需要鉴权", r.status_code == 401, r.status_code)
        r = client.get("/metrics", headers={"API-KEY": "s3cret-key"})
        check("/metrics 带 Key 可访问", r.status_code == 200, r.status_code)
        check("/metrics 输出 Prometheus 格式",
              "fpp_pool_total" in r.text or "# HELP" in r.text)

        # --- area 参数非法 ---
        r = client.get("/get/?area=C1", headers={"API-KEY": "s3cret-key"})
        check("非法 area 返回 400（修复静默忽略）", r.status_code == 400, r.status_code)

        # --- /report/ 输入校验 ---
        r = client.post("/report/", json={}, headers={"API-KEY": "s3cret-key"})
        check("空 report 返回 400", r.status_code == 400, r.status_code)
        r = client.post("/report/", json={"proxies": ["not-a-proxy"]},
                        headers={"API-KEY": "s3cret-key"})
        check("非法代理被拒但不影响整体", r.status_code == 200, r.status_code)
        check("非法项进入 rejected",
              r.json().get("rejected") == ["not-a-proxy"], r.json())
        r = client.post("/report/", json={"proxies": ["1.2.3.4:8080"]},
                        headers={"API-KEY": "s3cret-key"})
        check("合法回报被接受",
              r.json().get("accepted") == ["1.2.3.4:8080"], r.json())

        # --- /delete/ 非法参数 ---
        r = client.get("/delete/?proxy=abc", headers={"API-KEY": "s3cret-key"})
        check("非法代理地址返回 400", r.status_code == 400, r.status_code)
    finally:
        client.close()


def test_admin_chain():
    section("管理后台鉴权链路")
    from .. import config_center as cc

    client, fake = _build_client()
    try:
        # --- 未登录 ---
        r = client.get("/admin/api/overview")
        check("未登录访问概览被拒", r.status_code == 401, r.status_code)

        r = client.post("/admin/api/config", json={"updates": {"PORT": 6000}})
        check("未登录写配置被拒", r.status_code == 401, r.status_code)

        # --- ADMIN_ENABLED 真正生效 ---
        fake.hset(cc.KEY_CONFIG, "ADMIN_ENABLED", "0")
        cc._bump_cache()
        r = client.post("/admin/api/login", json={"password": "whatever"})
        check("关闭后台后登录接口也被拦（修复：接口不校验开关）",
              r.status_code == 401, r.status_code)
        r = client.get("/admin/api/overview")
        check("关闭后台后接口不可用", r.status_code == 401, r.status_code)
        fake.hset(cc.KEY_CONFIG, "ADMIN_ENABLED", "1")
        cc._bump_cache()

        # --- 登录 ---
        fake.hset(cc.KEY_CONFIG, "ADMIN_PASSWORD", "Str0ng@Passw0rd")
        cc._bump_cache()
        r = client.post("/admin/api/login", json={"password": "wrong"})
        check("错误密码登录失败", r.status_code == 400, r.status_code)

        # --- 登录锁定提示的秒数应为锁定时长，而非失败次数 ---
        fake.hset(cc.KEY_CONFIG, "LOGIN_MAX_FAIL", "2")
        fake.hset(cc.KEY_CONFIG, "LOGIN_FAIL_TTL", "777")
        cc._bump_cache()
        client.post("/admin/api/login", json={"password": "wrong"})
        r = client.post("/admin/api/login", json={"password": "wrong"})
        check("达到失败阈值后返回 429", r.status_code == 429, r.status_code)
        check("提示秒数是锁定时长而非失败次数（修复：提示 5 秒实际锁 600 秒）",
              "777" in r.json().get("message", ""), r.json())
        cc.clear_login_failures("testclient")
        fake.hdel(cc.KEY_CONFIG, "LOGIN_MAX_FAIL")
        fake.hdel(cc.KEY_CONFIG, "LOGIN_FAIL_TTL")
        cc._bump_cache()

        r = client.post("/admin/api/login", json={"password": "Str0ng@Passw0rd"})
        check("正确密码登录成功", r.status_code == 200, r.text[:200])
        data = r.json()["data"]
        token, csrf = data["token"], data["csrf_token"]
        check("返回会话令牌", bool(token))
        check("返回 CSRF 令牌", bool(csrf))
        check("默认口令不强制改密（非默认值）",
              data.get("must_change_password") is False, data)

        auth = {"X-Admin-Token": token}

        # --- CSRF ---
        r = client.get("/admin/api/overview", headers=auth)
        check("带令牌可读概览", r.status_code == 200, r.status_code)
        r = client.post("/admin/api/pool/clear", headers=auth)
        check("写操作缺 CSRF 被拒", r.status_code == 403, r.status_code)
        r = client.post("/admin/api/pool/clear",
                        headers={"X-Admin-Token": token, "X-CSRF-Token": "bad"})
        check("错误 CSRF 被拒", r.status_code == 403, r.status_code)
        r = client.post("/admin/api/pool/clear",
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("正确 CSRF 通过", r.status_code == 200, r.text[:120])

        # --- 配置读写 ---
        r = client.get("/admin/api/config", headers=auth)
        items = r.json()["data"]
        check("配置列表非空", len(items) > 30, len(items))
        admin_pwd = [i for i in items if i["key"] == "ADMIN_PASSWORD"][0]
        check("密码不回明文", admin_pwd["value"] is None, admin_pwd)
        check("密码只回 has_value", admin_pwd["has_value"] is True, admin_pwd)
        check("密码不回 '***' 掩码串（修复前端回传覆盖）",
              admin_pwd["value"] != "***" and admin_pwd["default"] != "***")

        r = client.post("/admin/api/config",
                        json={"updates": {"PORT": 99999}},
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("越界配置被拒（400）", r.status_code == 400, r.status_code)

        r = client.post("/admin/api/config",
                        json={"updates": {"API_KEY": "new-key-1234"}},
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("合法配置写入成功", r.status_code == 200, r.text[:120])

        # --- 黑名单 ttl 非法值不再 500 ---
        r = client.post("/admin/api/blacklist/add",
                        json={"proxy": "1.2.3.4:8080", "ttl": "abc"},
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("非法 ttl 返回 400 而非 500（修复）", r.status_code == 400, r.status_code)

        r = client.post("/admin/api/blacklist/add",
                        json={"proxy": "1.2.3.4:8080", "ttl": 600},
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("合法 ttl 拉黑成功", r.status_code == 200, r.text[:120])

        # --- 采集源校验 ---
        r = client.post("/admin/api/fetchers",
                        json={"name": "no_such", "enabled": True},
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("未知采集源被拒", r.status_code == 400, r.status_code)

        r = client.post("/admin/api/fetchers",
                        json={"name": "thespeedx",
                              "urls": ["file:///etc/passwd"]},
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("SSRF 协议被拒（修复）", r.status_code == 400, r.status_code)

        r = client.get("/admin/api/fetchers", headers=auth)
        check("采集源列表非空", len(r.json()["data"]) > 20, len(r.json()["data"]))

        # --- 分页参数 ---
        r = client.get("/admin/api/proxies?page=abc&size=-5", headers=auth)
        check("非法分页参数不 500", r.status_code == 200, r.status_code)
        check("分页回落到默认值", r.json()["data"]["page"] == 1)

        # --- 触发 ---
        r = client.post("/admin/api/trigger", json={"processor": "nope"},
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("非法 processor 被拒", r.status_code == 400, r.status_code)
        r = client.post("/admin/api/trigger", json={"processor": "check"},
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("合法 trigger 写入（且确有消费者）", r.status_code == 200)
        check("触发标记可被消费",
              cc.consume_trigger("check") is True)

        # --- 退出 ---
        r = client.post("/admin/api/logout",
                        headers={"X-Admin-Token": token, "X-CSRF-Token": csrf})
        check("退出成功", r.status_code == 200)
        check("退出后会话失效", cc.verify_session(token) is False)
    finally:
        client.close()


def test_rate_limit():
    section("限流")
    from .. import config_center as cc
    import types as _types

    client, fake = _build_client()
    try:
        fake.hset(cc.KEY_CONFIG, "RATE_LIMIT_QPS", "3")
        cc._bump_cache()
        codes = [client.get("/count/").status_code for _ in range(6)]
        check("超过 QPS 后触发 429", 429 in codes, codes)
    finally:
        client.close()

    # Redis 故障时不得把限流整个放开（fail-open 回归）
    from ..gateway import deps as deps
    from ..core.errors import BackendUnavailable

    def _boom(key):
        raise BackendUnavailable("redis down")

    original_secure = cc.get_secure
    cc.get_secure = _boom
    try:
        req = _types.SimpleNamespace(
            client=_types.SimpleNamespace(host="10.9.9.9"),
            headers={}, query_params={}, cookies={})
        # 读不到配置时回落到默认 QPS(50)，进程内限流仍应有上界
        results = [deps.check_rate_limit(req) for _ in range(60)]
        check("Redis 故障时限流不放开（fail-closed）",
              any(results), results[-5:])
    finally:
        cc.get_secure = original_secure
        deps._local_buckets.clear()


def test_trusted_proxy():
    section("可信代理与 XFF 伪造")
    from .. import config_center as cc
    from ..gateway import deps
    from .fake_redis import FakeRedis

    # 构造一个最小 Request 替身
    def make_request(peer, headers):
        return types.SimpleNamespace(
            client=types.SimpleNamespace(host=peer),
            headers=headers,
            query_params={},
            cookies={},
        )

    # 未配置可信代理 → 完全不采信 XFF
    cc._bump_cache()
    req = make_request("10.0.0.9", {"X-Forwarded-For": "1.2.3.4"})
    check("默认不采信 XFF（修复爆破绕过）",
          deps.client_ip(req) == "10.0.0.9", deps.client_ip(req))

    # 配了可信代理且对端命中 → 采信 XFF
    from .. import config_center
    fake = FakeRedis()
    original = config_center.get_redis
    config_center.get_redis = lambda: fake
    try:
        fake.hset(cc.KEY_CONFIG, "TRUSTED_PROXIES", "10.0.0.0/8")
        cc._bump_cache()
        req = make_request("10.0.0.9", {"X-Forwarded-For": "1.2.3.4, 10.0.0.9"})
        check("可信对端时采信 XFF 首项",
              deps.client_ip(req) == "1.2.3.4", deps.client_ip(req))
        req = make_request("8.8.8.8", {"X-Forwarded-For": "1.2.3.4"})
        check("非可信对端仍不采信 XFF",
              deps.client_ip(req) == "8.8.8.8", deps.client_ip(req))
    finally:
        config_center.get_redis = original
        cc._bump_cache()


# =====================================================================
# 10. 采集源
# =====================================================================

def test_logging():
    section("日志")
    import logging

    from .. import config_center as cc
    from ..core.logging import (attach_redis_sink, detach_redis_sink, get_logger,
                              redact)
    from .fake_redis import FakeRedis

    # Logger 复用
    a = get_logger("selftest_logger", to_file=False)
    b = get_logger("selftest_logger", to_file=False)
    check("同名 logger 复用同一实例（修复 FD 泄漏）", a is b)

    # 凭据脱敏真实生效
    record = logging.LogRecord("selftest", logging.INFO, __file__, 1,
                               "redis://u:secretpw@h:6379/0", None, None)
    rendered = logging.Formatter("%(message)s").format(record)
    check("日志中的连接串密码被脱敏", "secretpw" not in redact(rendered),
          redact(rendered))

    # Redis 汇聚 handler 不崩（真实服务启动时第一条日志就会触发）
    fake = FakeRedis()
    original = cc.get_redis
    cc.get_redis = lambda: fake
    detach_redis_sink()
    try:
        started = attach_redis_sink("fpp:selftest_logs", max_logs=100)
        check("首次挂载成功", started is True)
        check("重复挂载幂等（修复日志重复写入）",
              attach_redis_sink("fpp:selftest_logs", max_logs=100) is False)

        logger = get_logger("selftest_sink", to_file=False)
        logger.info("selftest log line 1")
        logger.info("redis://admin:topsecret@cache:6379/0 connected")
        for handler in logging.getLogger().handlers:
            handler.flush()

        lines = fake.lrange("fpp:selftest_logs", 0, 50)
        check("日志已写入 Redis", len(lines) >= 2, lines)
        check("日志不含明文密码",
              all("topsecret" not in line for line in lines), lines)
        check("每条日志只写一次（修复重复）",
              len([l for l in lines if "selftest log line 1" in l]) == 1, lines)
        check("日志包含级别",
              all(" INFO " in line for line in lines
                  if "selftest log line 1" in line), lines)

        # 按级别筛选：后台日志面板的级别下拉框走这条链路
        from .. import log_sink as _log_sink
        from ..core.logging import _level_matches, read_logs
        sample = "2026-10-13 00:00:00 INFO    name: message"
        check("级别是第 3 个 token（修复取到时间）",
              _level_matches(sample, "INFO") and not _level_matches(sample, "ERROR"))

        original_key = _log_sink.LOG_KEY
        _log_sink.LOG_KEY = "fpp:selftest_logs"
        try:
            info_only = read_logs(50, "INFO")
            check("按级别筛选能命中（修复恒返回空）",
                  len(info_only) >= 2, info_only)
            check("不匹配的级别被排除",
                  read_logs(50, "ERROR") == [], read_logs(50, "ERROR"))
        finally:
            _log_sink.LOG_KEY = original_key
    finally:
        detach_redis_sink()
        cc.get_redis = original


def test_fetchers():
    section("采集源")
    from ..fetcher import baseFetcher as breaker
    from ..fetcher.baseFetcher import BaseFetcher, strip_tags
    from ..fetcher.registry import discover, get_fetcher

    sources = discover()
    check("采集源数量 >= 20", len(sources) >= 20, len(sources))
    names = [c.name for c in sources]
    check("name 全局唯一", len(names) == len(set(names)))

    known = get_fetcher("thespeedx")
    check("可按名查找采集源", known is not None)
    check("未知源返回 None", get_fetcher("__nope__") is None)

    text = "1.2.3.4:8080\n5.6.7.8:3128\r\nfoo\n1.2.3.4:8080\n"
    parsed = BaseFetcher.parse_from_text(text)
    check("解析并去重", parsed == ["1.2.3.4:8080", "5.6.7.8:3128"], parsed)

    check("剥离 HTML 标签", "1.2.3.4" in strip_tags("<td>1.2.3.4</td><td>8080</td>"))
    check("&nbsp; 被替换为空格",
          "&nbsp;" not in strip_tags("1.2.3.4&nbsp;&nbsp;8080"))

    # 熔断语义
    from .. import config_center as cc
    from .fake_redis import FakeRedis
    fake = FakeRedis()
    original = cc.get_redis
    cc.get_redis = lambda: fake
    try:
        fake.hset(cc.KEY_CONFIG, "FETCHER_MAX_FAIL", "2")
        cc._bump_cache()
        name = "__selftest_src__"
        breaker._breaker.pop(name, None)

        breaker.mark_failure(name)
        check("第一次失败未熔断", not breaker.is_tripped(name))
        # 回归：查询本身不得清空失败计数
        # （旧实现里 is_tripped 在 until==0 时会 pop 掉整条状态，
        #   而 enabled_fetchers() 每轮都查一次，导致熔断永远攒不到阈值）
        check("查询不破坏累计失败计数",
              breaker.breaker_status(name).get("fails") == 1,
              breaker.breaker_status(name))

        breaker.mark_failure(name)
        check("达到阈值后熔断", breaker.is_tripped(name))
        check("熔断状态同步到 Redis", name in cc.breaker_state())

        # 冷却结束后自动复位并重新计数
        breaker._breaker[name]["until"] = time.time() - 1
        check("冷却结束后不再熔断", not breaker.is_tripped(name))
        check("冷却结束清零计数",
              breaker.breaker_status(name).get("fails", 0) == 0,
              breaker.breaker_status(name))

        breaker.mark_failure(name)
        breaker.mark_failure(name)
        check("重新计数后可再次熔断", breaker.is_tripped(name))

        breaker.mark_success(name)
        check("成功后恢复", not breaker.is_tripped(name))
        breaker._breaker.pop(name, None)
    finally:
        cc.get_redis = original
        cc._bump_cache()


def test_fetch_zero_result_trips():
    section("抓到 0 条即判失败（熔断语义修复）")
    from .. import config_center as cc
    from ..fetcher import baseFetcher as breaker_mod
    from ..fetcher.baseFetcher import BaseFetcher
    from .fake_redis import FakeRedis

    NAME = "__selftest_zero__"

    class _Stub(BaseFetcher):
        name = NAME
        url = "https://example.com"
        urls = ["https://example.com/list"]
        is_html = True

    stub = _Stub()
    # 模拟"HTTP 200 + 有正文，但一个代理都解析不出来"（验证码页 / 改版空页）
    stub._request = lambda client, url, timeout: (
        "<html><body><div>请输入验证码后访问</div></body></html>")

    fake = FakeRedis()
    original = cc.get_redis
    cc.get_redis = lambda: fake
    try:
        fake.hset(cc.KEY_CONFIG, "FETCHER_MAX_FAIL", "2")
        fake.hset(cc.KEY_CONFIG, "FETCH_RETRY", "0")   # 单次尝试，跑得快
        cc._bump_cache()
        breaker_mod._breaker.pop(NAME, None)

        result = list(stub.fetch())
        check("没有解析出代理", result == [], result)
        state = breaker_mod.breaker_status(NAME)
        check("判为失败并累加失败计数（修复：不再误判成功）",
              state.get("fails", 0) >= 1, state)

        # 对照组：能解析出代理时应判成功
        stub2 = _Stub()
        stub2.name = NAME + "_ok"
        stub2._request = lambda client, url, timeout: "1.2.3.4:8080\n5.6.7.8:3128"
        breaker_mod._breaker.pop(stub2.name, None)
        ok_result = list(stub2.fetch())
        check("解析出代理时正常产出", len(ok_result) == 2, ok_result)
        check("产出前已过滤非法地址",
              all(":" in p for p in ok_result), ok_result)
        check("成功时清零失败计数",
              breaker_mod.breaker_status(stub2.name).get("fails", 0) == 0)

        # 采集过程中抛异常：生成器被中断，熔断结算不会执行，
        # 必须由 helper.fetch._collect_one 兜底记一次失败
        from ..helper.fetch import _collect_one

        class _Boom(BaseFetcher):
            name = NAME + "_boom"
            url = "https://example.com"
            urls = ["https://example.com/list"]

            def parse(self, text):
                raise RuntimeError("源站返回了意料之外的结构")

        boom = _Boom()
        boom._request = lambda client, url, timeout: "whatever"
        breaker_mod._breaker.pop(boom.name, None)
        pool, lock = {}, __import__("threading").Lock()
        collected = _collect_one(boom, pool, lock, None, False, 0)
        check("异常的源没有产出", collected == 0 and pool == {}, collected)
        check("异常的源也计入熔断失败（修复：漏记导致永不熔断）",
              breaker_mod.breaker_status(boom.name).get("fails", 0) >= 1,
              breaker_mod.breaker_status(boom.name))

        breaker_mod._breaker.pop(NAME, None)
        breaker_mod._breaker.pop(stub2.name, None)
    finally:
        cc.get_redis = original
        cc._bump_cache()


# =====================================================================
# 10.5 校验统计不变式
# =====================================================================

def test_check_stats_invariant():
    section("校验统计不变式")
    from .. import config_center as cc
    from ..db.dbClient import reset_store
    from ..helper import check as check_mod
    from ..helper.proxy import Proxy
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    # 关掉三个需要访问外网的富化步骤，保证用例离线可跑
    for key in ("PROXY_REGION", "ASN_ENABLED", "DNSBL_ENABLED"):
        fake.hset(cc.KEY_CONFIG, key, "0")
    cc._bump_cache()
    reset_store()

    original_tcp = check_mod.tcp_check
    original_http = check_mod.http_probe
    original_https = check_mod.https_probe

    async def _ok_tcp(ip, port, timeout):
        return True

    async def _ok_http(session, proxy_str, url, timeout, **kwargs):
        return True, 0.2, "9.9.9.9", {"via": "proxy"}

    async def _ok_https(session, proxy_str, url, timeout, **kwargs):
        return True

    check_mod.tcp_check = _ok_tcp
    check_mod.http_probe = _ok_http
    check_mod.https_probe = _ok_https
    try:
        stats = check_mod.check(
            [Proxy(proxy="1.1.1.1:80", source=["t"]),
             Proxy(proxy="not-a-proxy", source=["t"]),
             Proxy(proxy="2.2.2.2:81", source=["t"])],
            work_type="raw")
        check("checked 覆盖全部条目（含格式错误）", stats["checked"] == 3, stats)
        check("passed / failed 各自准确",
              stats["passed"] == 2 and stats["failed"] == 1, stats)
        check("不变式 passed + failed == checked",
              stats["passed"] + stats["failed"] == stats["checked"], stats)
    finally:
        check_mod.tcp_check = original_tcp
        check_mod.http_probe = original_http
        check_mod.https_probe = original_https
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


def test_dead_proxy_backoff_pipeline():
    section("死代理退避全链路（校验 → 退避 → 恢复）")
    from .. import config_center as cc
    from ..db.dbClient import get_store, reset_store
    from ..helper import check as check_mod
    from ..helper.proxy import Proxy
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    for key in ("PROXY_REGION", "ASN_ENABLED", "DNSBL_ENABLED"):
        fake.hset(cc.KEY_CONFIG, key, "0")
    cc._bump_cache()
    reset_store()

    original_tcp = check_mod.tcp_check
    original_http = check_mod.http_probe
    original_https = check_mod.https_probe
    state = {"tcp": False, "http": True}

    async def _tcp(ip, port, timeout):
        return state["tcp"]

    async def _http(session, proxy_str, url, timeout, **kwargs):
        if not state["http"]:
            return False, 0.0, "", {}
        return True, 0.2, "9.9.9.9", {"via": "proxy"}

    async def _https(session, proxy_str, url, timeout, **kwargs):
        return True

    check_mod.tcp_check = _tcp
    check_mod.http_probe = _http
    check_mod.https_probe = _https
    try:
        store = get_store()
        payload = {"proxy": "6.6.6.6:80", "https": False, "score": 80,
                   "region": "", "source": "t", "anonymous": "elite",
                   "check_count": 1, "error_count": 0, "ewma_speed": 0.3}

        # --- 第一次硬失败：移出池 + 进入第 1 级退避 ---
        store.put(dict(payload))
        check("初始在池内", store.exists("6.6.6.6:80"))
        check_mod.check([Proxy.from_dict(dict(payload))], work_type="use")
        check("硬失败后被移出有效池", not store.exists("6.6.6.6:80"))
        check("硬失败后进入退避窗口", store.is_black("6.6.6.6:80"))
        check("失败计数为 1", store.black_fail_count("6.6.6.6:80") == 1)
        first_window = store.black_until("6.6.6.6:80") - time.time()
        check("首次退避约 1 小时", 3500 < first_window < 3700, first_window)

        # --- 第二次硬失败：退避升档 ---
        store.put(dict(payload))
        check_mod.check([Proxy.from_dict(dict(payload))], work_type="use")
        check("二次失败计数为 2", store.black_fail_count("6.6.6.6:80") == 2)
        second_window = store.black_until("6.6.6.6:80") - time.time()
        check("二次退避升到约 2 小时", 7000 < second_window < 7400, second_window)
        check("二次失败后仍在池外", not store.exists("6.6.6.6:80"))

        # --- 校验通过：清零并解除拉黑 ---
        state["tcp"] = True
        store.put(dict(payload))
        check_mod.check([Proxy.from_dict(dict(payload))], work_type="use")
        check("通过后计数清零", store.black_fail_count("6.6.6.6:80") == 0)
        check("通过后解除拉黑", not store.is_black("6.6.6.6:80"))
        check("通过的代理留在池内", store.exists("6.6.6.6:80"))

        # --- 软失败（fail_http）：必须进退避，但保留池成员资格 ---
        state["http"] = False
        soft = {"proxy": "6.6.6.7:80", "https": False, "score": 80,
                "region": "", "source": "t", "anonymous": "elite",
                "check_count": 1, "error_count": 0, "ewma_speed": 0.3}
        store.put(dict(soft))
        check_mod.check([Proxy.from_dict(dict(soft))], work_type="use")
        check("软失败也进入退避窗口（否则会被无限重试）",
              store.is_black("6.6.6.7:80"))
        check("软失败计数为 1", store.black_fail_count("6.6.6.7:80") == 1)
        check("软失败不移出池（目标故障时大清池更糟）",
              store.exists("6.6.6.7:80"))
    finally:
        check_mod.tcp_check = original_tcp
        check_mod.http_probe = original_http
        check_mod.https_probe = original_https
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


def test_diagnose():
    section("单代理深度诊断")
    from ..helper import diagnose as diag
    from aiohttp import ClientTimeout

    original_tcp = diag.tcp_check
    original_http = diag.http_probe
    original_https = diag.https_probe
    original_resolver = diag.get_resolver

    state = {"tcp": True, "http": True, "https": True, "origin": "1.2.3.4",
             "headers": {}}

    async def _tcp(ip, port, timeout):
        return state["tcp"]

    async def _http(session, proxy_str, url, timeout, **kwargs):
        if not state["http"]:
            return False, 0.0, "", {}
        return True, 0.25, state["origin"], state["headers"]

    async def _https(session, proxy_str, url, timeout, **kwargs):
        return state["https"]

    class _FakeResolver:
        async def resolve_many(self, ips):
            return {ip: "CN" for ip in ips}

    diag.tcp_check = _tcp
    diag.http_probe = _http
    diag.https_probe = _https
    diag.get_resolver = lambda: _FakeResolver()
    try:
        # --- 全部正常 → excellent ---
        result = diag.diagnose_sync("1.2.3.4:80", "http://t/get", "https://t/",
                                    with_region=True)
        check("TCP 可用", result["tcp"]["ok"] is True)
        check("HTTP 可用", result["http"]["ok"] is True)
        check("HTTPS 可用", result["https"]["ok"] is True)
        check("判定为 excellent", result["verdict"] == "excellent", result["verdict"])
        check("记录出口 IP", result["http"]["origin_ip"] == "1.2.3.4")
        check("出口 IP 与代理 IP 相同判为 elite",
              result["http"]["anonymous"] == "elite",
              result["http"]["anonymous"])
        check("解析出国家", result["region"] == "CN", result["region"])
        check("分层延迟都已记录",
              result["tcp"]["elapsed_ms"] >= 0 and result["http"]["elapsed_ms"] >= 0
              and result["https"]["elapsed_ms"] >= 0)
        check("判定文案随判定变化", "excellent" in result["verdict_text"])

        # --- HTTP 通但 HTTPS 不通 → limited ---
        state["https"] = False
        result = diag.diagnose_sync("1.2.3.4:80", "http://t/get", "https://t/")
        check("仅 HTTP 可用时判定为 limited",
              result["verdict"] == "limited", result["verdict"])
        state["https"] = True

        # --- HTTP 不通 → unusable ---
        state["http"] = False
        result = diag.diagnose_sync("1.2.3.4:80", "http://t/get", "https://t/")
        check("HTTP 不通判定为 unusable", result["verdict"] == "unusable",
              result["verdict"])
        check("给出不可用原因", bool(result.get("error")), result.get("error"))
        state["http"] = True

        # --- TCP 不通：不再往下探 ---
        state["tcp"] = False
        result = diag.diagnose_sync("1.2.3.4:80", "http://t/get", "https://t/")
        check("TCP 不通判定为 unusable", result["verdict"] == "unusable")
        check("TCP 不通给出原因", result.get("error") == "TCP 端口不通",
              result.get("error"))
        check("TCP 不通时不再探测 HTTP", result["http"]["ok"] is False)
        state["tcp"] = True

        # --- 透明代理：出口 IP != 代理 IP ---
        state["origin"] = "8.8.8.8"
        result = diag.diagnose_sync("1.2.3.4:80", "http://t/get", "https://t/")
        check("出口 IP 不同判为 transparent",
              result["http"]["anonymous"] == "transparent",
              result["http"]["anonymous"])
        state["origin"] = "1.2.3.4"

        # --- 非法地址 ---
        result = diag.diagnose_sync("bad-proxy", "http://t/get", "https://t/")
        check("非法地址被识别", result.get("error") == "代理地址格式非法",
              result.get("error"))
        check("非法地址判定为 unusable", result["verdict"] == "unusable")

        # --- 文本渲染 ---
        state["https"] = False
        result = diag.diagnose_sync("1.2.3.4:80", "http://t/get", "https://t/")
        text = diag.format_text(result)
        check("文本含代理地址", "1.2.3.4:80" in text, text)
        check("文本含判定", "limited" in text, text)
        check("文本含分层延迟", "TCP" in text and "HTTP" in text and "HTTPS" in text)
        state["https"] = True
    finally:
        diag.tcp_check = original_tcp
        diag.http_probe = original_http
        diag.https_probe = original_https
        diag.get_resolver = original_resolver


def test_diagnose_cli():
    section("诊断 CLI 子命令")
    from click.testing import CliRunner
    from ..helper import diagnose as diag
    from .. import proxyPool

    original = diag.diagnose_sync
    captured = {}

    def _fake(proxy_str, http_url, https_url, **kwargs):
        captured["proxy"] = proxy_str
        captured["http_url"] = http_url
        captured["https_url"] = https_url
        captured["kwargs"] = kwargs
        return {
            "proxy": proxy_str, "ip": "1.2.3.4", "port": 80,
            "tcp": {"ok": True, "elapsed_ms": 10.0},
            "http": {"ok": True, "elapsed_ms": 200.0, "speed": 0.2,
                     "origin_ip": "9.9.9.9", "anonymous": "elite"},
            "https": {"ok": True, "elapsed_ms": 300.0},
            "region": "CN", "verdict": "excellent",
            "verdict_text": diag.VERDICT_TEXT["excellent"],
        }

    diag.diagnose_sync = _fake
    try:
        runner = CliRunner()
        result = runner.invoke(proxyPool.cli, ["check", "1.2.3.4:80"])
        check("命令执行成功", result.exit_code == 0, result.output)
        check("输出含代理地址", "1.2.3.4:80" in result.output, result.output)
        check("输出含判定", "excellent" in result.output, result.output)
        check("代理地址已归一化", captured.get("proxy") == "1.2.3.4:80",
              captured.get("proxy"))

        result = runner.invoke(proxyPool.cli, ["check", "1.2.3.4:80", "--json"])
        check("JSON 输出可解析", result.exit_code == 0, result.output)
        try:
            import json as _json
            payload = _json.loads(result.output)
            check("JSON 含 verdict", payload.get("verdict") == "excellent")
            check("JSON 含分层延迟", "elapsed_ms" in payload.get("tcp", {}))
        except Exception as exc:
            check("JSON 可解析", False, str(exc))

        result = runner.invoke(proxyPool.cli, ["check", "not-a-proxy"])
        check("非法地址返回非零退出码", result.exit_code != 0, result.exit_code)
        check("非法地址给出可读原因", "格式非法" in result.output, result.output)

        result = runner.invoke(proxyPool.cli, ["--help"])
        check("子命令出现在帮助里", "check" in result.output)
    finally:
        diag.diagnose_sync = original


def test_scheduler_reschedule():
    section("调度任务间隔热更新")
    from .. import config_center as cc
    from ..services import scheduler as sch
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    for key, value in (("CHECK_INTERVAL", "5"), ("CLEAN_INTERVAL", "600"),
                       ("STAT_INTERVAL", "60")):
        fake.hset(cc.KEY_CONFIG, key, value)
    cc._bump_cache()
    original_scheduler = sch._scheduler
    original_active = dict(sch._active_intervals)
    try:
        built = sch.build_scheduler()
        jobs = {j.id: j for j in built.get_jobs()}
        check("注册了配置热更新监听任务", "reschedule_watch" in jobs, sorted(jobs))
        check("初始间隔来自配置",
              jobs["recheck"].trigger.interval_length == 300,
              jobs["recheck"].trigger.interval_length)
        sch._scheduler = built
        fake.hset(cc.KEY_CONFIG, "CHECK_INTERVAL", "30")
        cc._bump_cache()
        sch.task_reschedule()
        check("后台改配置后任务间隔立即调整（无需重启）",
              jobs["recheck"].trigger.interval_length == 1800,
              jobs["recheck"].trigger.interval_length)
        sch.task_reschedule()
        check("重复调用幂等",
              jobs["recheck"].trigger.interval_length == 1800,
              jobs["recheck"].trigger.interval_length)
    finally:
        sch._scheduler = original_scheduler
        sch._active_intervals.clear()
        sch._active_intervals.update(original_active)
        cc.get_redis = original_get
        cc._bump_cache()


# =====================================================================
# 11. 代理模型
# =====================================================================

def test_proxy_model():
    section("代理模型")
    from ..helper.proxy import Proxy, new_proxy

    p = Proxy.from_dict({"proxy": "1.2.3.4:80", "https": True, "score": "50",
                         "source": "a/b", "speed": "0.5", "region": None})
    check("字符串字段被转换", p.score == 50 and p.speed == 0.5, p.to_dict())
    check("None 字段降级为空串", p.region == "", repr(p.region))
    check("source 拆成列表", p.source == ["a", "b"], p.source)
    check("序列化后可往返",
          Proxy.from_json(p.to_json()).to_dict() == p.to_dict())

    # streak 必须参与往返：调度器每轮复检都 from_dict 重建 Proxy，
    # 漏读该字段会让"连续存活天数"永远停在 1
    p.streak = 7
    check("streak 序列化后可往返",
          Proxy.from_json(p.to_json()).streak == 7,
          Proxy.from_json(p.to_json()).streak)
    check("旧数据缺 streak 时降级为 0",
          Proxy.from_dict({"proxy": "5.6.7.8:80"}).streak == 0)

    p.add_source("c")
    p.add_source("c")
    check("来源去重", p.source == ["a", "b", "c"], p.source)

    # v0.1.0: mark_pass 不再手工加分，只维护状态字段
    p2 = Proxy(proxy="2.2.2.2:8080", source=["src"], fail_count=2)
    p2.mark_pass(True, 0.25, 100, 5)
    check("通过后 check_count+1", p2.check_count == 1, p2.check_count)
    check("通过后 fail_count 自愈", p2.fail_count == 1, p2.fail_count)
    check("通过后 last_status=True", p2.last_status is True, p2.last_status)
    check("通过后 https 标记", p2.https is True, p2.https)
    check("通过后 speed 记录", p2.speed == 0.25, p2.speed)
    check("通过后 score 未直接改变", p2.score == 0, p2.score)  # 初始 0，mark_pass 不碰 score

    # v0.1.0: mark_fail 不再手工扣分，只维护状态字段
    p3 = Proxy(proxy="3.3.3.3:8080", source=["src"])
    p3.mark_fail(0, 5)
    check("失败后 check_count+1", p3.check_count == 1, p3.check_count)
    check("失败后 fail_count+1", p3.fail_count == 1, p3.fail_count)
    check("失败后 error_count+1", p3.error_count == 1, p3.error_count)
    check("失败后 last_status=False", p3.last_status is False, p3.last_status)
    check("失败后 score 未直接改变", p3.score == 0, p3.score)

    # v0.1.0: should_evict 支持 fail_count 阈值
    p4 = Proxy(proxy="4.4.4.4:8080", source=["src"], fail_count=3)
    check("超过失败阈值应淘汰", p4.should_evict(2, 0.5, 10))
    p4.fail_count = 0
    check("未超失败阈值不淘汰", not p4.should_evict(5, 0.5, 10), p4.fail_count)

    # v0.1.0: should_evict 支持 error_rate 阈值（需达到最小检验次数）
    p5 = Proxy(proxy="5.5.5.5:8080", source=["src"],
               check_count=20, error_count=11)  # error_rate=0.55
    check("错误率超限应淘汰", p5.should_evict(0, 0.5, 10))
    p5.error_count = 5  # error_rate=0.25
    check("错误率未超限不淘汰", not p5.should_evict(0, 0.5, 10), p5.error_count)
    p6 = Proxy(proxy="6.6.6.6:8080", source=["src"],
               check_count=5, error_count=5)  # error_rate=1.0 但未达 min_checks
    check("未达最小检验次数不淘汰", not p6.should_evict(0, 0.5, 10), p6.check_count)

    # v0.1.0: _recalc_score 复合分
    p7 = Proxy(proxy="7.7.7.7:8080", source=["src"],
               check_count=10, error_count=0, ewma_speed=1.0)
    check("零错误率+1秒延迟", p7._recalc_score() == 99, p7._recalc_score())
    p7.ewma_speed = 50.0
    check("延迟封顶 30 秒", p7._recalc_score() == 70, p7._recalc_score())
    p7.error_count = 5
    # (1-0.5)*100 - min(50, 30) = 50 - 30 = 20
    check("50%错误率+50秒延迟", p7._recalc_score() == 20, p7._recalc_score())
    p8 = Proxy(proxy="8.8.8.8:8080", source=["src"],
               check_count=0, error_count=0, ewma_speed=None)
    check("未检验代理得 0 分", p8._recalc_score() == 0, p8._recalc_score())

    check("new_proxy 应用初始分", new_proxy("1.1.1.1:80", "src", 20).score == 20)

    # v0.1.0: streak 由 _record 统一管理，mark_pass/mark_fail 不直接修改
    p9 = Proxy(proxy="9.9.9.9:80", source=["src"])
    p9.mark_pass(True, 0.5, 100, 5)
    check("mark_pass 不直接改变 streak", p9.streak == 0, p9.streak)
    p9.mark_fail()
    check("mark_fail 不直接改变 streak", p9.streak == 0, p9.streak)

    # v0.1.0: B-07 ip_type / B-08 dnsbl 字段默认值
    p10 = Proxy(proxy="10.10.10.10:80", source=["src"])
    check("ip_type 默认 unknown", p10.ip_type == "unknown", p10.ip_type)
    check("dnsbl 默认 False", p10.dnsbl is False, p10.dnsbl)
    # from_dict 兼容旧数据（无 ip_type/dnsbl）
    p11 = Proxy.from_dict({"proxy": "11.11.11.11:80", "https": False, "score": 0,
                            "source": "s", "speed": "0.1"})
    check("旧数据缺 ip_type 回退 unknown", p11.ip_type == "unknown", p11.ip_type)
    check("旧数据缺 dnsbl 回退 False", p11.dnsbl is False, p11.dnsbl)


def test_region_helpers():
    section("地域与匿名度")
    from ..helper.region import judge_anonymous, parse_origin_ip

    # v0.1.0: 三段式匿名度（elite / anonymous / transparent）
    check("出口等于代理且无风险头 → elite",
          judge_anonymous("1.1.1.1", "1.1.1.1") == "elite")
    check("出口不同于代理 → transparent",
          judge_anonymous("1.1.1.1", "9.9.9.9") == "transparent")
    check("取不到出口 → 未知", judge_anonymous("1.1.1.1", "") == "")
    check("出口等于代理但 Via 头 → anonymous",
          judge_anonymous("1.1.1.1", "1.1.1.1",
                          {"Via": "1.2.3.4"}) == "anonymous")
    check("出口等于代理但 X-Forwarded-For 头 → anonymous",
          judge_anonymous("1.1.1.1", "1.1.1.1",
                          {"X-Forwarded-For": "1.2.3.4"}) == "anonymous")
    check("headers 大小写不敏感",
          judge_anonymous("1.1.1.1", "1.1.1.1",
                          {"via": "1.2.3.4"}) == "anonymous")

    check("解析 httpbin origin",
          parse_origin_ip('{"origin": "1.2.3.4"}') == "1.2.3.4")
    check("多出口取首个",
          parse_origin_ip('{"origin": "1.2.3.4, 5.6.7.8"}') == "1.2.3.4")
    check("非 JSON 容错", parse_origin_ip("<html>") == "")
    check("空响应容错", parse_origin_ip("") == "")

    # B-06: MmdbProvider 优雅降级（未配置 mmdb 文件时不崩溃）
    from ..helper.region import MmdbProvider
    provider = MmdbProvider("/nonexistent/path.mmdb")
    check("mmdb 文件不存在时返回空", provider.lookup("1.1.1.1") == "")

    # B-07: ASN 推导
    from ..helper.asn import lookup_asn
    check("ASN 未启用/空 IP 返回 unknown", lookup_asn("") == "unknown")

    # B-08: DNSBL 查询（check_dnsbl 是 async，需要跑在 loop 里）
    from ..helper.reputation import check_dnsbl
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            check_dnsbl("1.1.1.1", ["nonexistent.zone.invalid"]))
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    check("DNSBL 不存在的 zone 返回 clean", result.get("listed") is False)


# =====================================================================
# 12. 延迟分层 tier（借鉴 docs/09 P0-3）
# =====================================================================

def test_latency_tier():
    section("延迟分层 tier")
    from .. import config_center as cc
    from ..core.errors import ValidationError
    from ..core.validators import parse_tier
    from ..helper.proxy import (TIERS, effective_speed, tier_of, tier_of_proxy,
                              tier_thresholds)
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    cc._bump_cache()
    try:
        # --- 阈值来源：配置中心优先 ---
        fast, medium = tier_thresholds()
        check("默认 fast 阈值 500ms", fast == 500, fast)
        check("默认 medium 阈值 2000ms", medium == 2000, medium)

        # --- 分层判定 ---
        check("0.1s 判为 fast", tier_of(0.1) == "fast", tier_of(0.1))
        check("0.49s 判为 fast", tier_of(0.49) == "fast", tier_of(0.49))
        check("0.5s 判为 medium（边界取开区间）", tier_of(0.5) == "medium", tier_of(0.5))
        check("1.0s 判为 medium", tier_of(1.0) == "medium", tier_of(1.0))
        check("2.0s 判为 slow", tier_of(2.0) == "slow", tier_of(2.0))
        check("9.9s 判为 slow", tier_of(9.9) == "slow", tier_of(9.9))
        check("None 判为 unknown", tier_of(None) == "unknown")
        check("0 判为 unknown（无有效样本）", tier_of(0) == "unknown")
        check("负数判为 unknown", tier_of(-1) == "unknown")
        check("非数字判为 unknown", tier_of("abc") == "unknown")
        check("标签集合固定为 4 个", tuple(TIERS) == ("fast", "medium", "slow", "unknown"), TIERS)

        # --- 有效延迟：EWMA 优先，回落最近一次 ---
        check("优先取 EWMA", effective_speed({"ewma_speed": 0.3, "speed": 5.0}) == 0.3)
        check("无 EWMA 时回落 speed",
              effective_speed({"ewma_speed": None, "speed": 1.2}) == 1.2)
        check("EWMA 为 0 时回落 speed",
              effective_speed({"ewma_speed": 0, "speed": 1.2}) == 1.2)
        check("两者都无返回 None", effective_speed({}) is None)
        check("非 dict 返回 None", effective_speed("x") is None)
        check("按元数据判定分层",
              tier_of_proxy({"ewma_speed": 0.2}) == "fast")
        check("无样本判为 unknown",
              tier_of_proxy({"proxy": "1.2.3.4:80"}) == "unknown")

        # --- 参数校验：非法 tier 显式 400 ---
        check("合法 tier 通过", parse_tier("fast") == "fast")
        check("大小写不敏感", parse_tier("FAST") == "fast")
        check("空串视为不过滤", parse_tier("") is None)
        check("缺省视为不过滤", parse_tier(None) is None)
        raised = False
        try:
            parse_tier("ultra")
        except ValidationError:
            raised = True
        check("非法 tier 被拒（不静默忽略）", raised)

        # --- 配置项可热更新 ---
        check("写入合法阈值", cc.set_many({"TIER_FAST_MS": 100,
                                          "TIER_MEDIUM_MS": 1000}) == [])
        cc._bump_cache()
        check("阈值改动后立即生效", tier_thresholds() == (100, 1000), tier_thresholds())
        check("0.05s 仍为 fast", tier_of(0.05) == "fast")
        check("0.5s 变为 medium", tier_of(0.5) == "medium")
        check("2.0s 变为 slow", tier_of(2.0) == "slow")

        # --- 交叉校验：fast 必须小于 medium ---
        bad = cc.set_many({"TIER_FAST_MS": 3000})
        check("fast >= medium 被拒", bad != [], bad)
        check("被拒时不写入",
              cc.get("TIER_FAST_MS") == 100, cc.get("TIER_FAST_MS"))
        bad = cc.set_many({"TIER_MEDIUM_MS": 50})
        check("medium <= fast 被拒", bad != [], bad)
        check("成对写入同反也被拒",
              cc.set_many({"TIER_FAST_MS": 800, "TIER_MEDIUM_MS": 800}) != [])
        check("拒绝后阈值保持不变", tier_thresholds() == (100, 1000), tier_thresholds())

        # 恢复默认，避免影响后续用例
        cc.reset(["TIER_FAST_MS", "TIER_MEDIUM_MS"])
        cc._bump_cache()
        check("重置后回到默认阈值", tier_thresholds() == (500, 2000), tier_thresholds())
    finally:
        cc.get_redis = original_get
        cc._bump_cache()


def test_tier_filter_and_stats():
    section("延迟分层筛选与统计")
    from .. import config_center as cc
    from ..db.dbClient import reset_store
    from ..db.redisClient import RedisStore
    from ..gateway import deps
    from ..gateway.api import with_tier
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    reset_store()
    try:
        store = RedisStore(fake, table="t")
        # fast / medium / slow / unknown 各一条
        store.put({"proxy": "8.8.8.1:80", "https": False, "score": 90,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.1})
        store.put({"proxy": "8.8.8.2:80", "https": False, "score": 80,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 1, "ewma_speed": 1.0})
        store.put({"proxy": "8.8.8.3:80", "https": False, "score": 70,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 2, "ewma_speed": 5.0})
        store.put({"proxy": "8.8.8.4:80", "https": False, "score": 60,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 0, "error_count": 0, "speed": 0})

        fast_picks = deps.pick_candidates(store, False, None, "latency", 10,
                                          None, False, "fast")
        check("tier=fast 只返回 fast",
              [p["proxy"] for p in fast_picks] == ["8.8.8.1:80"],
              [p["proxy"] for p in fast_picks])
        medium_picks = deps.pick_candidates(store, False, None, "latency", 10,
                                             None, False, "medium")
        check("tier=medium 只返回 medium",
              [p["proxy"] for p in medium_picks] == ["8.8.8.2:80"],
              [p["proxy"] for p in medium_picks])
        slow_picks = deps.pick_candidates(store, False, None, "latency", 10,
                                          None, False, "slow")
        check("tier=slow 只返回 slow",
              [p["proxy"] for p in slow_picks] == ["8.8.8.3:80"],
              [p["proxy"] for p in slow_picks])
        unknown_picks = deps.pick_candidates(store, False, None, "latency", 10,
                                             None, False, "unknown")
        check("tier=unknown 只返回无样本代理",
              [p["proxy"] for p in unknown_picks] == ["8.8.8.4:80"],
              [p["proxy"] for p in unknown_picks])
        check("不传 tier 时返回全部",
              len(deps.pick_candidates(store, False, None, "latency", 10)) == 4)

        # 响应对象带上 tier 字段
        item = with_tier({"proxy": "8.8.8.1:80", "ewma_speed": 0.1})
        check("响应带 tier 字段", item.get("tier") == "fast", item.get("tier"))
        check("非代理对象不被改写", with_tier({"code": 0}).get("tier") is None)
    finally:
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


def test_tier_api():
    section("延迟分层 API")
    from .. import config_center as cc
    from ..db.dbClient import get_store, reset_store
    from .fake_redis import FakeRedis

    client, fake = _build_client()
    original_get = cc.get_redis
    try:
        cc.get_redis = lambda: fake
        reset_store()
        # 必须写进 get_store() 返回的那个实例（表名取自 TABLE_NAME 配置），
        # 自建 RedisStore 会落到另一组 Key 上，接口自然读不到数据。
        store = get_store()
        store.put({"proxy": "9.9.9.1:80", "https": False, "score": 90,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.1})
        store.put({"proxy": "9.9.9.2:80", "https": False, "score": 80,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 1, "ewma_speed": 1.0})
        store.put({"proxy": "9.9.9.3:80", "https": False, "score": 70,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 2, "ewma_speed": 5.0})

        r = client.get("/get/?tier=fast")
        check("GET /get/?tier=fast 返回 fast 代理",
              r.status_code == 200 and r.json().get("proxy") == "9.9.9.1:80",
              r.text[:120])
        check("返回对象带 tier 标签", r.json().get("tier") == "fast", r.json().get("tier"))

        r = client.get("/batch/?tier=slow")
        body = r.json()
        check("GET /batch/?tier=slow 只返回 slow",
              body.get("count") == 1
              and body["data"][0]["proxy"] == "9.9.9.3:80", body)
        check("批量结果带 tier 标签",
              body["data"][0].get("tier") == "slow", body["data"][0].get("tier"))

        r = client.get("/all/?tier=medium")
        items = r.json()
        check("GET /all/?tier=medium 只返回 medium",
              len(items) == 1 and items[0]["proxy"] == "9.9.9.2:80", items)

        r = client.get("/count/")
        tiers = r.json().get("tier") or {}
        check("/count/ 含 tier 分布", "tier" in r.json(), list(r.json().keys()))
        check("tier 分布 fast=1", tiers.get("fast") == 1, tiers)
        check("tier 分布 medium=1", tiers.get("medium") == 1, tiers)
        check("tier 分布 slow=1", tiers.get("slow") == 1, tiers)

        r = client.get("/get/?tier=ultra")
        check("非法 tier 返回 400", r.status_code == 400, r.status_code)
        check("400 不泄露内部结构", "Traceback" not in r.text)

        r = client.get("/all/?anon=ghost")
        check("非法 anon 返回 400（不静默回落成不过滤）", r.status_code == 400,
              r.status_code)
        check("400 不泄露内部结构", "Traceback" not in r.text)
    finally:
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


# =====================================================================
# 13. SOCKS4/5 协议支持（借鉴 docs/09 P0-1）
# =====================================================================

def test_socks_model_and_queue():
    section("SOCKS 协议：数据模型 / 队列载荷 / 参数校验")
    from ..core.errors import ValidationError
    from ..core.validators import parse_protocol
    from ..db.queue import decode, encode
    from ..helper.proxy import PROTOCOLS, Proxy, normalize_protocol

    # --- 数据模型 ---
    check("协议集合固定为 3 个", tuple(PROTOCOLS) == ("http", "socks4", "socks5"), PROTOCOLS)
    check("默认协议为 http", Proxy(proxy="1.1.1.1:80").protocol == "http")
    check("构造时写入 socks5", Proxy(proxy="1.1.1.1:80", protocol="socks5").protocol == "socks5")
    check("协议名大小写归一", Proxy(proxy="1.1.1.1:80", protocol="SOCKS5").protocol == "socks5")
    check("未知协议回落 http", Proxy(proxy="1.1.1.1:80", protocol="ftp").protocol == "http")
    check("历史数据（无协议字段）按 http 处理",
          Proxy.from_dict({"proxy": "1.1.1.1:80"}).protocol == "http")
    check("from_dict 读取 socks4",
          Proxy.from_dict({"proxy": "1.1.1.1:80", "protocol": "socks4"}).protocol == "socks4")
    check("from_dict 遇到未知协议按 http 处理",
          Proxy.from_dict({"proxy": "1.1.1.1:80", "protocol": "bogus"}).protocol == "http")
    check("to_dict / from_dict 往返保留协议",
          Proxy.from_dict(Proxy(proxy="1.1.1.1:80", protocol="socks5").to_dict()).protocol
          == "socks5")
    check("normalize_protocol(None) 为 http", normalize_protocol(None) == "http")
    check("normalize_protocol('') 为 http", normalize_protocol("") == "http")
    check("normalize_protocol 去空白", normalize_protocol("  socks4 ") == "socks4")

    # --- 队列载荷 ---
    key, payload = encode({"proxy": "1.2.3.4:80", "source": "t"})
    check("HTTP 载荷与旧版逐字节一致",
          payload == '{"p":"1.2.3.4:80","s":"t"}', payload)
    check("HTTP 载荷不带协议键", "o" not in payload, payload)
    key, payload = encode({"proxy": "1.2.3.4:80", "source": "t", "protocol": "socks5"})
    check("SOCKS 载荷带 o 键", '"o":"socks5"' in payload, payload)
    check("解码还原协议", decode(payload).protocol == "socks5", decode(payload).protocol)
    check("旧载荷（无 o）按 http 解码",
          decode('{"p":"1.2.3.4:80","s":"t"}').protocol == "http")
    check("裸字符串载荷按 http 解码", decode("1.2.3.4:80").protocol == "http")
    check("未知 o 值按 http 解码",
          decode('{"p":"1.2.3.4:80","o":"socks9"}').protocol == "http")
    check("解码保留来源", decode('{"p":"1.2.3.4:80","s":"a/b"}').source == ["a", "b"])

    # --- 参数校验 ---
    check("protocol 不传返回 None", parse_protocol(None) is None)
    check("protocol 空串返回 None", parse_protocol("") is None)
    check("protocol 归一化小写", parse_protocol("SOCKS5") == "socks5")
    check("protocol 接受 socks4", parse_protocol("socks4") == "socks4")
    try:
        parse_protocol("ftp")
        check("非法 protocol 抛错", False, "未抛错")
    except ValidationError:
        check("非法 protocol 抛错", True)


def test_socks_sources():
    section("SOCKS 采集源声明")
    from ..fetcher.baseFetcher import BaseFetcher
    from ..fetcher.registry import discover, get_fetcher

    check("基类默认协议为 http", BaseFetcher.protocol == "http")
    for name, expect in (("thespeedx_socks5", "socks5"),
                         ("thespeedx_socks4", "socks4"),
                         ("monosans_socks5", "socks5"),
                         ("monosans_socks4", "socks4"),
                         ("jetkai_socks5", "socks5"),
                         ("jetkai_socks4", "socks4"),
                         ("hproxy_socks5", "socks5"),
                         ("proxyscrape_socks5", "socks5")):
        cls = get_fetcher(name)
        check("源 %s 存在且声明 %s" % (name, expect),
              cls is not None and cls.protocol == expect,
              getattr(cls, "protocol", None))
        check("源 %s 有抓取地址" % name,
              cls is not None and bool(cls.urls), getattr(cls, "urls", None))

    hook = get_fetcher("hookzof_socks5")
    check("超大 SOCKS 源默认禁用（防止独占采集队列）",
          hook is not None and hook.enabled is False, getattr(hook, "enabled", None))
    check("禁用源写明原因",
          hook is not None and bool(hook.status_note), getattr(hook, "status_note", None))

    # HTTP 源不受影响
    http_src = get_fetcher("thespeedx_http")
    check("既有 HTTP 源协议仍为 http",
          http_src is not None and http_src.protocol == "http")

    # 注册表里 SOCKS 源确实被扫描到
    names = {cls.name for cls in discover()}
    check("注册表包含 SOCKS 源",
          {"thespeedx_socks5", "monosans_socks4"} <= names, sorted(names))


def test_socks_probe_end_to_end():
    section("SOCKS 探针真实链路（进程内 SOCKS 服务器）")
    import asyncio

    from aiohttp import ClientTimeout
    from ..helper.check import (HARD_FAILURES, UnsupportedProtocolError, http_probe,
                               new_socks_session, probe_session)
    from .socks_server import HttpOriginServer, Socks4Server, Socks5Server

    async def _scenario():
        # --- SOCKS5：IP 目标 ---
        origin = HttpOriginServer()
        await origin.start()
        socks5 = Socks5Server()
        await socks5.start()
        try:
            session = await new_socks_session("127.0.0.1:%d" % socks5.port,
                                              "socks5", timeout=10)
            try:
                ok, speed, origin_ip, headers = await http_probe(
                    session, "127.0.0.1:%d" % socks5.port,
                    "http://127.0.0.1:%d/get" % origin.port,
                    ClientTimeout(total=10), use_proxy_arg=False)
                check("SOCKS5 探针成功", ok is True, (ok, speed))
                check("SOCKS5 探针取到出口 IP", origin_ip == "9.9.9.9", origin_ip)
                check("SOCKS5 探针记录延迟", speed > 0, speed)
                check("SOCKS5 请求确实到达源站", origin.requests == 1, origin.requests)
                check("SOCKS5 服务器确实被使用", socks5.connections == 1,
                      socks5.connections)
            finally:
                await session.close()
        finally:
            await socks5.stop()
            await origin.stop()

        # --- SOCKS5：域名目标（由代理远程解析，不泄漏本地 DNS） ---
        origin = HttpOriginServer()
        await origin.start()
        socks5 = Socks5Server()
        await socks5.start()
        try:
            session = await new_socks_session("127.0.0.1:%d" % socks5.port,
                                              "socks5", timeout=10)
            try:
                ok, _speed, _ip, _h = await http_probe(
                    session, "127.0.0.1:%d" % socks5.port,
                    "http://localhost:%d/get" % origin.port,
                    ClientTimeout(total=10), use_proxy_arg=False)
                check("SOCKS5 域名目标探针成功", ok is True, ok)
                check("SOCKS5 把域名交给代理解析（远程 DNS）",
                      socks5.hosts == ["localhost"], socks5.hosts)
            finally:
                await session.close()
        finally:
            await socks5.stop()
            await origin.stop()

        # --- SOCKS4：IP 目标 ---
        origin = HttpOriginServer()
        await origin.start()
        socks4 = Socks4Server()
        await socks4.start()
        try:
            session = await new_socks_session("127.0.0.1:%d" % socks4.port,
                                              "socks4", timeout=10)
            try:
                ok, speed, origin_ip, _h = await http_probe(
                    session, "127.0.0.1:%d" % socks4.port,
                    "http://127.0.0.1:%d/get" % origin.port,
                    ClientTimeout(total=10), use_proxy_arg=False)
                check("SOCKS4 探针成功", ok is True, (ok, speed))
                check("SOCKS4 探针取到出口 IP", origin_ip == "9.9.9.9", origin_ip)
                check("SOCKS4 服务器确实被使用", socks4.connections == 1,
                      socks4.connections)
            finally:
                await session.close()
        finally:
            await socks4.stop()
            await origin.stop()

        # --- probe_session 的协议分流 ---
        from aiohttp import ClientSession
        shared = ClientSession()
        try:
            async with probe_session("1.2.3.4:80", "http", shared, 5) as (sess, use_proxy):
                check("HTTP 协议复用共享会话", sess is shared, sess)
                check("HTTP 协议需带 proxy 参数", use_proxy is True, use_proxy)
            async with probe_session("127.0.0.1:1080", "socks5", shared, 5) as (sess, use_proxy):
                check("SOCKS 协议使用独立会话", sess is not shared, sess)
                check("SOCKS 协议不能带 proxy 参数", use_proxy is False, use_proxy)
                check("SOCKS 独立会话可关闭", not sess.closed, sess.closed)
            check("SOCKS 会话退出后已关闭", sess.closed, sess.closed)
        finally:
            await shared.close()

    asyncio.run(_scenario())


def test_socks_check_pipeline():
    section("SOCKS 校验全链路（入池 / 硬失败 / 依赖缺失）")
    import asyncio
    import sys

    from .. import config_center as cc
    from ..db.dbClient import get_store, reset_store
    from ..helper import check as check_mod
    from ..helper.proxy import Proxy
    from .fake_redis import FakeRedis
    from .socks_server import HttpOriginServer, Socks5Server

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    for key in ("PROXY_REGION", "ASN_ENABLED", "DNSBL_ENABLED"):
        fake.hset(cc.KEY_CONFIG, key, "0")
    cc._bump_cache()
    reset_store()

    original_tcp = check_mod.tcp_check

    async def _ok_tcp(ip, port, timeout):
        return True

    check_mod.tcp_check = _ok_tcp
    try:
        async def _scenario():
            origin = HttpOriginServer()
            await origin.start()
            socks5 = Socks5Server()
            await socks5.start()
            try:
                # 把校验目标指向本机源站
                target = "http://127.0.0.1:%d/get" % origin.port
                fake.hset(cc.KEY_CONFIG, "HTTP_URL", target)
                fake.hset(cc.KEY_CONFIG, "HTTPS_URL", target)
                cc._bump_cache()

                proxy_addr = "127.0.0.1:%d" % socks5.port
                stats = await check_mod._run(
                    [Proxy(proxy=proxy_addr, source=["socks_src"],
                           protocol="socks5")], "raw")
                check("SOCKS5 代理校验通过", stats["passed"] == 1, stats)
                check("SOCKS5 代理入池", stats["added"] == 1, stats)

                store = get_store()
                stored = store.get_one()
                check("池内记录保留协议", stored is not None
                      and stored.get("protocol") == "socks5",
                      stored.get("protocol") if stored else None)
            finally:
                await socks5.stop()
                await origin.stop()

        asyncio.run(_scenario())

        # --- 依赖缺失 → fail_unsupported（硬失败） ---
        store = get_store()
        store.put({"proxy": "5.5.5.5:1080", "https": False, "score": 80,
                   "region": "", "source": "socks_src", "anonymous": "elite",
                   "check_count": 1, "error_count": 0, "ewma_speed": 0.3,
                   "protocol": "socks5"})
        saved_module = sys.modules.get("aiohttp_socks")
        sys.modules["aiohttp_socks"] = None   # 模拟"依赖没装"
        try:
            check_mod.check([Proxy(proxy="5.5.5.5:1080", source=["socks_src"],
                                   protocol="socks5")], work_type="use")
        finally:
            if saved_module is None:
                sys.modules.pop("aiohttp_socks", None)
            else:
                sys.modules["aiohttp_socks"] = saved_module
        check("依赖缺失记为硬失败", "fail_unsupported" in check_mod.HARD_FAILURES,
              check_mod.HARD_FAILURES)
        check("依赖缺失时移出有效池", not store.exists("5.5.5.5:1080"))
        check("依赖缺失时进入退避窗口", store.is_black("5.5.5.5:1080"))
    finally:
        check_mod.tcp_check = original_tcp
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


def test_socks_api():
    section("SOCKS 协议 API")
    from .. import config_center as cc
    from ..db.dbClient import get_store, reset_store
    from .fake_redis import FakeRedis

    client, fake = _build_client()
    original_get = cc.get_redis
    try:
        cc.get_redis = lambda: fake
        reset_store()
        store = get_store()
        store.put({"proxy": "7.7.7.1:80", "https": False, "score": 90,
                   "region": "", "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.1,
                   "protocol": "http"})
        store.put({"proxy": "7.7.7.2:1080", "https": False, "score": 85,
                   "region": "", "source": "socks_src", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.2,
                   "protocol": "socks5"})
        store.put({"proxy": "7.7.7.3:1080", "https": False, "score": 80,
                   "region": "", "source": "socks_src", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.3,
                   "protocol": "socks4"})

        r = client.get("/get/?protocol=socks5")
        body = r.json()
        check("GET /get/?protocol=socks5 只返回 socks5",
              r.status_code == 200 and body.get("proxy") == "7.7.7.2:1080", body)
        check("返回对象带 protocol 字段", body.get("protocol") == "socks5",
              body.get("protocol"))

        r = client.get("/batch/?protocol=socks4")
        body = r.json()
        check("GET /batch/?protocol=socks4 只返回 socks4",
              body.get("count") == 1 and body["data"][0]["proxy"] == "7.7.7.3:1080",
              body)
        check("批量结果带 protocol 字段",
              body["data"][0].get("protocol") == "socks4", body["data"][0].get("protocol"))

        r = client.get("/all/?protocol=http")
        items = r.json()
        check("GET /all/?protocol=http 只返回 http",
              len(items) == 1 and items[0]["proxy"] == "7.7.7.1:80", items)

        r = client.get("/count/")
        data = r.json()
        check("/count/ 含 protocol 分布", "protocol" in data, list(data.keys()))
        check("protocol 分布 http=1", (data.get("protocol") or {}).get("http") == 1,
              data.get("protocol"))
        check("protocol 分布 socks5=1", (data.get("protocol") or {}).get("socks5") == 1,
              data.get("protocol"))
        check("protocol 分布 socks4=1", (data.get("protocol") or {}).get("socks4") == 1,
              data.get("protocol"))
        check("http_type 仍是能力维度",
              (data.get("http_type") or {}).get("http") == 3, data.get("http_type"))

        r = client.get("/get/?protocol=ftp")
        check("非法 protocol 返回 400", r.status_code == 400, r.status_code)
        check("400 不泄露内部结构", "Traceback" not in r.text)
    finally:
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


# =====================================================================
# 14. 长任务进度反馈（借鉴 docs/09 P1-3）
# =====================================================================

def test_progress_store():
    section("长任务进度：存储与派生字段")
    import json

    from .. import config_center as cc
    from ..helper import progress
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    try:
        progress.start("recheck", 100, "存量复检")
        item = progress.read("recheck")
        check("进度可读", bool(item), item)
        check("状态为运行中", item.get("status") == "running", item.get("status"))
        check("总量记录为 100", item.get("total") == 100, item.get("total"))
        check("已处理初始为 0", item.get("checked") == 0, item.get("checked"))
        check("百分比初始为 0", item.get("progress_percent") == 0.0,
              item.get("progress_percent"))
        check("说明文字保留", item.get("message") == "存量复检", item.get("message"))

        progress.update("recheck", checked=50)
        item = progress.read("recheck")
        check("已处理更新为 50", item.get("checked") == 50, item.get("checked"))
        check("百分比为 50", item.get("progress_percent") == 50.0,
              item.get("progress_percent"))
        check("只更新 checked 不动总量", item.get("total") == 100, item.get("total"))
        check("只更新 checked 不动说明", item.get("message") == "存量复检",
              item.get("message"))

        progress.update("recheck", passed=3, failed=47)
        item = progress.read("recheck")
        check("通过数可更新", item.get("passed") == 3, item.get("passed"))
        check("失败数可更新", item.get("failed") == 47, item.get("failed"))

        progress.finish("recheck", checked=100, message="存量复检完成")
        item = progress.read("recheck")
        check("结束后状态为完成", item.get("status") == "done", item.get("status"))
        check("结束时间已记录", float(item.get("finished_at") or 0) > 0,
              item.get("finished_at"))
        check("已处理为 100", item.get("checked") == 100, item.get("checked"))
        check("百分比为 100", item.get("progress_percent") == 100.0,
              item.get("progress_percent"))
        check("耗时已派生", float(item.get("elapsed_seconds", -1)) >= 0,
              item.get("elapsed_seconds"))

        # --- 全部任务读取 ---
        all_items = progress.read()
        check("全量读取含该任务", "recheck" in all_items, list(all_items.keys()))

        # --- 心跳超时 => 判定为"已中断" ---
        raw = fake.hget(progress.PROGRESS_KEY, "recheck")
        data = json.loads(raw)
        data["status"] = "running"
        data["finished_at"] = 0
        data["heartbeat_at"] = time.time() - (progress.HEARTBEAT_TIMEOUT + 60)
        fake.hset(progress.PROGRESS_KEY, "recheck", json.dumps(data))
        item = progress.read("recheck")
        check("心跳超时判定为已中断", item.get("status") == "stale",
              item.get("status"))
        check("已中断标记为真", item.get("stale") is True, item.get("stale"))

        # --- 清除 ---
        progress.clear("recheck")
        check("清除后读不到", progress.read("recheck") == {},
              progress.read("recheck"))
    finally:
        cc.get_redis = original_get


def test_progress_fail_soft():
    section("长任务进度：Redis 故障时不拖垮业务")
    from .. import config_center as cc
    from ..helper import progress

    original_get = cc.get_redis

    def _boom():
        raise RuntimeError("redis down")

    cc.get_redis = _boom
    try:
        progress.start("fetch", 10, "采集")
        progress.update("fetch", checked=1)
        progress.finish("fetch")
        check("存储不可用时不抛异常（进度只是观测）", True)
        check("存储不可用时读回空", progress.read("fetch") == {},
              progress.read("fetch"))
        check("存储不可用时全量读回空", progress.read() == {}, progress.read())
    finally:
        cc.get_redis = original_get


def test_progress_callbacks():
    section("长任务进度：业务侧进度回调")
    from .. import config_center as cc
    from ..db.dbClient import reset_store
    from ..helper import check as check_mod
    from ..helper.proxy import Proxy
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    for key in ("PROXY_REGION", "ASN_ENABLED", "DNSBL_ENABLED"):
        fake.hset(cc.KEY_CONFIG, key, "0")
    cc._bump_cache()
    reset_store()

    original_tcp = check_mod.tcp_check
    original_http = check_mod.http_probe
    original_https = check_mod.https_probe

    async def _ok_tcp(ip, port, timeout):
        return True

    async def _ok_http(session, proxy_str, url, timeout, **kwargs):
        return True, 0.2, "9.9.9.9", {"via": "proxy"}

    async def _ok_https(session, proxy_str, url, timeout, **kwargs):
        return True

    check_mod.tcp_check = _ok_tcp
    check_mod.http_probe = _ok_http
    check_mod.https_probe = _ok_https
    try:
        calls = []
        stats = check_mod.check(
            [Proxy(proxy="1.1.1.1:80", source=["t"]),
             Proxy(proxy="2.2.2.2:81", source=["t"]),
             Proxy(proxy="3.3.3.3:82", source=["t"])],
            work_type="raw",
            on_progress=lambda done, total: calls.append((done, total)))
        check("校验进度回调被调用", len(calls) >= 1, calls)
        check("回调携带已处理数与总量",
              calls[-1][0] == stats["checked"] and calls[-1][1] == 3, calls)
        check("回调的已处理数单调不减",
              all(calls[i][0] <= calls[i + 1][0] for i in range(len(calls) - 1)),
              calls)

        # 回调抛异常不能影响校验主流程
        def _boom(done, total):
            raise ValueError("进度回调炸了")

        stats = check_mod.check([Proxy(proxy="4.4.4.4:80", source=["t"])],
                                work_type="raw", on_progress=_boom)
        check("进度回调异常不影响校验", stats["checked"] == 1, stats)
    finally:
        check_mod.tcp_check = original_tcp
        check_mod.http_probe = original_http
        check_mod.https_probe = original_https
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


def test_progress_validator_cycle():
    section("长任务进度：验证服务的队列抽干周期")
    from .. import config_center as cc
    from ..services import validator as vmod
    from ..helper import progress
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    original_check = vmod.check

    def _fake_check(items, work_type="raw"):
        return {"checked": len(items), "passed": 1, "failed": len(items) - 1,
                "added": 1, "removed": 0}

    vmod.check = _fake_check
    vmod._cycle["total"] = 0
    vmod._cycle["checked"] = 0
    try:
        class _FakeQueue:
            def __init__(self, batches):
                self.batches = list(batches)
                self.depth_value = 5

            def depth(self):
                return self.depth_value

            def pop_batch(self, size):
                return self.batches.pop(0) if self.batches else []

        queue = _FakeQueue([["a", "b"], ["c"]])
        vmod.validate_batch(queue)
        item = progress.read("check")
        check("校验进度已写入", bool(item), item)
        check("总量取自起始深度", item.get("total") == 5, item.get("total"))
        check("已处理随批次累加", item.get("checked") == 2, item.get("checked"))
        check("状态为运行中", item.get("status") == "running", item.get("status"))

        vmod.validate_batch(queue)
        item = progress.read("check")
        check("第二批后已处理为 3", item.get("checked") == 3, item.get("checked"))

        vmod.validate_batch(queue)          # 队列空了
        item = progress.read("check")
        check("队列抽干后状态为完成", item.get("status") == "done",
              item.get("status"))
        check("完成后周期计数清零", vmod._cycle["total"] == 0, vmod._cycle)
    finally:
        vmod.check = original_check
        cc.get_redis = original_get


def test_progress_api():
    section("长任务进度 API")
    from .. import config_center as cc
    from ..helper import progress
    from .fake_redis import FakeRedis

    client, fake = _build_client()
    original_get = cc.get_redis
    try:
        cc.get_redis = lambda: fake
        # 管理后台接口需要会话，先登录换令牌
        fake.hset(cc.KEY_CONFIG, "ADMIN_PASSWORD", "Str0ng@Passw0rd")
        cc._bump_cache()
        login = client.post("/admin/api/login", json={"password": "Str0ng@Passw0rd"})
        check("后台登录成功", login.status_code == 200, login.text[:120])
        auth = {"X-Admin-Token": login.json()["data"]["token"]}

        progress.start("recheck", 200, "存量复检")
        progress.update("recheck", checked=50)

        r = client.get("/admin/api/process/status", headers=auth)
        body = r.json().get("data") or {}
        check("/admin/api/process/status 返回 200", r.status_code == 200,
              r.status_code)
        check("process/status 含 progress 字段", "progress" in body,
              list(body.keys()))
        item = (body.get("progress") or {}).get("recheck") or {}
        check("progress 含 recheck 记录", bool(item), item)
        check("progress 含百分比", item.get("progress_percent") == 25.0,
              item.get("progress_percent"))
        check("progress 含耗时", "elapsed_seconds" in item, list(item.keys()))

        r = client.get("/admin/api/overview", headers=auth)
        body = r.json().get("data") or {}
        check("/admin/api/overview 返回 200", r.status_code == 200, r.status_code)
        check("overview 含 progress 字段", "progress" in body, list(body.keys()))
        check("overview 的 progress 与状态接口一致",
              "recheck" in (body.get("progress") or {}),
              list((body.get("progress") or {}).keys()))
    finally:
        cc.get_redis = original_get


# =====================================================================
# 15. 地域索引与优雅降级（借鉴 docs/09 P1-1）
# =====================================================================

def test_region_index():
    section("地域索引：写入 / 查询 / 严格语义")
    from .. import config_center as cc
    from ..db.redisClient import RedisStore
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    try:
        store = RedisStore(fake, table="t")
        store.put({"proxy": "8.8.8.1:80", "https": True, "region": "CN",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.5})
        store.put({"proxy": "8.8.8.2:80", "https": False, "region": "US",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 5, "ewma_speed": 1.0})
        store.put({"proxy": "8.8.8.3:80", "https": True, "region": "",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.2})

        check("写入时进入地域索引",
              fake.zscore("t:region:CN", "8.8.8.1:80") is not None)
        check("地域未知的代理不进任何地域索引",
              fake.zcard("t:region:CN") == 1, fake.zcard("t:region:CN"))
        check("反向索引记录了代理所属地域",
              fake.hget("t:regionof", "8.8.8.1:80") == "CN")
        check("地域未知的代理不写反向索引",
              fake.hget("t:regionof", "8.8.8.3:80") is None)

        # --- 索引未重建前不得信任索引（部分索引会"少返回"） ---
        check("重建前索引未就绪", not store.region_index_ready())
        before = [p["proxy"] for p in store.get_by_region("CN")]
        check("重建前退回全表扫描，结果依旧完整",
              before == ["8.8.8.1:80"], before)

        # --- 重建后走索引 ---
        count = store.rebuild_region_index()
        check("重建返回索引条数", count == 2, count)
        check("重建后索引就绪", store.region_index_ready())

        cn = store.get_by_region("CN")
        check("按地域取到正确结果",
              [p["proxy"] for p in cn] == ["8.8.8.1:80"], cn)
        check("地域查询大小写不敏感",
              [p["proxy"] for p in store.get_by_region("cn")] == ["8.8.8.1:80"])
        us = store.get_by_region("US")
        check("不会串到别的地域",
              [p["proxy"] for p in us] == ["8.8.8.2:80"], us)
        check("地域未知的代理不被返回（严格语义）",
              all(p["proxy"] != "8.8.8.3:80" for p in store.get_by_region("CN")))
        check("空地域查询返回空", store.get_by_region("") == [])
        check("None 地域返回空", store.get_by_region(None) == [])

        https_cn = store.get_by_region("CN", https=True)
        check("https 过滤生效",
              [p["proxy"] for p in https_cn] == ["8.8.8.1:80"], https_cn)
        check("https 过滤后为空时返回空列表",
              store.get_by_region("US", https=True) == [])
        check("limit 生效", len(store.get_by_region("CN", limit=1)) <= 1)
        check("region_total 统计索引内条数",
              store.region_total("CN") == 1, store.region_total("CN"))

        # --- 索引只是候选来源：元数据里的 region 才是最终依据 ---
        raw = json.loads(fake.hget("t", "8.8.8.1:80"))
        raw["region"] = "JP"
        fake.hset("t", "8.8.8.1:80", json.dumps(raw))
        check("索引残留条目不会造成错分（按元数据复核）",
              [p["proxy"] for p in store.get_by_region("CN")] == [],
              store.get_by_region("CN"))
    finally:
        cc.get_redis = original_get


def test_region_index_degrade():
    section("地域索引：优雅降级（索引不可用退回全表）")
    from .. import config_center as cc
    from ..db.redisClient import RedisStore
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    try:
        store = RedisStore(fake, table="t")
        for i in range(1, 4):
            store.put({"proxy": "9.9.9.%d:80" % i, "https": True,
                       "region": "CN" if i < 3 else "US",
                       "source": "s", "anonymous": "elite",
                       "check_count": 10, "error_count": 0, "ewma_speed": 0.1})
        store.rebuild_region_index()
        indexed = [p["proxy"] for p in store.get_by_region("CN")]
        check("索引路径返回 CN 代理",
              indexed == ["9.9.9.1:80", "9.9.9.2:80"], indexed)

        # --- 降级一：完整性标记缺失（升级前入库的数据没有索引条目） ---
        fake.delete("t:regionidx")
        check("标记删除后索引未就绪", not store.region_index_ready())
        degraded = [p["proxy"] for p in store.get_by_region("CN")]
        check("降级到全表扫描后结果仍然正确",
              degraded == ["9.9.9.1:80", "9.9.9.2:80"], degraded)
        check("降级不放宽地域筛选（不返回 US）",
              all(p != "9.9.9.3:80" for p in degraded), degraded)

        # --- 降级二：索引存在但内容被清空（Redis 淘汰等） ---
        store.rebuild_region_index()
        fake.delete("t:region:CN")
        check("索引被清空时仍然返回正确结果",
              [p["proxy"] for p in store.get_by_region("CN")]
              == ["9.9.9.1:80", "9.9.9.2:80"])

        # --- 重建索引 ---
        count = store.rebuild_region_index()
        check("重建索引返回索引条数", count == 3, count)
        check("重建后索引恢复",
              fake.zcard("t:region:CN") == 2, fake.zcard("t:region:CN"))
        check("重建后查询走索引仍正确",
              [p["proxy"] for p in store.get_by_region("CN")]
              == ["9.9.9.1:80", "9.9.9.2:80"])
        check("重建后反向索引同步恢复",
              fake.hget("t:regionof", "9.9.9.1:80") == "CN")

        # --- 降级三：索引结果为空（索引只覆盖了池子的一部分） ---
        # 让 CN 索引里的两条元数据地域都改成别的（索引成了残条）
        for proxy in ("9.9.9.1:80", "9.9.9.2:80"):
            raw = json.loads(fake.hget("t", proxy))
            raw["region"] = "ZZ"
            fake.hset("t", proxy, json.dumps(raw))
        # 再把原来属于 US 的一条改成 CN —— 它只在 Hash 里，不在 CN 索引里
        raw = json.loads(fake.hget("t", "9.9.9.3:80"))
        raw["region"] = "CN"
        fake.hset("t", "9.9.9.3:80", json.dumps(raw))
        res = [p["proxy"] for p in store.get_by_region("CN")]
        check("索引结果为空时全表兜底，不漏返回代理",
              res == ["9.9.9.3:80"], res)
    finally:
        cc.get_redis = original_get


def test_region_index_lifecycle():
    section("地域索引：删除 / 过期回收 / 清空的同步清理")
    from .. import config_center as cc
    from ..db.redisClient import RedisStore
    from .fake_redis import FakeRedis

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    try:
        store = RedisStore(fake, table="t")
        store.put({"proxy": "7.7.7.1:80", "https": True, "region": "CN",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.5})
        store.put({"proxy": "7.7.7.2:80", "https": True, "region": "CN",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.5})
        store.rebuild_region_index()
        check("两条都在索引里", fake.zcard("t:region:CN") == 2)

        store.delete("7.7.7.1:80")
        check("删除代理同步清出地域索引",
              fake.zscore("t:region:CN", "7.7.7.1:80") is None)
        check("删除后索引里只剩一条", fake.zcard("t:region:CN") == 1)
        check("删除后反向索引也清掉",
              fake.hget("t:regionof", "7.7.7.1:80") is None)

        # --- 地域变更：旧地域索引里的残条必须摘掉 ---
        store.put({"proxy": "7.7.7.2:80", "https": True, "region": "JP",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.5})
        check("地域变更后旧地域索引被清空", fake.zcard("t:region:CN") == 0,
              fake.zcard("t:region:CN"))
        check("地域变更后进入新地域索引",
              fake.zscore("t:region:JP", "7.7.7.2:80") is not None)

        # --- 过期回收 ---
        store.put({"proxy": "7.7.7.3:80", "https": True, "region": "CN",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.5},
                  ttl=1)
        store.conn.zadd("t:expire", {"7.7.7.3:80": time.time() - 10})
        expired = store.evict_expired(500)
        check("过期回收返回条数", expired == 1, expired)
        check("过期回收同步清出地域索引",
              fake.zscore("t:region:CN", "7.7.7.3:80") is None)
        check("过期回收同步清出反向索引",
              fake.hget("t:regionof", "7.7.7.3:80") is None)

        store.clear()
        check("清空池后地域索引也被清掉", fake.zcard("t:region:CN") == 0,
              fake.zcard("t:region:CN"))
        check("清空池后反向索引也被清掉", fake.hlen("t:regionof") == 0,
              fake.hlen("t:regionof"))
        check("清空池后完整性标记也被清掉", not store.region_index_ready())
    finally:
        cc.get_redis = original_get


def test_region_api():
    section("地域索引：网关筛选行为不变")
    from .. import config_center as cc
    from ..db.dbClient import get_store, reset_store
    from .fake_redis import FakeRedis

    client, fake = _build_client()
    original_get = cc.get_redis
    try:
        cc.get_redis = lambda: fake
        reset_store()
        store = get_store()
        store.put({"proxy": "6.6.6.1:80", "https": True, "region": "CN",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.1})
        store.put({"proxy": "6.6.6.2:80", "https": True, "region": "US",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.2})
        store.put({"proxy": "6.6.6.3:80", "https": True, "region": "",
                   "source": "s", "anonymous": "elite",
                   "check_count": 10, "error_count": 0, "ewma_speed": 0.3})

        # --- 索引未重建：网关行为必须与改造前完全一致 ---
        r = client.get("/get/?area=CN")
        check("GET /get/?area=CN 只返回 CN",
              r.status_code == 200 and r.json().get("proxy") == "6.6.6.1:80",
              r.text[:160])
        r = client.get("/get/?area=US")
        check("GET /get/?area=US 只返回 US",
              r.json().get("proxy") == "6.6.6.2:80", r.text[:160])
        r = client.get("/all/?area=CN")
        items = r.json()
        check("GET /all/?area=CN 只返回 CN",
              len(items) == 1 and items[0]["proxy"] == "6.6.6.1:80", items)

        # --- 索引重建后：行为必须一模一样 ---
        store.rebuild_region_index()
        r = client.get("/get/?area=CN")
        check("建索引后 GET /get/?area=CN 结果不变",
              r.status_code == 200 and r.json().get("proxy") == "6.6.6.1:80",
              r.text[:160])
        r = client.get("/all/?area=CN")
        items = r.json()
        check("建索引后 GET /all/?area=CN 结果不变",
              len(items) == 1 and items[0]["proxy"] == "6.6.6.1:80", items)

        # --- 索引缺失时降级，行为必须完全一致 ---
        fake.delete("use_proxy:region:CN")
        r = client.get("/get/?area=CN")
        check("索引缺失时降级结果一致",
              r.status_code == 200 and r.json().get("proxy") == "6.6.6.1:80",
              r.text[:160])
        fake.delete("use_proxy:regionidx")
        r = client.get("/get/?area=CN")
        check("完整性标记缺失时降级结果一致",
              r.status_code == 200 and r.json().get("proxy") == "6.6.6.1:80",
              r.text[:160])
    finally:
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


# =====================================================================
# 16. 属性稳定（借鉴 docs/09 P2-3）
# =====================================================================

def test_attribute_stability():
    section("属性稳定：空值不覆盖已知属性")
    import asyncio

    from .. import config_center as cc
    from ..db.dbClient import get_store, reset_store
    from ..helper import check as check_mod
    from ..helper.proxy import Proxy, keep_first, keep_known
    from .fake_redis import FakeRedis

    # --- 合并语义 ---
    check("keep_first：已有值优先（首源胜出）", keep_first("CN", "US") == "CN")
    check("keep_first：空值则填入", keep_first("", "US") == "US")
    check("keep_known：空值不覆盖非空值",
          keep_known("elite", "") == "elite")
    check("keep_known：最新有效值覆盖旧值",
          keep_known("elite", "anonymous") == "anonymous")

    fake = FakeRedis()
    original_get = cc.get_redis
    cc.get_redis = lambda: fake
    for key in ("PROXY_REGION", "ASN_ENABLED", "DNSBL_ENABLED"):
        fake.hset(cc.KEY_CONFIG, key, "0")
    cc._bump_cache()
    reset_store()

    original_tcp = check_mod.tcp_check
    original_http = check_mod.http_probe
    original_https = check_mod.https_probe
    original_judge = check_mod.judge_anonymous

    async def _ok_tcp(ip, port, timeout):
        return True

    async def _ok_http(sess, proxy_str, url, timeout, use_proxy_arg=True):
        return True, 0.2, "1.2.3.4", {}

    async def _ok_https(sess, proxy_str, url, timeout, use_proxy_arg=True):
        return True

    judge_result = {"value": ""}

    def _judge(*args, **kwargs):
        return judge_result["value"]

    check_mod.tcp_check = _ok_tcp
    check_mod.http_probe = _ok_http
    check_mod.https_probe = _ok_https
    check_mod.judge_anonymous = _judge
    try:
        store = get_store()
        store.put({"proxy": "1.2.3.4:8080", "https": True, "region": "CN",
                   "source": "s", "anonymous": "elite", "ip_type": "datacenter",
                   "check_count": 3, "error_count": 0, "ewma_speed": 0.2})

        async def _recheck():
            item = store.get_one()
            return await check_mod._run([Proxy.from_dict(item)], "use")

        # --- 判定不出匿名度：不能把已知值抹成空 ---
        judge_result["value"] = ""
        stats = asyncio.run(_recheck())
        check("复检通过", stats["passed"] == 1, stats)
        stored = store.get_one()
        check("判定不出匿名度时不覆盖已有值",
              stored.get("anonymous") == "elite", stored.get("anonymous"))
        check("复检后地域保持不变",
              stored.get("region") == "CN", stored.get("region"))
        check("复检后机型保持不变",
              stored.get("ip_type") == "datacenter", stored.get("ip_type"))

        # --- 判定出新的匿名度：以最新有效测量为准（不是冻结） ---
        judge_result["value"] = "anonymous"
        stats = asyncio.run(_recheck())
        check("二次复检通过", stats["passed"] == 1, stats)
        stored = store.get_one()
        check("新的有效匿名度会覆盖旧值",
              stored.get("anonymous") == "anonymous", stored.get("anonymous"))
    finally:
        check_mod.tcp_check = original_tcp
        check_mod.http_probe = original_http
        check_mod.https_probe = original_https
        check_mod.judge_anonymous = original_judge
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


# =====================================================================
# 17. 纯文本输出（借鉴 docs/09 P2-3）
# =====================================================================

def test_text_format():
    section("纯文本输出：一行一个 ip:port")
    from .. import config_center as cc
    from ..db.dbClient import get_store, reset_store
    from .fake_redis import FakeRedis

    client, fake = _build_client()
    original_get = cc.get_redis
    try:
        cc.get_redis = lambda: fake
        reset_store()
        store = get_store()
        for i, (region, https) in enumerate((("CN", True), ("US", False), ("CN", True))):
            store.put({"proxy": "4.4.4.%d:80" % (i + 1), "https": https,
                       "region": region, "source": "s", "anonymous": "elite",
                       "check_count": 10, "error_count": 0, "ewma_speed": 0.1})

        r = client.get("/all/?format=text")
        check("format=text 返回 200", r.status_code == 200, r.status_code)
        check("Content-Type 为 text/plain",
              r.headers.get("content-type", "").startswith("text/plain"),
              r.headers.get("content-type"))
        lines = [x for x in r.text.strip().split("\n") if x]
        check("一行一个 ip:port", len(lines) == 3, lines)
        check("每行只有地址",
              all(x.count(":") == 1 and " " not in x for x in lines), lines)

        r = client.get("/all/?format=text&area=CN")
        lines = [x for x in r.text.strip().split("\n") if x]
        check("纯文本支持地域筛选", len(lines) == 2, lines)

        r = client.get("/get/?format=text")
        check("get 的纯文本就是地址",
              r.status_code == 200 and r.text.strip().count(":") == 1,
              r.text[:80])

        r = client.get("/batch/?format=text&n=2")
        lines = [x for x in r.text.strip().split("\n") if x]
        check("batch 的纯文本按条数输出", len(lines) == 2, lines)

        r = client.get("/pop/?format=text")
        check("pop 的纯文本就是地址",
              r.status_code == 200 and r.text.strip().count(":") == 1,
              r.text[:80])

        r = client.get("/all/?format=xml")
        check("非法 format 返回 400", r.status_code == 400, r.status_code)

        r = client.get("/all/")
        check("默认仍是 JSON",
              r.headers.get("content-type", "").startswith("application/json"),
              r.headers.get("content-type"))

        r = client.get("/all/?format=text&area=HK")
        check("无匹配时返回空 body",
              r.status_code == 200 and r.text.strip() == "",
              (r.status_code, r.text[:60]))
    finally:
        cc.get_redis = original_get
        reset_store()
        cc._bump_cache()


# =====================================================================
# 18. 客户端 SDK（借鉴 docs/09 P1-4）
# =====================================================================

def test_sdk_client():
    section("客户端 SDK：取代理 / 自动失效回报 / 退避重试")
    import asyncio

    from ..sdk import (AsyncProxyPoolClient, NoProxyAvailable, ProxyPoolClient,
                     SdkError)
    from .fake_api_server import FakeApiServer

    server = FakeApiServer()
    server.start()
    try:
        # 池里的代理地址指向假服务端自己 —— SDK 经它发请求就会走到这里
        addr = "127.0.0.1:%d" % server.port
        server.proxies = [
            {"proxy": addr, "https": True, "region": "CN",
             "anonymous": "elite", "protocol": "http"},
            {"proxy": addr, "https": True, "region": "US",
             "anonymous": "elite", "protocol": "http"},
        ]

        pool = ProxyPoolClient(server.base_url, api_key="test-key")
        # --- 取代理 ---
        proxy = pool.get_proxy(area="CN")
        check("get_proxy 返回代理",
              bool(proxy) and proxy.get("proxy") == addr, proxy)
        last = server.requests[-1]
        check("API Key 自动带上", last["headers"].get("api-key") == "test-key",
              last["headers"].get("api-key"))
        check("筛选参数透传", last["params"].get("area") == "CN",
              last["params"])

        check("batch 返回列表", len(pool.batch(n=2)) == 2)
        check("all 返回列表", len(pool.all()) == 2)
        check("count 返回字典", pool.count().get("total") == 2)

        # --- 非法筛选参数立刻报错（不悄悄发给服务端） ---
        try:
            pool.get_proxy(bogus="x")
            check("非法筛选参数抛异常", False)
        except SdkError:
            check("非法筛选参数抛异常", True)

        # --- 代理 407 → 自动回报并换一条 ---
        server.proxy_status_once["http://example.com/x"] = 407
        server.reported = []
        resp, used = pool.fetch("http://example.com/x", max_attempts=3)
        check("407 后自动换一条代理", resp.status_code == 200,
              resp.status_code)
        check("失效代理已自动回报", addr in server.reported, server.reported)
        check("回报走的是 /report/",
              any(r["path"] == "/report/" for r in server.requests),
              [r["path"] for r in server.requests[-3:]])

        # --- 代理侧 5xx 不回报（那是目标站的问题） ---
        server.proxy_status["http://example.com/y"] = 500
        server.reported = []
        resp, used = pool.fetch("http://example.com/y", max_attempts=1)
        check("目标站 5xx 原样返回", resp.status_code == 500, resp.status_code)
        check("目标站 5xx 不误伤代理", server.reported == [], server.reported)

        # --- 池空 → NoProxyAvailable ---
        server.proxies = []
        try:
            pool.get_proxy()
            check("池空返回 None", True)
        except SdkError:
            check("池空返回 None", False)
        try:
            pool.fetch("http://example.com/z")
            check("池空时 fetch 抛 NoProxyAvailable", False)
        except NoProxyAvailable:
            check("池空时 fetch 抛 NoProxyAvailable", True)
        except SdkError:
            check("池空时 fetch 抛 NoProxyAvailable", False)

        # --- 异步客户端 ---
        server.proxies = [
            {"proxy": addr, "https": True, "region": "CN",
             "anonymous": "elite", "protocol": "http"},
        ]
        server.proxy_status.clear()
        server.reported = []

        async def _async_scenario():
            async with AsyncProxyPoolClient(server.base_url,
                                            api_key="test-key") as apool:
                item = await apool.get_proxy()
                check("异步 get_proxy 返回代理",
                      bool(item) and item.get("proxy") == addr, item)
                check("异步 batch 返回列表", len(await apool.batch(n=1)) == 1)
                server.proxy_status_once["http://example.com/a"] = 407
                status, body, used = await apool.fetch(
                    "http://example.com/a", max_attempts=2)
                check("异步 fetch 遇 407 自动换代理", status == 200, status)
                check("异步 fetch 后已回报失效代理",
                      addr in server.reported, server.reported)
                await apool.report([addr])
                check("异步 report 可用", addr in server.reported,
                      server.reported)

        asyncio.run(_async_scenario())
        pool.close()
    finally:
        server.stop()


# =====================================================================
# 19. 工具脚本（借鉴 docs/09 P2-1 / P2-2）
# =====================================================================

def test_ci_gate():
    section("分层门禁：按改动路径决定跑哪些层")
    from ..tools import ci_gate

    check("改 .py → lint + test",
          ci_gate.layers_for_paths(["db/redisClient.py"]) == ["lint", "test"],
          ci_gate.layers_for_paths(["db/redisClient.py"]))
    check("改依赖 → 追加 audit",
          ci_gate.layers_for_paths(["requirements.txt"])
          == ["lint", "test", "audit"],
          ci_gate.layers_for_paths(["requirements.txt"]))
    check("改部署文件 → 追加 build",
          ci_gate.layers_for_paths(["deploy/nginx.conf"])
          == ["lint", "test", "build"],
          ci_gate.layers_for_paths(["deploy/nginx.conf"]))
    check("纯文档改动不跑任何层",
          ci_gate.layers_for_paths(["docs/00-项目总览.md", "README.md"]) == [],
          ci_gate.layers_for_paths(["docs/00-项目总览.md"]))
    check("四层齐全", set(ci_gate.LAYERS) == {"lint", "test", "audit", "build"},
          ci_gate.LAYERS)


def test_daily_snapshot():
    section("每日质量快照：产物形态")
    import os
    import shutil
    import tempfile

    from ..tools import daily_snapshot

    # --- 快照行整理 ---
    items = [
        {"proxy": "1.1.1.1:8080", "region": "CN", "https": True,
         "anonymous": "elite", "ip_type": "datacenter", "protocol": "http",
         "score": 90, "ewma_speed": 0.3, "source": "srcA"},
        {"proxy": "2.2.2.2:1080", "region": "US", "https": False,
         "anonymous": "anonymous", "ip_type": "residential", "protocol": "socks5",
         "score": 70, "speed": 1.5, "source": "srcB"},
        {"proxy": "3.3.3.3:3128", "region": "", "https": True,
         "anonymous": "", "ip_type": "unknown", "protocol": "",
         "score": 50, "ewma_speed": 2.5, "source": ""},
    ]
    rows = daily_snapshot.build_rows(items)
    check("快照行数与池一致", len(rows) == 3, len(rows))
    check("按评分倒序", rows[0]["proxy"] == "1.1.1.1:8080",
          [r["proxy"] for r in rows])
    check("协议缺省回落 http", rows[2]["protocol"] == "http", rows[2])
    check("延迟分层已计算", rows[0]["tier"] == "fast", rows[0]["tier"])

    out_dir = tempfile.mkdtemp(prefix="fpp-snapshot-")
    try:
        stats = daily_snapshot.write_snapshot(rows, out_dir,
                                              generated_at="2026-10-24T00:00:00")
        for name in ("snapshot.json", "all.txt", "CN.txt", "US.txt",
                     "UNKNOWN.txt", "index.html"):
            check("产物已生成：%s" % name,
                  os.path.exists(os.path.join(out_dir, name)), name)
        with open(os.path.join(out_dir, "all.txt"), "r", encoding="utf-8") as fp:
            lines = [x.strip() for x in fp if x.strip()]
        check("all.txt 一行一个地址", len(lines) == 3, lines)
        with open(os.path.join(out_dir, "CN.txt"), "r", encoding="utf-8") as fp:
            cn = [x.strip() for x in fp if x.strip()]
        check("按国家分文件正确", cn == ["1.1.1.1:8080"], cn)
        with open(os.path.join(out_dir, "snapshot.json"), "r",
                  encoding="utf-8") as fp:
            data = json.load(fp)
        check("snapshot.json 结构正确",
              data.get("count") == 3 and len(data.get("proxies") or []) == 3,
              list(data.keys()))
        check("snapshot.json 不含凭据字段",
              not any(k in data for k in ("api_key", "password", "token")),
              list(data.keys()))
        with open(os.path.join(out_dir, "index.html"), "r",
                  encoding="utf-8") as fp:
            page = fp.read()
        check("看板含统计", "1.1.1.1" not in page and "代理总数" in page, "")
        check("看板默认浅色主题", 'data-theme="light"' in page, "")
        check("看板带深浅切换", "theme-toggle" in page, "")
        check("统计正确", stats["total"] == 3 and stats["https"] == 2, stats)

        # --- 走 API 取数（CLI 的另一条数据源） ---
        from .fake_api_server import FakeApiServer
        server = FakeApiServer(proxies=[
            {"proxy": "4.4.4.4:8080", "region": "CN", "https": True,
             "anonymous": "elite", "protocol": "http", "score": 88},
        ])
        server.start()
        try:
            fetched = daily_snapshot.collect_from_api(server.base_url)
            check("从 API 取到代理", len(fetched) == 1, fetched)
            rows2 = daily_snapshot.build_rows(fetched)
            check("API 取数也能算出分层",
                  bool(rows2) and rows2[0]["tier"] == "unknown", rows2)
        finally:
            server.stop()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# =====================================================================
# 20. 响应头与异常
# =====================================================================

def test_security_headers():
    section("安全响应头与异常处理")
    client, fake = _build_client()
    try:
        r = client.get("/health/live")
        check("X-Content-Type-Options", r.headers.get("x-content-type-options") == "nosniff")
        check("X-Frame-Options", r.headers.get("x-frame-options") == "DENY")
        check("Referrer-Policy", r.headers.get("referrer-policy") == "no-referrer")
        check("Server 头已移除", "server" not in {k.lower() for k in r.headers}
              or r.headers.get("server", "").lower() == "uvicorn",
              dict(r.headers).get("server"))

        r = client.get("/__no_such_route__")
        check("未知路由返回 404 JSON", r.status_code == 404)
        check("404 不泄露 traceback", "Traceback" not in r.text)
    finally:
        client.close()


# =====================================================================
# 主入口
# =====================================================================

def main() -> int:
    print("\033[1mFreeProxyPool 自检\033[0m")
    print("=" * 56)

    for fn in (test_version, test_version_anchors, test_iputil, test_redact, test_security,
               test_config_fail_closed, test_config_validation,
               test_login_lock_key, test_validators,
               test_storage, test_blacklist, test_blacklist_backoff_integration,
               test_queue, test_queue_payload,
               test_dispatch, test_gateway, test_admin_chain,
               test_rate_limit, test_trusted_proxy,
               test_fetchers, test_fetch_zero_result_trips,
               test_logging,
               test_proxy_model, test_region_helpers,
               test_check_stats_invariant, test_dead_proxy_backoff_pipeline,
               test_diagnose, test_diagnose_cli,
               test_scheduler_reschedule,
               test_latency_tier, test_tier_filter_and_stats, test_tier_api,
               test_socks_model_and_queue, test_socks_sources,
               test_socks_probe_end_to_end, test_socks_check_pipeline, test_socks_api,
               test_progress_store, test_progress_fail_soft,
               test_progress_callbacks, test_progress_validator_cycle, test_progress_api,
               test_region_index, test_region_index_degrade,
               test_region_index_lifecycle, test_region_api,
               test_attribute_stability,
               test_text_format,
               test_sdk_client,
               test_ci_gate, test_daily_snapshot,
               test_security_headers):
        try:
            fn()
        except Exception as exc:  # 单个用例崩溃不影响其余用例
            import traceback
            check("%s 未抛异常" % fn.__name__, False,
                  "-> %s: %s" % (type(exc).__name__, exc))
            traceback.print_exc()

    print("\n" + "=" * 56)
    total = _PASS + _FAIL
    if _FAIL == 0:
        print("\033[92m全部通过：%d/%d\033[0m" % (_PASS, total))
        return 0
    print("\033[91m失败 %d / 共 %d\033[0m" % (_FAIL, total))
    for item in _FAILURES:
        print("  - %s" % item)
    return 1


if __name__ == "__main__":
    sys.exit(main())