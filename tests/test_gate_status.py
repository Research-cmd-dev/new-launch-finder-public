"""Gate predicates for the paperV1 production gate — no live HTTP."""

from datetime import datetime, timedelta, timezone

from scripts.gate_status import (
    CHAINS,
    SAMPLE_BAR,
    evaluate_gates,
    has_hard_tag,
    knob_snapshot,
    loops_fresh,
    rev_age_hours,
    summarize_chain,
    wide_hi_baseline,
    window_days,
)


def _row(mint, status, tags=None, hit2x=False, live_p=None, skip_reason=""):
    return {
        "mint": mint,
        "status": status,
        "tags": tags or [],
        "hit2x": hit2x,
        "live_p": live_p,
        "skip_reason": skip_reason,
    }


def test_hard_tag_is_github_dev_cto_not_meme():
    assert has_hard_tag(["meme", "github"])
    assert has_hard_tag(["cto"])
    assert not has_hard_tag(["meme"])
    assert not has_hard_tag([])
    assert not has_hard_tag(None)


def test_window_days_newest_first():
    assert window_days("2026-09-29", 3) == ["2026-09-29", "2026-09-28", "2026-09-27"]
    assert window_days("2026-09-29", 0) == ["2026-09-29"]


def test_summarize_chain_counts_hard_tags_on_opens_only_and_resolved_closes():
    today = "2026-09-29"
    reviews = {
        # Resolved day: everything closed. Two opens, one hard tag, one hit.
        "2026-09-27": {
            "taken_chain": 2,
            "cap_per_chain": 3,
            "picked": [_row("a", "closed", ["github", "meme"], True), _row("b", "closed", ["meme"], False)],
            "closed": [_row("a", "closed", ["github", "meme"], True), _row("b", "closed", ["meme"], False)],
            "skipped": [_row("s1", "skipped", skip_reason="v1 cap")],
            "shadow": [],
            "queued": [],
        },
        # Unresolved: one still open, one early close (survivor) — must not count as sample.
        "2026-09-28": {
            "taken_chain": 2,
            "cap_per_chain": 3,
            "picked": [_row("c", "open", ["meme"]), _row("d", "closed", ["meme"], True)],
            "closed": [_row("d", "closed", ["meme"], True)],
            "skipped": [_row("s2", "skipped", skip_reason="")],
            "shadow": [],
            "queued": [],
        },
        # Today: nothing yet.
        today: {"taken_chain": 0, "cap_per_chain": 3, "picked": [], "closed": [], "skipped": [], "shadow": [], "queued": []},
    }
    s = summarize_chain("robinhood", reviews, today=today, now_hour_utc=13)
    assert s["opens_n"] == 4
    assert s["hard_tag_n"] == 1
    assert s["hard_tag_coverage"] == 0.25
    assert s["any_tag_coverage"] == 1.0
    assert s["resolved_days"] == ["2026-09-27"]
    assert s["resolved_closed_n"] == 2
    assert s["resolved_hit2x"] == 0.5
    assert s["unresolved_closed_n"] == 1
    assert s["unresolved_hit2x"] == 1.0
    assert s["unreadable_skips"] == 1
    assert s["cap_breaches"] == []
    assert s["silent_days"] == []
    assert s["silent_today"] is True


def test_summarize_chain_flags_cap_breach_and_silent_days():
    today = "2026-09-29"
    empty = {"picked": [], "closed": [], "skipped": [], "shadow": [], "queued": []}
    reviews = {
        "2026-09-27": {**empty, "taken_chain": 5, "cap_per_chain": 3},
        "2026-09-28": {**empty, "taken_chain": 0, "cap_per_chain": 3},
        today: {**empty, "taken_chain": 0, "cap_per_chain": 3},
    }
    s = summarize_chain("sol", reviews, today=today, now_hour_utc=6)
    assert s["cap_breaches"] == [{"day": "2026-09-27", "taken_chain": 5, "cap_per_chain": 3}]
    # A day that spent slots but returns no rows is a truncated review, not silence.
    assert s["truncated_days"] == ["2026-09-27"]
    assert s["silent_days"] == ["2026-09-28"]
    # 06:00 UTC is too early to call today silent.
    assert s["silent_today"] is False


