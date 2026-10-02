import asyncio
import json
from datetime import timedelta

from launchfinder.app import paper_ledger
from launchfinder.db import SessionLocal, init_db, session_scope
from launchfinder.models import HuntCard, Outcome, Research, Snapshot, Token, utcnow


def test_reclassified_live_positions_stay_in_ledger():
    init_db()
    with session_scope() as session:
        # Backfill seed: must never appear in the ledger.
        seed = Token(mint="PaperSeedMint1111", symbol="SEED", source="backfill", is_historical=True, first_seen_at=utcnow())
        seed.research = Research(p_good=0.9, features_json="{}")
        session.add(seed)
        session.flush()
        session.add(Outcome(token_id=seed.id, t0_mcap=69000.0, max_mcap=300000.0, multiple=4.3, label=1))

        # Entered live at p=0.8, later reclassified historical: stays, closes as win.
        aged = Token(mint="PaperAgedMint1111", symbol="AGED", source="poll", is_historical=True, first_seen_at=utcnow())
        aged.research = Research(p_good=0.8, features_json="{}")
        session.add(aged)
        session.flush()
        session.add(Outcome(token_id=aged.id, t0_mcap=80000.0, max_mcap=200000.0, multiple=2.5, label=1, t24h_mcap=150000.0))
        # liquid snapshot at 2.5x confirms the 2x was sellable
        session.add(Snapshot(token_id=aged.id, kind="live", mcap_usd=200000.0, liquidity_usd=25000.0))

        # Wick: labeled a 2x win but no liquid snapshot ever saw it — exit at 24h.
        wick = Token(mint="PaperWickMint1111", symbol="WICK", source="poll", first_seen_at=utcnow())
        wick.research = Research(p_good=0.75, features_json="{}")
        session.add(wick)
        session.flush()
        session.add(Outcome(token_id=wick.id, t0_mcap=100000.0, max_mcap=250000.0, multiple=2.5, label=1, t24h_mcap=110000.0))
        session.add(Snapshot(token_id=wick.id, kind="live", mcap_usd=250000.0, liquidity_usd=900.0))

    ledger = asyncio.run(paper_ledger(min_p=0.7, target=2.0))
    mints = {t["mint"] for t in ledger["closed"]} | {t["mint"] for t in ledger["open"]}
    assert "PaperAgedMint1111" in mints
    assert "PaperSeedMint1111" not in mints
    closed = [t for t in ledger["closed"] if t["mint"] == "PaperAgedMint1111"]
    assert closed and closed[0]["return_pct"] == 100.0  # sold at the 2x target
    wick = [t for t in ledger["closed"] if t["mint"] == "PaperWickMint1111"]
    assert wick and wick[0]["return_pct"] == 10.0  # unfilled wick exits at 24h (+10%)
    assert ledger["unfilled_2x_wicks"] >= 1


def test_desk_paper_buys_only_ninety_and_banks_half_at_two():
    # Desk Paper tab: buy graduation ≥90, sell half at confirmed 2×,
    # ride the rest to 10× or the 24h print. 88 stays off the book.
    init_db()
    now = utcnow()
    with session_scope() as session:
        miss = Token(
            mint="PaperMiss88Mint1111111111111111111111",
            symbol="MISS88",
            source="poll",
            chain="sol",
            first_seen_at=now,
        )
        miss.research = Research(p_good=0.88, features_json="{}")
        session.add(miss)
        session.flush()
        session.add(Outcome(token_id=miss.id, t0_mcap=80_000.0, max_mcap=240_000.0, multiple=3.0, label=1, t24h_mcap=90_000.0))
        session.add(Snapshot(token_id=miss.id, kind="t0", mcap_usd=80_000.0, liquidity_usd=20_000.0, p_good=0.88))
        session.add(Snapshot(token_id=miss.id, kind="live", mcap_usd=240_000.0, liquidity_usd=25_000.0))

        hit = Token(
            mint="PaperHit92Mint11111111111111111111111",
            symbol="HIT92",
            source="poll",
            chain="sol",
            first_seen_at=now,
        )
        hit.research = Research(p_good=0.92, features_json="{}")
        session.add(hit)
        session.flush()
        session.add(Outcome(token_id=hit.id, t0_mcap=80_000.0, max_mcap=240_000.0, multiple=3.0, label=1, t24h_mcap=96_000.0))
        session.add(Snapshot(token_id=hit.id, kind="t0", mcap_usd=80_000.0, liquidity_usd=20_000.0, p_good=0.92))
        session.add(Snapshot(token_id=hit.id, kind="live", mcap_usd=240_000.0, liquidity_usd=25_000.0))

        held = Token(
            mint="PaperOpen95Mint1111111111111111111111",
            symbol="OPEN95",
            source="poll",
            chain="sol",
            first_seen_at=now,
        )
        held.research = Research(p_good=0.95, features_json="{}")
        session.add(held)
        session.flush()
        session.add(Outcome(token_id=held.id, t0_mcap=70_000.0, max_mcap=98_000.0, multiple=1.4, label=None))
        session.add(Snapshot(token_id=held.id, kind="t0", mcap_usd=70_000.0, liquidity_usd=18_000.0, p_good=0.95))
        session.add(Snapshot(token_id=held.id, kind="now", mcap_usd=98_000.0, liquidity_usd=18_000.0))

    ledger = asyncio.run(paper_ledger(min_p=0.9, target=2.0, ride=10.0, strategy="moonbag"))
    closed_mints = {row["mint"] for row in ledger["closed"]}
    open_mints = {row["mint"] for row in ledger["open"]}
    assert "PaperMiss88Mint1111111111111111111111" not in closed_mints
    assert "PaperMiss88Mint1111111111111111111111" not in open_mints
    assert "PaperHit92Mint11111111111111111111111" in closed_mints
    assert "PaperOpen95Mint1111111111111111111111" in open_mints
    hit = next(row for row in ledger["closed"] if row["symbol"] == "HIT92")
    # Half +100% at 2×, half +20% at the 24h print → +60%.
    assert hit["return_pct"] == 60.0
    assert "sell half at 2.0x" in ledger["strategy"]


