# -*- coding: utf-8 -*-
"""支持 ``python -m freeproxypool`` 启动命令行入口。

等价于 ``python -m freeproxypool.proxyPool`` 与安装后的 ``freeproxypool`` 命令。
"""
from .proxyPool import cli

if __name__ == "__main__":
    cli()
