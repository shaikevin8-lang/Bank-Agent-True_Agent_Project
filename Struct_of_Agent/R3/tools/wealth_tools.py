"""R3 理财操作领域工具集（5 个理财工具）。

对应 R3 理财操作的四项能力：
    1. risk_assessment     风险评估（联网搜索评估标准 + LLM 判定风险等级，落库一年有效）
    2. recommend_products  产品推荐（联网搜索行情 + 按客户风险等级适当性过滤）
    3. compare_products    产品对比（多产品风险/期限/费率/流动性横向对比）
    4. purchase_product    一键申购（适当性校验 + 余额扣减 + 持仓入库 + 订单回执）
    5. redeem_product      一键赎回（持仓校验 + 份额扣减 + 资金到账 + 订单回执）

说明：
    - 金额统一以「分」为最小单位计算，避免浮点误差。
    - 风险评估与产品推荐/对比需联网搜索（Tavily），搜索结果作为事实参考来源，
      未配置搜索服务时自动降级为本地规则/本地产品数据，不影响流程闭环。
    - 申购/赎回为沙箱内的本地模拟写操作，直接读写 WealthStore；
      申购强制执行投资者适当性：产品风险等级不得高于客户风险测评等级，
      未完成测评的客户必须先测评后申购。

[模型接入点] 工具通过闭包持有 ``model``（ChatModel 协议），LLM 判定仅在这里发生。
[沙箱接入点] 接入真实理财交易沙箱时替换 ``WealthStore`` 读写实现即可，
工具签名与返回结构不变。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any

from langchain_core.tools import tool

from common.tavily_search import run_tavily_search

from ..llm import ChatModel
from ..persistence import RISK_LABELS, RISK_RANK, WealthStore

# R3 理财工具名集合（供图编排层识别「产生回执」的工具）
WEALTH_TOOL_NAMES = {
    "risk_assessment",
    "recommend_products",
    "compare_products",
    "purchase_product",
    "redeem_product",
}

# 缺省理财账户：与 web_test 演示账户一致
DEFAULT_ACCOUNT_ID = "622200001"

# 风险等级关键词（规则兜底，LLM 不可用时保证测评闭环）
_RISK_KEYWORDS: dict[str, list[str]] = {
    "R1": ["保守", "保本", "不能亏", "极低风险", "安稳"],
    "R2": ["稳健", "低风险", "小幅波动", "偏保守"],
    "R3": ["平衡", "中等", "适中", "可接受一定波动", "波动"],
    "R4": ["成长", "较高风险", "积极"],
    "R5": ["进取", "激进", "高风险", "高收益", "大幅波动"],
}


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


def _keyword_risk_level(text: str) -> str:
    """依据用户自述关键词做确定性风险等级兜底判定。

    取命中的最高风险等级（用户同时提到「稳健」与「高风险」时，说明其能承受
    较高波动，按较高者定级）；无命中时默认 R2（稳健型）。

    Args:
        text: 用户自述文本。

    Returns:
        风险等级（R1~R5）。
    """
    matched = [level for level, keywords in _RISK_KEYWORDS.items() if any(k in text for k in keywords)]
    if not matched:
        return "R2"
    return max(matched, key=lambda level: RISK_RANK[level])


def _llm_risk_level(model: ChatModel, user_input: str, search_summary: str) -> str:
    """由 LLM 结合搜索到的评估标准判定风险等级，失败时回退关键词规则。

    Args:
        model: ChatModel 实例。
        user_input: 用户自述（投资经验、期限、可承受波动等）。
        search_summary: 联网搜索到的风险评估标准摘要（可空）。

    Returns:
        风险等级（R1~R5）。
    """
    fallback = _keyword_risk_level(user_input)
    try:
        prompt = (
            "你是银行理财风险测评专员。请根据用户的自述，并参考以下风险等级评估标准，"
            "判定用户的风险承受等级。只回复等级代号（R1/R2/R3/R4/R5 之一），不要解释。\n"
            f"评估标准参考：{search_summary or 'R1保守型/R2稳健型/R3平衡型/R4成长型/R5进取型'}\n"
            f"用户自述：{user_input}"
        )
        response = model.chat([{"role": "user", "content": prompt}], None)
        text = (response["choices"][0]["message"].get("content") or "").strip()
        for level in ("R5", "R4", "R3", "R2", "R1"):
            if level in text:
                return level
        for level, label in RISK_LABELS.items():
            if label in text:
                return level
    except Exception:
        pass
    return fallback


def _parse_term_days(term_preference: str) -> int | None:
    """从期限偏好文本中解析天数（如 "90天"/"三个月"）。

    Args:
        term_preference: 期限偏好文本。

    Returns:
        天数；无法解析返回 None。
    """
    if not term_preference:
        return None
    match = re.search(r"(\d+)\s*天", term_preference)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)\s*个?月", term_preference)
    if match:
        return int(match.group(1)) * 30
    match = re.search(r"(\d+)\s*年", term_preference)
    if match:
        return int(match.group(1)) * 365
    if "活期" in term_preference or "随时" in term_preference:
        return 0
    return None


def _search_summary(search: dict[str, Any]) -> str:
    """从 Tavily 搜索结果中提取摘要文本（answer 优先，其次拼接结果标题）。

    Args:
        search: run_tavily_search 的返回字典。

    Returns:
        摘要文本；无可用内容返回空串。
    """
    if search.get("status") != "ok":
        return ""
    answer = (search.get("answer") or "").strip()
    if answer:
        return answer
    return "；".join(item.get("title", "") for item in search.get("results", []) if item.get("title"))


def build_wealth_tools(model: ChatModel, store: WealthStore) -> list:
    """构造 R3 理财工具集（闭包注入 model 与 store）。

    Args:
        model: ChatModel 实例（用于风险等级判定）。
        store: WealthStore 实例（用于测评/产品/持仓/订单持久化）。

    Returns:
        工具列表（共 5 个）。
    """

    @tool
    def risk_assessment(user_input: str, account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
        """为客户完成一次理财风险承受能力评估（风险测评）。

        当用户要求做风险评估/风险测评，或申购前需要确认风险等级时使用本工具。
        依据用户自述（投资经验、收入稳定性、可承受波动、投资期限等），
        先联网搜索风险等级评估标准作为参考，再由 LLM 判定 R1~R5 风险等级，
        结果落库留存（有效期一年），作为后续申购的适当性依据。

        Args:
            user_input: 用户的风险偏好自述（如「稳健偏保守，只能接受小幅波动」）。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、risk_level、risk_label、score、valid_until、
            suitability（适配产品等级列表）与 search（联网搜索参考）字段。

        Raises:
            ValueError: 用户自述为空。
        """
        if not user_input or not user_input.strip():
            raise ValueError("风险测评需要用户的风险偏好自述")
        search = run_tavily_search("银行理财 投资者风险承受能力评估标准 R1 R2 R3 R4 R5 等级划分", max_results=3)
        summary = _search_summary(search)
        risk_level = _llm_risk_level(model, user_input.strip(), summary)
        score = RISK_RANK[risk_level] * 20
        suitability = [level for level in RISK_RANK if RISK_RANK[level] <= RISK_RANK[risk_level]]
        record = store.save_risk_assessment(
            account_id=account_id,
            risk_level=risk_level,
            score=score,
            suitability=suitability,
            source=summary[:255],
        )
        return {
            "status": "ok",
            "tool": "risk_assessment",
            "account_id": account_id,
            "risk_level": risk_level,
            "risk_label": RISK_LABELS.get(risk_level, risk_level),
            "score": score,
            "valid_until": record["valid_until"],
            "suitability": suitability,
            "search": {"status": search.get("status"), "summary": summary, "source": search.get("source")},
        }

    @tool
    def recommend_products(
        amount: float = 0.0,
        term_preference: str = "",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        """按客户风险等级推荐可申购的理财产品。

        当用户询问「推荐理财」「买什么产品好」时使用本工具。
        读取客户最近一次风险测评结果（未测评则按全量展示并提示先测评），
        仅推荐风险等级不超过客户承受能力、且满足起购金额与期限偏好的在售产品；
        同时联网搜索当前理财行情作为推荐依据。

        Args:
            amount: 计划投资金额（元），0 表示不限。
            term_preference: 期限偏好（如 "90天"/"三个月"/"活期"，可空）。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status、risk_level、products（推荐列表）与
            search（联网行情参考）字段。
        """
        search = run_tavily_search("银行理财产品推荐 稳健 固收 净值型 最新行情", max_results=3)
        assessment = store.get_risk_assessment(account_id)
        user_rank = RISK_RANK[assessment["risk_level"]] if assessment else None
        products = [p for p in store.list_products() if p["status"] == "on_sale"]
        if user_rank is not None:
            products = [p for p in products if RISK_RANK[p["risk_level"]] <= user_rank]
        amount_fen = _yuan_to_fen(amount) if amount and amount > 0 else 0
        if amount_fen > 0:
            products = [p for p in products if p["min_amount_fen"] <= amount_fen]
        term_days = _parse_term_days(term_preference)
        if term_days is not None:
            products = [p for p in products if p["term_days"] == 0 or p["term_days"] <= term_days]
        for product in products:
            product["min_amount_yuan"] = _fen_to_yuan(product["min_amount_fen"])
        return {
            "status": "ok",
            "tool": "recommend_products",
            "account_id": account_id,
            "risk_level": assessment["risk_level"] if assessment else "",
            "risk_label": RISK_LABELS.get(assessment["risk_level"], "") if assessment else "未测评",
            "assessed": assessment is not None,
            "notice": "" if assessment else "您尚未完成风险测评，以下展示全部在售产品；申购前需先完成风险测评。",
            "products": products,
            "search": {"status": search.get("status"), "summary": _search_summary(search), "source": search.get("source")},
        }

    @tool
    def compare_products(product_names: list[str] | None = None) -> dict[str, Any]:
        """对比多款理财产品的风险、期限、起购金额、费率与流动性。

        当用户要求「对比一下这几款产品」时使用本工具。
        按产品名称或编号筛选（缺省时对比全部在售产品），
        并联网搜索理财对比维度作为参考。

        Args:
            product_names: 产品名称或编号列表（可空，空则对比全部在售产品）。

        Returns:
            一个字典，包含 status、comparison（对比明细列表）与
            search（联网参考）字段。
        """
        products = [p for p in store.list_products() if p["status"] == "on_sale"]
        if product_names:
            selected = [
                p
                for p in products
                if any(name and (name in p["name"] or name == p["product_id"]) for name in product_names)
            ]
            missing = [
                name
                for name in product_names
                if name and not any(name in p["name"] or name == p["product_id"] for p in products)
            ]
        else:
            selected, missing = products, []
        comparison = [
            {
                "product_id": p["product_id"],
                "name": p["name"],
                "risk_level": p["risk_level"],
                "risk_label": p["risk_label"],
                "term_days": p["term_days"],
                "min_amount_yuan": _fen_to_yuan(p["min_amount_fen"]),
                "nav": p["nav"],
                "fee_rate": p["fee_rate"],
                "liquidity": p["liquidity"],
                "description": p["description"],
            }
            for p in selected
        ]
        search = run_tavily_search("银行理财产品对比 风险等级 期限 费率 流动性 怎么选", max_results=3)
        return {
            "status": "ok",
            "tool": "compare_products",
            "comparison": comparison,
            "missing": missing,
            "search": {"status": search.get("status"), "summary": _search_summary(search), "source": search.get("source")},
        }

    @tool
    def purchase_product(product_id: str, amount: float, account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
        """一键申购理财产品（沙箱内执行：扣减余额、增加持仓、生成订单回执）。

        当用户明确要「申购/购买/买入」某款产品并给出金额时使用本工具。
        执行前依次校验：产品存在且在售、金额不低于起购金额、客户已完成风险测评、
        产品风险等级不超过客户承受能力、账户余额充足；任一不满足即拒绝并说明原因，
        不产生任何写副作用。

        Args:
            product_id: 产品编号（如 "P001"）。
            amount: 申购金额（元），必须大于 0 且不低于产品起购金额。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status（accepted/rejected）、order_id、product、
            amount、shares、balance_after 等字段；拒绝时附带 reason。

        Raises:
            ValueError: 申购金额非正。
        """
        if amount <= 0:
            raise ValueError("申购金额必须大于 0")
        product = store.get_product(product_id)
        if product is None:
            return {"status": "rejected", "tool": "purchase_product", "reason": f"产品 {product_id} 不存在"}
        if product["status"] != "on_sale":
            return {"status": "rejected", "tool": "purchase_product", "reason": f"产品「{product['name']}」已停售"}
        amount_fen = _yuan_to_fen(amount)
        if amount_fen < product["min_amount_fen"]:
            return {
                "status": "rejected",
                "tool": "purchase_product",
                "reason": f"低于起购金额：「{product['name']}」起购 {_fen_to_yuan(product['min_amount_fen'])} 元",
            }
        assessment = store.get_risk_assessment(account_id)
        if assessment is None:
            return {
                "status": "rejected",
                "tool": "purchase_product",
                "reason": "您尚未完成风险测评，请先进行风险评估后再申购",
            }
        if RISK_RANK[product["risk_level"]] > RISK_RANK[assessment["risk_level"]]:
            return {
                "status": "rejected",
                "tool": "purchase_product",
                "reason": (
                    f"超出风险承受能力：您是{assessment['risk_level']}（{RISK_LABELS.get(assessment['risk_level'])}），"
                    f"产品「{product['name']}」为{product['risk_level']}（{product['risk_label']}）"
                ),
            }
        if not store.debit(account_id, amount_fen):
            return {
                "status": "rejected",
                "tool": "purchase_product",
                "reason": f"账户余额不足：当前余额 {_fen_to_yuan(store.get_balance(account_id))} 元",
            }
        shares = round(amount_fen / 100 / product["nav"], 4)
        store.add_holding(account_id, product_id, shares, product["nav"])
        order = {
            "status": "accepted",
            "tool": "purchase_product",
            "order_id": f"pur-{uuid.uuid4().hex[:12]}",
            "order_type": "purchase",
            "account_id": account_id,
            "product_id": product_id,
            "product_name": product["name"],
            "amount_fen": amount_fen,
            "amount_yuan": _fen_to_yuan(amount_fen),
            "shares": shares,
            "nav": product["nav"],
            "balance_after_fen": store.get_balance(account_id),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": f"已申购「{product['name']}」{_fen_to_yuan(amount_fen)} 元，确认份额 {shares} 份（沙箱回执）",
        }
        return store.save_order(order)

    @tool
    def redeem_product(product_id: str, shares: float = 0.0, account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
        """一键赎回理财产品（沙箱内执行：扣减持仓、资金到账、生成订单回执）。

        当用户明确要「赎回/卖出」某款产品时使用本工具。
        执行前校验：产品存在、账户持有该产品且份额充足；shares 传 0 表示全部赎回。
        赎回资金按当前净值折算，即刻返还账户余额（沙箱模拟）。

        Args:
            product_id: 产品编号（如 "P001"）。
            shares: 赎回份额，0 表示全部赎回。
            account_id: 账户标识，默认演示账户。

        Returns:
            一个字典，包含 status（accepted/rejected）、order_id、product、
            shares、amount、balance_after 等字段；拒绝时附带 reason。

        Raises:
            ValueError: 赎回份额为负。
        """
        if shares < 0:
            raise ValueError("赎回份额不能为负")
        product = store.get_product(product_id)
        if product is None:
            return {"status": "rejected", "tool": "redeem_product", "reason": f"产品 {product_id} 不存在"}
        holding = store.get_holding(account_id, product_id)
        if holding is None or holding["shares"] <= 0:
            return {
                "status": "rejected",
                "tool": "redeem_product",
                "reason": f"您未持有「{product['name']}」，无可赎回份额",
            }
        redeem_shares = round(holding["shares"], 4) if shares == 0 else round(shares, 4)
        if redeem_shares > holding["shares"]:
            return {
                "status": "rejected",
                "tool": "redeem_product",
                "reason": f"赎回份额不足：当前持有 {holding['shares']} 份",
            }
        if not store.reduce_holding(account_id, product_id, redeem_shares):
            return {"status": "rejected", "tool": "redeem_product", "reason": "持仓扣减失败，请稍后重试"}
        amount_fen = _yuan_to_fen(Decimal(str(redeem_shares)) * Decimal(str(product["nav"])))
        store.credit(account_id, amount_fen)
        order = {
            "status": "accepted",
            "tool": "redeem_product",
            "order_id": f"red-{uuid.uuid4().hex[:12]}",
            "order_type": "redeem",
            "account_id": account_id,
            "product_id": product_id,
            "product_name": product["name"],
            "shares": redeem_shares,
            "nav": product["nav"],
            "amount_fen": amount_fen,
            "amount_yuan": _fen_to_yuan(amount_fen),
            "balance_after_fen": store.get_balance(account_id),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": f"已赎回「{product['name']}」{redeem_shares} 份，到账 {_fen_to_yuan(amount_fen)} 元（沙箱回执）",
        }
        return store.save_order(order)

    return [risk_assessment, recommend_products, compare_products, purchase_product, redeem_product]


__all__ = ["build_wealth_tools", "WEALTH_TOOL_NAMES", "DEFAULT_ACCOUNT_ID"]