def test_gated_paper_fills_now_and_vetoes_clones():
    init_db()
    now = utcnow()
    start = now - timedelta(hours=3)
    with session_scope() as session:
        hit = Token(
            mint="GatedHitMint111111111111111111111111",
            symbol="OK92",
            source="poll",
            chain="sol",
            first_seen_at=start,
            migrated_at=start,
            website="https://ok92.example",
        )
        hit.research = Research(
            p_good=0.92,
            features_json="{}",
            risk_flags_json='["Heavy sniper presence at launch (20+)"]',
            twitter_handle="okbuilder",
            twitter_followers=1200,
            twitter_age_days=400,
            twitter_verified=False,
        )
        session.add(hit)
        session.flush()
        session.add(
            Outcome(
                token_id=hit.id,
                t0_mcap=80_000.0,
                t15m_mcap=92_000.0,
                max_mcap=240_000.0,
                last_mcap=96_000.0,
                last_liq=20_000.0,
                multiple=3.0,
                label=1,
                t24h_mcap=96_000.0,
            )
        )
        session.add(Snapshot(token_id=hit.id, kind="t0", taken_at=start, mcap_usd=80_000.0, liquidity_usd=20_000.0, volume_h1=12_000.0, p_good=0.92))
        session.add(
            Snapshot(
                token_id=hit.id,
                kind="t15m",
                taken_at=start + timedelta(minutes=15),
                mcap_usd=92_000.0,
                liquidity_usd=20_000.0,
                volume_h1=14_000.0,
            )
        )
        session.add(Snapshot(token_id=hit.id, kind="live", mcap_usd=240_000.0, liquidity_usd=25_000.0, volume_h1=30_000.0))

        clone = Token(
            mint="GatedCloneMint1111111111111111111111",
            symbol="GOOG90",
            source="poll",
            chain="sol",
            first_seen_at=start,
            migrated_at=start,
            website="https://gemini.google.com/app",
        )
        clone.research = Research(
            p_good=0.98,
            features_json="{}",
            risk_flags_json='["Same ticker launched repeatedly in 24h (copycat spam)"]',
            twitter_handle="GeminiApp",
            twitter_followers=573_300,
            twitter_age_days=808,
            twitter_verified=True,
        )
        session.add(clone)
        session.flush()
        session.add(Outcome(token_id=clone.id, t0_mcap=80_000.0, t15m_mcap=90_000.0, max_mcap=90_000.0, last_liq=20_000.0, multiple=1.1, label=None))
        session.add(Snapshot(token_id=clone.id, kind="t0", taken_at=start, mcap_usd=80_000.0, liquidity_usd=20_000.0, p_good=0.98))
        session.add(
            Snapshot(
                token_id=clone.id,
                kind="t15m",
                taken_at=start + timedelta(minutes=15),
                mcap_usd=90_000.0,
                liquidity_usd=20_000.0,
            )
        )

        dump = Token(
            mint="GatedDumpMint11111111111111111111111",
            symbol="DUMP90",
            source="poll",
            chain="sol",
            first_seen_at=start,
            migrated_at=start,
        )
        dump.research = Research(
            p_good=0.92,
            features_json="{}",
            risk_flags_json='["First-hour tape is dumping on real volume"]',
        )
        session.add(dump)
        session.flush()
        session.add(Outcome(token_id=dump.id, t0_mcap=80_000.0, t15m_mcap=60_000.0, max_mcap=80_000.0, last_liq=18_000.0, multiple=0.75, label=0, t24h_mcap=20_000.0))
        session.add(Snapshot(token_id=dump.id, kind="t0", taken_at=start, mcap_usd=80_000.0, liquidity_usd=18_000.0, p_good=0.92))
        session.add(
            Snapshot(
                token_id=dump.id,
                kind="t15m",
                taken_at=start + timedelta(minutes=15),
                mcap_usd=60_000.0,
                liquidity_usd=18_000.0,
            )
        )

        wait = Token(
            mint="GatedWaitMint11111111111111111111111",
            symbol="WAIT90",
            source="poll",
            chain="sol",
            first_seen_at=now - timedelta(minutes=6),
            migrated_at=now - timedelta(minutes=6),
        )
        wait.research = Research(p_good=0.94, features_json="{}", risk_flags_json="[]")
        session.add(wait)
        session.flush()
        session.add(Outcome(token_id=wait.id, t0_mcap=70_000.0, max_mcap=72_000.0, last_liq=16_000.0, multiple=1.03, label=None))
        session.add(
            Snapshot(
                token_id=wait.id,
                kind="t0",
                taken_at=now - timedelta(minutes=6),
                mcap_usd=70_000.0,
                liquidity_usd=16_000.0,
                p_good=0.94,
            )
        )

    gated = asyncio.run(paper_ledger(min_p=0.9, target=2.0, ride=10.0, strategy="moonbag", gated=True, ledger=False))
    closed_mints = {row["mint"] for row in gated["closed"]}
    open_mints = {row["mint"] for row in gated["open"]}
    wait_mints = {row["mint"] for row in gated["waiting"]}
    assert gated["gated"] is True
    assert "t15m" not in gated["strategy"]
    assert "now if liquid" in gated["strategy"]
    assert "GatedHitMint111111111111111111111111" in closed_mints
    assert "GatedCloneMint1111111111111111111111" not in closed_mints
    assert "GatedCloneMint1111111111111111111111" not in open_mints
    assert "GatedDumpMint11111111111111111111111" not in closed_mints
    assert "GatedWaitMint11111111111111111111111" not in wait_mints
    assert "GatedWaitMint11111111111111111111111" in open_mints
    hit = next(row for row in gated["closed"] if row["symbol"] == "OK92")
    assert hit["return_pct"] == 60.0

    classic = asyncio.run(paper_ledger(min_p=0.9, target=2.0, ride=10.0, strategy="moonbag", gated=False))
    classic_mints = {row["mint"] for row in classic["closed"]} | {row["mint"] for row in classic["open"]}
    assert "GatedCloneMint1111111111111111111111" in classic_mints


def test_gated_paper_this_window_next_ninety():
    # Live book after dropping t15m: 61 open, most 2–14 days old at the
    # $69k floor. Desk Paper is the next 90+ that land (Hunt window).
    # Root: Hunt froze 0.92, live faded to 50 — still a fill.
    init_db()
    now = utcnow()
    with session_scope() as session:
        leftover = Token(
            mint="GatedOldMint1111111111111111111111111",
            symbol="SANTA90",
            source="poll",
            chain="sol",
            first_seen_at=now - timedelta(hours=40),
            migrated_at=now - timedelta(hours=40),
        )
        leftover.research = Research(p_good=0.96, features_json="{}", risk_flags_json="[]")
        session.add(leftover)
        session.flush()
        session.add(Outcome(token_id=leftover.id, t0_mcap=69_000.0, last_mcap=76_000.0, last_liq=22_000.0, multiple=1.1, label=None))
        session.add(Snapshot(token_id=leftover.id, kind="t0", taken_at=now - timedelta(hours=40), mcap_usd=69_000.0, liquidity_usd=22_000.0, volume_h1=8_000.0, p_good=0.96))

        root = Token(
            mint="GatedRootMint111111111111111111111111",
            symbol="ROOT92",
            source="poll",
            chain="sol",
            first_seen_at=now - timedelta(hours=4),
            migrated_at=now - timedelta(hours=4),
        )
        root.research = Research(p_good=0.50, features_json="{}", risk_flags_json='["Heavy sniper presence at launch (20+)"]')
        session.add(root)
        session.flush()
        session.add(Outcome(token_id=root.id, t0_mcap=44_136.0, last_mcap=57_174.0, last_liq=18_477.0, multiple=1.74, label=None))
        session.add(Snapshot(token_id=root.id, kind="t0", taken_at=now - timedelta(hours=4), mcap_usd=44_136.0, liquidity_usd=18_477.0, volume_h1=20_000.0, p_good=0.92))
        session.add(
            HuntCard(
                chain="sol",
                mint=root.mint,
                token_id=root.id,
                first_seen_at=root.first_seen_at,
                launched_at=root.migrated_at,
                entry_p=0.92,
                conviction_p=0.50,
                t0_mcap=44_136.0,
                last_mcap=57_174.0,
                multiple=1.74,
                last_liq=18_477.0,
                holders=200,
            )
        )

        salary = Token(
            mint="GatedSalaryMint1111111111111111111111",
            symbol="SALARY92",
            source="poll",
            chain="sol",
            first_seen_at=now - timedelta(hours=3),
            migrated_at=now - timedelta(hours=3),
        )
        salary.research = Research(
            p_good=0.35,
            features_json="{}",
            risk_flags_json='["Heavy sniper presence at launch (20+)"]',
        )
        session.add(salary)
        session.flush()
        session.add(Outcome(token_id=salary.id, t0_mcap=39_663.0, last_mcap=40_268.0, last_liq=15_332.0, multiple=1.04, label=None))
        session.add(
            Snapshot(
                token_id=salary.id,
                kind="t0",
                taken_at=now - timedelta(hours=3),
                mcap_usd=39_663.0,
                liquidity_usd=0.0,
                volume_h1=0.0,
                p_good=0.92,
            )
        )
        session.add(
            Snapshot(
                token_id=salary.id,
                kind="early",
                taken_at=now - timedelta(hours=3) + timedelta(minutes=5),
                mcap_usd=35_746.0,
                liquidity_usd=14_340.0,
                volume_h1=65_315.0,
                p_good=0.95,
            )
        )
        session.add(
            HuntCard(
                chain="sol",
                mint=salary.mint,
                token_id=salary.id,
                first_seen_at=salary.first_seen_at,
                launched_at=salary.migrated_at,
                entry_p=0.92,
                conviction_p=0.50,
                t0_mcap=39_663.0,
                last_mcap=40_268.0,
                multiple=1.04,
                last_liq=15_332.0,
                holders=180,
            )
        )

        faucet = Token(
            mint="GatedFaucetMint1111111111111111111111",
            symbol="FAUCET97",
            source="poll",
            chain="sol",
            first_seen_at=now - timedelta(hours=2),
            migrated_at=now - timedelta(hours=2),
        )
        faucet.research = Research(
            p_good=0.97,
            features_json="{}",
            risk_flags_json='["Late Dex catch-up on a real book (start-high t0)"]',
        )
        session.add(faucet)
        session.flush()
        session.add(Outcome(token_id=faucet.id, t0_mcap=1_966_799.0, last_mcap=1_858.0, last_liq=1_883.0, multiple=1.08, label=None))
        session.add(Snapshot(token_id=faucet.id, kind="t0", taken_at=now - timedelta(hours=2), mcap_usd=1_966_799.0, liquidity_usd=80_000.0, volume_h1=40_000.0, p_good=0.97))

    gated = asyncio.run(paper_ledger(min_p=0.9, target=2.0, ride=10.0, strategy="moonbag", gated=True, ledger=False))
    open_mints = {row["mint"] for row in gated["open"]}
    closed_mints = {row["mint"] for row in gated["closed"]}
    book = open_mints | closed_mints
    assert "this-window" in gated["strategy"]
    assert "GatedOldMint1111111111111111111111111" not in book
    assert "GatedRootMint111111111111111111111111" in open_mints
    assert "GatedSalaryMint1111111111111111111111" in open_mints
    salary_row = next(row for row in gated["open"] if row["mint"] == "GatedSalaryMint1111111111111111111111")
    assert salary_row["entry_mcap"] == 35746
    assert "GatedFaucetMint1111111111111111111111" not in book

    classic = asyncio.run(paper_ledger(min_p=0.9, target=2.0, ride=10.0, strategy="moonbag", gated=False))
    classic_book = {row["mint"] for row in classic["open"]} | {row["mint"] for row in classic["closed"]}
    assert "GatedOldMint1111111111111111111111111" in classic_book


