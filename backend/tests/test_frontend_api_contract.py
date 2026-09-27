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

# 这些路径的写接口必须有请求体，空手调用等于什么都没传
_BODY_REQUIRED_PATHS = {
    "/system/backfill": "回填：interval / start_date / end_date 必须放在请求体里",
    "/system/proxy/test": "代理测试：proxy_url 必须放在请求体里",
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
        assert second and second[0] in "{[a-zA-Z_$", (
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
