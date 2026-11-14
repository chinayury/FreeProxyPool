# Changelog

本文件记录 FreeProxyPool 的全部版本变更，**只追加、不删改历史记录**。
每条条目包含：时间、改动类型、受影响文件、变更详情。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

---

## [v0.1.0] - 2026-11-10

首个正式稳定版。相对 v0.2.0 快照完成一次**基线重置**：消除两套 API 实现与两套
调度器并存的行为不一致，清零 58 项缺陷（含 12 项高危安全缺陷），补齐 6 项业务
闭环，建立 710 项离线回归自检与多阶段构建自检。版本号锁定为 `0.1.0`，由根目录
`VERSION` 文件单一来源派生，并设版本锚点常驻门禁。

### 新增

- **失效回报接口** `POST /report/`：客户端发现代理失效可立即回报，服务端剔除并拉黑，
  形成业务闭环（原需等下一轮周期复检）。受影响文件：`gateway/api.py`、`gateway/deps.py`。
- **批量分发接口** `GET /batch/`：一次取 N 个互不重复的代理。受影响文件：`gateway/api.py`。
- **代理地域（region）完整链路**：异步 GeoIP 解析、双接口容错、Redis 缓存 7 天、
  并发限流、地域索引 + 优雅降级。受影响文件：`helper/region.py`、`db/redisClient.py`、`gateway/api.py`。
- **代理存活 TTL 与自动回收**：新增 `:expire` 过期索引，调度器周期回收。受影响文件：`db/redisClient.py`、`services/scheduler.py`。
- **待验队列背压**：`QUEUE_MAX_LEN` 上限 + `LTRIM` 裁剪，验证服务停摆不再 OOM。受影响文件：`db/queue.py`。
- **熔断状态跨进程可见**：熔断状态写入 `fpp:breaker`，后台与指标读到真实数据。受影响文件：`fetcher/baseFetcher.py`、`db/redisClient.py`。
- **可信反向代理 `TRUSTED_PROXIES`**：仅直连来源命中网段时采信 XFF，默认不采信。受影响文件：`gateway/deps.py`、`config_center.py`。
- **离线自检套件（710 项）**：内存版 Redis 上运行，覆盖版本、校验、脱敏、安全、存储、分发、鉴权、采集、tier、SOCKS、地域索引等。受影响文件：`tests/run_selftest.py`、`tests/fake_redis.py`、`tests/socks_server.py`、`tests/fake_api_server.py`。
- **多阶段 Dockerfile 构建自检**：checker 阶段语法检查 + 自检，不通过即构建失败。受影响文件：`Dockerfile`。
- **延迟分层 tier**：`fast`/`medium`/`slow`/`unknown` 作为一等可筛选维度，阈值可配、热生效。受影响文件：`helper/proxy.py`、`gateway/api.py`、`config_center.py`。
- **死代理指数退避**：黑名单从固定时长升级为 `1h→2h→6h→24h` 阶梯退避，反复失败代理不再被反复重试。受影响文件：`db/redisClient.py`、`helper/check.py`。
- **单代理深度诊断 CLI**：`python proxyPool.py check <ip:port>` 输出分层延迟、出口 IP、匿名度、地域与判定。受影响文件：`helper/diagnose.py`、`proxyPool.py`。
- **SOCKS4 / SOCKS5 协议支持**：传输协议字段、校验分流、域名解析策略、网关筛选、新增 8 个 SOCKS 采集源。受影响文件：`fetcher/sources/socks.py`、`helper/check.py`、`db/redisClient.py`；新增依赖 `aiohttp-socks`。
- **长任务进度反馈**：采集/校验/复检进度写 Redis，后台面板显示进度条与中断判定。受影响文件：`helper/progress.py`、`gateway/admin_api.py`、`admin/templates/admin.html`。
- **属性稳定**：空值不覆盖已知属性，匿名度不再因单次探测失败被抹空。受影响文件：`helper/proxy.py`、`helper/check.py`。
- **双轨输出 `format=json|text`**：`/get/` `/batch/` `/pop/` `/all/` 支持纯文本一行一个 `ip:port`。受影响文件：`gateway/api.py`。
- **客户端 SDK + Scrapy 中间件范例**：`sdk/`（同步 + 异步）自动带 Key、自动失效回报、指数退避；SDK 不依赖项目内任何模块。受影响文件：`sdk/_common.py`、`sdk/client.py`、`sdk/async_client.py`、`examples/scrapy_middleware.py`。
- **CI 分层门禁**：`tools/ci_gate.py` 四层（lint/test/audit/build），按改动路径触发，纯文档改动不跑测试。受影响文件：`tools/ci_gate.py`、`.github/workflows/ci.yml`。
- **每日质量快照**：`tools/daily_snapshot.py` 产出快照 JSON / 纯文本清单 / 按国家分文件 / 静态看板。受影响文件：`tools/daily_snapshot.py`。
- **版本锁常驻门禁**：自检 `test_version_anchors` 校验全部版本锚点，漂移即判失败。受影响文件：`tests/run_selftest.py`。
- **参数校验口径补齐**：非法 `anon` 显式 400，不再静默回落成"不过滤"，与 `area`/`tier`/`protocol` 同口径。受影响文件：`gateway/api.py`、`core/validators.py`。
- **`/health/live` 纯存活探针**、`/stats/` 精简统计、`/admin/api/audit` 访问审计视图、`ADMIN_FORCE_PASSWORD_CHANGE` 强制改密、口令强度策略。受影响文件：`gateway/api.py`、`gateway/admin_api.py`、`core/security.py`。