def test_watch_preview_roundtrip():
    from launchfinder.app import graduating_soon
    from launchfinder.ingest.pump_poll import _store_watch_preview_rows

    _store_watch_preview_rows(
        [
            {"address": "NearMint111", "symbol": "NR", "name": "Near", "usd_market_cap": 55000, "progress": 0.93},
            {"address": "NearMint222", "symbol": "MC", "name": "McapOnly", "market_cap": 43000.5},
        ]
    )
    data = asyncio.run(graduating_soon())
    rows = data["graduating_soon"]
    assert rows and rows[0]["symbol"] == "NR"
    assert rows[0]["mcap_usd"] == 55000.0
    assert rows[1]["mcap_usd"] == 43000.5  # near_completion field name
    assert "gmgn.ai" in rows[0]["gmgn"]


def test_watch_preview_drops_bonded_dust_and_ticker_twins():
    from launchfinder.app import graduating_soon
    from launchfinder.ingest.pump_poll import _store_watch_preview_rows

    _store_watch_preview_rows(
        [
            {"address": "BondedMint111", "symbol": "POKEMON", "name": "Bonded", "usd_market_cap": 1820, "progress": 1.0},
            {"address": "DustMint11111", "symbol": "PDOWN", "name": "Dust", "usd_market_cap": 22.95, "progress": 0.95},
            {"address": "RstLeftover11", "symbol": "RST", "name": "RST leftover", "usd_market_cap": 2_100_000, "progress": 1.0},
            {"address": "RstNearMint11", "symbol": "RST", "name": "RST near", "usd_market_cap": 18_000, "progress": 0.91},
            {"address": "RstSkinny1111", "symbol": "RST", "name": "RST skinny", "usd_market_cap": 1_700, "progress": 0.89},
            {"address": "LoongMint1111", "symbol": "LOONG", "name": "Loong", "usd_market_cap": 32_000, "progress": 0.956},
            {"address": "NearMint111", "symbol": "NR", "name": "Near", "usd_market_cap": 55_000, "progress": 0.93},
        ]
    )
    data = asyncio.run(graduating_soon())
    rows = data["graduating_soon"]
    symbols = [r["symbol"] for r in rows]
    assert "POKEMON" not in symbols
    assert "PDOWN" not in symbols
    assert symbols.count("RST") == 1
    rst = next(r for r in rows if r["symbol"] == "RST")
    assert rst["mcap_usd"] == 18_000.0
    assert rst["progress"] == 0.91
    assert rows[0]["symbol"] == "LOONG"
    assert "NR" in symbols


def test_sol_graduating_keeps_last_real_strip_when_trenches_empty():
    # Live 14:52 BANNED boot: Sol graduating went 0 after ZERO/BFONE.
    # Keep the last real books; empty / all-bonded writes must not wipe.
    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll

    init_db()
    pump_poll._watch_preview = []
    pump_poll._store_watch_preview_rows(
        [
            {"address": "ZeroKeepMint11", "symbol": "ZEROKEEP", "name": "Zero", "usd_market_cap": 29876.42, "progress": 0.941},
            {"address": "BfoneKeepMint1", "symbol": "BFONEKEEP", "name": "Bfone", "usd_market_cap": 29175.17, "progress": 0.937},
        ]
    )
    pump_poll._store_watch_preview_rows([])
    pump_poll._store_watch_preview_rows(
        [
            {"address": "BondedWipeMint", "symbol": "POKEMON", "name": "Bonded", "usd_market_cap": 1820, "progress": 1.0},
        ]
    )
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "ZEROKEEP" in symbols
    assert "BFONEKEEP" in symbols
    assert "POKEMON" not in symbols

    pump_poll._watch_preview = []  # deploy boot
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "ZEROKEEP" in symbols
    assert "BFONEKEEP" in symbols


def test_sol_graduating_keeps_first_seen_after_trench_eviction():
    # Live 05:20: CHESTER 02:50 / 0.77 left the 15-chair persist
    # (no Token row), then bounced at 05:16 with GAMBLER's stamp.
    # Keep the oldest clock so 8h freeze can still fire.
    from datetime import datetime, timezone

    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll

    init_db()
    aged = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=18)).isoformat()
    pump_poll._watch_preview = []
    pump_poll._first_seen_clocks = {}
    pump_poll._store_watch_preview_rows(
        [
            {
                "address": "ChesterClockMint111111111111111111111",
                "symbol": "CHESTCLK",
                "name": "CHESTCLK",
                "usd_market_cap": 15011.23,
                "progress": 0.767,
            },
            {
                "address": "MmwaKeepClockMint11111111111111111111",
                "symbol": "MMWACLK",
                "name": "MMWACLK",
                "usd_market_cap": 28054.07,
                "progress": 0.925,
            },
        ]
    )
    chest = next(r for r in pump_poll._watch_preview if r.get("symbol") == "CHESTCLK")
    chest_mint = chest["mint"]
    chest["first_seen"] = aged
    pump_poll._first_seen_clocks[chest_mint] = aged
    pump_poll._persist_clocks()
    pump_poll._persist_watch_preview(pump_poll._watch_preview)

    pump_poll._watch_preview = []
    pump_poll._first_seen_clocks = {}
    pump_poll._store_watch_preview_rows(
        [
            {
                "address": "MmwaKeepClockMint11111111111111111111",
                "symbol": "MMWACLK",
                "name": "MMWACLK",
                "usd_market_cap": 28054.07,
                "progress": 0.925,
            }
        ]
    )

    pump_poll._watch_preview = []
    pump_poll._first_seen_clocks = {}
    pump_poll._store_watch_preview_rows(
        [
            {
                "address": "ChesterClockMint111111111111111111111",
                "symbol": "CHESTCLK",
                "name": "CHESTCLK",
                "usd_market_cap": 15011.23,
                "progress": 0.767,
            },
            {
                "address": "MmwaKeepClockMint11111111111111111111",
                "symbol": "MMWACLK",
                "name": "MMWACLK",
                "usd_market_cap": 28054.07,
                "progress": 0.925,
            },
        ]
    )
    bounced = next(r for r in pump_poll._watch_preview if r.get("symbol") == "CHESTCLK")
    assert bounced.get("first_seen") == aged
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "MMWACLK" in symbols
    assert "CHESTCLK" in symbols


