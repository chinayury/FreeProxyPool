# FreeProxyPool

轻量、可分布式扩容的**免费代理 IP 池系统**：自动采集公开免费代理 → 三层递进验证 →
标准化 API 分发，专为爬虫与自有测试提供低成本代理资源。

- **版本**：由根目录 `VERSION` 文件单一来源派生
- **许可**：[AGPL-3.0-or-later](LICENSE)（GNU Affero 通用公共许可证 v3）
- **技术栈**：Python 3.12 · FastAPI · Redis（Hash + ZSet 索引 + List）· aiohttp 异步校验
- **定位**：预期用于个人学习与自有爬虫测试（许可证为 AGPL-3.0，使用期望见文末）

---

## 一、它解决什么问题

免费代理的公开来源普遍存在三个老问题，本项目针对每一点都做了闭环处理：

| 老问题 | 本项目的处理 |
|--------|--------------|
| 采到的代理一半是死的，浪费调用方带宽 | 三层递进验证（TCP → 代理功能 → 多轮复检）+ 客户端**失效回报**接口 |
| 坏源一直拖慢每一轮采集 | 单源熔断（连续失败计数）+ **"解析出 0 条即判失败"** |
| 调用方发现代理失效只能等下一轮复检 | `POST /report/` 立即淘汰并拉黑 |

---

## 二、五分钟上手

```bash
# 1. 准备配置（必填项未设置时 compose 会直接报错，这是刻意设计）
cp .env.example .env
python -c "import secrets;print('API_KEY =', secrets.token_urlsafe(32))"
python -c "import secrets;print('ADMIN_PASSWORD = Fp#'+secrets.token_urlsafe(14))"
# 把上面两行结果填进 .env

# 2. 启动
docker compose up -d --build

# 3. 打开管理后台
open http://localhost:5020/admin
```

不用 Docker 时：

```bash
pip install -e .
export DB_CONN="redis://127.0.0.1:6379/0"
export API_KEY="你的密钥"
export ADMIN_PASSWORD="强口令（至少10位，含3类字符）"

python -m freeproxypool selftest      # 离线自检，不依赖 Redis
python -m freeproxypool collector     # 采集服务
python -m freeproxypool validator     # 验证服务
python -m freeproxypool scheduler     # 调度服务
python -m freeproxypool server        # API 网关
python -m freeproxypool all           # 本地调试：单进程跑全部
```

---

## 三、系统架构

```
                    ┌──────────┐
   客户端 ─────────▶│  Nginx   │  统一入口 / 限流 / 静态资源反代
                    └────┬─────┘
                         │  (可 --scale gateway=N)
                    ┌────▼─────┐        ┌──────────────┐
                    │  gateway │───────▶│ Redis（权威） │◀──── collector
                    └────┬─────┘        │ Hash + ZSet  │◀──── validator
                         │              │ List(队列)   │◀──── scheduler
                    ┌────▼─────┐        └──────┬───────┘
                    │   MySQL  │◀──────────────┘（可选冷存储归档）
                    │  归档审计 │
                    └──────────┘
```

四个进程职责**严格分离、互不重复触发**：

| 进程 | 职责 | 可否多副本 |
|------|------|-----------|
| `collector` | 扫描采集源 → 过滤去重 → 推入待验队列 | 可 |
| `validator` | 消费队列 → 三层验证 → 写入有效池 | 可 |
| `gateway` | API 分发 + 管理后台 + 指标 | 可（推荐 2~4） |
| `scheduler` | 存量复检 / 数据清理 / 统计告警 / 手动触发 | **不可**（会重复执行） |

---

## 四、核心能力

### 采集层
- 38 个采集源（30 个默认启用），声明式注册，新增源只需在 `src/freeproxypool/fetcher/sources/` 放一个 `.py`
- 单源熔断：连续失败达阈值进入冷却，冷却结束自动复位
- 指数退避重试（1s → 2s → 4s，上限 8s）
- 内网/保留地址过滤（覆盖 `0/8`、`10/8`、`100.64/10`、`127/8`、`169.254/16`、`172.16/12`、`192.168/16`、`198.18/15`、`224/4`、`240/4`）
- 三层去重：本轮内存 → 有效池 → 黑名单
- 采集 URL 可在后台覆盖，带**协议白名单 + 内网主机黑名单**双重校验（防 SSRF）

