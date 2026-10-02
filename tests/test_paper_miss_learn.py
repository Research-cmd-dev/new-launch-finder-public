"""stack-v204: early-book would-have + copycat carve. Paper-only. No open path."""

from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.desk_lines import SCORER_FIRST_SIGHT
from launchfinder.ledger import (
    _stamp_entry_paper_miss,
    _write_v1_shadow_reason,
)
from launchfinder.models import Decision, Outcome, PaperFill, Research, Token
from launchfinder.scoring.early_book import (
    EARLY_BOOK_KEY,
    EARLY_BOOK_OPEN,
    early_runner_book,
    stamp_early_book_side_key,
    would_have_early_book,
)
from launchfinder.scoring.miss_cohort import (
    PAPER_MISS_FIXTURE,
    PAPER_MISS_FIXTURE_MINT,
    PAPER_MISS_JOIN_KEY,
    PAPER_MISS_REASON_KEY,
    PAPER_MISS_SKIP_REASONS,
    PAPER_MISS_SNAP_KEY,
    PAPER_MISS_WIN_MULT,
    freeze_paper_miss_features,
    has_paper_miss_join,
    is_paper_miss_reason,
    miss_cohort,
    paper_miss_autopsy,
    paper_miss_fixture_autopsy,
    paper_miss_learn,
    paper_miss_membership,
    paper_miss_reason,
    stamp_paper_miss_join,
)
from launchfinder.scoring.paper_gate import (
    PAPER_CHASE_MULT,
    paper_chase_quality,
    paper_fill_verdict,
    paper_is_chase,
)
from launchfinder.scoring.paper_v1 import (
    PAPER_V1_SKIP_EARLY_BOOK,
    PAPER_V1_SKIP_LATE_CHASE,
    PAPER_V1_SKIP_LIVE_COLD,
    PAPER_V1_SKIP_NO_THESIS,
    PAPER_V1_SKIP_SCORE_MISS,
    PAPER_V1_SOL_LIVE,
    is_copycat_skip,
    v1_skip_family,
    v1_skip_stamp,
    v1_thesis_from_features,
)


def test_v202_late_chase_10x_and_quality_bypass_stay():
    assert PAPER_CHASE_MULT == 10.0
    assert PAPER_MISS_WIN_MULT == 5.0
    assert paper_is_chase(39_500, 86_900) is False  # 2.2× SAPLING-class is not a chase
    assert paper_is_chase(39_500, 395_000) is True  # 10× is a chase
    hard = {"github_auth_n": 0.9, "real_project": 1.0, "gmgn_cto": 0.0, "name_quality": 0.2}
    assert paper_chase_quality(features=hard, live_p=0.2)["ok"] is True
    assert paper_fill_verdict(entry_mcap=39_500, last_mcap=395_000, last_liq=16_000) == "late chase"
    assert (
        paper_fill_verdict(
            entry_mcap=39_500,
            last_mcap=395_000,
            last_liq=16_000,
            features=hard,
        )
        == "pass"
    )


def test_paper_miss_reason_taxonomy():
    assert PAPER_V1_SKIP_LATE_CHASE in PAPER_MISS_SKIP_REASONS
    assert PAPER_V1_SKIP_LIVE_COLD in PAPER_MISS_SKIP_REASONS
    assert PAPER_V1_SKIP_SCORE_MISS in PAPER_MISS_SKIP_REASONS
    assert PAPER_V1_SKIP_NO_THESIS in PAPER_MISS_SKIP_REASONS
    assert "v1 meme-q" in PAPER_MISS_SKIP_REASONS
    assert PAPER_V1_SKIP_EARLY_BOOK in PAPER_MISS_SKIP_REASONS
    assert paper_miss_reason("v1 late-chase|d:2026-10-02") == "v1 late-chase"
    assert paper_miss_reason("v1 meme-q|d:2026-10-02") == "v1 meme-q"
    assert paper_miss_reason("v1 early-book|d:2026-10-02") == "v1 early-book"
    assert is_paper_miss_reason("v1 leftover|d:2026-10-02") is False
    assert is_paper_miss_reason("v1 cap|d:2026-10-02") is False
    assert is_paper_miss_reason("v1 veto|copycat|d:2026-10-02") is False
    assert is_paper_miss_reason("v1 veto|cc-fomo|d:2026-10-02") is False


