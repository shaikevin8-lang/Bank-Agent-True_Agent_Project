'''R1 main-agent end to end in memory: function calling -> dispatch -> confirmation draft card.

No database and no network: the ports are in-memory fakes, so this proves the orchestration
routes natural language to one R1 tool and that a draft can never claim or cause a write.
'''

import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4
from packages.adapters.agent_model import DeterministicAgentModel
from packages.application.confirmations import Confirmations
from packages.application.r1_agent import R1Agent
from packages.application.tool_dispatch import ToolDispatcher
from packages.domain.auth import RequestContext
from packages.domain.bank import BankAccount, FactNotVisible
from packages.domain.confirmations import (ConfirmationNotVisible, ConfirmationRecord, ConfirmationState, MfaChallengeRecord, NewConfirmation, NewMfaChallenge)
from packages.domain.conversations import ConversationScope
from packages.orchestration.tool_schemas import r1_tool_schemas

class FakeConfirmations:
    def __init__(self) -> None:
        self.records = {}
        self.challenges = {}

    def insert(self, scope: ConversationScope, record: NewConfirmation) -> ConfirmationRecord:
        value = ConfirmationRecord(tenant_id=scope.tenant_id, owner_id=scope.owner_id, state='pending', decided_at=None, created_at=datetime.now(UTC), **record.model_dump())
        self.records[value.confirmation_id] = value
        return value

    def find(self, scope: ConversationScope, confirmation_id: UUID, *, for_update: bool=False) -> ConfirmationRecord:
        try:
            return self.records[confirmation_id]
        except KeyError:
            raise ConfirmationNotVisible('not visible') from None

    def set_state(self, scope: ConversationScope, confirmation_id: UUID, state: ConfirmationState, decided_at: datetime) -> ConfirmationRecord:
        current = self.find(scope, confirmation_id, for_update=True)
        updated = current.model_copy(update={'state': state, 'decided_at': decided_at})
        self.records[confirmation_id] = updated
        return updated

    def insert_mfa(self, scope: ConversationScope, challenge: NewMfaChallenge) -> MfaChallengeRecord:
        value = MfaChallengeRecord(attempts=0, consumed_at=None, created_at=datetime.now(UTC), **challenge.model_dump())
        self.challenges[value.confirmation_id] = value
        return value

    def find_mfa(self, scope: ConversationScope, confirmation_id: UUID, *, for_update: bool=False) -> MfaChallengeRecord | None:
        return self.challenges.get(confirmation_id)

    def fail_mfa(self, scope: ConversationScope, challenge_id: UUID) -> int:
        return 1

    def consume_mfa(self, scope: ConversationScope, challenge_id: UUID, at: datetime) -> None:
        pass


class NoWritePort:
    def execute(self, scope: ConversationScope, params: Any, *, idempotency_key: UUID, session_id: UUID | None=None) -> dict[str, Any]:
        raise AssertionError('The draft chain must never execute a write')


def build(*, balance_fen: int=5000000) -> tuple[R1Agent, ConversationScope, RequestContext, BankAccount]:
    confirmations = Confirmations(FakeConfirmations())
    dispatcher = ToolDispatcher({'bank': (lambda scope: NoWritePort()), 'transfer': (lambda scope: NoWritePort())}, confirmations)
    agent = R1Agent(dispatcher, DeterministicAgentModel())
    owner_id = uuid4()
    context = RequestContext(environment='local', tenant_id='demo-tenant', principal_id=owner_id, role='user', auth_session_id=uuid4(), request_id=uuid4(), trace_id=uuid4())
    scope = ConversationScope(environment='local', tenant_id='demo-tenant', owner_id=owner_id, trace_id=context.trace_id)
    account = BankAccount(account_id=uuid4(), tenant_id='demo-tenant', owner_id=owner_id, display_name='演示账户', account_last4='1001', currency='CNY', status='active', balance_fen=balance_fen)
    return agent, scope, context, account

def run(agent: R1Agent, scope: ConversationScope, context: RequestContext, account: BankAccount, content: str) -> Any:
    return agent.run(content, context=context, scope=scope, session_id=uuid4(), primary_account=(lambda: account))

