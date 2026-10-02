from launchfinder.scoring.outcomes import conviction_tier


def _tier(**kw) -> str:
    base = dict(entry_p=0.7, runner_p=0.8, second_leg=False, liq_now=25_000, hard_stopped=False, forensics={})
    base.update(kw)
    return conviction_tier(**base)


def test_all_gates_pass_is_tier_a():
    assert _tier() == "A"


def test_any_failed_gate_drops_from_a():
    assert _tier(entry_p=0.5) != "A"                 # weak entry
    assert _tier(runner_p=0.7) != "A"                # trajectory not strong enough
    assert _tier(runner_p=None) != "A"               # no trajectory yet
    assert _tier(liq_now=15_000) != "A"              # pool below the bar
    assert _tier(hard_stopped=True) == "C"           # scam flag kills everything
    assert _tier(forensics={"funding_cluster": 0.5, "n": 20}) != "A"
    assert _tier(forensics={"early_exit": 0.8, "n": 20}) != "A"


def test_tier_b_watchlist_paths():
    # decent entry + second leg but no strong runner score yet
    assert _tier(entry_p=0.58, runner_p=0.4, second_leg=True) == "B"
    # decent entry + decent trajectory
    assert _tier(entry_p=0.6, runner_p=0.65, liq_now=6_000) == "B"
    # Live ANGRYCATS sat B at $2.1k after the book dumped.
    assert _tier(entry_p=0.6, runner_p=0.7, liq_now=2_150) == "C"


def test_background_noise_is_c():
    assert _tier(entry_p=0.3, runner_p=0.3) == "C"
    assert _tier(entry_p=0.58, runner_p=0.4, second_leg=False) == "C"


def test_artifact_multiples_and_prepumped_are_not_tier_a():
    # Live HeeHaw 98x / ARROW Dex artifacts were paging as Tier A.
    assert _tier(mcap_ratio=98.48) == "C"
    assert _tier(mcap_ratio=81.0) == "C"
    assert _tier(mcap_ratio=44.1) == "A"  # CAC-class honest runner
    assert _tier(prepumped=True) == "C"


def test_runnerwatch_tiebreaks_saturated_scores_by_multiple():
    # Live RH: a 2.1x and a 4.9x climber both scored runner_p=1.0, so
    # the smaller climb sat above the name closer to the 5x bar.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import ScanState

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with session_scope() as session:
        session.add(
            ScanState(
                key="runnerp:0xbombatie000000000000000000000000000001",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 2.10, "symbol": "BOMBA", "chain": "robinhood", "at": now, "tier": "C"}
                ),
            )
        )
        session.add(
            ScanState(
                key="runnerp:0xrocktie0000000000000000000000000000001",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 4.88, "symbol": "ROCK", "chain": "robinhood", "at": now, "tier": "C"}
                ),
            )
        )
        session.add(
            ScanState(
                key="runnerp:0xpovtie00000000000000000000000000000001",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 3.47, "symbol": "POV", "chain": "robinhood", "at": now, "tier": "C"}
                ),
            )
        )
    board = asyncio.run(runner_watch_board(chain="robinhood"))
    order = [r["symbol"] for r in board["items"] if r["symbol"] in {"BOMBA", "ROCK", "POV"}]
    assert order[:3] == ["ROCK", "POV", "BOMBA"]


def test_runnerwatch_drops_historical_rh_leftovers():
    # Live AIAIAI: quiet-retired leftover kept a runnerp 55.6× row after
    # /api/runners already hid historical RH tape. Solana majors stay.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import ScanState, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    leftover_mint = "0xaiaiaiwatchhist000000000000000000001"
    live_mint = "0xrockwatchlive0000000000000000000001"
    sol_mint = "SolHistWatchMint11111111111111111111"
    with session_scope() as session:
        leftover = Token(
            mint=leftover_mint,
            symbol="AIAIAI",
            chain="robinhood",
            source="rh_trenches",
            is_historical=True,
            first_seen_at=utcnow(),
        )
        live = Token(
            mint=live_mint,
            symbol="ROCK",
            chain="robinhood",
            source="rh_trenches",
            is_historical=False,
            first_seen_at=utcnow(),
        )
        old_sol = Token(
            mint=sol_mint,
            symbol="CAC",
            chain="sol",
            source="poll",
            is_historical=True,
            first_seen_at=utcnow(),
        )
        session.add_all([leftover, live, old_sol])
        session.add(
            ScanState(
                key=f"runnerp:{leftover_mint}",
                value=json.dumps(
                    {"runner_p": 0.53, "mcap_ratio": 55.6, "symbol": "AIAIAI", "chain": "robinhood", "at": now, "tier": "C"}
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{live_mint}",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 4.2, "symbol": "ROCK", "chain": "robinhood", "at": now, "tier": "A"}
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{sol_mint}",
                value=json.dumps(
                    {"runner_p": 0.79, "mcap_ratio": 4.5, "symbol": "CAC", "chain": "sol", "at": now, "tier": "A"}
                ),
            )
        )
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    sol = asyncio.run(runner_watch_board(chain="sol"))
    conv = asyncio.run(conviction_board(chain="robinhood"))
    assert leftover_mint not in {r["mint"] for r in rh["items"]}
    assert live_mint in {r["mint"] for r in rh["items"]}
    assert sol_mint in {r["mint"] for r in sol["items"]}
    assert leftover_mint not in {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert live_mint in {r["mint"] for r in conv["tier_a"]}


def test_runnerwatch_drops_thin_rh_prints():
    # Live CYBERCAB / DESPONSITO: 4 wallets, 2.08x / 2.01x, runner_p=1.0
    # sat on /api/runnerwatch after approaching already split them to thin.
    # ROCK (79 holders) and a Pappy-class 13-wallet Sol sample stay.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Research, ScanState, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    thin_mint = "0xcybercabwatchthin00000000000000000001"
    desp_mint = "0xdesponsitowatchthin000000000000000001"
    live_mint = "0xrockwatchthick0000000000000000000001"
    sol_mint = "SolThinWatchMint22222222222222222222"
    with session_scope() as session:
        thin = Token(
            mint=thin_mint,
            symbol="CYBERCAB",
            chain="robinhood",
            source="rh_trenches",
            is_historical=False,
            first_seen_at=utcnow(),
        )
        desp = Token(
            mint=desp_mint,
            symbol="DESPONSITO",
            chain="robinhood",
            source="rh_trenches",
            is_historical=False,
            first_seen_at=utcnow(),
        )
        live = Token(
            mint=live_mint,
            symbol="ROCK",
            chain="robinhood",
            source="rh_trenches",
            is_historical=False,
            first_seen_at=utcnow(),
        )
        sol_thin = Token(
            mint=sol_mint,
            symbol="Pappy",
            chain="sol",
            source="poll",
            is_historical=False,
            first_seen_at=utcnow(),
        )
        session.add_all([thin, desp, live, sol_thin])
        session.flush()
        thin.research = Research(p_good=0.6, holder_count=4, features_json="{}", risk_flags_json="[]")
        desp.research = Research(p_good=0.55, holder_count=4, features_json="{}", risk_flags_json="[]")
        live.research = Research(p_good=0.65, holder_count=79, features_json="{}", risk_flags_json="[]")
        sol_thin.research = Research(p_good=0.4, holder_count=13, features_json="{}", risk_flags_json="[]")
        session.add(
            ScanState(
                key=f"runnerp:{thin_mint}",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 2.08, "symbol": "CYBERCAB", "chain": "robinhood", "at": now, "tier": "C"}
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{desp_mint}",
                value=json.dumps(
                    {"runner_p": 0.72, "mcap_ratio": 2.01, "symbol": "DESPONSITO", "chain": "robinhood", "at": now, "tier": "A"}
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{live_mint}",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 4.8, "symbol": "ROCK", "chain": "robinhood", "at": now, "tier": "A"}
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{sol_mint}",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 4.2, "symbol": "Pappy", "chain": "sol", "at": now, "tier": "C"}
                ),
            )
        )
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    sol = asyncio.run(runner_watch_board(chain="sol"))
    conv = asyncio.run(conviction_board(chain="robinhood"))
    rh_mints = {r["mint"] for r in rh["items"]}
    assert thin_mint not in rh_mints
    assert desp_mint not in rh_mints
    assert live_mint in rh_mints
    assert sol_mint in {r["mint"] for r in sol["items"]}
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert desp_mint not in conv_mints
    assert live_mint in {r["mint"] for r in conv["tier_a"]}


def test_runnerwatch_drops_stale_and_thin_sol():
    # Live Sol watch ranked USMS 65× (5 wallets) and HeeHaw 48× from
    # day-old ScanState above NICKGPRO. Conviction already used 12h.
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Research, ScanState, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=20)).isoformat()
    fresh = now.isoformat()
    usms = "SolUsmsWatchMint33333333333333333333"
    heehaw = "SolHeeHawStaleMint33333333333333333"
    nick = "SolNickFreshMint3333333333333333333"
    with session_scope() as session:
        u = Token(mint=usms, symbol="USMS", chain="sol", source="poll", first_seen_at=utcnow())
        h = Token(mint=heehaw, symbol="HeeHaw", chain="sol", source="poll", first_seen_at=utcnow())
        n = Token(mint=nick, symbol="NICKGPRO", chain="sol", source="poll", first_seen_at=utcnow())
        session.add_all([u, h, n])
        session.flush()
        u.research = Research(p_good=0.3, holder_count=5, features_json="{}", risk_flags_json="[]")
        h.research = Research(p_good=0.4, holder_count=40, features_json="{}", risk_flags_json="[]")
        n.research = Research(p_good=0.6, holder_count=80, features_json="{}", risk_flags_json="[]")
        session.add(
            ScanState(
                key=f"runnerp:{usms}",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 65.01, "symbol": "USMS", "chain": "sol", "at": fresh, "tier": "C"}
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{heehaw}",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 48.03, "symbol": "HeeHaw", "chain": "sol", "at": stale, "tier": "A"}
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{nick}",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 4.5, "symbol": "NICKGPRO", "chain": "sol", "at": fresh, "tier": "B"}
                ),
            )
        )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    conv = asyncio.run(conviction_board(chain="sol"))
    mints = {r["mint"] for r in sol["items"]}
    assert usms not in mints
    assert heehaw not in mints
    assert nick in mints
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert heehaw not in conv_mints
    assert nick in conv_mints


