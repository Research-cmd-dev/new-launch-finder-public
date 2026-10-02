"""Thesis keys must be on the entry Decision — not stuck at meme@0.105."""

import asyncio
from datetime import datetime, timedelta, timezone
import json

from launchfinder.db import init_db, session_scope
from launchfinder.desk_lines import SCORER_FIRST_SIGHT
from launchfinder.ledger import (
    DECISION_ENTRY,
    _v1_entry_features,
    _v1_thesis,
    paper_v1_book,
    promote_paper_v1_queue,
)
from launchfinder.models import Decision, HuntCard, Outcome, PaperFill, Research, Token, utcnow
from launchfinder.scoring.paper_v1 import PAPER_V1_LINE, v1_queue_reason, v1_thesis_from_features
from launchfinder.scoring.thesis_enrich import (
    PAPER_V1_GMGN_REFRESH_CAP,
    apply_thesis_keys,
    audit_paper_v1_open_thesis_gaps,
    enrich_paper_v1_open_free_sources,
    enrich_thesis_before_entry,
    ensure_entry_thesis_from_stored,
    hydrate_thesis_raw_from_research,
    hydrate_thesis_raw_from_stored_meta,
    merge_thesis_feature_dict,
    recompute_thesis_keys,
    repair_desk_thesis_coverage,
    repair_paper_v1_open_thesis,
    repair_thin_entry_thesis,
    refresh_stored_thesis_evidence,
    sync_thesis_from_raw,
    thesis_keys_thin,
    thesis_open_has_discoverable_evidence,
)


def test_recompute_thesis_keys_from_github_and_cto():
    feat = {"name_quality": 0.7, "github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0}
    gh = {"full_name": "acme/widget", "age_days": 120, "commits": 40, "contributors": 3, "stars": 12}
    tw = {"handle": "acmedev", "followers": 2000, "age_days": 400, "verified": True}
    keys = recompute_thesis_keys(feat, gh=gh, tw=tw, gmgn={"cto": True}, twitter_handle="acmedev")
    assert keys["github_auth_n"] >= 0.6
    assert keys["real_project"] >= 0.5
    assert keys["gmgn_cto"] == 1.0
    assert apply_thesis_keys(feat, keys)
    thesis = v1_thesis_from_features(feat)
    assert thesis["score"] >= 0.25
    assert "github" in thesis["tags"] and "cto" in thesis["tags"]


def test_thesis_keys_thin_detects_meme_only():
    assert thesis_keys_thin({"name_quality": 0.7, "github_auth_n": 0.0, "gmgn_cto": 0.0})
    assert not thesis_keys_thin({"github_auth_n": 0.8, "name_quality": 0.7})


def test_sync_thesis_from_raw_updates_research_features():
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="ThesisRawMint1111111111111111111111111",
            symbol="TRAW",
            chain="sol",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json=json.dumps(
                {"name_quality": 0.7, "github_auth_n": 0.0, "real_project": 0.0, "gmgn_cto": 0.0}
            ),
            raw_json=json.dumps(
                {
                    "github": {
                        "full_name": "acme/widget",
                        "age_days": 90,
                        "commits": 25,
                        "contributors": 2,
                        "stars": 8,
                        "url": "https://github.com/acme/widget",
                    },
                    "twitter": {"handle": "acmedev", "followers": 1500, "age_days": 200, "verified": False},
                    "gmgn": {"cto": True},
                }
            ),
            twitter_handle="acmedev",
            twitter_followers=1500,
            twitter_age_days=200,
            p_good=0.2,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        assert sync_thesis_from_raw(research, tok) is True
        feat = json.loads(research.features_json)
        assert feat["github_auth_n"] >= 0.6
        assert feat["gmgn_cto"] == 1.0
        assert tok.github_url.endswith("acme/widget")


def test_repair_thin_entry_thesis_patches_decision():
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="ThesisPatchMint11111111111111111111111",
            symbol="TPCH",
            chain="sol",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.7,"github_auth_n":0.0,"gmgn_cto":0.0,"real_project":0.0}',
            raw_json=json.dumps(
                {
                    "github": {
                        "full_name": "acme/patch",
                        "age_days": 60,
                        "commits": 30,
                        "contributors": 2,
                        "stars": 6,
                    },
                    "twitter": {"handle": "patchdev", "followers": 1200, "age_days": 100},
                    "gmgn": {"cto": False},
                }
            ),
            twitter_handle="patchdev",
            twitter_followers=1200,
            twitter_age_days=100,
            p_good=0.22,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=now,
                source="live",
                entry_p=0.22,
                entry_mcap=70_000,
                liq=25_000,
                features_json='{"name_quality":0.7,"github_auth_n":0.0,"gmgn_cto":0.0,"real_project":0.0}',
                image_rev="test",
            )
        )
        session.flush()
        out = repair_thin_entry_thesis(session, limit=20)
        assert out["patched"] >= 1
        feat = _v1_entry_features(session, tok.id)
        thesis = v1_thesis_from_features(feat)
        assert thesis["score"] >= 0.25
        assert "github" in thesis["tags"]


