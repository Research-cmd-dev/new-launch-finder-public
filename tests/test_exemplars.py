from launchfinder.scoring.exemplars import desk_caught, follow_lessons, load_exemplars, pons_first_day
from launchfinder.scoring.features import FEATURE_NAMES


def test_feature_names_untouched():
    assert len(FEATURE_NAMES) == 66


def test_pons_first_day_is_not_the_leftover_stamp():
    row = pons_first_day()
    day = row["first_day"]
    assert row["mint"] == "0x39dbed3a2bd333467115de45665cc57f813c4571"
    assert row["caught_by_desk"] is False
    assert day["fdv_open"] < 5_000
    assert day["multiple_close"] > 50
    assert "410" not in str(day["fdv_open"])
    lessons = follow_lessons()
    assert any("leftover" in line.lower() or "410" in line for line in lessons)


def test_desk_caught_basket_has_live_dump_winners():
    symbols = {row["symbol"] for row in desk_caught()}
    assert {"DEX", "MeiMei", "POOF", "PPAID", "SPONK"} <= symbols
    assert load_exemplars()["purpose"]
