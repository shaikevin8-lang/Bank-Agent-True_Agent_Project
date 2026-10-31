"""R2 沙箱统计工具：分类月均统计的「受限代码沙箱」补强。

契约来源：``docs/_work/阶段2-代码沙箱-接口冻结.md`` 第 8 节。本模块只做一件
事——把 ``store`` 里已有的消费流水整形成沙箱输入，交给
:func:`common.code_compute.compute_category_monthly` 在沙箱里算，再把结果按
工具回执口径返回。

为什么不在这里自己算：沙箱不可用时必须**如实降级**（``label="标准统计"``、
``result=None``），本地重算一遍再贴上「沙箱计算」的标签会掩盖降级事实，
让「受限执行」这条安全边界形同虚设。

``computed_by`` 口径：沙箱算出来的为 ``code-sandbox``，降级为 ``local``
（表示「没有自定义统计，只有标准口径」，不是一个真在本地跑过的数字）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from common import code_compute

from ..persistence import TransactionStore

#: 结果来源：沙箱。
COMPUTED_BY_SANDBOX = "code-sandbox"
#: 结果来源：降级（无自定义统计）。
COMPUTED_BY_LOCAL = "local"
#: 回执固定提示（试算口径，不能当收益/承诺用）。
RESULT_NOTE = "试算结果，非收益承诺"


def _month_of(transaction: dict[str, Any], period: str) -> str:
    """从流水里取月份（``YYYY-MM``），取不到时退回统计期间。

    Args:
        transaction: 一条流水字典（``occurred_at`` 为本地 ISO 字符串）。
        period: 调用方给出的统计期间（可能是 ``YYYY-MM`` / ``YYYY`` / 空串）。

    Returns:
        月份字符串；无法判定时返回 ``""``（沙箱侧会当成一个「未知月份」桶）。
    """
    occurred_at = str(transaction.get("occurred_at") or "").strip()
    if len(occurred_at) >= 7 and occurred_at[4] == "-":
        return occurred_at[:7]
    scoped = (period or "").strip()
    return scoped if len(scoped) == 7 else ""


def _records_of(transactions: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    """把流水整形成沙箱输入 ``records``（只做搬运，不做聚合）。

    Args:
        transactions: ``store.list_transactions`` 返回的流水列表。
        period: 统计期间（用于无法解析月份时的兜底）。

    Returns:
        每项含 ``category`` / ``month`` / ``amount_fen`` 的记录列表。
    """
    records: list[dict[str, Any]] = []
    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue
        try:
            amount_fen = int(transaction.get("amount_fen") or 0)
        except (TypeError, ValueError):
            amount_fen = 0
        records.append(
            {
                "category": str(transaction.get("category") or "其他"),
                "month": _month_of(transaction, period),
                "amount_fen": amount_fen,
            }
        )
    return records


def build_sandbox_tools(store: TransactionStore) -> list:
    """构造 R2 沙箱统计工具集（闭包注入 store）。

    Args:
        store: :class:`R2.persistence.TransactionStore`，用于读取已有流水。

    Returns:
        工具列表，当前只含 ``category_monthly_statistics``。
    """

    @tool
    def category_monthly_statistics(account_id: str = "default", period: str = "") -> dict[str, Any]:
        """按消费分类计算月均与全期均值（由受限代码沙箱试算）。

        用于回答「我每个月各类花多少」「餐饮月均多少」这类需要自定义口径的问题：
        本工具读取指定账户已有消费流水，交给受限代码沙箱按分类分组，
        用整数分计算「组内月均」与「全期月均」，口径与 category_statistics 一致
        （统计全部 posted 流水，含收支两个方向）。

        period 支持 "2026-10"（某月）、"2026"（某年），空串表示全部。

        Args:
            account_id: 账户标识，默认 default。
            period: 统计期间前缀（可空）。

        Returns:
            一个字典，包含 ok、label（沙箱计算/标准统计）、computed_by
            （code-sandbox/local）、degraded、result、note；
            降级时 label 为「标准统计」、degraded 为 True、result 为 None。
        """
        transactions = store.list_transactions(account_id, period)
        outcome = code_compute.compute_category_monthly({"records": _records_of(transactions, period)})
        degraded = bool(outcome.get("degraded"))
        label = str(outcome.get("label") or code_compute.STANDARD_LABEL)
        return {
            "ok": bool(outcome.get("ok")),
            "label": label,
            "computed_by": COMPUTED_BY_LOCAL if degraded else COMPUTED_BY_SANDBOX,
            "degraded": degraded,
            "result": outcome.get("result"),
            "note": RESULT_NOTE,
        }

    return [category_monthly_statistics]


__all__ = ["COMPUTED_BY_LOCAL", "COMPUTED_BY_SANDBOX", "RESULT_NOTE", "build_sandbox_tools"]
