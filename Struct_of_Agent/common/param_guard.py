"""参数来源校验（L4）：模型不得自己提供「已确认 / 身份 / 角色」类参数。

设计口径（``docs/_work/阶段4-确认凭据与注入五层-接口冻结.md`` §2）：

- **唯一可信来源是服务端**：``confirmed`` / ``mfa_code`` / ``pin`` / ``role`` /
  ``tenant_id`` 等参数只能由工具层闸门或服务端上下文注入；模型在 ``tool_call``
  参数里夹带这些键，一律原地删除并审计。
- **只放行一个例外**：``confirmation_grant_id``（一次性凭据引用）允许透传，但必须
  是合法 UUID 或 43 字符 base64url（32 字节 token），否则按注入处理并删除。
- **值类型白名单**：``str / int / float / bool / None``；出现 ``dict`` / ``list``
  等嵌套值即删除——工具参数是扁平业务字段，嵌套结构最容易藏指令与越权字段。
- **失败不影响主流程**：审计与熔断上报都在旁路 try/except 里，删除动作本身是
  确定的（不因审计失败而保留敏感参数）。
"""

from __future__ import annotations

import re
from typing import Any

#: 模型不得自行提供的参数名（比较时统一小写）。
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "confirmed",
        "mfa_code",
        "mfa",
        "otp",
        "pin",
        "password",
        "passcode",
        "actor_id",
        "tenant_id",
        "user_id",
        "role",
        "scope",
        "idempotency_key",
        "confirmation_grant",
        "grant_id",
    }
)

#: 唯一允许透传的「确认相关」参数（服务端签发的凭据引用）。
ALLOWED_GRANT_KEY = "confirmation_grant_id"

#: 审计动作名（冻结口径）。
AUDIT_PARAM_INJECTION = "security.param_injection_attempt"

#: 熔断器原因码（一次即开启）。
VIOLATION_PARAM_INJECTION = "param_injection"

#: 参数值允许的标量类型。
_ALLOWED_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))

#: 32 字节 token 的 base64url 文本形态（``secrets.token_urlsafe(32)``）。
_GRANT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _grant_id_is_valid(value: Any) -> bool:
    """判断 ``confirmation_grant_id`` 是否是合法凭据引用。

    Args:
        value: 参数值。

    Returns:
        合法（UUID 或 43 字符 base64url）返回 ``True``。
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if _GRANT_TOKEN_RE.match(text):
        return True
    # 延迟导入：uuid 只在需要时解析，避免顶层多一个无用依赖。
    import uuid

    try:
        uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _report(tool_name: str, removed: list[str], *, detail: str) -> None:
    """审计一次参数注入尝试并上报熔断器（失败只影响旁路）。

    Args:
        tool_name: 工具名。
        removed: 被删除的键。
        detail: 判定说明（``sensitive_key`` / ``nested_value`` / ``bad_grant_id``）。
    """
    try:
        from common import confirm, hitl  # 延迟导入：避免 common 包内循环导入

        context = hitl.current_context()
        confirm.audit_quietly(
            context.tenant_id,
            action=AUDIT_PARAM_INJECTION,
            tool_name=tool_name,
            removed_keys=",".join(removed)[:256],
            detail=detail,
            session_id=context.session_id,
        )
    except Exception:  # noqa: BLE001 - 审计旁路失败不得改变删除结论
        pass
    try:
        from common import hitl

        hitl.report_violation(VIOLATION_PARAM_INJECTION, detail=f"{tool_name}:{detail}:{','.join(removed)}"[:300])
    except Exception:  # noqa: BLE001 - 上报失败不得改变删除结论
        pass


def sanitize(tool_name: str, args: dict[str, Any]) -> list[str]:
    """原地删除模型夹带的敏感 / 嵌套 / 非法凭据参数，返回被删键。

    删除是**确定动作**：即使审计或熔断上报失败，敏感键也不会留在参数里。

    Args:
        tool_name: 工具名（审计用）。
        args: 工具参数字典（**原地修改**）。

    Returns:
        被删除的键名列表（空列表表示参数干净）。
    """
    removed: list[str] = []
    reasons: dict[str, str] = {}
    for key in list(args):
        if not isinstance(key, str):
            args.pop(key, None)
            removed.append(str(key))
            reasons.setdefault("non_string_key", str(key))
            continue
        lowered = key.strip().lower()
        value = args.get(key)
        if lowered == ALLOWED_GRANT_KEY:
            if not _grant_id_is_valid(value):
                args.pop(key, None)
                removed.append(key)
                reasons.setdefault("bad_grant_id", key)
            continue
        if lowered in SENSITIVE_KEYS:
            args.pop(key, None)
            removed.append(key)
            reasons.setdefault("sensitive_key", key)
            continue
        if not isinstance(value, _ALLOWED_TYPES):
            args.pop(key, None)
            removed.append(key)
            reasons.setdefault("nested_value", key)
    if removed:
        _report(tool_name, removed, detail=",".join(sorted(reasons)))
    return removed


__all__ = [
    "ALLOWED_GRANT_KEY",
    "AUDIT_PARAM_INJECTION",
    "SENSITIVE_KEYS",
    "VIOLATION_PARAM_INJECTION",
    "sanitize",
]