"""stack-v206: FOMO trending → no-Hunt Learn grain. Paper only. No open."""

from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.desk_lines import SCORER_FIRST_SIGHT
from launchfinder.models import Decision, HuntCard, Outcome, Research, ScanState, Token
from launchfinder.scoring.early_book import EARLY_BOOK_OPEN
from launchfinder.scoring.features import FEATURE_NAMES
from launchfinder.scoring.fomo_trend_no_hunt import (
    FOMO_NO_HUNT_AUTOPSY_LIMIT,
    FOMO_NO_HUNT_OPEN,
    FOMO_NO_HUNT_SEPARATOR_KEYS,
    FOMO_TREND_NO_HUNT_KEY,
    FOMO_TREND_NO_HUNT_SNAP_KEY,
    FOMO_TREND_NO_HUNT_WHY_KEY,
    WHY_AGED_OFF,
    WHY_DOOR_MISS,
    WHY_GATE,
    WHY_HISTORICAL,
    WHY_NEVER_CARDED,
    WHY_SCORE_FLOOR,
    capture_fomo_trend_no_hunt,
    fomo_trend_no_hunt_learn,
    freeze_fomo_no_hunt_snap,
    has_fomo_trend_no_hunt,
    is_fomo_no_hunt_item,
    resolve_fomo_no_hunt_why,
    stamp_fomo_trend_no_hunt,
    why_not_hunt,
)
from launchfinder.scoring.miss_cohort import miss_cohort, paper_miss_learn
from launchfinder.scoring.paper_v1 import PAPER_V1_SOL_LIVE


NOW = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
AGED_MINT = "FomoNoHuntAged111111111111111111111111111"
DESK_MINT = "FomoNoHuntDesk111111111111111111111111111"
MISS_MINT = "FomoNoHuntMiss111111111111111111111111111"
HUNT_MINT = "FomoOnHunt1111111111111111111111111111111"


def test_constants_stay_paper_only():
    assert FOMO_NO_HUNT_OPEN is False
    assert EARLY_BOOK_OPEN is False
    assert PAPER_V1_SOL_LIVE == 0.50
    assert FOMO_TREND_NO_HUNT_KEY == "fomo_trend_no_hunt"
    assert FOMO_TREND_NO_HUNT_KEY not in FEATURE_NAMES
    assert len(FEATURE_NAMES) == 66
    assert FOMO_NO_HUNT_AUTOPSY_LIMIT >= 32
    assert "holders" in FOMO_NO_HUNT_SEPARATOR_KEYS
    assert "liq" in FOMO_NO_HUNT_SEPARATOR_KEYS
    assert "entry_p" in FOMO_NO_HUNT_SEPARATOR_KEYS
    assert "vol_h1" in FOMO_NO_HUNT_SEPARATOR_KEYS


def test_why_taxonomy_and_membership():
    assert is_fomo_no_hunt_item({"on_desk": True, "on_hunt": False, "status": "caught"})
    assert is_fomo_no_hunt_item({"on_desk": False, "on_hunt": False, "status": "miss"})
    assert not is_fomo_no_hunt_item({"on_desk": True, "on_hunt": True, "status": "caught"})
    assert not is_fomo_no_hunt_item({"on_desk": False, "on_hunt": False, "status": "leftover"})
    assert why_not_hunt({"on_desk": True, "on_hunt": True, "status": "caught"}) == ""
    assert why_not_hunt({"on_desk": False, "on_hunt": False, "status": "miss"}) == WHY_DOOR_MISS
    assert why_not_hunt({"on_desk": True, "on_hunt": False, "status": "caught"}) == WHY_NEVER_CARDED
    assert (
        why_not_hunt(
            {
                "on_desk": True,
                "on_hunt": False,
                "status": "caught",
                "gate_veto": "copycat spam",
                "entry_p": 0.20,
            }
        )
        == WHY_GATE
    )
    assert (
        why_not_hunt(
            {
                "on_desk": True,
                "on_hunt": False,
                "status": "caught",
                "entry_p": 0.01,
            }
        )
        == WHY_SCORE_FLOOR
    )


