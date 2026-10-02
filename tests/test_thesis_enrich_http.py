"""HTTP enrich for thin runners — patches Decision after website/GitHub lookup."""

from __future__ import annotations

import asyncio
import json

from launchfinder.db import init_db, session_scope
from launchfinder.ledger import DECISION_ENTRY, features_hash
from launchfinder.models import Decision, Outcome, Research, Token, utcnow
from launchfinder.ledger import _v1_thesis
from launchfinder.scoring.paper_v1 import PAPER_V1_LINE
from launchfinder.scoring.thesis_enrich import (
    enrich_paper_v1_open_thin_thesis_http,
    enrich_runners_thin_thesis,
    thesis_keys_thin,
)
from launchfinder.models import PaperFill


def test_enrich_runners_thin_thesis_http_patches(monkeypatch):
    init_db()

    async def fake_lookup(ref):
        return {
            "full_name": "acme/runner",
            "url": "https://github.com/acme/runner",
            "age_days": 200,
            "commits": 50,
            "contributors": 4,
            "stars": 20,
            "forks": 2,
        }

    async def fake_discover(token, *, allow_website_fetch=True, website=None):
        return "https://github.com/acme/runner", "acme/runner"

    monkeypatch.setattr("launchfinder.research.github.lookup_repo", fake_lookup)
    monkeypatch.setattr(
        "launchfinder.scoring.thesis_enrich.discover_github_url",
        fake_discover,
    )

    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="EnrichMint111111111111111111111111111",
            symbol="ENR",
            chain="sol",
            website="https://acme.example",
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
                features_json=json.dumps(
                    {"name_quality": 0.7, "github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0}
                ),
                raw_json="{}",
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
        feat = {"name_quality": 0.7, "github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0}
        blob = json.dumps(feat)
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=now,
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
        assert thesis_keys_thin(feat)
        out = asyncio.run(enrich_runners_thin_thesis(session, "sol", limit=5, website_budget=4))
        assert out["patched"] >= 1 or out["enriched"] >= 1
        dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
        patched = json.loads(dec.features_json)
        assert not thesis_keys_thin(patched)
        assert float(patched.get("github_auth_n") or 0) >= 0.05


def test_enrich_paper_v1_open_thin_thesis_http_tags_open_fill(monkeypatch):
    init_db()

    async def fake_lookup(ref):
        return {
            "full_name": "acme/paperv1",
            "url": "https://github.com/acme/paperv1",
            "age_days": 200,
            "commits": 50,
            "contributors": 4,
            "stars": 20,
            "forks": 2,
        }

    async def fake_discover(token, *, allow_website_fetch=True, website=None):
        return "https://github.com/acme/paperv1", "acme/paperv1"

    monkeypatch.setattr("launchfinder.research.github.lookup_repo", fake_lookup)
    monkeypatch.setattr(
        "launchfinder.scoring.thesis_enrich.discover_github_url",
        fake_discover,
    )

    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="PaperV1HttpMint11111111111111111111111",
            symbol="PV1H",
            chain="sol",
            website="https://pv1.example",
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
                p_good=0.52,
                features_json=json.dumps(
                    {"name_quality": 0.7, "github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0}
                ),
                raw_json="{}",
                risk_flags_json="[]",
            )
        )
        feat = {"name_quality": 0.7, "github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0}
        blob = json.dumps(feat)
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=now,
                source="live",
                entry_p=0.52,
                entry_mcap=70_000,
                liq=25_000,
                features_json=blob,
                features_hash=features_hash(blob),
                image_rev="test",
            )
        )
        session.add(
            PaperFill(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                line=PAPER_V1_LINE,
                opened_at=now,
                entry_p=0.52,
                entry_mcap=70_000,
                entry_liq=25_000,
                status="open",
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        out = asyncio.run(enrich_paper_v1_open_thin_thesis_http(session, limit=5, website_budget=4))
        assert out["scanned"] >= 1
        assert out["tagged"] >= 1 or out["patched"] >= 1
        thesis = _v1_thesis(session, tok.id)
        assert "github" in (thesis.get("hard_tags") or [])
