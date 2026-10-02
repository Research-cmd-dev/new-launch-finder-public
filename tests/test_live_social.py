import asyncio
import json

from launchfinder.db import SessionLocal, init_db, session_scope
from launchfinder.models import Research, ScanState, Token, utcnow
from launchfinder.research import live_social, twitter
from launchfinder.research.live_social import (
    MAX_PER_CYCLE,
    compute_social_delta,
    fold_live_social,
    live_social_delta,
    refresh_live_social,
    worth_live_x_spend,
)
from launchfinder.scoring.bloom import promise_score
from launchfinder.scoring.features import FEATURE_NAMES


def test_feature_names_stay_sixty_six() -> None:
    assert len(FEATURE_NAMES) == 66


def test_first_scan_is_baseline_only() -> None:
    delta, reasons = compute_social_delta(mentions_now=40, followers_now=900)
    assert delta == 0.0
    assert any("baseline" in row.lower() for row in reasons)


def test_mentions_doubling_adds_live_score() -> None:
    delta, reasons = compute_social_delta(
        mentions_now=24,
        mentions_prev=10,
        followers_now=400,
        followers_prev=400,
    )
    assert delta == 0.05
    assert any("doubled" in row.lower() for row in reasons)


def test_mentions_fade_cuts_live_score() -> None:
    delta, reasons = compute_social_delta(
        mentions_now=3,
        mentions_prev=20,
        followers_now=400,
        followers_prev=400,
    )
    assert delta == -0.05
    assert any("faded" in row.lower() for row in reasons)


def test_follower_climb_is_capped() -> None:
    delta, _ = compute_social_delta(
        mentions_now=24,
        mentions_prev=10,
        followers_now=1200,
        followers_prev=400,
    )
    assert 0.05 < delta <= 0.08


def test_fold_does_not_revive_hard_zero() -> None:
    db = SessionLocal()
    try:
        db.add(
            ScanState(
                key="livesocial:MintSocial111111111111111111111111111",
                value=json.dumps({"delta": 0.08, "reasons": ["X mentions doubled (8 → 24/h)"]}),
            )
        )
        db.commit()
        p, extra = fold_live_social(db, "MintSocial111111111111111111111111111", 0.0)
        assert p == 0.0
        assert extra == []
        p, extra = fold_live_social(db, "MintSocial111111111111111111111111111", 0.70)
        assert abs(p - 0.78) < 1e-6
        assert extra
        assert live_social_delta(db, "MintSocial111111111111111111111111111")[0] == 0.08
    finally:
        db.close()


def test_live_social_skips_brand_and_keeps_midsize() -> None:
    assert MAX_PER_CYCLE == 8
    assert worth_live_x_spend("NASA", followers=92_390_162, flags=["Claimed celebrity/brand X — not this launch"]) is False
    assert worth_live_x_spend("launchonquick", followers=4_200, flags=[]) is True
    assert worth_live_x_spend("tinynew", followers=12, flags=[]) is False
    assert worth_live_x_spend(
        "middev",
        followers=8_000,
        flags=["Same ticker launched repeatedly in 24h (copycat spam)"],
    ) is False


def test_tweet_climb_adds_live_score() -> None:
    delta, reasons = compute_social_delta(
        followers_now=400,
        followers_prev=400,
        tweets_now=40,
        tweets_prev=20,
    )
    assert delta == 0.04
    assert any("tweets" in row.lower() for row in reasons)


def test_bio_match_change_adds_live_score() -> None:
    delta, reasons = compute_social_delta(
        followers_now=400,
        followers_prev=400,
        tweets_now=20,
        tweets_prev=20,
        bio_now="building $QUICK on solana",
        bio_prev="just a guy",
        symbol="QUICK",
        name="Quick",
    )
    assert delta == 0.02
    assert any("bio" in row.lower() for row in reasons)


def test_follower_only_scan_does_not_need_mentions() -> None:
    delta, reasons = compute_social_delta(
        mentions_now=0,
        mentions_prev=None,
        followers_now=1200,
        followers_prev=400,
    )
    assert delta == 0.03
    assert any("followers" in row.lower() for row in reasons)


def test_refresh_live_social_never_calls_mention_count(monkeypatch) -> None:
    async def boom(*_a, **_k):
        raise AssertionError("live social must not spend a counts credit")

    async def fake_lookup(handle):
        return {
            "handle": handle,
            "followers": 4200,
            "tweets": 180,
            "bio": "building $QUICK",
            "source": "fxtwitter",
        }

    monkeypatch.setattr(twitter, "mention_count", boom)
    monkeypatch.setattr(twitter, "lookup_handle", fake_lookup)
    monkeypatch.setattr(live_social, "_due", lambda _session: True)
    monkeypatch.setattr(live_social, "_stale", lambda _session, _mint: True)

    init_db()
    with session_scope() as session:
        token = Token(
            mint="LiveSocMint11111111111111111111111111",
            symbol="QUICK",
            name="Quick",
            chain="sol",
            twitter="https://x.com/launchonquick",
            first_seen_at=utcnow(),
        )
        token.research = Research(
            twitter_handle="launchonquick",
            twitter_followers=4200,
            risk_flags_json="[]",
        )
        session.add(token)
        session.flush()
        monkeypatch.setattr(live_social, "_candidate_mints", lambda _session: [token.mint])
        wrote = asyncio.run(refresh_live_social(session))
        assert wrote == 1
        key = live_social.social_key(token.mint)
        row = next(
            (obj for obj in list(session.new) + list(session.dirty) if isinstance(obj, ScanState) and obj.key == key),
            None,
        )
        assert row is not None
        stored = json.loads(row.value)
        assert stored["mentions"] == 0
        assert stored["followers"] == 4200
        assert stored["tweets"] == 180
        assert stored["handle"] == "launchonquick"
        assert token.research.p_good == 0.0
        assert token.research.twitter_tweets == 180


def test_copycat_still_hard_stops_before_social() -> None:
    p, _ = promise_score(
        chain="sol",
        entry_p=0.9,
        multiple=3.0,
        last_mcap=300_000,
        t0_mcap=100_000,
        max_mcap=300_000,
        last_liq=40_000,
        holders=200,
        top10_pct=20,
        vol_h1=50_000,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=["Same ticker launched repeatedly in 24h (copycat spam)"],
    )
    assert p == 0.0
