from launchfinder.scoring.paper_gate import (
    PAPER_CHASE_FRESH_MULT,
    PAPER_CHASE_MULT,
    PAPER_CHASE_QUALITY_MULT,
    PAPER_ENRICH_VETO,
    PAPER_FIRST_BOOK_GRACE_MIN,
    PAPER_LATE_OK_KEY,
    PAPER_LATE_OK_WHY_KEY,
    PAPER_NO_RUN_HOURS,
    PAPER_PROVISIONAL_KEY,
    paper_chase_quality,
    paper_entry_p,
    paper_fill_verdict,
    paper_hard_veto,
    paper_is_chase,
    paper_is_fresh_fat,
    paper_live_dump,
    paper_no_run_exit,
    paper_tape_dump,
    paper_window_hours,
    research_is_provisional,
    stamp_paper_late_ok,
    stamp_paper_provisional,
)


def test_paper_hard_veto_stops_brand_and_copycat():
    assert paper_hard_veto(["Same ticker launched repeatedly in 24h (copycat spam)"])
    assert paper_hard_veto(["Claimed X account never tweets this ticker (likely hijacked)"])
    assert paper_hard_veto(["Claimed celebrity/brand X — not this launch"])
    assert paper_hard_veto(["Creator was funded by a wallet behind prior rugs"])
    assert paper_hard_veto(["Project X is a pasted tweet on a generated website"])
    assert paper_hard_veto(
        [],
        twitter="https://x.com/AragornSol/status/2099235852543315992",
        website="https://twine.auction/a/twin-zwjg",
    )
    assert paper_hard_veto(
        [],
        twitter="https://x.com/YoungThugDevvor/status/2099263978467725392",
        website="https://otcdesks.cash/coin/CJ7oyJJ4pYLV9Phkkyp2yhUZ87QUqS6GdsWuV4ytpump",
    )
    assert not paper_hard_veto(
        [],
        twitter="https://x.com/Bymotionn/status/2099168136456991013",
        website="https://www.reddit.com/r/ninathemonkey/",
    )
    assert paper_hard_veto([], website="https://gemini.google.com/app")
    assert paper_hard_veto([], twitter_followers=573_300, twitter_age_days=808, twitter_verified=True)
    assert not paper_hard_veto(
        ["Heavy sniper presence at launch (20+)"],
        website="https://www.reddit.com/r/ninathemonkey/",
        twitter_handle="ninathemonkey",
        twitter_followers=1162,
        twitter_age_days=626,
        twitter_verified=True,
    )
    # Live Metapad: 8y+ project X on a new launch — bought handle.
    assert paper_hard_veto(
        [],
        twitter_handle="metapadspace",
        twitter_age_days=3565.3,
        twitter_followers=188,
        twitter_verified=True,
    )
    assert (
        paper_fill_verdict(
            entry_mcap=80_000.0,
            last_mcap=109_000.0,
            last_liq=31_000.0,
            twitter_handle="metapadspace",
            twitter_age_days=3565.3,
            twitter_followers=188,
            twitter_verified=True,
        )
        == "fail"
    )
    # Personal Dev X only (display_token_x emptied the handle).
    assert not paper_hard_veto([], twitter_handle="", twitter_age_days=5239)
    # Official NASA is a different veto — not the bought-aged tell.
    from launchfinder.research.twitter import is_bought_aged_token_x

    assert is_bought_aged_token_x("NASA", 6843) is False
    assert is_bought_aged_token_x("amemecoinrh", 0.02) is False