def test_why_actionable_beats_blanket_historical():
    hist = Token(
        mint="HistWhy1111111111111111111111111111111111",
        symbol="HIST",
        chain="sol",
        is_historical=True,
        first_seen_at=NOW - timedelta(hours=2),
        migrated_at=NOW - timedelta(hours=2),
        created_at_chain=NOW - timedelta(hours=2),
    )
    hist.research = Research(
        features_json="{}",
        p_good=0.01,
        holder_count=40,
        risk_flags_json="[]",
        scorer=SCORER_FIRST_SIGHT,
    )
    hist.outcome = Outcome(t0_mcap=40_000, last_mcap=42_000, last_liq=12_000, max_mcap=42_000)
    item = {
        "on_desk": True,
        "on_hunt": False,
        "status": "caught",
        "entry_p": 0.01,
    }
    assert why_not_hunt(item, token=hist, now=NOW) == WHY_SCORE_FLOOR
    assert why_not_hunt({**item, "gate_veto": "copycat spam"}, token=hist, now=NOW) == WHY_GATE
    aged = Token(
        mint="HistAged111111111111111111111111111111111",
        symbol="AGEDH",
        chain="sol",
        is_historical=True,
        first_seen_at=NOW - timedelta(hours=22),
        migrated_at=NOW - timedelta(hours=22),
        created_at_chain=NOW - timedelta(hours=22),
    )
    aged.research = Research(
        features_json="{}",
        p_good=0.12,
        holder_count=40,
        risk_flags_json="[]",
        scorer=SCORER_FIRST_SIGHT,
    )
    aged.outcome = Outcome(t0_mcap=15_000, last_mcap=90_000, last_liq=12_000, max_mcap=90_000)
    assert (
        why_not_hunt(
            {"on_desk": True, "on_hunt": False, "status": "caught", "entry_p": 0.12},
            token=aged,
            now=NOW,
        )
        == WHY_AGED_OFF
    )
    assert resolve_fomo_no_hunt_why(WHY_HISTORICAL, item=item, token=hist, now=NOW) == WHY_SCORE_FLOOR
    assert resolve_fomo_no_hunt_why(WHY_GATE, item=item, token=hist, now=NOW) == WHY_GATE
    assert WHY_HISTORICAL not in (
        why_not_hunt(item, token=hist, now=NOW),
        why_not_hunt({**item, "gate_veto": "copycat spam"}, token=hist, now=NOW),
    )


def test_freeze_snap_keeps_book_vol_fields():
    snap = freeze_fomo_no_hunt_snap(
        {
            "on_desk": True,
            "on_hunt": False,
            "status": "caught",
            "entry_p": 0.12,
            "vol_h1": 39_500,
            "volume_m5": 8_200,
            "last_liq": 16_000,
            "last_mcap": 80_000,
        },
        why=WHY_NEVER_CARDED,
        now=NOW,
    )
    assert snap["vol_h1"] == 39_500.0
    assert snap["volume_m5"] == 8_200.0
    assert snap["liq"] == 16_000
    assert snap["entry_p"] == 0.12
    assert "github_auth_n" not in snap
    assert "real_project" not in snap
    assert "gmgn_cto" not in snap


def test_stamp_is_write_once():
    first = stamp_fomo_trend_no_hunt(
        {}, why=WHY_AGED_OFF, snap={"first_seen": "2026-10-01T00:00:00+00:00", "mcap": 80_000}
    )
    assert first is not None
    assert first[FOMO_TREND_NO_HUNT_KEY] is True
    assert first[FOMO_TREND_NO_HUNT_WHY_KEY] == WHY_AGED_OFF
    assert first[FOMO_TREND_NO_HUNT_SNAP_KEY]["mcap"] == 80_000
    assert has_fomo_trend_no_hunt(first)
    second = stamp_fomo_trend_no_hunt(first, why=WHY_GATE, snap={"mcap": 9})
    assert second is None
    assert first[FOMO_TREND_NO_HUNT_WHY_KEY] == WHY_AGED_OFF
    assert stamp_fomo_trend_no_hunt({}, why="not-a-reason") is None


