# -*- coding: utf-8 -*-
"""对同一个代理地址做「协议 × 目标」矩阵探测。

存在理由：用户拿到的代理往往只写了「socks5，user@ip:port」，页面上不会告诉你
它到底是纯 SOCKS5、还是同一端口同时提供 HTTP CONNECT，也不会告诉你规则集放行哪些目标。
与其让用户猜，不如一次性把矩阵跑出来：

    协议 { socks5, http-connect }  ×  目标 { 各交易所 / 出口IP / 纯 HTTP 站点 }

输出每格的 SOCKS5 拒绝码（2=规则不允许 / 3=网络不可达 / 4=主机不可达 / 5=连接被拒）
或 HTTP CONNECT 的状态码，据此可以直接判定「代理侧限制」还是「目标侧限制」。
"""
from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_TARGETS = [
    ("egress_ip", "api.ipify.org", 443),
    ("egress_ip_http", "api.ipify.org", 80),
    ("binance", "api.binance.com", 443),
    ("binance_vision", "data-api.binance.vision", 443),
    ("coinbase", "api.coinbase.com", 443),
    ("kraken", "api.kraken.com", 443),
    ("gemini", "api.gemini.com", 443),
    ("bitfinex", "api-pub.bitfinex.com", 443),
    ("coingecko", "api.coingecko.com", 443),
    ("example_http", "example.com", 80),
]

# SOCKS5 REP 字段的官方含义，写出来是为了让报告能直接给人看
SOCKS_REP = {
    0: "成功",
    1: "服务器故障",
    2: "规则不允许（ruleset 拒绝——通常是代理侧白名单/套餐限制）",
    3: "网络不可达",
    4: "主机不可达",
    5: "连接被拒",
    6: "TTL 过期",
    7: "命令不支持",
    8: "地址类型不支持",
}


