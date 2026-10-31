"""Internal probe: fixed graph interrupt/resume and closed mock/financial surface.

旧架构 ``packages.orchestration.probe.FixedProbeRuntime`` / ``LocalProbeModel``
已删除，等价对象映射到：

    - langgraph ``interrupt`` / ``Command(resume=...)`` 的固定图流程
    - ``common.llm.MockChat``（离线、确定性、绝不执行财务副作用）
    - ``db.idempotency.fingerprint``（请求指纹 / 防篡改）
    - ``common.model_manager.redact_secrets``（凭据擦除）
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from common.llm import ChatModel, MockChat
from common.model_manager import redact_secrets
from db.idempotency import IdempotencyError, fingerprint
from R1 import ALL_TOOLS


class ProbeState(TypedDict, total=False):
    value: int
    result: int


def _build_probe(saver: MemorySaver):
    graph = StateGraph(ProbeState)

    def pause(state: ProbeState) -> dict:
        interrupted = interrupt({"kind": "s0_probe", "value": state["value"]})
        return {"result": state["value"] + int(interrupted)}

    graph.add_node("pause", pause)
    graph.add_edge(START, "pause")
    graph.add_edge("pause", END)
    return graph.compile(checkpointer=saver)


def test_local_mock_deadline_rejects_late_candidate_and_records_actual_calls(monkeypatch):
    # 离线 Mock：不建立真实 OpenAI 客户端，绝不发起网络请求
    model = MockChat()
    assert not hasattr(model, "_client")

    first = model.chat([{"role": "user", "content": "未命中任何路由的提问"}])
    second = model.chat([{"role": "user", "content": "未命中任何路由的提问"}])
    # 确定性：相同输入得到完全相同的输出（可回放、可核查）
    assert first == second
    assert first["choices"][0]["finish_reason"] == "stop"
    assert first["choices"][0]["message"]["role"] == "assistant"

    # 请求指纹稳定且键序无关
    assert fingerprint({"tenant": "demo-tenant", "amount": 1}) == fingerprint(
        {"amount": 1, "tenant": "demo-tenant"}
    )


def test_fixed_graph_pause_resume_and_zero_financial_tools():
    saver = MemorySaver()
    graph = _build_probe(saver)
    config = {"configurable": {"thread_id": "probe-fixed"}}

    graph.invoke({"value": 4}, config)
    state = graph.get_state(config)
    assert state.next == ("pause",)
    assert state.tasks[0].interrupts[0].value["value"] == 4

    result = graph.invoke(Command(resume=3), config)
    assert result == {"value": 4, "result": 7}
    assert graph.get_state(config).next == ()

    # 工具集封闭：只有转账/搜索工具，不存在任何 execute / resume 财务执行工具
    tool_names = {tool.name for tool in ALL_TOOLS}
    assert not any("execute" in name or "resume" in name for name in tool_names)


@pytest.mark.parametrize("candidate", ["姓名", "手机号", "备注", "定时", "AA", "收款", "搜索", "转账"])
def test_mock_cannot_change_route_authorization_or_execute(candidate):
    routing = {
        candidate: {"name": "transfer_by_name", "arguments": {"amount": 1.0, "recipient_name": "张三"}}
    }
    model = MockChat(routing=routing)
    response = model.chat([{"role": "user", "content": candidate}])
    tool_calls = response["choices"][0]["message"].get("tool_calls") or []
    assert len(tool_calls) == 1
    name = tool_calls[0]["function"]["name"]
    # 路由只产出固定工具名，绝不产出 execute / resume
    assert name == "transfer_by_name"
    assert "execute" not in name and "resume" not in name


def test_mock_parameters_are_fixed_and_invalid_envelope_rejected(monkeypatch):
    model = MockChat()
    # 空路由 → 纯文本回复
    empty = model.chat([{"role": "user", "content": "hello"}])
    assert empty["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in empty["choices"][0]["message"]

    # 命中路由 → tool_calls 信封固定
    routed = MockChat(routing={"转账": {"name": "smart_transfer", "arguments": {"amount": 1.0}}})
    hit = routed.chat([{"role": "user", "content": "帮我转账"}])
    assert hit["choices"][0]["finish_reason"] == "tool_calls"
    assert hit["choices"][0]["message"]["content"] is None

    # 协议信封：MockChat 满足 ChatModel 协议，普通对象不满足
    assert isinstance(MockChat(), ChatModel)
    assert not isinstance(object(), ChatModel)


def test_prompt_digest_refuses_tampering_and_unsupported_fields(tmp_path):
    payload = {"prompt": "transfer", "amount": 100}
    digest = fingerprint(payload)
    assert fingerprint({"amount": 100, "prompt": "transfer"}) == digest
    # 内容篡改 → 指纹变化
    assert fingerprint({"prompt": "transfer", "amount": 101}) != digest
    # 非 JSON 可序列化字段 → 拒绝
    with pytest.raises(IdempotencyError):
        fingerprint({1, 2, 3})


@pytest.mark.parametrize("value", [True, False, {}, {"kind": "approval"}])
def test_resume_is_not_boolean_approval_or_arbitrary_object(value):
    saver = MemorySaver()
    graph = _build_probe(saver)
    config = {"configurable": {"thread_id": f"resume-{value!r}"}}
    # 未挂起中断的线程凭空 resume → 拒绝（恢复必须来自真实中断点）
    with pytest.raises(KeyError):
        graph.invoke(Command(resume=value), config)


def test_checkpoint_version_change_and_extra_identity_are_rejected():
    base = {"version": 1, "tenant": "demo-tenant"}
    digest = fingerprint(base)
    assert fingerprint({"version": 2, "tenant": "demo-tenant"}) != digest
    assert fingerprint({"version": 1, "tenant": "demo-tenant", "identity": "other"}) != digest

    secret = "sk-live-1234567890"
    cleaned = redact_secrets(f"key={secret}", [secret])
    assert secret not in cleaned
    assert cleaned == "key=***"