def test_sol_graduating_drops_frozen_stuck_door():
    # Live 17:40: ZERO $29876 / 0.941 sat for hours. A this-window
    # 0.93 book stays; the 8h stuck-at-the-door trench does not.
    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll
    from launchfinder.ingest.pump_poll import _store_watch_preview_rows
    from launchfinder.models import Token

    init_db()
    pump_poll._watch_preview = []
    session = SessionLocal()
    now = utcnow()
    frozen = Token(
        mint="ZeroStuckDoorMint1",
        symbol="ZEROFZ",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(hours=10),
        migrated_at=now - timedelta(hours=10),
        is_historical=False,
    )
    fresh = Token(
        mint="BfoneFreshDoorMint",
        symbol="BFONEFR",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(hours=1),
        migrated_at=now - timedelta(hours=1),
        is_historical=False,
    )
    mid = Token(
        mint="MidFillFreshMint11",
        symbol="MIDFILL",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(hours=10),
        migrated_at=now - timedelta(hours=10),
        is_historical=False,
    )
    session.add_all([frozen, fresh, mid])
    session.commit()
    session.close()

    _store_watch_preview_rows(
        [
            {
                "address": "ZeroStuckDoorMint1",
                "symbol": "ZEROFZ",
                "name": "ZEROFZ",
                "usd_market_cap": 29876.42,
                "progress": 0.941,
            },
            {
                "address": "BfoneFreshDoorMint",
                "symbol": "BFONEFR",
                "name": "BFONEFR",
                "usd_market_cap": 29175.17,
                "progress": 0.937,
            },
            {
                "address": "MidFillFreshMint11",
                "symbol": "MIDFILL",
                "name": "MIDFILL",
                "usd_market_cap": 12000.0,
                "progress": 0.62,
            },
        ]
    )
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "BFONEFR" in symbols
    assert "MIDFILL" in symbols
    assert "ZEROFZ" not in symbols

    # Deploy boot reloads persist without first_seen. Backfill from Token.
    from launchfinder.ingest import pump_poll
    from launchfinder.models import ScanState

    session = SessionLocal()
    session.query(ScanState).filter(ScanState.key == pump_poll.GRAD_KEY).delete()
    session.add(
        ScanState(
            key=pump_poll.GRAD_KEY,
            value='[{"mint":"ZeroStuckDoorMint1","symbol":"ZEROFZ","name":"ZEROFZ","mcap_usd":29876.42,"progress":0.941},{"mint":"BfoneFreshDoorMint","symbol":"BFONEFR","name":"BFONEFR","mcap_usd":29175.17,"progress":0.937},{"mint":"MidFillFreshMint11","symbol":"MIDFILL","name":"MIDFILL","mcap_usd":12000.0,"progress":0.62}]',
        )
    )
    session.commit()
    session.close()
    pump_poll._watch_preview = []
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "BFONEFR" in symbols
    assert "MIDFILL" in symbols
    assert "ZEROFZ" not in symbols


def test_sol_graduating_drops_bonded_stuck_after_two_hours():
    # Live 18:25: HOODINU 0.999 / $43k / 2.1h sat watch #1.
    # The 8h / 0.90 door waits. A 2h book parked at 0.99+ is
    # bonded leftover. This-window 0.999 / 1h stays. SONIC
    # 0.84 / 11h stays. Do not change stuck-door 0.90.
    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll
    from launchfinder.ingest.pump_poll import _store_watch_preview_rows
    from launchfinder.models import Token

    init_db()
    pump_poll._watch_preview = []
    session = SessionLocal()
    now = utcnow()
    bonded = Token(
        mint="HoodinuBondedStuckMint111111111",
        symbol="HOODSTK",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(hours=2, minutes=6),
        migrated_at=now - timedelta(hours=2, minutes=6),
        is_historical=False,
    )
    young = Token(
        mint="YoungBondedAlmostMint1111111111",
        symbol="YOUNG99",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(hours=1),
        migrated_at=now - timedelta(hours=1),
        is_historical=False,
    )
    sonic = Token(
        mint="SonicMidProgressStayMint1111111",
        symbol="SONICST",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(hours=11),
        migrated_at=now - timedelta(hours=11),
        is_historical=False,
    )
    session.add_all([bonded, young, sonic])
    session.commit()
    session.close()

    _store_watch_preview_rows(
        [
            {
                "address": "HoodinuBondedStuckMint111111111",
                "symbol": "HOODSTK",
                "name": "HOODSTK",
                "usd_market_cap": 43048.1,
                "progress": 0.99997,
            },
            {
                "address": "YoungBondedAlmostMint1111111111",
                "symbol": "YOUNG99",
                "name": "YOUNG99",
                "usd_market_cap": 29175.17,
                "progress": 0.999,
            },
            {
                "address": "SonicMidProgressStayMint1111111",
                "symbol": "SONICST",
                "name": "SONICST",
                "usd_market_cap": 19791.72,
                "progress": 0.842,
            },
        ]
    )
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "YOUNG99" in symbols
    assert "SONICST" in symbols
    assert "HOODSTK" not in symbols


def test_sol_graduating_drops_legacy_stuck_door_without_token():
    # ZERO was never ingested (still on the curve). Old persist has
    # no first_seen; stamp it aged so the chair frees immediately.
    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll
    from launchfinder.models import ScanState

    init_db()
    session = SessionLocal()
    session.query(ScanState).filter(ScanState.key == pump_poll.GRAD_KEY).delete()
    session.add(
        ScanState(
            key=pump_poll.GRAD_KEY,
            value='[{"mint":"LegacyZeroNoTok11","symbol":"ZEROLEG","name":"ZEROLEG","mcap_usd":29876.42,"progress":0.941},{"mint":"LegacyNearKeep111","symbol":"NEARLEG","name":"NEARLEG","mcap_usd":18000.0,"progress":0.71}]',
        )
    )
    session.commit()
    session.close()
    pump_poll._watch_preview = []
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "NEARLEG" in symbols
    assert "ZEROLEG" not in symbols
    near = next(r for r in data["graduating_soon"] if r["symbol"] == "NEARLEG")
    assert near.get("first_seen")


def test_restore_watch_boards_refills_empty_memory():
    """worker + lifespan call this after init_db so both strips refill
    before the first poll. GET already restores; boot must too."""
    from launchfinder.ingest import pump_poll, rh_poll
    from launchfinder.worker import restore_watch_boards

    init_db()
    rh_poll._watch_preview = []
    pump_poll._watch_preview = []
    rh_poll._store_watch_preview_rows(
        [
            {
                "address": "0xrhbootrestore000000000000000000000001",
                "symbol": "BOOTRH",
                "name": "BOOTRH",
                "usd_market_cap": 9100,
                "progress": 0.71,
            }
        ]
    )
    pump_poll._store_watch_preview_rows(
        [
            {
                "address": "BootSolRestoreMint111111111111111",
                "symbol": "BOOTSOL",
                "name": "BOOTSOL",
                "usd_market_cap": 14200,
                "progress": 0.66,
            }
        ]
    )
    rh_poll._watch_preview = []
    pump_poll._watch_preview = []
    restore_watch_boards()
    assert any(r.get("symbol") == "BOOTRH" for r in rh_poll._watch_preview)
    assert any(r.get("symbol") == "BOOTSOL" for r in pump_poll._watch_preview)