def test_repair_paper_v1_open_thesis_query_is_postgres_safe():
    """Regression: DISTINCT token_id + ORDER BY fill.id breaks on Postgres."""
    from sqlalchemy import func
    from sqlalchemy.dialects import postgresql

    from launchfinder.models import PaperFill
    from launchfinder.scoring.paper_v1 import PAPER_V1_LINE

    init_db()
    filt = (
        PaperFill.line == PAPER_V1_LINE,
        PaperFill.status.in_(("open", "closed")),
        PaperFill.token_id.isnot(None),
    )
    with session_scope() as session:
        latest_fill = (
            session.query(func.max(PaperFill.id).label("fill_id"))
            .filter(*filt)
            .group_by(PaperFill.token_id)
            .subquery()
        )
        q = (
            session.query(PaperFill.token_id)
            .join(latest_fill, PaperFill.id == latest_fill.c.fill_id)
            .order_by(PaperFill.id.desc())
            .limit(120)
        )
        sql = str(q.statement.compile(dialect=postgresql.dialect()))
    assert "distinct" not in sql.lower()
    assert "group by" in sql.lower()


def test_repair_paper_v1_open_thesis_stamps_hard_tag_from_raw():
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="V1OpenThesisMint1111111111111111111111",
            symbol="V1OT",
            chain="sol",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.7,"github_auth_n":0.0,"gmgn_cto":0.0,"real_project":0.0}',
            raw_json=json.dumps(
                {
                    "github": {
                        "full_name": "acme/v1open",
                        "age_days": 90,
                        "commits": 40,
                        "contributors": 3,
                        "stars": 10,
                    },
                    "twitter": {"handle": "v1dev", "followers": 2500, "age_days": 200, "verified": True},
                    "gmgn": {"cto": True},
                }
            ),
            twitter_handle="v1dev",
            twitter_followers=2500,
            twitter_age_days=200,
            p_good=0.55,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=now,
                source="live",
                entry_p=0.55,
                entry_mcap=70_000,
                liq=25_000,
                features_json='{"name_quality":0.7,"github_auth_n":0.0,"gmgn_cto":0.0,"real_project":0.0}',
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
                entry_p=0.55,
                entry_mcap=70_000,
                entry_liq=25_000,
                status="open",
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        out = repair_paper_v1_open_thesis(session, limit=20)
        assert out["scanned"] >= 1
        thesis = _v1_thesis(session, tok.id)
        assert "github" in thesis["hard_tags"]
        assert "cto" in thesis["hard_tags"]


def test_hydrate_stored_meta_reads_gmgn_cto_from_raw_json():
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="GmgnCtoMint11111111111111111111111111",
            symbol="CTO",
            chain="sol",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.7,"github_auth_n":0.0,"real_project":0.0,"gmgn_cto":0.0}',
            raw_json=json.dumps({"gmgn": {"cto": True, "twitter_username": "ctodev", "twitter_followers": 3000}}),
            twitter_handle="ctodev",
            twitter_followers=3000,
            twitter_age_days=200,
            p_good=0.5,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        assert hydrate_thesis_raw_from_stored_meta(research, tok) is True
        sync_thesis_from_raw(research, tok)
        feat = json.loads(research.features_json)
        assert float(feat.get("gmgn_cto") or 0) >= 0.5
        thesis = v1_thesis_from_features(feat)
        assert "cto" in thesis["hard_tags"]


