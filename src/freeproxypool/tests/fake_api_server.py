# -*- coding: utf-8 -*-
"""自检用的假 FreeProxyPool 服务端（给 SDK 端到端测试当服务端）。

用标准库 ``http.server`` 起一个线程，只监听 ``127.0.0.1`` 的随机端口，
不产生任何外网流量。

它同时扮演两个角色：

1. **API 服务端** —— 实现 ``/get/`` ``/batch/`` ``/all/`` ``/count/``
   ``/report/``，并记录每次请求（路径、查询参数、请求头、body）供断言；
2. **代理服务端** —— 收到**绝对 URI** 的请求（``GET http://target/... HTTP/1.1``，
   这正是 HTTP 代理收到的样子）时按代理处理，可按目标 URL 模拟
   407 / 5xx 等异常，用来验证 SDK 的"自动失效回报"。

用法::

    server = FakeApiServer()
    server.start()
    # 让池子里的代理地址指向它自己，SDK 经它发请求就会走到这里
    server.proxies = [{"proxy": "127.0.0.1:%d" % server.port}]
    ...
    server.stop()
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


class FakeApiServer:
    """假 FreeProxyPool 服务端（线程内运行）。"""

    def __init__(self, proxies=None):
        #: 池里的代理列表；``/get/`` 按游标轮着发，模拟"每次拿到不同的"
        self.proxies = list(proxies or [])
        self.proxy_cursor = 0
        #: 目标 URL → 状态码；命中则按该状态码应答（模拟代理侧异常）
        self.proxy_status = {}
        #: 目标 URL → 状态码；**只命中一次**（模拟"这一条代理坏了，
        #: 换一条就好了" —— 代理池里两条代理指向同一个地址时，
        #: 只能靠"一次性"来区分先后）
        self.proxy_status_once = {}
        #: 收到的请求记录
        self.requests = []
        #: 被回报的代理地址
        self.reported = []
        self._server = None
        self._thread = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    # ---------- 生命周期 ----------

    def start(self) -> "FakeApiServer":
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                """不往 stderr 刷访问日志。"""

            # ---------- 工具 ----------

            def _record(self, method):
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                entry = {
                    "method": method,
                    "path": parsed.path,
                    "params": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body.decode("utf-8") if body else "",
                    "proxy_request": self.path.startswith("http://"),
                }
                server.requests.append(entry)
                return entry, parsed

            def _send(self, status, payload, content_type="application/json"):
                if isinstance(payload, (bytes, bytearray)):
                    data = bytes(payload)
                else:
                    data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            # ---------- 处理 ----------

            def do_GET(self):
                entry, parsed = self._record("GET")
                if entry["proxy_request"]:
                    # 代理请求：路径是绝对 URI
                    target = self.path
                    once = server.proxy_status_once.pop(target, None)
                    if once:
                        self._send(once, {"code": once, "message": "simulated"})
                        return
                    status = server.proxy_status.get(target)
                    if status:
                        self._send(status, {"code": status, "message": "simulated"})
                    else:
                        self._send(200, b"proxied-ok", content_type="text/plain")
                    return
                path = parsed.path
                if path == "/get/":
                    if not server.proxies:
                        self._send(200, {"code": 0, "message": "暂无可用代理"})
                        return
                    index = server.proxy_cursor % len(server.proxies)
                    server.proxy_cursor += 1
                    self._send(200, server.proxies[index])
                elif path == "/batch/":
                    try:
                        n = int(entry["params"].get("n") or 10)
                    except (TypeError, ValueError):
                        n = 10
                    self._send(200, {"code": 0, "count": len(server.proxies),
                                     "data": server.proxies[:n]})
                elif path == "/all/":
                    self._send(200, server.proxies)
                elif path == "/count/":
                    self._send(200, {"total": len(server.proxies),
                                     "https": len(server.proxies)})
                else:
                    self._send(404, {"code": 404, "message": "not found"})

            def do_POST(self):
                entry, parsed = self._record("POST")
                if parsed.path == "/report/":
                    try:
                        data = json.loads(entry["body"] or "{}")
                    except ValueError:
                        data = {}
                    items = data.get("proxies") or []
                    server.reported.extend(items)
                    self._send(200, {"code": 0, "reported": len(items)})
                else:
                    self._send(404, {"code": 404, "message": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


__all__ = ["FakeApiServer"]