def test_sol_graduating_drops_same_mint_fat_hunt():
    # Live 06:00: FARTATM 0.999 sat watch #1 while the same mint was
    # already hunt #5 / 1.04× / $18k / 322w. A launched Sol book is
    # not graduating. PULSE-class mid-fill with no hunt book stays.
    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll
    from launchfinder.ingest.pump_poll import _store_watch_preview_rows

    init_db()
    pump_poll._watch_preview = []
    session = SessionLocal()
    now = utcnow()
    launched = Token(
        mint="SolFartAtmHuntMint111111111111111111",
        symbol="FARTDROP",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(minutes=40),
        migrated_at=now - timedelta(minutes=40),
        is_historical=False,
    )
    launched.research = Research(p_good=0.48, holder_count=322, features_json="{}")
    session.add(launched)
    session.flush()
    session.add(
        Outcome(
            token_id=launched.id,
            t0_mcap=54_000.0,
            max_mcap=56_105.0,
            multiple=1.04,
            last_liq=18_180.0,
        )
    )
    session.commit()
    session.close()

    _store_watch_preview_rows(
        [
            {
                "address": "SolFartAtmHuntMint111111111111111111",
                "symbol": "FARTDROP",
                "name": "FARTDROP",
                "usd_market_cap": 56105.0,
                "progress": 0.999,
            },
            {
                "address": "SolPulseKeepWatchMint11111111111111",
                "symbol": "PULSEKP",
                "name": "PULSEKP",
                "usd_market_cap": 18400.0,
                "progress": 0.791,
            },
        ]
    )
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "PULSEKP" in symbols
    assert "FARTDROP" not in symbols


def test_sol_watch_read_drops_same_mint_fat_hunt_without_store():
    # Live 09:24: Bulljak 0.986 sat watch #1 after hunt 1.33× /
    # $16k / 199w because watch_preview only filtered frozen
    # trenches. An empty trench write keeps the persist.
    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll

    init_db()
    session = SessionLocal()
    now = utcnow()
    launched = Token(
        mint="SolBulljakReadDropMint11111111111111",
        symbol="BULLREAD",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(minutes=8),
        migrated_at=now - timedelta(minutes=8),
        is_historical=False,
    )
    launched.research = Research(p_good=0.08, holder_count=199, features_json="{}")
    session.add(launched)
    session.flush()
    session.add(
        Outcome(
            token_id=launched.id,
            t0_mcap=33_800.0,
            max_mcap=44_831.0,
            multiple=1.33,
            last_liq=16_047.0,
        )
    )
    session.commit()
    session.close()

    pump_poll._watch_preview = [
        {
            "mint": "SolBulljakReadDropMint11111111111111",
            "symbol": "BULLREAD",
            "name": "BULLREAD",
            "mcap_usd": 38267.0,
            "progress": 0.986,
            "first_seen": (now - timedelta(minutes=8)).isoformat(),
        },
        {
            "mint": "SolPulseKeepReadMint111111111111111",
            "symbol": "PULSERD",
            "name": "PULSERD",
            "mcap_usd": 18400.0,
            "progress": 0.791,
            "first_seen": (now - timedelta(minutes=20)).isoformat(),
        },
    ]
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "PULSERD" in symbols
    assert "BULLREAD" not in symbols


def test_sol_watch_read_drops_same_mint_without_fat_book():
    # Live 12:00: solshiba 0.999 sat watch #1 after the same mint
    # was already hunt #4. Fat/crowded was the FARTATM example, not
    # a gate — a launched Outcome (skinny or missing holders) is
    # not graduating. PULSE-class with no hunt row stays.
    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll

    init_db()
    session = SessionLocal()
    now = utcnow()
    launched = Token(
        mint="SolShibaSameMintWatchDrop11111111111",
        symbol="SHIBDROP",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(minutes=12),
        migrated_at=now - timedelta(minutes=12),
        is_historical=False,
    )
    session.add(launched)
    session.flush()
    session.add(
        Outcome(
            token_id=launched.id,
            t0_mcap=41_330.0,
            max_mcap=52_517.0,
            multiple=1.27,
            last_liq=2_100.0,
        )
    )
    session.commit()
    session.close()

    pump_poll._watch_preview = [
        {
            "mint": "SolShibaSameMintWatchDrop11111111111",
            "symbol": "SHIBDROP",
            "name": "SHIBDROP",
            "mcap_usd": 41143.0,
            "progress": 0.999,
            "first_seen": (now - timedelta(minutes=12)).isoformat(),
        },
        {
            "mint": "SolPulseKeepShibaWatchMint111111111",
            "symbol": "PULSESH",
            "name": "PULSESH",
            "mcap_usd": 18400.0,
            "progress": 0.791,
            "first_seen": (now - timedelta(minutes=20)).isoformat(),
        },
    ]
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "PULSESH" in symbols
    assert "SHIBDROP" not in symbols


def test_sol_graduating_drops_hunt_ticker_twins():
    # Live 06:00: BEAR 0.66 sat watch while a different BEAR mint
    # already had a $18k / 282w / 1.98× hunt book. Same-ticker fat
    # hunt twins are not graduating. MvC-class stays.
    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll
    from launchfinder.ingest.pump_poll import _store_watch_preview_rows

    init_db()
    pump_poll._watch_preview = []
    session = SessionLocal()
    now = utcnow()
    hunt = Token(
        mint="SolBearHuntTwinMint11111111111111111",
        symbol="BEARTWN",
        chain="sol",
        source="poll",
        first_seen_at=now - timedelta(minutes=8),
        migrated_at=now - timedelta(minutes=8),
        is_historical=False,
    )
    hunt.research = Research(p_good=0.48, holder_count=282, features_json="{}")
    session.add(hunt)
    session.flush()
    session.add(
        Outcome(
            token_id=hunt.id,
            t0_mcap=29_800.0,
            max_mcap=58_953.0,
            multiple=1.98,
            last_liq=18_839.0,
        )
    )
    session.commit()
    session.close()

    _store_watch_preview_rows(
        [
            {
                "address": "SolBearWatchTwinMint1111111111111111",
                "symbol": "BEARTWN",
                "name": "BEARTWN curve",
                "usd_market_cap": 14200.0,
                "progress": 0.663,
            },
            {
                "address": "SolMvcKeepWatchMint1111111111111111",
                "symbol": "MVCKEEP",
                "name": "MVCKEEP",
                "usd_market_cap": 16100.0,
                "progress": 0.848,
            },
        ]
    )
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "MVCKEEP" in symbols
    assert "BEARTWN" not in symbols


def test_sol_graduating_store_keeps_persist_first_seen_across_boot():
    # Live 18:05: empty memory + trench rewrite reset ZERO first_seen
    # to now. Restore persist before store so the 8h clock survives.
    from datetime import datetime, timezone, timedelta

    from launchfinder.app import graduating_soon
    from launchfinder.ingest import pump_poll
    from launchfinder.models import ScanState

    init_db()
    aged = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    session = SessionLocal()
    session.query(ScanState).filter(ScanState.key == pump_poll.GRAD_KEY).delete()
    session.add(
        ScanState(
            key=pump_poll.GRAD_KEY,
            value=json.dumps(
                [
                    {
                        "mint": "ZeroBootKeepMint1",
                        "symbol": "ZEROBOOT",
                        "name": "ZEROBOOT",
                        "mcap_usd": 29876.42,
                        "progress": 0.941,
                        "first_seen": aged,
                    },
                    {
                        "mint": "NearBootKeepMint1",
                        "symbol": "NEARBOOT",
                        "name": "NEARBOOT",
                        "mcap_usd": 18000.0,
                        "progress": 0.71,
                        "first_seen": aged,
                    },
                ]
            ),
        )
    )
    session.commit()
    session.close()
    pump_poll._watch_preview = []
    pump_poll._store_watch_preview_rows(
        [
            {
                "address": "ZeroBootKeepMint1",
                "symbol": "ZEROBOOT",
                "name": "ZEROBOOT",
                "usd_market_cap": 29876.42,
                "progress": 0.941,
            },
            {
                "address": "NearBootKeepMint1",
                "symbol": "NEARBOOT",
                "name": "NEARBOOT",
                "usd_market_cap": 18000.0,
                "progress": 0.71,
            },
        ]
    )
    data = asyncio.run(graduating_soon())
    symbols = [r["symbol"] for r in data["graduating_soon"]]
    assert "NEARBOOT" in symbols
    assert "ZEROBOOT" not in symbols


