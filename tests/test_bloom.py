import asyncio
from datetime import timedelta

from launchfinder.alerts import format_bloom_alert
from launchfinder.db import init_db, session_scope
from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
from launchfinder.scoring.bloom import (
    bloom_bundle_farm,
    bloom_key,
    bloom_scam_book,
    bloom_tape_alive,
    bloom_whale_book,
    consider_bloom,
    leftover_fdv,
    list_blooms,
    promise_score,
    should_alert,
)
from launchfinder.scoring.features import FEATURE_NAMES


def test_feature_names_untouched():
    assert len(FEATURE_NAMES) == 66


def test_leftover_fdv_never_blooms():
    p, reasons = promise_score(
        chain="sol",
        entry_p=0.2,
        multiple=120.0,
        last_mcap=8_000_000,
        t0_mcap=69_000,
        max_mcap=8_000_000,
        last_liq=40_000,
        holders=200,
        top10_pct=30,
        vol_h1=80_000,
        runner_p=0.9,
        second_leg=True,
        label=None,
        flags=[],
    )
    assert p == 0.0
    assert leftover_fdv("sol", 69_000, 8_000_000) is True
    assert any("FDV" in row or "80" in row for row in reasons)


def test_copycat_spam_never_blooms():
    p, reasons = promise_score(
        chain="sol",
        entry_p=0.95,
        multiple=1.58,
        last_mcap=2_203_361,
        t0_mcap=1_391_639,
        max_mcap=2_203_361,
        last_liq=123_625,
        holders=1665,
        top10_pct=17,
        vol_h1=69_106,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=[
            "Same ticker launched repeatedly in 24h (copycat spam)",
            "Late Dex catch-up on a real book (start-high t0)",
            "GMGN rat-trader volume is high",
        ],
    )
    assert p == 0.0
    assert any("Hard-stop" in row for row in reasons)


def test_uswr_bundle_farm_never_blooms():
    # Live USWR: $5M start-high t0, 691 wallets, 96% top10, Bloom 58.
    # Holder count is the lie — top10 is the book.
    assert bloom_scam_book(["Late Dex catch-up on a real book (start-high t0)"]) is True
    assert bloom_bundle_farm(95.89, 691) is True
    assert bloom_bundle_farm(17.0, 630) is False
    p, reasons = promise_score(
        chain="sol",
        entry_p=0.62,
        multiple=1.48,
        last_mcap=7_462_068,
        t0_mcap=5_036_728,
        max_mcap=7_462_068,
        last_liq=229_648,
        holders=691,
        top10_pct=95.89,
        vol_h1=4_933,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=["Supply looks concentrated in top wallets"],
    )
    assert p == 0.0
    assert any("bundle" in row.lower() for row in reasons)
    nina, _ = promise_score(
        chain="sol",
        entry_p=0.92,
        multiple=26.27,
        last_mcap=483_198,
        t0_mcap=43_082,
        max_mcap=1_131_649,
        last_liq=69_092,
        holders=630,
        top10_pct=16.55,
        vol_h1=40_000,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=["Heavy sniper presence at launch (20+)"],
    )
    assert nina >= 0.50
    from launchfinder.serialize import desk_entry_cap, still_doing_well

    assert (
        desk_entry_cap(
            0.62,
            {"risk_flags": ["Late Dex catch-up on a real book (start-high t0)"]},
        )
        == 0.48
    )
    assert (
        still_doing_well(
            {
                "chain": "sol",
                "last_mcap": 7_462_068,
                "max_mcap": 7_462_068,
                "t0_mcap": 5_036_728,
                "last_liq": 229_648,
                "multiple": 1.48,
                "holder_count": 691,
                "top10_pct": 95.89,
                "risk_flags": ["Late Dex catch-up on a real book (start-high t0)"],
            }
        )
        is False
    )