def _tok(
    session,
    mint,
    *,
    symbol="NOHUNT",
    age_h=2.0,
    p=0.12,
    t0=40_000,
    last=240_000,
    liq=20_000,
    holders=80,
    vol_h1=25_000,
    hunt=False,
    veto="",
    historical=False,
    features=None,
    now=NOW,
):
    import json as _json

    seen = now - timedelta(hours=age_h)
    tok = Token(
        mint=mint,
        symbol=symbol,
        chain="sol",
        first_seen_at=seen,
        migrated_at=seen,
        created_at_chain=seen,
        source="poll",
        is_historical=historical,
    )
    feat = features if isinstance(features, dict) else {}
    tok.research = Research(
        features_json=_json.dumps(feat) if feat else "{}",
        p_good=p,
        holder_count=holders,
        risk_flags_json="[]",
        scorer=SCORER_FIRST_SIGHT,
        top10_pct=24.0,
    )
    tok.outcome = Outcome(
        t0_mcap=t0,
        last_mcap=last,
        last_liq=liq,
        max_mcap=last,
        multiple=(last / t0) if t0 else 0.0,
    )
    session.add(tok)
    session.flush()
    session.add(
        Decision(
            token_id=tok.id,
            mint=tok.mint,
            kind="entry",
            chain="sol",
            at=seen,
            source="live",
            entry_p=p,
            entry_mcap=t0,
            liq=liq,
            vol_h1=vol_h1,
            holders=holders,
            features_json=_json.dumps(feat) if feat else "{}",
            scorer=SCORER_FIRST_SIGHT,
            image_rev="test",
        )
    )
    if veto:
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind="gate",
                chain="sol",
                at=seen,
                source="live",
                entry_p=p,
                entry_mcap=t0,
                veto=veto,
                features_json="{}",
                image_rev="test",
            )
        )
    if hunt:
        session.add(
            HuntCard(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                first_seen_at=seen,
                launched_at=seen,
                entry_p=p,
                scorer=SCORER_FIRST_SIGHT,
                conviction_p=0.4,
                t0_mcap=t0,
                last_mcap=last,
                multiple=last / t0,
                last_liq=liq,
                holders=80,
                updated_at=now,
            )
        )
    session.flush()
    return tok


def test_capture_desk_no_hunt_and_door_miss_not_on_hunt():
    init_db()
    with session_scope() as session:
        aged = _tok(session, AGED_MINT, symbol="AGED", age_h=22.0, t0=15_000, last=90_000)
        desk = _tok(session, DESK_MINT, symbol="DESK", age_h=2.0, last=80_000)
        on_hunt = _tok(session, HUNT_MINT, symbol="HUNT", age_h=2.0, hunt=True, last=90_000)
        leftover_off = {
            "mint": "LeftoverOff11111111111111111111111111111",
            "chain": "sol",
            "symbol": "OLD",
            "status": "leftover",
            "on_desk": False,
            "on_hunt": False,
        }
        items = [
            {
                "mint": aged.mint,
                "chain": "sol",
                "symbol": "AGED",
                "status": "caught",
                "on_desk": True,
                "on_hunt": False,
                "entry_p": 0.12,
                "mcap_usd": 90_000,
                "last_mcap": 90_000,
                "first_seen_at": (NOW - timedelta(hours=22)).isoformat(),
                "paper": {"gated90": "open"},
            },
            {
                "mint": desk.mint,
                "chain": "sol",
                "symbol": "DESK",
                "status": "caught",
                "on_desk": True,
                "on_hunt": False,
                "entry_p": 0.12,
                "mcap_usd": 80_000,
                "last_mcap": 80_000,
                "paper": {},
            },
            {
                "mint": on_hunt.mint,
                "chain": "sol",
                "symbol": "HUNT",
                "status": "caught",
                "on_desk": True,
                "on_hunt": True,
                "entry_p": 0.12,
            },
            leftover_off,
            {
                "mint": MISS_MINT,
                "chain": "sol",
                "symbol": "MISS",
                "status": "miss",
                "on_desk": False,
                "on_hunt": False,
                "mcap_usd": 180_000,
            },
        ]
        assert why_not_hunt(items[0], token=aged, now=NOW) == WHY_AGED_OFF
        out = capture_fomo_trend_no_hunt(session, items, now=NOW)
        assert out["open"] is False
        assert out["side_key"] == FOMO_TREND_NO_HUNT_KEY
        assert out["n"] == 3
        assert out["n_desk_no_hunt"] == 2
        assert out["n_door_miss"] == 1
        assert out["n_ran"] >= 1  # aged 40k → 240k = 6×
        assert out["by_why"].get(WHY_AGED_OFF) == 1
        assert out["by_why"].get(WHY_NEVER_CARDED) == 1
        assert out["by_why"].get(WHY_DOOR_MISS) == 1
        mints = {r["mint"] for r in out["items"]}
        assert AGED_MINT in mints and DESK_MINT in mints and MISS_MINT in mints
        assert HUNT_MINT not in mints
        entry = (
            session.query(Decision)
            .filter(Decision.token_id == aged.id, Decision.kind == "entry")
            .one()
        )
        feat = __import__("json").loads(entry.features_json)
        assert feat[FOMO_TREND_NO_HUNT_KEY] is True
        assert feat[FOMO_TREND_NO_HUNT_WHY_KEY] == WHY_AGED_OFF
        assert feat[FOMO_TREND_NO_HUNT_SNAP_KEY]["mcap"] == 90_000
        assert feat[FOMO_TREND_NO_HUNT_SNAP_KEY]["vol_h1"] == 25_000.0
        assert feat[FOMO_TREND_NO_HUNT_SNAP_KEY]["holders"] == 80
        assert feat[FOMO_TREND_NO_HUNT_SNAP_KEY]["liq"] == 20_000
        assert feat[FOMO_TREND_NO_HUNT_SNAP_KEY].get("historical") is False
        assert "github_auth_n" not in feat[FOMO_TREND_NO_HUNT_SNAP_KEY]
        # write-once
        again = capture_fomo_trend_no_hunt(session, items, now=NOW + timedelta(hours=1))
        assert again["n_new"] == 0
        feat2 = __import__("json").loads(
            session.query(Decision)
            .filter(Decision.token_id == aged.id, Decision.kind == "entry")
            .one()
            .features_json
        )
        assert feat2[FOMO_TREND_NO_HUNT_WHY_KEY] == WHY_AGED_OFF
        state = session.query(ScanState).filter(ScanState.key == "fomo_trend_no_hunt").one()
        assert FOMO_TREND_NO_HUNT_KEY in state.value


