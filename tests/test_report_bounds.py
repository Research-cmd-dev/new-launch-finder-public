"""Learn/report query bounds — honest windows, not unbounded joins."""

from launchfinder.db import apply_report_guards, init_db, session_scope
from launchfinder.ledger import (
    PAPER_V1_SUMMARY_CAP,
    paper_v1_summary,
)


def test_report_guards_are_a_noop_on_sqlite():
    init_db()
    with session_scope() as session:
        apply_report_guards(session, timeout_ms=5_000)
        apply_report_guards(session, timeout_ms=45_000)


def test_paper_v1_summary_caps_the_pull():
    import inspect

    from launchfinder import ledger

    src = inspect.getsource(ledger.paper_v1_summary)
    assert "PAPER_V1_SUMMARY_CAP" in src
    assert ".limit(" in src
    init_db()
    with session_scope() as session:
        out = paper_v1_summary(session, "sol")
        assert out["paper_only"] is True
        assert PAPER_V1_SUMMARY_CAP >= 100
        assert "today" in out
        assert out["cap"] >= 1
