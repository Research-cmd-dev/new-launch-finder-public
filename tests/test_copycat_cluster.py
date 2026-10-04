"""Copycat cluster path: birth is a feature, label is the path, buy stays off."""

from datetime import datetime, timedelta, timezone

from launchfinder.scoring.copycat_cluster import (
    CAPTURES,
    ENTERS_BUY_PATH,
    assign_birth,
    attach_label,
    beats_dumb_rules,
    cluster_key,
    cluster_winner,
    evaluate_time_split,
    freeze_clock,
    frozen_row,
    hard_stops,
    label_path,
    normalize_symbol,
)


T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def test_symbol_cluster_ignores_case_and_punctuation():
    assert normalize_symbol("WWW!") == normalize_symbol("www")
    assert cluster_key("sol", "Pepe") == "sol:pepe"
    assert cluster_key("sol", "") is None


def test_fourth_birth_is_a_feature_not_a_skip():
    earlier = [
        {"chain": "sol", "symbol": "pepe", "mint": f"m{i}", "seen_at": T0 + timedelta(minutes=i)}
        for i in range(3)
    ]
    birth = assign_birth(
        earlier,
        chain="sol",
        symbol="PEPE",
        mint="m4",
        seen_at=T0 + timedelta(minutes=10),
    )
    assert birth["ticker_birth_n"] == 4
    assert birth["first_mint"] == "m0"
    frozen = freeze_clock(
        birth,
        {"holders": 40, "liq": 8000, "top10_pct": 22, "dev_sold": False},
        migrated_at=T0 + timedelta(minutes=10),
    )
    assert frozen["ticker_birth_n"] == 4
    assert hard_stops(liq=frozen["liq"], dev_sold=False) == []


def test_future_clones_do_not_change_birth():
    earlier = [
        {"chain": "sol", "symbol": "pepe", "mint": "later", "seen_at": T0 + timedelta(hours=2)}
    ]
    birth = assign_birth(
        earlier, chain="sol", symbol="pepe", mint="first", seen_at=T0
    )
    assert birth["ticker_birth_n"] == 1


def test_path_label_keeps_production_gate_as_a_column():
    label = label_path(
        decision_price=1.0,
        price_15m=2.0,
        price_1h=6.0,
        worst_drawdown=-0.4,
        liq_1h=7000,
    )
    assert label["ret_1h"] == 5.0
    assert label["production_gate"] is True
    dud = label_path(
        decision_price=1.0,
        price_15m=0.8,
        price_1h=1.2,
        worst_drawdown=-0.5,
        liq_1h=100,
    )
    assert dud["production_gate"] is False


def test_cluster_winner_can_be_the_fourth():
    rows = []
    for n, mult in ((1, 1.1), (2, 0.4), (4, 8.0)):
        rows.append(
            {
                "mint": f"m{n}",
                "ticker_birth_n": n,
                "label": {"ret_1h": mult - 1.0},
            }
        )
    winner = cluster_winner(rows)
    assert winner["ticker_birth_n"] == 4
    assert winner["multiple_1h"] == 8.0


def test_capture_does_not_enter_buy_path():
    assert CAPTURES is True
    assert ENTERS_BUY_PATH is False
    row = frozen_row(
        chain="sol",
        symbol="www",
        mint="GAwh",
        seen_at=T0,
        migrated_at=T0,
        earlier=[],
        book={"holders": 30, "liq": 9000, "dev_sold": False},
        decision_price=1.0,
    )
    assert row["enters_buy_path"] is False
    assert row["paper_only"] is True
    assert row["ticker_birth_n"] == 1
    labeled = attach_label(
        row,
        decision_price=1.0,
        price_15m=3.0,
        price_1h=7.0,
        worst_drawdown=-0.2,
        liq_1h=9000,
    )
    assert labeled["ticker_birth_n"] == 1
    assert labeled["label"]["production_gate"] is True


def test_time_split_can_beat_skip_all_and_buy_first():
    rows = []
    # Train: first-of-name mostly fails; a later clean book hits.
    for i in range(8):
        at = T0 + timedelta(hours=i)
        first = frozen_row(
            chain="sol",
            symbol=f"name{i}",
            mint=f"a{i}",
            seen_at=at,
            migrated_at=at,
            earlier=[],
            book={"holders": 6, "liq": 1500, "top10_pct": 80, "bundler_pct": 40, "dev_sold": False},
            decision_price=1.0,
        )
        rows.append(
            attach_label(
                first,
                decision_price=1.0,
                price_15m=0.7,
                price_1h=0.5,
                worst_drawdown=-0.6,
                liq_1h=400,
            )
        )
        later_seen = at + timedelta(minutes=20)
        later = frozen_row(
            chain="sol",
            symbol=f"name{i}",
            mint=f"b{i}",
            seen_at=later_seen,
            migrated_at=later_seen,
            earlier=[{"chain": "sol", "symbol": f"name{i}", "mint": f"a{i}", "seen_at": at}],
            book={"holders": 55, "liq": 12000, "top10_pct": 18, "bundler_pct": 2, "dev_sold": False},
            decision_price=1.0,
        )
        assert later["ticker_birth_n"] == 2
        rows.append(
            attach_label(
                later,
                decision_price=1.0,
                price_15m=3.0,
                price_1h=6.0,
                worst_drawdown=-0.15,
                liq_1h=11000,
            )
        )
    report = evaluate_time_split(rows)
    assert report["enters_buy_path"] is False
    assert report["promote"] is False
    assert report["n_test"] > 0
    assert report["beats_dumb_rules"] is True
    assert beats_dumb_rules(
        model_precision=0.4,
        skip_all_precision=0.0,
        buy_first_precision=0.1,
    )