def test_runner_watch_write_drops_thin_rh_row():
    # Existing CYBERCAB runnerp row must go away so we do not keep
    # spending GMGN forensics on a 4-wallet wick.
    import asyncio
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
    from launchfinder.scoring.outcomes import _runner_watch

    init_db()
    mint = "0xcybercabwritedrop00000000000000000001"
    with session_scope() as session:
        tok = Token(
            mint=mint,
            symbol="CYBERCAB",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=utcnow(),
        )
        tok.research = Research(p_good=0.6, holder_count=4, features_json="{}", risk_flags_json="[]")
        session.add(tok)
        session.flush()
        session.add(Outcome(token_id=tok.id, t0_mcap=20_000.0, max_mcap=41_600.0, multiple=2.08, last_liq=1_200.0))
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps({"runner_p": 1.0, "mcap_ratio": 2.08, "symbol": "CYBERCAB", "chain": "robinhood", "tier": "C"}),
            )
        )
        session.flush()
        asyncio.run(
            _runner_watch(
                session,
                tok,
                session.query(Outcome).filter(Outcome.token_id == tok.id).one(),
                {"mcap_usd": 41_600.0, "liquidity_usd": 1_200.0, "volume_h1": 800.0},
            )
        )
        assert session.query(ScanState).filter(ScanState.key == f"runnerp:{mint}").one_or_none() is None


def test_runner_watch_write_drops_labeled_loss_row():
    # Post-label tracking rewrote SHARK's runnerp after the 24h loss.
    # Write path must delete the row so we do not keep a labeled 4.11×
    # on watch or spend GMGN on it.
    import asyncio
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
    from launchfinder.scoring.outcomes import _runner_watch

    init_db()
    mint = "0xsharkwritedroplabeled000000000000001"
    with session_scope() as session:
        tok = Token(
            mint=mint,
            symbol="SHARKLOSSW",
            chain="robinhood",
            source="rh_trenches",
            first_seen_at=utcnow(),
        )
        tok.research = Research(p_good=0.65, holder_count=99, features_json="{}", risk_flags_json="[]")
        session.add(tok)
        session.flush()
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=42_374.0,
                max_mcap=174_122.0,
                multiple=4.11,
                last_liq=76_171.0,
                label=0,
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps(
                    {"runner_p": 1.0, "mcap_ratio": 4.11, "symbol": "SHARKLOSSW", "chain": "robinhood", "tier": "A"}
                ),
            )
        )
        session.flush()
        asyncio.run(
            _runner_watch(
                session,
                tok,
                session.query(Outcome).filter(Outcome.token_id == tok.id).one(),
                {"mcap_usd": 112_550.0, "liquidity_usd": 76_171.0, "volume_h1": 0.0},
            )
        )
        assert session.query(ScanState).filter(ScanState.key == f"runnerp:{mint}").one_or_none() is None


def test_runnerwatch_drops_confirmed_5x_both_chains():
    # Live watch was a hall of fame: USMS 65× / HeeHaw 48× on Sol,
    # ROCK 15× / LAST 6× on RH. Those belong on /api/runners.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
    from launchfinder.scoring.outcomes import _runner_watch

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    usms = "SolUsmsAlreadyRanMint4444444444444444"
    climb = "SolClimbWatchMint4444444444444444444"
    rock = "0xrockalreadyran000000000000000000001"
    last = "0xlastclimberwatch0000000000000000001"
    with session_scope() as session:
        for mint, symbol, chain, holders, ratio, tier in (
            (usms, "USMS", "sol", 21, 65.01, "C"),
            (climb, "FADD", "sol", 260, 3.35, "B"),
            (rock, "ROCK", "robinhood", 79, 15.7, "A"),
            (last, "CHARLES", "robinhood", 187, 2.40, "A"),
        ):
            tok = Token(mint=mint, symbol=symbol, chain=chain, source="poll", first_seen_at=utcnow())
            session.add(tok)
            session.flush()
            tok.research = Research(p_good=0.7, holder_count=holders, features_json="{}", risk_flags_json="[]")
            session.add(
                ScanState(
                    key=f"runnerp:{mint}",
                    value=json.dumps(
                        {
                            "runner_p": 1.0,
                            "mcap_ratio": ratio,
                            "symbol": symbol,
                            "chain": chain,
                            "at": now,
                            "tier": tier,
                        }
                    ),
                )
            )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    conv_sol = asyncio.run(conviction_board(chain="sol"))
    conv_rh = asyncio.run(conviction_board(chain="robinhood"))
    assert usms not in {r["mint"] for r in sol["items"]}
    assert climb in {r["mint"] for r in sol["items"]}
    assert rock not in {r["mint"] for r in rh["items"]}
    assert last in {r["mint"] for r in rh["items"]}
    assert usms not in {r["mint"] for r in conv_sol["tier_a"] + conv_sol["tier_b"]}
    assert rock not in {r["mint"] for r in conv_rh["tier_a"] + conv_rh["tier_b"]}
    assert last in {r["mint"] for r in conv_rh["tier_a"]}

    with session_scope() as session:
        tok = session.query(Token).filter(Token.mint == rock).one()
        session.add(Outcome(token_id=tok.id, t0_mcap=32_561.0, max_mcap=511_000.0, multiple=15.7, last_liq=193_000.0))
        session.flush()
        asyncio.run(
            _runner_watch(
                session,
                tok,
                session.query(Outcome).filter(Outcome.token_id == tok.id).one(),
                {"mcap_usd": 511_000.0, "liquidity_usd": 193_000.0, "volume_h1": 8_000.0},
            )
        )
        assert session.query(ScanState).filter(ScanState.key == f"runnerp:{rock}").one_or_none() is None


