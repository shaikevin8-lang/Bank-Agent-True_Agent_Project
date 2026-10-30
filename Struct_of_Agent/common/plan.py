"""DAG 计划模型与回退/回滚执行器（D03）。

本模块把「调度器给出的一串子任务」升级为**有依赖关系的图**，并提供：

1. 数据模型：:class:`PlanNode` / :class:`Edge` / :class:`Plan`（E10 统一为
   ``Edge(from_, to, kind)``，不再用裸 ``tuple``）；
2. 图算法：建图即环检测（:func:`detect_cycle` / :func:`topological_order`）、
   Kahn 拓扑 + ready 队列；
3. 执行语义：单线程顺序推进（演示安全优先）、失败分类
   （``retryable`` / ``permission`` / ``unknown``）、上游失败级联跳过、
   ``unknown`` 只置待核不重试、重规划上限 2 次、节点边界暂停与按
   ``plan_id+version`` 恢复（已完成节点凭幂等键**绝不重放**）；
4. 补偿映射与补偿铁律：补偿本身是写操作（走同一安全门 + 审计），
   ``irreversible`` 节点不进补偿流程，补偿失败 → 审计
   ``plan.compensation_failed`` + 立即熔断。

设计约束（不可违反）：

- 服务端生成 ``idempotency_key``，模型无法指定；
- 计划状态变化与拒绝一律产审计事件，且可在进程内回放（:func:`recent_audit`）；
- 本模块不直接调用任何工具：真正的执行由注入的 ``runner`` 完成，
  因此 DAG 层可以独立单测，不依赖数据库与网络。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 边类型：``sequential`` 顺序依赖 / ``data`` 数据依赖 / ``confirm`` 确认点。
KIND_SEQUENTIAL = "sequential"
KIND_DATA = "data"
KIND_CONFIRM = "confirm"
EDGE_KINDS: tuple[str, ...] = (KIND_SEQUENTIAL, KIND_DATA, KIND_CONFIRM)

#: 风险分级（与 :mod:`common.hitl` 的三色一致）。
RISK_GREEN = "green"
RISK_YELLOW = "yellow"
RISK_RED = "red"
RISK_LEVELS: tuple[str, ...] = (RISK_GREEN, RISK_YELLOW, RISK_RED)

#: 节点状态机。
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_NEEDS_HUMAN = "needs_human"
STATUS_CANCELLED = "cancelled"

NODE_STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_NEEDS_HUMAN,
    STATUS_CANCELLED,
)

#: 终态：不再推进、不会被 resume 重新执行。
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED, STATUS_CANCELLED}
)

#: 失败分类。
FAILURE_RETRYABLE = "retryable"
FAILURE_PERMISSION = "permission"
FAILURE_UNKNOWN = "unknown"
FAILURE_KINDS: tuple[str, ...] = (FAILURE_RETRYABLE, FAILURE_PERMISSION, FAILURE_UNKNOWN)

#: 重规划上限（超出升级人工）。
MAX_REPLANS = 2

#: 单次重试的退避秒数（只重试一次，见 :data:`MAX_ATTEMPTS`）。
RETRY_DELAY_S = 0.0
MAX_ATTEMPTS = 2

#: 这些错误码代表「没资格做」而不是「这次没好」，重试无意义。
_PERMISSION_ERROR_CODES: frozenset[str] = frozenset(
    {"AUTHZ_001", "AUTH_001", "NOT_READY_001", "CONFLICT_001"}
)

#: 这些错误码代表「不知道结果」，只能置待核（先查后决），绝不自动重放。
_UNKNOWN_ERROR_CODES: frozenset[str] = frozenset({"UNKNOWN_RESULT_001"})

#: 审计动作名。
AUDIT_INVALID_CYCLE = "plan.invalid_cycle"
AUDIT_REPLANNED = "plan.replanned"
AUDIT_REPLAN_LIMIT = "plan.replan_limit"
AUDIT_NODE_FAILED = "plan.node_failed"
AUDIT_NODE_SKIPPED = "plan.node_skipped"
AUDIT_NODE_UNKNOWN = "plan.node_unknown"
AUDIT_COMPENSATED = "plan.compensated"
AUDIT_COMPENSATION_BLOCKED = "plan.compensation_blocked"
AUDIT_COMPENSATION_FAILED = "plan.compensation_failed"
AUDIT_IRREVERSIBLE = "plan.irreversible_no_compensation"
AUDIT_PAUSED = "plan.paused"
AUDIT_RESUMED = "plan.resumed"
AUDIT_REPLAY_SUPPRESSED = "plan.replay_suppressed"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class PlanError(RuntimeError):
    """计划层错误的基类。"""


class PlanValidationError(PlanError):
    """计划结构非法（缺节点、类型不符、未知边类型等）。"""


class PlanCycleError(PlanValidationError):
    """计划存在环：必须拒绝执行并回退顺序模式。

    Attributes:
        nodes: 环上的节点 ID 列表（尽力给出，可能只含部分）。
    """

    def __init__(self, message: str, *, nodes: Sequence[str] | None = None) -> None:
        self.nodes = list(nodes or [])
        super().__init__(message)


class ReplanLimitError(PlanError):
    """重规划次数已达上限（:data:`MAX_REPLANS`），需转人工。"""


class PlanGateBlocked(PlanError):
    """安全门禁拦下了某个节点（熔断 / 越权 / 未确认）。

    Attributes:
        node_id: 被拦下的节点。
        receipt: 门禁返回的阻断回执（原样透传给调用方）。
    """

    def __init__(self, node_id: str, receipt: Mapping[str, Any] | None = None) -> None:
        self.node_id = node_id
        self.receipt = dict(receipt or {})
        super().__init__(f"节点 {node_id} 被安全门禁拦下")


# ---------------------------------------------------------------------------
# 审计（进程内可回放 + 可选落库）
# ---------------------------------------------------------------------------


_AUDIT_LOCK = threading.Lock()
_AUDIT_LOG: deque[dict[str, Any]] = deque(maxlen=512)
_PERSIST = False


class AuditSink(Protocol):
    """审计落点协议（便于测试注入）。"""

    def __call__(self, event: dict[str, Any]) -> None:  # pragma: no cover - 协议声明
        ...


_audit_sink: AuditSink | None = None


def configure_persist(persist: bool) -> None:
    """开关审计落库（默认关闭：单元测试与离线演示不需要数据库）。

    Args:
        persist: ``True`` 时 ``audit()`` 会尽力追加到 ``audit.event``。
    """
    global _PERSIST
    _PERSIST = bool(persist)


def configure_audit_sink(sink: AuditSink | None) -> None:
    """注入自定义审计落点（测试用）。

    Args:
        sink: 可调用对象；``None`` 表示移除。
    """
    global _audit_sink
    _audit_sink = sink


def audit(action: str, *, tenant_id: Any = None, **metadata: Any) -> dict[str, Any]:
    """追加一条计划层审计事件（失败仅记日志，不改变业务结论）。

    事件先进进程内环形缓冲（:func:`recent_audit` 可回放），再按配置决定是否
    落库。落库永远尽力而为：审计不可用时由调用方按各自语义 fail-closed
    （例如补偿失败已经通过熔断器零容忍开启表达）。

    Args:
        action: 动作名（``plan.*``）。
        tenant_id: 租户标识（落库用；``None`` 时取默认租户）。
        **metadata: 结构化补充字段（禁止写入密钥/完整卡号/模型原文）。

    Returns:
        事件字典（供调用方直接放入 SSE 或断言）。
    """
    event = {
        "action": action,
        "at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "tenant_id": str(tenant_id) if tenant_id is not None else None,
        **{key: value for key, value in metadata.items() if value is not None},
    }
    with _AUDIT_LOCK:
        _AUDIT_LOG.append(event)
    if _audit_sink is not None:
        try:
            _audit_sink(event)
        except Exception as exc:  # noqa: BLE001 - 审计落点失败不影响主流程
            logger.error("计划审计落点失败 action=%s：%s", action, exc)
    if _PERSIST:
        _persist(event, tenant_id)
    return event


def _persist(event: dict[str, Any], tenant_id: Any) -> None:
    """尽力把事件写入 ``audit.event``（失败只记 debug 日志）。

    Args:
        event: 事件字典。
        tenant_id: 租户标识。
    """
    try:  # pragma: no cover - 依赖数据库，离线路径不覆盖
        from common import hitl
        from db import audit as db_audit

        tenant = tenant_id if tenant_id is not None else hitl.default_tenant_id()
        payload = {
            key: value
            for key, value in event.items()
            if key not in {"action", "at", "tenant_id"}
        }
        with hitl.session_scope(tenant_id=tenant) as session:
            db_audit.append_event(
                session,
                tenant_id=tenant,
                action=event["action"],
                resource_type="plan",
                metadata=payload,
            )
    except Exception as exc:  # noqa: BLE001 - 审计落库不可用不阻断演示
        logger.debug("计划审计落库跳过 action=%s：%s", event.get("action"), exc)


def recent_audit(action: str | None = None) -> list[dict[str, Any]]:
    """读取进程内审计缓冲（按写入顺序）。

    Args:
        action: 只返回该动作名的事件；``None`` 返回全部。

    Returns:
        事件字典列表（浅拷贝）。
    """
    with _AUDIT_LOCK:
        events = list(_AUDIT_LOG)
    if action is None:
        return [dict(item) for item in events]
    return [dict(item) for item in events if item.get("action") == action]


def clear_audit() -> None:
    """清空进程内审计缓冲（测试隔离用）。"""
    with _AUDIT_LOCK:
        _AUDIT_LOG.clear()


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


def new_plan_id() -> str:
    """生成计划 ID（服务端生成，模型无法指定）。

    Returns:
        形如 ``PL-1a2b3c4d5e6f`` 的字符串。
    """
    return f"PL-{uuid.uuid4().hex[:12]}"


def make_idempotency_key(plan_id: str, version: int, node_id: str) -> str:
    """生成节点幂等键（同一计划同一版本同一节点恒定）。

    已完成节点恢复执行时凭此键复用结果，**绝不重放**副作用。

    Args:
        plan_id: 计划 ID。
        version: 计划版本。
        node_id: 节点 ID。

    Returns:
        32 位十六进制字符串。
    """
    raw = f"{plan_id}:{int(version)}:{node_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _now() -> datetime:
    """当前本地时间。

    Returns:
        带时区的 ``datetime``。
    """
    return datetime.now().astimezone()


@dataclass(frozen=True)
class Edge:
    """计划中的一条依赖边（E10：不再使用裸 tuple）。

    Attributes:
        from_: 上游节点 ID。
        to: 下游节点 ID。
        kind: 边类型（:data:`EDGE_KINDS` 之一）。
    """

    from_: str
    to: str
    kind: str = KIND_SEQUENTIAL

    def __post_init__(self) -> None:
        if self.kind not in EDGE_KINDS:
            raise PlanValidationError(f"未知边类型：{self.kind}")
        if not self.from_ or not self.to:
            raise PlanValidationError("边的两端都不能为空")

    def to_dict(self) -> dict[str, str]:
        """转换为 SSE 友好的字典。

        Returns:
            ``{"from": ..., "to": ..., "kind": ...}``。
        """
        return {"from": self.from_, "to": self.to, "kind": self.kind}


@dataclass
class PlanNode:
    """计划中的一个节点（一次子 Agent 调用）。

    Attributes:
        node_id: 节点 ID（计划内唯一）。
        agent: 目标子 Agent（``R1``…``R6`` / ``common``）。
        intent: 自包含的任务描述（调度器改写后）。
        params: 结构化参数（可选，进审计前需脱敏）。
        depends_on: 依赖的上游节点 ID。
        risk_level: 三色分级。
        requires_confirmation: 是否需要用户确认（黄/红节点）。
        irreversible: 是否不可逆（不可逆节点不进补偿流程）。
        compensating_action: 补偿动作名（``transfer.cancel_scheduled`` 等）。
        idempotency_key: 幂等键（服务端生成）。
        timeout_s: 节点墙钟超时（秒）。
        status: 节点状态（:data:`NODE_STATUSES`）。
        result_ref: 结果引用（轨迹/回执 ID）。
        attempts: 已尝试次数。
        error: 失败原因（人可读）。
        compensation: 补偿执行结果（``ok``/``blocked``/``failed``/``manual``/``none``）。
    """

    node_id: str
    agent: str
    intent: str
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    risk_level: str = RISK_GREEN
    requires_confirmation: bool = False
    irreversible: bool = False
    compensating_action: str | None = None
    idempotency_key: str = ""
    timeout_s: float = 30.0
    status: str = STATUS_PENDING
    result_ref: str | None = None
    attempts: int = 0
    error: str | None = None
    compensation: str | None = None

    def __post_init__(self) -> None:
        if self.risk_level not in RISK_LEVELS:
            raise PlanValidationError(f"未知风险等级：{self.risk_level}")
        if self.status not in NODE_STATUSES:
            raise PlanValidationError(f"未知节点状态：{self.status}")

    def to_dict(self, *, full: bool = False) -> dict[str, Any]:
        """转换为 SSE 事件里的节点字典。

        Args:
            full: 是否输出全部字段（默认只输出前端需要的展示字段）。

        Returns:
            节点字典。
        """
        payload: dict[str, Any] = {
            "node_id": self.node_id,
            "agent": self.agent,
            "intent": self.intent,
            "risk_level": self.risk_level,
            "status": self.status,
            "depends_on": list(self.depends_on),
        }
        if full:
            payload.update(
                {
                    "params": dict(self.params),
                    "requires_confirmation": self.requires_confirmation,
                    "irreversible": self.irreversible,
                    "compensating_action": self.compensating_action,
                    "idempotency_key": self.idempotency_key,
                    "timeout_s": self.timeout_s,
                    "result_ref": self.result_ref,
                    "attempts": self.attempts,
                    "error": self.error,
                    "compensation": self.compensation,
                }
            )
        return payload


@dataclass
class Plan:
    """一次执行的完整计划（可多版本，旧版本保留）。

    Attributes:
        plan_id: 计划 ID。
        version: 版本号（重规划 +1）。
        goal: 用户目标原文（回放用）。
        nodes: 节点列表。
        edges: 依赖边列表。
        created_at: 创建时间（ISO 字符串，便于 JSON 序列化）。
        replan_count: 已重规划次数（上限 :data:`MAX_REPLANS`）。
        origin: ``llm`` 或 ``fallback``（规则回退）。
    """

    plan_id: str
    version: int = 1
    goal: str = ""
    nodes: list[PlanNode] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))
    replan_count: int = 0
    origin: str = "llm"

    # ------------------------------------------------------------ 查询

    def node(self, node_id: str) -> PlanNode:
        """按 ID 取节点。

        Args:
            node_id: 节点 ID。

        Returns:
            节点对象。

        Raises:
            KeyError: 节点不存在。
        """
        for item in self.nodes:
            if item.node_id == node_id:
                return item
        raise KeyError(node_id)

    def get(self, node_id: str) -> PlanNode | None:
        """按 ID 取节点（不存在返回 ``None``）。

        Args:
            node_id: 节点 ID。

        Returns:
            节点对象或 ``None``。
        """
        try:
            return self.node(node_id)
        except KeyError:
            return None

    @property
    def node_ids(self) -> list[str]:
        """节点 ID 列表（保持插入顺序）。

        Returns:
            ID 列表。
        """
        return [item.node_id for item in self.nodes]

    def dependents(self, node_id: str) -> list[str]:
        """直接下游节点。

        Args:
            node_id: 上游节点 ID。

        Returns:
            直接依赖它的节点 ID。
        """
        return [item.node_id for item in self.nodes if node_id in item.depends_on]

    def transitive_dependents(self, node_id: str) -> list[str]:
        """传递下游节点（含间接依赖，按发现顺序）。

        Args:
            node_id: 上游节点 ID。

        Returns:
            受影响的节点 ID 列表。
        """
        seen: OrderedDict[str, None] = OrderedDict()
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            for child in self.dependents(current):
                if child not in seen:
                    seen[child] = None
                    queue.append(child)
        return list(seen)

    def status_counts(self) -> dict[str, int]:
        """各状态节点计数。

        Returns:
            状态 → 数量。
        """
        counts = {status: 0 for status in NODE_STATUSES}
        for item in self.nodes:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    @property
    def finished(self) -> bool:
        """是否所有节点都已进入终态。

        Returns:
            ``True`` 表示没有待推进节点。
        """
        return all(item.status in TERMINAL_STATUSES for item in self.nodes)

    # ------------------------------------------------------------ 序列化

    def to_dict(self, *, full: bool = False) -> dict[str, Any]:
        """转换为 JSON 友好字典。

        Args:
            full: 是否输出节点全部字段。

        Returns:
            计划字典。
        """
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "goal": self.goal,
            "created_at": self.created_at,
            "replan_count": self.replan_count,
            "origin": self.origin,
            "nodes": [item.to_dict(full=full) for item in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    def to_event(self) -> dict[str, Any]:
        """生成 SSE ``plan`` 事件的载荷（向后兼容 + 只增不改）。

        Returns:
            含 ``plan_id`` / ``version`` / ``nodes`` / ``edges`` 的字典。
        """
        payload = self.to_dict()
        payload["counts"] = self.status_counts()
        return payload

    def to_legacy_list(self) -> list[dict[str, str]]:
        """转换为旧版 ``plan`` 事件的扁平列表（向后兼容）。

        Returns:
            ``[{"agent": ..., "task": ...}]``。
        """
        return [{"agent": item.agent, "task": item.intent} for item in self.nodes]

    # ------------------------------------------------------------ 版本

    def clone(self, *, version: int | None = None) -> "Plan":
        """深拷贝计划（节点对象独立）。

        Args:
            version: 新版本号；``None`` 保持当前版本。

        Returns:
            新的 :class:`Plan`。
        """
        return Plan(
            plan_id=self.plan_id,
            version=self.version if version is None else int(version),
            goal=self.goal,
            nodes=[
                replace(node, depends_on=list(node.depends_on), params=dict(node.params))
                for node in self.nodes
            ],
            edges=list(self.edges),
            created_at=self.created_at,
            replan_count=self.replan_count,
            origin=self.origin,
        )

    def replanned(self, reason: str, *, nodes: Iterable[PlanNode] | None = None) -> "Plan":
        """产生重规划后的新版本（旧版本由调用方保留）。

        版本变了幂等键跟着变；同一版本内节点幂等键恒定，因此恢复执行时已完成
        节点不会被重放。

        Args:
            reason: 重规划原因（进审计）。
            nodes: 新节点集合；``None`` 表示沿用当前节点。

        Returns:
            版本号 +1、``replan_count`` +1 的新计划。

        Raises:
            ReplanLimitError: 已达 :data:`MAX_REPLANS` 上限（须转人工）。
        """
        if self.replan_count >= MAX_REPLANS:
            audit(
                AUDIT_REPLAN_LIMIT,
                plan_id=self.plan_id,
                version=self.version,
                replan_count=self.replan_count,
                reason=reason,
            )
            raise ReplanLimitError(
                f"计划 {self.plan_id} 重规划已达上限 {MAX_REPLANS} 次，请转人工处理"
            )
        fresh = self.clone(version=self.version + 1)
        source = list(nodes) if nodes is not None else list(fresh.nodes)
        fresh.nodes = [
            replace(
                node,
                idempotency_key=make_idempotency_key(fresh.plan_id, fresh.version, node.node_id),
            )
            for node in source
        ]
        fresh.edges = _rebuild_edges(fresh.nodes, self.edges)
        fresh.replan_count = self.replan_count + 1
        audit(
            AUDIT_REPLANNED,
            plan_id=self.plan_id,
            from_version=self.version,
            to_version=fresh.version,
            replan_count=fresh.replan_count,
            reason=reason,
        )
        return fresh


# ---------------------------------------------------------------------------
# 建图与图算法
# ---------------------------------------------------------------------------


def _rebuild_edges(nodes: Iterable[PlanNode], previous: Iterable[Edge] = ()) -> list[Edge]:
    """按节点 ``depends_on`` 重建边集（重规划换节点集后用）。

    旧边里已有的 ``(from, to)`` 保留原来的 ``kind``（例如 ``data``/``confirm``），
    新出现的依赖退化为 ``sequential``；指向已不存在节点的依赖被丢弃。

    Args:
        nodes: 新节点集合。
        previous: 旧边集合（提供 kind 记忆）。

    Returns:
        新边列表（去重、按节点声明顺序）。
    """
    node_list = list(nodes)
    known = {node.node_id for node in node_list}
    kinds = {(edge.from_, edge.to): edge.kind for edge in previous}
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for node in node_list:
        for dep in node.depends_on:
            if dep not in known or dep == node.node_id:
                continue
            pair = (dep, node.node_id)
            if pair in seen:
                continue
            seen.add(pair)
            edges.append(Edge(dep, node.node_id, kinds.get(pair, KIND_SEQUENTIAL)))
    return edges


def build_plan(
    tasks: Sequence[Mapping[str, Any]] | Sequence[str],
    *,
    goal: str = "",
    origin: str = "llm",
    plan_id: str | None = None,
    version: int = 1,
    replan_count: int = 0,
) -> Plan:
    """把调度器的子任务列表建成 DAG。

    默认语义：**顺序链**——第 N 个任务依赖第 N-1 个（与原「列表推导顺序执行」
    完全等价，保证向后兼容）。任务项可选字段 ``depends_on`` 可显式改写依赖，
    从而表达并行分支（如「先查支出，再按结果下单」的 R2 → R6）。

    Args:
        tasks: 任务项；字符串视为 ``{"agent": "common", "task": 原文}``。
        goal: 用户目标原文。
        origin: ``llm`` / ``fallback``。
        plan_id: 计划 ID；``None`` 时服务端生成。
        version: 版本号。
        replan_count: 已重规划次数。

    Returns:
        建好的计划（幂等键已生成）。

    Raises:
        PlanValidationError: 任务为空或字段非法。
    """
    if not tasks:
        raise PlanValidationError("计划至少需要一个节点")
    identifier = plan_id or new_plan_id()
    nodes: list[PlanNode] = []
    edges: list[Edge] = []
    previous: str | None = None
    for index, item in enumerate(tasks):
        raw: Mapping[str, Any] = {"task": item} if isinstance(item, str) else dict(item)
        node_id = str(raw.get("node_id") or f"n{index + 1}")
        agent = str(raw.get("agent") or "common")
        intent = str(raw.get("task") or raw.get("intent") or goal or "").strip()
        if not intent:
            raise PlanValidationError(f"节点 {node_id} 缺少任务描述")
        explicit = raw.get("depends_on")
        if explicit is None:
            depends_on = [previous] if previous else []
            kind = KIND_SEQUENTIAL
        else:
            depends_on = [str(dep) for dep in explicit]
            kind = str(raw.get("edge_kind") or KIND_DATA)
        nodes.append(
            PlanNode(
                node_id=node_id,
                agent=agent,
                intent=intent,
                params=dict(raw.get("params") or {}),
                depends_on=depends_on,
                risk_level=str(raw.get("risk_level") or RISK_GREEN),
                requires_confirmation=bool(raw.get("requires_confirmation", False)),
                irreversible=bool(raw.get("irreversible", False)),
                compensating_action=raw.get("compensating_action"),
                idempotency_key=make_idempotency_key(identifier, version, node_id),
                timeout_s=float(raw.get("timeout_s") or 30.0),
            )
        )
        for dependency in depends_on:
            edges.append(Edge(from_=dependency, to=node_id, kind=kind))
        previous = node_id
    return Plan(
        plan_id=identifier,
        version=version,
        goal=goal,
        nodes=nodes,
        edges=edges,
        replan_count=replan_count,
        origin=origin,
    )


def _adjacency(plan: Plan) -> tuple[dict[str, list[str]], dict[str, int]]:
    """由 ``depends_on`` 派生邻接表与入度表。

    Args:
        plan: 计划。

    Returns:
        ``(children, indegree)``。

    Raises:
        PlanValidationError: 依赖了不存在的节点。
    """
    known = set(plan.node_ids)
    children: dict[str, list[str]] = {node_id: [] for node_id in plan.node_ids}
    indegree: dict[str, int] = {node_id: 0 for node_id in plan.node_ids}
    for node in plan.nodes:
        for dependency in node.depends_on:
            if dependency not in known:
                raise PlanValidationError(
                    f"节点 {node.node_id} 依赖了不存在的节点 {dependency}"
                )
            children[dependency].append(node.node_id)
            indegree[node.node_id] += 1
    return children, indegree


def detect_cycle(plan: Plan) -> list[str]:
    """检测计划中的环（Kahn 消元后仍未消掉的节点即为环上节点）。

    Args:
        plan: 计划。

    Returns:
        环上节点 ID（无环时为空列表）。
    """
    children, indegree = _adjacency(plan)
    queue = deque([node_id for node_id, degree in indegree.items() if degree == 0])
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited == len(indegree):
        return []
    return [node_id for node_id, degree in indegree.items() if degree > 0]


def topological_order(plan: Plan) -> list[str]:
    """Kahn 拓扑排序（同层按插入顺序，保证可复现）。

    Args:
        plan: 计划。

    Returns:
        节点 ID 顺序。

    Raises:
        PlanCycleError: 存在环。
    """
    children, indegree = _adjacency(plan)
    order: list[str] = []
    queue = deque([node_id for node_id in plan.node_ids if indegree[node_id] == 0])
    while queue:
        current = queue.popleft()
        order.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(plan.nodes):
        cycle = [node_id for node_id, degree in indegree.items() if degree > 0]
        raise PlanCycleError(f"计划 {plan.plan_id} 存在依赖环：{cycle}", nodes=cycle)
    return order


def ready_nodes(plan: Plan) -> list[str]:
    """当前可执行节点（依赖全部 done，且自身处于 pending/ready）。

    Args:
        plan: 计划。

    Returns:
        按拓扑序排列的节点 ID。
    """
    order = topological_order(plan)
    ready: list[str] = []
    for node_id in order:
        node = plan.node(node_id)
        if node.status not in (STATUS_PENDING, STATUS_READY):
            continue
        if all(plan.node(dep).status == STATUS_DONE for dep in node.depends_on):
            ready.append(node_id)
    return ready


def annotate_ready(plan: Plan) -> list[str]:
    """把可执行节点标为 ``ready``（纯标记，不执行）。

    Args:
        plan: 计划。

    Returns:
        本次新标记的节点 ID。
    """
    marked: list[str] = []
    for node_id in ready_nodes(plan):
        node = plan.node(node_id)
        if node.status == STATUS_PENDING:
            node.status = STATUS_READY
            marked.append(node_id)
    return marked


def validate(plan: Plan) -> None:
    """建图即校验：环、悬空依赖、重复 ID。

    Args:
        plan: 计划。

    Returns:
        ``None``（通过）。

    Raises:
        PlanValidationError: 结构非法。
        PlanCycleError: 存在环。
    """
    if len(set(plan.node_ids)) != len(plan.nodes):
        raise PlanValidationError("计划中存在重复节点 ID")
    _adjacency(plan)
    topological_order(plan)


# ---------------------------------------------------------------------------
# 失败分类
# ---------------------------------------------------------------------------


def classify_failure(
    *,
    error: BaseException | None = None,
    result: Mapping[str, Any] | None = None,
) -> str:
    """把一次节点失败归类为 ``retryable`` / ``permission`` / ``unknown``。

    规则（不可放宽）：

    - 回执 ``unknown`` 或错误码 ``UNKNOWN_RESULT_001`` → ``unknown``（置待核，
      **不重试**：先查原操作再决定）；
    - 越权 / 未就绪 / 冲突 / ``PermissionError`` / HITL 不可用 / 熔断开启 → ``permission``
      （重试无意义，直接失败）；
    - 超时（``TimeoutError``）→ ``unknown``（写操作可能已在远端生效，按「未知」转人工）；
    - 其余（网络抖动、模型抖动、未分类异常）→ ``retryable``（退避重试 1 次）。

    Args:
        error: 抛出的异常（可为空）。
        result: 工具/子 Agent 的回执（可为空）。

    Returns:
        分类字符串。
    """
    payload = dict(result or {})
    if payload.get("unknown") is True or payload.get("status") == "unknown":
        return FAILURE_UNKNOWN
    code = str(payload.get("error_code") or "")
    if code in _UNKNOWN_ERROR_CODES:
        return FAILURE_UNKNOWN
    if code in _PERMISSION_ERROR_CODES:
        return FAILURE_PERMISSION
    if isinstance(error, (PermissionError, PlanGateBlocked)):
        return FAILURE_PERMISSION
    if isinstance(error, TimeoutError):
        return FAILURE_UNKNOWN
    if error is not None and type(error).__name__ in {"HitlUnavailable", "CircuitOpenError"}:
        return FAILURE_PERMISSION
    if payload.get("status") in {"rejected", "blocked", "hitl_required"}:
        return FAILURE_PERMISSION
    return FAILURE_RETRYABLE


# ---------------------------------------------------------------------------
# 补偿映射
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Compensation:
    """一条补偿规则。

    Attributes:
        action: 补偿动作名（审计与展示用）。
        tool_name: 实际执行的工具名；``None`` 表示本系统尚无自动化补偿，
            只能转人工（如赎回、挂失）。
        reversible: 是否可逆（``False`` 表示执行前必须二次确认并明示不可撤销）。
        note: 说明。
    """

    action: str
    tool_name: str | None = None
    reversible: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        if not self.action:
            raise PlanValidationError("补偿动作名不能为空")


#: 补偿映射表（与 DOC-AGENT-001 的「补偿映射」章节逐条对应）。
COMPENSATIONS: dict[str, Compensation] = {
    # R1 转账
    "schedule_transfer": Compensation(
        "transfer.cancel_scheduled", None, True, "未到执行时点可撤销（需人工或后续工具支持）"
    ),
    "smart_transfer": Compensation("transfer.reverse", None, False, "已出账转账不可程序化逆冲"),
    "transfer_by_name": Compensation("transfer.reverse", None, False, "已出账转账不可程序化逆冲"),
    "transfer_by_phone": Compensation("transfer.reverse", None, False, "已出账转账不可程序化逆冲"),
    "transfer_by_note": Compensation("transfer.reverse", None, False, "已出账转账不可程序化逆冲"),
    "split_aa_collection": Compensation("transfer.reverse", None, False, "已出账收款不可程序化逆冲"),
    # R3 理财
    "purchase_product": Compensation("wealth.cancel_order", None, True, "未结算可撤单（需理财侧工具）"),
    "redeem_product": Compensation("wealth.cancel_redeem", None, False, "赎回不可逆 → 转人工"),
    # R4 卡片
    "freeze_card": Compensation("card.unfreeze", "unfreeze_card", True, "解冻即补偿"),
    "set_restriction": Compensation("card.remove_restriction", "remove_restriction", True, "解除限制即补偿"),
    "report_loss": Compensation("card.unreport_loss", None, False, "挂失不可逆 → 明示 + 转人工"),
    "apply_card": Compensation("card.cancel_application", None, False, "已提交申请需人工撤回"),
    "adjust_limit": Compensation("card.restore_limit", None, True, "额度调整需人工回退"),
    # R5 订阅
    "cancel_subscription": Compensation("subscription.resubscribe", None, False, "解约后需重新签约"),
    "set_reminder": Compensation("subscription.clear_reminder", None, True, "提醒可清除"),
    # R6 礼品 / 工作流
    "place_gift_orders": Compensation("gift.cancel_order", "cancel_workflow", True, "未发货可撤单"),
    "reserve_budget": Compensation("gift.release_reservation", "cancel_workflow", True, "释放预算预留"),
    "cancel_workflow": Compensation("gift.reopen_workflow", None, False, "已取消工作流不可自动恢复"),
}


def compensation_for(
    tool_name: str | None,
    fallback_action: str | None = None,
) -> Compensation | None:
    """查补偿规则。

    Args:
        tool_name: 原工具名（可空）。
        fallback_action: 节点自带的补偿动作名（优先于工具映射）。

    Returns:
        补偿规则；未登记时返回 ``None``（视为不可逆）。
    """
    if fallback_action:
        for item in COMPENSATIONS.values():
            if item.action == fallback_action:
                return item
    if tool_name and tool_name in COMPENSATIONS:
        return COMPENSATIONS[tool_name]
    return None


def is_reversible(node: PlanNode, *, tool_name: str | None = None) -> bool:
    """节点是否可逆（``irreversible`` 标记优先）。

    Args:
        node: 节点。
        tool_name: 节点实际调用的写工具名（可空）。

    Returns:
        是否可逆。
    """
    if node.irreversible:
        return False
    rule = compensation_for(tool_name, node.compensating_action)
    return bool(rule and rule.reversible)


# ---------------------------------------------------------------------------
# 事件载荷
# ---------------------------------------------------------------------------


def node_status_event(plan: Plan, node: PlanNode) -> dict[str, Any]:
    """生成单个节点的 ``node_status`` 事件（流式与批处理共用）。

    Args:
        plan: 计划。
        node: 节点。

    Returns:
        事件字典。
    """
    payload: dict[str, Any] = {
        "type": "node_status",
        "plan_id": plan.plan_id,
        "version": plan.version,
        "node_id": node.node_id,
        "index": plan.node_ids.index(node.node_id),
        "agent": node.agent,
        "intent": node.intent,
        "status": node.status,
        "risk_level": node.risk_level,
        "depends_on": list(node.depends_on),
        "counts": plan.status_counts(),
    }
    if node.error:
        payload["reason"] = node.error
    if node.compensation:
        payload["compensation"] = node.compensation
    return payload


def cascade_skip(
    plan: Plan,
    node: PlanNode,
    *,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """上游终止 → 下游全部 ``skipped``（含间接依赖）。

    Args:
        plan: 计划。
        node: 触发终止的节点。
        emit: 事件收集回调（流式路径直接下发给前端）。

    Returns:
        本次产生的事件列表。
    """
    reason = f"dependency_failed:{node.node_id}"
    events: list[dict[str, Any]] = []
    for node_id in plan.transitive_dependents(node.node_id):
        dependent = plan.node(node_id)
        if dependent.status in TERMINAL_STATUSES:
            continue
        dependent.status = STATUS_SKIPPED
        dependent.error = reason
        audit(
            AUDIT_NODE_SKIPPED,
            plan_id=plan.plan_id,
            version=plan.version,
            node_id=node_id,
            reason=reason,
        )
        event = node_status_event(plan, dependent)
        events.append(event)
        if emit is not None:
            emit(event)
    return events


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


@dataclass
class PlanRunResult:
    """一次计划执行的结果。

    Attributes:
        plan: 执行后的计划（含各节点终态）。
        status: 总体状态（``done``/``failed``/``needs_human``/``paused``/``rolled_back``）。
        events: 执行过程中产生的 SSE 事件（可回放）。
        results: 节点 ID → runner 原始返回值。
        compensated: 补偿结果列表。
    """

    plan: Plan
    status: str
    events: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    compensated: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为统一契约字典。

        Returns:
            含 ``plan`` / ``status`` / ``events`` 的字典。
        """
        return {
            "plan": self.plan.to_dict(full=True),
            "status": self.status,
            "events": list(self.events),
            "compensated": list(self.compensated),
        }