def test_robinhood_paper_skips_start_high_and_prepumped_only():
    """Sol still vetoes the late book. RH books it. Late chase stays both."""
    assert paper_hard_veto(["start-high rug"], chain="sol") == "start-high"
    assert paper_hard_veto(["start-high rug"]) == "start-high"
    assert paper_hard_veto(["start-high rug"], chain="robinhood") == ""
    assert paper_hard_veto(["pre-pumped"], chain="sol") == "pre-pumped"
    assert paper_hard_veto(["pre-pumped"], chain="robinhood") == ""
    assert paper_hard_veto(["prepumped"], chain="robinhood") == ""
    assert paper_hard_veto(["Creator was funded by a wallet behind prior rugs"], chain="robinhood") == "prior rugs"
    assert (
        paper_fill_verdict(
            entry_mcap=80_000.0,
            last_mcap=90_000.0,
            last_liq=20_000.0,
            chain="robinhood",
            flags=["Late Dex catch-up on a real book (start-high t0)"],
        )
        == "pass"
    )
    assert (
        paper_fill_verdict(
            entry_mcap=80_000.0,
            last_mcap=90_000.0,
            last_liq=20_000.0,
            chain="sol",
            flags=["pre-pumped"],
        )
        == "fail"
    )
    # 2.5× is under the 10× floor — RH start-high skip does not invent a chase.
    assert (
        paper_fill_verdict(
            entry_mcap=80_000.0,
            last_mcap=200_000.0,
            last_liq=20_000.0,
            chain="robinhood",
            flags=["start-high rug"],
        )
        == "pass"
    )
    assert (
        paper_fill_verdict(
            entry_mcap=80_000.0,
            last_mcap=80_000.0 * 11,
            last_liq=20_000.0,
            chain="robinhood",
            flags=["start-high rug"],
        )
        == "late chase"
    )


def test_paper_fill_is_instant():
    assert paper_fill_verdict(entry_mcap=80_000.0, last_mcap=90_000.0, last_liq=20_000.0) == "pass"
    assert paper_fill_verdict(entry_mcap=80_000.0, last_mcap=60_000.0, last_liq=20_000.0) == "fail"
    assert paper_fill_verdict(entry_mcap=80_000.0, last_mcap=90_000.0, last_liq=800.0) == "fail"
    assert paper_tape_dump(["First-hour tape is dumping on real volume"])
    assert (
        paper_fill_verdict(
            entry_mcap=80_000.0,
            last_mcap=90_000.0,
            last_liq=20_000.0,
            flags=["Sellers already dominate"],
        )
        == "fail"
    )
    # Young liquid 90+ fills now — no t15m hold.
    assert paper_fill_verdict(entry_mcap=70_000.0, last_mcap=70_000.0, last_liq=16_000.0) == "pass"
    assert (
        paper_fill_verdict(
            entry_mcap=80_000.0,
            last_mcap=80_000.0,
            last_liq=20_000.0,
            flags=["Same ticker launched repeatedly in 24h (copycat spam)"],
        )
        == "fail"
    )
    # Live Faucet: Dex catch-up, not a 90+ graduation book.
    assert (
        paper_fill_verdict(
            entry_mcap=1_966_799.0,
            last_mcap=1_966_799.0,
            last_liq=80_000.0,
            flags=["Late Dex catch-up on a real book (start-high t0)"],
        )
        == "fail"
    )
    assert PAPER_FIRST_BOOK_GRACE_MIN == 20.0
    assert paper_window_hours("sol") == 18.0
    assert paper_window_hours("robinhood") == 12.0
    assert paper_entry_p(0.92, 0.85, 0.50) == 0.92
    assert paper_entry_p(0.0, 0.85, 0.92) == 0.85
    assert paper_entry_p(0.0, 0.0, 0.94) == 0.94


def test_paper_skips_two_tick_and_late_chase_but_takes_fresh_fat():
    assert PAPER_CHASE_MULT == 10.0
    assert PAPER_CHASE_FRESH_MULT == 1.8
    assert PAPER_CHASE_QUALITY_MULT == 0.0
    assert paper_is_chase(27_802, 1_797_918)
    assert not paper_is_chase(80_000, 90_000)
    assert not paper_is_chase(57_000, 124_000)
    assert (
        paper_fill_verdict(
            entry_mcap=41_154.0,
            last_mcap=40_090.0,
            last_liq=16_000.0,
            max_mcap=84_648.0,
        )
        == "two-tick"
    )
    assert (
        paper_fill_verdict(
            entry_mcap=27_802.0,
            last_mcap=1_797_918.0,
            last_liq=138_000.0,
            max_mcap=2_223_088.0,
        )
        == "late chase"
    )
    assert paper_is_fresh_fat(entry_mcap=69_000.0, last_mcap=70_000.0, last_liq=25_000.0, holders=120)
    assert not paper_is_fresh_fat(entry_mcap=69_000.0, last_mcap=70_000.0, last_liq=25_000.0, holders=0)
    assert not paper_is_fresh_fat(entry_mcap=69_000.0, last_mcap=70_000.0, last_liq=25_000.0, holders=10)
    assert not paper_is_fresh_fat(entry_mcap=27_802.0, last_mcap=1_797_918.0, last_liq=138_000.0, holders=80)