def test_runnerwatch_drops_flat_sub_2x_both_chains():
    # After 5x names left, Sol watch still had ~3200 rows and RH's top
    # 30 was A 1.62 / CHARLES 1.46. Same 2x floor as approaching.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
    from launchfinder.scoring.outcomes import _runner_watch

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    flat_sol = "SolFlatWatchMint555555555555555555555"
    climb_sol = "SolClimb2xWatchMint55555555555555555"
    flat_rh = "0xcharlesflatwatch0000000000000000001"
    climb_rh = "0xbelieveclimbwatch000000000000000001"
    with session_scope() as session:
        for mint, symbol, chain, holders, ratio, tier in (
            (flat_sol, "CHANCE", "sol", 80, 1.45, "A"),
            (climb_sol, "GTA", "sol", 200, 4.45, "C"),
            (flat_rh, "CHARLES", "robinhood", 187, 1.46, "A"),
            (climb_rh, "BELIEVE", "robinhood", 148, 4.97, "A"),
        ):
            tok = Token(mint=mint, symbol=symbol, chain=chain, source="poll", first_seen_at=utcnow())
            session.add(tok)
            session.flush()
            tok.research = Research(p_good=0.7, holder_count=holders, features_json="{}", risk_flags_json="[]")
            session.add(
                ScanState(
                    key=f"runnerp:{mint}",
                    value=json.dumps(
                        {
                            "runner_p": 1.0,
                            "mcap_ratio": ratio,
                            "symbol": symbol,
                            "chain": chain,
                            "at": now,
                            "tier": tier,
                        }
                    ),
                )
            )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    conv_rh = asyncio.run(conviction_board(chain="robinhood"))
    assert flat_sol not in {r["mint"] for r in sol["items"]}
    assert climb_sol in {r["mint"] for r in sol["items"]}
    assert flat_rh not in {r["mint"] for r in rh["items"]}
    assert climb_rh in {r["mint"] for r in rh["items"]}
    assert flat_rh not in {r["mint"] for r in conv_rh["tier_a"] + conv_rh["tier_b"]}
    assert climb_rh in {r["mint"] for r in conv_rh["tier_a"]}

    with session_scope() as session:
        tok = session.query(Token).filter(Token.mint == flat_rh).one()
        session.add(Outcome(token_id=tok.id, t0_mcap=20_000.0, max_mcap=29_200.0, multiple=1.46, last_liq=34_000.0))
        session.flush()
        asyncio.run(
            _runner_watch(
                session,
                tok,
                session.query(Outcome).filter(Outcome.token_id == tok.id).one(),
                {"mcap_usd": 29_200.0, "liquidity_usd": 34_000.0, "volume_h1": 2_000.0},
            )
        )
        assert session.query(ScanState).filter(ScanState.key == f"runnerp:{flat_rh}").one_or_none() is None


def test_runnerwatch_drops_dumped_live_book():
    # Live LAST: ScanState still 3.25× from 00:23 while the book dumped
    # to $5.6k. Approaching already uses last snap / t0; watch did not.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    dumped = "0xlastdumpedwatchsnap0000000000000001"
    climb = "0xasslivewatchsnap0000000000000000001"
    with session_scope() as session:
        last = Token(
            mint=dumped, symbol="LASTDUMP", chain="robinhood", source="rh_trenches", first_seen_at=utcnow()
        )
        ass = Token(
            mint=climb, symbol="ASSLIVE", chain="robinhood", source="rh_trenches", first_seen_at=utcnow()
        )
        last.research = Research(p_good=0.71, holder_count=187, features_json="{}", risk_flags_json="[]")
        ass.research = Research(p_good=0.72, holder_count=47, features_json="{}", risk_flags_json="[]")
        session.add_all([last, ass])
        session.flush()
        session.add(Outcome(token_id=last.id, t0_mcap=40_000.0, max_mcap=248_000.0, multiple=6.2, last_liq=5_600.0, label=1))
        session.add(Outcome(token_id=ass.id, t0_mcap=21_700.0, max_mcap=76_600.0, multiple=3.53, last_liq=27_588.0))
        # Stale high snap still 3.25× — last_liq $5.6k is the leftover book.
        session.add(Snapshot(token_id=last.id, kind="post", mcap_usd=130_000.0, liquidity_usd=5_600.0, taken_at=utcnow()))
        session.add(Snapshot(token_id=ass.id, kind="post", mcap_usd=76_600.0, liquidity_usd=27_588.0, taken_at=utcnow()))
        session.add(
            ScanState(
                key=f"runnerp:{dumped}",
                value=json.dumps(
                    {
                        "runner_p": 0.73,
                        "mcap_ratio": 3.25,
                        "symbol": "LASTDUMP",
                        "chain": "robinhood",
                        "at": now,
                        "tier": "B",
                        "liq_now": 5600,
                    }
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{climb}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 3.53,
                        "symbol": "ASSLIVE",
                        "chain": "robinhood",
                        "at": now,
                        "tier": "A",
                        "liq_now": 27588,
                    }
                ),
            )
        )
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    conv = asyncio.run(conviction_board(chain="robinhood"))
    mints = {r["mint"] for r in rh["items"]}
    assert dumped not in mints
    assert climb in mints
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert dumped not in conv_mints
    assert climb in conv_mints


def test_runnerwatch_drops_confirmed_5x_when_scanstate_is_stale():
    # Live BELIEVE 28.5× / SANDIH 8.9× / A 5.1× sat on RH watch because
    # ScanState still said 3.55 / 4.38 / 4.16. Fat last_liq used to
    # keep them (the LAST leftover needed $8k). Confirmed 5× belong
    # on /api/runners. YOLO 2.81 stays.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    ran = "0xbelievestalewatchratio0000000000001"
    climb = "0xyoloclimbwatchstay0000000000000001"
    with session_scope() as session:
        believe = Token(
            mint=ran, symbol="BELIEVEWIN", chain="robinhood", source="rh_trenches", first_seen_at=utcnow()
        )
        yolo = Token(
            mint=climb, symbol="YOLOSTAY", chain="robinhood", source="rh_pons", first_seen_at=utcnow()
        )
        believe.research = Research(p_good=0.78, holder_count=200, features_json="{}", risk_flags_json="[]")
        yolo.research = Research(p_good=0.64, holder_count=1140, features_json="{}", risk_flags_json="[]")
        session.add_all([believe, yolo])
        session.flush()
        session.add(Outcome(token_id=believe.id, t0_mcap=40_000.0, max_mcap=1_140_000.0, multiple=28.5, last_liq=62_406.0, label=1))
        session.add(Outcome(token_id=yolo.id, t0_mcap=63_516.0, max_mcap=178_000.0, multiple=2.81, last_liq=45_291.0))
        session.add(
            ScanState(
                key=f"runnerp:{ran}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 3.55,
                        "symbol": "BELIEVEWIN",
                        "chain": "robinhood",
                        "at": now,
                        "tier": "A",
                        "liq_now": 62406,
                    }
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{climb}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 2.80,
                        "symbol": "YOLOSTAY",
                        "chain": "robinhood",
                        "at": now,
                        "tier": "B",
                        "liq_now": 45291,
                    }
                ),
            )
        )
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    conv = asyncio.run(conviction_board(chain="robinhood"))
    mints = {r["mint"] for r in rh["items"]}
    assert ran not in mints
    assert climb in mints
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert ran not in conv_mints
    assert climb in conv_mints