def test_bubble_whale_leftover_never_blooms():
    # Live bubble: July pair ingested today, 1090 wallets, top10 67%,
    # one EOA 64.5%, two-point Dex line. Hunt live printed 71–80.
    # RH MEME fair LP and NINA do not carry the huge-share flag.
    from launchfinder.scoring.hunt import conviction_from_tape

    flags = [
        "X account created very recently",
        "Supply looks concentrated in top wallets",
        "One wallet holds a huge share",
        "Linked X account has almost no posts",
    ]
    assert bloom_whale_book(66.66, flags) is True
    assert bloom_whale_book(16.55, flags) is False
    assert bloom_whale_book(66.66, ["Supply looks concentrated in top wallets"]) is False
    p, reasons = promise_score(
        chain="robinhood",
        entry_p=0.1211,
        multiple=3.24,
        last_mcap=87_984,
        t0_mcap=27_128,
        max_mcap=87_984,
        last_liq=31_358,
        holders=1090,
        top10_pct=66.66,
        vol_h1=11_390,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=flags,
    )
    assert p == 0.0
    assert any("wallet" in row.lower() for row in reasons)
    live = conviction_from_tape(
        chain="robinhood",
        entry_p=0.1211,
        multiple=3.24,
        last_mcap=87_984,
        t0_mcap=27_128,
        max_mcap=87_984,
        last_liq=31_358,
        holders=1090,
        top10_pct=66.66,
        flags=flags,
    )
    assert live < 0.40
    rocket, _ = promise_score(
        chain="robinhood",
        entry_p=0.92,
        multiple=2.82,
        last_mcap=72_971,
        t0_mcap=25_893,
        max_mcap=72_971,
        last_liq=78_578,
        holders=910,
        top10_pct=18.0,
        vol_h1=20_000,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=["Logo reused on other tokens"],
    )
    assert rocket >= 0.50
    nina, _ = promise_score(
        chain="sol",
        entry_p=0.92,
        multiple=26.27,
        last_mcap=192_068,
        t0_mcap=43_082,
        max_mcap=1_131_649,
        last_liq=43_828,
        holders=630,
        top10_pct=16.55,
        vol_h1=40_000,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=["Heavy sniper presence at launch (20+)"],
    )
    assert nina >= 0.50


def test_dead_pool_and_rug_never_bloom():
    dead, _ = promise_score(
        chain="sol",
        entry_p=0.2,
        multiple=2.2,
        last_mcap=140_000,
        t0_mcap=69_000,
        max_mcap=140_000,
        last_liq=200,
        holders=80,
        top10_pct=40,
        vol_h1=20_000,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=[],
    )
    rug, _ = promise_score(
        chain="robinhood",
        entry_p=0.2,
        multiple=2.2,
        last_mcap=90_000,
        t0_mcap=40_000,
        max_mcap=90_000,
        last_liq=20_000,
        holders=80,
        top10_pct=40,
        vol_h1=20_000,
        runner_p=None,
        second_leg=False,
        label=0,
        flags=[],
    )
    assert dead == 0.0
    assert rug == 0.0


def test_weak_entry_live_tape_scores_a_promise():
    p, reasons = promise_score(
        chain="sol",
        entry_p=0.18,
        multiple=2.3,
        last_mcap=160_000,
        t0_mcap=69_000,
        max_mcap=170_000,
        last_liq=28_000,
        holders=120,
        top10_pct=38,
        vol_h1=40_000,
        runner_p=0.62,
        second_leg=True,
        label=None,
        flags=[],
    )
    assert p >= 0.58
    assert any("Entry score was only" in row for row in reasons)
    assert should_alert(0.18, p, False, 0.58) is True
    assert should_alert(0.80, 0.62, False, 0.58) is False
    assert should_alert(0.18, p, True, 0.58) is False
    # High-entry 5×+ (MIZO class) — promise cannot beat entry+0.12 at the 0.95 cap.
    assert should_alert(0.92, 0.95, False, 0.58, multiple=17.2, last_liq=20_000) is True
    assert should_alert(0.92, 0.95, False, 0.58, multiple=2.1, last_liq=20_000) is False
    assert should_alert(0.80, 0.62, False, 0.58, multiple=5.1, last_liq=400) is False


def test_dumping_under_entry_does_not_keep_high_promise():
    p, reasons = promise_score(
        chain="sol",
        entry_p=0.94,
        multiple=0.76,
        last_mcap=240_000,
        t0_mcap=316_000,
        max_mcap=316_000,
        last_liq=46_000,
        holders=1021,
        top10_pct=19.5,
        vol_h1=450_000,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=[
            "Creator was funded by a wallet behind prior rugs",
            "First-hour tape is dumping on real volume",
        ],
    )
    assert p == 0.0
    assert any("under entry" in row.lower() for row in reasons)


def test_rugged_ath_print_is_not_a_bloom():
    """SPC-class: stored 1.67× ATH + second-leg still 0 after last/t0 = 0.06."""
    p, reasons = promise_score(
        chain="sol",
        entry_p=0.89,
        multiple=1.67,
        last_mcap=2_295,
        t0_mcap=40_112,
        max_mcap=67_000,
        last_liq=2_882,
        holders=257,
        top10_pct=26.7,
        vol_h1=20_000,
        runner_p=0.7,
        second_leg=True,
        label=None,
        flags=["Bonding curve filled almost instantly (bundle risk)"],
    )
    assert p == 0.0
    assert bloom_tape_alive("sol", 2_295, 40_112, 2_882) is False
    assert bloom_tape_alive("sol", 370_773, 43_082, 56_483) is True
    assert any("under entry" in row.lower() for row in reasons)


