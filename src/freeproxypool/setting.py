# -*- coding: utf-8 -*-
"""FreeProxyPool 默认配置常量。

所有默认值只在这里出现一次；运行期可被"环境变量 → Redis 配置中心"覆盖，
覆盖规则见 :mod:`config_center`。

版本号不在这里硬编码，统一从根目录 VERSION 文件读取（见 :mod:`core.version`）。
"""
from .core.version import VERSION

__version__ = VERSION

BANNER = r"""
  _____                     ____                       ____            _
 |  ___| __ ___  ___       |  _ \ _ __ _____  ___    _|  _ \ ___   ___ | |
 | |_ | '__/ _ \/ _ \ _____| |_) | '__/ _ \ \/ / | |  | | | / _ \ / _ \| |
 |  _|| | | | | |  __/_____|  __/| | | (_) >  <| |_| |  __/ (_) | (_) | |
 |_|  |_| |_| |_\___|       |_|   |_|  \___/_/\_\\__, |_|   \___/ \___/|_|
                                              |___/
"""

# ============ 服务端 / 网关 ============
HOST = "0.0.0.0"
PORT = 5020
#: 空字符串表示不开启 API 鉴权（仅建议在受控内网使用）
API_KEY = ""
#: 单标识每秒最大请求数，0 = 不限制
RATE_LIMIT_QPS = 50
#: 代理池低于该值时在响应中标记 pool_low 并触发告警
POOL_LOW_THRESHOLD = 50
#: 同一 IP 在该冷却窗口内最多被分发 REUSE_MAX 次
IP_REUSE_WINDOW = 60
#: 单次 /get/ 参与筛选的候选上限（避免大池下每次请求全量排序）
DISPATCH_CANDIDATE_LIMIT = 200
#: /batch/ 单次最多返回条数
BATCH_MAX = 100
#: /all/ 单次最多返回条数
ALL_MAX = 200

# ============ 存储 ============
DB_CONN = "redis://:pwdstring@127.0.0.1:6379/0"
TABLE_NAME = "use_proxy"
#: 代理入池初始分（此前声明但从未被使用，v0.1.0 已接入 put 流程）
PROXY_SCORE_INIT = 0
PROXY_SCORE_MAX = 100
PROXY_SCORE_MIN = 0
PROXY_SCORE_STEP = 1
#: 代理在池内的最长存活时间（秒）。0 = 永不过期，仅靠校验淘汰。
PROXY_TTL = 86400
#: 采集队列最大长度，超出后丢弃最旧的待验代理（防止 validator 停摆导致 OOM）
QUEUE_MAX_LEN = 50000
#: 采集去重集合存活时间（秒）
QUEUE_DEDUP_TTL = 300

# ============ 采集 ============
FETCH_INTERVAL = 5          # 采集周期（分钟）
FETCHER_PAUSED = False
FETCH_TIMEOUT = 15          # 单次请求超时（秒）
FETCH_RETRY = 2             # 失败重试次数
FETCHER_MAX_FAIL = 5        # 单源连续失败熔断阈值
FETCHER_COOLDOWN = 600      # 熔断冷却（秒）
FETCHER_CONCURRENCY = 12    # 并发采集的源数上限
FILTER_INTERNAL = True      # 过滤内网/保留地址

# ============ 校验 ============
CHECK_INTERVAL = 5          # 存量复检周期（分钟）
CHECK_CONCURRENCY = 50      # 异步校验并发
HTTP_URL = "http://httpbin.org"
HTTPS_URL = "https://www.qq.com"
VERIFY_TIMEOUT = 10         # 校验超时（秒）
TCP_CHECK_TIMEOUT = 3       # TCP 连通超时（秒）
CHECK_MULTI_ROUND = 2       # 连续通过次数才判定有效
BLACKLIST_TTL = 3600        # 黑名单存活（秒）
MAX_FAIL_COUNT = 0          # 超过该失败次数直接剔除
PROXY_REGION = True         # 是否解析代理地域
#: 地域解析并发上限
REGION_CONCURRENCY = 10
#: 地域解析结果缓存时长（秒）
REGION_CACHE_TTL = 604800
#: GeoIP mmdb 文件路径（留空表示不使用自托管 GeoIP）
GEOIP_DB = ""
#: 是否启用 GeoIP mmdb 解析
GEOIP_ENABLED = True
#: ASN mmdb 文件路径（留空表示不使用 ASN 推导）
ASN_DB = ""
#: 是否启用 ASN 推导
ASN_ENABLED = True
#: DNSBL zone 列表（逗号分隔字符串）
DNSBL_ZONES = "zen.spamhaus.org,bl.spamcop.net,dnsbl.sorbs.net"
#: 是否启用 DNSBL / IP 声誉过滤
DNSBL_ENABLED = True
#: DNSBL 查询结果缓存时长（秒）
DNSBL_CACHE_TTL = 3600

# ============ v0.1.0 评分模型 (B-01/B-02) ============
#: EWMA 平滑因子（越大越偏向最新样本）
EWMA_ALPHA = 0.3
#: speed history 定长 List 最大长度
SPEED_HISTORY_MAX = 100
#: should_evict 默认最大错误率（超过该比例且检验次数达标即淘汰）
PROXY_MAX_ERROR_RATE = 0.5
#: should_evict 生效前的最低检验次数
PROXY_EVICT_MIN_CHECKS = 10

# ============ 延迟分层（借鉴 Thordata，见 docs/09 P0-3） ============
#: 阈值单位为**毫秒**；内部 speed / ewma_speed 单位为秒，比较时统一换算。
#: 低于 TIER_FAST_MS → fast；低于 TIER_MEDIUM_MS → medium；其余 → slow。
#: 延迟未知（从未通过校验）→ unknown。
TIER_FAST_MS = 500
TIER_MEDIUM_MS = 2000

# ============ 调度 / 运维 ============
CLEAN_INTERVAL = 600        # 数据清理周期（秒）
STAT_INTERVAL = 60          # 统计与告警周期（秒）
REGION_INDEX_REBUILD_INTERVAL = 600  # 地域索引全量重建周期（秒）
ALERT_WEBHOOK = ""          # 钉钉/企微机器人，空则不推送
ALERT_COOLDOWN = 600        # 同类告警冷却（秒）
TIMEZONE = "Asia/Shanghai"

# ============ 管理后台 ============
ADMIN_ENABLED = True
ADMIN_PASSWORD = "changeme_123456"   # 弱默认口令：首登强制修改
ADMIN_SESSION_TTL = 43200
#: 首次登录是否强制跳转修改密码（仅当仍使用默认口令时生效）
ADMIN_FORCE_PASSWORD_CHANGE = True
#: 连续登录失败达到该次数后锁定该来源
LOGIN_MAX_FAIL = 5
LOGIN_FAIL_TTL = 600

# ============ 网关安全 ============
#: 可信反向代理网段。只有直连来源落在这些网段内时，才采信 X-Forwarded-For。
#: 留空表示**完全不采信** XFF —— 这是 v0.1.0 对"伪造 XFF 绕过登录限流"的修复。
TRUSTED_PROXIES = ""
#: /metrics 是否要求携带 API_KEY
METRICS_AUTH = True

# ============ 日志 ============
LOG_LEVEL = "INFO"
LOG_TO_FILE = True