### 验证层
- **第一层** TCP 连通探测，不通直接拉黑
- **第二层** 经代理请求目标，记录延迟、出口 IP、匿名度；再探测 CONNECT 隧道能力
- **第三层** 连续 N 轮全通过才判定有效（N 可配）
- **SOCKS4 / SOCKS5 支持**：按协议分流建立会话，协议与"能否用于 HTTPS 目标"是**两个正交维度**
- 地域解析（GeoIP + Redis 缓存 7 天 + 并发限流 + 双接口容错）
- 评分机制：通过加分、失败扣分，触底淘汰
- 事件循环与会话**全进程复用**，不再每轮重建

### 存储层
- Hash（元数据）+ ZSet 索引（评分 / HTTPS / 过期 / 黑名单 / **地域**）+ List（待验队列）
- `HLEN` + `ZCARD` 实现 **O(1)** 池统计（不再每次请求全量扫描）
- **地域索引** `{table}:region:{CODE}`：`?area=` 只在该地域内取候选；索引不可用时自动退回全表扫描，但**只改取数方式、不改返回什么**
- 代理最长存活时间（TTL）到期自动回收
- 待验队列**长度上限 + 背压裁剪**，验证服务长时间停摆也不会 OOM

### 网关层
- API Key 鉴权（恒定时长比较）
- 按标识 QPS 限流，Redis 故障时降级为**进程内限流**而非无限制
- 三种分发策略：延迟优先 / 随机 / 轮询
- **延迟分层 tier**：`fast` / `medium` / `slow` / `unknown`，可按档筛选、可统计分布
- **传输协议筛选** `protocol=http|socks4|socks5`
- 批量分发、失效回报、精简统计
- 安全响应头 + 全局异常处理（不泄露 traceback）

### 运维层
- `/health`（含 `/health/live` 纯存活探针）
- `/metrics` Prometheus 指标（默认需 API Key）
- 六类内置告警（日志 + Redis 事件 + Webhook 推送，带冷却去重）
- **长任务进度**：采集 / 校验 / 存量复检的进度写 Redis，后台面板显示进度条与百分比
- **单代理诊断 CLI**：`python -m freeproxypool check <ip:port>` 一次给出 TCP / HTTP / HTTPS 分层延迟、出口 IP、匿名度、国家码与最终判定

### 集成层
- `sdk/` 客户端 SDK：同步（`requests`）与异步（`aiohttp`）两个客户端，自动带 API Key、**自动回报失效代理**、指数退避重试
- `examples/scrapy_middleware.py`：Scrapy 下载中间件范例，代理失效即回报
- SDK **不依赖项目内任何模块**，外部调用方只要拷走 `sdk/` 一个目录

---

## 五、API 速查

开启 `API_KEY` 后，所有 `/get/` `/batch/` `/pop/` `/all/` `/count/` `/delete/`
`/report/` `/stats/` `/metrics` 均需携带请求头 `API-KEY: <key>`。

```bash
KEY="你的密钥"

curl -H "API-KEY: $KEY" "http://localhost:5020/get/"
curl -H "API-KEY: $KEY" "http://localhost:5020/get/?type=https&area=CN&strategy=latency"
curl -H "API-KEY: $KEY" "http://localhost:5020/get/?tier=fast"      # 只要最快的
curl -H "API-KEY: $KEY" "http://localhost:5020/get/?protocol=socks5" # 只要 SOCKS5
curl -H "API-KEY: $KEY" "http://localhost:5020/batch/?n=20&tier=medium"
curl -H "API-KEY: $KEY" "http://localhost:5020/all/?format=text"    # 一行一个 ip:port
curl -H "API-KEY: $KEY" "http://localhost:5020/count/"

# 客户端发现代理失效 → 回报，服务端立即剔除并拉黑
curl -X POST -H "API-KEY: $KEY" -H "Content-Type: application/json" \
     -d '{"proxies":["1.2.3.4:8080","5.6.7.8:3128"]}' \
     "http://localhost:5020/report/"

curl "http://localhost:5020/health"
curl "http://localhost:5020/version"
curl -H "API-KEY: $KEY" "http://localhost:5020/metrics"
```

