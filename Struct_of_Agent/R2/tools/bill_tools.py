"""R2 账单分析领域工具集。

对应 R2 账单分析的四项能力：
    1. record_consumption          记录一笔消费/支出（LLM 关键词分析后分类入库）
    2. category_statistics         消费分类统计（读取消费流水按分类汇总）
    3. detect_abnormal_transactions 异常交易识别（规则 + LLM 判断）
    4. monthly_report              月度账单报告（按期生成、留存、可查询）
    5. annual_report               年度账单报告（按期生成、留存、可查询）

说明：
    - 金额统一以「分」为最小单位计算，避免浮点误差。
    - 消费分类：LLM 依据备注做关键词分析得到类别，规则词表作为确定性兜底。
    - 异常识别：规则（同一对手 >10 次、虚拟物品、线上赌博）确定性标记，
      LLM 负责生成自然语言判断摘要。
    - 报告：按期自动聚合生成并留存数据库，供用户随时查询（生成即推送）。

[模型接入点] 工具通过闭包持有 ``model``（ChatModel 协议），LLM 分类/判断仅在
这里发生，替换模型供应商不影响工具契约。
[事件接入点] 用户转账/支出成功后，R1 应调用 ``TransactionStore.record_transaction``
记录流水，实现「转账 - 账单」联动。
"""

from __future__ import annotations

import uuid
from collections import Counter
from decimal import Decimal, ROUND_DOWN
from typing import Any

from langchain_core.tools import tool

from ..llm import ChatModel
from ..persistence import TransactionStore

# 消费分类（LLM 关键词分析的目标类别，规则词表兜底）
_CATEGORIES = ["餐饮", "购物", "交通", "娱乐", "居住", "医疗", "教育", "其他"]

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "餐饮": ["餐", "饭", "外卖", "奶茶", "咖啡", "火锅", "餐厅", "美团", "饿了么", "早餐", "午餐", "晚餐"],
    "购物": ["购物", "淘宝", "京东", "拼多多", "超市", "商场", "买", "衣服", "鞋", "日用品"],
    "交通": ["打车", "滴滴", "地铁", "公交", "高铁", "机票", "加油", "停车", "火车"],
    "娱乐": ["电影", "游戏", "KTV", "会员", "视频", "音乐", "门票", "健身", "直播"],
    "居住": ["房租", "物业", "水电", "燃气", "宽带", "房贷"],
    "医疗": ["医院", "药", "挂号", "体检", "门诊", "诊所"],
    "教育": ["学费", "课程", "培训", "书", "考试", "辅导"],
}

# 异常交易识别关键词
_VIRTUAL_ITEM_KEYWORDS = ["虚拟", "点券", "Q币", "Q 币", "道具", "装备", "皮肤", "元宝", "游戏充值", "网游"]
_GAMBLING_KEYWORDS = ["赌博", "博彩", "彩票", "下注", "赌", "菠菜", "六合彩", "外围"]

# 同一交易对手出现次数的异常阈值
_SAME_COUNTERPARTY_THRESHOLD = 10