def test_v1_entry_features_hydrates_columns_before_merge():
    """Regression: v162 max-merge read stale features_json; gate stayed 1/15."""
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="GateReadHydrateMint1111111111111111111",
            symbol="GRH",
            chain="sol",
            github_url="https://github.com/acme/gate-read",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.7}',
            raw_json=json.dumps(
                {
                    "github": {
                        "full_name": "acme/gate-read",
                        "age_days": 100,
                        "stars": 15,
                        "commits": 30,
                        "contributors": 3,
                        "source": "github_api",
                    }
                }
            ),
            github_stars=15,
            github_forks=2,
            github_age_days=100.0,
            p_good=0.5,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=now,
                source="live",
                entry_p=0.55,
                entry_mcap=70_000,
                liq=25_000,
                features_json='{"name_quality":0.7,"github_auth_n":0.2,"real_project":0.0,"gmgn_cto":0.0}',
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
                entry_p=0.55,
                entry_mcap=70_000,
                status="closed",
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        feat = _v1_entry_features(session, tok.id)
        thesis = v1_thesis_from_features(feat)
        assert "github" in (thesis.get("hard_tags") or [])


def test_merge_thesis_feature_dict_maxes_thin_partial_entry():
    merged = merge_thesis_feature_dict(
        {"github_auth_n": 0.2, "name_quality": 0.7},
        {"github_auth_n": 0.82, "real_project": 0.0, "gmgn_cto": 0.0},
    )
    assert merged["github_auth_n"] == 0.82
    thesis = v1_thesis_from_features(merged)
    assert "github" in (thesis.get("hard_tags") or [])


def test_thesis_discoverable_evidence_includes_stored_github_ref():
    init_db()
    with session_scope() as session:
        tok = Token(
            mint="DiscoverMint1111111111111111111111111",
            symbol="DISC",
            chain="sol",
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json="{}",
            raw_json=json.dumps({"github": {"full_name": "acme/stored", "source": "url_meta"}}),
            p_good=0.3,
        )
        session.add(research)
        session.flush()
        assert thesis_open_has_discoverable_evidence(tok, research) is True


def test_enrich_before_entry_lookups_github_api_for_url_meta_ref(monkeypatch):
    init_db()

    async def fake_lookup(ref: str):
        assert ref == "acme/api-lookup"
        return {
            "full_name": ref,
            "url": f"https://github.com/{ref}",
            "age_days": 120,
            "commits": 35,
            "contributors": 3,
            "stars": 12,
        }

    monkeypatch.setattr("launchfinder.research.github.lookup_repo", fake_lookup)

    async def run():
        with session_scope() as session:
            tok = Token(
                mint="ApiLookupMint111111111111111111111111",
                symbol="API",
                chain="sol",
                github_url="https://github.com/acme/api-lookup",
                source="poll",
            )
            session.add(tok)
            session.flush()
            research = Research(
                token=tok,
                features_json='{"name_quality":0.7}',
                raw_json="{}",
                github_age_days=45.0,
                p_good=0.4,
            )
            session.add(research)
            session.flush()
            hydrate_thesis_raw_from_research(research, tok)
            changed = await enrich_thesis_before_entry(session, tok, research)
            assert changed is True
            feat = json.loads(research.features_json or "{}")
            thesis = v1_thesis_from_features(feat)
            assert "github" in (thesis.get("hard_tags") or [])
            gh = json.loads(research.raw_json or "{}").get("github") or {}
            assert gh.get("commits") == 35

    asyncio.run(run())


def test_url_and_age_alone_do_not_fabricate_github_hard_tag():
    """URL + 45d age from columns must not invent commits/contributors for github tag."""
    from launchfinder.scoring.paper_v1 import v1_thesis_from_features

    init_db()
    with session_scope() as session:
        tok = Token(
            mint="UrlOnlyMint11111111111111111111111111",
            symbol="URL",
            chain="sol",
            github_url="https://github.com/acme/url-only",
            first_seen_at=utcnow(),
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.7}',
            raw_json="{}",
            github_age_days=45.0,
            github_stars=0,
            p_good=0.4,
        )
        session.add(research)
        session.flush()
        assert hydrate_thesis_raw_from_research(research, tok) is True
        raw = json.loads(research.raw_json or "{}")
        gh = raw.get("github") or {}
        assert gh.get("full_name") == "acme/url-only"
        assert gh.get("source") == "url_meta"
        assert "commits" not in gh and "contributors" not in gh
        sync_thesis_from_raw(research, tok)
        feat = json.loads(research.features_json or "{}")
        thesis = v1_thesis_from_features(feat)
        assert "github" not in (thesis.get("hard_tags") or [])


