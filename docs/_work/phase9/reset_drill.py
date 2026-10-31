"""阶段9彩排：运维复位脚本（逐能力复位 + 复位后复读）。

先在不带鉴权头的情况下探测错误码，再带 X-Role/X-Operator 复位。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18000"
CAPS = ["write:transfer", "write:card", "write:wealth", "write:subscribe", "write:gift", "code_compute"]
SESSIONS = ["drill-injection-1", "drill-injection-2", "drill-injection-3"]
REASON = "阶段9彩排复位"


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    url = BASE + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        return -1, f"TRANSPORT_ERROR: {error!r}"


def show(label: str, status: int, raw: str, limit: int = 400) -> None:
    print(f"--- {label} :: HTTP {status}")
    print(raw[:limit])


def main() -> int:
    # 1) 无鉴权探测
    s, r = call("POST", "/api/safety/circuit-breakers/write:transfer/reset",
                {"reason": REASON, "session_id": "drill-injection-1"})
    show("probe: no headers", s, r)

    # 2) 仅 X-Role，无 X-Operator（预期 operator_required）
    s, r = call("POST", "/api/safety/circuit-breakers/write:transfer/reset",
                {"reason": REASON, "session_id": "drill-injection-1"}, {"X-Role": "ops_admin"})
    show("probe: X-Role only", s, r)

    # 3) 完整鉴权复位
    ok_count = 0
    fail = []
    for sid in SESSIONS:
        for cap in CAPS:
            s, r = call("POST", f"/api/safety/circuit-breakers/{cap}/reset",
                        {"reason": REASON, "session_id": sid},
                        {"X-Role": "ops_admin", "X-Operator": "ops-rehearsal-phase9"})
            ok_flag = s == 200 and '"ok":true' in r
            if ok_flag:
                ok_count += 1
            else:
                fail.append((sid, cap, s, r[:240]))
            print(f"reset {sid} / {cap} -> HTTP {s} ok={ok_flag}")
    print(f"RESET OK COUNT = {ok_count} / 18")
    for item in fail:
        print("FAILED:", item)

    # 4) 复位后复读
    s, r = call("GET", "/health/capabilities")
    print(f"--- health/capabilities AFTER RESET :: HTTP {s}")
    print(r[:1200])
    for sid in SESSIONS:
        s, r = call("GET", f"/api/safety/circuit-breakers?session_id={sid}")
        print(f"--- GET circuit-breakers session={sid} :: HTTP {s}")
        print(r[:900])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