def test_ath_recap_below_2x_is_not_a_live_book():
    """HOUSECAT / APU: still 1.5× vs t0 after dumping 75%+ of ATH."""
    house, house_why = promise_score(
        chain="sol",
        entry_p=0.92,
        multiple=9.16,
        last_mcap=92_848,
        t0_mcap=63_032,
        max_mcap=577_557,
        last_liq=32_274,
        holders=400,
        top10_pct=22.0,
        vol_h1=80_000,
        runner_p=0.7,
        second_leg=True,
        label=None,
        flags=["Dumped off ATH (now 16% of peak)"],
    )
    apu, _ = promise_score(
        chain="sol",
        entry_p=0.92,
        multiple=5.66,
        last_mcap=72_968,
        t0_mcap=50_492,
        max_mcap=285_892,
        last_liq=24_606,
        holders=350,
        top10_pct=24.0,
        vol_h1=40_000,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=["Dumped off ATH (now 26% of peak)"],
    )
    mizo, _ = promise_score(
        chain="sol",
        entry_p=0.92,
        multiple=17.0,
        last_mcap=262_613,
        t0_mcap=46_391,
        max_mcap=772_000,
        last_liq=49_000,
        holders=200,
        top10_pct=30.0,
        vol_h1=40_000,
        runner_p=None,
        second_leg=False,
        label=None,
        flags=["Dumped off ATH (now 34% of peak)"],
    )
    assert house <= 0.32
    assert apu <= 0.32
    assert any("recap" in row.lower() or "dumped off ath" in row.lower() for row in house_why)
    assert mizo >= 0.50


def test_list_blooms_drops_alerted_rug():
    init_db()
    with session_scope() as session:
        session.add(
            ScanState(
                key=bloom_key("SpcRug11111111111111111111111111111111111"),
                value=(
                    '{"mint":"SpcRug11111111111111111111111111111111111",'
                    '"chain":"sol","promise_p":0.59,"entry_p":0.89,'
                    '"multiple":1.67,"last_mcap":2295,"t0_mcap":40112,'
                    '"last_liq":2882,"alerted":true}'
                ),
            )
        )
        session.add(
            ScanState(
                key=bloom_key("NinaLive111111111111111111111111111111111"),
                value=(
                    '{"mint":"NinaLive111111111111111111111111111111111",'
                    '"chain":"sol","promise_p":0.95,"entry_p":0.92,'
                    '"multiple":8.6,"last_mcap":370773,"t0_mcap":43082,'
                    '"last_liq":56483,"alerted":true}'
                ),
            )
        )
        session.flush()
        rows = list_blooms(session, "sol", limit=40)
        mints = {row.get("mint") for row in rows}
        assert "SpcRug11111111111111111111111111111111111" not in mints
        assert "NinaLive111111111111111111111111111111111" in mints


