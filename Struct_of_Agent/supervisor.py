"""总 Agent（Supervisor）：R1~R6 的统一入口、LLM 意图分析与任务编排。

职责（对应基座文档「图状态只保存路由/澄清/计划」）：
    1. 用 LLM 分析用户意图：把用户输入拆分为一个或多个子任务，并为每个子任务
       选择负责的场景子 Agent（R1 转账 / R2 账单 / R3 理财 / R4 卡片 / common 通用）；
    2. 按计划逐个分发执行，并汇总为一条统一回复；
    3. LLM 不可用或输出非法时，自动回退到规则路由（关键词 + 正则），保证离线可用；
    4. 不承载任何转账/账单/理财/卡片业务规则——业务在子 Agent 内，安全/事实/持久化在共享底座。

分发目标：
    - 涉及转账 → R1（TransferAgent）
    - 涉及账单 → R2（BillAgent）
    - 涉及理财 → R3（WealthAgent）
    - 涉及卡片 → R4（CardAgent）
    - 其余（搜索/寒暄/通用问答）→ common（CommonAgent，走 Tavily 搜索 + LLM）

[前后端接入点] 前端/后端只依赖本类即可，无需感知子 Agent 细节。

[路由说明] 规则路由（``_ROUTE_KEYWORDS`` / ``_ROUTE_PATTERNS``）只作为 LLM 调度器
不可用时的兜底：关键词穷举无法覆盖自然语言表达（如「给李四转一百块」不含「转账」），
正式路径一律由 :meth:`_plan` 的 LLM 意图分析承担。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any

from common.agent import CommonAgent
from common.config import load_config
from common.llm import DeepSeekChat
from common.memory import SessionMemory, build_llm_summarizer
from common.middleware import ContentModerationMiddleware, LLMReviewer
from common.model_manager import RuntimeChatModel, resolve_manager, trace_info
from common import plan as plan_dag
from common.prompts import SUPERVISOR_SYSTEM_PROMPT
from common.text_format import ReplyStreamFilter, sanitize_reply
from R1 import TransferAgent
from R2 import BillAgent
from R3 import WealthAgent
from R4 import CardAgent
from R5 import SubscriptionAgent
from R6 import WorkflowAgent

logger = logging.getLogger(__name__)

# 规则路由关键词表：LLM 调度器不可用时的兜底（R4~R6 追加条目即可扩展）
_ROUTE_KEYWORDS: dict[str, list[str]] = {
    "R1": ["转账", "汇款", "收款", "AA", "转给", "定时", "余额", "姓名", "手机号", "备注"],
    "R2": ["账单", "消费", "分类", "统计", "异常", "月报", "年报", "月度", "年度", "记录", "支出"],
    "R3": ["理财", "风险", "测评", "申购", "赎回", "基金", "产品推荐", "产品对比", "对比", "持仓", "买入", "卖出"],
    "R4": ["卡片", "办卡", "额度", "提额", "限额", "解锁", "冻结", "挂失", "信用卡", "银行卡", "虚拟卡", "演示卡"],
    "R5": ["订阅", "续费", "代扣", "自动扣费", "会员", "解约", "取消订阅"],
    "R6": ["生日", "礼品", "蛋糕", "鲜花", "预留", "生日预算"],
}

# 规则路由正则兜底：覆盖口语化表达（如「给李四转一百块」不含「转账」也能路由到 R1）。
# 只在关键词全部未命中后启用，要求「转」紧跟金额数字/中文数字，避免误伤「反转」「转达」等。
_ROUTE_PATTERNS: dict[str, list[str]] = {
    "R1": [r"转\s*[0-9一二三四五六七八九十百千万]", r"给\S{1,6}转"],
}

# 摘要器缓存哨兵：区分「尚未构造」与「构造结果为 None（无可用模型）」
_SUMMARIZER_UNSET: Any = object()


# 各 Agent 能力说明（供 LLM 调度器生成计划时参考）
_AGENT_CAPABILITIES: dict[str, str] = {
    "R1": "智能转账：按姓名/手机号/历史备注转账、定时转账、AA 收款、余额查询",
    "R2": "账单分析：记录消费、消费分类统计、异常交易识别、月度/年度账单报告",
    "R3": "理财操作：风险评估、理财产品推荐与对比、申购、赎回、持仓查询",
    "R4": "卡片管理：虚拟/演示卡申请、额度调整、交易限制与解锁、挂失与冻结",
    "R5": "订阅/代扣管理：识别订阅扣费、续费提醒、取消订阅/代扣",
    "R6": "跨场景联动：生日预算预留、定时触发、鲜花与蛋糕礼品下单",
    "common": "通用问答：天气、汇率、新闻、百科、寒暄，以及无法归入上述场景的请求",
}

# 各模块的默认风险分级（D03 计划卡片展示用；真正的门禁在工具层 HITL）。
_AGENT_RISK: dict[str, str] = {
    "R1": plan_dag.RISK_YELLOW,
    "R2": plan_dag.RISK_GREEN,
    "R3": plan_dag.RISK_YELLOW,
    "R4": plan_dag.RISK_RED,
    "R5": plan_dag.RISK_GREEN,
    "R6": plan_dag.RISK_YELLOW,
    "common": plan_dag.RISK_GREEN,
}


def _record_moderation_violation(result: Any, session_id: str) -> None:
    """把注入 / 越权类拦截记为熔断零容忍事件（D01）。

    只对 `prompt_injection` / `privilege_escalation` 两类命中记账：它们
    意味着写路径正在被主动攻击，按「零容忍」立即开启该会话全部写能力；普通
    违规内容（如辱骂、其他 blocklist 命中）只做内容拦截，不动业务可用性。

    Args:
        result: 内容审核结果（`common.middleware.ModerationResult`）。
        session_id: 会话 ID；作为熔断的 user 维度。

    Returns:
        `None`。记账失败只写日志，绝不影响本条输入的拦截结论。
    """
    category = str(getattr(result, "category", "") or "")
    reasons = {
        "prompt_injection": None,
        "privilege_escalation": None,
    }
    if category not in reasons:
        return
    try:
        from common import circuit_breaker, hitl

        reason_code = (
            circuit_breaker.REASON_PROMPT_INJECTION
            if category == "prompt_injection"
            else circuit_breaker.REASON_PRIVILEGE_VIOLATION
        )
        circuit_breaker.record_violation(
            reason_code,
            tenant_id=hitl.default_tenant_id(),
            user_id=session_id,
            detail=f"内容审核拦截：{category}",
        )
    except Exception as error:  # noqa: BLE001 - 记账失败不影响拦截结论
        logger.error("熔断零容忍事件记录失败 category=%s：%s", category, error)


class SupervisorAgent:
    """总 Agent：统一入口 + LLM 意图分析/任务拆分 + 多任务编排 + 结果汇总。"""

    def __init__(
        self,
        agents: dict[str, Any] | None = None,
        route_keywords: dict[str, list[str]] | None = None,
        common_agent: Any | None = None,
        model_manager: Any | None = None,
        memory: SessionMemory | None = None,
    ) -> None:
        """初始化总 Agent。

        Args:
            agents: 场景子 Agent 映射，形如 ``{"R1": TransferAgent(), "R2": BillAgent()}``；
                    缺省时按默认配置构造 R1/R2/R3。
            route_keywords: 规则路由关键词表（兜底用）；缺省使用内置 ``_ROUTE_KEYWORDS``。
            common_agent: 通用搜索 Agent（处理未命中 R1/R2/R3 的输入）；缺省构造 CommonAgent。
            model_manager: 运行时模型配置管理器；注入后缺省子 Agent 与 LLM 调度器
                均使用可热切换的模型，并在返回结果的 trace 中附带当前模型模式。
            memory: 会话上下文记忆；注入后本类负责「读上下文 → 注入调度器与子 Agent
                → 写回本轮 → 超预算自动压缩」。为 ``None`` 时退化为旧的无记忆行为。
        """
        self.model_manager = model_manager
        self.agents = agents if agents is not None else self._default_agents()
        self.route_keywords = route_keywords or _ROUTE_KEYWORDS
        self.common_agent = common_agent or CommonAgent(model_manager=model_manager)
        self.memory = memory
        # D03：会话 → 当前计划版本（重规划保留旧版，恢复按 plan_id+version）。
        self._plans: dict[str, plan_dag.Plan] = {}
        self._router_model = self._build_router_model()
        self._summarizer_cache: Any = _SUMMARIZER_UNSET
        # 输入门：规则层（词表 + 独立注入检测）始终生效；已配置模型时叠加 LLM 审核增强。
        self._input_moderator = ContentModerationMiddleware(
            llm_reviewer=LLMReviewer(model_resolver=self._resolve_review_model)
        )

    def _default_agents(self) -> dict[str, Any]:
        """构造缺省子 Agent 集合（R1/R2/R3）。

        Returns:
            agent 名称到实例的映射。
        """
        return {
            "R1": TransferAgent(model_manager=self.model_manager),
            "R2": BillAgent(model_manager=self.model_manager),
            "R3": WealthAgent(model_manager=self.model_manager),
            "R4": CardAgent(model_manager=self.model_manager),
            "R5": SubscriptionAgent(model_manager=self.model_manager),
            "R6": WorkflowAgent(model_manager=self.model_manager),
        }

    def register(self, name: str, agent: Any) -> None:
        """注册一个场景子 Agent（供 R4~R6 接入）。

        Args:
            name: 场景名（如 "R4"）。
            agent: 实现 ``chat(user_input, session_id)`` 的 Agent 实例。
        """
        self.agents[name] = agent

    # ------------------------------------------------------------ LLM 调度器

    def _build_router_model(self) -> Any | None:
        """构造 LLM 调度器使用的模型；无可用模型时返回 None（回退规则路由）。

        优先级：页面运行时配置（model_manager）> 环境变量配置。
        调度器只走对话补全（不带工具），任何构造失败都静默降级。

        Returns:
            实现 ChatModel 协议的模型实例，或 None。
        """
        try:
            if self.model_manager is not None:
                return RuntimeChatModel(self.model_manager, fallback=None)
            config = load_config()
            if config.is_configured:
                return DeepSeekChat(config)
        except Exception:  # noqa: BLE001 - 调度器不可用不阻断规则路由
            pass
        return None

    def _resolve_review_model(self) -> Any | None:
        """解析输入门 LLM 审核使用的模型；不可用时返回 None（降级到规则层）。

        Returns:
            实现 ChatModel 协议的模型实例，或 None。
        """
        try:
            manager = resolve_manager(self.model_manager)
            return manager.resolve() if manager is not None else None
        except Exception:  # noqa: BLE001 - 审核模型不可用不阻断对话
            return None

    def _moderate_input(self, user_input: str, session_id: str = "default") -> dict[str, Any] | None:
        """输入门：对用户输入执行规则层 + LLM 审核，阻断时返回统一契约结果。

        Args:
            user_input: 用户原始输入。
            session_id: 会话 ID（注入/越权命中的熔断零容忍事件按此维度记账）。

        Returns:
            审核未通过时返回阻断结果字典；通过时返回 None。
        """
        result = self._input_moderator.check_with_llm(user_input)
        if not result.passed:
            _record_moderation_violation(result, session_id)
            reply = sanitize_reply(f"抱歉，您的输入未通过内容审核：{result.reason}")
            return {
                "reply": reply,
                "blocked": True,
                "moderation": result.to_dict(),
                "tasks": [],
                "agent": "input_gate",
                "trace": {"router": "input_gate", "plan": []},
            }
        return None

    def _plan(self, user_input: str, history: list[dict[str, Any]] | None = None) -> list[dict[str, str]] | None:
        """LLM 意图分析：结合会话历史，把用户输入拆分为子任务计划。

        让 LLM 输出 JSON 数组，每个元素为 ``{"agent": "R1|R2|R3|common", "task": "..."}``；
        输出无法解析或模型不可用时返回 None，由调用方回退到规则路由。

        修复要点（多轮指代）：调度器现在能看到本会话最近若干轮对话，因此
        「确认撤销」「就那个」「第一笔」这类**依赖上文的输入**能被正确解析，
        并被改写成自包含的 ``task`` 下发——这是截图里第二轮被误路由到通用
        搜索的直接原因（此前只喂当前一句，模型无从判断「撤销」指订阅）。

        Args:
            user_input: 用户自然语言输入。
            history: 本会话历史（精简版，含滚动摘要）；为空表示首轮。

        Returns:
            子任务计划列表；不可用时返回 None。
        """
        if self._router_model is None:
            return None
        capabilities = "\n".join(
            f"- {name}：{desc}"
            for name, desc in _AGENT_CAPABILITIES.items()
            if name in self.agents or name == "common"
        )
        history_block = self._render_history(history)
        prompt = (
            "可用执行模块：\n"
            f"{capabilities}\n"
            f"{history_block}"
            "要求：\n"
            "1. 只输出 JSON 数组，形如 [{\"agent\": \"R1\", \"task\": \"给张三转账100元\"}]，不要输出任何其它文字；\n"
            "2. 即使只有一个子任务，也必须输出数组；\n"
            "3. task 必须完整保留原文中的关键信息（金额、姓名、时间、产品等）；\n"
            "4. 无法归入任何场景的请求选择 common；\n"
            "5. 若当前输入依赖上文（确认/取消/就它/第一笔/验证码/是/好的 等），必须结合"
            "会话历史把 task 改写为自包含的完整指令：补全被指代的对象（订阅名、金额、"
            "收款人、卡片、产品等）与已给出的信息（如 6 位验证码），不要反问、不要只说"
            "「用户确认撤销」。\n"
            f"用户当前输入：{user_input}"
        )
        try:
            response = self._router_model.chat(
                [
                    {"role": "system", "content": SUPERVISOR_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                None,
            )
            text = (response["choices"][0]["message"].get("content") or "").strip()
        except Exception:  # noqa: BLE001 - 模型异常时静默回退规则路由
            return None

        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list):
            return None

        plan: list[dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("agent", "")).strip()
            task = str(item.get("task", "")).strip() or user_input
            if name in self.agents or name == "common":
                plan.append({"agent": name, "task": task})
        return plan or None

    # ------------------------------------------------------------ DAG 计划（D03）

    def _plan_tasks(
        self, user_input: str, router_history: list[dict[str, Any]] | None
    ) -> tuple[list[dict[str, Any]], str]:
        """取得子任务列表（LLM 优先，失败回退规则路由）。

        Args:
            user_input: 用户输入。
            router_history: 调度器可见的历史。

        Returns:
            ``(任务列表, origin)``，``origin`` 为 ``"llm"`` 或 ``"fallback"``。
        """
        tasks = self._plan(user_input, router_history)
        if tasks:
            return list(tasks), "llm"
        target = self.route(user_input)
        agent = target if target in self.agents else "common"
        return [{"agent": agent, "task": user_input}], "fallback"

    def _decorate_tasks(self, tasks: list[Any]) -> list[dict[str, Any]]:
        """补齐节点 ID / 风险分级 / 确认点（仅展示与编排元数据）。

        Args:
            tasks: 原始任务项（字符串或字典）。

        Returns:
            补全后的任务项列表。
        """
        decorated: list[dict[str, Any]] = []
        for index, item in enumerate(tasks):
            raw = {"task": item} if isinstance(item, str) else dict(item)
            agent = str(raw.get("agent") or "common")
            raw["agent"] = agent
            raw["node_id"] = str(raw.get("node_id") or f"n{index + 1}")
            raw.setdefault("risk_level", _AGENT_RISK.get(agent, plan_dag.RISK_GREEN))
            raw.setdefault("requires_confirmation", raw["risk_level"] != plan_dag.RISK_GREEN)
            decorated.append(raw)
        return decorated

    @staticmethod
    def _sequential_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把带显式依赖的任务改写成纯顺序链（环检测失败时的回退模式）。

        Args:
            tasks: 已装饰的任务项。

        Returns:
            去掉 ``depends_on`` 的任务项列表。
        """
        fallback: list[dict[str, Any]] = []
        for item in tasks:
            copy = dict(item)
            copy.pop("depends_on", None)
            copy.pop("edge_kind", None)
            fallback.append(copy)
        return fallback

    def build_plan(
        self,
        user_input: str,
        *,
        session_id: str = "default",
        router_history: list[dict[str, Any]] | None = None,
        tasks: list[Any] | None = None,
    ) -> plan_dag.Plan:
        """构建（或续写为下一版本的）执行计划。

        建图即校验：有环 → 审计 ``plan.invalid_cycle`` 并回退**顺序模式**；
        结构非法 → 审计并降级为单节点 ``common`` 计划。同一会话存在**未完成**
        的旧计划时，本轮视为「用户改约束」，产出 ``version+1`` 的新版本并保留
        旧版（重规划上限 :data:`plan_dag.MAX_REPLANS`）。

        Args:
            user_input: 用户输入（作为计划目标）。
            session_id: 会话 ID（计划按会话保存）。
            router_history: 调度器可见历史。
            tasks: 直接指定任务项（测试/API 用）；``None`` 时走 LLM 调度器。

        Returns:
            计划对象。
        """
        if tasks is None:
            tasks, origin = self._plan_tasks(user_input, router_history)
        else:
            tasks, origin = list(tasks), "llm"
        decorated = self._decorate_tasks(tasks)
        try:
            plan = plan_dag.build_plan(decorated, goal=user_input, origin=origin)
            plan_dag.validate(plan)
        except plan_dag.PlanCycleError as exc:
            plan_dag.audit(
                plan_dag.AUDIT_INVALID_CYCLE,
                goal=user_input,
                nodes=list(exc.nodes),
                detail=str(exc)[:512],
            )
            plan = plan_dag.build_plan(
                self._sequential_tasks(decorated), goal=user_input, origin="fallback"
            )
        except plan_dag.PlanValidationError as exc:
            plan_dag.audit(
                plan_dag.AUDIT_INVALID_CYCLE, goal=user_input, detail=str(exc)[:512]
            )
            plan = plan_dag.build_plan(
                [{"agent": "common", "task": user_input}], goal=user_input, origin="fallback"
            )

        previous = self._plans.get(session_id)
        if (
            previous is not None
            and not previous.finished
            and previous.replan_count < plan_dag.MAX_REPLANS
        ):
            try:
                plan = previous.replanned("user_revised_goal", nodes=plan.nodes)
            except plan_dag.ReplanLimitError:  # 已达上限：开新计划并转人工
                logger.warning("会话 %s 重规划超限，转人工并开启新计划", session_id)
        self._plans[session_id] = plan
        return plan

    def current_plan(self, session_id: str = "default") -> plan_dag.Plan | None:
        """取会话当前计划。

        Args:
            session_id: 会话 ID。

        Returns:
            计划对象或 ``None``。
        """
        return self._plans.get(session_id)

    def replan(
        self,
        session_id: str = "default",
        *,
        reason: str = "tool_unavailable",
        tasks: list[Any] | None = None,
        user_input: str | None = None,
    ) -> plan_dag.Plan:
        """显式重规划：产出 ``version+1`` 新版本，旧版本保留。

        Args:
            session_id: 会话 ID。
            reason: 重规划原因（进审计）。
            tasks: 新任务项；``None`` 时沿用当前节点。
            user_input: 新目标（``None`` 时沿用旧目标）。

        Returns:
            新版本计划。

        Raises:
            plan_dag.PlanError: 会话没有计划。
            plan_dag.ReplanLimitError: 已达重规划上限。
        """
        current = self._plans.get(session_id)
        if current is None:
            raise plan_dag.PlanError(f"会话 {session_id} 没有可重规划的计划")
        nodes = None
        if tasks is not None:
            rebuilt = plan_dag.build_plan(
                self._decorate_tasks(tasks), goal=user_input or current.goal
            )
            nodes = rebuilt.nodes
        fresh = current.replanned(reason, nodes=nodes)
        if user_input:
            fresh.goal = user_input
        self._plans[session_id] = fresh
        return fresh

    def resume_plan(
        self,
        session_id: str = "default",
        *,
        version: int | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> plan_dag.PlanRunResult:
        """按 ``plan_id+version`` 从首个非 ``done`` 节点继续（已完成节点不重放）。

        Args:
            session_id: 会话 ID。
            version: 期望版本；不匹配直接拒绝。
            history: 子 Agent 上下文；``None`` 时读会话记忆。

        Returns:
            执行结果。

        Raises:
            plan_dag.PlanError: 会话没有计划。
        """
        plan = self._plans.get(session_id)
        if plan is None:
            raise plan_dag.PlanError(f"会话 {session_id} 没有可恢复的计划")
        if history is None:
            history = self._recall(session_id)[0]
        outcomes: dict[str, dict[str, Any]] = {}
        executor = plan_dag.PlanExecutor(self._node_runner(session_id, history, outcomes))
        result = executor.resume(plan, version=version)
        self._plans[session_id] = result.plan
        return result

    def rollback_plan(
        self,
        session_id: str = "default",
        *,
        reason: str = "user_cancelled",
    ) -> plan_dag.PlanRunResult:
        """按逆拓扑序补偿本会话计划中已完成的节点。

        Args:
            session_id: 会话 ID。
            reason: 回滚原因（进审计）。

        Returns:
            回滚结果（``rolled_back`` / ``needs_human``）。

        Raises:
            plan_dag.PlanError: 会话没有计划。
        """
        plan = self._plans.get(session_id)
        if plan is None:
            raise plan_dag.PlanError(f"会话 {session_id} 没有可回滚的计划")
        executor = plan_dag.PlanExecutor(
            lambda node: {"status": "ok"},
            gate=self._compensation_gate,
            compensate=self._compensate_node,
        )
        result = executor.rollback(plan, reason=reason, tool_of=self._node_tool)
        self._plans[session_id] = result.plan
        return result

    @staticmethod
    def _node_tool(node: plan_dag.PlanNode) -> str | None:
        """从节点参数里取原写工具名。

        Args:
            node: 节点。

        Returns:
            工具名或 ``None``。
        """
        params = node.params if isinstance(node.params, dict) else {}
        tool = params.get("tool")
        return str(tool) if tool else None

    def _compensation_tool(self, node: plan_dag.PlanNode) -> str | None:
        """解析节点对应的补偿工具名。

        Args:
            node: 节点。

        Returns:
            补偿工具名（无自动化补偿时为 ``None``）。
        """
        rule = plan_dag.compensation_for(self._node_tool(node), node.compensating_action)
        return rule.tool_name if rule else None

    def _plan_gate(self, node: plan_dag.PlanNode) -> dict[str, Any] | None:
        """执行前的熔断门禁（工具层 HITL 之外的第一道闸）。

        Args:
            node: 节点。

        Returns:
            阻断回执或 ``None``。
        """
        tool = self._node_tool(node)
        if not tool:
            return None
        try:
            from common import hitl

            return hitl.breaker_gate(tool)
        except Exception:  # noqa: BLE001 - 门禁自身异常不阻断编排
            logger.debug("计划门禁不可用，跳过 tool=%s", tool)
            return None

    def _compensation_gate(self, node: plan_dag.PlanNode) -> dict[str, Any] | None:
        """补偿动作的熔断门禁（补偿本身也是写操作）。

        Args:
            node: 待补偿节点。

        Returns:
            阻断回执或 ``None``。
        """
        tool = self._compensation_tool(node)
        if not tool:
            return None
        try:
            from common import hitl

            return hitl.breaker_gate(tool)
        except Exception:  # noqa: BLE001 - 门禁自身异常不阻断补偿判定
            return None

    def _invoke_tool(self, agent_name: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """直接调用某模块已注册的写工具（补偿用，绕过 LLM）。

        Args:
            agent_name: 目标模块。
            tool_name: 工具名。
            arguments: 工具参数。

        Returns:
            统一回执字典。

        Raises:
            RuntimeError: 该模块未注册此工具。
        """
        agent = self._resolve_agent(agent_name)
        for tool in getattr(agent, "_tools", None) or []:
            if getattr(tool, "name", None) == tool_name:
                raw = tool.invoke(arguments)
                return {"status": "ok", "tool": tool_name, "message": str(raw)[:500]}
        raise RuntimeError(f"模块 {agent_name} 未注册补偿工具 {tool_name}")

    def _compensate_node(self, node: plan_dag.PlanNode) -> dict[str, Any]:
        """执行单节点补偿（走熔断门禁 + 直接工具调用）。

        Args:
            node: 待补偿节点。

        Returns:
            补偿回执。

        Raises:
            RuntimeError: 无补偿工具或工具不可用（由执行器升级为零容忍失败）。
        """
        tool = self._compensation_tool(node)
        if tool is None:
            raise RuntimeError("没有可用的自动化补偿工具")
        params = node.params if isinstance(node.params, dict) else {}
        arguments = dict(params.get("compensation_args") or {})
        return self._invoke_tool(node.agent, tool, arguments)

    def _node_runner(
        self,
        session_id: str,
        history: list[dict[str, Any]] | None,
        outcomes: dict[str, dict[str, Any]],
    ) -> Any:
        """构造 DAG 执行器使用的节点执行函数。

        Args:
            session_id: 会话 ID。
            history: 子 Agent 上下文。
            outcomes: 节点结果收集器（原地更新）。

        Returns:
            可传给 :class:`plan_dag.PlanExecutor` 的 ``runner``。
        """

        def runner(node: plan_dag.PlanNode) -> dict[str, Any]:
            result = self._dispatch(
                node.agent, self._resolve_agent(node.agent), node.intent, session_id, history
            )
            outcomes[node.node_id] = result
            trace = result.get("trace") or {}
            if result.get("blocked") or trace.get("failed"):
                return {
                    "status": "failed",
                    "error_code": "TASK_FAILED",
                    "message": str(result.get("reply") or "")[:512],
                }
            return {"status": "ok"}

        return runner

    @staticmethod
    def _node_status_from(result: dict[str, Any]) -> str:
        """由子 Agent 结果推断节点状态。

        Args:
            result: 统一契约结果。

        Returns:
            ``done`` 或 ``failed``。
        """
        trace = result.get("trace") or {}
        if result.get("blocked") or trace.get("failed"):
            return plan_dag.STATUS_FAILED
        return plan_dag.STATUS_DONE

    def _skipped_result(self, node: plan_dag.PlanNode) -> dict[str, Any]:
        """为被级联跳过的节点造一条统一契约结果。

        Args:
            node: 节点。

        Returns:
            统一契约结果。
        """
        reason = node.error or "dependency_failed"
        return {
            "reply": f"（因上游任务未成功，已跳过：{node.intent}）",
            "blocked": False,
            "moderation": {},
            "tasks": [],
            "agent": node.agent,
            "trace": {
                "agent": node.agent,
                "tools": [],
                "failed": True,
                "plan_node": node.node_id,
                "plan_status": plan_dag.STATUS_SKIPPED,
                "plan_reason": reason,
            },
        }

    def _needs_human_result(self, node: plan_dag.PlanNode) -> dict[str, Any]:
        """为待人工处理的节点造一条统一契约结果。

        Args:
            node: 节点。

        Returns:
            统一契约结果。
        """
        detail = node.error or "该步骤需要人工确认后继续"
        return {
            "reply": f"（该步骤需要人工处理：{node.intent}——{detail}）",
            "blocked": True,
            "moderation": {},
            "tasks": [],
            "agent": node.agent,
            "trace": {
                "agent": node.agent,
                "tools": [],
                "failed": True,
                "plan_node": node.node_id,
                "plan_status": node.status,
                "plan_reason": detail,
            },
        }

    def _collect_results(
        self, plan: plan_dag.Plan, outcomes: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """按节点顺序组装结果列表（与计划等长同序）。

        Args:
            plan: 计划。
            outcomes: 节点结果。

        Returns:
            统一契约结果列表。
        """
        results: list[dict[str, Any]] = []
        for node in plan.nodes:
            if node.node_id in outcomes:
                results.append(outcomes[node.node_id])
            elif node.status == plan_dag.STATUS_SKIPPED:
                results.append(self._skipped_result(node))
            elif node.status == plan_dag.STATUS_NEEDS_HUMAN:
                results.append(self._needs_human_result(node))
            else:
                results.append(self._skipped_result(node))
        return results

    def _plan_trace(self, plan: plan_dag.Plan, status: str) -> dict[str, Any]:
        """构造 trace 里的 DAG 字段（向后兼容：``plan`` 仍是旧扁平列表）。

        Args:
            plan: 计划。
            status: 计划总体状态。

        Returns:
            trace 片段。
        """
        return {
            "plan": plan.to_legacy_list(),
            "plan_dag": plan.to_dict(),
            "plan_status": status,
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
        }

    # ------------------------------------------------------------ 会话上下文记忆

    @property
    def memory_enabled(self) -> bool:
        """是否启用会话上下文记忆。"""
        return self.memory is not None and bool(getattr(self.memory.config, "enabled", False))

    @staticmethod
    def _render_history(history: list[dict[str, Any]] | None) -> str:
        """把会话历史渲染成调度器 prompt 里的「会话历史」段落。

        Args:
            history: 精简版历史消息（可能含 ``system`` 摘要）。

        Returns:
            可直接拼进 prompt 的文本；无历史时为空串。
        """
        if not history:
            return ""
        lines: list[str] = []
        for message in history:
            role = str(message.get("role") or "")
            content = " ".join(str(message.get("content") or "").split())
            if not content:
                continue
            if role == "system":
                lines.append(f"【此前对话摘要】{content}")
            elif role == "user":
                lines.append(f"用户：{content}")
            elif role == "assistant":
                lines.append(f"助手：{content}")
        if not lines:
            return ""
        return "会话历史（按时间先后，供解析指代；无需复述）：\n" + "\n".join(lines) + "\n"

    def _recall(self, session_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """读取本会话上下文。

        Args:
            session_id: 会话 ID。

        Returns:
            ``(注入子 Agent 的历史, 供调度器的精简历史)``；未启用记忆时均为空列表。
        """
        if not self.memory_enabled:
            return [], []
        try:
            snapshot = self.memory.load(session_id)
            return snapshot.prompt_messages(), snapshot.router_history(self.memory.config)
        except Exception as error:  # noqa: BLE001 - 记忆读取失败不得阻断对话
            print(f"[supervisor] 读取会话上下文失败：{error!r}")
            return [], []

    def _summarizer(self) -> Any:
        """惰性构造摘要器：优先用调度器模型，未配置模型时返回 None（回退规则摘要）。"""
        if self.memory is None:
            return None
        if self._summarizer_cache is _SUMMARIZER_UNSET:
            try:
                self._summarizer_cache = build_llm_summarizer(
                    self._router_model,
                    max_chars=self.memory.config.summary_max_chars,
                )
            except Exception as error:  # noqa: BLE001 - 摘要器不可用则走规则摘要
                print(f"[supervisor] 构造摘要器失败：{error!r}")
                self._summarizer_cache = None
        return self._summarizer_cache

    def _remember(
        self,
        session_id: str,
        user_input: str,
        reply: str,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """写回本轮问答，并在超出预算时自动压缩上下文。

        Args:
            session_id: 会话 ID。
            user_input: 用户原始输入。
            reply: 本轮最终回复。
            agent: 命中的子 Agent 名称。

        Returns:
            trace 中的 ``memory`` 字段；未启用记忆时为空字典。
        """
        if not self.memory_enabled:
            return {}
        try:
            self.memory.remember(session_id, user_input, reply, agent=agent)
            snapshot = self.memory.maintain(session_id, summarizer=self._summarizer())
        except Exception as error:  # noqa: BLE001 - 记忆写入失败不得影响已生成的回复
            print(f"[supervisor] 会话记忆写入失败：{error!r}")
            return {"error": f"{type(error).__name__}: {error}"}
        return snapshot.to_trace()

    def _call_agent(
        self,
        agent: Any,
        user_input: str,
        session_id: str,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """调用子 Agent，并在其支持时注入共享上下文。

        子 Agent 的 ``chat`` 若声明了 ``history`` 关键字（R1~R6、common 均已支持），
        就传入本会话的统一上下文；否则退回旧签名，保持对自定义/第三方 Agent 的兼容。

        Args:
            agent: 子 Agent 实例。
            user_input: 本轮用户输入（可能是调度器改写后的自包含任务）。
            session_id: 会话 ID。
            history: 共享上下文消息列表。

        Returns:
            子 Agent 的统一契约结果字典。
        """
        chat = agent.chat
        if history:
            try:
                parameters = inspect.signature(chat).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "history" in parameters:
                return chat(user_input, session_id, history=history)
        return chat(user_input, session_id)

    # ------------------------------------------------------------ 规则路由（兜底）

    def route(self, user_input: str) -> str | None:
        """规则路由：关键词 + 正则兜底，返回目标子 Agent 名称。

        仅在 LLM 调度器不可用时使用；无法识别时返回 None（交由 common 处理）。

        Args:
            user_input: 用户自然语言输入。

        Returns:
            目标 Agent 名称；无法识别时返回 None。
        """
        for name, keywords in self.route_keywords.items():
            if any(kw in user_input for kw in keywords):
                return name

        for name, patterns in _ROUTE_PATTERNS.items():
            if any(re.search(pattern, user_input) for pattern in patterns):
                return name
        return None

    # ------------------------------------------------------------ 编排与汇总

    def chat(self, user_input: str, session_id: str = "default") -> dict[str, Any]:
        """处理一条用户消息：LLM 拆分任务 → 逐个分发到子 Agent → 汇总返回。

        流程：
            1. LLM 意图分析得到子任务计划；不可用时回退规则路由（单任务）；
            2. 按计划逐个调用对应子 Agent（R1/R2/R3/common）；
            3. 单任务直接透传结果，多任务合并为一条分节回复。

        Args:
            user_input: 用户自然语言输入。
            session_id: 会话 ID。

        Returns:
            统一契约字典：
                reply / blocked / moderation / tasks / trace（公共字段）
                agent: 命中的子 Agent 名称（R1/R2/R3/common/multi）。
                trace.router: 本次路由方式（"llm" 或 "rule"）。
                trace.plan:   本次执行的子任务计划。
                trace.memory: 本轮结束后的上下文占用与压缩状态（启用记忆时）。
        """
        blocked = self._moderate_input(user_input, session_id)
        if blocked is not None:
            return blocked
        history, router_history = self._recall(session_id)
        plan = self.build_plan(user_input, session_id=session_id, router_history=router_history)
        router_mode = "llm" if plan.origin == "llm" else "rule"

        # D03：按 DAG 语义执行（拓扑序 / 失败分类 / 级联跳过 / 幂等键防重放）。
        outcomes: dict[str, dict[str, Any]] = {}
        executor = plan_dag.PlanExecutor(self._node_runner(session_id, history, outcomes))
        run = executor.run(plan)
        plan = run.plan
        results = self._collect_results(plan, outcomes)

        merged = results[0] if len(results) == 1 else self._merge_results(plan.to_legacy_list(), results)
        trace = dict(merged.get("trace") or {})
        trace["router"] = router_mode
        trace.update(self._plan_trace(plan, run.status))
        merged["trace"] = trace
        # 安全网：无论哪条分支（单任务透传 / 多任务合并 / 兜底失败），下发给用户前
        # 再过一次净化。``sanitize_reply`` 幂等，纯文本逐字符不变；trace/plan 等
        # 结构化字段一律不动。
        merged["reply"] = sanitize_reply(merged.get("reply") or "")
        # 写回本轮问答（超出预算时顺带自动压缩），失败只记录不影响已生成的回复。
        memory_trace = self._remember(session_id, user_input, merged["reply"], merged.get("agent"))
        if memory_trace:
            trace = dict(merged.get("trace") or {})
            trace["memory"] = memory_trace
            merged["trace"] = trace
        return merged

    def chat_stream(self, user_input: str, session_id: str = "default"):
        """流式处理一条用户消息：token 级实时产出 + 任务级进度通知。

        事件格式（SSE data 载荷）：
            - ``{"type":"plan","plan":list,"router":str}``：LLM 调度器输出的任务计划。
            - ``{"type":"task_start","index":int,"total":int,"agent":str,"task":str}``：开始执行子任务。
            - ``{"type":"token","index":int,"content":str}``：LLM 增量文本（index 对应任务序号）。
            - ``{"type":"task_done","index":int,"total":int,"agent":str,"result":dict}``：子任务完成。
            - ``{"type":"done","result":dict}``：全部任务完成后的合并结果（用于落库）。
            - ``{"type":"error","error":str}``：执行异常。

        Args:
            user_input: 用户自然语言输入。
            session_id: 会话 ID。

        Note:
            所有 ``token`` 片段都经过 :class:`ReplyStreamFilter` 有状态净化，
            ``task_done`` / ``done`` 里的 ``result["reply"]`` 经过
            :func:`sanitize_reply` 净化：前端拿到的永远是纯文本，
            **事件类型名、字段名与顺序语义保持不变**。

        Yields:
            事件字典。
        """
        blocked = self._moderate_input(user_input, session_id)
        if blocked is not None:
            yield {"type": "plan", "plan": [], "router": "input_gate"}
            yield {"type": "done", "result": blocked}
            return
        history, router_history = self._recall(session_id)
        plan = self.build_plan(user_input, session_id=session_id, router_history=router_history)
        router_mode = "llm" if plan.origin == "llm" else "rule"

        # D03：``plan`` 事件保留旧的扁平列表 ``plan``，并新增 nodes/edges/版本字段（只增不改）。
        yield {
            "type": "plan",
            "plan": plan.to_legacy_list(),
            "router": router_mode,
            **plan.to_event(),
        }

        results: list[dict[str, Any]] = []
        outcomes: dict[str, dict[str, Any]] = {}
        total = len(plan.nodes)
        # 每个子任务 index 一个流式净化器：模型会把 ``**`` / 围栏 ``` 拆到不同 chunk，
        # 逐块直发会把半截标记漏给前端，故由 ReplyStreamFilter 持有未完成标记，
        # 等 flush() 时再收尾。任务结束后立即丢弃该 index 的过滤器。
        filters: dict[int, ReplyStreamFilter] = {}
        for index, node_id in enumerate(list(plan.node_ids)):
            node = plan.node(node_id)
            if node.status in plan_dag.TERMINAL_STATUSES:
                continue
            if any(plan.node(dep).status != plan_dag.STATUS_DONE for dep in node.depends_on):
                # 上游未成功 → 级联跳过（与批处理路径同一套语义）。
                node.status = plan_dag.STATUS_SKIPPED
                node.error = "dependency_failed:" + ",".join(node.depends_on)
                skipped = self._skipped_result(node)
                outcomes[node_id] = skipped
                results.append(skipped)
                plan_dag.audit(
                    plan_dag.AUDIT_NODE_SKIPPED,
                    plan_id=plan.plan_id,
                    version=plan.version,
                    node_id=node_id,
                    reason=node.error,
                )
                yield plan_dag.node_status_event(plan, node)
                yield {
                    "type": "task_done",
                    "index": index,
                    "total": total,
                    "agent": node.agent,
                    "result": skipped,
                    "skipped": True,
                }
                continue
            node.status = plan_dag.STATUS_RUNNING
            yield plan_dag.node_status_event(plan, node)
            yield {
                "type": "task_start",
                "index": index,
                "total": total,
                "agent": node.agent,
                "task": node.intent,
            }
            agent = self._resolve_agent(node.agent)
            stream_fn = getattr(agent, "chat_stream", None)
            if stream_fn is None:
                # 该 Agent 无流式能力，回退同步调用
                result = self._dispatch(node.agent, agent, node.intent, session_id, history)
                results.append(result)
            else:
                task_result: dict[str, Any] | None = None
                stream_filter = ReplyStreamFilter()
                filters[index] = stream_filter
                try:
                    for ev in self._call_agent_stream(agent, node.intent, session_id, history):
                        if ev["type"] == "token":
                            # 只在净化后确有内容时下发；空片段会让前端闪空气泡。
                            piece = stream_filter.feed(ev.get("content") or "")
                            if piece:
                                yield {**ev, "type": "token", "index": index, "content": piece}
                        elif ev["type"] == "final":
                            task_result = ev["result"]
                        elif ev["type"] == "error":
                            task_result = self._failure_result(RuntimeError(ev["error"]))
                    tail = stream_filter.flush()
                finally:
                    filters.pop(index, None)
                if tail:
                    # 收尾尾巴必须在 task_done 之前下发，顺序语义与原来一致。
                    yield {"type": "token", "index": index, "content": tail}
                results.append(
                    self._wrap(node.agent, task_result) if task_result is not None
                    else self._wrap(node.agent, self._failure_result(RuntimeError("无结果")))
                )

            # task_done 的 result 只改 reply，其它键（blocked/tasks/trace/agent…）原样保留。
            results[-1]["reply"] = sanitize_reply(results[-1].get("reply") or "")
            outcomes[node_id] = results[-1]
            node.status = self._node_status_from(results[-1])
            if node.status == plan_dag.STATUS_FAILED:
                node.error = str(results[-1].get("reply") or "")[:200] or "task_failed"
            yield plan_dag.node_status_event(plan, node)
            yield {
                "type": "task_done",
                "index": index,
                "total": total,
                "agent": node.agent,
                "result": results[-1],
            }
            if node.status == plan_dag.STATUS_FAILED:
                # 下游全部跳过（事件逐条下发，前端可即时置灰）。
                for ev in plan_dag.cascade_skip(plan, node):
                    yield ev
                # 被级联跳过的节点没有 task_start/token，需补一条 ``task_done``
                # （带 ``skipped: true``）让前端关闭对应进度条，否则进度会卡住。
                for skipped_node in plan.nodes:
                    if skipped_node.status != plan_dag.STATUS_SKIPPED:
                        continue
                    if skipped_node.node_id in outcomes:
                        continue
                    skipped = self._skipped_result(skipped_node)
                    outcomes[skipped_node.node_id] = skipped
                    results.append(skipped)
                    yield {
                        "type": "task_done",
                        "index": plan.node_ids.index(skipped_node.node_id),
                        "total": total,
                        "agent": skipped_node.agent,
                        "result": skipped,
                        "skipped": True,
                    }

        merged = results[0] if len(results) == 1 else self._merge_results(plan.to_legacy_list(), results)
        trace = dict(merged.get("trace") or {})
        trace["router"] = router_mode
        trace.update(self._plan_trace(plan, plan_dag.final_status(plan)))
        merged["trace"] = trace
        # 与 chat() 同一道安全网：落库 / 前端最终气泡用的都是这条 reply。
        merged["reply"] = sanitize_reply(merged.get("reply") or "")
        # 流式路径同样写回上下文：压缩触发的摘要调用已在 build_llm_summarizer 内部
        # 抑制 token 下发，因此不会污染本次流。
        memory_trace = self._remember(session_id, user_input, merged["reply"], merged.get("agent"))
        if memory_trace:
            trace = dict(merged.get("trace") or {})
            trace["memory"] = memory_trace
            merged["trace"] = trace
        yield {"type": "done", "result": merged}

    def _resolve_agent(self, name: str) -> Any:
        """按名称取子 Agent 实例；未注册的名称回落到 common。

        Args:
            name: 计划中的 Agent 名称。

        Returns:
            子 Agent 实例。
        """
        if name == "common":
            return self.common_agent
        return self.agents.get(name) or self.common_agent

    def _merge_results(self, plan: list[dict[str, str]], results: list[dict[str, Any]]) -> dict[str, Any]:
        """把多个子任务的结果合并为一条统一契约回复。

        回复正文一律是**纯文本**（前端气泡用 ``textContent`` 渲染，不解析
        Markdown）：每段子回复先过 :func:`sanitize_reply`，段落之间空一行。

        - 只有 1 个子任务时**直接返回该子任务的 reply**，不添加任何前缀，
          保持用户观感自然（与单任务直接透传路径一致）；
        - 多子任务时每段以「关于「任务描述」：」自然语言引出，不再使用
          ``## 标题`` / ``【任务N】`` 这类结构化标记。

        Args:
            plan: 子任务计划。
            results: 各子任务的结果字典（与 plan 等长、同序）。

        Returns:
            合并后的统一契约字典（agent 为 "multi"）。
        """
        replies = [sanitize_reply(result.get("reply") or "") for result in results]
        if len(replies) == 1:
            merged_reply = replies[0]
        else:
            sections = [
                f"关于「{item['task']}」：\n{reply}" for item, reply in zip(plan, replies)
            ]
            merged_reply = "\n\n".join(sections)
        return {
            "reply": merged_reply,
            "blocked": any(bool(result.get("blocked")) for result in results),
            "moderation": {},
            "tasks": [task for result in results for task in result.get("tasks", [])],
            "agent": "multi",
            "trace": {"agents": [result.get("agent") for result in results]},
        }

    def _dispatch(
        self,
        agent_name: str,
        agent: Any,
        user_input: str,
        session_id: str,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """调用子 Agent 并汇总结果；模型/工具异常统一转为可读回复。

        页面填写了不可用的 Base URL 或过期 Key 时，若让异常直接抛出，前端只会
        收到一个 500 与一段堆栈。这里把异常收敛为一条自然语言回复，同时保留
        ``trace.error`` 供排障，保证「对话不崩、错误可读」。

        Args:
            agent_name: 目标子 Agent 名称。
            agent: 子 Agent 实例。
            user_input: 用户自然语言输入（调度器改写后的自包含任务）。
            session_id: 会话 ID。
            history: 本会话共享上下文；子 Agent 支持时注入。

        Returns:
            统一契约字典。
        """
        try:
            result = self._call_agent(agent, user_input, session_id, history)
        except Exception as error:  # noqa: BLE001 - 顶层兜底，避免 500 直接暴露给用户
            return self._wrap(agent_name, self._failure_result(error))
        return self._wrap(agent_name, result)

    def _call_agent_stream(
        self,
        agent: Any,
        user_input: str,
        session_id: str,
        history: list[dict[str, Any]] | None = None,
    ) -> Any:
        """流式调用子 Agent，并在其支持时注入共享上下文。

        Args:
            agent: 子 Agent 实例。
            user_input: 本轮用户输入。
            session_id: 会话 ID。
            history: 共享上下文消息列表。

        Returns:
            子 Agent ``chat_stream`` 返回的事件迭代器。
        """
        stream_fn = agent.chat_stream
        if history:
            try:
                parameters = inspect.signature(stream_fn).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "history" in parameters:
                return stream_fn(user_input, session_id, history=history)
        return stream_fn(user_input, session_id)

    def _failure_result(self, error: Exception) -> dict[str, Any]:
        """把子 Agent 抛出的异常转换为统一契约的失败结果。

        Args:
            error: 子 Agent 抛出的异常。

        Returns:
            含 reply / blocked / moderation / tasks / trace 的结果字典。
        """
        from common.model_manager import current_secrets, describe_error

        # 部分网关会在错误信息里回显收到的 Key；reply 与 trace 都会回到前端，
        # 因此必须把密钥传进 describe_error，让它「先擦除、再截断」——
        # 若先截断再擦除，截断点落在 Key 中间时前缀会存活下来。
        secrets = current_secrets(self.model_manager)
        # 错误详情会同时进入 reply 与 trace.error_detail，两者都会被前端展示：
        # 这里先净化成纯文本（错误信息里偶尔夹带网关回显的 Markdown / JSON 片段）。
        detail = sanitize_reply(describe_error(error, secrets))
        hint = "请检查「模型设置」中的 Base URL、API Key 与模型名是否正确。"
        if isinstance(error, RuntimeError) and "API Key" in detail:
            reply = f"当前未配置可用的模型：{detail}"
        else:
            reply = f"模型调用失败，未能完成本次请求：{detail}\n{hint}"
        return {
            "reply": sanitize_reply(reply),
            "blocked": False,
            "moderation": {},
            "tasks": [],
            "trace": {"error": type(error).__name__, "error_detail": detail, "failed": True},
        }

    def _wrap(self, agent_name: str, result: dict[str, Any]) -> dict[str, Any]:
        """为子 Agent 结果附加路由与模型信息，构成统一返回契约。

        Args:
            agent_name: 命中的子 Agent 名称。
            result: 子 Agent 返回的结果字典。

        Returns:
            附加 ``agent`` 字段后的结果字典。
        """
        wrapped: dict[str, Any] = dict(result)
        wrapped["agent"] = agent_name
        trace = dict(wrapped.get("trace") or {})
        trace["agent"] = agent_name
        # 子 Agent 未注入管理器时，由总 Agent 补齐模型模式，保证前端始终可展示
        if "model_mode" not in trace:
            trace.update(trace_info(self.model_manager))
        wrapped["trace"] = trace
        return wrapped


__all__ = ["SupervisorAgent", "_ROUTE_KEYWORDS"]
