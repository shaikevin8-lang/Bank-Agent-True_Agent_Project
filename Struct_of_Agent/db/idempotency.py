"""写入口的最小幂等能力。

对应数据库设计文档 §4.9 与 §6.4：

    - 只有会产生余额、任务或外部写副作用的入口才占用幂等键；普通查询不写。
    - 写请求先占用唯一键，再执行余额或任务变更。
    - 同一键 + 相同 ``request_hash`` 返回已有结果；同一键 + 不同请求必须拒绝。

实现要点：

    1. 占用用 ``INSERT ... ON CONFLICT DO NOTHING RETURNING id``，由数据库的
       唯一约束保证「同一键只有一个赢家」，不依赖先查后插。
    2. 冲突时对既有行 ``SELECT ... FOR UPDATE``：并发事务未提交时本事务会等待，
       等它提交后再按它的最终状态决定回放还是拒绝；若对方回滚，行会消失，
       于是重试一次插入。
    3. 幂等记录与业务写入必须在**同一个事务**里：业务失败整体回滚，不会留下
       「已占用但没执行」的脏键；需要留痕的失败由 :func:`fail_operation`
       在同一事务内显式落库。

本模块不决定业务语义（例如是否允许失败重试），只把状态如实返回给调用方。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .base import utcnow
from .models import IdempotencyKey

MAX_KEY_LENGTH = 128
MAX_OPERATION_LENGTH = 64
HASH_LENGTH = 64

STATUS_PROCESSING = "processing"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


class IdempotencyError(RuntimeError):
    """幂等键使用错误的基类。"""


class IdempotencyConflict(IdempotencyError):
    """同一幂等键被用于不同请求（``request_hash`` 不一致）。"""


class IdempotencyInProgress(IdempotencyError):
    """同一幂等键已有请求在执行，且尚未完成。"""


@dataclass(frozen=True)
class IdempotencyResult:
    """一次幂等占用的结果。

    Attributes:
        record: 幂等记录（``status`` 为占用后的状态）。
        replay: True 表示本次没有执行新写入，``response`` 是历史结果。
        response: 历史结果；``replay`` 为 False 时通常为 None。
    """

    record: IdempotencyKey
    replay: bool
    response: dict[str, Any] | None


def fingerprint(payload: Any) -> str:
    """计算请求体的稳定指纹（SHA-256 十六进制）。

    键排序 + 紧凑分隔符 + ``ensure_ascii=False``，保证同一逻辑请求在不同进程、
    不同字典顺序下得到同一个值。

    Args:
        payload: 可 JSON 序列化的请求体（金额用字符串或整数，不用 float）。

    Returns:
        64 位十六进制字符串。

    Raises:
        IdempotencyError: 请求体无法 JSON 序列化。
    """
    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise IdempotencyError(f"请求体无法序列化：{error}") from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate(
    *,
    operation_type: str,
    key_value: str,
    request_hash: str,
) -> None:
    """校验幂等键的长度与格式。

    Args:
        operation_type: 操作类型，如 ``transfer.create``。
        key_value: 客户端提供的幂等键。
        request_hash: 请求指纹。

    Raises:
        IdempotencyError: 任一字段为空、过长或指纹格式非法。
    """
    if not operation_type or len(operation_type) > MAX_OPERATION_LENGTH:
        raise IdempotencyError(f"operation_type 长度必须在 1..{MAX_OPERATION_LENGTH}")
    if not key_value or len(key_value) > MAX_KEY_LENGTH:
        raise IdempotencyError(f"key_value 长度必须在 1..{MAX_KEY_LENGTH}")
    if len(request_hash) != HASH_LENGTH or any(
        char not in "0123456789abcdef" for char in request_hash.lower()
    ):
        raise IdempotencyError("request_hash 必须是 64 位十六进制 SHA-256")


def begin_operation(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    owner_principal_id: uuid.UUID,
    operation_type: str,
    key_value: str,
    request_hash: str,
) -> IdempotencyResult:
    """占用幂等键（不提交事务）。

    必须在业务写入之前、并在同一个事务中调用。

    Args:
        session: 当前事务的 ORM 会话。
        tenant_id: 租户 UUID。
        owner_principal_id: 所有者 UUID。
        operation_type: 操作类型，如 ``transfer.create``、``bill.recategorize``。
        key_value: 客户端提供的幂等键。
        request_hash: 用 :func:`fingerprint` 计算的请求指纹。

    Returns:
        ``replay`` 为 True 时应直接返回 ``response``，不再执行写入；
        为 False 时调用方继续执行，并在成功后调用 :func:`complete_operation`。

    Raises:
        IdempotencyError: 字段非法。
        IdempotencyConflict: 同一键被用于不同请求。
        IdempotencyInProgress: 同一键的另一次请求仍在执行。
    """
    _validate(operation_type=operation_type, key_value=key_value, request_hash=request_hash)

    # 指纹统一按小写十六进制比较：调用方传大写也应回放到同一条记录，
    # 否则「同键同请求」会因为大小写差异被误判成冲突。
    request_hash = request_hash.lower()

    scope = {
        "tenant_id": tenant_id,
        "owner_principal_id": owner_principal_id,
        "operation_type": operation_type,
        "key_value": key_value,
    }

    # 最多两次：第一次插入可能因对方事务回滚而"看起来"冲突，重试即可成功。
    for _ in range(2):
        inserted_id = session.execute(
            insert(IdempotencyKey.__table__)
            .values(
                **scope,
                request_hash=request_hash,
                status=STATUS_PROCESSING,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    IdempotencyKey.__table__.c.tenant_id,
                    IdempotencyKey.__table__.c.owner_principal_id,
                    IdempotencyKey.__table__.c.operation_type,
                    IdempotencyKey.__table__.c.key_value,
                ]
            )
            .returning(IdempotencyKey.__table__.c.id)
        ).scalar_one_or_none()

        if inserted_id is not None:
            record = session.get(IdempotencyKey, inserted_id)
            if record is None:  # pragma: no cover - 同一事务内必然可见
                raise IdempotencyError("幂等记录插入后不可见")
            return IdempotencyResult(record=record, replay=False, response=None)

        existing = session.execute(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.tenant_id == tenant_id,
                IdempotencyKey.owner_principal_id == owner_principal_id,
                IdempotencyKey.operation_type == operation_type,
                IdempotencyKey.key_value == key_value,
            )
            .with_for_update()
        ).scalar_one_or_none()

        if existing is None:
            continue  # 对方回滚了，键已释放，重试插入

        if existing.request_hash != request_hash:
            raise IdempotencyConflict(
                f"幂等键 {key_value!r} 已用于另一个请求（operation={operation_type}）"
            )
        if existing.status == STATUS_PROCESSING:
            raise IdempotencyInProgress(
                f"幂等键 {key_value!r} 的请求仍在执行（created_at={existing.created_at}）"
            )
        return IdempotencyResult(
            record=existing,
            replay=True,
            response=dict(existing.response) if existing.response else None,
        )

    raise IdempotencyInProgress(f"幂等键 {key_value!r} 正在被并发请求占用")


def complete_operation(
    session: Session,
    record: IdempotencyKey,
    *,
    response: dict[str, Any] | None = None,
    completed_at: datetime | None = None,
) -> IdempotencyKey:
    """把幂等记录标记为成功并保存可回放的结果（不提交事务）。

    Args:
        session: 当前事务的 ORM 会话。
        record: :func:`begin_operation` 返回的记录。
        response: 可回放的响应体（JSON 可序列化）。
        completed_at: 完成时间；缺省为当前 UTC 时间。

    Returns:
        更新后的记录。
    """
    record.status = STATUS_SUCCEEDED
    record.response = dict(response) if response is not None else None
    record.completed_at = completed_at or utcnow()
    session.flush()
    return record


def fail_operation(
    session: Session,
    record: IdempotencyKey,
    *,
    response: dict[str, Any] | None = None,
    completed_at: datetime | None = None,
) -> IdempotencyKey:
    """把幂等记录标记为失败（不提交事务）。

    失败结果同样写入 ``response``，同一键的后续请求可以回放到失败原因，
    而不是重复执行一次可能再次扣款的写操作。

    Args:
        session: 当前事务的 ORM 会话。
        record: :func:`begin_operation` 返回的记录。
        response: 失败详情（JSON 可序列化）。
        completed_at: 完成时间；缺省为当前 UTC 时间。

    Returns:
        更新后的记录。
    """
    record.status = STATUS_FAILED
    record.response = dict(response) if response is not None else None
    record.completed_at = completed_at or utcnow()
    session.flush()
    return record


__all__ = [
    "IdempotencyConflict",
    "IdempotencyError",
    "IdempotencyInProgress",
    "IdempotencyResult",
    "STATUS_FAILED",
    "STATUS_PROCESSING",
    "STATUS_SUCCEEDED",
    "begin_operation",
    "complete_operation",
    "fail_operation",
    "fingerprint",
]