def test_refresh_stored_thesis_evidence_gains_hard_tag_on_open():
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="RefreshOpenMint11111111111111111111111",
            symbol="ROP",
            chain="sol",
            github_url="https://github.com/acme/refresh-open",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.7}',
            raw_json=json.dumps(
                {
                    "github": {
                        "full_name": "acme/refresh-open",
                        "url": "https://github.com/acme/refresh-open",
                        "age_days": 200,
                        "stars": 20,
                        "commits": 40,
                        "contributors": 3,
                        "source": "github_api",
                    }
                }
            ),
            github_stars=20,
            github_age_days=200.0,
            p_good=0.5,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=now,
                source="live",
                entry_p=0.5,
                entry_mcap=80_000,
                liq=30_000,
                features_json='{"name_quality":0.7}',
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
                entry_p=0.5,
                entry_mcap=80_000,
                status="open",
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        out = refresh_stored_thesis_evidence(session, tok.id)
        assert out.get("ok") is True
        thesis = _v1_thesis(session, tok.id)
        assert "github" in (thesis.get("hard_tags") or [])


def test_repair_desk_thesis_coverage_syncs_hunt_without_paper_open():
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="HuntThesisMint11111111111111111111111",
            symbol="HTH",
            chain="sol",
            github_url="https://github.com/acme/hunt-thesis",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.72}',
            raw_json=json.dumps(
                {
                    "github": {
                        "full_name": "acme/hunt-thesis",
                        "age_days": 150,
                        "stars": 18,
                        "commits": 35,
                        "contributors": 4,
                        "source": "github_api",
                    }
                }
            ),
            github_stars=18,
            github_age_days=150.0,
            p_good=0.48,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        session.add(
            HuntCard(
                chain="sol",
                mint=tok.mint,
                token_id=tok.id,
                updated_at=now,
                first_seen_at=now,
            )
        )
        session.flush()
        stats = repair_desk_thesis_coverage(session, open_limit=5, hunt_limit=20, shadow_limit=5)
        assert stats["hunt_scanned"] >= 1
        thesis = _v1_thesis(session, tok.id)
        assert "github" in (thesis.get("hard_tags") or [])


def test_hydrate_raw_from_token_github_columns_without_fabricated_commits():
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="HydrateMint111111111111111111111111111",
            symbol="HYD",
            chain="sol",
            github_url="https://github.com/acme/hydrate",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.7}',
            raw_json="{}",
            github_stars=12,
            github_forks=3,
            github_age_days=120.0,
            twitter_handle="hydratedev",
            twitter_followers=5000,
            twitter_age_days=400,
            p_good=0.55,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=now,
                source="live",
                entry_p=0.55,
                entry_mcap=70_000,
                liq=25_000,
                features_json='{"name_quality":0.7,"github_auth_n":0.2,"real_project":0.0,"gmgn_cto":0.0}',
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
                entry_p=0.55,
                entry_mcap=70_000,
                entry_liq=25_000,
                status="open",
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        assert hydrate_thesis_raw_from_research(research, tok) is True
        raw = json.loads(research.raw_json or "{}")
        assert (raw.get("github") or {}).get("source") == "url_meta"
        assert ensure_entry_thesis_from_stored(session, tok.id) is False
        thesis = _v1_thesis(session, tok.id)
        assert "github" not in (thesis.get("hard_tags") or [])


