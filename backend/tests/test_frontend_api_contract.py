"""前端调用后端 API 的契约守卫。

这一组用例来自一个真实缺陷：系统任务页点「开始回填」一直报

    该模块当前不可用 / 请求失败 HTTP 422

根因是 ``core.js`` 里 ``post`` 的签名是 ``post(path, body, params)``，
而回填按钮写成了 ``post('/system/backfill', null, {...payload})`` ——
请求体被放进了**第三个**参数（那其实是拼到 URL 上的 query），
真正发出去的 body 是 ``JSON.stringify(null)``，也就是字符串 ``"null"``。
后端拿 ``null`` 去解析 ``BackfillRequest``，直接 422。

浏览器里没有 JS 测试运行时，所以用 Python 静态扫描把这些用法挡住：
错一次是排障成本，错两次就是工程质量问题了。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JS_ROOT = REPO_ROOT / "frontend" / "static" / "js"

# post('路径', null, {...}) / post(`路径`, undefined, {...}) —— 请求体放错位置
_MISUSED_BODY = re.compile(
    r"""\bpost\(\s*(?P<quote>['"`])(?P<path>[^'"`]+)(?P=quote)\s*,\s*
        (?:null|undefined)\s*,""",
    re.VERBOSE | re.MULTILINE,
)

# 任何一次 post( 调用；after 是紧跟路径之后的分隔符，用来判断有没有第二个参数
_ANY_POST = re.compile(
    r"""\bpost\(\s*(?P<quote>['"`])(?P<path>[^'"`]+)(?P=quote)\s*(?P<after>[,)])""",
    re.VERBOSE,
)


def _strip_comments(text: str) -> str:
    """扫描前先去掉注释。

    这个 bug 的排障过程本身会被写进注释里（「以前写成 post(path, null, {...})」），
    不剔注释的话那句示范性的错误写法会让正则再次命中，测试变成自打嘴巴。
    `//` 前面带冒号的要留下（`http://...` 这种字符串内容），不能一刀切。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", "", text)

# 第二个参数的合法起点：对象/数组字面量、变量名、或函数调用的返回值
_SECOND_ARG_OK = re.compile(r"^(\{|\[|[A-Za-z_$])")

# 这些路径的写接口必须有请求体，空手调用等于什么都没传
_BODY_REQUIRED_PATHS = {
    "/system/backfill": "回填：interval / start_date / end_date 必须放在请求体里",
    "/system/proxy/test": "代理测试：proxy_url 必须放在请求体里",
    "/system/setup/test": "安装引导自检：连接参数必须放在请求体里",
    "/system/setup/complete": "安装引导完成：连接参数必须放在请求体里",
    "/assistant/ask": "问答助手：question 必须放在请求体里",
}


def _js_files() -> list[Path]:
    if not JS_ROOT.is_dir():
        return []
    return sorted(JS_ROOT.glob("*.js"))


@pytest.mark.parametrize("path", _js_files(), ids=lambda p: p.name)
def test_post_body_is_never_passed_as_query(path) -> None:
    """``post`` 的第二个参数是请求体，第三个别才是 query，位置不能错。"""
    text = _strip_comments(path.read_text(encoding="utf-8"))
    bad = list(_MISUSED_BODY.finditer(text))
    for m in bad:
        line_no = text[: m.start()].count("\n") + 1
        pytest.fail(
            f"{path.name}:{line_no} 把 {m.group('path')} 的请求体写成了 null/undefined，"
            "字段被放进了第三个参数（query）。正确写法是 post(path, body, params)"
        )


@pytest.mark.parametrize("path", _js_files(), ids=lambda p: p.name)
def test_body_required_endpoints_send_an_object(path) -> None:
    """已知需要请求体的接口，调用处第二参必须是对象字面量或变量，不能是空的。"""
    text = _strip_comments(path.read_text(encoding="utf-8"))
    for m in _ANY_POST.finditer(text):
        api_path = m.group("path")
        hint = _BODY_REQUIRED_PATHS.get(api_path)
        if not hint:
            continue
        assert m.group("after") == ",", (
            f"{path.name} 里 POST {api_path} 没有传任何参数：{hint}"
        )
        second = text[m.end(): m.end() + 400].lstrip()
        # 允许三种形态：对象/数组字面量、变量名、函数调用的返回值。
        # 早前这里写成 `second[0] in "{[a-zA-Z_$"`，那是**字符串包含**而不是字符类匹配 ——
        # 只有恰好以 a / z / A / Z / { / [ / _ / $ 开头的写法才过得去，
        # 像 post(p, payload()) 这种完全正常的调用会被误判成缺请求体。
        assert second and _SECOND_ARG_OK.match(second), (
            f"{path.name} 里 POST {api_path} 缺少请求体：{hint}"
        )
        assert not second.startswith(("null", "undefined")), (
            f"{path.name} 里 POST {api_path} 的请求体是 null/undefined：{hint}"
        )


def test_core_post_has_misuse_guard() -> None:
    """`core.js` 应当主动把「body 写成 null 还带了 params」的调用挡掉。

    单靠代码 review 抓不住这种事，让它在运行时直接抛错最省事。
    """
    core = (JS_ROOT / "core.js").read_text(encoding="utf-8")
    assert "第二个参数才是请求体" in core or "post(path, body, params)" in core, (
        "core.js 里应保留 post() 的参数位置说明与误用保护"
    )
    assert "Object.keys(params).length" in core, (
        "post() 应检测「body 为空却带了 query 参数」这种误用并抛错"
    )


def test_unwrap_reads_fastapi_detail() -> None:
    """错误提示要能读出框架层的 `detail`，否则用户只能看到「请求失败 HTTP 422」。"""
    core = (JS_ROOT / "core.js").read_text(encoding="utf-8")
    assert "detail" in core, (
        "unwrap() 应同时解析 {error:{message}} 与 FastAPI 标准的 {detail:[...]}"
    )


# ============================================================ 安装引导 / 代理 UI


def test_setup_wizard_is_wired_into_boot_and_routing() -> None:
    """引导页必须在 index.html 里被加载、在 app.js 里被路由，否则永远走不到。"""
    idx = (REPO_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "pages-setup.js" in idx, "index.html 没有加载 pages-setup.js"

    app = (JS_ROOT / "app.js").read_text(encoding="utf-8")
    assert "checkSetupState" in app, "app.js 启动时必须先查一次安装状态"
    assert "window.PSU" in app, "路由表里要把引导页模块也纳入候选（window.PSU）"
    assert "setup-mode" in app, "未安装时应给 body 加 setup-mode，隐藏导航与顶栏状态"


def test_setup_wizard_page_contract() -> None:
    """引导页自身的几条硬约束：三种数据库可选、没测通不能点保存。"""
    page = (JS_ROOT / "pages-setup.js").read_text(encoding="utf-8")
    assert _strip_comments(page)  # 能读到的前提下再谈内容
    for token in ("sqlite", "postgresql", "mysql"):
        assert token in page, f"引导页必须提供 {token} 选项"
    assert "saveBtn.disabled = true" in page, "初始状态必须禁用「保存并完成」"
    assert "lastOk" in page, "保存按钮只能由自检结果解锁"
    assert "/system/setup/test" in page and "/system/setup/complete" in page


def test_proxy_ui_has_scheme_selector_and_per_scheme_fields() -> None:
    """代理设置要像浏览器一样：先选协议，再填主机/端口/账号/密码。"""
    page = _strip_comments((JS_ROOT / "pages-system.js").read_text(encoding="utf-8"))
    for token in ("px-scheme", "px-host", "px-port", "px-user", "px-pass"):
        assert token in page, f"代理区缺少 {token} 输入框"
    for scheme in ("socks5", "http", "https"):
        assert scheme in page, f"代理类型下拉要有 {scheme}"
    # 用户名/密码必须编码，否则带特殊字符的口令会被当成主机分隔符
    assert "encodeURIComponent" in page, "拼接代理地址时必须对用户名/密码做 URL 编码"


def test_proxy_result_shows_who_initiated_the_test() -> None:
    """「到底是谁发起的测试」必须在界面上说清楚，这是这个页面最大的误会来源。"""
    page = _strip_comments((JS_ROOT / "pages-system.js").read_text(encoding="utf-8"))
    assert "initiator" in page, "结果区应展示服务端身份（initiator）"
    assert "服务端" in page