def test_runnerwatch_drops_zero_liq_ghosts():
    # Live Bros 2.16× / stonkape 4.15× sat on Sol watch (Bros as Tier B)
    # after approaching already dropped last_liq $0. ANGRYCATS $30k stays.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    ghost = "SolBrosZeroWatchMint1111111111111111"
    stay = "SolAngryWatchStayMint11111111111111"
    with session_scope() as session:
        bros = Token(mint=ghost, symbol="BROSZERO", chain="sol", source="poll", first_seen_at=utcnow())
        angry = Token(mint=stay, symbol="ANGRYSTAY", chain="sol", source="poll", first_seen_at=utcnow())
        bros.research = Research(p_good=0.60, holder_count=80, features_json="{}", risk_flags_json="[]")
        angry.research = Research(p_good=0.60, holder_count=93, features_json="{}", risk_flags_json="[]")
        session.add_all([bros, angry])
        session.flush()
        session.add(Outcome(token_id=bros.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 2.16, multiple=2.16, last_liq=0.0))
        session.add(Outcome(token_id=angry.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 3.04, multiple=3.04, last_liq=30_812.0))
        session.add(
            ScanState(
                key=f"runnerp:{ghost}",
                value=json.dumps(
                    {
                        "runner_p": 0.7,
                        "mcap_ratio": 2.16,
                        "symbol": "BROSZERO",
                        "chain": "sol",
                        "at": now,
                        "tier": "B",
                        "liq_now": 0,
                    }
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{stay}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 3.04,
                        "symbol": "ANGRYSTAY",
                        "chain": "sol",
                        "at": now,
                        "tier": "B",
                        "liq_now": 30812,
                    }
                ),
            )
        )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    conv = asyncio.run(conviction_board(chain="sol"))
    mints = {r["mint"] for r in sol["items"]}
    assert ghost not in mints
    assert stay in mints
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert ghost not in conv_mints
    assert stay in conv_mints


def test_runner_watch_write_drops_zero_liq_ghost():
    import asyncio
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
    from launchfinder.scoring.outcomes import _runner_watch

    init_db()
    mint = "SolStonkZeroWriteMint111111111111111"
    with session_scope() as session:
        tok = Token(mint=mint, symbol="STONKWRITE", chain="sol", source="poll", first_seen_at=utcnow())
        tok.research = Research(p_good=0.13, holder_count=60, features_json="{}", risk_flags_json="[]")
        session.add(tok)
        session.flush()
        session.add(Outcome(token_id=tok.id, t0_mcap=69_000.0, max_mcap=69_000.0 * 4.15, multiple=4.15, last_liq=0.0))
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps({"runner_p": 0.38, "mcap_ratio": 4.15, "symbol": "STONKWRITE", "chain": "sol", "tier": "C"}),
            )
        )
        session.flush()
        asyncio.run(
            _runner_watch(
                session,
                tok,
                session.query(Outcome).filter(Outcome.token_id == tok.id).one(),
                {"mcap_usd": 286_350.0, "liquidity_usd": 0.0, "volume_h1": 0.0},
            )
        )
        assert session.query(ScanState).filter(ScanState.key == f"runnerp:{mint}").one_or_none() is None


def test_runner_watch_write_drops_copycat_row():
    import asyncio
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
    from launchfinder.scoring.outcomes import _runner_watch

    init_db()
    mint = "SolNtdaCopycatWriteDrop111111111111"
    flags = json.dumps(
        [
            "Bonding curve filled almost instantly (bundle risk)",
            "Same ticker launched repeatedly in 24h (copycat spam)",
        ]
    )
    with session_scope() as session:
        tok = Token(mint=mint, symbol="NTDAWRITE", chain="sol", source="websocket", first_seen_at=utcnow())
        tok.research = Research(p_good=0.88, holder_count=130, features_json="{}", risk_flags_json=flags)
        session.add(tok)
        session.flush()
        session.add(Outcome(token_id=tok.id, t0_mcap=227_217.0, max_mcap=1_055_371.0, multiple=4.64, last_liq=100_919.0))
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps({"runner_p": 1.0, "mcap_ratio": 4.46, "symbol": "NTDAWRITE", "chain": "sol", "tier": "A"}),
            )
        )
        session.flush()
        asyncio.run(
            _runner_watch(
                session,
                tok,
                session.query(Outcome).filter(Outcome.token_id == tok.id).one(),
                {"mcap_usd": 1_055_371.0, "liquidity_usd": 100_919.0, "volume_h1": 20_000.0},
            )
        )
        assert session.query(ScanState).filter(ScanState.key == f"runnerp:{mint}").one_or_none() is None


def test_runner_watch_write_drops_rh_airdrop_row():
    import asyncio
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
    from launchfinder.scoring.outcomes import _runner_watch

    init_db()
    mint = "0xrhignairdropwrite00000000000000001"
    with session_scope() as session:
        tok = Token(mint=mint, symbol="IGNWRITE", chain="robinhood", source="rh_pons", first_seen_at=utcnow())
        tok.research = Research(p_good=0.71, holder_count=1131, features_json="{}", risk_flags_json="[]")
        session.add(tok)
        session.flush()
        session.add(Outcome(token_id=tok.id, t0_mcap=10_400.0, max_mcap=33_600.0, multiple=3.23, last_liq=3_354.0))
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps({"runner_p": 0.99, "mcap_ratio": 3.23, "symbol": "IGNWRITE", "chain": "robinhood", "tier": "B"}),
            )
        )
        session.flush()
        asyncio.run(
            _runner_watch(
                session,
                tok,
                session.query(Outcome).filter(Outcome.token_id == tok.id).one(),
                {"mcap_usd": 33_600.0, "liquidity_usd": 3_354.0, "volume_h1": 8_000.0},
            )
        )
        assert session.query(ScanState).filter(ScanState.key == f"runnerp:{mint}").one_or_none() is None


def test_runnerwatch_drops_labeled_24h_losses():
    # Live SHARK 4.11× / JOHNAPPLE 2.47× / WATER 2.5× sat on watch
    # (SHARK and JOHNAPPLE as Tier A) after the 24h label=0. ScanState
    # was still in the 2×–5× band and last_liq was fat. YOLO unlabeled
    # 2.81 stays.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    lost = "0xsharklabeledwatch000000000000000001"
    climb = "0xyolounlabeledwatchstay000000000001"
    with session_scope() as session:
        shark = Token(
            mint=lost, symbol="SHARKLOSS", chain="robinhood", source="rh_trenches", first_seen_at=utcnow()
        )
        yolo = Token(
            mint=climb, symbol="YOLOSTAY2", chain="robinhood", source="rh_pons", first_seen_at=utcnow()
        )
        shark.research = Research(p_good=0.65, holder_count=99, features_json="{}", risk_flags_json="[]")
        yolo.research = Research(p_good=0.64, holder_count=1140, features_json="{}", risk_flags_json="[]")
        session.add_all([shark, yolo])
        session.flush()
        session.add(Outcome(token_id=shark.id, t0_mcap=42_374.0, max_mcap=174_122.0, multiple=4.11, last_liq=76_171.0, label=0))
        session.add(Outcome(token_id=yolo.id, t0_mcap=63_516.0, max_mcap=178_000.0, multiple=2.81, last_liq=45_291.0))
        session.add(
            ScanState(
                key=f"runnerp:{lost}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 4.11,
                        "symbol": "SHARKLOSS",
                        "chain": "robinhood",
                        "at": now,
                        "tier": "A",
                        "liq_now": 76171,
                    }
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{climb}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 2.80,
                        "symbol": "YOLOSTAY2",
                        "chain": "robinhood",
                        "at": now,
                        "tier": "B",
                        "liq_now": 45291,
                    }
                ),
            )
        )
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    conv = asyncio.run(conviction_board(chain="robinhood"))
    mints = {r["mint"] for r in rh["items"]}
    assert lost not in mints
    assert climb in mints
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert lost not in conv_mints
    assert climb in conv_mints