def test_later_run_cross_links_paper_miss_loop():
    init_db()
    with session_scope() as session:
        aged = _tok(session, AGED_MINT, symbol="AGED", age_h=22.0, t0=15_000, last=90_000)
        items = [
            {
                "mint": aged.mint,
                "chain": "sol",
                "symbol": "AGED",
                "status": "caught",
                "on_desk": True,
                "on_hunt": False,
                "entry_p": 0.12,
                "mcap_usd": 90_000,
                "last_mcap": 90_000,
                "paper": {"gated90": "closed"},
            }
        ]
        capture_fomo_trend_no_hunt(session, items, now=NOW)
        learn = fomo_trend_no_hunt_learn(session, "sol", days=7, now=NOW)
        assert learn["open"] is False
        assert learn["n_joined"] >= 1
        assert learn["n_runners"] >= 1
        assert learn["n_total"] >= 1
        assert learn["autopsy_limit"] >= 32
        assert isinstance(learn.get("separators"), list)
        assert any(a["mint"] == AGED_MINT and a["hit5x"] for a in learn["autopsies"])
        pm = paper_miss_learn(session, "sol", days=7, now=NOW)
        assert "fomo_trend_no_hunt" in pm
        assert pm["fomo_trend_no_hunt"]["n_runners"] >= 1
        assert pm["would_have"]["open"] is False
        assert any(a.get("mint") == AGED_MINT for a in pm["autopsies"])
        cohort = miss_cohort(session, "sol", days=7, now=NOW)
        assert cohort["paper_misses"]["fomo_trend_no_hunt"]["n_runners"] >= 1
        assert "fomo_trend_no_hunt" in (cohort.get("note") or "")