### 变更（重构）

- **删除整套 Flask 实现（约 770 行）**：仅保留 FastAPI 一套 API，消除"容器跑 A、命令行跑 B"的双行为风险与安全修复漏改。受影响文件：删除 `api/proxyApi.py`、`admin/__init__.py`（Flask 版）等。
- **新增 `core/` 公共层（7 模块）**：版本、异常、日志、校验、IP、安全、脱敏，硬性约束不 import 任何业务模块，消除循环导入。受影响文件：`core/version.py`、`core/errors.py`、`core/logging.py`、`core/validators.py`、`core/iputil.py`、`core/security.py`、`core/redact.py`。
- **统一异常信封**：`core/errors.py` 每个异常自带状态码与公开文案，全局处理器不吐 traceback。受影响文件：`core/errors.py`、`gateway/main.py`。
- **服务边界清晰化**：`services/collector.py`（只扫源入队）、`services/validator.py`（只消费验收入池）、`services/scheduler.py`（只复检/清理/统计/响应手动触发），`CHECK_INTERVAL` 真正生效。受影响文件：`services/`。
- **配置中心双语义读取**：`get()`（Redis 故障回落默认，保可用）与 `get_secure()`（Redis 故障抛 503，fail-closed），安全参数用 `secure=True` 声明。受影响文件：`config_center.py`。
- **统一 CLI 入口**：`proxyPool.py` 子命令 `server/collector/validator/scheduler/all/selftest/config/version/check`。受影响文件：`proxyPool.py`。
- **存储层 O(1) 化**：HTTPS 计数由 `HVALS` 全量扫描改为 `ZCARD` 索引；分发候选由全池反序列化改为 `ZREVRANGE` 窗口。受影响文件：`db/redisClient.py`、`gateway/api.py`。
- **管理后台视觉重构**：配色蓝→青绿 teal，全量 emoji→内联 SVG，移除暗色主题；业务逻辑、接口契约、i18n 键、安全链零变更，重构后自检 710/710 全通过。受影响文件：`admin/templates/admin.html`。

### 修复

- **12 项高危安全缺陷**（S-01~S-12）：鉴权 Redis 故障绕过、登录爆破绕过、`ADMIN_ENABLED` 失效、Redis Key 注入、采集源 URL SSRF、存储型 XSS、未登录 400 而非 401、`ttl` 异常 500、`/metrics` 公开、默认弱口令、密码回显、CSRF 密钥耦合。详见 `docs/07-安全审查报告.md`。
- **16 项中危业务缺陷**（B-01~B-16）：`area` 过滤失效、熔断不触发、"抓到 0 条"当成功、队列无背压、手动触发无效、`CHECK_INTERVAL` 失效、校验统计恒空、Prometheus 指标恒 0、代理地址正则放过畸形输入、内网判定两份不一致、`pop/` 不遵守复用限制、IP 复用限制语义失真、`_clear_reuse` 一次 DEL 上万 Key、`/all/` 与 `/get/` 全量扫描、`config/reset` 静默无效、采集源名未校验。详见 `docs/08-开发日志与变更.md`。
- **25 项工程质量修复**（Q-01~Q-25）：日志重复写入、Logger 句柄泄漏、配置文件污染、脱敏语义反推、HTTP 客户端空壳对象、User-Agent 过期、环境代理绕走、重试不检查状态码等。详见 `docs/08-开发日志与变更.md`。
- **验证阶段新发现 4 项**（N-01~N-04）：`get_redis()` 二次调用崩溃、`ssl=` 参数崩溃、RESP3 握手致老 Redis 不可用、import 错名——均为离线自检测不出、上线必触发的缺陷。

