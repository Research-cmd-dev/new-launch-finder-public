import json

from launchfinder.db import init_db, session_scope
from launchfinder.models import Research, ScanState, Token, utcnow
from launchfinder.scoring.features import extract_features, heuristic_probability
from launchfinder.scoring.outcomes import (
    _update_funder_stats,
    funder_key,
    funder_stats,
    is_funder_wallet,
)
from launchfinder.scoring.paper_gate import paper_hard_veto


def _token(mint: str, funder: str) -> Token:
    t = Token(mint=mint, symbol=mint[:4], source="poll", first_seen_at=utcnow())
    t.research = Research(
        p_good=0.5,
        features_json="{}",
        raw_json=json.dumps({"gmgn": {"fund_from": funder}}),
    )
    return t


def test_funder_stats_accumulate_and_dedupe():
    init_db()
    funder = "26oK69pYx7R25ULts9hLYF2HpTnZ421jPYMPsds9GtYA"
    with session_scope() as session:
        for i in range(3):
            t = _token(f"FunderRugMint{i}11", funder)
            session.add(t)
            session.flush()
            _update_funder_stats(session, t, 0)
            _update_funder_stats(session, t, 0)  # same token twice: no double count
        stats = funder_stats(session, funder)
        assert stats["rugs"] == 3
        assert stats["wins"] == 0


def test_rug_factory_funder_is_penalized():
    base = {
        "coin": {"name": "Test", "symbol": "TST", "reply_count": 30},
        "holders": {"holder_count": 300, "top10_pct": 30, "creator_hold_pct": 5},
        "time_to_migrate_min": 45,
        "market": {"buys_m5": 20, "sells_m5": 10, "liquidity_usd": 20000, "volume_h1": 8000},
    }
    clean = extract_features(base)
    dirty = extract_features({**base, "funder_stats": {"rugs": 3, "wins": 0}})
    forgiven = extract_features({**base, "funder_stats": {"rugs": 3, "wins": 3}})
    assert dirty["funder_rug_n"] == 1.0
    assert clean["funder_rug_n"] == 0.0
    assert forgiven["funder_rug_n"] == 0.0
    flags: list[str] = []
    p_dirty = heuristic_probability(dirty, [], flags)
    p_clean = heuristic_probability(clean, [], [])
    assert p_dirty < p_clean
    assert any("funded by a wallet behind prior rugs" in f for f in flags)
    assert paper_hard_veto(flags) == "prior rugs"


def test_is_funder_wallet_rejects_cex_hop_labels():
    # Live Stamp / TIPPED / ZIP / JEANPHISOL / CHILLPHIL hops.
    assert is_funder_wallet("Binance") is False
    assert is_funder_wallet("Gate.io") is False
    assert is_funder_wallet("Kucoin Wallet") is False
    assert is_funder_wallet("") is False
    assert is_funder_wallet("0xfund") is False
    assert is_funder_wallet("26oK69pYx7R25ULts9hLYF2HpTnZ421jPYMPsds9GtYA") is True
    assert is_funder_wallet("0x1249a3544fbdb3a81763550d3191dc3724f1d3ec") is True


def test_cex_hop_label_does_not_inherit_or_grow_a_factory_tally():
    """Stamp-class: fund_from='Binance' must not share a rug factory key.

    A planted funder:Binance row from older labels is unread. New
    Binance-funded tokens do not increment it. FEATURE_NAMES stays 66.
    Does not late-fill Stamp.
    """
    init_db()
    with session_scope() as session:
        session.add(
            ScanState(
                key=funder_key("Binance"),
                value=json.dumps({"rugs": 9, "wins": 0, "mints": ["old1", "old2", "old3"]}),
            )
        )
        session.flush()
        assert funder_stats(session, "Binance") == {}
        t = _token("StampCexHopMint1111111111111111111", "Binance")
        session.add(t)
        session.flush()
        _update_funder_stats(session, t, 0)
        assert funder_stats(session, "Binance") == {}
        planted = session.query(ScanState).filter(ScanState.key == funder_key("Binance")).one()
        assert json.loads(planted.value)["rugs"] == 9
        feats = extract_features(
            {
                "coin": {"name": "Stamp", "symbol": "Stamp", "reply_count": 30},
                "holders": {"holder_count": 300, "top10_pct": 30, "creator_hold_pct": 5},
                "time_to_migrate_min": 45,
                "market": {"buys_m5": 20, "sells_m5": 10, "liquidity_usd": 20000, "volume_h1": 8000},
                "funder_stats": funder_stats(session, "Binance"),
            }
        )
        assert feats["funder_rug_n"] == 0.0
        flags: list[str] = []
        heuristic_probability(feats, [], flags)
        assert not any("prior rugs" in f for f in flags)
        assert paper_hard_veto(flags) == ""