def test_moonbag_strategy_rides_runners():
    init_db()
    with session_scope() as session:
        # confirmed 12x runner: moonbag half rides to 10x
        moon = Token(mint="MoonbagMint11111", symbol="MOON", source="poll", first_seen_at=utcnow())
        moon.research = Research(p_good=0.8, features_json="{}")
        session.add(moon)
        session.flush()
        session.add(Snapshot(token_id=moon.id, kind="post", mcap_usd=12 * 80_000.0, liquidity_usd=90_000.0))
        session.add(Outcome(token_id=moon.id, t0_mcap=80_000.0, max_mcap=12 * 80_000.0, multiple=12.0, label=1, t24h_mcap=500_000.0))

    flat = asyncio.run(paper_ledger(min_p=0.7, target=2.0, strategy="flat"))
    moonbag = asyncio.run(paper_ledger(min_p=0.7, target=2.0, strategy="moonbag"))
    flat_row = [t for t in flat["closed"] if t["mint"] == "MoonbagMint11111"][0]
    moon_row = [t for t in moonbag["closed"] if t["mint"] == "MoonbagMint11111"][0]
    assert flat_row["return_pct"] == 100.0          # sold everything at 2x
    assert moon_row["return_pct"] == 500.0          # half at 2x (+50%), half at 10x (+450%)
    scale5 = asyncio.run(paper_ledger(min_p=0.7, target=2.0, ride=5.0, strategy="moonbag"))
    scale5_row = [t for t in scale5["closed"] if t["mint"] == "MoonbagMint11111"][0]
    assert scale5_row["return_pct"] == 250.0        # half at 2x (+50%), half at 5x (+200%)
    assert scale5["ride"] == 5.0
    assert scale5["target"] == 2.0


def test_sol_paper_buys_live_t0_not_graduation_floor():
    # FRUG-class: outcome.t0 is $69k but the first live book was $264k.
    # Treating the floor as the fill minted a fake 5x moonbag. CAC at a
    # real $69k graduation must still pay.
    init_db()
    with session_scope() as session:
        frug = Token(mint="FrugFloorEntryMint1", symbol="FRUG", source="poll", first_seen_at=utcnow())
        frug.research = Research(p_good=0.75, features_json="{}")
        session.add(frug)
        session.flush()
        session.add(
            Outcome(
                token_id=frug.id,
                t0_mcap=69_000.0,
                max_mcap=350_000.0,
                multiple=5.07,
                label=1,
                t24h_mcap=350_000.0,
            )
        )
        session.add(Snapshot(token_id=frug.id, kind="t0", mcap_usd=264_000.0, liquidity_usd=33_000.0))
        session.add(Snapshot(token_id=frug.id, kind="t6h", mcap_usd=350_000.0, liquidity_usd=40_000.0))

        cac = Token(mint="CacHonest69kMint11", symbol="CAC", source="poll", first_seen_at=utcnow())
        cac.research = Research(p_good=0.79, features_json="{}")
        session.add(cac)
        session.flush()
        session.add(
            Outcome(
                token_id=cac.id,
                t0_mcap=69_000.0,
                max_mcap=69_000.0 * 10,
                multiple=10.0,
                label=1,
                t24h_mcap=69_000.0 * 8,
            )
        )
        session.add(Snapshot(token_id=cac.id, kind="t0", mcap_usd=69_000.0, liquidity_usd=40_000.0))
        session.add(Snapshot(token_id=cac.id, kind="t6h", mcap_usd=69_000.0 * 10, liquidity_usd=140_000.0))

    book = asyncio.run(paper_ledger(min_p=0.7, target=5.0, ride=10.0, strategy="moonbag"))
    frug_row = [t for t in book["closed"] if t["mint"] == "FrugFloorEntryMint1"][0]
    cac_row = [t for t in book["closed"] if t["mint"] == "CacHonest69kMint11"][0]
    assert frug_row["entry_mcap"] == 264_000
    assert frug_row["return_pct"] < 50.0  # 350/264, not a 5x from $69k
    assert cac_row["entry_mcap"] == 69_000
    assert cac_row["return_pct"] == 650.0  # half@5x + half@10x


def test_sol_paper_uses_t0_score_not_repaired_p():
    # Organic-book repair lifted MACRODUCK-class 0.31 → 0.91. Paper must
    # still judge the at-entry t0 snap, or every later lift floods the desk
    # with rugs. RH keeps research.p_good (young-model repairs are fills).
    init_db()
    with session_scope() as session:
        lifted = Token(mint="LiftedOrganicMint1b", symbol="DUCK", source="poll", first_seen_at=utcnow())
        lifted.research = Research(p_good=0.91, features_json="{}")
        session.add(lifted)
        session.flush()
        session.add(
            Outcome(
                token_id=lifted.id,
                t0_mcap=41_695.0,
                max_mcap=50_000.0,
                multiple=1.2,
                label=0,
                t24h_mcap=20_000.0,
            )
        )
        session.add(Snapshot(token_id=lifted.id, kind="t0", mcap_usd=41_695.0, liquidity_usd=40_000.0, p_good=0.31))

        honest = Token(mint="HonestEntryMint11b", symbol="CAC", source="poll", first_seen_at=utcnow())
        honest.research = Research(p_good=0.79, features_json="{}")
        session.add(honest)
        session.flush()
        session.add(
            Outcome(
                token_id=honest.id,
                t0_mcap=69_000.0,
                max_mcap=69_000.0 * 6,
                multiple=6.0,
                label=1,
                t24h_mcap=69_000.0 * 6,
            )
        )
        session.add(Snapshot(token_id=honest.id, kind="t0", mcap_usd=69_000.0, liquidity_usd=40_000.0, p_good=0.79))
        session.add(Snapshot(token_id=honest.id, kind="t6h", mcap_usd=69_000.0 * 6, liquidity_usd=80_000.0))

        rh = Token(
            mint="0xrhrepairpaper00000000000000000000000b",
            symbol="SANDIH",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=utcnow(),
        )
        rh.research = Research(p_good=0.55, features_json="{}")
        session.add(rh)
        session.flush()
        session.add(
            Outcome(
                token_id=rh.id,
                t0_mcap=39_000.0,
                max_mcap=113_000.0,
                multiple=2.9,
                label=1,
                t24h_mcap=113_000.0,
                last_liq=20_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=rh.id,
                kind="t0",
                mcap_usd=39_000.0,
                liquidity_usd=15_000.0,
                volume_h1=8_000.0,
                p_good=0.43,
            )
        )
        session.add(Snapshot(token_id=rh.id, kind="t6h", mcap_usd=113_000.0, liquidity_usd=20_000.0, volume_h1=12_000.0))

    sol = asyncio.run(paper_ledger(min_p=0.7, target=5.0, ride=10.0, strategy="moonbag", chain="sol"))
    sol_mints = {t["mint"] for t in sol["closed"]} | {t["mint"] for t in sol["open"]}
    assert "LiftedOrganicMint1b" not in sol_mints
    cac = [t for t in sol["closed"] if t["mint"] == "HonestEntryMint11b"]
    assert cac and cac[0]["p_good"] == 0.79

    rh_book = asyncio.run(paper_ledger(min_p=0.5, target=5.0, ride=10.0, strategy="moonbag", chain="robinhood"))
    sandih = [t for t in rh_book["closed"] if t["mint"] == "0xrhrepairpaper00000000000000000000000b"]
    assert sandih and sandih[0]["p_good"] == 0.55


