from launchfinder.db import init_db, session_scope
from launchfinder.scoring.alert_tiers import already_pinged, claim_ping
from launchfinder.scoring.features import FEATURE_NAMES


def test_feature_names_untouched():
    assert len(FEATURE_NAMES) == 66


def test_claim_ping_is_once():
    init_db()
    with session_scope() as session:
        assert claim_ping(session, "bloom", "Mint111111111111111111111111111111111111") is True
        assert already_pinged(session, "bloom", "Mint111111111111111111111111111111111111") is True
        assert claim_ping(session, "bloom", "Mint111111111111111111111111111111111111") is False
        assert claim_ping(session, "lift", "Mint111111111111111111111111111111111111") is True
