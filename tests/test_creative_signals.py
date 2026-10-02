import json

from launchfinder.db import init_db, session_scope
from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
from launchfinder.scoring import outcomes as outcomes_mod
from launchfinder.scoring.outcomes import (
    _credit_alpha_wallets,
    alpha_wallet_count,
    extract_meta_words,
    hot_metas,
)


def test_meta_word_extraction():
    words = extract_meta_words("Grok Cat Official", "GCAT")
    assert "grok" in words and "cat" in words and "gcat" in words
    assert "official" not in words  # stopword


def test_hot_metas_from_recent_winners():
    init_db()
    outcomes_mod._meta_cache = {}  # bust cache
    with session_scope() as session:
        for i, name in enumerate(["Space Cat", "cat wizard", "unrelated dog"]):
            t = Token(mint=f"MetaWinMint{i}11", symbol=f"MW{i}", name=name, source="poll", first_seen_at=utcnow())
            session.add(t)
            session.flush()
            session.add(Outcome(token_id=t.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 8, multiple=8.0, label=1))
        hot = hot_metas(session)
    assert "cat" in hot          # two distinct winners share it
    assert "dog" not in hot      # only one winner
    outcomes_mod._meta_cache = {}


def test_alpha_wallet_registry_roundtrip():
    init_db()
    with session_scope() as session:
        t = Token(mint="AlphaRunMint1111", symbol="ARUN", source="poll", first_seen_at=utcnow())
        t.research = Research(p_good=0.7, features_json="{}")
        session.add(t)
        session.flush()
        session.add(
            ScanState(
                key=f"runnerp:{t.mint}",
                value=json.dumps({"forensics": {"top_addresses": ["WalletAAA", "WalletBBB"]}}),
            )
        )
        session.flush()
        _credit_alpha_wallets(session, t)
        _credit_alpha_wallets(session, t)  # same run never double-counts
        assert alpha_wallet_count(session, ["WalletAAA"]) == 0  # 1 run < threshold

        # second confirmed runner with the same early wallet crosses the bar
        t2 = Token(mint="AlphaRunMint2222", symbol="ARN2", source="poll", first_seen_at=utcnow())
        t2.research = Research(p_good=0.7, features_json="{}")
        session.add(t2)
        session.flush()
        session.add(
            ScanState(
                key=f"runnerp:{t2.mint}",
                value=json.dumps({"forensics": {"top_addresses": ["WalletAAA"]}}),
            )
        )
        session.flush()
        _credit_alpha_wallets(session, t2)
        assert alpha_wallet_count(session, ["WalletAAA", "WalletZZZ"]) == 1