def test_coverage_exposes_no_hunt_learn(monkeypatch):
    import asyncio

    from launchfinder.db import SessionLocal
    from launchfinder.ingest.fomo_poll import save_trending_snapshot
    from launchfinder.research.fomo_coverage import fomo_trending_coverage

    init_db()
    db = SessionLocal()
    try:
        tok = _tok(db, DESK_MINT, symbol="DESK", age_h=3.0, last=90_000, now=NOW)
        save_trending_snapshot(
            db,
            [
                {"mint": DESK_MINT, "chain": "sol", "symbol": "DESK", "mcap_usd": 90_000, "rank": 1},
                {"mint": MISS_MINT, "chain": "sol", "symbol": "MISS", "mcap_usd": 160_000, "rank": 2},
            ],
            NOW,
        )
        db.commit()

        async def _markets(mints, chain="sol"):
            out = {}
            for mint in mints:
                if mint == MISS_MINT:
                    out[mint] = {
                        "mint": mint,
                        "created_at": NOW - timedelta(hours=2),
                        "liquidity_usd": 20_000,
                    }
            return out

        monkeypatch.setattr("launchfinder.research.fomo_coverage.token_markets", _markets)

        async def _resolve(snap):
            rows = list((snap or {}).get("items") or [])
            return rows, {"source": "snapshot", "board_stale": False}, None

        monkeypatch.setattr(
            "launchfinder.research.fomo_coverage._resolve_trending_board", _resolve
        )

        async def _empty_sec():
            return [], {}

        monkeypatch.setattr(
            "launchfinder.research.fomo_coverage._fetch_graduated_secondary", _empty_sec
        )
        monkeypatch.setattr(
            "launchfinder.research.fomo_coverage._fetch_gmgn_trending_secondary", _empty_sec
        )
        monkeypatch.setattr(
            "launchfinder.research.fomo_coverage._fetch_dexscreener_trending_secondary",
            _empty_sec,
        )
        card = asyncio.run(fomo_trending_coverage(db))
        assert "fomo_trend_no_hunt" in card
        learn = card["fomo_trend_no_hunt"]
        assert learn["open"] is False
        assert learn["n"] >= 1
        by_mint = {row["mint"]: row for row in card["items"]}
        assert DESK_MINT in by_mint
        assert by_mint[DESK_MINT]["on_desk"] is True
        assert by_mint[DESK_MINT]["on_hunt"] is False
        assert by_mint[DESK_MINT].get("why_not_hunt")
        assert card["sanity"].get("no_hunt_n") == learn["n"]
        _ = tok
    finally:
        db.close()


def test_historical_token_stamps_actionable_why_and_flag():
    init_db()
    with session_scope() as session:
        hist = _tok(
            session,
            "FomoHistScore1111111111111111111111111111",
            symbol="HSCORE",
            age_h=3.0,
            p=0.01,
            last=42_000,
            historical=True,
            vol_h1=12_000,
            holders=40,
            features={"holder_n": 0.4, "volume_n": 0.7, "organic_book": 1.0, "liquidity_n": 0.5},
        )
        items = [
            {
                "mint": hist.mint,
                "chain": "sol",
                "symbol": "HSCORE",
                "status": "caught",
                "on_desk": True,
                "on_hunt": False,
                "entry_p": 0.01,
                "vol_h1": 12_000,
                "last_mcap": 42_000,
            }
        ]
        assert why_not_hunt(items[0], token=hist, now=NOW) == WHY_SCORE_FLOOR
        out = capture_fomo_trend_no_hunt(session, items, now=NOW)
        assert out["open"] is False
        assert out["by_why"].get(WHY_SCORE_FLOOR) == 1
        assert WHY_HISTORICAL not in (out.get("by_why") or {})
        entry = (
            session.query(Decision)
            .filter(Decision.token_id == hist.id, Decision.kind == "entry")
            .one()
        )
        feat = __import__("json").loads(entry.features_json)
        assert feat[FOMO_TREND_NO_HUNT_WHY_KEY] == WHY_SCORE_FLOOR
        snap = feat[FOMO_TREND_NO_HUNT_SNAP_KEY]
        assert snap["historical"] is True
        assert snap["vol_h1"] == 12_000.0
        assert snap["holders"] == 40
        assert snap.get("volume_n") == 0.7
        assert snap.get("organic_book") == 1.0
        assert "github_auth_n" not in snap


