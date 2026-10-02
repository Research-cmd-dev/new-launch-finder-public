"""paperV1 sits beside the wide book. It does not change who that book buys."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from launchfinder.db import init_db, session_scope
from launchfinder.desk_lines import SCORER_FIRST_SIGHT
from launchfinder.ledger import (
    _v1_entry_features,
    lock_paper_v1,
    mark_paper_fills,
    paper_ledger_view,
    paper_v1_book,
    paper_v1_review,
    reconsider_paper_v1,
    sync_paper_ledger,
)
from launchfinder.models import Decision, HuntCard, ModelArtifact, Outcome, PaperFill, Research, ScanState, Ticket, Token, utcnow
from launchfinder.scoring.batch_fit import reset_artifact_cache
from launchfinder.scoring.hunt import upsert_hunt
from launchfinder.scoring.paper_v1 import (
    PAPER_V1_CAP,
    PAPER_V1_OPEN_VIA_THIN,
    choose_v1,
    lock_due,
    v1_immediate,
    v1_qualifies,
    v1_queue_reason,
    v1_score_path,
    v1_thesis_from_features,
)


def test_sol_needs_the_buy_line_and_live_rh_needs_entry():
    hard = v1_thesis_from_features(
        {"github_auth_n": 0.9, "real_project": 1.0, "gmgn_cto": 0.0, "name_quality": 0.2}
    )
    assert v1_score_path("sol", 0.20, 0.50, hi=0.14) is True
    assert v1_score_path("sol", 0.20, 0.40, hi=0.14) is False
    assert v1_score_path("robinhood", 0.40, None, hi=0.30) is True
    assert v1_score_path("robinhood", 0.39, 0.90, hi=0.30) is False
    assert v1_qualifies("sol", 0.20, 0.50, hi=0.14, thesis=hard) is True
    assert v1_qualifies("sol", 0.20, 0.50, hi=0.14, thesis=None) is False
    assert v1_qualifies("sol", 0.20, 0.39, hi=0.14, thesis=hard) is False
    assert v1_qualifies("sol", 0.10, 0.80, hi=0.14, thesis=hard) is False
    assert v1_qualifies("sol", 0.20, None, hi=0.14, thesis=hard) is False
    assert v1_qualifies("robinhood", 0.40, None, hi=0.30, thesis=hard) is True
    assert v1_qualifies("robinhood", 0.40, None, hi=0.30, thesis=None) is False
    assert v1_qualifies("robinhood", 0.29, 0.90, hi=0.30, thesis=hard) is False
    assert v1_immediate(0.70, hard) is True and v1_immediate(0.69, hard) is False
    assert v1_immediate(0.70, None) is False and v1_immediate(None, None) is False


def test_thesis_path_softens_floors_and_can_take_immediate():
    thesis = v1_thesis_from_features(
        {"github_auth_n": 0.9, "real_project": 1.0, "gmgn_cto": 0.0, "name_quality": 0.2}
    )
    assert thesis["score"] >= 0.25 and "github" in thesis["tags"]
    # Soft Sol path: Live 0.40 + thesis, Entry still near buy line.
    assert v1_qualifies("sol", 0.12, 0.40, hi=0.14, thesis=thesis) is True
    assert v1_qualifies("sol", 0.12, 0.40, hi=0.14, thesis=None) is False
    assert v1_qualifies("robinhood", 0.32, None, hi=0.30, thesis=thesis) is True
    assert v1_qualifies("robinhood", 0.32, None, hi=0.30, thesis=None) is False
    strong = v1_thesis_from_features(
        {"github_auth_n": 0.9, "real_project": 1.0, "gmgn_cto": 1.0, "name_quality": 0.8}
    )
    assert v1_immediate(0.50, strong) is True
    assert v1_immediate(0.50, thesis) is False


def test_score_only_wide_fill_queues_thin_paper_v1_without_hard_tag():
    init_db()
    reset_artifact_cache()
    now = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        _live(session, "sol", 1.0)
        buy = _token("PaperV1NoThesis11111111111111111111111111", p=0.20, thesis=False)
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        sync_paper_ledger(session, "sol", now=now)
        assert session.query(PaperFill).filter(PaperFill.mint == buy.mint, PaperFill.line == "gated90").count() == 1
        v1 = session.query(PaperFill).filter(PaperFill.mint == buy.mint, PaperFill.line == "paper_v1").one()
        assert v1.status == "queued" and v1.open_via == PAPER_V1_OPEN_VIA_THIN
        assert session.query(PaperFill).filter(PaperFill.mint == buy.mint, PaperFill.line == "paper_v1", PaperFill.status == "open").count() == 0


def test_choose_respects_the_cap_and_an_earlier_tie():
    early = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 27, 11, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "late", "chain": "sol", "entry_p": 0.20, "live_p": 0.60, "opened_at": late},
        {"id": "early", "chain": "sol", "entry_p": 0.20, "live_p": 0.60, "opened_at": early},
        {"id": "rh", "chain": "robinhood", "entry_p": 0.90, "live_p": 0.10, "opened_at": late},
        {"id": "weak", "chain": "sol", "entry_p": 0.14, "live_p": 0.51, "opened_at": early},
    ]
    # Per-chain lanes call choose with min_per_chain=0; signal-rank picks RH then early.
    assert choose_v1(rows, already=0, cap=2, min_per_chain=0) == ["rh", "early"]
    # Legacy shared-cap mix still reserves a Sol slot when asked.
    assert choose_v1(rows, already=0, cap=2, min_per_chain=1) == ["early", "rh"]
    assert choose_v1(rows, already=6, cap=PAPER_V1_CAP) == []
    # Single remaining slot: pure rank (RH Entry strength wins).
    assert choose_v1(rows, already=2, cap=3) == ["rh"]


def test_thesis_score_prefers_github_and_cto():
    bits = v1_thesis_from_features(
        {"github_auth_n": 0.9, "real_project": 1.0, "gmgn_cto": 1.0, "name_quality": 0.8}
    )
    assert bits["score"] > 0.7
    assert "github" in bits["tags"] and "cto" in bits["tags"] and "dev" in bits["tags"]
    assert "github" in bits["hard_tags"]
    early = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 27, 11, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "hot", "chain": "sol", "entry_p": 0.20, "live_p": 0.90, "opened_at": early, "thesis_score": 0.1},
        {
            "id": "thesis",
            "chain": "sol",
            "entry_p": 0.15,
            "live_p": 0.55,
            "opened_at": late,
            "thesis_score": 0.85,
        },
    ]
    assert choose_v1(rows, already=0, cap=1, policy="thesis") == ["thesis"]
    # Signal policy prefers Live strength when signal_score is unset.
    assert choose_v1(rows, already=0, cap=1, policy="signal") == ["hot"]


def test_lock_is_due_at_23_and_for_a_missed_day():
    noon = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    lock = datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)
    nxt = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
    assert lock_due("2026-09-27", noon) is False
    assert lock_due("2026-09-27", lock) is True
    assert lock_due("2026-09-27", nxt) is True
    assert lock_due("2026-09-28", lock) is False


_THESIS_RAW = {
    "github": {
        "full_name": "acme/fixture",
        "age_days": 120,
        "commits": 40,
        "contributors": 3,
        "stars": 10,
        "url": "https://github.com/acme/fixture",
    }
}


def _token(mint, *, p, chain="sol", holders=120, thesis=True):
    import json

    now = utcnow()
    tok = Token(
        mint=mint,
        symbol=mint[:4].upper(),
        chain=chain,
        first_seen_at=now - timedelta(minutes=6),
        migrated_at=now - timedelta(minutes=6),
        created_at_chain=now - timedelta(minutes=36),
        source="poll",
    )
    feat = {"holder_n": 0.4, "name_quality": 0.7, "github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0}
    raw = _THESIS_RAW if thesis else None
    tok.research = Research(
        features_json=json.dumps(feat),
        raw_json=json.dumps(raw) if raw else None,
        p_good=p,
        heuristic_p=0.8,
        model_p=0.9,
        holder_count=holders,
        risk_flags_json="[]",
        scorer=SCORER_FIRST_SIGHT,
    )
    tok.outcome = Outcome(t0_mcap=69_000, last_mcap=70_000, last_liq=25_000, max_mcap=70_000, multiple=1.01)
    return tok


def _promote(session, chain):
    from launchfinder.scoring import first_sight as fs
    import json

    session.add(
        ModelArtifact(
            chain=chain,
            kind=fs.KIND,
            created_at=utcnow(),
            version=1,
            weights_json=json.dumps({"w": {n: 0.0 for n in fs.FEATURES}, "mu": {n: 0.0 for n in fs.FEATURES}, "sd": {n: 1.0 for n in fs.FEATURES}}),
            bias=0.0,
            calibration_json="[]",
            n_train=10,
            n_valid=10,
            metrics_json="{}",
            incumbent_json="{}",
            promoted=True,
        )
    )
    session.flush()
    fs.reset_cache()


def _live(session, chain, bias):
    session.add(
        ModelArtifact(
            chain=chain,
            kind="live",
            created_at=utcnow(),
            version=1,
            weights_json="{}",
            bias=bias,
            calibration_json="[]",
            n_train=400,
            n_valid=100,
            metrics_json="{}",
            incumbent_json="{}",
            promoted=True,
        )
    )
    session.flush()
    reset_artifact_cache()


def test_wide_book_still_fills_and_a_quiet_sol_name_only_queues():
    init_db()
    reset_artifact_cache()
    now = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        _live(session, "sol", 0.0)  # sigmoid(0) == 0.50, under the 0.70 now-line
        buy = _token("PaperV1Sol1111111111111111111111111111111", p=0.20)
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        out = sync_paper_ledger(session, "sol", now=now)
        assert out["opened"] == 1
        wide = session.query(PaperFill).filter(PaperFill.line == "gated90").one()
        side = session.query(PaperFill).filter(PaperFill.line == "paper_v1").one()
        assert wide.status == "open" and side.status == "queued"
        assert side.opened_at == wide.opened_at
        assert side.exit_reason.startswith("q:0.5000|2026-09-27")
        assert session.query(Ticket).count() == 1
        assert session.query(Decision).filter(Decision.kind == "gate").count() == 1
        wide.opened_at = now - timedelta(hours=25)
        side.opened_at = now - timedelta(hours=25)
        mark_paper_fills(session, "sol", now=now)
        assert wide.status == "closed" and wide.exit_reason == "24h"
        assert side.status == "queued"
        view = paper_ledger_view(session, "sol")
        assert view["closed_trades"] == 1
        assert view["scorecard"]["this_window"]["n"] == 0
        assert view["scorecard"]["paper_v1"]["queued"] == 1
        assert view["scorecard"]["paper_v1"]["closed"] == 0
        assert view["scorecard"]["paper_v1"]["today"]["taken"] == 0


def test_sol_under_live_floor_stays_off_the_short_list():
    init_db()
    reset_artifact_cache()
    now = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        _live(session, "sol", -2.0)
        buy = _token("PaperV1Low1111111111111111111111111111111", p=0.20)
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        assert sync_paper_ledger(session, "sol", now=now)["opened"] == 1
        assert session.query(PaperFill).filter(PaperFill.line == "paper_v1").count() == 0
        assert session.query(PaperFill).filter(PaperFill.line == "gated90").count() == 1


def test_live_070_takes_a_slot_and_a_name_after_the_lock_waits():
    init_db()
    reset_artifact_cache()
    morning = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        _live(session, "sol", 1.0)  # sigmoid(1) ~ 0.73
        first = _token("PaperV1Now1111111111111111111111111111111", p=0.20)
        session.add(first)
        session.flush()
        upsert_hunt(session, first)
        sync_paper_ledger(session, "sol", now=morning)
        side = session.query(PaperFill).filter(PaperFill.line == "paper_v1").one()
        assert side.status == "open" and side.exit_reason == "d:2026-09-27"
        assert session.query(Ticket).count() == 1
        locked = lock_paper_v1(session, now=evening)
        assert locked["opened"] == 0
        assert session.query(ScanState).filter(ScanState.key == "paper_v1:locked:2026-09-27").count() == 1
        second = _token("PaperV1Nxt1111111111111111111111111111111", p=0.22)
        session.add(second)
        session.flush()
        upsert_hunt(session, second)
        sync_paper_ledger(session, "sol", now=evening)
        nxt = session.query(PaperFill).filter(PaperFill.mint == second.mint, PaperFill.line == "paper_v1").one()
        assert nxt.status == "queued" and nxt.exit_reason.endswith("|2026-09-28")
        assert session.query(PaperFill).filter(PaperFill.line == "gated90").count() == 2


def test_rh_below_040_does_not_fill_a_spare_slot():
    init_db()
    reset_artifact_cache()
    now = datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "robinhood")
        low = _token("0x18e600000000000000000000000000000000aa01", p=0.36, chain="robinhood", thesis=False)
        high = _token("0x18e600000000000000000000000000000000aa02", p=0.42, chain="robinhood", thesis=True)
        session.add_all([low, high])
        session.flush()
        upsert_hunt(session, low)
        upsert_hunt(session, high)
        assert sync_paper_ledger(session, "robinhood", now=now)["opened"] == 2
        sides = {row.mint: row for row in session.query(PaperFill).filter(PaperFill.line == "paper_v1").all()}
        assert set(sides) == {high.mint}
        assert sides[high.mint].status == "queued"


def test_lock_picks_per_chain_lanes_and_a_missed_day():
    init_db()
    day = "2026-09-26"
    opened = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        specs = [
            ("sol", "PaperV1A11111111111111111111111111111111", 0.20, 0.90),
            ("sol", "PaperV1B11111111111111111111111111111111", 0.20, 0.80),
            ("sol", "PaperV1C11111111111111111111111111111111", 0.20, 0.60),
            ("sol", "PaperV1D11111111111111111111111111111111", 0.20, 0.55),
            ("robinhood", "0x18e600000000000000000000000000000000bb01", 0.90, 0.10),
            ("robinhood", "0x18e600000000000000000000000000000000bb02", 0.50, 0.10),
            ("robinhood", "0x18e600000000000000000000000000000000bb03", 0.41, 0.10),
        ]
        for chain, mint, entry, live in specs:
            tok = _token(mint, p=entry, chain=chain)
            session.add(tok)
            session.flush()
            session.add(
                PaperFill(
                    chain=chain,
                    mint=mint,
                    token_id=tok.id,
                    line="paper_v1",
                    status="queued",
                    opened_at=opened,
                    entry_p=entry,
                    entry_mcap=70_000,
                    entry_liq=25_000,
                    target=2.0,
                    ride=10.0,
                    max_mcap=70_000,
                    min_mcap=70_000,
                    last_mcap=70_000,
                    last_liq=25_000,
                    exit_reason=v1_queue_reason(live, day),
                    image_rev="test",
                    updated_at=opened,
                )
            )
        session.flush()
        # The desk was down through 23:00. The next morning locks that day.
        # Independent lanes: 3 Sol + 3 RH = 6 open, 1 Sol skipped.
        out = lock_paper_v1(session, now=datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc))
        assert out["opened"] == 6 and out["skipped"] == 1
        chosen = {
            row.mint
            for row in session.query(PaperFill).filter(PaperFill.line == "paper_v1", PaperFill.status == "open").all()
        }
        assert "0x18e600000000000000000000000000000000bb01" in chosen  # RH entry 0.90
        assert "0x18e600000000000000000000000000000000bb03" in chosen  # RH lane has room now
        assert "PaperV1A11111111111111111111111111111111" in chosen
        assert "PaperV1D11111111111111111111111111111111" not in chosen  # 4th Sol skipped
        again = lock_paper_v1(session, now=datetime(2026, 9, 27, 1, 5, tzinfo=timezone.utc))
        assert again == {"opened": 0, "skipped": 0}


def test_lock_skips_stragglers_on_an_already_locked_day():
    """A queued row that arrives after the ScanState lock must not crash the lock."""
    init_db()
    day = "2026-09-25"
    opened = datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        tok = _token("PaperV1Late111111111111111111111111111111", p=0.20)
        session.add(tok)
        session.flush()
        key = f"paper_v1:locked:{day}"
        prior = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if prior is None:
            session.add(ScanState(key=key, value=day, updated_at=opened))
        else:
            prior.value = day
            prior.updated_at = opened
        session.add(
            PaperFill(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                line="paper_v1",
                status="queued",
                opened_at=opened,
                entry_p=0.20,
                entry_mcap=70_000,
                entry_liq=25_000,
                target=2.0,
                ride=10.0,
                max_mcap=70_000,
                min_mcap=70_000,
                last_mcap=70_000,
                last_liq=25_000,
                exit_reason=v1_queue_reason(0.60, day),
                image_rev="test",
                updated_at=opened,
            )
        )
        session.flush()
        out = lock_paper_v1(session, now=datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc))
        assert out == {"opened": 0, "skipped": 1}
        fill = session.query(PaperFill).filter(PaperFill.line == "paper_v1").one()
        assert fill.status == "skipped" and fill.exit_reason.startswith("v1 cap")


def test_after_lock_hour_a_hot_live_queues_until_lock():
    """Past 23:00, Live >= 0.70 waits for choose_v1 instead of taking a slot early."""
    init_db()
    reset_artifact_cache()
    evening = datetime(2026, 10, 1, 23, 15, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        _live(session, "sol", 1.0)
        buy = _token("PaperV1Eve1111111111111111111111111111111", p=0.20)
        session.add(buy)
        session.flush()
        upsert_hunt(session, buy)
        sync_paper_ledger(session, "sol", now=evening)
        side = session.query(PaperFill).filter(PaperFill.line == "paper_v1").one()
        assert side.status == "queued" and side.exit_reason.endswith("|2026-10-01")
        locked = lock_paper_v1(session, now=evening)
        assert locked["opened"] == 1 and locked["skipped"] == 0
        session.refresh(side)
        assert side.status == "open" and side.exit_reason == "d:2026-10-01"


def test_close_keeps_the_book_day_on_the_cap():
    """A name locked onto tomorrow must still count for tomorrow after it closes."""
    from launchfinder.scoring.paper_v1 import book_day_from_reason, v1_close_reason

    assert book_day_from_reason(v1_close_reason("24h", "2026-09-28")) == "2026-09-28"
    init_db()
    opened = datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc)
    with session_scope() as session:
        tok = _token("PaperV1Cls1111111111111111111111111111111", p=0.20)
        session.add(tok)
        session.flush()
        session.add(
            PaperFill(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                line="paper_v1",
                status="open",
                opened_at=opened,
                entry_p=0.20,
                entry_mcap=70_000,
                entry_liq=25_000,
                target=2.0,
                ride=10.0,
                max_mcap=70_000,
                min_mcap=70_000,
                last_mcap=70_000,
                last_liq=25_000,
                exit_reason="d:2026-09-28",
                image_rev="test",
                updated_at=opened,
            )
        )
        session.flush()
        mark_paper_fills(session, "sol", now=opened + timedelta(hours=25))
        fill = session.query(PaperFill).filter(PaperFill.line == "paper_v1").one()
        assert fill.status == "closed"
        assert fill.exit_reason.startswith("24h|")
        assert book_day_from_reason(fill.exit_reason) == "2026-09-28"
        from launchfinder.ledger import _v1_taken

        assert _v1_taken(session, "2026-09-28") == 1
        assert _v1_taken(session, "2026-09-27") == 0


def test_claiming_the_lock_first_blocks_a_second_worker():
    """Two lock passes for one day: the loser must not open anyone."""
    from launchfinder.ledger import _claim_v1_lock

    init_db()
    day = "2026-10-05"
    opened = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        tok = _token("PaperV1Race11111111111111111111111111111", p=0.20)
        session.add(tok)
        session.flush()
        session.add(
            PaperFill(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                line="paper_v1",
                status="queued",
                opened_at=opened,
                entry_p=0.20,
                entry_mcap=70_000,
                entry_liq=25_000,
                target=2.0,
                ride=10.0,
                max_mcap=70_000,
                min_mcap=70_000,
                last_mcap=70_000,
                last_liq=25_000,
                exit_reason=v1_queue_reason(0.80, day),
                image_rev="test",
                updated_at=opened,
            )
        )
        session.flush()
        now = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)
        assert _claim_v1_lock(session, day, now=now) is True
        assert _claim_v1_lock(session, day, now=now) is False
        out = lock_paper_v1(session, now=now)
        assert out["opened"] == 0 and out["skipped"] == 1
        fill = session.query(PaperFill).filter(PaperFill.line == "paper_v1").one()
        assert fill.status == "skipped"


def test_promote_holds_a_per_day_lease_so_overlapping_workers_cannot_double_open():
    """Two promoters in one tick: the second does nothing; the next cycle re-claims."""
    from launchfinder.ledger import PAPER_V1_PROMOTE_LEASE_S, _claim_v1_promote_lease, promote_paper_v1_queue

    init_db()
    day = "2026-10-19"
    opened = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        for i in range(4):
            tok = _token(f"PaperV1Lease{i}111111111111111111111111111", p=0.20)
            session.add(tok)
            session.flush()
            session.add(
                PaperFill(
                    chain="sol",
                    mint=tok.mint,
                    token_id=tok.id,
                    line="paper_v1",
                    status="queued",
                    opened_at=opened,
                    entry_p=0.20,
                    entry_mcap=70_000,
                    entry_liq=25_000,
                    target=2.0,
                    ride=10.0,
                    max_mcap=70_000,
                    min_mcap=70_000,
                    last_mcap=70_000,
                    last_liq=25_000,
                    exit_reason=v1_queue_reason(0.80, day),  # Live ≥ 0.70: immediate
                    image_rev="test",
                    updated_at=opened,
                )
            )
        session.flush()
        now = opened + timedelta(minutes=5)
        assert _claim_v1_promote_lease(session, day, now=now) is True
        # Same tick, second worker: lease held → nothing opens.
        assert _claim_v1_promote_lease(session, day, now=now + timedelta(seconds=1)) is False
        out = promote_paper_v1_queue(session, now=now + timedelta(seconds=2))
        assert out["opened"] == 0 and out.get("leased") == 1
        assert session.query(PaperFill).filter(PaperFill.status == "open").count() == 0
        # Next cycle (lease stale): the single worker promotes and the cap holds at 3.
        later = now + timedelta(seconds=PAPER_V1_PROMOTE_LEASE_S + 1)
        out = promote_paper_v1_queue(session, now=later)
        assert out["opened"] == 3
        assert session.query(PaperFill).filter(PaperFill.status == "open").count() == 3
        assert session.query(ScanState).filter(ScanState.key == f"paper_v1:promote_lease:{day}").count() == 1


def test_v1_entry_features_prefers_entry_over_empty_gate():
    """Wide fills stamp a gate Decision id; thesis must still read entry features."""
    init_db()
    with session_scope() as session:
        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        day = "2026-09-27"
        tok = _token("GateFeatMint1111111111111111111111111", p=0.20, thesis=False)
        session.add(tok)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind="entry",
                chain="sol",
                at=now,
                entry_p=0.20,
                entry_mcap=70_000,
                liq=25_000,
                features_json='{"github_auth_n":0.9,"real_project":1.0,"gmgn_cto":0.0,"name_quality":0.2}',
                image_rev="test",
            )
        )
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind="gate",
                chain="sol",
                at=now,
                entry_p=0.20,
                entry_mcap=70_000,
                liq=25_000,
                features_json="{}",
                image_rev="test",
            )
        )
        session.flush()
        gate = session.query(Decision).filter(Decision.token_id == tok.id, Decision.kind == "gate").one()
        feat = _v1_entry_features(session, tok.id, gate.id)
        assert feat.get("github_auth_n") == 0.9
        assert feat.get("real_project") == 1.0
        thesis = v1_thesis_from_features(feat)
        assert thesis["score"] > 0 and "github" in thesis["tags"]
        session.add(
            PaperFill(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                decision_id=gate.id,
                line="paper_v1",
                opened_at=now,
                entry_p=0.20,
                entry_mcap=70_000,
                entry_liq=25_000,
                target=2.0,
                ride=10.0,
                max_mcap=70_000,
                min_mcap=70_000,
                last_mcap=70_000,
                last_liq=25_000,
                status="queued",
                exit_reason=v1_queue_reason(0.55, day),
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        book = paper_v1_book(session, "sol", now=now)
        assert book["items"], "queued fill should appear on today's book"
        row = book["items"][0]
        assert float(row.get("thesis_score") or 0.0) > 0
        assert "github" in (row.get("tags") or [])


def test_paper_v1_review_buckets_picked_and_skipped():
    init_db()
    with session_scope() as session:
        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        day = "2026-09-27"
        for i, (status, reason) in enumerate(
            [
                ("open", f"d:{day}"),
                ("skipped", "v1 cap"),
                ("queued", v1_queue_reason(0.55, day)),
            ]
        ):
            tok = _token(f"RevwMint{i}11111111111111111111111111", p=0.20)
            session.add(tok)
            session.flush()
            session.add(
                Decision(
                    token_id=tok.id,
                    mint=tok.mint,
                    kind="entry",
                    chain="sol",
                    at=now,
                    entry_p=0.20,
                    entry_mcap=70_000,
                    liq=25_000,
                    features_json='{"github_auth_n":0.9,"real_project":1.0,"gmgn_cto":0.0,"name_quality":0.2}',
                    image_rev="test",
                )
            )
            session.flush()
            dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
            session.add(
                PaperFill(
                    chain="sol",
                    mint=tok.mint,
                    token_id=tok.id,
                    decision_id=dec.id,
                    line="paper_v1",
                    opened_at=now,
                    entry_p=0.20,
                    entry_mcap=70_000,
                    entry_liq=25_000,
                    target=2.0,
                    ride=10.0,
                    max_mcap=140_000 if status == "open" else 70_000,
                    min_mcap=70_000,
                    last_mcap=140_000 if status == "open" else 70_000,
                    last_liq=25_000,
                    status=status,
                    exit_reason=reason if status != "skipped" else f"v1 cap|d:{day}",
                    image_rev="test",
                    updated_at=now,
                )
            )
        session.flush()
        # Skipped book day is stamped via exit_reason for review filter.
        skip = session.query(PaperFill).filter(PaperFill.status == "skipped").one()
        skip.exit_reason = f"v1 cap|d:{day}"
        session.flush()
        out = paper_v1_review(session, "sol", day=day, now=now)
        assert out["day"] == day
        assert len(out["picked"]) == 1
        assert len(out["queued"]) == 1
        assert len(out["skipped"]) == 1
        assert "thesis" in out["picked"][0]["why"]
        assert out["picked"][0]["hit2x"] is True


def _label_row(session, chain, i, *, opened_at, status, reason):
    tok = _token(f"Scan{chain[:2]}{i:04d}111111111111111111111111111", p=0.20, chain=chain)
    session.add(tok)
    session.flush()
    session.add(
        PaperFill(
            chain=chain,
            mint=tok.mint,
            token_id=tok.id,
            line="paper_v1_shadow" if reason.startswith("v1 ") else "paper_v1",
            opened_at=opened_at,
            entry_p=0.20,
            entry_mcap=70_000,
            entry_liq=25_000,
            target=2.0,
            ride=10.0,
            max_mcap=70_000,
            min_mcap=70_000,
            last_mcap=70_000,
            last_liq=25_000,
            status=status,
            exit_reason=reason,
            image_rev="test",
            updated_at=opened_at,
        )
    )


def test_review_scan_is_bounded_by_chain_and_book_day_not_a_row_tail():
    """An RH day with hundreds of labels must not blank Sol or RH-yesterday."""
    init_db()
    with session_scope() as session:
        d0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        d1 = d0 + timedelta(days=1)
        # Yesterday (RH): 3 opens + 2 skips. Today (RH): 300 shadow labels.
        for i in range(3):
            _label_row(session, "robinhood", i, opened_at=d0, status="open", reason="d:2026-09-27")
        for i in range(3, 5):
            _label_row(session, "robinhood", i, opened_at=d0, status="skipped", reason="v1 cap|d:2026-09-27")
        for i in range(100, 400):
            _label_row(session, "robinhood", i, opened_at=d1, status="skipped", reason="v1 score-miss|d:2026-09-28")
        # Sol yesterday: one queued row written after the lock for 09-28, one live-cold shadow
        # written after midnight for 09-27.
        _label_row(session, "sol", 1, opened_at=d0.replace(hour=23, minute=30), status="queued", reason=v1_queue_reason(0.55, "2026-09-28"))
        _label_row(session, "sol", 2, opened_at=d1.replace(hour=0, minute=40), status="skipped", reason="v1 live-cold|d:2026-09-27")
        session.flush()
        rh_yesterday = paper_v1_review(session, "robinhood", day="2026-09-27", now=d1)
        assert len(rh_yesterday["picked"]) == 3 and len(rh_yesterday["skipped"]) == 2
        assert rh_yesterday["taken_chain"] == 3 and rh_yesterday["silence"] is None
        rh_today = paper_v1_review(session, "robinhood", day="2026-09-28", now=d1)
        assert len(rh_today["shadow"]) == 300 and rh_today["picked"] == []
        sol_yesterday = paper_v1_review(session, "sol", day="2026-09-27", now=d1)
        assert [r["skip_reason"] for r in sol_yesterday["shadow"]] == ["v1 live-cold"]
        assert sol_yesterday["queued"] == []
        sol_today = paper_v1_review(session, "sol", day="2026-09-28", now=d1)
        assert len(sol_today["queued"]) == 1 and sol_today["shadow"] == []
        # Other chain's rows never leak into a chain's buckets.
        assert all(r["chain"] == "sol" for r in sol_today["queued"] + sol_yesterday["shadow"])


def test_desk_shows_the_side_list():
    js = Path("launchfinder/static/desk.js").read_text()
    html = Path("launchfinder/static/desk.html").read_text()
    rh = Path("launchfinder/static/desk-rh.html").read_text()
    assert "paperV1" in js and "paper_v1" in js
    assert 'deskTab = "picks"' in js
    assert "/api/paper/v1" in js
    assert "/api/paper/v1/review" in js
    assert "/api/paper/v1/report" in js
    assert "renderLearnPane" in js
    assert "Shadow near-miss" in js and "skip_reason" in js
    assert "Paper miss autopsies" in js and "paper_misses" in js
    assert "fomo_trend_no_hunt" in js and "FOMO trending · not on Hunt" in js
    assert "FOMO separators" in js or "Separators ·" in js
    assert "would-have" in js and "skip_reasons_signal" in js
    assert "/api/paper/v1/miss-cohort" in js
    assert 'data-desk="picks"' in html and 'data-desk="learn"' in html
    assert 'data-desk="live"' in html and 'data-desk="board"' in html
    assert 'data-desk="fomo"' in html and 'data-desk="fomo"' in rh
    assert "Doing well" not in html and "Wide paper" not in html
    assert "Tickets" not in html and "Wallets" not in html
    assert "Classic" not in html and "Classic" not in rh
    assert "seg-version" not in html
    assert "CLASSIC_HREF" not in js
    assert "learn-pane" in html and "learn-pane" in rh
    assert (
        "stack-v207" in html or "stack-v206" in html or "stack-v205" in html or "stack-v204" in html or "stack-v203" in html
    ) and (
        "stack-v207" in rh or "stack-v206" in rh or "stack-v205" in rh or "stack-v204" in rh or "stack-v203" in rh
    )
    assert "Copycat vetoed winners" in js
    assert "Evidence #1" in js
    assert "gate_veto=copycat" in js
    assert "rh_hydrate_alarms" in js
    assert "/api/model/calibration?days=21" in js
    assert 'name === "fomo_trending"' in js
    assert "Paper · 1–5 / day · learn loop" in html or "paperV1" in html
    assert '["picks", "learn", "live", "fomo", "board"]' in js
    assert "cap_per_chain" in js and "signal rank" in js
    assert "/api/fomo-trending" in js and "FOMO trending" in js
    assert "renderFomoPane" in js and "fomo-mirror-banner" in js
    assert "learn-fomo-traders" in js
    assert "/api/fomo-alerts/traders" in js
    assert "/api/fomo-alerts/flow" in js
    assert "FOMO traders" in js

def test_leftover_reject_and_chain_mix_choose():
    from launchfinder.scoring.paper_v1 import v1_leftover_reject, choose_v1, PAPER_V1_LEFTOVER_MCAP

    assert PAPER_V1_LEFTOVER_MCAP == 1_000_000.0
    assert v1_leftover_reject(1_000_000) is True
    assert v1_leftover_reject(999_999) is False
    early = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 29, 11, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "rh_strong", "chain": "robinhood", "entry_p": 0.9, "live_p": 0.1, "opened_at": late, "thesis_score": 0.9},
        {"id": "rh_mid", "chain": "robinhood", "entry_p": 0.8, "live_p": 0.1, "opened_at": late, "thesis_score": 0.8},
        {"id": "sol_weak", "chain": "sol", "entry_p": 0.2, "live_p": 0.55, "opened_at": early, "thesis_score": 0.1},
    ]
    # Legacy shared-cap mix still reserves Sol when min_per_chain=1.
    picked = choose_v1(rows, already=0, cap=2, min_per_chain=1)
    assert "sol_weak" in picked
    assert len(picked) == 2


def test_meme_alone_does_not_soft_qualify():
    from launchfinder.scoring.paper_v1 import v1_thesis_ok, v1_qualifies, v1_thesis_from_features

    meme = v1_thesis_from_features({"github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0, "name_quality": 0.9})
    assert "meme" in meme["tags"] and not meme["hard_tags"]
    assert v1_thesis_ok(meme) is False
    assert v1_qualifies("robinhood", 0.32, None, hi=0.30, thesis=meme) is False
    hard = v1_thesis_from_features({"github_auth_n": 0.9, "real_project": 0.0, "gmgn_cto": 0.0, "name_quality": 0.2})
    assert v1_thesis_ok(hard) is True
    assert v1_qualifies("robinhood", 0.32, None, hi=0.30, thesis=hard) is True


def test_live_at_entry_freeze_is_write_once():
    from launchfinder.scoring.paper_v1 import merge_live_at_entry, read_live_at_entry
    from launchfinder.ledger import freeze_live_at_entry

    merged = merge_live_at_entry({}, 0.55, src="hunt_card")
    assert merged["live_p_at_entry"] == 0.55
    assert read_live_at_entry(merged)[0] == 0.55
    assert merge_live_at_entry(merged, 0.9) is None
    init_db()
    with session_scope() as session:
        tok = _token("FreezeLiveMint111111111111111111111111", p=0.20)
        session.add(tok)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind="entry",
                chain="sol",
                at=utcnow(),
                entry_p=0.20,
                entry_mcap=70_000,
                liq=25_000,
                features_json='{"buy_pressure":0.8,"holder_n":0.4}',
                image_rev="test",
            )
        )
        session.flush()
        assert freeze_live_at_entry(session, tok.id, 0.61, src="paper_qualify") is True
        assert freeze_live_at_entry(session, tok.id, 0.99, src="later") is False
        entry = session.query(Decision).filter(Decision.token_id == tok.id, Decision.kind == "entry").one()
        live, src = read_live_at_entry(__import__("json").loads(entry.features_json))
        assert live == 0.61 and src == "paper_qualify"


def test_runners_retro_exposes_miss_taxonomy():
    from launchfinder.scoring.runners_retro import paper_v1_runners_retro
    from launchfinder.scoring.miss_cohort import miss_cohort

    init_db()
    with session_scope() as session:
        out = paper_v1_runners_retro(session, "sol", limit=5)
        assert "by_miss" in out and "live_reconstructed" in out
        cohort = miss_cohort(session, "sol", days=7)
        assert "n_winners" in cohort and "tag_rate_winners" in cohort
        assert "signal_means_winners" in cohort
        assert "paper_misses" in cohort
        assert cohort["paper_misses"]["side_key"] == "paper_miss_join"
        assert cohort["paper_misses"]["fixture"]["mint"].startswith("7cYaQc21")
        assert cohort["paper_misses"]["would_have"]["open"] is False
        assert cohort["paper_misses"]["would_have"]["knob"] == "early_runner_book"
        assert cohort["paper_misses"]["fomo_trend_no_hunt"]["open"] is False
        assert cohort["paper_misses"]["fomo_trend_no_hunt"]["side_key"] == "fomo_trend_no_hunt"



# --- Cycle 4 knob: reconsider-on-paper-sync (SELF_IMPROVE_PLAN §M1) -------------


def _reconsider_fill(session, mint, *, now, chain="sol", p=0.20, thesis=True):
    """Wide fill opened Live-cold with no paperV1 row of any kind."""
    _live(session, chain, -2.0)  # sigmoid(-2) ~ 0.12: cold at the first liquid print
    tok = _token(mint, p=p, chain=chain, thesis=thesis)
    session.add(tok)
    session.flush()
    session.add(
        Decision(
            token_id=tok.id,
            mint=tok.mint,
            kind="entry",
            chain=chain,
            at=now - timedelta(minutes=5),
            entry_p=p,
            entry_mcap=70_000,
            liq=25_000,
            features_json='{"buy_pressure":0.8,"holder_n":0.4}',
            scorer=SCORER_FIRST_SIGHT,
            image_rev="test",
        )
    )
    session.flush()
    upsert_hunt(session, tok)
    assert sync_paper_ledger(session, chain, now=now)["opened"] == 1
    assert session.query(PaperFill).filter(PaperFill.line.in_(("paper_v1", "paper_v1_shadow"))).count() == 0
    return tok


def _v1_rows(session, mint):
    return (
        session.query(PaperFill)
        .filter(PaperFill.mint == mint, PaperFill.line.in_(("paper_v1", "paper_v1_shadow")))
        .order_by(PaperFill.id.asc())
        .all()
    )


def test_reconsider_queues_a_sol_name_once_live_warms():
    init_db()
    reset_artifact_cache()
    t_fill = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        tok = _reconsider_fill(session, "ReconWarm11111111111111111111111111111111", now=t_fill)
        # Still cold one tick later: nothing written, the name waits.
        out = reconsider_paper_v1(session, "sol", now=t_fill + timedelta(minutes=20))
        assert out == {"scanned": 1, "queued": 0, "opened": 0, "shadow": 0, "waiting": 1}
        assert _v1_rows(session, tok.mint) == []
        # Live warms to 0.50 inside the window: the unchanged qualify now passes.
        _live(session, "sol", 0.0)
        t_warm = t_fill + timedelta(hours=1)
        out = reconsider_paper_v1(session, "sol", now=t_warm)
        assert out["queued"] == 1 and out["shadow"] == 0
        (side,) = _v1_rows(session, tok.mint)
        assert side.line == "paper_v1" and side.status == "queued"
        assert side.exit_reason == "q:0.5000|2026-10-12"
        assert side.opened_at.replace(tzinfo=timezone.utc) == t_warm
        # Priced at the current sellable print, same decision as the wide fill.
        wide = session.query(PaperFill).filter(PaperFill.line == "gated90").one()
        assert side.decision_id == wide.decision_id and side.entry_mcap == 70_000
        # Idempotent: the mint has a row now, a later sync leaves it alone.
        again = reconsider_paper_v1(session, "sol", now=t_warm + timedelta(minutes=20))
        assert again["scanned"] == 0 and len(_v1_rows(session, tok.mint)) == 1
        # Live at entry keeps the first (fill-time) stamp — never overwritten.
        from launchfinder.scoring.paper_v1 import read_live_at_entry

        entry = session.query(Decision).filter(Decision.token_id == tok.id, Decision.kind == "entry").one()
        live, src = read_live_at_entry(__import__("json").loads(entry.features_json))
        assert live is not None and live < 0.2 and src == "live_model"
        # Wide book untouched: one gated90 row, still open.
        assert wide.status == "open"


def test_reconsider_stamps_live_when_the_fill_had_none():
    init_db()
    reset_artifact_cache()
    t_fill = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        tok = _reconsider_fill(session, "ReconStamp1111111111111111111111111111111", now=t_fill)
        entry = session.query(Decision).filter(Decision.token_id == tok.id, Decision.kind == "entry").one()
        import json

        feat = json.loads(entry.features_json)
        for key in ("live_p_at_entry", "live_at_entry_src", "live_at_entry_at"):
            feat.pop(key, None)
        entry.features_json = json.dumps(feat)
        session.flush()
        _live(session, "sol", 0.0)
        reconsider_paper_v1(session, "sol", now=t_fill + timedelta(hours=1))
        from launchfinder.scoring.paper_v1 import read_live_at_entry

        session.refresh(entry)
        live, src = read_live_at_entry(json.loads(entry.features_json))
        assert live == 0.5 and src == "paper_reconsider"


def test_reconsider_labels_live_cold_when_the_window_ends():
    from launchfinder.scoring.paper_v1 import PAPER_V1_RECONSIDER_HOURS

    init_db()
    reset_artifact_cache()
    t_fill = datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        tok = _reconsider_fill(session, "ReconCold11111111111111111111111111111111", now=t_fill)
        inside = t_fill + timedelta(hours=PAPER_V1_RECONSIDER_HOURS) - timedelta(minutes=1)
        assert reconsider_paper_v1(session, "sol", now=inside)["waiting"] == 1
        assert _v1_rows(session, tok.mint) == []
        # Window ends across midnight: the label lands on the fill's book day.
        after = t_fill + timedelta(hours=PAPER_V1_RECONSIDER_HOURS) + timedelta(minutes=1)
        out = reconsider_paper_v1(session, "sol", now=after)
        assert out["shadow"] == 1 and out["queued"] == 0
        (shadow,) = _v1_rows(session, tok.mint)
        assert shadow.line == "paper_v1_shadow" and shadow.status == "skipped"
        assert shadow.exit_reason == "v1 live-cold|d:2026-10-12"
        assert shadow.image_rev
        # Review buckets the label on that day; a second pass writes nothing.
        review = paper_v1_review(session, "sol", day="2026-10-12", now=after)
        assert len(review["shadow"]) == 1 and review["shadow"][0]["skip_reason"] == "v1 live-cold"
        assert review["silence"] is None
        assert paper_v1_review(session, "sol", day="2026-10-13", now=after)["shadow"] == []
        assert reconsider_paper_v1(session, "sol", now=after + timedelta(minutes=20))["scanned"] == 0
        assert len(_v1_rows(session, tok.mint)) == 1


def test_reconsider_labels_a_closed_fill_without_queueing_it():
    init_db()
    reset_artifact_cache()
    t_fill = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        tok = _reconsider_fill(session, "ReconClosed111111111111111111111111111111", now=t_fill)
        wide = session.query(PaperFill).filter(PaperFill.line == "gated90").one()
        wide.status = "closed"
        wide.exit_reason = "live dump"
        wide.closed_at = t_fill + timedelta(minutes=40)
        session.flush()
        _live(session, "sol", 1.0)  # warm now, but the wide line already exited
        out = reconsider_paper_v1(session, "sol", now=t_fill + timedelta(hours=1))
        assert out["queued"] == 0 and out["opened"] == 0 and out["shadow"] == 1
        (shadow,) = _v1_rows(session, tok.mint)
        assert shadow.line == "paper_v1_shadow" and shadow.exit_reason.startswith("v1 ")


def test_reconsider_refuses_a_late_chase_and_a_leftover():
    init_db()
    reset_artifact_cache()
    t_fill = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        chase = _reconsider_fill(session, "ReconChase1111111111111111111111111111111", now=t_fill, thesis=False)
        _live(session, "sol", 0.0)
        # Thin book ran 11× from t0 (above the 10× floor): refuse, label, never open.
        hunt = session.query(HuntCard).filter(HuntCard.mint == chase.mint).one()
        hunt.last_mcap = 69_000 * 11
        hunt.last_liq = 40_000
        chase.outcome.last_mcap = hunt.last_mcap
        session.flush()
        out = reconsider_paper_v1(session, "sol", now=t_fill + timedelta(hours=1))
        assert out["shadow"] == 1 and out["queued"] == 0
        (shadow,) = _v1_rows(session, chase.mint)
        assert shadow.exit_reason == "v1 late-chase|d:2026-10-12"
        assert session.query(PaperFill).filter(PaperFill.line == "paper_v1").count() == 0
        # A fat first print is a leftover, not a zero-hour short-list name.
        left = _token("ReconLeft11111111111111111111111111111111", p=0.20)
        left.outcome.t0_mcap = 1_200_000
        left.outcome.last_mcap = 1_250_000
        left.outcome.max_mcap = 1_250_000
        session.add(left)
        session.flush()
        session.add(
            PaperFill(
                chain="sol",
                mint=left.mint,
                token_id=left.id,
                line="gated90",
                opened_at=t_fill,
                entry_p=0.20,
                entry_mcap=1_250_000,
                entry_liq=40_000,
                target=2.0,
                ride=3.0,
                max_mcap=1_250_000,
                min_mcap=1_250_000,
                last_mcap=1_250_000,
                last_liq=40_000,
                status="open",
                image_rev="test",
                updated_at=t_fill,
            )
        )
        session.flush()
        out = reconsider_paper_v1(session, "sol", now=t_fill + timedelta(hours=1))
        assert out["shadow"] == 1
        (shadow,) = _v1_rows(session, left.mint)
        assert shadow.exit_reason == "v1 leftover|d:2026-10-12"


def test_reconsider_allows_a_quality_late_chase():
    import json

    init_db()
    reset_artifact_cache()
    t_fill = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        _promote(session, "sol")
        tok = _reconsider_fill(session, "ReconLateOk111111111111111111111111111111", now=t_fill)
        _live(session, "sol", 0.0)
        entry = session.query(Decision).filter(Decision.token_id == tok.id, Decision.kind == "entry").one()
        feat = json.loads(entry.features_json)
        feat.update({"github_auth_n": 0.9, "real_project": 1.0, "gmgn_cto": 0.0, "name_quality": 0.2})
        entry.features_json = json.dumps(feat)
        tok.research.features_json = json.dumps(feat)
        hunt = session.query(HuntCard).filter(HuntCard.mint == tok.mint).one()
        # 15× from a $69k t0 prints past $1M last — leftover is first-seen,
        # not leftover-of-the-print, once quality bypasses late-chase.
        hunt.last_mcap = 69_000 * 15
        hunt.last_liq = 40_000
        tok.outcome.last_mcap = hunt.last_mcap
        tok.outcome.max_mcap = hunt.last_mcap
        session.flush()
        out = reconsider_paper_v1(session, "sol", now=t_fill + timedelta(hours=1))
        assert out["queued"] == 1 and out["shadow"] == 0
        (side,) = _v1_rows(session, tok.mint)
        assert side.line == "paper_v1" and side.status == "queued"
        assert side.open_via == "late_ok"
        session.refresh(entry)
        stamped = json.loads(entry.features_json)
        assert stamped.get("paper_late_ok") is True
        assert "hard_thesis" in (stamped.get("paper_late_ok_why") or [])


def test_reconsider_is_sol_only_and_obeys_the_kill_switch():
    from launchfinder.risk import save_risk

    init_db()
    reset_artifact_cache()
    t_fill = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        rh = _token("ReconRH111111111111111111111111111111111", p=0.20, chain="robinhood")
        session.add(rh)
        session.flush()
        session.add(
            PaperFill(
                chain="robinhood",
                mint=rh.mint,
                token_id=rh.id,
                line="gated90",
                opened_at=t_fill,
                entry_p=0.20,
                entry_mcap=70_000,
                entry_liq=25_000,
                target=2.0,
                ride=3.0,
                max_mcap=70_000,
                min_mcap=70_000,
                last_mcap=70_000,
                last_liq=25_000,
                status="open",
                image_rev="test",
                updated_at=t_fill,
            )
        )
        session.flush()
        # RH qualifies on Entry; it is not in PAPER_V1_RECONSIDER_CHAINS.
        out = reconsider_paper_v1(session, "robinhood", now=t_fill + timedelta(hours=4))
        assert out == {"scanned": 0, "queued": 0, "opened": 0, "shadow": 0, "waiting": 0}
        assert _v1_rows(session, rh.mint) == []
        # Kill switch: no rows of any kind, like the lock.
        _promote(session, "sol")
        tok = _reconsider_fill(session, "ReconKill11111111111111111111111111111111", now=t_fill)
        save_risk(session, {"kill_switch": True, "note": "test"})
        out = reconsider_paper_v1(session, "sol", now=t_fill + timedelta(hours=4))
        assert out["scanned"] == 0 and _v1_rows(session, tok.mint) == []
        save_risk(session, {"kill_switch": False, "note": "test"})
        out = reconsider_paper_v1(session, "sol", now=t_fill + timedelta(hours=4))
        assert out["shadow"] == 1
        (shadow,) = _v1_rows(session, tok.mint)
        assert shadow.exit_reason == "v1 live-cold|d:2026-10-12"
