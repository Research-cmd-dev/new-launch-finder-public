import os
import tempfile

import pytest

fd, path = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{path}"
os.environ.setdefault("BACKFILL_LIMIT", "0")
os.environ.setdefault("POLL_SECONDS", "60")


@pytest.fixture(autouse=True)
def _no_holder_rewards_fetch(monkeypatch):
    """The holder-rewards board is a 3MB public page. Tests stay offline."""

    def _quiet() -> int:
        return 0

    monkeypatch.setattr("launchfinder.research.holder_rewards.refresh", _quiet)


@pytest.fixture(autouse=True)
def _no_live_blockscout(monkeypatch):
    """RH wallet maps hit Blockscout. Tests stay offline; hydrate tests
    mock holder_stats instead."""

    async def _empty(*_a, **_k):
        return {}

    monkeypatch.setattr("launchfinder.research.holders._blockscout_holders", _empty)


@pytest.fixture(autouse=True)
def _no_live_dev_dive(monkeypatch):
    """Bloom dives hit Pump / website / FxTwitter. Tests stay offline.

    Stub the bloom hook only — do not patch ``twitter.lookup_handle``
    globally or X-budget tests lose their ``source`` field.
    """

    async def _quiet_dive(_token, _research):
        return {
            "handle": "",
            "tier": "none",
            "delta": 0.0,
            "reasons": [],
            "line": "",
            "github": "",
        }

    async def _no_bloom_alert(**_k):
        return False

    monkeypatch.setattr("launchfinder.scoring.bloom.dive_developer", _quiet_dive)
    monkeypatch.setattr("launchfinder.alerts.notify_bloom", _no_bloom_alert)


@pytest.fixture(autouse=True)
def _isolate_token_rows():
    """Each test seeds its own tokens; clear rows so desk order tests
    are not skewed by leftover / crowded-flat ranks from earlier cases."""
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import (
        Decision,
        EarlyWallet,
        EarlyWalletHit,
        FomoWallet,
        FomoWalletHit,
        HuntCard,
        LiveSample,
        ModelArtifact,
        Outcome,
        PaperFill,
        Research,
        ScanState,
        Snapshot,
        TapeBar,
        Ticket,
        Token,
    )

    init_db()
    session = SessionLocal()
    try:
        session.query(ScanState).filter(ScanState.key == "ledger:seeded").delete()
        # Risk / thesis-weight controls must not leak across tests.
        session.query(ScanState).filter(
            ScanState.key.in_(
                (
                    "risk:controls",
                    "paper_v1:thesis_weights",
                    "paper_v1:rank_policy",
                )
            )
        ).delete(synchronize_session=False)
        # Day locks / promote leases are keyed by book day; tests reuse the
        # same fake days, so a lock from one test must not silence the next.
        for pattern in ("paper_v1:locked:%", "paper_v1:promote_lease:%"):
            session.query(ScanState).filter(ScanState.key.like(pattern)).delete(synchronize_session=False)
        session.query(Ticket).delete()
        session.query(LiveSample).delete()
        session.query(ModelArtifact).delete()
        session.query(PaperFill).delete()
        session.query(Decision).delete()
        session.query(TapeBar).delete()
        session.query(EarlyWalletHit).delete()
        session.query(EarlyWallet).delete()
        session.query(FomoWalletHit).delete()
        session.query(FomoWallet).delete()
        session.query(HuntCard).delete()
        session.query(Snapshot).delete()
        session.query(Outcome).delete()
        session.query(Research).delete()
        session.query(Token).delete()
        session.commit()
    finally:
        session.close()
    # Artifact rows are gone; the 5-minute promoted-artifact caches must
    # not carry a promotion (and its desk lines) into the next test.
    from launchfinder.scoring.batch_fit import reset_artifact_cache
    from launchfinder.scoring.first_sight import reset_cache

    reset_cache()
    reset_artifact_cache()
    yield