def socks5_connect(host: str, port: int, user: str, pwd: str, target: str, tport: int,
                   timeout: float) -> dict:
    """跑完整的 SOCKS5：握手 -> 认证 -> CONNECT -> 读拒绝码。"""
    started = time.perf_counter()
    out = {"protocol": "socks5", "verdict": None, "detail": None,
           "latency_ms": None, "rep_code": None}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            # greeting：告诉服务端我们支持 0(无认证) 和 2(用户名密码)
            sock.sendall(b"\x05\x02\x00\x02")
            ver, meth = sock.recv(2)
            if ver != 5:
                out["verdict"] = "fail"
                out["detail"] = f"握手失败：返回的 VER={ver}"
                return out
            if meth == 0xFF:
                out["verdict"] = "fail"
                out["detail"] = "握手失败：服务端不接受任何认证方式"
                return out
            if meth == 2:
                ub, pb = user.encode(), pwd.encode()
                sock.sendall(bytes([1, len(ub)]) + ub + bytes([len(pb)]) + pb)
                rep = sock.recv(2)
                if rep != b"\x01\x00":
                    out["verdict"] = "fail"
                    out["detail"] = f"认证失败：服务端返回 {rep.hex()}"
                    return out
            elif meth != 0:
                out["verdict"] = "fail"
                out["detail"] = f"未知的认证方式 {meth}"
                return out

            # CONNECT 请求
            th = target.encode()
            req = b"\x05\x01\x00\x03" + bytes([len(th)]) + th + tport.to_bytes(2, "big")
            sock.sendall(req)
            head = sock.recv(4)
            if len(head) < 4:
                out["verdict"] = "fail"
                out["detail"] = "CONNECT 响应不完整"
                return out
            rep_code = head[1]
            atyp = head[3]
            # 把 BND.ADDR 读完，保持协议合规
            addr_len = {1: 4, 3: sock.recv(1)[0], 4: 16}.get(atyp, 0)
            if addr_len:
                sock.recv(addr_len)
            sock.recv(2)

            out["rep_code"] = rep_code
            out["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            if rep_code == 0:
                out["verdict"] = "ok"
                out["detail"] = "CONNECT 成功"
            else:
                out["verdict"] = "blocked"
                out["detail"] = f"SOCKS5 REP={rep_code}：{SOCKS_REP.get(rep_code, '未知')}"
    except socket.timeout:
        out["verdict"] = "timeout"
        out["detail"] = f"连接代理超时（{timeout}s）——可能代理端口不通或网络被丢包"
    except OSError as exc:
        out["verdict"] = "error"
        out["detail"] = f"{type(exc).__name__}: {exc}"
    out["latency_ms"] = out["latency_ms"] or round((time.perf_counter() - started) * 1000, 1)
    return out


def http_connect(host: str, port: int, user: str, pwd: str, target: str, tport: int,
                 timeout: float) -> dict:
    """同一端口用 HTTP CONNECT 再试一次——有些供应商两种协议共用端口。"""
    started = time.perf_counter()
    out = {"protocol": "http-connect", "verdict": None, "detail": None, "latency_ms": None}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            line = f"CONNECT {target}:{tport} HTTP/1.1\r\nHost: {target}:{tport}\r\n"
            if user:
                token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                line += f"Proxy-Authorization: Basic {token}\r\n"
            line += "\r\n"
            sock.sendall(line.encode())
            resp = b""
            while b"\r\n\r\n" not in resp and len(resp) < 4096:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                resp += chunk
            head = resp.decode("latin-1").split("\r\n")[0] if resp else ""
            out["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            if head.startswith("HTTP/1.") and " 200" in head:
                out["verdict"] = "ok"
                out["detail"] = head
            elif head:
                out["verdict"] = "blocked"
                out["detail"] = head
            else:
                out["verdict"] = "error"
                out["detail"] = "无响应（该端口大概率不是 HTTP 代理）"
    except socket.timeout:
        out["verdict"] = "timeout"
        out["detail"] = f"超时（{timeout}s）"
    except OSError as exc:
        out["verdict"] = "error"
        out["detail"] = f"{type(exc).__name__}: {exc}"
    out["latency_ms"] = out["latency_ms"] or round((time.perf_counter() - started) * 1000, 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="代理可用性矩阵探测")
    ap.add_argument("proxy", nargs="?", default="2048507772t@43.213.7.34:1080")
    ap.add_argument("--password", default="", help="留空时用用户名同时当密码（多数隧道代理如此）")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    raw = args.proxy.strip()
    if "@" in raw:
        userinfo, hp = raw.rsplit("@", 1)
    else:
        userinfo, hp = "", raw
    if ":" in userinfo:
        user, pwd = userinfo.split(":", 1)
    else:
        user, pwd = userinfo, (args.password or userinfo)
    host, _, port_s = hp.partition(":")
    port = int(port_s or 1080)

    print(f"探测目标代理：{host}:{port}  账号={user or '(无)'}  密码={'*' * len(pwd) if pwd else '(无)'}")
    print(f"超时 {args.timeout}s\n")

    jobs = []
    for key, th, tp in DEFAULT_TARGETS:
        jobs.append((key, th, tp, "socks5"))
        jobs.append((key, th, tp, "http-connect"))

    def run(job: tuple) -> dict:
        key, th, tp, proto = job
        fn = socks5_connect if proto == "socks5" else http_connect
        r = fn(host, port, user, pwd, th, tp, args.timeout)
        r.update(key=key, host=th, port=tp)
        return r

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(run, jobs))

    # 打印矩阵
    width = max(len(k) for k, *_ in DEFAULT_TARGETS)
    print(f"{'目标':<{width}}  {'协议':<14} {'结论':<8} {'耗时':>8}  说明")
    print("-" * 100)
    for key, th, tp in DEFAULT_TARGETS:
        for r in results:
            if r["key"] == key:
                mark = {"ok": "OK  ", "blocked": "拒绝", "timeout": "超时",
                        "error": "错误", "fail": "失败"}.get(r["verdict"], r["verdict"])
                print(f"{key:<{width}}  {r['protocol']:<14} {mark:<8} "
                      f"{str(r['latency_ms']) + 'ms':>8}  {r['detail']}")

    allow_ok = [r for r in results if r["verdict"] == "ok"]
    socks_rows = [r for r in results if r["protocol"] == "socks5"]
    summary = {
        "proxy_host": host, "proxy_port": port, "username": user,
        "total": len(results),
        "ok": len(allow_ok),
        "socks5_ok": sum(1 for r in socks_rows if r["verdict"] == "ok"),
        "socks5_blocked_rep": sorted({r["rep_code"] for r in socks_rows
                                      if r["verdict"] == "blocked" and r["rep_code"]}),
        "http_connect_ok": sum(1 for r in results
                               if r["protocol"] == "http-connect" and r["verdict"] == "ok"),
        "reachable_hosts": sorted({r["host"] for r in allow_ok}),
        "results": results,
    }
    print("\n汇总：")
    print(f"  通过 SOCKS5 的目标数：{summary['socks5_ok']}")
    print(f"  通过 HTTP CONNECT 的目标数：{summary['http_connect_ok']}")
    print(f"  SOCKS5 拒绝码分布：{summary['socks5_blocked_rep'] or '—'}"
          f"（2=规则不允许 3=网络不可达 4=主机不可达 5=连接被拒）")
    print(f"  实际放行域名：{summary['reachable_hosts'] or '无'}")

    if not summary["socks5_ok"] and summary["socks5_blocked_rep"] == [2]:
        print("\n结论：握手与认证都通过，但 CONNECT 一律返回 REP=2。"
              "\n      => 账号密码正确、地址正确，是**代理服务端规则集**不允放行。"
              "\n      => 换代理，或让供应商把目标域名加入白名单。本项目代码无需改动。")
    elif summary["socks5_ok"]:
        print("\n结论：有目标可以走通，可以把放行的源启用起来。")

    if args.json:
        print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