def test_quality_bypasses_late_chase_thin_stays_on_the_10x_floor():
    """10× floor for everyone; quality skips the veto after a big run."""
    hard = {"github_auth_n": 0.9, "real_project": 1.0, "gmgn_cto": 0.0, "name_quality": 0.2}
    sapling_t0, sapling_last = 57_000.0, 124_000.0
    assert sapling_last / sapling_t0 < PAPER_CHASE_MULT
    q = paper_chase_quality(features=hard, live_p=0.2)
    assert q["ok"] and "hard_thesis" in q["why"] and q["bypass"] is True
    # Under the floor: pass even when thin / unknown.
    assert (
        paper_fill_verdict(
            entry_mcap=sapling_t0,
            last_mcap=sapling_last,
            last_liq=40_000.0,
        )
        == "pass"
    )
    # Tilcayo-class 63×: thin refuses; quality bypasses.
    assert (
        paper_fill_verdict(
            entry_mcap=27_802.0,
            last_mcap=1_797_918.0,
            last_liq=138_000.0,
            max_mcap=2_223_088.0,
        )
        == "late chase"
    )
    assert (
        paper_fill_verdict(
            entry_mcap=27_802.0,
            last_mcap=1_797_918.0,
            last_liq=138_000.0,
            max_mcap=2_223_088.0,
            features=hard,
        )
        == "pass"
    )
    assert paper_chase_quality(features={}, live_p=0.50)["ok"] is False
    assert paper_chase_quality(features={}, live_p=0.70)["ok"] is True
    assert (
        paper_fill_verdict(
            entry_mcap=27_802.0,
            last_mcap=1_797_918.0,
            last_liq=138_000.0,
            live_p=0.72,
        )
        == "pass"
    )
    stamped = stamp_paper_late_ok({}, why=["hard_thesis"], multiple=2.175)
    assert stamped[PAPER_LATE_OK_KEY] is True
    assert stamped[PAPER_LATE_OK_WHY_KEY] == ["hard_thesis"]
    thin = stamp_paper_provisional({}, True)
    assert research_is_provisional(thin) and thin[PAPER_PROVISIONAL_KEY] is True
    assert PAPER_ENRICH_VETO == "enrich veto"
    assert len(PAPER_ENRICH_VETO) <= 32


def test_paper_live_dump_keeps_the_same_cuts_off_the_hunt_board():
    """OAK: 2.95× then 0.15× with Live 0 dumps. A 0.40 Live or a 1.2× hold does not."""
    assert paper_live_dump(entry_mcap=25_712, peak_mcap=75_903, last_mcap=3_939, live_p=0.0)
    assert not paper_live_dump(entry_mcap=25_712, peak_mcap=75_903, last_mcap=3_939, live_p=0.40)
    assert not paper_live_dump(entry_mcap=70_000, peak_mcap=84_000, last_mcap=70_000, live_p=0.10)
    assert not paper_live_dump(entry_mcap=70_000, peak_mcap=525_000, last_mcap=9_800, live_p=None)


def test_paper_no_run_exit_closes_graves_not_runners():
    assert PAPER_NO_RUN_HOURS == 1.0
    # CATFLIX-class: never 1.5×, last already 0.01×, aged 1h.
    assert paper_no_run_exit(entry_mcap=164_986, peak_mcap=164_986, last_mcap=1_650, age_hours=1.0)
    # fomocoin-class: peaked 1.32× then died.
    assert paper_no_run_exit(entry_mcap=275_222, peak_mcap=363_293, last_mcap=2_752, age_hours=2.0)
    # Confirmation clock — 10-minute wick stays open.
    assert not paper_no_run_exit(entry_mcap=164_986, peak_mcap=164_986, last_mcap=1_650, age_hours=0.5)
    # Still-alive 1.2× book is not a grave.
    assert not paper_no_run_exit(entry_mcap=70_000, peak_mcap=84_000, last_mcap=70_000, age_hours=2.0)
    # KPORT-class runner is live dump, not no-run.
    assert not paper_no_run_exit(entry_mcap=70_000, peak_mcap=525_000, last_mcap=9_800, age_hours=2.0)
    # Missing last print does not close.
    assert not paper_no_run_exit(entry_mcap=70_000, peak_mcap=70_000, last_mcap=0, age_hours=3.0)
