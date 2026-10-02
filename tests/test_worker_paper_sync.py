"""paper_sync heartbeat must survive promote/lock failures."""

from launchfinder.db import init_db
from launchfinder.worker import _sync_paper_ledger


def test_paper_sync_beats_when_promote_raises(monkeypatch):
    init_db()
    beats: list[str] = []

    def capture_beat(name: str, note: str = "") -> None:
        beats.append(name)

    monkeypatch.setattr("launchfinder.worker._beat", capture_beat)
    monkeypatch.setattr(
        "launchfinder.scoring.thesis_enrich.repair_thin_entry_thesis",
        lambda session, limit=200: {"patched": 0},
    )
    monkeypatch.setattr(
        "launchfinder.scoring.thesis_enrich.repair_desk_thesis_coverage",
        lambda session, **kw: {},
    )
    monkeypatch.setattr(
        "launchfinder.scoring.sanity_jobs.apply_sanity_jobs_sync",
        lambda session, chain, actions=None: {},
    )
    monkeypatch.setattr(
        "launchfinder.ledger.sync_paper_ledger",
        lambda session, chain: {"opened": 0, "marked": 3},
    )
    monkeypatch.setattr(
        "launchfinder.ledger.reconsider_paper_v1",
        lambda session, chain: {"queued": 0, "opened": 0, "shadow": 0},
    )
    monkeypatch.setattr(
        "launchfinder.ledger.reconsider_copycat_veto_shadow",
        lambda session, chain: {"shadow": 0},
    )
    monkeypatch.setattr(
        "launchfinder.ledger.reconsider_high_si_fomo_shadow",
        lambda session, chain: {"shadow": 0},
    )
    monkeypatch.setattr(
        "launchfinder.ledger.reconsider_meme_quality_shadow",
        lambda session, chain: {"shadow": 0},
    )

    def promote_fail(session, *, now=None):
        raise RuntimeError("promote blew up")

    monkeypatch.setattr("launchfinder.ledger.promote_paper_v1_queue", promote_fail)
    monkeypatch.setattr(
        "launchfinder.ledger.lock_paper_v1",
        lambda session, **kw: {"opened": 0, "skipped": 0},
    )
    monkeypatch.setattr(
        "launchfinder.scoring.thesis_weights.refit_thesis_weights",
        lambda session: {"fitted": False, "n": 0},
    )

    out = _sync_paper_ledger()
    assert out.get("marked", 0) >= 3
    assert beats == ["paper_sync"]