def test_sol_paper_skips_artifact_multiples_above_80x():
    # Live ARROW: outcome.multiple 4929x at the $69k floor, paper paid 650%
    # moonbag as if it were a 10x ride. CAC 44x must stay.
    init_db()
    with session_scope() as session:
        arrow = Token(mint="ArrowFake4929Mint", symbol="ARROW", source="poll", first_seen_at=utcnow())
        arrow.research = Research(p_good=0.84, features_json="{}")
        session.add(arrow)
        session.flush()
        session.add(
            Outcome(
                token_id=arrow.id,
                t0_mcap=69_000.0,
                max_mcap=69_000.0 * 4929.53,
                multiple=4929.53,
                label=1,
                t24h_mcap=69_000.0 * 10,
            )
        )
        session.add(Snapshot(token_id=arrow.id, kind="t6h", mcap_usd=69_000.0 * 4929.53, liquidity_usd=40_000.0))

        cac = Token(mint="CacHonest44xMint1", symbol="CAC", source="poll", first_seen_at=utcnow())
        cac.research = Research(p_good=0.79, features_json="{}")
        session.add(cac)
        session.flush()
        session.add(
            Outcome(
                token_id=cac.id,
                t0_mcap=69_000.0,
                max_mcap=69_000.0 * 44.1,
                multiple=44.1,
                label=1,
                t24h_mcap=69_000.0 * 20,
            )
        )
        session.add(Snapshot(token_id=cac.id, kind="t6h", mcap_usd=69_000.0 * 44.1, liquidity_usd=140_000.0))

    book = asyncio.run(paper_ledger(min_p=0.7, target=5.0))
    mints = {t["mint"] for t in book.get("open") or []} | {t["mint"] for t in book.get("closed") or []}
    assert "ArrowFake4929Mint" not in mints
    assert "CacHonest44xMint1" in mints