class PlanExecutor:
    """DAG 执行器（单线程、节点边界可暂停）。

    执行器不碰数据库、不碰工具：``runner`` 负责真正调用子 Agent，
    ``gate`` 负责安全门禁（熔断/越权），``compensate`` 负责执行补偿。
    这样 DAG 语义可以脱离基础设施独立验证。
    """

    def __init__(
        self,
        runner: Callable[[PlanNode], Any],
        *,
        gate: Callable[[PlanNode], Mapping[str, Any] | None] | None = None,
        compensate: Callable[[PlanNode], Any] | None = None,
        on_compensation_failure: Callable[[PlanNode, str], None] | None = None,
        max_replans: int = MAX_REPLANS,
        retry_delay_s: float = RETRY_DELAY_S,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化执行器。

        Args:
            runner: 节点执行函数，返回回执字典（或抛异常）。
            gate: 安全门禁；返回非空字典表示阻断。
            compensate: 补偿执行函数（返回回执字典）。
            on_compensation_failure: 补偿失败钩子（默认接熔断器零容忍）。
            max_replans: 重规划上限。
            retry_delay_s: 重试退避秒数。
            sleep: 睡眠函数（测试注入）。
            clock: 单调时钟（测试注入）。
        """
        self._runner = runner
        self._gate = gate
        self._compensate = compensate
        self._on_compensation_failure = on_compensation_failure or _default_compensation_failure
        self._max_replans = int(max_replans)
        self._retry_delay_s = float(retry_delay_s)
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._events: list[dict[str, Any]] = []
        self._paused = False

    # ------------------------------------------------------------ 暂停

    def pause(self) -> None:
        """请求暂停（只在节点边界生效，不会中断进行中的节点）。"""
        self._paused = True

    def clear_pause(self) -> None:
        """清除暂停请求。"""
        self._paused = False

    @property
    def events(self) -> list[dict[str, Any]]:
        """已产生的事件列表（只读快照）。

        Returns:
            事件字典列表。
        """
        return list(self._events)

    # ------------------------------------------------------------ 主循环

    def run(
        self,
        plan: Plan,
        *,
        replan: Callable[[Plan, PlanNode, str], Plan | None] | None = None,
    ) -> PlanRunResult:
        """执行计划直到没有可推进节点（或暂停）。

        Args:
            plan: 待执行计划。
            replan: 重规划工厂：``(plan, failed_node, reason) -> 新版本计划|None``；
                ``None`` 表示失败不重规划（直接级联跳过 + 转人工）。

        Returns:
            执行结果。

        Raises:
            PlanCycleError: 计划有环（调用方应回退顺序模式）。
        """
        current = plan
        self._emit({"type": "plan_start", "plan": current.to_event()})
        results: dict[str, Any] = {}
        while True:
            if self._paused:
                audit(AUDIT_PAUSED, plan_id=current.plan_id, version=current.version)
                self._emit(
                    {"type": "plan_paused", "plan_id": current.plan_id, "version": current.version}
                )
                break
            annotate_ready(current)
            batch = ready_nodes(current)
            if not batch:
                break
            node = current.node(batch[0])
            if node.status == STATUS_DONE:  # 幂等键保护：绝不重放
                audit(AUDIT_REPLAY_SUPPRESSED, plan_id=current.plan_id, node_id=node.node_id)
                continue
            outcome = self._execute_node(current, node, results)
            if outcome == "replanned" and replan is not None:
                fresh = replan(current, node, node.error or "node_failed")
                if fresh is not None:
                    self._emit(
                        {
                            "type": "plan_replanned",
                            "plan_id": fresh.plan_id,
                            "from_version": current.version,
                            "to_version": fresh.version,
                            "reason": node.error or "node_failed",
                            "plan": fresh.to_event(),
                        }
                    )
                    current = fresh
                    continue
            if node.status in (STATUS_FAILED, STATUS_NEEDS_HUMAN, STATUS_CANCELLED):
                cascade_skip(current, node, emit=self._emit)
        status = self._final_status(current)
        self._emit(
            {
                "type": "plan_done",
                "plan_id": current.plan_id,
                "version": current.version,
                "status": status,
                "plan": current.to_event(),
            }
        )
        return PlanRunResult(
            plan=current, status=status, events=list(self._events), results=results
        )

    def resume(self, plan: Plan, *, version: int | None = None) -> PlanRunResult:
        """从首个非 ``done`` 节点继续（按 ``plan_id+version`` 恢复）。

        Args:
            plan: 计划。
            version: 期望版本（不匹配直接拒绝，避免跨版本误恢复）。

        Returns:
            执行结果。

        Raises:
            PlanValidationError: 版本不匹配。
        """
        if version is not None and int(version) != plan.version:
            raise PlanValidationError(f"恢复版本不匹配：期望 {version}，实际 {plan.version}")
        audit(
            AUDIT_RESUMED,
            plan_id=plan.plan_id,
            version=plan.version,
            resumed_from=_first_pending(plan),
        )
        self.clear_pause()
        return self.run(plan)

    # ------------------------------------------------------------ 单节点

    def _execute_node(self, plan: Plan, node: PlanNode, results: dict[str, Any]) -> str:
        """执行单个节点（可重试失败退避重试一次）。

        Args:
            plan: 当前计划。
            node: 目标节点。
            results: 结果收集器（原地更新）。

        Returns:
            ``"ok"`` / ``"failed"`` / ``"replanned"``。
        """
        node.status = STATUS_RUNNING
        self._emit_node(plan, node)
        while True:
            node.attempts += 1
            started = self._clock()
            error: BaseException | None = None
            payload: dict[str, Any] = {}
            if self._gate is not None:
                blocked = self._gate(node)
                if blocked:
                    error = PlanGateBlocked(node.node_id, blocked)
                    payload = dict(blocked)
            if error is None:
                try:
                    raw = self._runner(node)
                    payload = dict(raw) if isinstance(raw, Mapping) else {"result": raw}
                except BaseException as exc:  # noqa: BLE001 - 分类后统一处理
                    error = exc
            elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)

            receipt_status = str(payload.get("status") or "").lower()
            failed = (
                error is not None
                or payload.get("blocked") is True
                or payload.get("failed") is True
                or receipt_status
                in {"failed", "error", "unknown", "rejected", "blocked", "hitl_required", "timeout"}
            )
            if not failed:
                node.status = STATUS_DONE
                node.error = None
                node.result_ref = str(
                    payload.get("result_ref") or payload.get("id") or node.node_id
                )
                results[node.node_id] = payload
                self._emit_node(plan, node, metric_ms=elapsed_ms)
                return "ok"

            kind = classify_failure(error=error, result=payload)
            if kind == FAILURE_UNKNOWN:
                node.status = STATUS_NEEDS_HUMAN
                node.error = f"unknown: {_failure_text(error, payload, kind)}"
                audit(
                    AUDIT_NODE_UNKNOWN,
                    plan_id=plan.plan_id,
                    version=plan.version,
                    node_id=node.node_id,
                    intent=node.intent,
                    detail=node.error[:512],
                )
                self._emit_node(plan, node, metric_ms=elapsed_ms)
                return "failed"

            if kind == FAILURE_RETRYABLE and node.attempts < MAX_ATTEMPTS:
                logger.warning(
                    "节点 %s 可重试失败（第 %s 次）：%s", node.node_id, node.attempts, payload or error
                )
                if self._retry_delay_s:
                    self._sleep(self._retry_delay_s)
                continue

            node.status = STATUS_FAILED
            node.error = _failure_text(error, payload, kind)
            audit(
                AUDIT_NODE_FAILED,
                plan_id=plan.plan_id,
                version=plan.version,
                node_id=node.node_id,
                intent=node.intent,
                failure_kind=kind,
                detail=node.error[:512],
                attempts=node.attempts,
            )
            self._emit_node(plan, node, metric_ms=elapsed_ms)
            if kind == FAILURE_RETRYABLE and plan.replan_count < self._max_replans:
                return "replanned"
            return "failed"

    # ------------------------------------------------------------ 补偿

    def rollback(
        self,
        plan: Plan,
        *,
        reason: str = "user_cancelled",
        tool_of: Callable[[PlanNode], str | None] | None = None,
    ) -> PlanRunResult:
        """按逆拓扑序补偿已完成节点（补偿铁律见模块文档）。

        - ``irreversible`` 节点不进补偿流程，直接置 ``needs_human`` 并审计；
        - 补偿走同一安全门（``gate``）：被拦下即审计 ``plan.compensation_blocked``；
        - 补偿本身失败 → 审计 ``plan.compensation_failed`` + 触发零容忍钩子；
        - 无自动化补偿工具（``tool_name is None``）或未注入补偿函数 → 转人工。

        Args:
            plan: 计划（只有 ``done`` 节点会被补偿）。
            reason: 回滚原因。
            tool_of: 从节点解析原工具名（用于查补偿映射）。

        Returns:
            执行结果（``status`` 为 ``rolled_back`` / ``needs_human``）。
        """
        order = topological_order(plan)
        done_order = [node_id for node_id in reversed(order) if plan.node(node_id).status == STATUS_DONE]
        compensated: list[dict[str, Any]] = []
        elevated = False
        for node_id in done_order:
            node = plan.node(node_id)
            tool_name: str | None = None
            if tool_of is not None:
                tool_name = tool_of(node)
            elif isinstance(node.params, Mapping) and node.params.get("tool"):
                tool_name = str(node.params["tool"])
            rule = compensation_for(tool_name, node.compensating_action)
            if node.irreversible or rule is None or not rule.reversible:
                node.status = STATUS_NEEDS_HUMAN
                node.compensation = "none"
                node.error = f"不可逆节点，需人工处理（{reason}）"
                audit(
                    AUDIT_IRREVERSIBLE,
                    plan_id=plan.plan_id,
                    version=plan.version,
                    node_id=node.node_id,
                    reason=reason,
                    detail=node.error,
                )
                self._emit_node(plan, node)
                compensated.append({"node_id": node.node_id, "result": "irreversible"})
                elevated = True
                continue
            if self._gate is not None:
                blocked = self._gate(node)
                if blocked:
                    node.status = STATUS_NEEDS_HUMAN
                    node.compensation = "blocked"
                    audit(
                        AUDIT_COMPENSATION_BLOCKED,
                        plan_id=plan.plan_id,
                        version=plan.version,
                        node_id=node.node_id,
                        compensation_action=rule.action,
                        reason=reason,
                    )
                    self._emit_node(plan, node)
                    compensated.append(
                        {"node_id": node.node_id, "result": "blocked", "action": rule.action}
                    )
                    elevated = True
                    continue
            if rule.tool_name is None or self._compensate is None:
                node.status = STATUS_NEEDS_HUMAN
                node.compensation = "manual"
                node.error = f"无自动化补偿工具，需人工执行 {rule.action}"
                audit(
                    AUDIT_IRREVERSIBLE,
                    plan_id=plan.plan_id,
                    version=plan.version,
                    node_id=node.node_id,
                    compensation_action=rule.action,
                    reason=reason,
                )
                self._emit_node(plan, node)
                compensated.append(
                    {"node_id": node.node_id, "result": "manual", "action": rule.action}
                )
                elevated = True
                continue
            try:
                receipt = self._compensate(node)
                ok = not (
                    isinstance(receipt, Mapping)
                    and (receipt.get("blocked") or receipt.get("failed"))
                )
                if not ok:
                    raise RuntimeError(str(dict(receipt).get("message") or "补偿回执失败"))
            except Exception as exc:  # noqa: BLE001 - 补偿失败必须升级处理
                node.status = STATUS_NEEDS_HUMAN
                node.compensation = "failed"
                node.error = f"补偿失败：{exc}"
                audit(
                    AUDIT_COMPENSATION_FAILED,
                    plan_id=plan.plan_id,
                    version=plan.version,
                    node_id=node.node_id,
                    compensation_action=rule.action,
                    reason=reason,
                    detail=str(exc)[:512],
                )
                self._emit_node(plan, node)
                compensated.append(
                    {"node_id": node.node_id, "result": "failed", "action": rule.action}
                )
                elevated = True
                self._on_compensation_failure(node, str(exc))
                continue
            node.status = STATUS_CANCELLED
            node.compensation = "ok"
            audit(
                AUDIT_COMPENSATED,
                plan_id=plan.plan_id,
                version=plan.version,
                node_id=node.node_id,
                compensation_action=rule.action,
                reason=reason,
            )
            self._emit_node(plan, node)
            compensated.append({"node_id": node.node_id, "result": "ok", "action": rule.action})
        return PlanRunResult(
            plan=plan,
            status="needs_human" if elevated else "rolled_back",
            events=list(self._events),
            compensated=compensated,
        )

    # ------------------------------------------------------------ 内部

    def _emit(self, event: dict[str, Any]) -> None:
        """记录一条事件。

        Args:
            event: 事件字典。
        """
        self._events.append(event)

    def _emit_node(self, plan: Plan, node: PlanNode, *, metric_ms: float | None = None) -> None:
        """记录一条 ``node_status`` 事件。

        Args:
            plan: 计划。
            node: 节点。
            metric_ms: 本次执行墙钟毫秒（可选）。
        """
        payload = node_status_event(plan, node)
        if metric_ms is not None:
            payload["wall_ms"] = round(metric_ms, 1)
        self._emit(payload)

    @staticmethod
    def _final_status(plan: Plan) -> str:
        """由节点终态推导计划总体状态。

        Args:
            plan: 计划。

        Returns:
            ``done`` / ``needs_human`` / ``failed`` / ``paused``。
        """
        counts = plan.status_counts()
        if counts.get(STATUS_NEEDS_HUMAN):
            return "needs_human"
        if counts.get(STATUS_FAILED):
            return "failed"
        if counts.get(STATUS_PENDING) or counts.get(STATUS_READY) or counts.get(STATUS_RUNNING):
            return "paused"
        return "done"


def _default_compensation_failure(node: PlanNode, detail: str) -> None:
    """默认补偿失败处理：立即熔断写能力（PRD §8.4）。

    Args:
        node: 失败的补偿节点。
        detail: 失败详情。
    """
    try:
        from common import circuit_breaker, hitl

        context = hitl.current_context()
        tool = str(node.params.get("tool") or "") if isinstance(node.params, Mapping) else ""
        capability = circuit_breaker.capability_for_tool(tool) or circuit_breaker.CAP_TRANSFER
        circuit_breaker.record_failure(
            tenant_id=context.tenant_id,
            user_id=context.session_id,
            capability=capability,
            reason_code=circuit_breaker.REASON_WRITE_TOOL_FAILURE,
            detail=f"补偿失败 node={node.node_id} {detail}"[:512],
            immediate=True,
        )
    except Exception as exc:  # noqa: BLE001 - 上报失败不改变「已失败」结论
        logger.error("补偿失败熔断上报失败 node=%s：%s", node.node_id, exc)


def _failure_text(error: BaseException | None, payload: Mapping[str, Any], kind: str) -> str:
    """生成人可读失败原因。

    Args:
        error: 异常。
        payload: 回执。
        kind: 失败分类。

    Returns:
        失败原因文本。
    """
    data = dict(payload)
    message = str(data.get("message") or data.get("reply") or "").strip()
    if not message and error is not None:
        message = f"{type(error).__name__}: {error}"
    return f"{kind}: {message or '未知失败'}"


def _first_pending(plan: Plan) -> str | None:
    """首个非 ``done`` 节点（恢复点）。

    Args:
        plan: 计划。

    Returns:
        节点 ID 或 ``None``。
    """
    for node in plan.nodes:
        if node.status != STATUS_DONE:
            return node.node_id
    return None


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


def plan_event(plan: Plan) -> dict[str, Any]:
    """生成 SSE ``plan`` 事件（``type`` + 计划载荷）。

    Args:
        plan: 计划。

    Returns:
        ``{"type": "plan", ...}``。
    """
    return {"type": "plan", **plan.to_event()}


def final_status(plan: Plan) -> str:
    """由节点终态推导计划总体状态（对外公开版本）。

    Args:
        plan: 计划。

    Returns:
        ``done`` / ``needs_human`` / ``failed`` / ``paused``。
    """
    return PlanExecutor._final_status(plan)


def has_cycle(tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    """建图后检测环（供调度器在采纳 LLM 计划前一票否决）。

    Args:
        tasks: 任务项。

    Returns:
        环上节点 ID（无环为空）。
    """
    try:
        plan = build_plan(tasks)
    except PlanValidationError:
        return ["<invalid>"]
    return detect_cycle(plan)


def dump_plan(plan: Plan) -> str:
    """把计划序列化为 JSON 文本（写 ``docs/_work`` 样例用）。

    Args:
        plan: 计划。

    Returns:
        JSON 字符串。
    """
    return json.dumps(plan.to_dict(full=True), ensure_ascii=False, indent=2)


__all__ = [
    "AUDIT_COMPENSATED",
    "AUDIT_COMPENSATION_BLOCKED",
    "AUDIT_COMPENSATION_FAILED",
    "AUDIT_INVALID_CYCLE",
    "AUDIT_IRREVERSIBLE",
    "AUDIT_NODE_FAILED",
    "AUDIT_NODE_SKIPPED",
    "AUDIT_NODE_UNKNOWN",
    "AUDIT_PAUSED",
    "AUDIT_REPLAY_SUPPRESSED",
    "AUDIT_REPLANNED",
    "AUDIT_REPLAN_LIMIT",
    "AUDIT_RESUMED",
    "COMPENSATIONS",
    "Compensation",
    "EDGE_KINDS",
    "Edge",
    "FAILURE_KINDS",
    "FAILURE_PERMISSION",
    "FAILURE_RETRYABLE",
    "FAILURE_UNKNOWN",
    "KIND_CONFIRM",
    "KIND_DATA",
    "KIND_SEQUENTIAL",
    "MAX_REPLANS",
    "NODE_STATUSES",
    "Plan",
    "PlanCycleError",
    "PlanError",
    "PlanExecutor",
    "PlanGateBlocked",
    "PlanNode",
    "PlanRunResult",
    "PlanValidationError",
    "ReplanLimitError",
    "RISK_GREEN",
    "RISK_RED",
    "RISK_YELLOW",
    "STATUS_CANCELLED",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_NEEDS_HUMAN",
    "STATUS_PENDING",
    "STATUS_READY",
    "STATUS_RUNNING",
    "STATUS_SKIPPED",
    "TERMINAL_STATUSES",
    "annotate_ready",
    "audit",
    "build_plan",
    "cascade_skip",
    "classify_failure",
    "clear_audit",
    "compensation_for",
    "configure_audit_sink",
    "configure_persist",
    "detect_cycle",
    "dump_plan",
    "final_status",
    "has_cycle",
    "is_reversible",
    "make_idempotency_key",
    "new_plan_id",
    "node_status_event",
    "plan_event",
    "ready_nodes",
    "recent_audit",
    "topological_order",
    "validate",
]
