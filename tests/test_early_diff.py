"""SI early-diff pipeline — paper-safe Learn/shadow modules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from launchfinder.db import init_db, session_scope
from launchfinder.models import Research, Snapshot, TapeBar, Token
from launchfinder.research.holder_curves import (
    append_top10_history,
    concentration_class,
    holder_concentration_curve,
)
from launchfinder.research.liq_behavior import liq_path_from_tape
from launchfinder.research.social_velocity import classify_social_ramp
from launchfinder.research.wallet_clusters import wallet_clusters
from launchfinder.scoring.early_diff import (
    ALERT_THRESHOLD,
    FIRST_HOUR_PASS_SCORE,
    divergence_score,
    early_diff_status,
    emit_threshold_alerts,
    load_findings,
    passed_first_hour_filter,
    prove_on_cohort,
    score_cohort_row,
    study_baselines,
    survivor_hour_rescores,
)


def test_findings_and_baselines_load():
    findings = load_findings()
    assert findings.get("paper_safe") is True
    assert findings.get("n_printers") == 2
    assert "PRIOR" in (findings.get("serial_policy") or "")
    base = study_baselines(findings)
    assert "rising_book_15m" in base
    assert "top10_pct" in base
    assert base["rising_book_15m"]["printer_median"] == 1.0
    assert base["top10_pct"]["dud_median"] > 80


def test_divergence_prefers_printer_shape():
    base = study_baselines()
    printerish = divergence_score(
        {
            "rising_book_15m": 1.0,
            "mcap_t0_to_early_ratio": 8.0,
            "bar_mcap_15m_ratio": 2.0,
            "dump_on_volume": 0.0,
            "top10_pct": 30.0,
            "fomo_late_hot": 0.0,
            "serial_pattern_break": 1.0,
            "organic_top10_band": 1.0,
            "liq_build_score": 0.8,
            "liq_farm_score": 0.0,
            "social_organic": 0.2,
            "social_shill": 0.0,
        },
        base,
    )
    dudish = divergence_score(
        {
            "rising_book_15m": 0.0,
            "mcap_t0_to_early_ratio": 0.2,
            "bar_mcap_15m_ratio": 0.05,
            "dump_on_volume": 1.0,
            "top10_pct": 92.0,
            "fomo_late_hot": 0.0,
            "serial_pattern_break": 0.0,
            "organic_top10_band": 0.0,
            "liq_build_score": 0.0,
            "liq_farm_score": 0.8,
            "social_organic": 0.0,
            "social_shill": 0.5,
        },
        base,
    )
    assert printerish["score"] > dudish["score"]
    assert printerish["score"] > 0.55
    assert dudish["score"] < 0.45


def test_cohort_proof_separates_printers():
    proof = prove_on_cohort()
    assert proof["paper_only"] is True
    assert proof["n_printers"] == 2
    assert proof["n_duds"] >= 20
    assert proof["separation_ok"] is True
    assert proof["printer_score_median"] > proof["dud_score_median"]
    # Named printers should rank in the top half at least
    for p in proof["printers"]:
        assert p["rank"] is not None
        assert p["rank"] <= proof["n"] // 2 + 2


def test_serial_printer_not_blocked():
    """Super Intelligence is serial AND a printer — score must stay high."""
    rows = prove_on_cohort()
    # score_cohort_row path
    from launchfinder.scoring.early_diff import load_cohort_rows

    si = next(
        r
        for r in load_cohort_rows()
        if r.get("mint", "").startswith("9aqmJjCnnMQv42TXLk921ceUkN35nea2QP969n1caqjj")
        or (r.get("label") == "printer" and "Intelligence" in (r.get("name") or ""))
    )
    scored = score_cohort_row(si)
    assert scored["divergence_score"] > 0.5
    # serial_pattern_break should help not hurt
    assert float(scored["features"].get("serial_pattern_break") or 0) >= 0


def test_concentration_class_bands():
    assert concentration_class(30.0) == "organic_band"
    assert concentration_class(92.0) == "concentrated"
    assert concentration_class(None) == "unknown"


def test_append_top10_history():
    h = append_top10_history({}, 34.97)
    assert h["top10_pct"] == 34.97
    assert len(h["top10_history"]) == 1
    h2 = append_top10_history(h, 35.0)
    assert len(h2["top10_history"]) == 1  # near-equal updates in place


def test_liq_build_vs_pull():
    building = liq_path_from_tape(
        [
            {"mcap": 10_000, "liq": 5_000},
            {"mcap": 12_000, "liq": 5_500},
            {"mcap": 15_000, "liq": 6_500},
            {"mcap": 18_000, "liq": 7_500},
        ]
    )
    assert building["build_score"] > 0.3
    assert building["label"] in {"building", "steady_build"}

    farming = liq_path_from_tape(
        [
            {"mcap": 10_000, "liq": 8_000},
            {"mcap": 14_000, "liq": 6_000},
            {"mcap": 18_000, "liq": 4_500},
            {"mcap": 22_000, "liq": 3_000},
        ]
    )
    assert farming["farm_score"] > 0.3
    assert farming["label"] in {"pulled_on_uptick", "liq_draining"}


def test_social_velocity_shill_vs_organic():
    organic = classify_social_ramp(
        followers_now=200,
        followers_prev=100,
        tweets_now=40,
        tweets_prev=25,
    )
    assert organic["organic_score"] > 0
    assert organic["label"] == "organic_ramp"

    shill = classify_social_ramp(
        followers_now=12,
        followers_prev=10,
        tweets_now=80,
        tweets_prev=5,
    )
    assert shill["shill_score"] > 0.5
    assert shill["label"] == "shill_burst"


def test_holder_curve_and_status_api_shape():
    init_db()
    with session_scope() as session:
        now = datetime.now(timezone.utc)
        token = Token(
            mint="EdDiff1111111111111111111111111111111111111",
            symbol="EDTEST",
            name="Early Diff Test",
            chain="sol",
            first_seen_at=now - timedelta(minutes=40),
        )
        session.add(token)
        session.flush()
        session.add(
            Research(
                token_id=token.id,
                holder_count=400,
                top10_pct=32.0,
                creator_prior_launches=33,
                creator_prior_wins=4,
                creator_prior_rugs=21,
            )
        )
        session.add(
            Snapshot(
                token_id=token.id,
                kind="t0",
                taken_at=now - timedelta(minutes=35),
                mcap_usd=14_000,
                liquidity_usd=8_000,
                volume_h1=17_000,
            )
        )
        session.add(
            Snapshot(
                token_id=token.id,
                kind="live",
                taken_at=now - timedelta(minutes=26),
                mcap_usd=119_000,
                liquidity_usd=30_000,
                volume_h1=108_000,
            )
        )
        for i in range(20):
            session.add(
                TapeBar(
                    chain="sol",
                    mint=token.mint,
                    token_id=token.id,
                    minute=now - timedelta(minutes=35 - i),
                    mcap_usd=14_000 * (1.1 ** i),
                    liquidity_usd=8_000 * (1.05 ** i),
                    volume_h1=17_000 + i * 4_000,
                    holders=200 + i * 15,
                )
            )
        session.flush()

        curve = holder_concentration_curve(session, token)
        assert curve["paper_only"] is True
        assert curve["concentration_class"] == "organic_band"
        assert curve["organic_top10_band"] == 1.0
        assert curve["n_curve_points"] >= 5

        wallets = wallet_clusters(session, token)
        assert wallets["serial_prior"] is True
        assert wallets["serial_hard_block"] is False

        out = early_diff_status(session, "sol", hours=48, top_n=5)
        assert out["paper_only"] is True
        assert "feature_divergence_ranker" in out["workstreams"]
        assert out["cohort_proof"]["separation_ok"] is True
        assert out["serial_policy"]
        assert "PRIOR" in out["serial_policy"] or "prior" in out["serial_policy"].lower()


def test_desk_js_wires_early_diff():
    from pathlib import Path

    js = Path("launchfinder/static/desk.js").read_text()
    assert "earlyDiffData" in js
    assert "/api/paper/v1/early-diff" in js
    assert "learn-early-diff" in js
    assert "Early diff · SI cohort shadow" in js
    assert "maybeEarlyDiffDesktopNotify" in js
    assert "Survivor re-score" in js


def test_app_route_registered():
    from launchfinder.app import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/paper/v1/early-diff" in paths


def test_first_hour_filter_and_survivor_only_universe():
    survivors = [
        {
            "mint": "pass1",
            "symbol": "PASS",
            "divergence_score": 0.7,
            "features": {"rising_book_15m": 1.0, "dump_on_volume": 0.0, "fomo_late_hot": 0.0},
            "bundles": {"wallets": {"serial_prior": False}},
        },
        {
            "mint": "dump1",
            "symbol": "DUMP",
            "divergence_score": 0.8,
            "features": {"rising_book_15m": 0.0, "dump_on_volume": 1.0, "fomo_late_hot": 0.0},
            "bundles": {"wallets": {"serial_prior": False}},
        },
        {
            "mint": "low1",
            "symbol": "LOW",
            "divergence_score": 0.2,
            "features": {"rising_book_15m": 0.0, "dump_on_volume": 0.0, "fomo_late_hot": 0.0},
            "bundles": {"wallets": {"serial_prior": False}},
        },
    ]
    assert passed_first_hour_filter(survivors[0]) is True
    assert passed_first_hour_filter(survivors[1]) is False
    assert passed_first_hour_filter(survivors[2]) is False
    assert FIRST_HOUR_PASS_SCORE == 0.45

    init_db()
    with session_scope() as session:
        # Only PASS exists in DB — dump/low must not be rescored (no Token rows).
        now = datetime.now(timezone.utc)
        tok = Token(
            mint="pass1",
            symbol="PASS",
            name="Pass",
            chain="sol",
            first_seen_at=now - timedelta(hours=3),
        )
        session.add(tok)
        session.flush()
        for i in range(180):
            session.add(
                TapeBar(
                    chain="sol",
                    mint=tok.mint,
                    token_id=tok.id,
                    minute=now - timedelta(minutes=180 - i),
                    mcap_usd=10_000 * (1.01 ** min(i, 60)),
                    liquidity_usd=5_000 * (1.005 ** min(i, 60)),
                    volume_h1=20_000 + i * 100,
                    holders=100 + i,
                )
            )
        session.flush()
        out = survivor_hour_rescores(session, survivors)
        assert out["survivor_only"] is True
        assert out["n_h1_scanned"] == 3
        assert out["n_survivors"] == 1
        assert out["n_rescored"] == 1
        assert out["hours"] == [1, 2, 3, 4]
        hours = {p["hour"] for p in out["items"][0]["passes"]}
        assert hours == {1, 2, 3, 4}


def test_threshold_alert_idempotent():
    from launchfinder.scoring.early_diff import list_threshold_alerts

    init_db()
    with session_scope() as session:
        scored = [
            {
                "mint": "AlertMint11111111111111111111111111111111",
                "symbol": "ALRT",
                "name": "Alert",
                "divergence_score": ALERT_THRESHOLD + 0.1,
            }
        ]
        first = emit_threshold_alerts(session, scored)
        second = emit_threshold_alerts(session, scored)
        assert len(first) == 1
        assert first[0]["not_a_trade_signal"] is True
        assert first[0]["paper_only"] is True
        assert second == []
        feed = list_threshold_alerts(session)
        assert len(feed) >= 1
        assert feed[0]["symbol"] == "ALRT"
        assert feed[0]["not_a_trade_signal"] is True


def test_worker_wires_early_diff_loop():
    from pathlib import Path

    src = Path("launchfinder/worker.py").read_text()
    assert "_early_diff_alert_loop" in src
    assert "run_early_diff_tick" in src
    assert "early_diff_task" in src