def test_robinhood_paper_skips_ghost_t0():
    init_db()
    with session_scope() as session:
        ghost = Token(
            mint="0xrhghost000000000000000000000000000000001",
            symbol="METH",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        ghost.research = Research(p_good=0.56, features_json="{}")
        session.add(ghost)
        session.flush()
        session.add(Outcome(token_id=ghost.id, t0_mcap=34_195.0, max_mcap=48_061.0, multiple=1.41))
        session.add(Snapshot(token_id=ghost.id, kind="t0", mcap_usd=34_195.0, liquidity_usd=105.0, volume_h1=8.0))

        live = Token(
            mint="0xrhghost000000000000000000000000000000002",
            symbol="BRICKED",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        live.research = Research(p_good=0.54, features_json="{}")
        session.add(live)
        session.flush()
        session.add(Outcome(token_id=live.id, t0_mcap=41_774.0, max_mcap=101_919.0, multiple=2.44))
        session.add(Snapshot(token_id=live.id, kind="t0", mcap_usd=41_774.0, liquidity_usd=30_804.0, volume_h1=14_730.0))

    book = asyncio.run(paper_ledger(min_p=0.5, chain="robinhood"))
    mints = {t["mint"] for t in book.get("open") or []} | {t["mint"] for t in book.get("closed") or []}
    assert "0xrhghost000000000000000000000000000000001" not in mints
    assert "0xrhghost000000000000000000000000000000002" in mints


def test_robinhood_paper_skips_prepumped_t0():
    # Live CURATOR: t0 $172k / $82k liq, then the book was $24k. 4.3× the
    # RH floor is a start-high fill, not a 5-50x entry.
    init_db()
    with session_scope() as session:
        pumped = Token(
            mint="0xrhcurator000000000000000000000000000001",
            symbol="CURATOR",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        pumped.research = Research(p_good=0.54, features_json="{}", risk_flags_json="[]")
        session.add(pumped)
        session.flush()
        session.add(Outcome(token_id=pumped.id, t0_mcap=172_668.0, max_mcap=172_668.0, multiple=1.0))
        session.add(Snapshot(token_id=pumped.id, kind="t0", mcap_usd=172_668.0, liquidity_usd=82_752.0, volume_h1=71_035.0))

        honest = Token(
            mint="0xrhsofie00000000000000000000000000000001",
            symbol="SOFIE",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        honest.research = Research(p_good=0.54, features_json="{}", risk_flags_json="[]")
        session.add(honest)
        session.flush()
        session.add(Outcome(token_id=honest.id, t0_mcap=24_170.0, max_mcap=64_291.0, multiple=2.66))
        session.add(Snapshot(token_id=honest.id, kind="t0", mcap_usd=24_170.0, liquidity_usd=24_561.0, volume_h1=18_000.0))

    book = asyncio.run(paper_ledger(min_p=0.5, target=5.0, chain="robinhood"))
    mints = {t["mint"] for t in book.get("open") or []} | {t["mint"] for t in book.get("closed") or []}
    assert "0xrhcurator000000000000000000000000000001" not in mints
    assert "0xrhsofie00000000000000000000000000000001" in mints


def test_robinhood_paper_skips_kwak_class_3x_floor_t0():
    # Live KWAK $119k / ROBUX $123k — 3× the $40k RH floor, under Solana's
    # 4× line. That is already a start-high fill; OOOF $35k must stay.
    init_db()
    with session_scope() as session:
        kwak = Token(
            mint="0xrhkwak000000000000000000000000000000001",
            symbol="KWAK",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        kwak.research = Research(p_good=0.54, features_json="{}", risk_flags_json="[]")
        session.add(kwak)
        session.flush()
        session.add(Outcome(token_id=kwak.id, t0_mcap=119_604.0, max_mcap=119_604.0, multiple=1.0))
        session.add(Snapshot(token_id=kwak.id, kind="t0", mcap_usd=119_604.0, liquidity_usd=78_097.0, volume_h1=67_485.0))

        ooof = Token(
            mint="0xrhooof000000000000000000000000000000001",
            symbol="OOOF",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        ooof.research = Research(p_good=0.56, features_json="{}", risk_flags_json="[]")
        session.add(ooof)
        session.flush()
        session.add(Outcome(token_id=ooof.id, t0_mcap=35_110.0, max_mcap=217_536.0, multiple=6.2, label=1))
        session.add(Snapshot(token_id=ooof.id, kind="t0", mcap_usd=35_110.0, liquidity_usd=33_112.0, volume_h1=16_885.0))
        session.add(Snapshot(token_id=ooof.id, kind="t15m", mcap_usd=217_536.0, liquidity_usd=153_104.0, volume_h1=378_044.0))

    book = asyncio.run(paper_ledger(min_p=0.5, target=5.0, chain="robinhood"))
    mints = {t["mint"] for t in book.get("open") or []} | {t["mint"] for t in book.get("closed") or []}
    assert "0xrhkwak000000000000000000000000000000001" not in mints
    assert "0xrhooof000000000000000000000000000000001" in mints


def test_robinhood_paper_exits_live_book_at_last_print_not_neg85():
    # 24h Dex miss left t24h empty. A live $20k flat book must not close -85%.
    init_db()
    with session_scope() as session:
        flat = Token(
            mint="0xrhfatflat00000000000000000000000000001",
            symbol="FAT",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        flat.research = Research(p_good=0.54, features_json="{}")
        session.add(flat)
        session.flush()
        session.add(
            Outcome(
                token_id=flat.id,
                t0_mcap=20_364.0,
                max_mcap=20_364.0,
                multiple=1.0,
                label=0,
                last_liq=8_200.0,
            )
        )
        session.add(Snapshot(token_id=flat.id, kind="t6h", mcap_usd=20_364.0, liquidity_usd=8_200.0))

        ghost = Token(
            mint="0xrhfatghost0000000000000000000000000001",
            symbol="GHOST",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        ghost.research = Research(p_good=0.54, features_json="{}")
        session.add(ghost)
        session.flush()
        session.add(
            Outcome(
                token_id=ghost.id,
                t0_mcap=20_364.0,
                max_mcap=20_364.0,
                multiple=1.0,
                label=0,
                last_liq=148.0,
            )
        )

    book = asyncio.run(paper_ledger(min_p=0.5, target=5.0, chain="robinhood"))
    closed = {t["mint"]: t for t in book.get("closed") or []}
    assert "0xrhfatflat00000000000000000000000000001" in closed
    assert abs(closed["0xrhfatflat00000000000000000000000000001"]["return_pct"]) < 1.0
    assert "0xrhfatghost0000000000000000000000000001" in closed
    assert closed["0xrhfatghost0000000000000000000000000001"]["return_pct"] == -85.0


def test_robinhood_paper_uses_lower_score_line():
    init_db()
    with session_scope() as session:
        t = Token(
            mint="0xrhpaper000000000000000000000000000000001",
            symbol="BRICKED",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        t.research = Research(p_good=0.54, features_json="{}")
        session.add(t)
        session.flush()
        session.add(Outcome(token_id=t.id, t0_mcap=41_774.0, max_mcap=101_919.0, multiple=2.44))
    tight = asyncio.run(paper_ledger(min_p=0.7, target=5.0, chain="robinhood"))
    loose = asyncio.run(paper_ledger(min_p=0.5, target=5.0, chain="robinhood"))
    tight_mints = {t["mint"] for t in tight.get("open") or []} | {t["mint"] for t in tight.get("closed") or []}
    loose_open = [t for t in (loose.get("open") or []) if t["mint"].startswith("0xrhpaper")]
    assert "0xrhpaper000000000000000000000000000000001" not in tight_mints
    assert loose_open and loose["open_positions"] >= 1


def test_paper_open_marks_to_last_live_not_wick():
    # Live FOMODOG: t0 $50k -> t15m $195k (3.87x) then dumped to $3.4k.
    init_db()
    with session_scope() as session:
        wick = Token(
            mint="0xfomodogpaperlive00000000000000000001",
            symbol="FOMODOG",
            source="rh_pons",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        wick.research = Research(p_good=0.61, holder_count=1131, features_json="{}", risk_flags_json="[]")
        session.add(wick)
        session.flush()
        session.add(
            Outcome(
                token_id=wick.id,
                t0_mcap=50_802.0,
                max_mcap=196_834.0,
                multiple=3.87,
                last_liq=3_468.0,
            )
        )
        now = utcnow()
        from datetime import timedelta

        session.add(Snapshot(token_id=wick.id, kind="t0", mcap_usd=50_802.0, liquidity_usd=22_723.0, volume_h1=10_141.0, taken_at=now - timedelta(minutes=40)))
        session.add(Snapshot(token_id=wick.id, kind="t15m", mcap_usd=194_783.0, liquidity_usd=47_849.0, volume_h1=26_741.0, taken_at=now - timedelta(minutes=20)))
        session.add(Snapshot(token_id=wick.id, kind="early", mcap_usd=3_438.0, liquidity_usd=3_468.0, volume_h1=49_082.0, taken_at=now))

        climb = Token(
            mint="0xboosterspaperlive0000000000000000001",
            symbol="BOOSTER",
            source="rh_trenches",
            chain="robinhood",
            first_seen_at=utcnow(),
        )
        climb.research = Research(p_good=0.68, holder_count=112, features_json="{}", risk_flags_json="[]")
        session.add(climb)
        session.flush()
        session.add(
            Outcome(
                token_id=climb.id,
                t0_mcap=20_000.0,
                max_mcap=51_200.0,
                multiple=2.56,
                last_liq=35_000.0,
            )
        )
        session.add(Snapshot(token_id=climb.id, kind="t0", mcap_usd=20_000.0, liquidity_usd=18_000.0, volume_h1=8_000.0))
        session.add(Snapshot(token_id=climb.id, kind="early", mcap_usd=51_200.0, liquidity_usd=35_000.0, volume_h1=12_000.0))
    book = asyncio.run(paper_ledger(min_p=0.5, target=5.0, chain="robinhood"))
    open_pos = {t["mint"]: t for t in book.get("open") or []}
    dumped = open_pos["0xfomodogpaperlive00000000000000000001"]
    rising = open_pos["0xboosterspaperlive0000000000000000001"]
    assert dumped["multiple"] < 0.2
    assert rising["multiple"] == 2.56


def test_rh_paper_keeps_t0_fill_after_stall_fade():
    # Stall honesty rewrites live research.p_good 0.87→0.48. The desk
    # already bought at t0; fading the card cannot un-enter the fill.
    init_db()
    with session_scope() as session:
        teal = Token(
            mint="0xstallfadepaperteal000000000000000001",
            symbol="TEAL",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=utcnow(),
        )
        teal.research = Research(p_good=0.48, features_json="{}")
        session.add(teal)
        session.flush()
        session.add(
            Outcome(
                token_id=teal.id,
                t0_mcap=40_000.0,
                max_mcap=42_000.0,
                multiple=1.05,
                last_liq=12_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=teal.id,
                kind="t0",
                mcap_usd=40_000.0,
                liquidity_usd=12_000.0,
                volume_h1=5_000.0,
                p_good=0.87,
            )
        )
        session.add(
            Snapshot(
                token_id=teal.id,
                kind="live",
                mcap_usd=42_000.0,
                liquidity_usd=12_000.0,
                volume_h1=4_000.0,
            )
        )
    book = asyncio.run(paper_ledger(min_p=0.7, target=5.0, chain="robinhood"))
    held = (book.get("open") or []) + (book.get("closed") or [])
    row = next(t for t in held if t["mint"] == "0xstallfadepaperteal000000000000000001")
    assert row["p_good"] == 0.87


def test_rh_paper_drops_leftover_t0_fills_that_no_longer_clear_live_p():
    # t0-keep must not resurrect July PUFFLING (p 0.92→0.07). This-window
    # TEAL fades stay. Leftover ASS-class with live p still ≥ min_p stays.
    init_db()
    now = utcnow()
    with session_scope() as session:
        puff = Token(
            mint="0xleftoverpuffpaper000000000000000001",
            symbol="PUFFLING",
            chain="robinhood",
            source="rh_pons",
            created_at_chain=now - timedelta(days=37),
            first_seen_at=now,
        )
        puff.research = Research(p_good=0.48, features_json="{}")
        session.add(puff)
        session.flush()
        session.add(
            Outcome(
                token_id=puff.id,
                t0_mcap=45_000.0,
                max_mcap=97_000.0,
                multiple=0.07,
                last_liq=12_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=puff.id,
                kind="t0",
                mcap_usd=45_000.0,
                liquidity_usd=15_000.0,
                volume_h1=8_000.0,
                p_good=0.92,
            )
        )
        teal = Token(
            mint="0xthiswindowtealpaper00000000000000001",
            symbol="TEALNOW",
            chain="robinhood",
            source="rh_trenches",
            created_at_chain=now - timedelta(hours=2),
            first_seen_at=now - timedelta(hours=2),
        )
        teal.research = Research(p_good=0.48, features_json="{}")
        session.add(teal)
        session.flush()
        session.add(
            Outcome(
                token_id=teal.id,
                t0_mcap=40_000.0,
                max_mcap=42_000.0,
                multiple=1.05,
                last_liq=12_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=teal.id,
                kind="t0",
                mcap_usd=40_000.0,
                liquidity_usd=12_000.0,
                volume_h1=5_000.0,
                p_good=0.87,
            )
        )
        ass = Token(
            mint="0xleftoverasslivep0000000000000000001",
            symbol="ASSKEEP",
            chain="robinhood",
            source="rh_trenches",
            created_at_chain=now - timedelta(hours=20),
            first_seen_at=now - timedelta(hours=4),
        )
        ass.research = Research(p_good=0.72, features_json="{}")
        session.add(ass)
        session.flush()
        session.add(
            Outcome(
                token_id=ass.id,
                t0_mcap=16_000.0,
                max_mcap=66_000.0,
                multiple=2.03,
                last_liq=34_000.0,
            )
        )
        session.add(
            Snapshot(
                token_id=ass.id,
                kind="t0",
                mcap_usd=16_000.0,
                liquidity_usd=12_000.0,
                volume_h1=8_000.0,
                p_good=0.72,
            )
        )
    book = asyncio.run(paper_ledger(min_p=0.7, target=5.0, chain="robinhood"))
    held = {t["mint"] for t in (book.get("open") or []) + (book.get("closed") or [])}
    assert "0xleftoverpuffpaper000000000000000001" not in held
    assert "0xthiswindowtealpaper00000000000000001" in held
    assert "0xleftoverasslivep0000000000000000001" in held


def test_daily_report_shape():
    from launchfinder.app import daily_report

    init_db()
    report = asyncio.run(daily_report())
    for key in ("ingested_24h", "live_now", "labels_24h", "top_live", "evaluation_trend"):
        assert key in report
