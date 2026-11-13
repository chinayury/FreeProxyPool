# -*- coding: utf-8 -*-
"""FreeProxyPool 命令行入口。

::

    python proxyPool.py server        # 启动 API 网关
    python proxyPool.py scheduler     # 启动调度服务
    python proxyPool.py collector     # 启动采集服务
    python proxyPool.py validator     # 启动验证服务
    python proxyPool.py all           # 单进程跑全部（本地调试用）
    python proxyPool.py selftest      # 不依赖 Redis 的自检
    python proxyPool.py version       # 打印版本
    python proxyPool.py check 1.2.3.4:8080   # 诊断单个代理

v0.1.0 变更：``server`` 子命令不再启动 Flask，而是直接启动 FastAPI 网关。
旧版 ``server`` 起的是被 FastAPI 完全取代的 Flask 应用，
导致"容器里跑 A、命令行跑 B"，两套行为、两套鉴权、修复要改两遍。

v0.1.0 新增：``check`` 子命令 —— 单代理深度诊断（借鉴 docs/09 P1-2）。
回答"这个代理为什么不能用"，输出 TCP / HTTP / HTTPS 分层延迟、出口 IP、
匿名度、地域与最终判定（excellent / limited / unusable）。
"""
import sys

import click

from . import setting
from .core.version import VERSION

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _bootstrap_logging() -> None:
    """统一设置日志级别并挂载 Redis 汇聚。"""
    try:
        from . import config_center as cc
        from .core.logging import attach_redis_sink, set_log_level
        set_log_level(cc.get("LOG_LEVEL", "INFO"))
        attach_redis_sink(cc.KEY_LOGS, max_logs=2000)
    except Exception:
        pass


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(version=VERSION, prog_name="FreeProxyPool")
def cli():
    """FreeProxyPool —— 免费代理 IP 池（采集 / 验证 / 分发）。"""


@cli.command(name="server")
def server_cmd():
    """启动 API 网关（含代理分发、管理后台、健康检查、指标）。"""
    _bootstrap_logging()
    from .gateway.main import main
    main()


@cli.command(name="scheduler")
def scheduler_cmd():
    """启动调度服务（存量复检 / 数据清理 / 统计告警 / 手动触发）。"""
    from .services.scheduler import main
    main()


@cli.command(name="collector")
def collector_cmd():
    """启动采集服务。"""
    from .services.collector import main
    main()


@cli.command(name="validator")
def validator_cmd():
    """启动验证服务。"""
    from .services.validator import main
    main()


@cli.command(name="all")
def all_cmd():
    """单进程运行全部组件（本地调试用，生产请分进程部署）。"""
    import threading

    _bootstrap_logging()

    from .services.collector import main as collector_main
    from .services.scheduler import main as scheduler_main
    from .services.validator import main as validator_main

    threads = [
        threading.Thread(target=collector_main, name="collector", daemon=True),
        threading.Thread(target=validator_main, name="validator", daemon=True),
        threading.Thread(target=scheduler_main, name="scheduler", daemon=True),
    ]
    for thread in threads:
        thread.start()

    from .gateway.main import main as gateway_main
    gateway_main()


@cli.command(name="selftest")
def selftest_cmd():
    """运行内置自检（不依赖 Redis / MySQL）。"""
    from .tests.run_selftest import main as run
    sys.exit(run())


@cli.command(name="version")
def version_cmd():
    """打印版本与运行信息。"""
    click.echo("FreeProxyPool %s" % VERSION)
    click.echo("Python    %s" % sys.version.split()[0])
    click.echo("Platform  %s" % setting.HOST)


@cli.command(name="config")
def config_cmd():
    """打印当前生效的全部配置（密码类只显示是否已设置）。"""
    from . import config_center as cc
    for item in cc.describe():
        if item["type"] == "password":
            value = "<已设置>" if item["has_value"] else "<未设置>"
        else:
            value = str(item["value"])
        click.echo("%-28s %s" % (item["key"], value))


@cli.command(name="check")
@click.argument("proxy")
@click.option("--json", "as_json", is_flag=True,
              help="以 JSON 输出（便于脚本消费）")
@click.option("--http-url", default=None,
              help="HTTP 探针目标（默认取配置 HTTP_URL）")
@click.option("--https-url", default=None,
              help="HTTPS 探针目标（默认取配置 HTTPS_URL）")
@click.option("--timeout", default=10.0, type=float,
              help="单次 HTTP/HTTPS 探针超时（秒）")
@click.option("--protocol", default="http",
              type=click.Choice(["http", "socks4", "socks5"]),
              help="代理传输协议（借鉴 docs/09 P0-1）")
@click.option("--no-region", is_flag=True, help="跳过地域解析")
def check_cmd(proxy, as_json, http_url, https_url, timeout, protocol, no_region):
    """诊断单个代理：输出 TCP / HTTP / HTTPS 分层延迟、匿名度、地域与判定。

    回答"这个代理为什么不能用"——采集入池链路只回答"能不能进池"，
    一旦某个代理被剔除，排查就只能翻 validator 日志了。

    示例::

        python proxyPool.py check 1.2.3.4:8080
        python proxyPool.py check 1.2.3.4:8080 --json
        python proxyPool.py check 1.2.3.4:1080 --protocol socks5
    """
    import json as _json

    from . import config_center as cc
    from .core.errors import FreeProxyPoolError
    from .core.validators import require_proxy
    from .helper.check import origin_probe_url
    from .helper.diagnose import diagnose_sync, format_text

    try:
        proxy_str = require_proxy(proxy)
    except FreeProxyPoolError as exc:
        raise click.ClickException(str(exc))

    http_target = http_url or origin_probe_url(cc.get("HTTP_URL", "http://httpbin.org"))
    https_target = https_url or cc.get("HTTPS_URL", "https://www.qq.com")

    result = diagnose_sync(proxy_str, http_target, https_target,
                            timeout=timeout, with_region=not no_region,
                            protocol=protocol)
    if as_json:
        click.echo(_json.dumps(result, ensure_ascii=False, indent=2))
        return
    click.echo(format_text(result))


if __name__ == "__main__":
    cli()