交互式文档：<http://localhost:5020/docs>

### 单代理诊断

```bash
python -m freeproxypool check 1.2.3.4:8080
```

一次输出 TCP / HTTP / HTTPS **分层延迟**、出口 IP、匿名度、国家码与最终判定
（`excellent` / `limited` / `unusable`）。加 `--json` 可直接喂给脚本。

---

## 六、文档索引

| 文档 | 内容 |
|------|------|
| **[01-项目介绍与架构](docs/01-项目介绍与架构.md)** | **项目概述**、核心设计原则、系统架构、四进程职责、模块结构与分层原则、关键技术选型、数据流、Redis 数据结构、采集源 |
| [02-环境部署教程](docs/02-环境部署教程.md) | 环境要求、源码 / Docker 两种安装方式、目录与文件、配置说明、启动方式、部署检查清单、常见问题 |
| [03-功能使用说明](docs/03-功能使用说明.md) | CLI 各子命令与参数、API 接口速查、管理后台、客户端 SDK、Scrapy 集成、每日快照与 CI 门禁工具、已知限制 |
| [04-安全设计说明](docs/04-安全设计说明.md) | 安全架构、fail-closed 语义、API Key 鉴权、限流降级、后台鉴权与 CSRF、登录锁定、可信代理、SSRF 防护、输入校验、凭据脱敏、安全响应头 |
| [05-开发日志](docs/05-开发日志.md) | 版本策略、缺陷修复（现象→根因→解决方案）、功能新增、架构改进、src 布局重组记录与验证结果 |

> 变更日志见根目录 [CHANGELOG.md](CHANGELOG.md)（只追加，不删改历史）。

---

## 七、合规声明

本项目以 [GNU Affero General Public License v3.0（AGPL-3.0-or-later）](LICENSE) 发布，
再使用者须遵守许可证条款：保留版权与许可声明、修改文件需显著标注、分发时随附许可证
副本；**若将本系统作为网络服务对外提供（含修改版），须按许可证第 13 条向服务使用者
提供对应源码**。

在此之上的**使用期望与合规要求**（属项目方的期待与运营合规，不改变许可证授予的
权利）：预期用于个人学习与自有爬虫测试，不鼓励商用售卖、对第三方提供代理服务或
搭建流量中转代理；采集第三方站点时须遵守其 robots 协议、控制请求频率、禁止对源站
压测。使用者所在组织的合规要求比许可证更严格时，以组织的更严格要求为准。

---

## 八、许可与信息

- **开发者**：智维ITMSKY
- **版权**：Copyright (C) 2026 智维ITMSKY
- **官网**：http://www.itmsky.com
- **许可证**：GNU Affero General Public License v3.0 or later（AGPL-3.0-or-later，见 [LICENSE](LICENSE)）
- **SPDX-License-Identifier**：AGPL-3.0-or-later

> 本程序是自由软件：你可以遵照 GNU Affero 通用公共许可证第3版或后续版本的规定，重新分发和/或修改它。
> 本程序旨在广泛共享，但不提供任何担保；甚至不包含适销性和特定用途适用性的默示保证。详情请参阅 GNU Affero General Public License。
> 如果你修改本程序并通过网络向公众提供服务（SaaS、Web、API等在线服务），你必须向该服务的使用者提供对应修改后的完整源代码。
> 你应当随本程序一同收到 GNU Affero General Public License 的副本；如未收到，详见 <https://www.gnu.org/licenses/>。