def test_smart_transfer_produces_a_pending_draft_card():
    agent, scope, context, account = build()
    turn = run(agent, scope, context, account, '给张三转 100 元')
    @py_assert1 = turn.tool_name
    @py_assert4 = 'r1.smart_transfer'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.tool_name\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = turn.confirmation
    @py_assert4 = None
    @py_assert3 = @py_assert1 is not @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is not',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.confirmation\n} is not %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = turn.confirmation
    @py_assert3 = @py_assert1.state
    @py_assert6 = 'pending'
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.confirmation\n}.state\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = turn.confirmation
    @py_assert3 = @py_assert1.risk
    @py_assert6 = 'yellow'
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.confirmation\n}.risk\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = turn.confirmation
    @py_assert3 = @py_assert1.amount_fen
    @py_assert6 = 10000
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.confirmation\n}.amount_fen\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = turn.confirmation
    @py_assert3 = @py_assert1.requires_mfa
    @py_assert6 = False
    @py_assert5 = @py_assert3 is @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('is',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.confirmation\n}.requires_mfa\n} is %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert0 = '已生成转账草稿'
    @py_assert4 = turn.reply
    @py_assert2 = @py_assert0 in @py_assert4
    if not @py_assert2:
        @py_format6 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py5)s\n{%(py5)s = %(py3)s.reply\n}',), (@py_assert0, @py_assert4)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert4 = None
    @py_assert0 = '100.00'
    @py_assert4 = turn.reply
    @py_assert2 = @py_assert0 in @py_assert4
    if not @py_assert2:
        @py_format6 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py5)s\n{%(py5)s = %(py3)s.reply\n}',), (@py_assert0, @py_assert4)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert4 = None
    @py_assert0 = '未执行任何资金操作'
    @py_assert4 = turn.reply
    @py_assert2 = @py_assert0 in @py_assert4
    if not @py_assert2:
        @py_format6 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py5)s\n{%(py5)s = %(py3)s.reply\n}',), (@py_assert0, @py_assert4)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert4 = None

def test_large_transfer_is_red_and_requires_mfa():
    agent, scope, context, account = build()
    turn = run(agent, scope, context, account, '给张三转 12000 元')
    @py_assert1 = turn.confirmation
    @py_assert4 = None
    @py_assert3 = @py_assert1 is not @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is not',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.confirmation\n} is not %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = turn.confirmation
    @py_assert3 = @py_assert1.risk
    @py_assert6 = 'red'
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.confirmation\n}.risk\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = turn.confirmation
    @py_assert3 = @py_assert1.requires_mfa
    @py_assert6 = True
    @py_assert5 = @py_assert3 is @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('is',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.confirmation\n}.requires_mfa\n} is %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None
    @py_assert1 = turn.confirmation
    @py_assert3 = @py_assert1.mfa_challenge_id
    @py_assert6 = None
    @py_assert5 = @py_assert3 is not @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('is not',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.confirmation\n}.mfa_challenge_id\n} is not %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None

def test_schedule_transfer_routes_to_the_scheduled_tool():
    agent, scope, context, account = build()
    turn = run(agent, scope, context, account, '明天定时给张三转 100 元')
    @py_assert1 = turn.tool_name
    @py_assert4 = 'r1.schedule_transfer'
    @py_assert3 = @py_assert1 == @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.tool_name\n} == %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = turn.confirmation
    @py_assert4 = None
    @py_assert3 = @py_assert1 is not @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is not',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.confirmation\n} is not %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = turn.confirmation
    @py_assert3 = @py_assert1.amount_fen
    @py_assert6 = 10000
    @py_assert5 = @py_assert3 == @py_assert6
    if not @py_assert5:
        @py_format8 = @pytest_ar._call_reprcompare(('==',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s\n{%(py2)s = %(py0)s.confirmation\n}.amount_fen\n} == %(py7)s',), (@py_assert3, @py_assert6)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = 'assert %(py9)s' % {'py9': @py_format8}
        raise AssertionError(@pytest_ar._format_explanation(@py_format10))
    @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = None

def test_missing_slots_ask_for_clarification_without_a_card():
    agent, scope, context, account = build()
    turn = run(agent, scope, context, account, '我想转点钱')
    @py_assert1 = turn.tool_name
    @py_assert4 = None
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.tool_name\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = turn.confirmation
    @py_assert4 = None
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.confirmation\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert0 = '请补充'
    @py_assert4 = turn.reply
    @py_assert2 = @py_assert0 in @py_assert4
    if not @py_assert2:
        @py_format6 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py5)s\n{%(py5)s = %(py3)s.reply\n}',), (@py_assert0, @py_assert4)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert4 = None