### 移除

- **依赖精简**：移除 `Flask`、`gunicorn`、`lxml`、`supervisor`；其余依赖全部加版本上下界。
- **死代码清理**：8+ 死函数、6 个死配置项、1 套可插拔校验器装饰器 API。
- **监控栈部署**：移除 `deploy/prometheus.yml`、`deploy/alert.rules.yml`、`deploy/grafana/*` 及 compose 中的 Prometheus/Grafana 服务。应用侧 `metrics.py` 与 `/metrics` 端点保留，可由调用方自行接入监控。

### 验证

- `python -m compileall -q .` —— 语法与导入通过。
- `python proxyPool.py selftest` —— 710 / 710 通过。
- 真实 Redis 端到端验证：鉴权、地域严格匹配、批量分发、失效回报、指标鉴权等全链路通过。
- 镜像构建 checker 阶段自检不通过即构建失败。
- 版本锚点门禁：部署面锚点与文档声明漂移即判失败。

---

## [工程结构重组] - 2026-11-13

将项目从扁平布局重组为标准 `src` 布局，产出「纯代码 + 文档」初始版本（非 Git 仓库）。
功能与版本号不变（仍为 `0.1.0`），仅工程结构与部署链路调整。

### 变更（重构）

- **标准 src 布局**：全部源码收进 `src/freeproxypool/`，包目录名 = 发行名。新增 `__init__.py`、`__main__.py`（支持 `python -m freeproxypool`）。受影响文件：新增 `src/freeproxypool/__init__.py`、`src/freeproxypool/__main__.py`、`pyproject.toml`。
- **新增打包配置**：`pyproject.toml`（setuptools），`package-dir = {"" = "src"}`，`packages = find(where=["src"])`，package-data 收 `admin/templates/*.html`，console_scripts 入口 `freeproxypool = freeproxypool.proxyPool:cli`。
- **导入转相对导入**：约 240 处内部绝对导入转为相对导入，按文件所在包层级自动计算前缀；`examples/`、`tools/` 补 `__init__.py` 消除命名空间包。
- **路径解析修正**：`core/version.py` 的 VERSION 文件改为有界向上搜索；`gateway/admin_api.py` 管理后台模板改用 `importlib.resources` 定位；`core/logging.py` 日志目录改为环境变量 `FPP_LOG_DIR` 可覆盖。
- **sys.path hack 收敛**：删除 `proxyPool.py`、`gateway/main.py`、`tools/daily_snapshot.py`、`tests/run_selftest.py` 中的 `sys.path.insert` 自举块，统一通过包安装或 `PYTHONPATH` 导入。
- **部署配置同步**：Dockerfile `WORKDIR` 改名 `/build`、命令改为 `python -m freeproxypool`、冒烟检查与 `FPP_LOG_DIR` 同步；docker-compose 4 处 command 与 2 处健康检查同步；`tools/ci_gate.py` 的 `ROOT` 改为向上搜索、`layer_test` 命令与 PYTHONPATH 同步。受影响文件：`Dockerfile`、`docker-compose.yml`、`tools/ci_gate.py`。
- **文档重写**：`docs/` 重写为 5 篇（01-项目介绍与架构、02-环境部署教程、03-功能使用说明、04-安全设计说明、05-开发日志），删除与代码不符的旧文档。`README.md` 重写，文档索引与 `docs/` 对齐。

### 移除

- **Git 元数据与专用文件**：`.git/`、`.gitignore`、`.github/`（非 Git 仓库交付目标不保留）。
- **工具状态与敏感文件**：`.zcode/`、`.env`（真实密钥绝不保留，仅保留 `.env.example` 占位模板）。
- **编译缓存**：全部 `__pycache__/`、`*.pyc`。

### 保留