def test_fixture_mint_is_a_paper_miss_runner():
    fx = PAPER_MISS_FIXTURE
    assert fx["mint"] == PAPER_MISS_FIXTURE_MINT
    assert fx["symbol"] == "test"
    assert fx["skip_reason"] == PAPER_V1_SKIP_LATE_CHASE
    assert fx["multiple"] >= 5.0
    early = fx["early"]
    assert early["organic_book"] == 1.0
    assert early["top10_pct"] == 24.0
    assert early["creator_hold_pct"] == 2.0
    assert early["fresh_wallet_pct"] == 82.0
    assert early["liq"] == 16_000.0
    assert early["twitter_followers"] == 12
    assert early["twitter_tweets"] == 0
    assert fx["social_is_tell"] is False
    assert paper_miss_membership(
        chain="sol",
        skip_reason="v1 late-chase|d:2026-10-02",
        features={},
        t0_mcap=fx["t0_mcap"],
        peak_mcap=fx["peak_mcap"],
        last_liq=16_000,
        has_wide_fill=True,
    )
    assert not paper_miss_membership(
        chain="sol",
        skip_reason="v1 leftover|d:2026-10-02",
        features={},
        t0_mcap=fx["t0_mcap"],
        peak_mcap=fx["peak_mcap"],
        has_wide_fill=True,
    )
    card = paper_miss_fixture_autopsy()
    assert card["hit5x"] is True
    assert card["book"]["organic_book"] == 1.0
    assert card["social"]["is_tell"] is False


def test_feature_freeze_is_write_once():
    feat = {
        "holder_n": 0.59,
        "top10_inv": 0.76,
        "fresh_wallet_n": 0.82,
        "buy_pressure": 0.62,
        "liquidity_n": 0.86,
        "volume_n": 0.98,
        "organic_book": 1.0,
        "migrate_speed": 0.9,
        "creator_hold_inv": 0.975,
    }
    snap = freeze_paper_miss_features(
        feat,
        extra={
            "holders": 90,
            "top10_pct": 24,
            "creator_hold_pct": 2,
            "fresh_wallet_pct": 82,
            "liq": 16_000,
            "volume_m5": 39_500,
            "migrate_min": 21,
            "twitter_followers": 12,
            "twitter_tweets": 0,
        },
    )
    assert snap["organic_book"] == 1.0
    assert snap["top10_pct"] == 24.0
    assert snap["twitter_followers"] == 12
    first = stamp_paper_miss_join(feat, reason="v1 late-chase", snap=snap)
    assert first is not None
    assert first[PAPER_MISS_JOIN_KEY] is True
    assert first[PAPER_MISS_REASON_KEY] == "v1 late-chase"
    assert first[PAPER_MISS_SNAP_KEY]["liq"] == 16_000.0
    assert has_paper_miss_join(first)
    second = stamp_paper_miss_join(
        first, reason="v1 live-cold", snap={"organic_book": 0.0, "liq": 1}
    )
    assert second is None
    assert first[PAPER_MISS_SNAP_KEY]["liq"] == 16_000.0
    assert first[PAPER_MISS_REASON_KEY] == "v1 late-chase"


def test_side_key_joins_without_matching_skip_string():
    feat = {PAPER_MISS_JOIN_KEY: True, PAPER_MISS_SNAP_KEY: {"organic_book": 1.0}}
    assert paper_miss_membership(
        chain="sol",
        skip_reason="",
        features=feat,
        t0_mcap=39_500,
        peak_mcap=395_000,
        has_wide_fill=True,
    )
    assert not paper_miss_membership(
        chain="robinhood",
        skip_reason="v1 late-chase",
        features=feat,
        t0_mcap=39_500,
        peak_mcap=395_000,
        has_wide_fill=True,
    )
    assert not paper_miss_membership(
        chain="sol",
        skip_reason="v1 late-chase",
        features={},
        t0_mcap=39_500,
        peak_mcap=80_000,  # ~2×, not a 5× runner
        has_wide_fill=True,
    )