def test_runnerwatch_shows_live_multiple_not_stale_peak():
    # Live ASS ScanState 3.02 sat on watch while approaching used
    # last snap / t0 = 2.03. Show the climb, not the stale peak.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    mint = "0xrhasswatchlivemult00000000000000001"
    with session_scope() as session:
        tok = Token(mint=mint, symbol="ASSLIVE", chain="robinhood", source="rh_pons", first_seen_at=utcnow())
        tok.research = Research(p_good=0.72, holder_count=47, features_json="{}", risk_flags_json="[]")
        session.add(tok)
        session.flush()
        session.add(Outcome(token_id=tok.id, t0_mcap=20_000.0, max_mcap=60_400.0, multiple=3.02, last_liq=33_970.0))
        session.add(
            Snapshot(
                token_id=tok.id,
                kind="live",
                mcap_usd=40_600.0,
                liquidity_usd=25_000.0,
                volume_h1=8_000.0,
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 3.02,
                        "symbol": "ASSLIVE",
                        "chain": "robinhood",
                        "at": now,
                        "tier": "C",
                        "liq_now": 33970,
                    }
                ),
            )
        )
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    hit = next(r for r in rh["items"] if r["mint"] == mint)
    assert abs(float(hit["mcap_ratio"]) - 2.03) < 1e-6


def test_runnerwatch_stamps_outcome_liq_when_scanstate_is_zero():
    # Live ASS: ScanState Dex-miss wrote liq_now=0 while Outcome
    # last_liq was $35k and approaching already showed the book.
    # Overlay the real pool; do not drop a fat book from watch.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    mint = "0xrhasswatchliqnow000000000000000001"
    with session_scope() as session:
        tok = Token(mint=mint, symbol="ASSLIQ", chain="robinhood", source="rh_pons", first_seen_at=utcnow())
        tok.research = Research(p_good=0.72, holder_count=47, features_json="{}", risk_flags_json="[]")
        session.add(tok)
        session.flush()
        session.add(Outcome(token_id=tok.id, t0_mcap=17_250.0, max_mcap=35_019.0, multiple=2.03, last_liq=35_019.0))
        session.add(
            Snapshot(
                token_id=tok.id,
                kind="live",
                mcap_usd=35_019.0,
                liquidity_usd=35_019.0,
                volume_h1=8_000.0,
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps(
                    {
                        "runner_p": 0.63,
                        "mcap_ratio": 2.03,
                        "symbol": "ASSLIQ",
                        "chain": "robinhood",
                        "at": now,
                        "tier": "C",
                        "liq_now": 0,
                    }
                ),
            )
        )
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    hit = next(r for r in rh["items"] if r["mint"] == mint)
    assert float(hit["liq_now"]) == 35019
    conv = asyncio.run(conviction_board(chain="robinhood"))
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert mint not in conv_mints  # tier C is never shown on conviction


def test_runnerwatch_stamps_outcome_liq_over_stale_scanstate():
    # Live YOLO: ScanState still $3.7k while Outcome / approaching
    # was $45k. Overlay the real book, do not drop the climber.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    mint = "0xrhyolowatchliqstamp000000000000001"
    with session_scope() as session:
        tok = Token(mint=mint, symbol="YOLOLIQ", chain="robinhood", source="rh_pons", first_seen_at=utcnow())
        tok.research = Research(p_good=0.48, holder_count=1140, features_json="{}", risk_flags_json="[]")
        session.add(tok)
        session.flush()
        session.add(Outcome(token_id=tok.id, t0_mcap=63_516.0, max_mcap=178_711.0, multiple=2.81, last_liq=45_291.0))
        session.add(
            Snapshot(
                token_id=tok.id,
                kind="live",
                mcap_usd=178_000.0,
                liquidity_usd=45_291.0,
                volume_h1=20_000.0,
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps(
                    {
                        "runner_p": 0.67,
                        "mcap_ratio": 2.80,
                        "symbol": "YOLOLIQ",
                        "chain": "robinhood",
                        "at": now,
                        "tier": "C",
                        "liq_now": 3751,
                    }
                ),
            )
        )
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    hit = next(r for r in rh["items"] if r["mint"] == mint)
    assert float(hit["liq_now"]) == 45291


def test_runnerwatch_drops_copycat_and_start_high_rugs():
    # Live NTDA sat watch A 4.46× while approaching already dropped
    # instant-fill + same-ticker. PIG start-high rug same. A name
    # with only the same-ticker needle (nekomaru-class) stays.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    copycat = "SolNtdaCopycatWatchSkip111111111111"
    starthigh = "SolPigStartHighWatchSkip1111111111"
    stay = "SolFoneWatchStayNoFlags11111111111"
    ticker_only = "SolNekoTickerOnlyWatchStay1111111"

    def _row(session, mint, symbol, flags, t0, live, liq, holders, tier="B"):
        tok = Token(mint=mint, symbol=symbol, chain="sol", source="poll", first_seen_at=utcnow())
        tok.research = Research(
            p_good=0.60,
            holder_count=holders,
            features_json="{}",
            risk_flags_json=json.dumps(flags),
        )
        session.add(tok)
        session.flush()
        session.add(Outcome(token_id=tok.id, t0_mcap=t0, max_mcap=live, multiple=live / t0, last_liq=liq))
        session.add(Snapshot(token_id=tok.id, kind="live", mcap_usd=live, liquidity_usd=liq, volume_h1=8_000.0))
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": round(live / t0, 2),
                        "symbol": symbol,
                        "chain": "sol",
                        "at": now,
                        "tier": tier,
                        "liq_now": liq,
                    }
                ),
            )
        )

    with session_scope() as session:
        _row(
            session,
            copycat,
            "NTDASKIP",
            [
                "Bonding curve filled almost instantly (bundle risk)",
                "Same ticker launched repeatedly in 24h (copycat spam)",
            ],
            227_217.0,
            1_055_371.0,
            100_919.0,
            130,
            "A",
        )
        _row(
            session,
            starthigh,
            "PIGSKIP",
            ["Already dumped below graduation mcap (start-high rug pattern)"],
            69_000.0,
            163_085.0,
            35_439.0,
            370,
        )
        _row(session, stay, "FONESTAY", [], 78_581.0, 172_000.0, 33_400.0, 24)
        _row(
            session,
            ticker_only,
            "NEKOSTAY",
            ["Same ticker launched repeatedly in 24h (copycat spam)"],
            69_000.0,
            151_800.0,
            40_000.0,
            80,
        )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    mints = {r["mint"] for r in sol["items"]}
    assert copycat not in mints
    assert starthigh not in mints
    assert stay in mints
    assert ticker_only in mints
    conv = asyncio.run(conviction_board(chain="sol"))
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert copycat not in conv_mints
    assert starthigh not in conv_mints
    assert stay in conv_mints
    assert ticker_only in conv_mints


