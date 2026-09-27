"""本地 SOCKS5 中继：让「代理到底通不通」这件事能在离线环境里被验证。

它做两件事：
1. 在本地起一个真的 SOCKS5 服务（带用户名/密码认证，失败与否可配置）；
2. 把 CONNECT 请求转发到真实目标地址。

测试里用它把请求打到本机的一个 HTTP 服务上，于是整条链路
`httpx -> SOCKS5 握手 -> 中继 -> 目标 HTTP 服务`
都是真的，而不是靠 mock 假装通过。
"""
from __future__ import annotations

import select
import socket
import socketserver
import threading
from typing import Callable

_AUTH_OK = bytes([0x01, 0x00])
_AUTH_FAIL = bytes([0x01, 0x01])


class Socks5Handler(socketserver.BaseRequestHandler):
    """极简 SOCKS5：只实现 NO-AUTH / USERPASS 两种认证 + CONNECT 转发。"""

    expected_user: str = ""
    expected_password: str = ""
    on_connect: Callable[[str, int], None] | None = None

    def handle(self) -> None:  # noqa: C901 —— 协议状态机，拆开反而更难读
        sock = self.request
        try:
            head = sock.recv(262)
            if len(head) < 2 or head[0] != 0x05:
                return
            nmethods = head[1]
            methods = set(head[2:2 + nmethods])
            if self.expected_user or self.expected_password:
                if 0x02 not in methods:
                    sock.sendall(bytes([0x05, 0xFF]))
                    return
                sock.sendall(bytes([0x05, 0x02]))
                auth = sock.recv(513)
                if len(auth) < 3 or auth[0] != 0x01:
                    return
                ulen = auth[1]
                user = auth[2:2 + ulen].decode(errors="ignore")
                plen = auth[2 + ulen]
                pwd = auth[3 + ulen:3 + ulen + plen].decode(errors="ignore")
                if user != self.expected_user or pwd != self.expected_password:
                    sock.sendall(_AUTH_FAIL)
                    return
                sock.sendall(_AUTH_OK)
            else:
                if 0x00 not in methods:
                    sock.sendall(bytes([0x05, 0xFF]))
                    return
                sock.sendall(bytes([0x05, 0x00]))

            req = sock.recv(4)
            if len(req) < 4 or req[0] != 0x05 or req[1] != 0x01:
                self._reply(sock, 0x07)
                return
            atyp = req[3]
            if atyp == 0x01:
                host = socket.inet_ntoa(sock.recv(4))
            elif atyp == 0x03:
                n = sock.recv(1)[0]
                host = sock.recv(n).decode(errors="ignore")
            elif atyp == 0x04:
                host = socket.inet_ntop(socket.AF_INET6, sock.recv(16))
            else:
                self._reply(sock, 0x08)
                return
            port = int.from_bytes(sock.recv(2), "big")

            hook = getattr(type(self), "on_connect", None)
            if hook:
                # 注意不能用 self.on_connect：类上挂的是普通函数，
                # 通过实例访问会变成绑定方法（多传一个 self），这里必须走类属性。
                hook(host, port)

            try:
                upstream = socket.create_connection((host, port), timeout=5)
            except Exception:
                self._reply(sock, 0x05)  # 连接被目标拒绝
                return
            bind_ip, bind_port = upstream.getsockname()[:2]
            sock.sendall(bytes([0x05, 0x00, 0x00, 0x01]) +
                         socket.inet_aton(bind_ip) + bind_port.to_bytes(2, "big"))
            self._pump(sock, upstream)
        except Exception:  # noqa: BLE001 —— 中继出错就该静默断开，不该把异常抛进测试
            return

    @staticmethod
    def _reply(sock: socket.socket, code: int) -> None:
        sock.sendall(bytes([0x05, code, 0x00, 0x01]) + socket.inet_aton("0.0.0.0") + (0).to_bytes(2, "big"))

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket) -> None:
        """双向搬运字节，直到任一端关闭。"""
        try:
            while True:
                r, _, _ = select.select([a, b], [], [], 30)
                if not r:
                    break
                done = False
                for src, dst in ((a, b), (b, a)):
                    if src in r:
                        data = src.recv(8192)
                        if not data:
                            done = True
                            break
                        dst.sendall(data)
                if done:
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            for s in (a, b):
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass


class ThreadedSocks5Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], *, user: str = "", password: str = "",
                 on_connect: Callable[[str, int], None] | None = None) -> None:
        class _Handler(Socks5Handler):
            pass

        _Handler.expected_user = user
        _Handler.expected_password = password
        _Handler.on_connect = on_connect
        super().__init__(addr, _Handler)


def start_socks5(*, user: str = "", password: str = "",
                 on_connect: Callable[[str, int], None] | None = None
                 ) -> tuple[ThreadedSocks5Server, int]:
    """起一个随机端口的 SOCKS5 服务，返回 (server, port)。"""
    server = ThreadedSocks5Server(("127.0.0.1", 0), user=user, password=password, on_connect=on_connect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]