def _sol_token(session, mint, *, t0=39_500, peak=395_000, liq=16_000, reason="v1 late-chase"):
    now = datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)
    tok = Token(
        mint=mint,
        symbol="test",
        chain="sol",
        first_seen_at=now - timedelta(minutes=21),
        migrated_at=now - timedelta(minutes=21),
        source="poll",
    )
    feat = {
        "holder_n": 0.59,
        "top10_inv": 0.76,
        "top1_inv": 0.84,
        "fresh_wallet_n": 0.82,
        "buy_pressure": 0.62,
        "liquidity_n": 0.86,
        "volume_n": 0.98,
        "organic_book": 1.0,
        "migrate_speed": 0.9,
        "creator_hold_inv": 0.975,
        "mcap_per_holder_n": 0.011,
        "twitter_followers_n": 0.05,
        "twitter_age_n": 0.0,
    }
    tok.research = Research(
        features_json=__import__("json").dumps(feat),
        p_good=0.12,
        holder_count=90,
        top10_pct=24.0,
        creator_hold_pct=2.0,
        time_to_migrate_min=21.0,
        twitter_followers=12,
        twitter_tweets=0,
        twitter_age_days=0.0,
        scorer=SCORER_FIRST_SIGHT,
    )
    tok.outcome = Outcome(
        t0_mcap=t0,
        last_mcap=peak,
        last_liq=liq,
        max_mcap=peak,
        multiple=peak / t0,
    )
    session.add(tok)
    session.flush()
    session.add(
        Decision(
            token_id=tok.id,
            mint=tok.mint,
            kind="entry",
            chain="sol",
            at=now,
            source="live",
            entry_p=0.12,
            entry_mcap=t0,
            liq=liq,
            vol_h1=t0,
            holders=90,
            features_json=__import__("json").dumps(feat),
            scorer=SCORER_FIRST_SIGHT,
            image_rev="test",
        )
    )
    session.flush()
    session.add(
        PaperFill(
            chain="sol",
            mint=tok.mint,
            token_id=tok.id,
            line="gated90",
            opened_at=now,
            entry_p=0.12,
            entry_mcap=t0,
            entry_liq=liq,
            max_mcap=peak,
            last_mcap=peak,
            last_liq=liq,
            status="open",
            image_rev="test",
            updated_at=now,
        )
    )
    session.add(
        PaperFill(
            chain="sol",
            mint=tok.mint,
            token_id=tok.id,
            line="paper_v1_shadow",
            opened_at=now,
            entry_p=0.12,
            entry_mcap=t0,
            entry_liq=liq,
            max_mcap=peak,
            last_mcap=peak,
            last_liq=liq,
            status="skipped",
            exit_reason=v1_skip_stamp(reason, "2026-10-02"),
            image_rev="test",
            updated_at=now,
        )
    )
    session.flush()
    return tok


def test_miss_cohort_api_shape_includes_autopsies_and_separators():
    init_db()
    now = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _sol_token(session, PAPER_MISS_FIXTURE_MINT, reason="v1 late-chase")
        _sol_token(
            session,
            "DudMiss1111111111111111111111111111111111111",
            t0=40_000,
            peak=44_000,
            reason="v1 live-cold",
        )
        out = miss_cohort(session, "sol", days=7, now=now)
        assert "paper_misses" in out
        pm = out["paper_misses"]
        assert pm["side_key"] == "paper_miss_join"
        assert pm["win_multiple"] == 5.0
        assert pm["paper_only"] is True
        assert "v1 late-chase" in pm["skip_reasons"]
        assert pm["n_runners"] >= 1
        assert pm["n_duds"] >= 1
        mints = {a["mint"] for a in pm["autopsies"]}
        assert PAPER_MISS_FIXTURE_MINT in mints
        runner = next(a for a in pm["autopsies"] if a["mint"] == PAPER_MISS_FIXTURE_MINT)
        assert runner["hit5x"] is True
        assert runner["skip_reason"] == "v1 late-chase"
        assert runner["book"]["organic_book"] == 1.0
        assert runner["social"]["twitter_followers"] == 12
        feats = {s["feature"] for s in pm["separators"]}
        assert "organic_book" in feats or "fresh_wallet_n" in feats or "buy_pressure" in feats
        assert all(s.get("prefer") == "book_holders_wallets" for s in pm["separators"])
        assert pm["fixture"]["mint"] == PAPER_MISS_FIXTURE_MINT
        wh = pm["would_have"]
        assert wh["open"] is False
        assert wh["knob"] == "early_runner_book"
        assert wh["live_floor_unchanged"] == 0.50
        assert wh["meme_alone"] is False
        assert wh["n_hit_runners"] >= 1
        assert any(i.get("mint") == PAPER_MISS_FIXTURE_MINT for i in wh["items"])
        assert "fomo_trend_no_hunt" in pm
        assert pm["fomo_trend_no_hunt"]["open"] is False
        assert pm["fomo_trend_no_hunt"]["side_key"] == "fomo_trend_no_hunt"


