'''R1-R6 main-agent end to end in memory: language -> function-calling -> draft card.

No database and no network: the ports are in-memory fakes, so this proves the generalized
main agent routes natural language across all six business scenarios to the correct tool and
that a draft can never claim or cause a write.
'''

import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4
import pytest
from packages.adapters.agent_model import DeterministicAgentModel
from packages.application.confirmations import Confirmations
from packages.application.main_agent import MainAgent
from packages.application.tool_dispatch import ToolDispatcher
from packages.domain.auth import RequestContext
from packages.domain.bank import BankAccount
from packages.domain.confirmations import (ConfirmationNotVisible, ConfirmationRecord, ConfirmationState, MfaChallengeRecord, NewConfirmation, NewMfaChallenge)
from packages.domain.conversations import ConversationScope
from packages.orchestration.tool_schemas import agent_tool_schemas
from packages.security_gates import registry

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


def build() -> tuple[MainAgent, ConversationScope, RequestContext, BankAccount]:
    confirmations = Confirmations(FakeConfirmations())
    ports = {name: (lambda scope: NoWritePort()) for name in ('bank', 'bill', 'wealth', 'card', 'subscription', 'gift', 'transfer')}
    dispatcher = ToolDispatcher(ports, confirmations)
    agent = MainAgent(dispatcher, DeterministicAgentModel())
    owner_id = uuid4()
    context = RequestContext(environment='local', tenant_id='demo-tenant', principal_id=owner_id, role='user', auth_session_id=uuid4(), request_id=uuid4(), trace_id=uuid4())
    scope = ConversationScope(environment='local', tenant_id='demo-tenant', owner_id=owner_id, trace_id=context.trace_id)
    account = BankAccount(account_id=uuid4(), tenant_id='demo-tenant', owner_id=owner_id, display_name='演示账户', account_last4='1001', currency='CNY', status='active', balance_fen=5000000)
    return agent, scope, context, account

def run(agent: MainAgent, scope: ConversationScope, context: RequestContext, account: BankAccount, content: str) -> Any:
    return agent.run(content, context=context, scope=scope, session_id=uuid4(), primary_account=(lambda: account))

def test_agent_tool_schemas_covers_r1_r6_and_server_fills_account_id():
    schemas = {s['function']['name']: s['function'] for s in agent_tool_schemas()}
    @py_assert2 = set(schemas)
    @py_assert5 = {spec.name for spec in registry.specs()}
    @py_assert4 = @py_assert2 == @py_assert5
    if not @py_assert4:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py6)s',), (@py_assert2, @py_assert5)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(schemas) if 'schemas' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(schemas) else 'schemas', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert2 = @py_assert4 = @py_assert5 = None
    @py_assert2 = len(schemas)
    @py_assert5 = 22
    @py_assert4 = @py_assert2 == @py_assert5
    if not @py_assert4:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py6)s',), (@py_assert2, @py_assert5)) % {'py0': @pytest_ar._saferepr(len) if 'len' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(len) else 'len', 'py1': @pytest_ar._saferepr(schemas) if 'schemas' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(schemas) else 'schemas', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert2 = @py_assert4 = @py_assert5 = None
    account_bound = {'r1.smart_transfer', 'r1.schedule_transfer', 'r2.report', 'r6.order_gift', 'r3.redeem_product', 'r6.reserve_budget', 'r3.purchase_product'}
    for name in account_bound:
        props = schemas[name]['parameters']['properties']
        required = schemas[name]['parameters']['required']
        @py_assert0 = 'account_id'
        @py_assert2 = @py_assert0 in props
        if not @py_assert2:
            @py_format4 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py3)s',), (@py_assert0, props)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(props) if 'props' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(props) else 'props'}
            @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
            raise AssertionError(@pytest_ar._format_explanation(@py_format6))
        @py_assert0 = @py_assert2 = None
        @py_assert0 = 'account_id'
        @py_assert2 = @py_assert0 not in required
        if not @py_assert2:
            @py_format4 = @pytest_ar._call_reprcompare(('not in',), (@py_assert2,), ('%(py1)s not in %(py3)s',), (@py_assert0, required)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(required) if 'required' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(required) else 'required'}
            @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
            raise AssertionError(@pytest_ar._format_explanation(@py_format6))
        @py_assert0 = @py_assert2 = None
        @py_assert0 = '服务端'
        @py_assert3 = props['account_id']['description']
        @py_assert2 = @py_assert0 in @py_assert3
        if not @py_assert2:
            @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
            @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
            raise AssertionError(@pytest_ar._format_explanation(@py_format7))
        @py_assert0 = @py_assert2 = @py_assert3 = None
        continue