def test_bloom_board_shows_hunt_frozen_entry_not_repaired_p_good():
    # Live TTNB: t0 snap 0.85 (Hunt froze it), repair lifted p_good to 0.92,
    # Bloom showed Entry 92 next to Hunt's 85. Same frozen entry everywhere.
    from launchfinder.app import bloom_board
    from launchfinder.models import HuntCard, Snapshot

    init_db()
    now = utcnow()
    with session_scope() as session:
        ttnb = Token(
            mint="BloomTtnbMint111111111111111111111111111",
            symbol="TTNB",
            source="poll",
            chain="sol",
            first_seen_at=now - timedelta(minutes=30),
            migrated_at=now - timedelta(minutes=30),
        )
        ttnb.research = Research(p_good=0.92, features_json="{}", risk_flags_json="[]")
        session.add(ttnb)
        session.flush()
        session.add(Outcome(token_id=ttnb.id, t0_mcap=94_255.0, last_mcap=225_754.0, max_mcap=297_940.0, last_liq=60_000.0, multiple=3.2, label=None))
        session.add(Snapshot(token_id=ttnb.id, kind="t0", taken_at=now - timedelta(minutes=30), mcap_usd=94_255.0, liquidity_usd=40_000.0, volume_h1=50_000.0, p_good=0.85))
        session.add(Snapshot(token_id=ttnb.id, kind="early", taken_at=now - timedelta(minutes=5), mcap_usd=225_754.0, liquidity_usd=60_000.0, volume_h1=120_000.0, p_good=0.92))
        session.add(
            HuntCard(
                chain="sol",
                mint=ttnb.mint,
                token_id=ttnb.id,
                first_seen_at=ttnb.first_seen_at,
                launched_at=ttnb.migrated_at,
                entry_p=0.85,
                conviction_p=0.95,
                t0_mcap=94_255.0,
                last_mcap=225_754.0,
                multiple=3.2,
                last_liq=60_000.0,
                holders=900,
            )
        )
        session.add(
            ScanState(
                key=bloom_key(ttnb.mint),
                value=(
                    f'{{"mint":"{ttnb.mint}","chain":"sol","promise_p":0.95,"entry_p":0.92,'
                    '"multiple":3.2,"last_mcap":225754,"t0_mcap":94255,"last_liq":60000,"alerted":false}'
                ),
            )
        )

        # No hunt row: the t0 snap is the frozen entry (GIGANINA 0.68 → live 0.48).
        giga = Token(
            mint="BloomGigaMint111111111111111111111111111",
            symbol="GIGANINA",
            source="poll",
            chain="sol",
            first_seen_at=now - timedelta(minutes=30),
            migrated_at=now - timedelta(minutes=30),
        )
        giga.research = Research(p_good=0.48, features_json="{}", risk_flags_json="[]")
        session.add(giga)
        session.flush()
        session.add(Outcome(token_id=giga.id, t0_mcap=137_846.0, last_mcap=158_980.0, max_mcap=170_000.0, last_liq=45_000.0, multiple=1.15, label=None))
        session.add(Snapshot(token_id=giga.id, kind="t0", taken_at=now - timedelta(minutes=30), mcap_usd=137_846.0, liquidity_usd=40_000.0, volume_h1=30_000.0, p_good=0.68))
        session.add(
            ScanState(
                key=bloom_key(giga.mint),
                value=(
                    f'{{"mint":"{giga.mint}","chain":"sol","promise_p":0.70,"entry_p":0.48,'
                    '"multiple":1.15,"last_mcap":158980,"t0_mcap":137846,"last_liq":45000,"alerted":false}'
                ),
            )
        )

    board = asyncio.run(bloom_board(chain="sol", limit=40))
    by_mint = {row["mint"]: row for row in board["items"]}
    assert by_mint["BloomTtnbMint111111111111111111111111111"]["entry_p"] == 0.85
    assert by_mint["BloomTtnbMint111111111111111111111111111"]["score"] == 95.0
    assert by_mint["BloomGigaMint111111111111111111111111111"]["entry_p"] == 0.68
    # Live p_good itself is not rewritten.
    assert by_mint["BloomTtnbMint111111111111111111111111111"]["p_good"] == 0.92


def test_consider_bloom_does_not_rewrite_entry_p(monkeypatch):
    async def _nodive(token, research):
        return {"handle": "", "tier": "none", "delta": 0.0, "reasons": [], "line": "", "github": ""}

    monkeypatch.setattr("launchfinder.scoring.bloom.dive_developer", _nodive)
    init_db()
    now = utcnow()
    with session_scope() as session:
        token = Token(
            mint="BloomMint1111111111111111111111111111111",
            symbol="BLOOM",
            name="Bloom",
            chain="sol",
            first_seen_at=now - timedelta(hours=4),
            migrated_at=now - timedelta(hours=4),
        )
        token.research = Research(
            features_json="{}",
            reasons_json="[]",
            risk_flags_json="[]",
            p_good=0.18,
            heuristic_p=0.20,
            holder_count=140,
            top10_pct=36.0,
            thesis="entry thesis",
        )
        outcome = Outcome(
            token=token,
            t0_mcap=69_000,
            max_mcap=180_000,
            last_mcap=165_000,
            last_liq=32_000,
            multiple=2.39,
        )
        session.add_all([token, outcome])
        session.flush()
        payload = asyncio.run(
            consider_bloom(
                session,
                token,
                outcome,
                {"liquidity_usd": 32_000, "volume_h1": 45_000, "mcap_usd": 165_000},
                now=now,
            )
        )
        assert payload is not None
        assert token.research.p_good == 0.18
        assert token.research.thesis == "entry thesis"
        assert payload["promise_p"] >= 0.58
        assert "entry p(good)=18%" in payload["thesis"]
        assert payload["entry_p"] == 0.18


def test_bloom_alert_copy():
    text = format_bloom_alert(
        symbol="BLOOM",
        name="Bloom Coin",
        mint="So11111111111111111111111111111111111111112",
        promise_p=0.64,
        entry_p=0.18,
        mcap_usd=165000,
        thesis="BLOOM launched on sol with entry p(good)=18%.",
        reasons=["Held a 2× print", "140 holders"],
        flags=[],
        chain="sol",
    )
    assert "bloom 64%" in text
    assert "entry 18%" in text
    assert "why now:" in text
    assert "pump.fun" in text
