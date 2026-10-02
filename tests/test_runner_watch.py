from launchfinder.scoring.outcomes import detect_second_leg, runner_potential_score


def test_second_leg_shape_detection():
    # dip below entry, then recovery above both trough and entry
    assert detect_second_leg([(0, 69_000), (0, 45_000), (0, 52_000), (0, 88_000)])
    # straight fade: no second leg
    assert not detect_second_leg([(0, 69_000), (0, 50_000), (0, 30_000), (0, 12_000)])
    # straight pump with no dip: strong, but not the dip-recovery shape
    assert not detect_second_leg([(0, 69_000), (0, 90_000), (0, 140_000)])
    # too little history
    assert not detect_second_leg([(0, 69_000), (0, 88_000)])


def test_second_leg_raises_runner_score():
    base = dict(mcap_ratio=1.2, liq_ratio=1.1, liq_now=20_000, vol_now=25_000, entry_p=0.5)
    assert runner_potential_score(**base, second_leg=True) > runner_potential_score(**base)


def test_second_leg_scores_high():
    # Holding 1.6x above entry at 1h with deepening liquidity and volume:
    # the classic second-leg profile.
    p = runner_potential_score(
        mcap_ratio=1.6, liq_ratio=1.5, liq_now=40_000, vol_now=60_000, entry_p=0.7, smart_delta=2
    )
    assert p >= 0.85


def test_faded_token_scores_low():
    # Below 0.7x entry with thinning pool: the run is over before it began.
    p = runner_potential_score(
        mcap_ratio=0.55, liq_ratio=0.6, liq_now=2_000, vol_now=1_000, entry_p=0.7
    )
    assert p <= 0.2


def test_smart_money_exit_hurts():
    base = dict(mcap_ratio=1.2, liq_ratio=1.1, liq_now=20_000, vol_now=25_000, entry_p=0.6)
    holding = runner_potential_score(**base, smart_delta=0)
    exiting = runner_potential_score(**base, smart_delta=-3)
    adding = runner_potential_score(**base, smart_delta=3)
    assert exiting < holding < adding


def test_entry_score_matters_but_does_not_dominate():
    strong_junk = runner_potential_score(
        mcap_ratio=0.5, liq_ratio=0.5, liq_now=1_000, vol_now=500, entry_p=0.95
    )
    weak_runner = runner_potential_score(
        mcap_ratio=1.7, liq_ratio=1.6, liq_now=50_000, vol_now=80_000, entry_p=0.3
    )
    assert weak_runner > strong_junk