def test_balance_query_is_unsupported_without_a_card():
    agent, scope, context, account = build()
    turn = run(agent, scope, context, account, '帮我查一下余额')
    @py_assert1 = turn.tool_name
    @py_assert4 = None
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.tool_name\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert1 = turn.confirmation
    @py_assert4 = None
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.confirmation\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert0 = '尚未接入'
    @py_assert4 = turn.reply
    @py_assert2 = @py_assert0 in @py_assert4
    if not @py_assert2:
        @py_format6 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py5)s\n{%(py5)s = %(py3)s.reply\n}',), (@py_assert0, @py_assert4)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert4 = None

def test_missing_payer_account_returns_a_fixed_failure():
    agent, scope, context, account = build()
    def missing() -> BankAccount:
        raise FactNotVisible('not visible')

    turn = agent.run('给张三转 100 元', context=context, scope=scope, session_id=uuid4(), primary_account=missing)
    @py_assert1 = turn.confirmation
    @py_assert4 = None
    @py_assert3 = @py_assert1 is @py_assert4
    if not @py_assert3:
        @py_format6 = @pytest_ar._call_reprcompare(('is',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.confirmation\n} is %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert1 = @py_assert3 = @py_assert4 = None
    @py_assert0 = '未执行任何资金操作'
    @py_assert4 = turn.reply
    @py_assert2 = @py_assert0 in @py_assert4
    if not @py_assert2:
        @py_format6 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py5)s\n{%(py5)s = %(py3)s.reply\n}',), (@py_assert0, @py_assert4)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py5': @pytest_ar._saferepr(@py_assert4)}
        @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
        raise AssertionError(@pytest_ar._format_explanation(@py_format8))
    @py_assert0 = @py_assert2 = @py_assert4 = None

def test_write_port_is_never_invoked():
    agent, scope, context, account = build()
    turn = run(agent, scope, context, account, '给张三转 12000 元')
    @py_assert1 = []
    @py_assert3 = turn.confirmation
    @py_assert6 = None
    @py_assert5 = @py_assert3 is not @py_assert6
    @py_assert0 = @py_assert5
    if @py_assert5:
        @py_assert12 = turn.confirmation
        @py_assert14 = @py_assert12.requires_mfa
        @py_assert17 = True
        @py_assert16 = @py_assert14 is @py_assert17
        @py_assert0 = @py_assert16
    if not @py_assert0:
        @py_format8 = @pytest_ar._call_reprcompare(('is not',), (@py_assert5,), ('%(py4)s\n{%(py4)s = %(py2)s.confirmation\n} is not %(py7)s',), (@py_assert3, @py_assert6)) % {'py2': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py4': @pytest_ar._saferepr(@py_assert3), 'py7': @pytest_ar._saferepr(@py_assert6)}
        @py_format10 = '%(py9)s' % {'py9': @py_format8}
        @py_assert1.append(@py_format10)
        if @py_assert5:
            @py_format19 = @pytest_ar._call_reprcompare(('is',), (@py_assert16,), ('%(py15)s\n{%(py15)s = %(py13)s\n{%(py13)s = %(py11)s.confirmation\n}.requires_mfa\n} is %(py18)s',), (@py_assert14, @py_assert17)) % {'py11': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py13': @pytest_ar._saferepr(@py_assert12), 'py15': @pytest_ar._saferepr(@py_assert14), 'py18': @pytest_ar._saferepr(@py_assert17)}
            @py_format21 = '%(py20)s' % {'py20': @py_format19}
            @py_assert1.append(@py_format21)
        @py_format22 = @pytest_ar._format_boolop(@py_assert1, 0) % {}
        @py_format24 = 'assert %(py23)s' % {'py23': @py_format22}
        raise AssertionError(@pytest_ar._format_explanation(@py_format24))
    @py_assert0 = @py_assert1 = @py_assert3 = @py_assert5 = @py_assert6 = @py_assert12 = @py_assert14 = @py_assert16 = @py_assert17 = None

def test_schema_marks_account_id_as_server_filled():
    schemas = {s['function']['name']: s['function'] for s in r1_tool_schemas()}
    smart = schemas['r1.smart_transfer']
    @py_assert0 = 'account_id'
    @py_assert3 = smart['parameters']['properties']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = 'account_id'
    @py_assert3 = smart['parameters']['required']
    @py_assert2 = @py_assert0 not in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = 'payee_name'
    @py_assert3 = smart['parameters']['required']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = 'amount_fen'
    @py_assert3 = smart['parameters']['required']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None
    @py_assert0 = '服务端'
    @py_assert3 = smart['parameters']['properties']['account_id']['description']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

