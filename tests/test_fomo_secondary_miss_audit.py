"""Secondary union miss audit (stack-v192)."""

from __future__ import annotations

from launchfinder.research.fomo_coverage import _build_secondary_miss_audit


def _miss_item(mint: str, *, source: str = "graduated") -> dict:
    return {
        "mint": mint,
        "chain": "sol",
        "symbol": "RUN",
        "rank": 1,
        "mcap_usd": 100_000.0,
        "status": "miss",
        "on_desk": False,
        "board_kind": source,
    }


def test_union_miss_high_confidence_two_secondaries():
    m = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    grad = [_miss_item(m, source="graduated")]
    gmgn = [_miss_item(m, source="gmgn_trending")]
    audit = _build_secondary_miss_audit(
        trending_items=[],
        graduated_pack={"items": grad},
        gmgn_pack={"items": gmgn},
        dex_pack={"items": []},
        grad_desk=grad,
        gmgn_desk=gmgn,
        dex_desk=[],
        overlap_with_fomo={"fomo_trending": 0},
    )
    assert audit["union_miss"] == 1
    assert audit["high_confidence_miss_n"] == 1
    assert audit["misses"][0]["high_confidence_miss"] is True
    assert set(audit["misses"][0]["miss_sources"]) == {"graduated", "gmgn_trending"}


def test_high_confidence_one_secondary_and_fomo_trend_miss():
    m = "MintBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    grad = [_miss_item(m)]
    trend = [_miss_item(m)]
    trend[0]["board_kind"] = "trending"
    audit = _build_secondary_miss_audit(
        trending_items=trend,
        graduated_pack={"items": grad},
        gmgn_pack={"items": []},
        dex_pack={"items": []},
        grad_desk=grad,
        gmgn_desk=[],
        dex_desk=[],
        overlap_with_fomo={"fomo_trending": 1},
    )
    assert audit["high_confidence_miss_n"] == 1
    assert audit["misses"][0]["fomo_trending_miss"] is True


def test_skipped_gmgn_not_counted_in_union():
    m = "MintCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    grad = [_miss_item(m)]
    audit = _build_secondary_miss_audit(
        trending_items=[],
        graduated_pack={"items": grad},
        gmgn_pack={"items": [], "skipped": True},
        dex_pack={"items": []},
        grad_desk=grad,
        gmgn_desk=[],
        dex_desk=[],
        overlap_with_fomo={},
    )
    assert audit["counts_by_source"]["gmgn_trending"]["skipped"] == 1
    assert audit["union_board"] == 1


def test_fomo_mirror_unchanged_still_stale_only():
    from launchfinder.db import init_db, session_scope
    from launchfinder.models import ScanState
    from launchfinder.research.fomo_coverage import AUDIT_KEY
    from launchfinder.scoring.production_gate import _fomo_mirror_bar

    init_db()
    with session_scope() as session:
        row = session.query(ScanState).filter(ScanState.key == AUDIT_KEY).one_or_none()
        payload = '{"board_stale": false, "secondary_miss_audit": {"union_miss": 9}}'
        if row is None:
            session.add(ScanState(key=AUDIT_KEY, value=payload))
        else:
            row.value = payload
        session.commit()
        bar = _fomo_mirror_bar(session)
    assert bar["color"] == "green"
    assert bar["value"] == "fresh"
