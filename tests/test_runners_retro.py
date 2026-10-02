"""Runners retrospective + year-winners template."""

from datetime import datetime, timezone
import json

from launchfinder.db import init_db, session_scope
from launchfinder.desk_lines import SCORER_FIRST_SIGHT
from launchfinder.models import Decision, Outcome, Research, Snapshot, Token
from launchfinder.scoring.paper_v1 import v1_peak_ok
from launchfinder.scoring.runners_retro import paper_v1_runners_retro
from launchfinder.scoring.year_winners import ALPHA_EXTRACT, curated_incredible, year_winners_template


def test_v1_peak_ok_threshold():
    assert v1_peak_ok(50_000, 100_000) is True
    assert v1_peak_ok(50_000, 99_999) is False
    assert v1_peak_ok(0, 100_000) is False


def test_runners_retro_and_year_winners():
    init_db()
    with session_scope() as session:
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        tok = Token(
            mint="RetroRunnerMint111111111111111111111",
            symbol="RTRUN",
            chain="robinhood",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
            is_historical=False,
        )
        session.add(tok)
        session.flush()
        session.add(
            Research(
                token=tok,
                features_json="{}",
                p_good=0.42,
                scorer=SCORER_FIRST_SIGHT,
                risk_flags_json="[]",
                holder_count=220,
            )
        )
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=40_000,
                max_mcap=280_000,
                last_mcap=200_000,
                last_liq=40_000,
                multiple=7.0,
                label=1,
            )
        )
        session.add(
            Snapshot(
                token_id=tok.id,
                kind="t0",
                mcap_usd=40_000,
                liquidity_usd=20_000,
                taken_at=now,
            )
        )
        session.add(
            Snapshot(
                token_id=tok.id,
                kind="t6h",
                mcap_usd=280_000,
                liquidity_usd=40_000,
                taken_at=now,
            )
        )
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind="entry",
                chain="robinhood",
                at=now,
                entry_p=0.42,
                entry_mcap=40_000,
                liq=20_000,
                features_json=json.dumps(
                    {
                        "name_quality": 0.8,
                        "github_auth_n": 0.0,
                        "real_project": 0.0,
                        "gmgn_cto": 0.0,
                    }
                ),
                image_rev="test",
            )
        )
        session.flush()
        retro = paper_v1_runners_retro(session, "robinhood", min_multiple=5.0, limit=20)
        assert retro["n"] >= 1
        hit = next(r for r in retro["items"] if r["mint"] == tok.mint)
        assert hit["would_pass_v1"] is True
        assert hit["path"] == "score"
        assert "meme" in hit["tags"]

        yw = year_winners_template(session, "robinhood", min_multiple=5.0, limit=20)
        assert yw["ledger_runners"]["n"] >= 1
        assert len(ALPHA_EXTRACT) >= 4
        assert any(c.get("symbol") == "POOF" for c in curated_incredible())
        assert "first-print" in (yw["horizon_note"] or "").lower() or "first" in (yw["how_to_use"] or "").lower()