def test_skip_write_stamps_side_key_so_future_skips_join():
    init_db()
    now = datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        tok = Token(
            mint="StampJoin11111111111111111111111111111111",
            symbol="JOIN",
            chain="sol",
            first_seen_at=now,
            migrated_at=now,
            source="poll",
        )
        tok.research = Research(
            features_json='{"organic_book":1.0,"holder_n":0.6,"buy_pressure":0.7}',
            p_good=0.11,
            holder_count=80,
            top10_pct=22.0,
            creator_hold_pct=3.0,
            scorer=SCORER_FIRST_SIGHT,
        )
        tok.outcome = Outcome(t0_mcap=42_000, last_mcap=42_000, last_liq=16_000, max_mcap=42_000)
        session.add(tok)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind="entry",
                chain="sol",
                at=now,
                source="live",
                entry_p=0.11,
                entry_mcap=42_000,
                liq=16_000,
                holders=80,
                features_json='{"organic_book":1.0,"holder_n":0.6,"buy_pressure":0.7}',
                scorer=SCORER_FIRST_SIGHT,
                image_rev="test",
            )
        )
        session.flush()
        wide = PaperFill(
            chain="sol",
            mint=tok.mint,
            token_id=tok.id,
            line="gated90",
            opened_at=now,
            entry_p=0.11,
            entry_mcap=42_000,
            entry_liq=16_000,
            max_mcap=42_000,
            last_mcap=42_000,
            last_liq=16_000,
            status="open",
            image_rev="test",
            updated_at=now,
        )
        session.add(wide)
        session.flush()
        _write_v1_shadow_reason(
            session,
            wide,
            reason=PAPER_V1_SKIP_LIVE_COLD,
            day="2026-10-02",
            buy_mcap=42_000,
            liq=16_000,
            now=now,
        )
        entry = session.query(Decision).filter(Decision.token_id == tok.id, Decision.kind == "entry").one()
        feat = __import__("json").loads(entry.features_json)
        assert feat[PAPER_MISS_JOIN_KEY] is True
        assert feat[PAPER_MISS_REASON_KEY] == "v1 live-cold"
        assert feat[PAPER_MISS_SNAP_KEY]["organic_book"] == 1.0
        assert feat[PAPER_MISS_SNAP_KEY]["holders"] == 80
        # Write-once: a later leftover stamp must not overwrite the freeze.
        assert _stamp_entry_paper_miss(session, tok.id, reason="v1 leftover", now=now) is False
        assert _stamp_entry_paper_miss(session, tok.id, reason="v1 late-chase", now=now) is False
        feat2 = __import__("json").loads(entry.features_json)
        assert feat2[PAPER_MISS_REASON_KEY] == "v1 live-cold"


def test_paper_miss_learn_empty_db_still_surfaces_fixture():
    init_db()
    with session_scope() as session:
        out = paper_miss_learn(session, "sol", days=7)
        assert out["n_runners"] >= 1
        assert out["fixture"]["mint"] == PAPER_MISS_FIXTURE_MINT
        assert any(a["mint"] == PAPER_MISS_FIXTURE_MINT for a in out["autopsies"])
        assert out["side_key"] == "paper_miss_join"


