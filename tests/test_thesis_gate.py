"""Production-gate thesis inventory + repair."""

from launchfinder.db import init_db, session_scope
from launchfinder.desk_lines import SCORER_FIRST_SIGHT
from launchfinder.ledger import DECISION_ENTRY
from launchfinder.models import Decision, PaperFill, Research, Token, utcnow
from launchfinder.scoring.paper_v1 import PAPER_V1_LINE
from launchfinder.scoring.thesis_gate import thesis_gate_counts, thesis_gate_inventory


def test_thesis_gate_inventory_classifies_meme_only():
    init_db()
    with session_scope() as session:
        now = utcnow()
        tok = Token(
            mint="GateMemeMint1111111111111111111111111",
            symbol="GMEME",
            chain="robinhood",
            first_seen_at=now,
            migrated_at=now,
            created_at_chain=now,
            source="poll",
        )
        session.add(tok)
        session.flush()
        research = Research(
            token=tok,
            features_json='{"name_quality":0.72,"github_auth_n":0.0,"real_project":0.0,"gmgn_cto":0.0}',
            raw_json="{}",
            p_good=0.4,
            scorer=SCORER_FIRST_SIGHT,
        )
        session.add(research)
        session.flush()
        session.add(
            Decision(
                token_id=tok.id,
                mint=tok.mint,
                kind=DECISION_ENTRY,
                chain="robinhood",
                at=now,
                source="live",
                entry_p=0.35,
                entry_mcap=80_000,
                liq=25_000,
                features_json='{"name_quality":0.72,"github_auth_n":0.0,"real_project":0.0,"gmgn_cto":0.0}',
                image_rev="test",
            )
        )
        session.add(
            PaperFill(
                chain="robinhood",
                mint=tok.mint,
                token_id=tok.id,
                line=PAPER_V1_LINE,
                opened_at=now,
                entry_p=0.35,
                entry_mcap=80_000,
                status="closed",
                image_rev="test",
                updated_at=now,
            )
        )
        session.flush()
        n, tagged = thesis_gate_counts(session)
        assert n == 1
        assert tagged == 0
        inv = thesis_gate_inventory(session)
        assert inv["n_fills"] == 1
        assert inv["rows"][0]["class"] == "meme_only_no_stored_hard_evidence"
        assert inv["rows"][0]["hydrate_sync_on_read"] is True