def test_runnerwatch_drops_prepumped_huge_t0():
    # Live Dancedoge: t0 $8.5M / 11 wallets / 2.3× sat watch C $368k.
    # Approaching already drops is_prepumped_entry; flag needles miss it.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    dumped = "SolDancePrepumpWatchSkip1111111111111"
    stay = "SolFoneHonestWatchStay11111111111111"
    with session_scope() as session:
        for mint, symbol, t0, live, liq, holders, tier in (
            (dumped, "DANCEPRE", 8_499_399.0, 19_539_764.0, 367_726.0, 80, "C"),
            (stay, "FONESTAY2", 78_581.0, 172_000.0, 32_571.0, 24, "B"),
        ):
            tok = Token(mint=mint, symbol=symbol, chain="sol", source="poll", first_seen_at=utcnow())
            tok.research = Research(p_good=0.57, holder_count=holders, features_json="{}", risk_flags_json="[]")
            session.add(tok)
            session.flush()
            session.add(Outcome(token_id=tok.id, t0_mcap=t0, max_mcap=live, multiple=live / t0, last_liq=liq))
            session.add(Snapshot(token_id=tok.id, kind="live", mcap_usd=live, liquidity_usd=liq, volume_h1=8_000.0))
            session.add(
                ScanState(
                    key=f"runnerp:{mint}",
                    value=json.dumps(
                        {
                            "runner_p": 0.87,
                            "mcap_ratio": round(live / t0, 2),
                            "symbol": symbol,
                            "chain": "sol",
                            "at": now,
                            "tier": tier,
                            "liq_now": liq,
                        }
                    ),
                )
            )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    mints = {r["mint"] for r in sol["items"]}
    assert dumped not in mints
    assert stay in mints
    conv = asyncio.run(conviction_board(chain="sol"))
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert dumped not in conv_mints
    assert stay in conv_mints


def test_runner_watch_write_drops_prepumped_huge_t0():
    import asyncio
    import json

    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow
    from launchfinder.scoring.outcomes import _runner_watch

    init_db()
    mint = "SolDancePrepumpWriteDrop1111111111111"
    with session_scope() as session:
        tok = Token(mint=mint, symbol="DANCEWR", chain="sol", source="websocket", first_seen_at=utcnow())
        tok.research = Research(p_good=0.08, holder_count=80, features_json="{}", risk_flags_json="[]")
        session.add(tok)
        session.flush()
        session.add(
            Outcome(token_id=tok.id, t0_mcap=8_499_399.0, max_mcap=19_539_764.0, multiple=2.30, last_liq=367_726.0)
        )
        session.add(
            ScanState(
                key=f"runnerp:{mint}",
                value=json.dumps(
                    {"runner_p": 0.87, "mcap_ratio": 2.3, "symbol": "DANCEWR", "chain": "sol", "tier": "C"}
                ),
            )
        )
        session.flush()
        asyncio.run(
            _runner_watch(
                session,
                tok,
                session.query(Outcome).filter(Outcome.token_id == tok.id).one(),
                {"mcap_usd": 19_539_764.0, "liquidity_usd": 367_726.0, "volume_h1": 20_000.0},
            )
        )
        assert session.query(ScanState).filter(ScanState.key == f"runnerp:{mint}").one_or_none() is None


def test_conviction_drops_dumped_low_liq_books():
    # Live ANGRYCATS $2.1k sat conviction B after the dump.
    # Watch still lists it; conviction needs a real pool.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    dumped = "SolAngryDumpConvSkip11111111111111"
    stay = "SolPumpooorConvStay111111111111111"
    with session_scope() as session:
        for mint, symbol, t0, live, liq, holders, tier in (
            (dumped, "ANGRYDUMP", 39_889.0, 121_300.0, 2_150.0, 93, "B"),
            (stay, "PUMPSTAY", 69_000.0, 241_500.0, 75_838.0, 80, "B"),
        ):
            tok = Token(mint=mint, symbol=symbol, chain="sol", source="poll", first_seen_at=utcnow())
            tok.research = Research(p_good=0.60, holder_count=holders, features_json="{}", risk_flags_json="[]")
            session.add(tok)
            session.flush()
            session.add(Outcome(token_id=tok.id, t0_mcap=t0, max_mcap=live, multiple=live / t0, last_liq=liq))
            session.add(Snapshot(token_id=tok.id, kind="live", mcap_usd=live, liquidity_usd=liq, volume_h1=8_000.0))
            session.add(
                ScanState(
                    key=f"runnerp:{mint}",
                    value=json.dumps(
                        {
                            "runner_p": 0.7,
                            "mcap_ratio": round(live / t0, 2),
                            "symbol": symbol,
                            "chain": "sol",
                            "at": now,
                            "tier": tier,
                            "liq_now": liq,
                        }
                    ),
                )
            )
    watch = asyncio.run(runner_watch_board(chain="sol"))
    watch_mints = {r["mint"] for r in watch["items"]}
    assert dumped in watch_mints
    assert stay in watch_mints
    conv = asyncio.run(conviction_board(chain="sol"))
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert dumped not in conv_mints
    assert stay in conv_mints


def test_runnerwatch_drops_rh_airdrop_climb():
    # Live Ignoring: 1131 wallets / $3.3k / 3.23× sat watch B and
    # approaching. Hunt already buries airdrop tape.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import approaching, conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    airdrop = "0xrhignairdropwatch00000000000000001"
    stay = "0xrhvacstayairdrop00000000000000001"
    with session_scope() as session:
        for mint, symbol, holders, t0, live, liq, tier in (
            (airdrop, "IGNORING", 1131, 10_400.0, 33_600.0, 3_354.0, "B"),
            (stay, "VACSTAY", 44, 24_380.0, 51_700.0, 45_037.0, "A"),
        ):
            tok = Token(mint=mint, symbol=symbol, chain="robinhood", source="rh_pons", first_seen_at=utcnow())
            tok.research = Research(p_good=0.71, holder_count=holders, features_json="{}", risk_flags_json="[]")
            session.add(tok)
            session.flush()
            session.add(Outcome(token_id=tok.id, t0_mcap=t0, max_mcap=live, multiple=live / t0, last_liq=liq))
            session.add(Snapshot(token_id=tok.id, kind="live", mcap_usd=live, liquidity_usd=liq, volume_h1=8_000.0))
            session.add(
                ScanState(
                    key=f"runnerp:{mint}",
                    value=json.dumps(
                        {
                            "runner_p": 0.99,
                            "mcap_ratio": round(live / t0, 2),
                            "symbol": symbol,
                            "chain": "robinhood",
                            "at": now,
                            "tier": tier,
                            "liq_now": liq,
                        }
                    ),
                )
            )
    rh = asyncio.run(runner_watch_board(chain="robinhood"))
    mints = {r["mint"] for r in rh["items"]}
    assert airdrop not in mints
    assert stay in mints
    ap = asyncio.run(approaching(chain="robinhood"))
    ap_mints = {r["mint"] for r in ap["items"]}
    assert airdrop not in ap_mints
    assert stay in ap_mints
    conv = asyncio.run(conviction_board(chain="robinhood"))
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert airdrop not in conv_mints
    assert stay in conv_mints


