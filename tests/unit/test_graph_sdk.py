"""SDK API smoke only. MemorySaver does not prove PostgreSQL persistence.

旧架构 ``packages.persistence.checkpoint_setup``（SDK_VERSIONS / sdk_digest）已删除，
等价对象映射到 langgraph 原生契约：

    - ``Command.__annotations__`` 必须包含 ``update`` / ``resume``
    - ``PostgresSaver.MIGRATIONS`` 迁移数冻结为 10
    - ``interrupt`` / ``Command(resume=...)`` 与线程隔离行为
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class SdkState(TypedDict, total=False):
    value: int
    result: int


def _build_probe(saver):
    graph = StateGraph(SdkState)

    def pause(state: SdkState) -> dict:
        resumed = interrupt({"kind": "s0_sdk_compatibility", "value": state["value"]})
        return {"result": state["value"] + int(resumed)}

    graph.add_node("pause", pause)
    graph.add_edge(START, "pause")
    graph.add_edge("pause", END)
    return graph.compile(checkpointer=saver)


def test_frozen_sdk_interrupt_resume_and_thread_isolation():
    # SDK 契约冻结
    assert {"update", "resume"} <= set(Command.__annotations__)
    assert len(PostgresSaver.MIGRATIONS) == 10

    saver = MemorySaver()
    graph = _build_probe(saver)
    a = {"configurable": {"thread_id": "s0-a"}}
    b = {"configurable": {"thread_id": "s0-b"}}

    graph.invoke({"value": 4}, a)
    graph.invoke({"value": 40}, b)
    assert graph.get_state(a).tasks[0].interrupts[0].value["value"] == 4

    resumed = graph.invoke(Command(resume=3), a)
    assert resumed["result"] == 7
    assert graph.get_state(a).next == ()

    # 线程隔离：b 从未恢复，仍停在中断点且无结果
    assert graph.get_state(b).next == ("pause",)
    assert "result" not in graph.get_state(b).values