def _yuan_to_fen(amount: float | Decimal) -> int:
    """将金额（元）精确转换为「分」。

    Args:
        amount: 金额，单位元。

    Returns:
        金额的分值（整数）。
    """
    return int((Decimal(str(amount)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN))


def _fen_to_yuan(fen: int) -> str:
    """将「分」格式化为两位小数的元字符串。

    Args:
        fen: 金额（分）。

    Returns:
        形如 "100.00" 的字符串。
    """
    return f"{Decimal(fen) / Decimal('100'):.2f}"


def _keyword_classify(note: str) -> str:
    """依据备注关键词做确定性分类兜底。

    Args:
        note: 消费备注。

    Returns:
        分类名，未命中返回「其他」。
    """
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in note:
                return category
    return "其他"


def _classify_note(model: ChatModel, note: str) -> str:
    """对消费备注做分类：LLM 关键词分析优先，规则词表兜底。

    Args:
        model: ChatModel 实例。
        note: 消费备注。

    Returns:
        分类名（餐饮/购物/交通/娱乐/居住/医疗/教育/其他）。
    """
    fallback = _keyword_classify(note)
    try:
        prompt = (
            f"请将以下消费备注归类到：{'/'.join(_CATEGORIES)}。只回复类别名，不要解释。\n备注：{note}"
        )
        response = model.chat([{"role": "user", "content": prompt}], None)
        text = (response["choices"][0]["message"].get("content") or "").strip()
        for category in _CATEGORIES:
            if category in text:
                return category
    except Exception:
        pass
    return fallback


def _detect_abnormal_rules(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """依据规则标记异常交易。

    规则（示例，可扩展）：
        - 同一交易对手出现次数超过 10 次；
        - 备注命中虚拟物品购买关键词；
        - 备注命中线上赌博关键词。

    Args:
        transactions: 消费流水列表。

    Returns:
        被标记为异常的交易列表，每项附带 abnormal_reasons。
    """
    counterparty_counter = Counter(t.get("counterparty", "") for t in transactions if t.get("counterparty"))
    abnormal: list[dict[str, Any]] = []
    for txn in transactions:
        reasons: list[str] = []
        counterparty = txn.get("counterparty", "")
        if counterparty and counterparty_counter.get(counterparty, 0) > _SAME_COUNTERPARTY_THRESHOLD:
            reasons.append(f"同一交易对手「{counterparty}」出现 {counterparty_counter[counterparty]} 次，超过 {_SAME_COUNTERPARTY_THRESHOLD} 次")
        note = txn.get("note", "")
        if any(k in note for k in _VIRTUAL_ITEM_KEYWORDS):
            reasons.append("疑似购买虚拟物品")
        if any(k in note for k in _GAMBLING_KEYWORDS):
            reasons.append("疑似线上赌博转账")
        if reasons:
            abnormal.append({**txn, "abnormal_reasons": reasons})
    return abnormal


def _llm_judge_summary(model: ChatModel, abnormal: list[dict[str, Any]]) -> str:
    """由 LLM 生成异常交易的判断摘要（规则为确定性标记，LLM 负责自然语言判断）。

    Args:
        model: ChatModel 实例。
        abnormal: 被规则标记的异常交易列表。

    Returns:
        判断摘要文本。
    """
    fallback = f"共识别出 {len(abnormal)} 笔异常交易，请关注资金安全。" if abnormal else "未发现异常交易。"
    if not abnormal:
        return fallback
    try:
        brief = [{"note": a.get("note"), "reasons": a.get("abnormal_reasons")} for a in abnormal[:5]]
        prompt = f"以下是被标记的异常交易：{brief}。请用一句话判断这些交易的风险并提醒用户。"
        response = model.chat([{"role": "user", "content": prompt}], None)
        text = (response["choices"][0]["message"].get("content") or "").strip()
        return text or fallback
    except Exception:
        return fallback


def _aggregate(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合流水：计算收支总额与分类明细。

    Args:
        transactions: 消费流水列表。

    Returns:
        聚合结果字典。
    """
    total_expense = 0
    total_income = 0
    category_map: dict[str, dict[str, int]] = {}
    for txn in transactions:
        amount = int(txn.get("amount_fen", 0))
        if txn.get("direction") == "income":
            total_income += amount
        else:
            total_expense += amount
        category = txn.get("category") or "其他"
        entry = category_map.setdefault(category, {"count": 0, "amount_fen": 0})
        entry["count"] += 1
        entry["amount_fen"] += amount
    categories = [
        {"category": c, "count": v["count"], "amount_fen": v["amount_fen"], "amount_yuan": _fen_to_yuan(v["amount_fen"])}
        for c, v in sorted(category_map.items(), key=lambda item: -item[1]["amount_fen"])
    ]
    return {
        "total_expense_fen": total_expense,
        "total_income_fen": total_income,
        "categories": categories,
        "transaction_count": len(transactions),
    }


def build_bill_tools(model: ChatModel, store: TransactionStore) -> list:
    """构造 R2 账单工具集（闭包注入 model 与 store）。

    Args:
        model: ChatModel 实例（用于 LLM 分类与异常判断）。
        store: TransactionStore 实例（用于流水与报告持久化）。

    Returns:
        工具列表（共 5 个）。
    """

    @tool
    def record_consumption(amount: float, note: str, direction: str = "expense", counterparty: str = "") -> dict[str, Any]:
        """记录一笔消费/支出，并由 LLM 依据备注做关键词分析后分类入库。

        当用户发生转账或消费支出时使用本工具，金额与备注用于生成账单分类。
        分类由 LLM 分析备注得到，规则词表兜底；记录后可供分类统计与账单报告使用。

        Args:
            amount: 消费金额，单位元，必须大于 0。
            note: 消费备注（如「美团外卖」「滴滴打车」「房租」）。
            direction: 收支方向，income（收入）或 expense（支出），默认 expense。
            counterparty: 交易对手（可空，如商户名或收款人）。

        Returns:
            一个字典，包含 status、transaction_id、amount_fen、category、note 等字段。

        Raises:
            ValueError: 金额非正或备注为空。
        """
        if amount <= 0:
            raise ValueError("消费金额必须大于 0")
        if not note or not note.strip():
            raise ValueError("消费备注不能为空")
        direction = direction if direction in ("income", "expense") else "expense"
        category = _classify_note(model, note.strip())
        amount_fen = _yuan_to_fen(amount)
        record = store.record_transaction(
            account_id="default",
            amount_fen=amount_fen,
            note=note.strip(),
            category=category,
            direction=direction,
            counterparty=counterparty,
        )
        return {
            "status": "ok",
            "tool": "record_consumption",
            "transaction_id": record["transaction_id"],
            "amount_fen": amount_fen,
            "amount_yuan": _fen_to_yuan(amount_fen),
            "category": category,
            "direction": direction,
            "note": note.strip(),
            "counterparty": counterparty,
        }

    @tool
    def category_statistics(account_id: str = "default", period: str = "") -> dict[str, Any]:
        """统计指定账户在指定时间范围内的消费分类。

        读取消费流水，按分类汇总笔数与金额，返回各分类的占比信息。
        period 支持 "2026-10"（某月）、"2026"（某年），空串表示全部。

        Args:
            account_id: 账户标识，默认 default。
            period: 统计时间前缀（可空）。

        Returns:
            一个字典，包含 status、total_expense_fen、total_income_fen、categories 等字段。
        """
        transactions = store.list_transactions(account_id, period)
        aggregate = _aggregate(transactions)
        return {"status": "ok", "tool": "category_statistics", "account_id": account_id, "period": period, **aggregate}

    @tool
    def detect_abnormal_transactions(account_id: str = "default", period: str = "") -> dict[str, Any]:
        """识别指定账户的异常交易。

        依据规则标记异常（同一对手 >10 次、虚拟物品、线上赌博），
        并交由 LLM 生成自然语言判断摘要。

        Args:
            account_id: 账户标识，默认 default。
            period: 统计时间前缀（可空）。

        Returns:
            一个字典，包含 status、abnormal_count、abnormal_transactions、judgment 等字段。
        """
        transactions = store.list_transactions(account_id, period)
        abnormal = _detect_abnormal_rules(transactions)
        # 设计文档 §4.7：异常识别结果要落到 is_abnormal / abnormal_reason，
        # 报告统计才能直接读取，而不是每次重新跑规则。
        for item in abnormal:
            store.mark_abnormal(
                str(item.get("transaction_id") or ""),
                "；".join(item.get("abnormal_reasons") or []),
            )
        judgment = _llm_judge_summary(model, abnormal)
        return {
            "status": "ok",
            "tool": "detect_abnormal_transactions",
            "account_id": account_id,
            "period": period,
            "abnormal_count": len(abnormal),
            "abnormal_transactions": abnormal,
            "judgment": judgment,
        }

    @tool
    def monthly_report(account_id: str = "default", month: str = "", refresh: bool = False) -> dict[str, Any]:
        """生成或查询指定月份的月度账单报告。

        按期聚合消费流水生成月度报告，生成后留存数据库供用户随时查询；
        若报告已存在且未要求刷新，则直接返回已留存报告。

        Args:
            account_id: 账户标识，默认 default。
            month: 报告月份，形如 "2026-10"；空串取当前月份。
            refresh: 是否强制重新生成，默认 False。

        Returns:
            一个字典，包含 status、report_type、period、收支总额、分类明细与 source 字段。

        Raises:
            ValueError: month 格式不正确。
        """
        if not month:
            month = __import__("datetime").datetime.now().strftime("%Y-%m")
        if len(month) != 7 or month[4] != "-":
            raise ValueError("month 需为 YYYY-MM 格式，如 2026-10")
        if not refresh:
            existing = store.get_report(account_id, "monthly", month)
            if existing is not None:
                return {"status": "ok", "tool": "monthly_report", "source": "stored", **existing}
        transactions = store.list_transactions(account_id, month)
        content = {
            "report_type": "monthly",
            "period": month,
            "account_id": account_id,
            **_aggregate(transactions),
            "abnormal_count": len(_detect_abnormal_rules(transactions)),
        }
        store.save_report(account_id, "monthly", month, content)
        return {"status": "ok", "tool": "monthly_report", "source": "generated", **content}

    @tool
    def annual_report(account_id: str = "default", year: str = "", refresh: bool = False) -> dict[str, Any]:
        """生成或查询指定年度的年度账单报告。

        按期聚合全年消费流水生成年度报告，生成后留存数据库供用户随时查询；
        若报告已存在且未要求刷新，则直接返回已留存报告。

        Args:
            account_id: 账户标识，默认 default。
            year: 报告年份，形如 "2026"；空串取当前年份。
            refresh: 是否强制重新生成，默认 False。

        Returns:
            一个字典，包含 status、report_type、period、收支总额、分类明细与 source 字段。

        Raises:
            ValueError: year 格式不正确。
        """
        if not year:
            year = __import__("datetime").datetime.now().strftime("%Y")
        if len(year) != 4 or not year.isdigit():
            raise ValueError("year 需为 YYYY 格式，如 2026")
        if not refresh:
            existing = store.get_report(account_id, "annual", year)
            if existing is not None:
                return {"status": "ok", "tool": "annual_report", "source": "stored", **existing}
        transactions = store.list_transactions(account_id, year)
        content = {
            "report_type": "annual",
            "period": year,
            "account_id": account_id,
            **_aggregate(transactions),
            "abnormal_count": len(_detect_abnormal_rules(transactions)),
        }
        store.save_report(account_id, "annual", year, content)
        return {"status": "ok", "tool": "annual_report", "source": "generated", **content}

    return [record_consumption, category_statistics, detect_abnormal_transactions, monthly_report, annual_report]
