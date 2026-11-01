import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import pytest
from pydantic import ValidationError
from packages.domain.task_fixtures import ConfirmationSummary, TaskSummary
from tests.task_samples import samples

@pytest.mark.parametrize('change', [{'evidence_refs': ()}, {'completion_summary': None}, {'next_action': 'WAIT'}, {'is_fixture': False}, {'version': True}, {'tenant_id': 'forged'}])
def test_success_fixture_cannot_claim_execution_or_omit_evidence(change):
    task = next((t for t, _, _ in samples() if t.state == 'SUCCEEDED'))
    with pytest.raises(ValidationError):
        TaskSummary.model_validate(task.model_dump() | change)

@pytest.mark.parametrize('state', ['UNKNOWN', 'MANUAL_REVIEW', 'AWAITING_CONFIRMATION'])
def test_required_next_action_is_preserved(state):
    task = next((t for t, _, _ in samples() if t.state == state))
    with pytest.raises(ValidationError):
        TaskSummary.model_validate(task.model_dump() | {'next_action': 'NONE'})

@pytest.mark.parametrize('change', [{'executable': True}, {'risk_level': 'green'}, {'plan_version': 1}, {'display_lines': ('',)}])
def test_unavailable_confirmation_has_no_execution_plan(change):
    confirmation = samples()[0][2]
    with pytest.raises(ValidationError):
        ConfirmationSummary.model_validate(confirmation.model_dump() | change)