def test_wide_hi_baseline_uses_bins_at_or_above_hi_weighted_by_observed():
    cal = {
        "lines": {"hi": 0.3},
        "bins": [
            {"bin": "0.2-0.3", "n": 100, "n_observed": 50, "hit2x": 0.5, "hit2x_observed": 0.9},
            {"bin": "0.3-0.4", "n": 100, "n_observed": 50, "hit2x": 0.10, "hit2x_observed": 0.20},
            {"bin": "0.4-0.5", "n": 300, "n_observed": 150, "hit2x": 0.02, "hit2x_observed": 0.04},
        ],
    }
    b = wide_hi_baseline(cal)
    assert b["hi"] == 0.3
    assert b["n_observed"] == 200
    assert b["hit2x_observed"] == round((0.20 * 50 + 0.04 * 150) / 200, 4)
    assert b["n"] == 400
    assert b["hit2x"] == round((0.10 * 100 + 0.02 * 300) / 400, 4)
    assert wide_hi_baseline(None)["hit2x_observed"] is None


def test_loops_fresh_respects_per_loop_budget():
    health = {
        "loops": {
            "ingest": {"age_s": 16},
            "hunt_tape": {"age_s": 11},
            "tape_refresh": {"age_s": 57},
            "batch_fit": {"age_s": 456},
            "fomo_trending": {"age_s": 1232},
            "fomo_alerts": {"age_s": 120},
        }
    }
    assert loops_fresh(health) == []
    health["loops"]["ingest"]["age_s"] = 1200
    del health["loops"]["batch_fit"]
    assert loops_fresh(health) == ["ingest:1200s", "batch_fit:missing"]


def test_rev_age_resets_when_rev_changes():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    age, st = rev_age_hours(None, "stack-v129", now)
    assert age is None and st["image_rev"] == "stack-v129"
    later = now + timedelta(hours=30)
    age, st2 = rev_age_hours(st, "stack-v129", later)
    assert age == 30.0 and st2 == st
    age, st3 = rev_age_hours(st, "stack-v130", later)
    assert age is None and st3["image_rev"] == "stack-v130"


def _chain_summary(**over):
    base = {
        "opens_n": 0,
        "hard_tag_coverage": None,
        "any_tag_coverage": None,
        "resolved_closed_n": 0,
        "resolved_hit2x": None,
        "unresolved_closed_n": 0,
        "unresolved_hit2x": None,
        "cap_breaches": [],
        "silent_days": [],
        "truncated_days": [],
        "silent_today": False,
        "unreadable_skips": 0,
    }
    base.update(over)
    return base


def _health(rev="stack-v129", git_sha=None, x_api=False):
    return {
        "image_rev": rev,
        "git_sha": git_sha,
        "x_api": x_api,
        "loops": {
            k: {"age_s": 10}
            for k in ("ingest", "hunt_tape", "tape_refresh", "batch_fit", "fomo_trending", "fomo_alerts")
        },
    }


RISK_OFF = {
    "armed": False,
    "kill_switch": False,
    "live_allowed": False,
    "paper_only": True,
    "max_notional_usd": 200.0,
    "max_per_name_usd": 40.0,
}


def test_gates_are_red_on_today_shape_and_risk_stays_green():
    """2026-09-29 live shape: Sol silent, RH 0 hard tags, 5>3 breach, git_sha null."""
    per_chain = {
        "sol": _chain_summary(silent_days=["2026-09-28"], silent_today=True),
        "robinhood": _chain_summary(
            opens_n=5,
            hard_tag_coverage=0.0,
            any_tag_coverage=1.0,
            unresolved_closed_n=1,
            unresolved_hit2x=1.0,
            cap_breaches=[{"day": "2026-09-29", "taken_chain": 5, "cap_per_chain": 3}],
        ),
    }
    baselines = {c: {"hi": 0.3, "hit2x_observed": 0.1, "n_observed": 1000} for c in CHAINS}
    gates = evaluate_gates(
        per_chain,
        baselines,
        health=_health(),
        risk=RISK_OFF,
        delta_ok={"sol": True, "robinhood": True},
        repo_rev="stack-v129",
        rev_age_h=None,
    )
    by = {g["gate"]: g for g in gates}
    assert by["thesis_coverage"]["status"] == "fail"
    assert by["sample"]["status"] == "fail"
    assert by["hit_quality"]["status"] == "warn"  # not enough resolved sample to judge
    assert by["stability"]["status"] == "fail" and "cap breach" in by["stability"]["detail"]
    assert by["operator_ritual"]["status"] == "fail" and "silent days" in by["operator_ritual"]["detail"]
    assert by["risk_controls"]["status"] == "pass"


