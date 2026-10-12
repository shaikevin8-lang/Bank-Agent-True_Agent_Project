"""R1 智能转账领域工具集（6 个转账工具）。

对应需求 FR-TR-001 ~ FR-TR-004：
    1. smart_transfer          智能转账（通用，账号到账号）
    2. transfer_by_name        按姓名转账
    3. transfer_by_phone       按手机号转账
    4. transfer_by_note        按历史备注转账
    5. schedule_transfer       定时转账
    6. split_aa_collection     拆分 AA 收款

说明：
    - 本阶段不接入真实银行写沙箱，所有工具返回模拟回执（status/accepted 等），
      仅在需要时构造沙箱回执；真实副作用由下游银行写沙箱适配器承接。
    - 金额统一以「分」为最小单位计算，避免浮点误差（PRD 要求金额可精确计算）。
    - 每个工具均使用 Google 风格 docstring 说明契约与接入点。

[沙箱接入点] 当前工具返回的 ``receipt`` 为模拟回执。接入真实银行写沙箱时，
应在各工具的 ``_submit_to_sandbox`` 位置替换为对 ``BankWriteSandboxPort`` 的调用，
并在写前执行权限、风险、确认、幂等与审计安全门。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any

from langchain_core.tools import tool

# 转账工具名集合，供图编排层识别「产生任务回执」的工具
TRANSFER_TOOL_NAMES = {
    "smart_transfer",
    "transfer_by_name",
    "transfer_by_phone",
    "transfer_by_note",
    "schedule_transfer",
    "split_aa_collection",
}

# 单日转账高风险阈值（元），超过按红色风险处理（PRD FR-TR-002 / §8.1）
_HIGH_RISK_AMOUNT_YUAN = Decimal("10000.00")


def _yuan_to_fen(amount: float | Decimal) -> int:
    """将金额（元）精确转换为「分」。

    Args:
        amount: 金额，单位为元。

    Returns:
        金额的分值（整数）。
    """
    return int((Decimal(str(amount)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN))


def _fen_to_yuan(fen: int) -> str:
    """将「分」格式化为两位小数的元字符串。

    Args:
        fen: 金额，单位为分。

    Returns:
        形如 "100.00" 的字符串。
    """
    return f"{Decimal(fen) / Decimal('100'):.2f}"


def _risk_level(amount_fen: int) -> str:
    """依据金额返回风险等级（绿/黄/红）。

    规则（PRD §8.1）：日累计超过 1,0000 元的转账至少为红色，小额写入至少为黄色。
    本工具集仅做单笔金额的初步分级，日累计判断由安全门/风控策略承接。

    Args:
        amount_fen: 金额（分）。

    Returns:
        "green" / "yellow" / "red"。
    """
    if amount_fen >= _yuan_to_fen(_HIGH_RISK_AMOUNT_YUAN):
        return "red"
    return "yellow"


def _mask_phone(phone: str) -> str:
    """脱敏手机号，仅保留前 3 后 4 位。

    Args:
        phone: 原始手机号。

    Returns:
        脱敏后的手机号，如 "138****8000"。
    """
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return "*" * len(phone)
    return f"{digits[:3]}****{digits[-4:]}"


def _mask_account(account: str) -> str:
    """脱敏账号/卡号，仅保留后 4 位。

    Args:
        account: 原始账号或卡号。

    Returns:
        脱敏后的账号，如 "****0001"。
    """
    digits = re.sub(r"\D", "", account)
    if not digits:
        return "****"
    return f"****{digits[-4:]}"


def _new_id(prefix: str) -> str:
    """生成带前缀的唯一业务标识。

    Args:
        prefix: 标识前缀，如 "trf"、"aa"、"sch"。

    Returns:
        形如 "trf-<uuid8>" 的字符串。
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _receipt(
    tool_name: str,
    status: str,
    amount_fen: int,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    """构造统一的任务回执字典。

    该回执作为「事实」返回给 LLM 与前端；成功/失败/草稿状态不可混淆（PRD §4.2）。

    Args:
        tool_name: 触发本次操作的工具名。
        status: 回执状态，取值 accepted / rejected / requested / scheduled / draft。
        amount_fen: 金额（分）。
        message: 面向用户的说明文本。
        **extra: 附加字段（收款人、账号尾号、风险等级、份额等）。

    Returns:
        统一回执字典。
    """
    receipt: dict[str, Any] = {
        "tool": tool_name,
        "status": status,
        "amount_fen": amount_fen,
        "amount_yuan": _fen_to_yuan(amount_fen),
        "currency": "CNY",
        "idempotency_key": _new_id("idem"),
        "risk_level": _risk_level(amount_fen),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "message": message,
    }
    receipt.update(extra)
    return receipt


# [沙箱接入点] 以下工具当前返回模拟回执；接入真实沙箱时在此处替换提交逻辑。
def _submit_to_sandbox(receipt: dict[str, Any]) -> dict[str, Any]:
    """模拟向银行写沙箱提交一次转账请求。

    Args:
        receipt: 已构造的回执字典。

    Returns:
        原样回执（当前为模拟，不产生真实副作用）。
    """
    return receipt


@tool
def smart_transfer(amount: float, from_account: str, to_account: str, currency: str = "CNY") -> dict[str, Any]:
    """执行一笔智能转账（账号到账号的通用转账）。

    当用户明确了金额、付款账户、收款账户与币种时使用本工具。金额必须大于 0，
    首版仅支持 CNY；成功仅在沙箱明确受理后返回脱敏回执。

    Args:
        amount: 转账金额，单位元，必须大于 0。
        from_account: 付款账户（卡号/账号），将在回执中脱敏。
        to_account: 收款账户（卡号/账号），将在回执中脱敏。
        currency: 币种，首版固定 CNY。

    Returns:
        一个字典，包含 status、amount_fen、脱敏账户尾号、风险等级、幂等键等字段。

    Raises:
        ValueError: 金额非正或币种不为 CNY。
    """
    if amount <= 0:
        raise ValueError("转账金额必须大于 0")
    if currency.upper() != "CNY":
        raise ValueError("首版仅支持 CNY")
    amount_fen = _yuan_to_fen(amount)
    receipt = _receipt(
        "smart_transfer",
        "accepted",
        amount_fen,
        "转账已受理（模拟回执）",
        transfer_id=_new_id("trf"),
        from_account_tail=_mask_account(from_account),
        to_account_tail=_mask_account(to_account),
    )
    return _submit_to_sandbox(receipt)


@tool
def transfer_by_name(amount: float, recipient_name: str, from_account: str = "") -> dict[str, Any]:
    """按收款人姓名发起转账。

    根据姓名在本人历史收款人中匹配唯一账户；无候选时说明无法匹配，多候选时需用户选择
    （本工具在框架阶段仅返回模拟匹配结果，实际匹配逻辑由 Mock Core Banking 事实源承接）。

    Args:
        amount: 转账金额，单位元，必须大于 0。
        recipient_name: 收款人姓名。
        from_account: 付款账户（可空，空则使用默认付款账户）。

    Returns:
        一个字典，包含 status、脱敏收款人姓名、匹配账户尾号、风险等级等字段。

    Raises:
        ValueError: 金额非正或收款人姓名为空。
    """
    if amount <= 0:
        raise ValueError("转账金额必须大于 0")
    if not recipient_name or not recipient_name.strip():
        raise ValueError("收款人姓名不能为空")
    amount_fen = _yuan_to_fen(amount)
    receipt = _receipt(
        "transfer_by_name",
        "accepted",
        amount_fen,
        f"已按姓名「{recipient_name}」匹配收款人并发起转账（模拟回执）",
        transfer_id=_new_id("trf"),
        recipient_name=recipient_name,
        recipient_account_tail=_mask_account("6222***0002"),
        from_account_tail=_mask_account(from_account) if from_account else _mask_account("6222***0001"),
    )
    return _submit_to_sandbox(receipt)


@tool
def transfer_by_phone(amount: float, recipient_phone: str, from_account: str = "") -> dict[str, Any]:
    """按收款人手机号发起转账。

    根据手机号匹配本人历史收款人账户，手机号在回执中脱敏（仅保留前 3 后 4 位）。
    无法匹配时返回 rejected 并说明原因（PRD FR-TR-001）。

    Args:
        amount: 转账金额，单位元，必须大于 0。
        recipient_phone: 收款人手机号。
        from_account: 付款账户（可空）。

    Returns:
        一个字典，包含 status、脱敏手机号、匹配账户尾号、风险等级等字段。

    Raises:
        ValueError: 金额非正或手机号非法。
    """
    if amount <= 0:
        raise ValueError("转账金额必须大于 0")
    digits = re.sub(r"\D", "", recipient_phone)
    if len(digits) != 11:
        raise ValueError("手机号格式不正确")
    amount_fen = _yuan_to_fen(amount)
    receipt = _receipt(
        "transfer_by_phone",
        "accepted",
        amount_fen,
        f"已按手机号「{_mask_phone(recipient_phone)}」匹配收款人并发起转账（模拟回执）",
        transfer_id=_new_id("trf"),
        recipient_phone=_mask_phone(recipient_phone),
        recipient_account_tail=_mask_account("6222***0003"),
        from_account_tail=_mask_account(from_account) if from_account else _mask_account("6222***0001"),
    )
    return _submit_to_sandbox(receipt)


@tool
def transfer_by_note(amount: float, note_keyword: str, from_account: str = "") -> dict[str, Any]:
    """按本人历史转账备注发起转账。

    根据历史备注关键字（如「房租」）在本人历史记录中匹配收款人。备注只能查询本人历史记录
    （PRD FR-TR-001），匹配失败时返回 rejected。

    Args:
        amount: 转账金额，单位元，必须大于 0。
        note_keyword: 历史备注关键字。
        from_account: 付款账户（可空）。

    Returns:
        一个字典，包含 status、备注关键字、匹配到的历史收款人、风险等级等字段。

    Raises:
        ValueError: 金额非正或备注关键字为空。
    """
    if amount <= 0:
        raise ValueError("转账金额必须大于 0")
    if not note_keyword or not note_keyword.strip():
        raise ValueError("备注关键字不能为空")
    amount_fen = _yuan_to_fen(amount)
    receipt = _receipt(
        "transfer_by_note",
        "accepted",
        amount_fen,
        f"已按历史备注「{note_keyword}」匹配收款人并发起转账（模拟回执）",
        transfer_id=_new_id("trf"),
        note_keyword=note_keyword,
        matched_recipient="张三",
        recipient_account_tail=_mask_account("6222***0002"),
        from_account_tail=_mask_account(from_account) if from_account else _mask_account("6222***0001"),
    )
    return _submit_to_sandbox(receipt)


@tool
def schedule_transfer(
    amount: float,
    recipient_name: str,
    schedule_time: str,
    from_account: str = "",
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """创建一笔定时转账任务。

    保存日期、时刻与时区，用户可在触发前查看、修改、暂停或取消。触发时须重新检查余额、
    限额、账户、收款人与风险（PRD FR-TR-003）；本工具在框架阶段仅登记任务，不立即执行。

    Args:
        amount: 转账金额，单位元，必须大于 0。
        recipient_name: 收款人姓名。
        schedule_time: 触发时间，形如 "YYYY-MM-DD HH:MM:SS"。
        from_account: 付款账户（可空）。
        timezone: 时区，默认 Asia/Shanghai。

    Returns:
        一个字典，包含 status（scheduled）、任务 ID、触发时间、风险等级等字段。

    Raises:
        ValueError: 金额非正、收款人为空或时间格式不正确。
    """
    if amount <= 0:
        raise ValueError("转账金额必须大于 0")
    if not recipient_name or not recipient_name.strip():
        raise ValueError("收款人姓名不能为空")
    try:
        datetime.fromisoformat(schedule_time)
    except ValueError:
        raise ValueError("schedule_time 需为 ISO 格式，如 2026-10-12 09:00:00")
    amount_fen = _yuan_to_fen(amount)
    receipt = _receipt(
        "schedule_transfer",
        "scheduled",
        amount_fen,
        f"已登记定时转账任务，将于 {schedule_time}({timezone}) 触发（模拟回执）",
        task_id=_new_id("sch"),
        recipient_name=recipient_name,
        schedule_time=schedule_time,
        timezone=timezone,
        from_account_tail=_mask_account(from_account) if from_account else _mask_account("6222***0001"),
    )
    return _submit_to_sandbox(receipt)


@tool
def split_aa_collection(
    total_amount: float,
    participants: list[str],
    payer: str = "",
    split_mode: str = "equal",
    due_date: str = "",
) -> dict[str, Any]:
    """发起一笔 AA 收款请求（拆分收款）。

    支持总金额、参与人、均分/指定分摊与截止日期。金额以分计算，余数与发起人是否承担份额
    需明确；只生成站内请求，不自动扣参与人账户（PRD FR-TR-004）。每位参与人仅见本人份额。

    Args:
        total_amount: 收款总金额，单位元，必须大于 0。
        participants: 参与人名单（字符串列表）。
        payer: 发起人（可空）。
        split_mode: 分摊方式，取值 "equal"（均分）或 "custom"（指定分摊）。
        due_date: 截止日期（可空），形如 "YYYY-MM-DD"。

    Returns:
        一个字典，包含 status（requested）、每位参与人的份额（分）、余数处理、任务 ID 等字段。

    Raises:
        ValueError: 金额非正、参与人不足 2 人或分摊方式非法。
    """
    if total_amount <= 0:
        raise ValueError("收款总金额必须大于 0")
    if not participants or len(participants) < 2:
        raise ValueError("AA 收款至少需要 2 位参与人")
    if split_mode not in ("equal", "custom"):
        raise ValueError("split_mode 仅支持 equal / custom")
    total_fen = _yuan_to_fen(total_amount)
    count = len(participants)
    base_fen, remainder = divmod(total_fen, count)
    shares = []
    for index, name in enumerate(participants):
        share_fen = base_fen + (1 if index < remainder else 0)
        shares.append({"participant": name, "amount_fen": share_fen, "amount_yuan": _fen_to_yuan(share_fen), "status": "requested"})
    receipt = _receipt(
        "split_aa_collection",
        "requested",
        total_fen,
        "已生成 AA 收款请求，等待各参与人确认（不自动扣款）",
        request_id=_new_id("aa"),
        payer=payer or "发起人",
        split_mode=split_mode,
        due_date=due_date,
        shares=shares,
        remainder_fen=remainder,
    )
    return _submit_to_sandbox(receipt)


def to_openai_tools() -> list[dict[str, Any]]:
    """将所有转账工具转换为 OpenAI 函数调用 JSON Schema 列表。

    Returns:
        工具 JSON Schema 列表（可直接传给 DeepSeek 的 ``tools`` 参数）。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return [convert_to_openai_tool(t) for t in ALL_TRANSFER_TOOLS]


# 转账工具注册表（供图编排层按名称分发）
ALL_TRANSFER_TOOLS = [
    smart_transfer,
    transfer_by_name,
    transfer_by_phone,
    transfer_by_note,
    schedule_transfer,
    split_aa_collection,
]