def test_runnerwatch_drops_sol_airdrop_climb():
    # Live 12:38: Dark Arena 291w / $3.0k / 3.43× sat Sol
    # approaching + runnerwatch. Hunt already buries airdrop tape.
    # PUMPLESS 30w / $46k stays. PSYOPED $400 this-window stays.
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone

    from launchfinder.app import approaching, conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    airdrop = "SolDarkArenaAirdropWatchMint1111111"
    stay = "SolPumplessStayAirdropMint11111111"
    with session_scope() as session:
        aged = utcnow()
        for mint, symbol, holders, t0, live, liq, tier, age in (
            (airdrop, "DARKAIR", 291, 34_295.0, 117_562.0, 3_014.0, "B", timedelta(hours=3, minutes=30)),
            (stay, "PUMPSTAY", 30, 103_740.0, 302_798.0, 45_933.0, "A", timedelta(minutes=20)),
        ):
            tok = Token(
                mint=mint,
                symbol=symbol,
                chain="sol",
                source="poll",
                first_seen_at=aged - age,
                created_at_chain=aged - age,
            )
            tok.research = Research(p_good=0.33, holder_count=holders, features_json="{}", risk_flags_json="[]")
            session.add(tok)
            session.flush()
            session.add(Outcome(token_id=tok.id, t0_mcap=t0, max_mcap=live, multiple=live / t0, last_liq=liq))
            session.add(Snapshot(token_id=tok.id, kind="live", mcap_usd=live, liquidity_usd=liq, volume_h1=8_000.0))
            session.add(
                ScanState(
                    key=f"runnerp:{mint}",
                    value=json.dumps(
                        {
                            "runner_p": 0.99,
                            "mcap_ratio": round(live / t0, 2),
                            "symbol": symbol,
                            "chain": "sol",
                            "at": now,
                            "tier": tier,
                            "liq_now": liq,
                        }
                    ),
                )
            )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    mints = {r["mint"] for r in sol["items"]}
    assert airdrop not in mints
    assert stay in mints
    ap = asyncio.run(approaching(chain="sol"))
    ap_mints = {r["mint"] for r in ap["items"]}
    assert airdrop not in ap_mints
    assert stay in ap_mints
    conv = asyncio.run(conviction_board(chain="sol"))
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert airdrop not in conv_mints
    assert stay in conv_mints


def test_runnerwatch_drops_sol_sub25_airdrop_tape():
    # Live 13:08: Birkin 2.04× / 578w / $19k sat Sol approaching #2.
    # Hunt already buries airdrop tape ($33/w). Sat-dust needs <$8k
    # so $19k missed. PUMPLESS 30w / $46k / 2.92× stays. Bunny
    # 4.98× and SUNLIVE 3.70× stay (multiple ≥ 2.5).
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone

    from launchfinder.app import approaching, conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    airdrop = "SolBirkinAirdropWatchMint111111111"
    stay = "SolPumplessFatStayMint11111111111"
    with session_scope() as session:
        aged = utcnow()
        for mint, symbol, holders, t0, live, liq, tier, age in (
            (airdrop, "BIRKAIR", 578, 38_900.0, 79_356.0, 19_147.0, "B", timedelta(minutes=29)),
            (stay, "PUMPFAT", 30, 103_740.0, 302_798.0, 45_933.0, "A", timedelta(hours=5, minutes=9)),
        ):
            tok = Token(
                mint=mint,
                symbol=symbol,
                chain="sol",
                source="poll",
                first_seen_at=aged - age,
                created_at_chain=aged - age,
            )
            tok.research = Research(p_good=0.33, holder_count=holders, features_json="{}", risk_flags_json="[]")
            session.add(tok)
            session.flush()
            session.add(Outcome(token_id=tok.id, t0_mcap=t0, max_mcap=live, multiple=live / t0, last_liq=liq))
            session.add(Snapshot(token_id=tok.id, kind="live", mcap_usd=live, liquidity_usd=liq, volume_h1=8_000.0))
            session.add(
                ScanState(
                    key=f"runnerp:{mint}",
                    value=json.dumps(
                        {
                            "runner_p": 0.99,
                            "mcap_ratio": round(live / t0, 2),
                            "symbol": symbol,
                            "chain": "sol",
                            "at": now,
                            "tier": tier,
                            "liq_now": liq,
                        }
                    ),
                )
            )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    mints = {r["mint"] for r in sol["items"]}
    assert airdrop not in mints
    assert stay in mints
    ap = asyncio.run(approaching(chain="sol"))
    ap_mints = {r["mint"] for r in ap["items"]}
    assert airdrop not in ap_mints
    assert stay in ap_mints
    conv = asyncio.run(conviction_board(chain="sol"))
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert airdrop not in conv_mints
    assert stay in conv_mints


def test_runnerwatch_drops_8h_skinny_dumps():
    # Live 08:40: ☉ 13.9h / 3.70× / $3.2k sat conviction B after
    # approaching already dropped skinny dumps. Live 11:11:
    # TriplePONS 4.9h / $4.2k sat runnerwatch #7. PSYOPED $400
    # this-window and a fat 8h+ climb stay. Do not restore ☉ hunt.
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone

    from launchfinder.app import approaching, conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc)
    dumped = "SolSunSkinnyConvDumpMint11111111111"
    mid = "SolPsyopedWatchThisWindowMint11111"
    fat = "SolMvcFatEightHWatchMint1111111111"
    with session_scope() as session:
        early = "TriplePonsFourHWatchMint1111111111"
        for mint, symbol, holders, t0, live, liq, age, tier in (
            (dumped, "SUNDUMP", 762, 38_854.0, 143_762.0, 3_165.0, timedelta(hours=13, minutes=50), "B"),
            (early, "TP4H", 51, 49_350.0, 118_443.0, 4_275.87, timedelta(hours=4, minutes=55), "B"),
            (mid, "PSYKEEP", 347, 35_336.0, 173_500.0, 400.0, timedelta(minutes=40), "C"),
            (fat, "MVC8H", 192, 40_854.0, 144_802.0, 26_791.0, timedelta(hours=11, minutes=20), "B"),
        ):
            tok = Token(
                mint=mint,
                symbol=symbol,
                chain="sol",
                source="poll",
                created_at_chain=utcnow() - age,
                first_seen_at=utcnow() - age,
            )
            tok.research = Research(p_good=0.72, holder_count=holders, features_json="{}", risk_flags_json="[]")
            session.add(tok)
            session.flush()
            session.add(Outcome(token_id=tok.id, t0_mcap=t0, max_mcap=live, multiple=live / t0, last_liq=liq))
            session.add(Snapshot(token_id=tok.id, kind="live", mcap_usd=live, liquidity_usd=liq, volume_h1=8_000.0))
            session.add(
                ScanState(
                    key=f"runnerp:{mint}",
                    value=json.dumps(
                        {
                            "runner_p": 1.0,
                            "mcap_ratio": round(live / t0, 2),
                            "symbol": symbol,
                            "chain": "sol",
                            "at": now.isoformat(),
                            "tier": tier,
                            "liq_now": liq,
                        }
                    ),
                )
            )
    watch = asyncio.run(runner_watch_board(chain="sol"))
    watch_mints = {r["mint"] for r in watch["items"]}
    assert dumped not in watch_mints
    assert early not in watch_mints
    assert mid in watch_mints
    assert fat in watch_mints
    ap = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    ap_mints = {r["mint"] for r in ap["items"]} | {r["mint"] for r in ap["thin"]}
    assert dumped not in ap_mints
    assert early not in ap_mints
    assert mid in ap_mints
    assert fat in ap_mints
    conv = asyncio.run(conviction_board(chain="sol"))
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert dumped not in conv_mints
    assert early not in conv_mints
    assert fat in conv_mints


