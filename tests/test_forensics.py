from launchfinder.research.gmgn import analyze_holders
from launchfinder.scoring.outcomes import apply_holder_forensics


def _wallet(funder: str, sold: float = 0.0, holding: bool = True, **kw) -> dict:
    return {
        "address": f"W{funder}{sold}",
        "addr_type": 0,
        "native_transfer": {"address": funder},
        "sell_amount_percentage": sold,
        "end_holding_at": None if holding else 1_700_000_000,
        **kw,
    }


def test_organic_book_analysis():
    rows = [_wallet(f"Funder{i}", sold=0.1) for i in range(20)]
    rows.append({"addr_type": 2, "address": "pool"})  # excluded
    f = analyze_holders(rows)
    assert f["n"] == 20
    assert f["funding_cluster"] == 0.05  # every wallet funded separately
    assert f["funding_sources"] == 20
    assert f["diamond"] == 1.0
    assert f["early_exit"] == 0.0


def test_coordinated_cluster_detected():
    rows = [_wallet("MasterWallet", sold=0.95, holding=False) for _ in range(12)]
    rows += [_wallet(f"Funder{i}", sold=0.2) for i in range(8)]
    f = analyze_holders(rows)
    assert f["funding_cluster"] == 0.6
    assert f["early_exit"] == 0.6
    p, flags = apply_holder_forensics(0.8, f)
    assert p < 0.35
    assert any("coordinated cluster" in x for x in flags)
    assert any("fully exited" in x for x in flags)


def test_diamond_structure_boosts():
    rows = [_wallet(f"Funder{i}", sold=0.05) for i in range(20)]
    f = analyze_holders(rows)
    p, flags = apply_holder_forensics(0.6, f)
    assert p > 0.6
    assert any("diamond" in x.lower() for x in flags)


def test_sniper_retention():
    rows = [_wallet(f"F{i}", tags=["sniper"]) for i in range(4)]
    rows += [_wallet("F9", sold=1.0, holding=False, tags=["sniper"])]
    f = analyze_holders(rows)
    assert f["snipers"] == 5
    assert f["sniper_retention"] == 0.8


def test_empty_forensics_is_neutral():
    assert analyze_holders([]) == {}
    p, flags = apply_holder_forensics(0.7, {})
    assert p == 0.7 and flags == []
