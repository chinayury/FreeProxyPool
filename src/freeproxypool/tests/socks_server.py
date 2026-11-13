# -*- coding: utf-8 -*-
"""进程内 SOCKS4 / SOCKS5 测试服务器 + 最小 HTTP 源站（自检用）。

为什么不用 mock：SOCKS 探针的难点恰恰在**协议细节**上 ——
握手字节序、地址类型（IPv4 / 域名）、连接器与 ``proxy=`` 参数的冲突、
SOCKS4 与 SOCKS5 在域名解析上的差异。把探针 mock 掉等于把要验证的东西
也一起 mock 掉了，测出来的"全绿"没有意义。

这里用几百行实现**最小可用的** SOCKS 服务器：

* SOCKS5：无认证，``CONNECT`` 命令，支持 IPv4 / 域名 / IPv6 三种地址类型；
* SOCKS4：``CONNECT`` 命令，地址固定为 IPv4（``0.0.0.x`` 表示"后面跟域名"，
  即 SOCKS4a，一并支持）；
* HTTP 源站：返回 ``{"origin": "..."}``，与 ``httpbin.org/get`` 同构，
  便于复用 ``helper.region.parse_origin_ip`` 的解析口径。

全部只监听 ``127.0.0.1`` 的随机端口，用完即关，不产生任何外网流量。
"""
import asyncio
import socket


class HttpOriginServer:
    """最小 HTTP 源站（仿 ``httpbin.org/get``）。"""

    def __init__(self, body: str = None, status: int = 200):
        self.body = body if body is not None else '{"origin": "9.9.9.9"}'
        self.status = status
        self.server = None
        self.port = 0
        self.requests = 0

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None

    async def _handle(self, reader, writer):
        try:
            line = await reader.readline()
            if not line:
                return
            self.requests += 1
            # 读完请求头（到空行为止）
            while True:
                header = await reader.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break
            payload = self.body.encode("utf-8")
            head = (
                "HTTP/1.1 %d OK\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: %d\r\n"
                "Connection: close\r\n\r\n" % (self.status, len(payload))
            ).encode("latin-1")
            writer.write(head + payload)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


class Socks5Server:
    """最小 SOCKS5 服务器（无认证，仅 CONNECT）。"""

    def __init__(self):
        self.server = None
        self.port = 0
        self.connections = 0
        self.hosts = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None

    async def _handle(self, reader, writer):
        remote_writer = None
        try:
            version, nmethods = await reader.readexactly(2)
            if version != 0x05:
                return
            await reader.readexactly(nmethods)
            writer.write(b"\x05\x00")          # VER=5, METHOD=0（无认证）
            await writer.drain()

            version, cmd, _rsv, atyp = await reader.readexactly(4)
            if cmd != 0x01:                    # 只支持 CONNECT
                writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return
            host = await self._read_host(reader, atyp)
            if host is None:
                writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.hosts.append(host)

            try:
                remote_reader, remote_writer = await asyncio.open_connection(host, port)
            except Exception:
                writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return

            # REP=0x00（成功），BND.ADDR 用回环地址占位
            writer.write(b"\x05\x00\x00\x01"
                         + socket.inet_aton("127.0.0.1")
                         + port.to_bytes(2, "big"))
            await writer.drain()
            self.connections += 1

            await asyncio.gather(
                _pipe(reader, remote_writer),
                _pipe(remote_reader, writer),
                return_exceptions=True)
        except Exception:
            pass
        finally:
            for item in (writer, remote_writer):
                if item is not None:
                    try:
                        item.close()
                    except Exception:
                        pass

    @staticmethod
    async def _read_host(reader, atyp: int):
        if atyp == 0x01:
            return socket.inet_ntoa(await reader.readexactly(4))
        if atyp == 0x03:
            length = (await reader.readexactly(1))[0]
            return (await reader.readexactly(length)).decode("idna")
        if atyp == 0x04:
            return socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        return None


class Socks4Server:
    """最小 SOCKS4 服务器（仅 CONNECT）。

    地址字段为 ``0.0.0.x`` 时表示"后面跟域名"（SOCKS4a），一并支持。
    """

    def __init__(self):
        self.server = None
        self.port = 0
        self.connections = 0
        self.hosts = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None

    async def _handle(self, reader, writer):
        remote_writer = None
        try:
            header = await reader.readexactly(4)
            version, cmd = header[0], header[1]
            if version != 0x04 or cmd != 0x01:
                return
            port = int.from_bytes(header[2:4], "big")
            raw_ip = await reader.readexactly(4)
            # USERID（以 \0 结尾）
            while True:
                ch = await reader.readexactly(1)
                if ch == b"\x00":
                    break
            if raw_ip[:3] == b"\x00\x00\x00" and raw_ip[3] != 0:
                # SOCKS4a：域名紧跟在 USERID 之后
                length = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(length)).decode("idna")
            else:
                host = socket.inet_ntoa(raw_ip)
            self.hosts.append(host)

            try:
                remote_reader, remote_writer = await asyncio.open_connection(host, port)
            except Exception:
                writer.write(b"\x00\x5b" + port.to_bytes(2, "big") + raw_ip)
                await writer.drain()
                return

            writer.write(b"\x00\x5a" + port.to_bytes(2, "big") + raw_ip)
            await writer.drain()
            self.connections += 1

            await asyncio.gather(
                _pipe(reader, remote_writer),
                _pipe(remote_reader, writer),
                return_exceptions=True)
        except Exception:
            pass
        finally:
            for item in (writer, remote_writer):
                if item is not None:
                    try:
                        item.close()
                    except Exception:
                        pass


async def _pipe(reader, writer):
    """双向转发。"""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


__all__ = ["HttpOriginServer", "Socks5Server", "Socks4Server"]
