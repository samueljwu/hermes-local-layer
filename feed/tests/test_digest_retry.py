"""Bounded retry policy; no network or live-state writes."""
import pytest
from test_digest_contamination_atomicity import load_feed_ops


@pytest.mark.parametrize('dry_run', [False, True])
def test_only_protected_input_changes_retry_once(monkeypatch, capsys, dry_run):
    ops = load_feed_ops()
    calls = []
    def attempt(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ops.ProtectedInputsChanged('fixture churn')
        return 'fresh digest'
    monkeypatch.setattr(ops, 'digest_attempt', attempt)
    assert ops.digest(dry_run=dry_run) == 'fresh digest'
    assert calls == [{'dry_run': dry_run}] * 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'retrying once with fresh signals' in captured.err


@pytest.mark.parametrize('error', [ValueError('invalid state'), RuntimeError('publication failed'), SystemExit('validation failed')])
def test_other_errors_are_not_retried(monkeypatch, error):
    ops = load_feed_ops()
    calls = []
    def attempt(**kwargs):
        calls.append(kwargs)
        raise error
    monkeypatch.setattr(ops, 'digest_attempt', attempt)
    with pytest.raises(type(error)):
        ops.digest()
    assert len(calls) == 1


def test_persistent_churn_fails_after_two_attempts(monkeypatch):
    ops = load_feed_ops()
    calls = []
    def attempt(**kwargs):
        calls.append(kwargs)
        raise ops.ProtectedInputsChanged('fixture churn')
    monkeypatch.setattr(ops, 'digest_attempt', attempt)
    with pytest.raises(ops.ProtectedInputsChanged):
        ops.digest()
    assert len(calls) == 2