- **测试**：`tests/` 保留在包内 `src/freeproxypool/tests/`，作为核心质量门禁（对应测试红线），自检套件 710 项不变。
- **部署配置**：Dockerfile、docker-compose.yml、deploy/、deploy.sh 保留（运行部署必需的工程配置）。

### 验证

- `python -m compileall -q src/` —— 语法与导入通过。
- `python -m freeproxypool version` —— 输出 `FreeProxyPool 0.1.0`。
- `python -m freeproxypool --help` —— 子命令齐全。
- `python -m freeproxypool selftest` —— 710 / 710 全部通过。
- 包可导入：`import freeproxypool`、`from freeproxypool.gateway.main import app` 成功。
- 版本锚点一致：`VERSION` 文件、`pyproject.toml`、`core/version.py` 兜底值、docker-compose 镜像标签四处均为 `0.1.0`。

## [v0.1.1] - 2026-11-14

校验目标看护（unavailable ≠ failed）+ 同类项目调研借鉴首轮。无接口变更、无兼容性影响：目标健康时校验行为与历史完全一致，新逻辑仅在"校验目标自身不可达"时接管惩罚语义。

### 修复

- **校验目标故障毒化整池**（中危）：HTTP 校验目标宕机/被墙时，全量代理被误判 `fail_http` 进入退避黑名单、streak 清零、错误率抬升直至淘汰；HTTPS 目标宕机时全池 HTTPS 能力被抹成 False。现引入目标看护：连续 fail_http 达阈值且目标直连不可达时，失败改判 `deferred`（不退避/不清 streak/不计错误率/不淘汰），目标恢复自动重验；HTTPS 能力保值。受影响文件：`src/freeproxypool/helper/check.py`、`src/freeproxypool/helper/proxy.py`、`src/freeproxypool/services/validator.py`、`src/freeproxypool/services/scheduler.py`。
- **docs/01 许可声明过期**：Apache-2.0 修正为 AGPL-3.0-or-later（与 LICENSE/README 对齐）；版本行不再硬编码，引用 `VERSION` 单一来源。

### 新增

- 配置项 `TARGET_GUARD_ENABLED` / `TARGET_GUARD_THRESHOLD` / `TARGET_GUARD_TTL` / `TARGET_GUARD_TIMEOUT`（默认值见 `setting.py`，进入配置中心白名单，管理后台可热更）。
- 校验统计新增 `deferred` 键；不变式升级为 `passed + failed + deferred == checked`，通过率分母剔除 deferred。
- 自检新增 `test_target_guard`（目标健康/疑似故障/恢复/HTTPS 保值四场景），自检总数 710 → 718。
- 新增 `docs/06-调研与借鉴.md`（同类项目调研与借鉴决策记录）。

### 兼容性

- 新增统计键与配置项均为增量；`/get/` `/batch/` 等全部 API 入参出参不变；历史行为仅在目标看护触发时改变（该场景历史上产生误惩罚，属缺陷修复语义）。

### 验证

- `python -m freeproxypool selftest` —— 718 / 718 全部通过。
- 版本锚点一致：`VERSION` / `pyproject.toml` / `core/version.py` / docker-compose 镜像标签（4 处）/ docs 声明均为 `0.1.1`。

## [v0.1.2] - 2026-11-14

管理后台总览页渲染缺陷修复（`var t` 遮蔽 i18n 函数）。无接口变更、无配置变更；唯一行为变化是总览页从此正常渲染。

### 修复

- **管理后台总览面板恒空白**（中危）：`loadOverview()` 渲染回调内 `var t = getElementById('tierStats')` 发生 `var` 提升，遮蔽全局 i18n 函数 `t()`，回调首次调用 `t('stat.pool')` 即抛 `TypeError` 被静默吞掉，统计卡片与五个分布面板全部空白。修改变量名为 `tEl` 并留防回归注释。受影响文件：`src/freeproxypool/admin/templates/admin.html`（此前 0.1.1 的总览页在任何浏览器中均不可用）。
- 验证方式：部署后无头 Chrome + CDP 真实登录截图，总览页全部面板正常渲染。

### 验证

- `python -m freeproxypool selftest` —— 全部通过（用例总数随版本字面量同步更新）。
- 版本锚点一致：`VERSION` / `pyproject.toml` / `core/version.py` / docker-compose 镜像标签（4 处）/ docs 声明均为 `0.1.2`。