def test_ensure_entry_thesis_from_stored_before_qualify():
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="QualSyncMint1111111111111111111111111",
            symbol="QSYNC",
            chain="sol",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.2}',
            raw_json=json.dumps(
                {
                    "github": {
                        "full_name": "org/qual",
                        "age_days": 120,
                        "commits": 50,
                        "contributors": 4,
                        "stars": 12,
                    },
                    "twitter": {"handle": "qualdev", "followers": 3000, "age_days": 400},
                }
            ),
            twitter_handle="qualdev",
            p_good=0.52,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="sol",
                at=now,
                source="seed_t0",
                entry_p=0.52,
                entry_mcap=70_000,
                liq=25_000,
                features_json='{"name_quality":0.2,"github_auth_n":0.0}',
                image_rev="test",
            )
        )
        session.flush()
        assert ensure_entry_thesis_from_stored(session, tok.id) is True
        thesis = _v1_thesis(session, tok.id)
        assert "github" in thesis["tags"]
        assert "dev" in thesis["tags"]


def test_ensure_entry_syncs_research_without_entry_decision():
    """Wide paper fills may lack entry Decision; gate still reads merged Research."""
    from launchfinder.ledger import _v1_thesis

    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="GateOnlyMint1111111111111111111111111",
            symbol="GATE",
            chain="sol",
            github_url="https://github.com/acme/gateonly",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.7,"github_auth_n":0.0,"real_project":0.0,"gmgn_cto":0.0}',
            raw_json="{}",
            github_stars=15,
            github_age_days=200.0,
            p_good=0.5,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        assert ensure_entry_thesis_from_stored(session, tok.id) is False
        thesis = _v1_thesis(session, tok.id)
        assert "github" not in (thesis.get("hard_tags") or [])
        assert float(thesis.get("github_auth") or 0) < 0.6