def test_truncated_review_days_fail_stability_not_ritual():
    per_chain = {
        "sol": _chain_summary(),
        "robinhood": _chain_summary(truncated_days=["2026-09-27", "2026-09-28"]),
    }
    baselines = {c: {} for c in CHAINS}
    gates = evaluate_gates(
        per_chain,
        baselines,
        health=_health(git_sha="x"),
        risk=RISK_OFF,
        delta_ok={"sol": True, "robinhood": True},
        repo_rev="stack-v129",
        rev_age_h=48.0,
    )
    by = {g["gate"]: g for g in gates}
    assert by["stability"]["status"] == "fail" and "review lost 2 spent day(s)" in by["stability"]["detail"]
    assert by["operator_ritual"]["status"] == "pass"


def test_gates_pass_when_every_bar_clears():
    per_chain = {
        c: _chain_summary(opens_n=40, hard_tag_coverage=0.9, any_tag_coverage=1.0, resolved_closed_n=SAMPLE_BAR, resolved_hit2x=0.4)
        for c in CHAINS
    }
    baselines = {c: {"hi": 0.3, "hit2x_observed": 0.12, "n_observed": 1000} for c in CHAINS}
    gates = evaluate_gates(
        per_chain,
        baselines,
        health=_health(git_sha="abc123"),
        risk=RISK_OFF,
        delta_ok={"sol": True, "robinhood": True},
        repo_rev="stack-v129",
        rev_age_h=25.0,
    )
    assert all(g["status"] == "pass" for g in gates), gates


def test_risk_gate_fails_on_arm_or_paid_x_spend():
    per_chain = {c: _chain_summary() for c in CHAINS}
    baselines = {c: {} for c in CHAINS}
    armed = evaluate_gates(
        per_chain,
        baselines,
        health=_health(),
        risk={**RISK_OFF, "armed": True, "live_allowed": True},
        delta_ok={"sol": True, "robinhood": True},
        repo_rev="stack-v129",
        rev_age_h=None,
    )
    assert {g["gate"]: g["status"] for g in armed}["risk_controls"] == "fail"
    spend = evaluate_gates(
        per_chain,
        baselines,
        health=_health(x_api=True),
        risk=RISK_OFF,
        delta_ok={"sol": True, "robinhood": True},
        repo_rev="stack-v129",
        rev_age_h=None,
    )
    assert {g["gate"]: g["status"] for g in spend}["risk_controls"] == "fail"


def test_stability_fails_on_rev_drift_and_warns_on_young_rev():
    per_chain = {c: _chain_summary() for c in CHAINS}
    baselines = {c: {} for c in CHAINS}
    drift = evaluate_gates(
        per_chain, baselines, health=_health(rev="stack-v128"), risk=RISK_OFF,
        delta_ok={"sol": True, "robinhood": True}, repo_rev="stack-v129", rev_age_h=48.0,
    )
    assert {g["gate"]: g["status"] for g in drift}["stability"] == "fail"
    young = evaluate_gates(
        per_chain, baselines, health=_health(git_sha="x"), risk=RISK_OFF,
        delta_ok={"sol": True, "robinhood": True}, repo_rev="stack-v129", rev_age_h=3.0,
    )
    assert {g["gate"]: g["status"] for g in young}["stability"] == "warn"


def test_knob_snapshot_lists_every_tunable_once():
    snap = knob_snapshot({"rank_policy": "signal", "thesis_weights": {"github_auth_n": 0.35}})
    assert snap["rank_policy"] == "signal"
    assert snap["cap_per_chain"] == 3
    assert snap["hard_tags"] == ["cto", "dev", "github"]
    assert snap["refit_min_closed"] == 12
    assert snap["policy_flip_margin"] == 0.05
