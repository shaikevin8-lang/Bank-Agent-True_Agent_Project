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
import re
from typing import Any

from common.agent import CommonAgent
from common.config import load_config
from common.llm import DeepSeekChat
from common.memory import SessionMemory, build_llm_summarizer
from common.model_manager import RuntimeChatModel, trace_info
from common.prompts import SUPERVISOR_SYSTEM_PROMPT
from common.text_format import ReplyStreamFilter, sanitize_reply
from R1 import TransferAgent
from R2 import BillAgent
from R3 import WealthAgent
from R4 import CardAgent
from R5 import SubscriptionAgent
from R6 import WorkflowAgent

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
        self._router_model = self._build_router_model()
        self._summarizer_cache: Any = _SUMMARIZER_UNSET

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
        history, router_history = self._recall(session_id)
        plan = self._plan(user_input, router_history)
        router_mode = "llm" if plan else "rule"
        if not plan:
            target = self.route(user_input)
            plan = [{"agent": target if target in self.agents else "common", "task": user_input}]

        results = [
            self._dispatch(item["agent"], self._resolve_agent(item["agent"]), item["task"], session_id, history)
            for item in plan
        ]

        merged = results[0] if len(results) == 1 else self._merge_results(plan, results)
        trace = dict(merged.get("trace") or {})
        trace["router"] = router_mode
        trace["plan"] = plan
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
        history, router_history = self._recall(session_id)
        plan = self._plan(user_input, router_history)
        router_mode = "llm" if plan else "rule"
        if not plan:
            target = self.route(user_input)
            plan = [{"agent": target if target in self.agents else "common", "task": user_input}]

        yield {"type": "plan", "plan": plan, "router": router_mode}

        results: list[dict[str, Any]] = []
        # 每个子任务 index 一个流式净化器：模型会把 ``**`` / 围栏 ``` 拆到不同 chunk，
        # 逐块直发会把半截标记漏给前端，故由 ReplyStreamFilter 持有未完成标记，
        # 等 flush() 时再收尾。任务结束后立即丢弃该 index 的过滤器。
        filters: dict[int, ReplyStreamFilter] = {}
        for index, item in enumerate(plan):
            yield {
                "type": "task_start",
                "index": index,
                "total": len(plan),
                "agent": item["agent"],
                "task": item["task"],
            }
            agent = self._resolve_agent(item["agent"])
            stream_fn = getattr(agent, "chat_stream", None)
            if stream_fn is None:
                # 该 Agent 无流式能力，回退同步调用
                result = self._dispatch(item["agent"], agent, item["task"], session_id, history)
                results.append(result)
            else:
                task_result: dict[str, Any] | None = None
                stream_filter = ReplyStreamFilter()
                filters[index] = stream_filter
                try:
                    for ev in self._call_agent_stream(agent, item["task"], session_id, history):
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
                    self._wrap(item["agent"], task_result) if task_result is not None
                    else self._wrap(item["agent"], self._failure_result(RuntimeError("无结果")))
                )

            # task_done 的 result 只改 reply，其它键（blocked/tasks/trace/agent…）原样保留。
            results[-1]["reply"] = sanitize_reply(results[-1].get("reply") or "")
            yield {
                "type": "task_done",
                "index": index,
                "total": len(plan),
                "agent": item["agent"],
                "result": results[-1],
            }

        merged = results[0] if len(results) == 1 else self._merge_results(plan, results)
        trace = dict(merged.get("trace") or {})
        trace["router"] = router_mode
        trace["plan"] = plan
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
