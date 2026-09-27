"""诊断 SOCKS5 代理握手：看清服务器到底支持什么、为什么拒绝。

用法：python scripts/_socks_probe.py [user] [pwd]
"""
import socket
import sys

HOST = "43.213.7.34"
PORT = 1080

CODES = {0: "成功", 1: "一般失败", 2: "规则不允许", 3: "网络不可达", 4: "主机不可达",
         5: "连接被拒绝", 6: "TTL 超时", 7: "命令不支持", 8: "地址类型不支持"}


def handshake(user: str = "", pwd: str = "") -> socket.socket | None:
    s = socket.create_connection((HOST, PORT), timeout=12)
    s.sendall(bytes([0x05, 0x02, 0x00, 0x02]))
    rep = s.recv(2)
    if len(rep) < 2:
        raise RuntimeError("握手无响应")
    if rep[1] == 0x02:
        u, p = user.encode(), pwd.encode()
        s.sendall(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
        ar = s.recv(2)
        if len(ar) < 2 or ar[1] != 0x00:
            raise RuntimeError(f"认证失败 {ar.hex()}")
    elif rep[1] != 0x00:
        raise RuntimeError(f"不支持的认证方式 {rep[1]}")
    return s


def connect_by_ip(ip: str, port: int, user: str = "", pwd: str = "") -> str:
    """用 ATYP=IPv4 发起 CONNECT（有些代理只允许 IP，不允许域名）。"""
    try:
        s = handshake(user, pwd)
    except Exception as exc:
        return f"握手失败: {exc}"
    try:
        octets = bytes(int(x) for x in ip.split("."))
        req = bytes([0x05, 0x01, 0x00, 0x01]) + octets + port.to_bytes(2, "big")
        s.sendall(req)
        buf = s.recv(10)
        if not buf:
            return "无响应"
        code = buf[1] if len(buf) > 1 else -1
        return f"{code} {CODES.get(code, '未知')}"
    finally:
        s.close()


def try_http_through(ip: str, port: int, path: str, user: str = "", pwd: str = "") -> str:
    """握手 + CONNECT 之后直接发一个 HTTP 请求，看能不能真的取到数据。"""
    try:
        s = handshake(user, pwd)
    except Exception as exc:
        return f"握手失败: {exc}"
    try:
        octets = bytes(int(x) for x in ip.split("."))
        s.sendall(bytes([0x05, 0x01, 0x00, 0x01]) + octets + port.to_bytes(2, "big"))
        buf = s.recv(10)
        code = buf[1] if len(buf) > 1 else -1
        if code != 0:
            return f"CONNECT 被拒: {code} {CODES.get(code, '')}"
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: ip-api.com\r\nConnection: close\r\n\r\n".encode())
        data = b""
        s.settimeout(12)
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 4000:
                break
        return f"HTTP 成功，前 120 字节: {data[:120]!r}"
    finally:
        s.close()


if __name__ == "__main__":
    user = sys.argv[1] if len(sys.argv) > 1 else "2048507772t"
    pwd = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"user={user!r} pwd={pwd!r}")

    targets = {}
    for name in ("www.baidu.com", "api.binance.com", "ip-api.com", "example.com"):
        try:
            targets[name] = socket.gethostbyname(name)
        except Exception as exc:
            print(f"本地 DNS 解析 {name} 失败: {exc}")
    print("解析结果:", targets)

    for name, ip in targets.items():
        for port in (443, 80):
            r = connect_by_ip(ip, port, user, pwd)
            print(f"CONNECT {name}({ip}):{port} -> {r}")
