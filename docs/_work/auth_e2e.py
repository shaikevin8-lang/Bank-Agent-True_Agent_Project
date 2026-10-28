"""登录/注册功能端到端验收（HTTP 层）。

前置：数据库已 ``alembic upgrade head``、web 服务已用新代码重启（本脚本会先校验握手版本）。

用法：
    .venv\\Scripts\\python.exe docs\\_work\\auth_e2e.py
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
EXPECTED_VERSION = "2026-10-28.login-auth"
PASSWORD = "secret123"

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """记录一条断言结果。

    Args:
        name: 断言名。
        cond: 是否通过。
        detail: 附加信息（实际值等）。
    """
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name} {detail}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


def call(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict, str]:
    """发一次 HTTP 请求。

    Args:
        path: 以 ``/`` 开头的路径。
        method: HTTP 方法。
        payload: JSON 请求体。
        opener: 带 Cookie 的 opener；缺省用无 Cookie 的默认 opener。

    Returns:
        ``(status, body_dict_or_empty, raw_text)``。
    """
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    send = (opener or urllib.request.build_opener()).open
    try:
        with send(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
            head = resp.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
        head = exc.headers
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {}
    # HTTPMessage 保留服务端的大小写（Starlette 发的是小写 set-cookie），统一转小写键。
    body.setdefault("__headers__", {k.lower(): v for k, v in dict(head).items()})
    return status, body, raw


def new_jar_opener() -> urllib.request.OpenerDirector:
    """构造一个自动保存 Cookie 的 opener。"""
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def main() -> int:
    """跑完整验收流程。"""
    print("=== 1. 版本握手 ===")
    status, live, _ = call("/health/live")
    check("健康检查 200", status == 200, f"-> {status}")
    check("api_version 为新版", live.get("api_version") == EXPECTED_VERSION, f"-> {live.get('api_version')}")
    check("features 含 auth", "auth" in (live.get("features") or []), f"-> {live.get('features')}")

    print("=== 2. 注册 ===")
    user = "e2e_" + time.strftime("%H%M%S")
    status, body, _ = call(
        "/api/auth/register",
        "POST",
        {"username": user, "password": PASSWORD, "confirm_password": PASSWORD},
    )
    check("注册 201", status == 201, f"-> {status} {body.get('message')}")
    check("返回账号名", (body.get("data") or {}).get("user", {}).get("username") == user)
    check("响应不含口令哈希", "password" not in json.dumps(body.get("data") or {}))

    print("=== 3. 注册校验 ===")
    cases = [
        ({"username": user.upper(), "password": PASSWORD, "confirm_password": PASSWORD}, 409, "AUTH_USERNAME_TAKEN"),
        ({"username": "ab", "password": PASSWORD, "confirm_password": PASSWORD}, 422, "AUTH_USERNAME_INVALID"),
        ({"username": "e2e mm", "password": PASSWORD, "confirm_password": PASSWORD}, 422, "AUTH_USERNAME_INVALID"),
        ({"username": "e2e_mis", "password": PASSWORD, "confirm_password": "secret124"}, 422, "AUTH_PASSWORD_MISMATCH"),
        ({"username": "e2e_short", "password": "12345", "confirm_password": "12345"}, 422, "AUTH_PASSWORD_INVALID"),
        ({"username": "", "password": PASSWORD, "confirm_password": PASSWORD}, 422, "AUTH_USERNAME_REQUIRED"),
    ]
    for payload, want_status, want_code in cases:
        status, body, _ = call("/api/auth/register", "POST", payload)
        check(f"{want_code}", status == want_status and body.get("error_code") == want_code, f"-> {status} {body.get('error_code')}")

    status, body, _ = call(
        "/api/auth/register", "POST", {"username": "张三丰", "password": PASSWORD, "confirm_password": PASSWORD}
    )
    check("中文账号名可注册", status in (201, 409), f"-> {status} {body.get('error_code')}")

    print("=== 4. 登录 ===")
    status, body, _ = call("/api/auth/login", "POST", {"username": user, "password": "wrong-password"})
    check("错误密码 401 AUTH_BAD_CREDENTIALS", status == 401 and body.get("error_code") == "AUTH_BAD_CREDENTIALS", f"-> {body.get('message')}")
    status, body, _ = call("/api/auth/login", "POST", {"username": "no_such_user_xyz", "password": "whatever1"})
    check(
        "不存在账号与错误密码文案一致",
        status == 401 and body.get("error_code") == "AUTH_BAD_CREDENTIALS",
        f"-> {body.get('message')}",
    )

    status, body, _ = call("/api/auth/login", "POST", {"username": user, "password": ""})
    check("空密码 422 AUTH_PASSWORD_REQUIRED", status == 422 and body.get("error_code") == "AUTH_PASSWORD_REQUIRED", f"-> {status} {body.get('error_code')}")
    status, body, _ = call("/api/auth/login", "POST", {"username": "", "password": PASSWORD})
    check("空账号 422 AUTH_USERNAME_REQUIRED", status == 422 and body.get("error_code") == "AUTH_USERNAME_REQUIRED", f"-> {status} {body.get('error_code')}")

    opener = new_jar_opener()
    status, body, _ = call("/api/auth/login", "POST", {"username": user, "password": PASSWORD}, opener)
    check("登录 200", status == 200, f"-> {body.get('message')}")
    check("登录返回 user", (body.get("data") or {}).get("user", {}).get("username") == user)
    cookie_raw = (body.get("__headers__") or {}).get("set-cookie", "")
    check("Set-Cookie 含 HttpOnly", "httponly" in cookie_raw.lower(), f"-> {cookie_raw[:60]}")
    check("Set-Cookie 含 SameSite=lax", "samesite=lax" in cookie_raw.lower())
    check("Set-Cookie 名为 bank_agent_session", cookie_raw.startswith("bank_agent_session="))

    print("=== 5. 会话态 ===")
    status, body, _ = call("/api/auth/me", "GET", None, opener)
    check(
        "me 200 且已认证",
        status == 200 and (body.get("data") or {}).get("authenticated") is True,
        f"-> {status} {(body.get('data') or {}).get('user')}",
    )
    status, body, _ = call("/api/auth/me")
    check("无 Cookie 时 me 401 AUTH_UNAUTHORIZED", status == 401 and body.get("error_code") == "AUTH_UNAUTHORIZED", f"-> {status}")

    status, body, _ = call("/api/sessions", "GET", None, opener)
    count = len(((body.get("data") or {}).get("sessions")) or [])
    check("既有业务接口 /api/sessions 仍可用", status == 200 and body.get("ok") is True, f"-> 会话数 {count}")

    print("=== 6. 登出 ===")
    status, body, _ = call("/api/auth/logout", "POST", None, opener)
    check("logout 200", status == 200 and body.get("ok") is True, f"-> {body.get('message')}")
    status, body, _ = call("/api/auth/me", "GET", None, opener)
    check("登出后令牌失效 401", status == 401, f"-> {status}")
    status, body, _ = call("/api/auth/logout", "POST", None, opener)
    check("重复登出仍 200（幂等）", status == 200)

    print("=== 7. 首屏页面 ===")
    status, body, html = call("/")
    check("首页 200", status == 200, f"-> {status}")
    for needle in ('id="authGate"', 'id="authForm"', 'id="authConfirmPassword"', 'id="logoutBtn"', "/api/auth/me", "/api/auth/register", "/api/auth/logout"):
        check(f"页面含 {needle}", needle in html)

    print()
    print(f"=== 结果：PASS {passed} / FAIL {failed} ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
