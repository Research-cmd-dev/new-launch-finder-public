"""Sanity jobs: runner thesis repair + Live@entry freeze (write-once)."""

import json

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import DECISION_ENTRY, features_hash
from launchfinder.models import Decision, HuntCard, Outcome, Research, Token, utcnow
from launchfinder.scoring.paper_v1 import LIVE_AT_ENTRY_KEY, read_live_at_entry
from launchfinder.scoring.sanity_jobs import (
    apply_sanity_jobs,
    freeze_live_coverage_batch,
    repair_runners_thin_thesis,
)
from launchfinder.scoring.thesis_enrich import thesis_keys_thin


def _token(session, mint: str, *, chain: str = "sol"):
    now = utcnow()
    tok = Token(
        mint=mint,
        symbol="SN",
        chain=chain,
        first_seen_at=now,
        migrated_at=now,
        created_at_chain=now,
        source="poll",
    )
    session.add(tok)
    session.flush()
    session.add(
        Research(
            token_id=tok.id,
            p_good=0.2,
            features_json=json.dumps({"name_quality": 0.7}),
            raw_json=json.dumps(
                {
                    "github": {
                        "full_name": "acme/sn",
                        "age_days": 120,
                        "commits": 40,
                        "contributors": 3,
                        "stars": 12,
                    },
                    "twitter": {"handle": "acmedev", "followers": 3000, "age_days": 400, "verified": True},
                    "gmgn": {"cto": True},
                }
            ),
            risk_flags_json="[]",
        )
    )
    session.add(
        Outcome(
            token_id=tok.id,
            t0_mcap=70_000,
            max_mcap=500_000,
            last_mcap=200_000,
            last_liq=20_000,
            multiple=7.0,
            label=1,
        )
    )
    return tok


def test_repair_runners_thin_thesis_patches_from_raw():
    init_db()
    with session_scope() as session:
        tok = _token(session, "SnRepairMint1111111111111111111111111")
        feat = {"name_quality": 0.7, "github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0}
        assert thesis_keys_thin(feat)
        blob = json.dumps(feat)
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=utcnow(),
                source="live",
                entry_p=0.2,
                entry_mcap=70_000,
                liq=20_000,
                features_json=blob,
                features_hash=features_hash(blob),
                image_rev="test",
            )
        )
        session.flush()
        out = repair_runners_thin_thesis(session, "sol", limit=20)
        assert out["patched"] >= 1
        dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
        patched = json.loads(dec.features_json)
        assert not thesis_keys_thin(patched)
        assert float(patched.get("github_auth_n") or 0) >= 0.05


def test_freeze_live_coverage_write_once_from_hunt():
    init_db()
    with session_scope() as session:
        tok = _token(session, "SnFreezeMint1111111111111111111111111")
        blob = json.dumps({"name_quality": 0.7})
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=utcnow(),
                source="live",
                entry_p=0.2,
                entry_mcap=70_000,
                liq=20_000,
                features_json=blob,
                features_hash=features_hash(blob),
                image_rev="test",
            )
        )
        session.add(
            HuntCard(
                token_id=tok.id,
                mint=tok.mint,
                chain="sol",
                entry_p=0.2,
                conviction_p=0.62,
                last_mcap=80_000,
                updated_at=utcnow(),
            )
        )
        session.flush()
        out = freeze_live_coverage_batch(session, "sol", limit=20)
        assert out["frozen"] >= 1
        dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
        feat = json.loads(dec.features_json)
        live, src = read_live_at_entry(feat)
        assert live == 0.62
        assert src == "hunt_card"
        # Second pass must not overwrite.
        out2 = freeze_live_coverage_batch(session, "sol", limit=20)
        assert out2["frozen"] == 0
        assert feat.get(LIVE_AT_ENTRY_KEY) == 0.62


def test_apply_sanity_jobs_runs_both():
    import asyncio

    init_db()
    with session_scope() as session:
        out = asyncio.run(apply_sanity_jobs(session, "sol", actions=["repair_thin_thesis", "freeze_live_coverage"]))
        assert "repair_thin_thesis" in out["applied"]
        assert "freeze_live_coverage" in out["applied"]


def test_apply_sanity_jobs_one_knob_freeze_only():
    import asyncio

    init_db()
    with session_scope() as session:
        out = asyncio.run(apply_sanity_jobs(session, "sol", actions=["freeze_live_coverage"]))
        assert out["applied"] == ["freeze_live_coverage"]
        assert "repair_thin_thesis" in out["skipped"]
        assert "enrich_thin_thesis_http" in out["skipped"]