def test_learn_repairs_historical_why_and_emits_separators():
    init_db()
    with session_scope() as session:
        runner = _tok(
            session,
            "FomoSepRun1111111111111111111111111111111",
            symbol="RUN",
            age_h=4.0,
            p=0.11,
            t0=20_000,
            last=140_000,
            liq=22_000,
            holders=120,
            vol_h1=40_000,
            historical=True,
            features={"holder_n": 0.7, "volume_n": 0.9, "organic_book": 1.0, "liquidity_n": 0.8},
        )
        dud = _tok(
            session,
            "FomoSepDud11111111111111111111111111111111",
            symbol="DUD",
            age_h=5.0,
            p=0.02,
            t0=20_000,
            last=22_000,
            liq=6_000,
            holders=20,
            vol_h1=2_000,
            historical=True,
            features={"holder_n": 0.2, "volume_n": 0.2, "organic_book": 0.0, "liquidity_n": 0.2},
        )
        items = [
            {
                "mint": runner.mint,
                "chain": "sol",
                "symbol": "RUN",
                "status": "caught",
                "on_desk": True,
                "on_hunt": False,
                "entry_p": 0.11,
                "vol_h1": 40_000,
                "last_mcap": 140_000,
                "last_liq": 22_000,
            },
            {
                "mint": dud.mint,
                "chain": "sol",
                "symbol": "DUD",
                "status": "caught",
                "on_desk": True,
                "on_hunt": False,
                "entry_p": 0.02,
                "vol_h1": 2_000,
                "last_mcap": 22_000,
                "last_liq": 6_000,
            },
        ]
        # Seed the v205 collapse: write-once cards already say historical.
        import json

        from launchfinder.scoring.fomo_trend_no_hunt import _save_fomo_no_hunt_state

        cards = {
            f"sol:{runner.mint}": {
                "mint": runner.mint,
                "chain": "sol",
                "symbol": "RUN",
                "why": WHY_HISTORICAL,
                "at": NOW.isoformat(),
                "early": {
                    "why": WHY_HISTORICAL,
                    "holders": 120,
                    "liq": 22_000,
                    "entry_p": 0.11,
                    "age_hours": 4.0,
                    "vol_h1": 40_000,
                    "t0_mcap": 20_000,
                    "mcap": 140_000,
                    "on_desk": True,
                    "status": "caught",
                },
                "later_multiple": 7.0,
                "ran": True,
            },
            f"sol:{dud.mint}": {
                "mint": dud.mint,
                "chain": "sol",
                "symbol": "DUD",
                "why": WHY_HISTORICAL,
                "at": NOW.isoformat(),
                "early": {
                    "why": WHY_HISTORICAL,
                    "holders": 20,
                    "liq": 6_000,
                    "entry_p": 0.02,
                    "age_hours": 5.0,
                    "vol_h1": 2_000,
                    "t0_mcap": 20_000,
                    "mcap": 22_000,
                    "on_desk": True,
                    "status": "caught",
                },
                "later_multiple": 1.1,
                "ran": False,
            },
        }
        _save_fomo_no_hunt_state(
            session,
            {"cards": cards, "side_key": FOMO_TREND_NO_HUNT_KEY, "paper_only": True},
            now=NOW,
        )
        _ = items
        learn = fomo_trend_no_hunt_learn(session, "sol", days=7, now=NOW)
        assert learn["open"] is False
        assert learn["n_joined"] == 2
        assert learn["n_runners"] == 1
        assert learn["n_duds"] == 1
        assert learn["n_total"] == 2
        assert WHY_HISTORICAL not in (learn.get("by_why") or {})
        assert (learn.get("by_why") or {}).get(WHY_SCORE_FLOOR) == 1
        seps = {row["feature"]: row for row in learn["separators"]}
        assert "holders" in seps and seps["holders"]["delta"] > 0
        assert "liq" in seps and seps["liq"]["delta"] > 0
        assert "vol_h1" in seps and seps["vol_h1"]["delta"] > 0
        assert all(row.get("prefer") == "holders_vol_book" for row in learn["separators"])
        assert learn["autopsy_limit"] >= 32
        assert len(learn["autopsies"]) == 2
        # Repair persisted so next Learn read stays granular.
        raw = session.query(ScanState).filter(ScanState.key == "fomo_trend_no_hunt").one().value
        saved = json.loads(raw)
        whys = {c["why"] for c in saved["cards"].values()}
        assert WHY_HISTORICAL not in whys


def test_autopsy_limit_covers_thirty_two():
    init_db()
    with session_scope() as session:
        import json

        from launchfinder.scoring.fomo_trend_no_hunt import _save_fomo_no_hunt_state

        cards = {}
        for i in range(36):
            mint = f"FomoAuto{i:02d}" + "1" * (32)
            mint = mint[:44]
            cards[f"sol:{mint}"] = {
                "mint": mint,
                "chain": "sol",
                "symbol": f"A{i}",
                "why": WHY_NEVER_CARDED,
                "at": NOW.isoformat(),
                "early": {"why": WHY_NEVER_CARDED, "holders": 10 + i, "liq": 1000, "entry_p": 0.1},
            }
        _save_fomo_no_hunt_state(
            session,
            {"cards": cards, "side_key": FOMO_TREND_NO_HUNT_KEY, "paper_only": True},
            now=NOW,
        )
        learn = fomo_trend_no_hunt_learn(session, "sol", days=7, now=NOW)
        assert learn["n_total"] == 36
        assert learn["n_joined"] == 36
        assert learn["autopsy_limit"] >= 32
        assert len(learn["autopsies"]) == 32
        _ = json
