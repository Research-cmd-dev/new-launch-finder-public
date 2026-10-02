"""Holder-rewards board is a side flag, not a score input."""

from datetime import timedelta

from launchfinder.db import init_db, session_scope
from launchfinder.models import Research, Token, utcnow
from launchfinder.research.holder_rewards import lookup, parse_board, set_board_for_tests
from launchfinder.scoring.features import FEATURE_NAMES
from launchfinder.serialize import token_card

SOL = "So11111111111111111111111111111111111111112"
MEME = "FLWSojG1gB5VStYR3Sb4nQFRt43UBYkqih1j2CpVLqgd"
COIN = "HoK7vodcoVYQepStM1dDVyX5iMp2q5narY9bammuGPpe"
SOL_COIN = "B" * 40

BOARD = (
    rf'\"mint\":\"{COIN}\",\"name\":\"Tulip Coin\",\"symbol\":\"TULIP\",'
    rf'\"quoteMint\":\"{MEME}\",\"rewards\":{{\"status\":\"ok\",\"distributedUsd\":539863.12}}'
    rf'\"mint\":\"{SOL_COIN}\",\"name\":\"Sol Pair\",\"symbol\":\"SOLP\",'
    rf'\"quoteMint\":\"{SOL}\",\"rewards\":{{\"status\":\"ok\",\"distributedUsd\":1200.5}}'
    rf'\"quoteAssets\":{{\"{MEME}\":{{\"mint\":\"{MEME}\",\"symbol\":\"FLWS\"}},'
    rf'\"{SOL}\":{{\"mint\":\"{SOL}\",\"symbol\":\"SOL\"}}}}'
)


def test_parse_marks_a_meme_pair_and_a_sol_pair():
    rows = parse_board(BOARD)
    assert rows[COIN]["distributed_usd"] == 539863.12
    assert rows[COIN]["meme_pair"] is True
    assert rows[COIN]["quote_symbol"] == "FLWS"
    assert rows[SOL_COIN]["meme_pair"] is False
    assert rows[SOL_COIN]["quote_symbol"] == "SOL"
    assert parse_board("") == {}


def test_card_carries_the_flag_and_the_score_vector_stays_66():
    assert len(FEATURE_NAMES) == 66
    set_board_for_tests({COIN: {"distributed_usd": 1000.0, "quote_mint": MEME, "quote_symbol": "FLWS", "meme_pair": True}})
    assert lookup(COIN)["meme_pair"] is True
    init_db()
    now = utcnow()
    with session_scope() as session:
        tok = Token(mint=COIN, symbol="TULIP", chain="sol", first_seen_at=now - timedelta(hours=1), migrated_at=now - timedelta(hours=1), source="poll")
        tok.research = Research(features_json="{}", p_good=0.2, holder_count=40)
        session.add(tok)
        session.flush()
        card = token_card(tok)
    assert card["holder_rewards"]["distributed_usd"] == 1000.0
    assert card["holder_rewards"]["meme_pair"] is True
    assert "holder_rewards" not in (card.get("risk_flags") or [])
    set_board_for_tests({})
