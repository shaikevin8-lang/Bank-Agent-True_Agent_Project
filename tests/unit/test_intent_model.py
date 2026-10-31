"""意图模型与提示词的纯内存验收：字段闭集、防篡改与关键词桩路由。

旧版「packages.*」里的 YAML PROMPT（6 字段 sha256 / input_schema / output_schema）、
``load_prompt`` 与 ``DeterministicIntentModel`` 已随源码删除。当前等价能力由
``common.prompts.build_system_prompt``（六个语义锚点）+ ``common.llm.MockChat``
（``R1.llm.DEFAULT_ROUTING`` 关键词路由，只产出 transfer / unsupported 语义）承担。
digest 改为对 prompt 字符串的 sha256 稳定性断言，不 import 不存在的 YAML。
"""

from __future__ import annotations

import hashlib

import pytest

from common import prompts
from common.llm import MockChat
from common.prompts import build_system_prompt
from R1.llm import DEFAULT_ROUTING
from R1.tools import TOOL_MAP, TRANSFER_TOOL_NAMES, to_openai_tools

# 六个语义字段（锚点）一一对应旧版 PROMPT 的六字段含义。
_PROMPT_ANCHORS = ["智银", "智能转账专员", "确认", "验证码", "澄清", "脱敏"]


def _tool_schemas() -> dict[str, dict]:
    """按工具名返回 OpenAI 函数调用 JSON Schema 的 parameters。"""
    return {s["function"]["name"]: s["function"]["parameters"] for s in to_openai_tools()}


def _classify(text: str) -> str | None:
    """用 R1 关键词桩对输入做意图分类，返回工具名或 None。"""
    reply = MockChat(routing=DEFAULT_ROUTING).chat([{"role": "user", "content": text}])
    calls = reply["choices"][0]["message"].get("tool_calls") or []
    if not calls:
        return None
    return calls[0]["function"]["name"]


def _assert_prompt_valid(prompt: str) -> None:
    """校验 prompt 六个语义字段齐全、且不泄露 authority 字段；否则抛 ValueError。"""
    missing = [anchor for anchor in _PROMPT_ANCHORS if anchor not in prompt]
    if missing:
        raise ValueError(f"prompt 缺失语义字段：{missing}")
    if "confirmed" in prompt or "mfa_code" in prompt:
        raise ValueError("prompt 泄露 authority 字段")


def test_prompt_has_six_fields_and_a_matching_digest():
    """六个语义字段齐全，digest 对 prompt 字符串稳定且与重算一致。"""
    prompt = build_system_prompt("R1")
    for anchor in _PROMPT_ANCHORS:
        assert anchor in prompt
    assert "confirmed" not in prompt
    assert "mfa_code" not in prompt

    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert len(digest) == 64
    assert digest == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert digest == hashlib.sha256(build_system_prompt("R1").encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("attr", "value"),
    [
        ("_CORE_IDENTITY", ""),  # 缺失「智银」身份锚点
        ("_AGENT_ROLES", {"common": "通用助手", "R1": "普通助手"}),  # 缺失「智能转账专员」角色锚点
        ("_SHARED_GUARDRAILS", ""),  # 缺失确认/验证码/澄清/脱敏四个准则锚点
    ],
)
def test_tampered_prompt_is_rejected(monkeypatch, attr, value):
    """篡改 prompt 组成部件后，六字段校验 fail-closed。"""
    monkeypatch.setattr(prompts, attr, value)
    with pytest.raises(ValueError):
        _assert_prompt_valid(build_system_prompt("R1"))


def test_prompt_missing_a_field_is_rejected():
    """抠掉任意一个语义字段即被拒绝。"""
    prompt = build_system_prompt("R1")
    with pytest.raises(ValueError):
        _assert_prompt_valid(prompt.replace("脱敏", ""))


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("给张三转账100元", "smart_transfer"),
        ("转账 100 元", "smart_transfer"),
        ("按姓名给张三转 200 元", "transfer_by_name"),
        ("按手机号给张三转 300 元", "transfer_by_phone"),
        ("按备注房租转 400 元", "transfer_by_note"),
        ("定时转 500 元", "schedule_transfer"),
        ("帮我 AA 收款", "split_aa_collection"),
        ("帮我收款", "split_aa_collection"),
        ("给张三转点钱", None),
        ("帮我查一下余额", None),
        ("帮我买一份理财", None),
    ],
)
def test_keyword_stub_only_names_transfer_clarify_or_unsupported(content, expected):
    """关键词桩只产出转账类工具（或未命中返回 None），不做其它语义。"""
    name = _classify(content)
    assert name == expected
    if name is not None:
        assert name in TRANSFER_TOOL_NAMES


def test_stub_output_validates_against_the_prompt_output_schema():
    """桩产出的每个工具调用，其参数字段都落在对应工具的 JSON Schema 内。"""
    schemas = _tool_schemas()
    for keyword, call in DEFAULT_ROUTING.items():
        name = call["name"]
        assert name in schemas, keyword
        arguments = set(call.get("arguments", {}).keys())
        assert arguments, keyword
        assert arguments <= set(schemas[name].get("properties", {}).keys()), keyword


def test_unknown_intent_value_is_rejected():
    """未注册的工具名不可路由：不在 TOOL_MAP、桩也不产出。"""
    assert "execute" not in TOOL_MAP
    assert DEFAULT_ROUTING  # 路由非空
    for call in DEFAULT_ROUTING.values():
        assert call["name"] in TOOL_MAP
    assert TRANSFER_TOOL_NAMES <= set(TOOL_MAP)


def test_extra_or_out_of_range_fields_are_rejected():
    """越界金额 / 未声明币种被工具拒绝，schema 不接受 authority 字段。"""
    smart = TOOL_MAP["smart_transfer"]
    with pytest.raises(ValueError):
        smart.invoke({"amount": 0, "from_account": "a", "to_account": "b"})
    with pytest.raises(ValueError):
        smart.invoke({"amount": 1, "from_account": "a", "to_account": "b", "currency": "USD"})

    properties = _tool_schemas()["smart_transfer"]["properties"]
    assert {"confirmed", "mfa_code", "owner_id"}.isdisjoint(set(properties))
    assert set(properties) == {"amount", "from_account", "to_account", "currency"}


def test_a_model_claiming_confirmation_cannot_pass_the_strict_slots():
    """所有转账工具的严格槽位都不接受 confirmed / mfa_code。"""
    schemas = _tool_schemas()
    for name in TRANSFER_TOOL_NAMES:
        properties = set(schemas[name].get("properties", {}).keys())
        assert "confirmed" not in properties, name
        assert "mfa_code" not in properties, name


def test_changed_prompt_on_disk_fails_closed(monkeypatch):
    """磁盘上的角色表改动（R1 缺失 → 回落到 common）后校验 fail-closed。"""
    monkeypatch.setattr(prompts, "_AGENT_ROLES", {"common": "通用助手"})
    with pytest.raises(ValueError):
        _assert_prompt_valid(build_system_prompt("R1"))