def test_runnerwatch_drops_stale_sub_three_x():
    # Live 11:21: MONK 17.7h / 2.69× sat runnerwatch + conviction B.
    # An 8h+ book still under 3× is leftover tape. This-window 2×
    # stays. Honest 3×+ this-window stays.
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone

    from launchfinder.app import approaching, conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Snapshot, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc)
    stale = "MonkStaleWatchSubThreeMint1111111"
    young = "BenWatchThisWindowTwoXMint111111"
    honest = "OhOhSevenWatchThreeXMint11111111"
    with session_scope() as session:
        for mint, symbol, holders, t0, live, liq, age, tier, chain in (
            (stale, "MONK8W", 102, 131_700.0, 354_271.0, 19_658.04, timedelta(hours=17, minutes=40), "B", "sol"),
            (young, "BEN2W", 345, 51_515.0, 193_338.0, 42_281.84, timedelta(hours=2, minutes=40), "B", "sol"),
            (honest, "HON3W", 578, 55_591.0, 170_631.0, 19_071.36, timedelta(hours=5, minutes=40), "B", "robinhood"),
        ):
            tok = Token(
                mint=mint,
                symbol=symbol,
                chain=chain,
                source="poll" if chain == "sol" else "rh_pons",
                created_at_chain=utcnow() - age,
                first_seen_at=utcnow() - age,
            )
            tok.research = Research(p_good=0.84, holder_count=holders, features_json="{}", risk_flags_json="[]")
            session.add(tok)
            session.flush()
            session.add(Outcome(token_id=tok.id, t0_mcap=t0, max_mcap=live, multiple=live / t0, last_liq=liq))
            session.add(Snapshot(token_id=tok.id, kind="live", mcap_usd=live, liquidity_usd=liq, volume_h1=8_000.0))
            session.add(
                ScanState(
                    key=f"runnerp:{mint}",
                    value=json.dumps(
                        {
                            "runner_p": 1.0,
                            "mcap_ratio": round(live / t0, 2),
                            "symbol": symbol,
                            "chain": chain,
                            "at": now.isoformat(),
                            "tier": tier,
                            "liq_now": liq,
                        }
                    ),
                )
            )
    watch = asyncio.run(runner_watch_board(chain="sol"))
    watch_mints = {r["mint"] for r in watch["items"]}
    assert stale not in watch_mints
    assert young in watch_mints
    ap = asyncio.run(approaching(min_multiple=2.0, chain="sol"))
    ap_mints = {r["mint"] for r in ap["items"]} | {r["mint"] for r in ap["thin"]}
    assert stale not in ap_mints
    assert young in ap_mints
    conv = asyncio.run(conviction_board(chain="sol"))
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert stale not in conv_mints
    rh_watch = asyncio.run(runner_watch_board(chain="robinhood"))
    rh_mints = {r["mint"] for r in rh_watch["items"]}
    assert honest in rh_mints


def test_runnerwatch_ranks_fat_books_above_skinny():
    # Live ANGRYCATS $2.1k (runner_p 1.0) sat above Pumpooor $76k (0.6).
    # Fat books first — the desk hunts 5-50x.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import ScanState

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    skinny = "SolSkinnyWatchSortLow111111111111"
    fat = "SolFatWatchSortHigh11111111111111"
    with session_scope() as session:
        session.add(
            ScanState(
                key=f"runnerp:{skinny}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 3.04,
                        "symbol": "ANGRYSORT",
                        "chain": "sol",
                        "at": now,
                        "tier": "B",
                        "liq_now": 2124,
                    }
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{fat}",
                value=json.dumps(
                    {
                        "runner_p": 0.6,
                        "mcap_ratio": 3.50,
                        "symbol": "PUMPSORT",
                        "chain": "sol",
                        "at": now,
                        "tier": "C",
                        "liq_now": 75838,
                    }
                ),
            )
        )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    order = [r["mint"] for r in sol["items"] if r["mint"] in {skinny, fat}]
    assert order[0] == fat
    assert skinny in order


def test_runnerwatch_ranks_crowded_above_approaching_thin():
    # Live 20:40: BWA 9w / $28k sat #8 above ROBBIE 30w. Hunt and
    # approaching already use the 15-wallet Sol line. Stay on watch
    # (runner floor is still 8) — just sort below crowded fat books.
    import asyncio
    import json
    from datetime import datetime, timezone

    from launchfinder.app import runner_watch_board
    from launchfinder.db import SessionLocal, init_db
    from launchfinder.models import Research, ScanState, Token, utcnow

    init_db()
    session = SessionLocal()
    now = utcnow()
    thin_mint = "SolBwaThinWatchSort1111111111111111"
    crowd_mint = "SolRobbieCrowdWatchSort111111111111"
    thin = Token(
        mint=thin_mint,
        symbol="BWASORT",
        chain="sol",
        source="poll",
        first_seen_at=now,
        is_historical=False,
    )
    thin.research = Research(p_good=0.069, holder_count=9, features_json="{}")
    crowd = Token(
        mint=crowd_mint,
        symbol="ROBBSORT",
        chain="sol",
        source="poll",
        first_seen_at=now,
        is_historical=False,
    )
    crowd.research = Research(p_good=0.218, holder_count=30, features_json="{}")
    session.add_all([thin, crowd])
    stamp = datetime.now(timezone.utc).isoformat()
    session.add(
        ScanState(
            key=f"runnerp:{thin_mint}",
            value=json.dumps(
                {
                    "runner_p": 0.90,
                    "mcap_ratio": 2.89,
                    "symbol": "BWASORT",
                    "chain": "sol",
                    "at": stamp,
                    "tier": "C",
                    "liq_now": 28723,
                }
            ),
        )
    )
    session.add(
        ScanState(
            key=f"runnerp:{crowd_mint}",
            value=json.dumps(
                {
                    "runner_p": 0.60,
                    "mcap_ratio": 2.22,
                    "symbol": "ROBBSORT",
                    "chain": "sol",
                    "at": stamp,
                    "tier": "C",
                    "liq_now": 23265,
                }
            ),
        )
    )
    session.commit()
    session.close()

    sol = asyncio.run(runner_watch_board(chain="sol"))
    order = [r["symbol"] for r in sol["items"] if r["symbol"] in {"BWASORT", "ROBBSORT"}]
    assert order[0] == "ROBBSORT"
    assert "BWASORT" in order


def test_runnerwatch_keeps_live_climber_when_assessment_at_is_old():
    # Live 05:40: ☉ / MONK / draw sat approaching A (3.7 / 2.69 / 2.05)
    # with ScanState `at` ~10.5h old. A 12h cut on `at` drops them
    # around 07:05 even though Outcome is live. Day-old ScanState
    # without a live book still expires.
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone

    from launchfinder.app import conviction_board, runner_watch_board
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import Outcome, Research, ScanState, Token, utcnow

    init_db()
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=13)).isoformat()
    live_mint = "SolSunLiveOldAtMint111111111111111111"
    dead_mint = "SolDeadOldAtMint11111111111111111111"
    with session_scope() as session:
        live = Token(
            mint=live_mint,
            symbol="SUNLIVE",
            chain="sol",
            source="poll",
            first_seen_at=utcnow(),
        )
        live.research = Research(
            p_good=0.72, holder_count=762, features_json="{}", risk_flags_json="[]"
        )
        session.add(live)
        session.flush()
        session.add(
            Outcome(
                token_id=live.id,
                t0_mcap=20_000.0,
                max_mcap=74_000.0,
                multiple=3.70,
                last_liq=30_715.0,
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{live_mint}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 3.70,
                        "symbol": "SUNLIVE",
                        "chain": "sol",
                        "at": stale,
                        "tier": "A",
                        "liq_now": 30715,
                    }
                ),
            )
        )
        session.add(
            ScanState(
                key=f"runnerp:{dead_mint}",
                value=json.dumps(
                    {
                        "runner_p": 1.0,
                        "mcap_ratio": 2.69,
                        "symbol": "DEADAT",
                        "chain": "sol",
                        "at": stale,
                        "tier": "A",
                        "liq_now": 51217,
                    }
                ),
            )
        )
    sol = asyncio.run(runner_watch_board(chain="sol"))
    conv = asyncio.run(conviction_board(chain="sol"))
    watch_mints = {r["mint"] for r in sol["items"]}
    conv_mints = {r["mint"] for r in conv["tier_a"] + conv["tier_b"]}
    assert live_mint in watch_mints
    assert live_mint in conv_mints
    assert dead_mint not in watch_mints
    assert dead_mint not in conv_mints