def test_autopsy_prefers_book_over_social():
    card = paper_miss_autopsy(
        mint=PAPER_MISS_FIXTURE_MINT,
        symbol="test",
        skip_reason="v1 late-chase",
        t0_mcap=39_500,
        peak_mcap=395_000,
        early=PAPER_MISS_FIXTURE["early"],
    )
    assert "organic_book" in card["book"]
    assert "top10_pct" in card["book"]
    assert card["social"]["is_tell"] is False
    assert card["social"]["twitter_followers"] == 12
    hard = v1_thesis_from_features(
        {"github_auth_n": 0.9, "real_project": 1.0, "gmgn_cto": 0.0, "name_quality": 0.2}
    )
    assert hard["hard_tags"]
    # Quality bypass from v202 is unchanged — Learn does not open a new path.
    assert paper_fill_verdict(
        entry_mcap=39_500, last_mcap=395_000, last_liq=16_000, features=hard
    ) == "pass"


def test_early_runner_book_catches_fixture_not_dud():
    assert EARLY_BOOK_OPEN is False
    assert PAPER_V1_SOL_LIVE == 0.50
    fx = PAPER_MISS_FIXTURE["early"]
    hit = early_runner_book(fx, t0_mcap=PAPER_MISS_FIXTURE["t0_mcap"])
    assert hit["ok"] is True
    assert hit["open"] is False
    dud = {
        "top1_inv": 0.40,
        "volume_n": 0.50,
        "organic_book": 0.0,
        "liq": 4_000,
        "holders": 8,
    }
    assert early_runner_book(dud).get("ok") is False
    leftover = dict(fx)
    assert early_runner_book(leftover, t0_mcap=1_000_000).get("ok") is False
    # Missing keys fail closed — sparse duds do not inflate recall.
    assert early_runner_book({"top1_inv": 0.9}).get("ok") is False
    # top1_pct inverse (freeze_paper_miss_features) is enough without top1_inv.
    via_pct = dict(fx)
    via_pct.pop("top1_inv", None)
    via_pct["top1_pct"] = 8.0
    assert early_runner_book(via_pct).get("ok") is True
    # Live 0.50 is not a gate — cold Live still matches.
    assert "live" not in " ".join(hit["why"])
    first = stamp_early_book_side_key({}, fx, t0_mcap=39_500)
    assert first is not None and first[EARLY_BOOK_KEY] is True
    assert stamp_early_book_side_key(first, fx) is None
    table = would_have_early_book([fx], [dud, dud])
    assert table["n_hit_runners"] == 1
    assert table["n_hit_duds"] == 0
    assert table["precision"] == 1.0
    assert table["recall"] == 1.0
    assert table["open"] is False
    assert table["evidence"] == "thin"


def test_copycat_family_is_carved_from_miss_denominators():
    assert v1_skip_family("v1 veto|copycat|d:2026-10-02") == "copycat"
    assert v1_skip_family("v1 veto|cc-fomo|d:2026-10-02") == "copycat"
    assert v1_skip_family("v1 live-cold|d:2026-10-02") == "signal"
    assert v1_skip_family("v1 late-chase") == "signal"
    assert v1_skip_family("v1 si-pr|d:2026-10-02") == "si_printer"
    assert is_copycat_skip("v1 veto|copycat") is True
    assert is_copycat_skip("v1 live-cold") is False
    init_db()
    now = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _sol_token(session, PAPER_MISS_FIXTURE_MINT, reason="v1 late-chase")
        tok = Token(
            mint="CopycatDrown11111111111111111111111111111",
            symbol="CC",
            chain="sol",
            first_seen_at=now - timedelta(minutes=10),
            migrated_at=now - timedelta(minutes=10),
            source="poll",
        )
        tok.research = Research(features_json="{}", p_good=0.1, scorer=SCORER_FIRST_SIGHT)
        tok.outcome = Outcome(t0_mcap=40_000, last_mcap=42_000, last_liq=16_000, max_mcap=42_000)
        session.add(tok)
        session.flush()
        session.add(
            PaperFill(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                line="paper_v1_shadow",
                opened_at=now,
                entry_p=0.12,
                entry_mcap=40_000,
                entry_liq=16_000,
                max_mcap=42_000,
                last_mcap=42_000,
                last_liq=16_000,
                status="skipped",
                exit_reason="v1 veto|copycat|d:2026-10-02",
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        pm = paper_miss_learn(session, "sol", days=7, now=now)
        assert pm["n_copycat_suppressed"] >= 1
        assert pm["would_have"]["open"] is False
        # Copycat dud must not land in the runner/dud tables.
        assert all(
            (a.get("skip_reason") or "") != "v1 veto|copycat" for a in pm["autopsies"]
        )
