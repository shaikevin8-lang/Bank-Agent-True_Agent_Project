"""总 Agent（Supervisor）：R1~R6 的统一入口与意图路由。

职责（对应基座文档「图状态只保存路由/澄清/计划」）：
    1. 识别用户意图，路由到对应的场景子 Agent（R1 转账 / R2 账单 / ...）；
    2. 未命中任何场景时，交由通用搜索 Agent（CommonAgent）处理（天气、汇率等通用问题）；
    3. 汇总子 Agent 结果，返回统一契约结构；
    4. 不承载任何转账/账单业务规则——业务在子 Agent 内，安全/事实/持久化在共享底座。

三路分发：
    - 涉及转账 → R1（TransferAgent）
    - 涉及账单 → R2（BillAgent）
    - 其余（搜索/寒暄/通用问答）→ common（CommonAgent，走 Tavily 搜索 + LLM）

[前后端接入点] 前端/后端只依赖本类即可，无需感知子 Agent 细节。

[路由接入点] 当前为规则路由（关键词），已预留 :meth:`_llm_route` 钩子，
后续可接入 LLM 意图分类替换规则路由，无需改动 :meth:`chat` 主流程。
"""

from __future__ import annotations

from typing import Any

from common.agent import CommonAgent
from common.model_manager import trace_info
from R1 import TransferAgent
from R2 import BillAgent

# 规则路由关键词表：命中即路由到对应子 Agent（R3~R6 追加条目即可扩展）
_ROUTE_KEYWORDS: dict[str, list[str]] = {
    "R1": ["转账", "汇款", "收款", "AA", "转给", "定时", "余额", "姓名", "手机号", "备注"],
    "R2": ["账单", "消费", "分类", "统计", "异常", "月报", "年报", "月度", "年度", "记录", "支出"],
}


class SupervisorAgent:
    """总 Agent：统一入口 + 意图路由 + 结果汇总。"""

    def __init__(
        self,
        agents: dict[str, Any] | None = None,
        route_keywords: dict[str, list[str]] | None = None,
        common_agent: Any | None = None,
        model_manager: Any | None = None,
    ) -> None:
        """初始化总 Agent。

        Args:
            agents: 场景子 Agent 映射，形如 ``{"R1": TransferAgent(), "R2": BillAgent()}``；
                    缺省时按默认配置构造 R1/R2。
            route_keywords: 规则路由关键词表；缺省使用内置 ``_ROUTE_KEYWORDS``。
            common_agent: 通用搜索 Agent（处理未命中 R1/R2 的输入）；缺省构造 CommonAgent。
            model_manager: 运行时模型配置管理器；注入后缺省子 Agent 使用可热切换的模型，
                并在返回结果的 trace 中附带当前模型模式。
        """
        self.model_manager = model_manager
        self.agents = agents if agents is not None else self._default_agents()
        self.route_keywords = route_keywords or _ROUTE_KEYWORDS
        self.common_agent = common_agent or CommonAgent(model_manager=model_manager)

    def _default_agents(self) -> dict[str, Any]:
        """构造缺省子 Agent 集合（R1/R2）。

        Returns:
            agent 名称到实例的映射。
        """
        return {
            "R1": TransferAgent(model_manager=self.model_manager),
            "R2": BillAgent(model_manager=self.model_manager),
        }

    def register(self, name: str, agent: Any) -> None:
        """注册一个场景子 Agent（供 R3~R6 接入）。

        Args:
            name: 场景名（如 "R3"）。
            agent: 实现 ``chat(user_input, session_id)`` 的 Agent 实例。
        """
        self.agents[name] = agent

    def route(self, user_input: str) -> str | None:
        """识别用户意图并返回目标子 Agent 名称。

        优先尝试 LLM 路由（未启用时返回 None），回退规则关键词路由。

        Args:
            user_input: 用户自然语言输入。

        Returns:
            目标 Agent 名称；无法识别时返回 None。
        """
        llm_target = self._llm_route(user_input)
        if llm_target and llm_target in self.agents:
            return llm_target

        for name, keywords in self.route_keywords.items():
            if any(kw in user_input for kw in keywords):
                return name
        return None

    def _llm_route(self, user_input: str) -> str | None:
        """[路由接入点] 基于 LLM 的意图分类路由（当前未实现）。

        Args:
            user_input: 用户输入。

        Returns:
            目标 Agent 名称；未启用时返回 None。
        """
        # 预留：接入 LLM 意图分类后，返回 "R1" / "R2" / ... 之一
        return None

    def chat(self, user_input: str, session_id: str = "default") -> dict[str, Any]:
        """处理一条用户消息：路由到子 Agent 并汇总返回。

        路由逻辑：
            - 命中 R1/R2 关键词 → 交给对应子 Agent；
            - 未命中 → 交给通用搜索 Agent（CommonAgent）处理（天气、汇率、寒暄等）。

        Args:
            user_input: 用户自然语言输入。
            session_id: 会话 ID。

        Returns:
            统一契约字典：
                reply / blocked / moderation / tasks / trace（公共字段）
                agent: 命中的子 Agent 名称（R1/R2/common）。
        """
        target = self.route(user_input)
        if target is None or target not in self.agents:
            return self._dispatch("common", self.common_agent, user_input, session_id)

        return self._dispatch(target, self.agents[target], user_input, session_id)

    def _dispatch(
        self, agent_name: str, agent: Any, user_input: str, session_id: str
    ) -> dict[str, Any]:
        """调用子 Agent 并汇总结果；模型/工具异常统一转为可读回复。

        页面填写了不可用的 Base URL 或过期 Key 时，若让异常直接抛出，前端只会
        收到一个 500 与一段堆栈。这里把异常收敛为一条自然语言回复，同时保留
        ``trace.error`` 供排障，保证「对话不崩、错误可读」。

        Args:
            agent_name: 目标子 Agent 名称。
            agent: 子 Agent 实例。
            user_input: 用户自然语言输入。
            session_id: 会话 ID。

        Returns:
            统一契约字典。
        """
        try:
            result = agent.chat(user_input, session_id)
        except Exception as error:  # noqa: BLE001 - 顶层兜底，避免 500 直接暴露给用户
            return self._wrap(agent_name, self._failure_result(error))
        return self._wrap(agent_name, result)

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
        detail = describe_error(error, secrets)
        hint = "请检查「模型设置」中的 Base URL、API Key 与模型名是否正确。"
        if isinstance(error, RuntimeError) and "API Key" in detail:
            reply = f"当前未配置可用的模型：{detail}"
        else:
            reply = f"模型调用失败，未能完成本次请求：{detail}\n{hint}"
        return {
            "reply": reply,
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