@pytest.mark.parametrize(('content', 'tool_name'), [('帮我生成这个月的账单月报', 'r2.report'), ('把这笔流水改成餐饮分类', 'r2.categorize'), ('帮我做风险测评', 'r3.risk_assessment'), ('申购 1000 元的理财产品', 'r3.purchase_product'), ('全部赎回我的理财', 'r3.redeem_product'), ('申请一张虚拟卡', 'r4.apply_card'), ('把额度调到 20000 元', 'r4.adjust_limit'), ('限制境外交易', 'r4.set_restriction'), ('解除卡的限制', 'r4.unlock'), ('冻结我的卡', 'r4.freeze'), ('挂失我的卡', 'r4.report_lost'), ('开启续费提醒', 'r5.set_reminder'), ('取消这个订阅', 'r5.cancel'), ('给生日预留 500 元预算', 'r6.reserve_budget'), ('送一束 200 元的鲜花', 'r6.order_gift'), ('取消生日工作流', 'r6.cancel_workflow')])
def test_main_agent_routes_r2_r6_to_a_draft_card(content: str, tool_name: str):
    agent, scope, context, account = build()
    turn = run(agent, scope, context, account, content)
    @py_assert1 = turn.tool_name
    @py_assert3 = @py_assert1 == tool_name
    if not @py_assert3:
        @py_format5 = @pytest_ar._call_reprcompare(('==',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.tool_name\n} == %(py4)s',), (@py_assert1, tool_name)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(tool_name) if 'tool_name' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(tool_name) else 'tool_name'}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert1 = @py_assert3 = None
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
    @py_assert0 = '已生成'
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

def test_red_tool_draft_requires_mfa():
    agent, scope, context, account = build()
    turn = run(agent, scope, context, account, '挂失我的卡')
    @py_assert1 = turn.tool_name
    @py_assert4 = 'r4.report_lost'
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

def test_account_bound_tool_injects_account_id_from_facts():
    agent, scope, context, account = build()
    turn = run(agent, scope, context, account, '帮我生成这个月的账单月报')
    @py_assert1 = turn.tool_name
    @py_assert4 = 'r2.report'
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

def test_write_port_is_never_invoked_for_r2_r6():
    agent, scope, context, account = build()
    for content in ('挂失我的卡', '申购 1000 元的理财产品', '送一束 200 元的鲜花'):
        turn = run(agent, scope, context, account, content)
        @py_assert1 = turn.confirmation
        @py_assert4 = None
        @py_assert3 = @py_assert1 is not @py_assert4
        if not @py_assert3:
            @py_format6 = @pytest_ar._call_reprcompare(('is not',), (@py_assert3,), ('%(py2)s\n{%(py2)s = %(py0)s.confirmation\n} is not %(py5)s',), (@py_assert1, @py_assert4)) % {'py0': @pytest_ar._saferepr(turn) if 'turn' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(turn) else 'turn', 'py2': @pytest_ar._saferepr(@py_assert1), 'py5': @pytest_ar._saferepr(@py_assert4)}
            @py_format8 = 'assert %(py7)s' % {'py7': @py_format6}
            raise AssertionError(@pytest_ar._format_explanation(@py_format8))
        @py_assert1 = @py_assert3 = @py_assert4 = None