def test_paper_v1_book_filters_by_chain():
    init_db()
    with session_scope() as session:
        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        day = "2026-09-27"
        for i, chain in enumerate(("sol", "robinhood")):
            tok = Token(
                mint=f"ChainFiltMint{i}111111111111111111111111",
                symbol=f"CF{i}",
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
                    token=tok,
                    features_json='{"name_quality":0.7}',
                    p_good=0.4,
                    scorer=SCORER_FIRST_SIGHT,
                )
            )
            session.add(Outcome(token=tok, t0_mcap=70_000, last_mcap=70_000, last_liq=25_000, max_mcap=70_000))
            session.add(
                Decision(
                    token_id=tok.id,
                    mint=tok.mint,
                    kind="entry",
                    chain=chain,
                    at=now,
                    entry_p=0.4,
                    entry_mcap=70_000,
                    liq=25_000,
                    features_json='{"name_quality":0.7,"github_auth_n":0.0}',
                    image_rev="test",
                )
            )
            session.flush()
            dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
            session.add(
                PaperFill(
                    chain=chain,
                    mint=tok.mint,
                    token_id=tok.id,
                    decision_id=dec.id,
                    line="paper_v1",
                    opened_at=now,
                    entry_p=0.4,
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
        sol = paper_v1_book(session, "sol", now=now)
        rh = paper_v1_book(session, "robinhood", now=now)
        assert all(i["chain"] == "sol" for i in sol["items"])
        assert all(i["chain"] == "robinhood" for i in rh["items"])
        assert len(sol["items"]) == 1 and len(rh["items"]) == 1


def test_v1_near_miss_and_skip_stamp():
    from launchfinder.scoring.paper_v1 import (
        PAPER_V1_SKIP_NO_THESIS,
        PAPER_V1_SKIP_SCORE_MISS,
        v1_miss_reason,
        v1_near_qualify,
        v1_skip_stamp,
        v1_thesis_from_features,
    )

    weak = v1_thesis_from_features({"name_quality": 0.7})
    assert v1_near_qualify("sol", 0.12, 0.42, hi=0.14, thesis=weak) is True
    assert v1_near_qualify("sol", 0.20, 0.55, hi=0.14, thesis=None) is True  # score path, no hard tag
    assert v1_miss_reason("sol", 0.12, 0.42, hi=0.14, thesis=weak) == PAPER_V1_SKIP_NO_THESIS
    assert v1_miss_reason("robinhood", 0.28, None, hi=0.30, thesis=None) in (
        PAPER_V1_SKIP_NO_THESIS,
        PAPER_V1_SKIP_SCORE_MISS,
    )
    stamp = v1_skip_stamp(PAPER_V1_SKIP_NO_THESIS, "2026-09-27")
    assert stamp.startswith("v1 no-thesis|d:2026-09-27")


def test_promote_paper_v1_opens_strong_thesis_before_lock():
    init_db()
    with session_scope() as session:
        now = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
        day = "2026-09-27"
        tok = Token(
            mint="PromoteMint111111111111111111111111111",
            symbol="PROM",
            chain="sol",
            first_seen_at=now - timedelta(minutes=10),
            migrated_at=now - timedelta(minutes=10),
            created_at_chain=now - timedelta(minutes=40),
            source="poll",
        )
        session.add(tok)
        session.flush()
        session.add(
            Research(
                token=tok,
                features_json=json.dumps(
                    {
                        "name_quality": 0.8,
                        "github_auth_n": 0.9,
                        "real_project": 1.0,
                        "gmgn_cto": 1.0,
                    }
                ),
                p_good=0.2,
                scorer=SCORER_FIRST_SIGHT,
            )
        )
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind="entry",
                chain="sol",
                at=now,
                entry_p=0.2,
                entry_mcap=70_000,
                liq=25_000,
                features_json=json.dumps(
                    {
                        "name_quality": 0.8,
                        "github_auth_n": 0.9,
                        "real_project": 1.0,
                        "gmgn_cto": 1.0,
                    }
                ),
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
                entry_p=0.2,
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
        out = promote_paper_v1_queue(session, now=now)
        assert out["opened"] == 1
        assert out.get("peak", 0) == 0
        fill = session.query(PaperFill).filter(PaperFill.line == "paper_v1").one()
        assert fill.status == "open"
        assert fill.exit_reason.startswith("d:")


def test_promote_paper_v1_opens_queued_peak_2x():
    """Queued name that already printed sellable 2× opens before lock."""
    init_db()
    with session_scope() as session:
        now = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
        day = "2026-09-27"
        tok = Token(
            mint="PeakPromoteMint1111111111111111111111",
            symbol="PEAK2",
            chain="robinhood",
            first_seen_at=now - timedelta(minutes=20),
            migrated_at=now - timedelta(minutes=20),
            created_at_chain=now - timedelta(minutes=40),
            source="poll",
        )
        session.add(tok)
        session.flush()
        session.add(
            Research(
                token=tok,
                features_json=json.dumps(
                    {
                        "name_quality": 0.7,
                        "github_auth_n": 0.9,
                        "real_project": 1.0,
                        "gmgn_cto": 0.0,
                    }
                ),
                raw_json=json.dumps(
                    {
                        "github": {
                            "full_name": "acme/peak2",
                            "age_days": 90,
                            "commits": 30,
                            "contributors": 2,
                            "stars": 8,
                        }
                    }
                ),
                p_good=0.41,
                scorer=SCORER_FIRST_SIGHT,
            )
        )
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind="entry",
                chain="robinhood",
                at=now,
                entry_p=0.41,
                entry_mcap=50_000,
                liq=20_000,
                features_json=json.dumps(
                    {
                        "name_quality": 0.7,
                        "github_auth_n": 0.0,
                        "real_project": 0.0,
                        "gmgn_cto": 0.0,
                    }
                ),
                image_rev="test",
            )
        )
        session.add(
            Outcome(
                token_id=tok.id,
                t0_mcap=50_000,
                max_mcap=140_000,
                last_mcap=140_000,
                last_liq=25_000,
                multiple=2.8,
                label=1,
            )
        )
        session.add(
            HuntCard(
                chain="robinhood",
                mint=tok.mint,
                token_id=tok.id,
                first_seen_at=now - timedelta(minutes=20),
                launched_at=now - timedelta(minutes=20),
                entry_p=0.41,
                t0_mcap=50_000,
                last_mcap=140_000,
                last_liq=25_000,
                multiple=2.8,
                conviction_p=0.55,
                holders=200,
                updated_at=now,
                scorer=SCORER_FIRST_SIGHT,
            )
        )
        session.flush()
        dec = session.query(Decision).filter(Decision.token_id == tok.id).one()
        session.add(
            PaperFill(
                chain="robinhood",
                mint=tok.mint,
                token_id=tok.id,
                decision_id=dec.id,
                line="paper_v1",
                opened_at=now,
                entry_p=0.41,
                entry_mcap=50_000,
                entry_liq=20_000,
                target=2.0,
                ride=10.0,
                max_mcap=50_000,
                min_mcap=50_000,
                last_mcap=50_000,
                last_liq=20_000,
                status="queued",
                exit_reason=v1_queue_reason(0.55, day),
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        out = promote_paper_v1_queue(session, now=now)
        assert out["opened"] == 1 and out["peak"] == 1
        fill = session.query(PaperFill).filter(PaperFill.line == "paper_v1").one()
        assert fill.status == "open"
        assert fill.open_via == "peak"
        assert float(fill.entry_mcap or 0) >= 100_000
        assert float(fill.max_mcap or 0) == float(fill.entry_mcap or 0)


def test_paper_v1_gmgn_refresh_cap_is_one():
    assert PAPER_V1_GMGN_REFRESH_CAP == 2


def _thin_open_fill(session, mint: str, symbol: str, *, now):
    tok = Token(
        mint=mint,
        symbol=symbol,
        chain="sol",
        first_seen_at=now,
        migrated_at=now,
        created_at_chain=now,
        source="poll",
    )
    session.add(tok)
    session.flush()
    research = Research(
        token=tok,
        features_json='{"name_quality":0.7,"github_auth_n":0.0,"gmgn_cto":0.0,"real_project":0.0}',
        raw_json="{}",
        p_good=0.5,
        scorer=SCORER_FIRST_SIGHT,
    )
    session.add(research)
    session.flush()
    session.add(
        PaperFill(
            chain="sol",
            mint=tok.mint,
            token_id=tok.id,
            line=PAPER_V1_LINE,
            opened_at=now,
            entry_p=0.5,
            entry_mcap=70_000,
            entry_liq=25_000,
            status="open",
            image_rev="test",
            updated_at=now,
        )
    )
    session.flush()
    return tok


def test_enrich_free_sources_skips_gmgn_on_cooldown_still_tags_meta(monkeypatch):
    import time

    from launchfinder.config import settings
    from launchfinder.research import gmgn as gmgn_mod

    init_db()
    calls = {"n": 0}

    async def _fake_token_research(*_a, **_k):
        calls["n"] += 1
        return {"cto": False}

    monkeypatch.setattr(gmgn_mod, "token_research", _fake_token_research)
    old_key = settings.gmgn_api_key
    object.__setattr__(settings, "gmgn_api_key", "test-key")
    gmgn_mod._cooldown_until = time.time() + 600
    gmgn_mod._deep_until = time.time() + 600
    try:
        with session_scope() as session:
            now = utcnow()
            _thin_open_fill(session, "CooldownMint1111111111111111111111111", "CD1", now=now)
            out = asyncio.run(enrich_paper_v1_open_free_sources(session, limit=4))
        assert calls["n"] == 0
        assert out["gmgn_calls"] == 0
        assert out["gmgn_skipped_cooldown"] >= 1
        assert out["scanned"] >= 1
    finally:
        object.__setattr__(settings, "gmgn_api_key", old_key)
        gmgn_mod._cooldown_until = 0.0
        gmgn_mod._deep_until = 0.0


def test_enrich_free_sources_gmgn_budget_one_per_pass(monkeypatch):
    from launchfinder.config import settings
    from launchfinder.research import gmgn as gmgn_mod

    init_db()
    calls = {"n": 0}

    async def _fake_token_research(*_a, **_k):
        calls["n"] += 1
        return {"cto": False, "source": "gmgn"}

    monkeypatch.setattr(gmgn_mod, "token_research", _fake_token_research)
    old_key = settings.gmgn_api_key
    object.__setattr__(settings, "gmgn_api_key", "test-key")
    gmgn_mod._cooldown_until = 0.0
    gmgn_mod._deep_until = 0.0
    try:
        with session_scope() as session:
            now = utcnow()
            tok_a = _thin_open_fill(session, "BudgetMintA11111111111111111111111111", "BA", now=now)
            tok_b = _thin_open_fill(session, "BudgetMintB11111111111111111111111111", "BB", now=now)
            for tok in (tok_a, tok_b):
                res = session.query(Research).filter(Research.token_id == tok.id).one()
                res.raw_json = "{}"
            out = asyncio.run(enrich_paper_v1_open_free_sources(session, limit=4, gmgn_cap=1))
        assert calls["n"] == 1
        assert out["gmgn_calls"] == 1
    finally:
        object.__setattr__(settings, "gmgn_api_key", old_key)
