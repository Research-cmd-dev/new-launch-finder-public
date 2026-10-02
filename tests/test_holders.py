import asyncio
import json

from launchfinder.db import SessionLocal, init_db
from launchfinder.models import Research, Token, utcnow
from launchfinder.research.holders import (
    append_holder_history,
    apply_rh_holder_meta,
    holder_tape,
    holders_from_blockscout,
    holders_from_gmgn,
    holders_snapshot_age_sec,
    hydrate_rh_wallet_map,
    rh_wallet_map_missing,
    _fresh_share,
)


def test_fresh_share_ignores_pool():
    wallets = [
        {"owner": "a", "fresh": True, "label": ""},
        {"owner": "b", "fresh": False, "label": ""},
        {"owner": "pool", "fresh": True, "label": "pool"},
    ]
    assert _fresh_share(wallets) == 50.0


def test_holders_from_blockscout_maps_top_wallets_and_skips_lp():
    # Live THEINVESTOR: LP contract holds ~81.5%; GMGN top10 is ~11% of
    # total supply (EOAs only in the numerator).
    supply = 1_000_000_000 * 10**18
    lp = 815_841_679_379_596_356_482_196_985
    eoa1 = 12_937_275_628_206_290_687_058_010
    eoa2 = 11_302_617_582_398_713_917_863_523
    creator = "0x90bd000000000000000000000000000000000001"
    token = {
        "holders_count": "68",
        "total_supply": str(supply),
    }
    holders = {
        "items": [
            {
                "address": {
                    "hash": "0x8366a39CC670B4001A1121B8F6A443A643e40951",
                    "is_contract": True,
                },
                "value": str(lp),
            },
            {
                "address": {"hash": "0x4fe6C9D986D30791d87692490a4c0C280ad90ABE", "is_contract": False},
                "value": str(eoa1),
            },
            {
                "address": {"hash": "0x2D3D805517Ae175153A3166b915b6AE9d32f509a", "is_contract": False},
                "value": str(eoa2),
            },
            {
                "address": {"hash": creator, "is_contract": False},
                "value": "0",
            },
        ]
    }
    out = holders_from_blockscout(token, holders, creator=creator)
    assert out["source"] == "blockscout"
    assert out["holder_count"] == 68
    assert out["top_wallets"][0]["label"] == "pool"
    assert out["top_wallets"][1]["owner"] == "0x4fe6C9D986D30791d87692490a4c0C280ad90ABE"
    assert out["top1_pct"] == round(eoa1 / supply * 100.0, 2)
    assert out["top10_pct"] == round((eoa1 + eoa2) / supply * 100.0, 2)
    assert out["creator_hold_pct"] == 0.0
    assert 1.0 < out["top10_pct"] < 20.0


def test_hydrate_rh_wallet_map_writes_blockscout_rows(monkeypatch):
    init_db()
    session = SessionLocal()
    token = Token(
        mint="0xac292fda653018e459d69db1c88f11565e551e18",
        symbol="THEINVESTOR",
        chain="robinhood",
        source="rh_trenches",
        creator="0x90bd000000000000000000000000000000000001",
        first_seen_at=utcnow(),
        migrated_at=utcnow(),
    )
    token.research = Research(
        p_good=0.92,
        heuristic_p=0.92,
        holder_count=98,
        top10_pct=10.98,
        features_json="{}",
        raw_json=json.dumps(
            {
                "holders": {
                    "holder_count": 98,
                    "top10_pct": 10.98,
                    "source": "gmgn",
                }
            }
        ),
    )
    session.add(token)
    session.commit()
    assert rh_wallet_map_missing(token.research) is True

    async def fake_stats(mint, creator="", pool_address="", chain="sol"):
        return {
            "holder_count": 68,
            "top10_pct": 2.42,
            "top1_pct": 1.29,
            "creator_hold_pct": 0.0,
            "top_wallets": [
                {"owner": "0x8366a39CC670B4001A1121B8F6A443A643e40951", "pct": 81.58, "label": "pool"},
                {"owner": "0x4fe6C9D986D30791d87692490a4c0C280ad90ABE", "pct": 1.29, "label": ""},
            ],
            "source": "blockscout",
        }

    monkeypatch.setattr("launchfinder.research.holders.holder_stats", fake_stats)
    wrote = asyncio.run(hydrate_rh_wallet_map(session, token))
    session.commit()
    session.refresh(token.research)
    assert wrote is True
    assert rh_wallet_map_missing(token.research) is False
    raw = json.loads(token.research.raw_json)
    assert raw["holders"]["source"] == "blockscout"
    assert len(raw["holders"]["top_wallets"]) == 2
    assert token.research.holder_count == 68
    # features_json stays at-entry
    assert token.research.features_json == "{}"
    assert token.research.p_good == 0.92
    session.close()


def test_apply_rh_holder_meta_updates_count_without_lifting_p():
    init_db()
    session = SessionLocal()
    token = Token(
        mint="0xcccccccccccccccccccccccccccccccccccccccc",
        symbol="0xcccccccccccccccccccccccccccccccc",
        chain="robinhood",
        source="rh_bitquery",
        first_seen_at=utcnow(),
        migrated_at=utcnow(),
    )
    token.research = Research(
        p_good=0.61,
        heuristic_p=0.61,
        holder_count=6,
        features_json="{}",
        raw_json='{"holders":{"holder_count":6,"top_wallets":[{"owner":"0x1","pct":10}],"source":"blockscout"}}',
    )
    session.add(token)
    session.commit()
    assert holders_snapshot_age_sec(token.research) > 1000
    wrote = apply_rh_holder_meta(
        token,
        {"holder_count": 29, "name": "Ponshare", "symbol": "PONSHARE"},
    )
    session.commit()
    session.refresh(token.research)
    session.refresh(token)
    assert wrote is True
    assert token.symbol == "PONSHARE"
    assert token.research.holder_count == 29
    assert token.research.p_good == 0.61
    raw = json.loads(token.research.raw_json)
    assert raw["holders"]["holder_count"] == 29
    assert raw["holders"]["top_wallets"][0]["owner"] == "0x1"
    assert raw["holders"]["taken_at"]
    assert raw["holders"]["history"][-1]["n"] == 29
    assert holders_snapshot_age_sec(token.research) < 5
    session.close()


def test_holder_tape_grows_shrinks_and_flat_nina_does_not_fade():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)
    growing = {"history": [{"n": 40, "at": (now - timedelta(minutes=12)).isoformat()}], "taken_at": now.isoformat()}
    shrinking = {"history": [{"n": 80, "at": (now - timedelta(minutes=12)).isoformat()}], "taken_at": now.isoformat()}
    nina = {"history": [{"n": 630, "at": (now - timedelta(minutes=12)).isoformat()}], "taken_at": now.isoformat()}
    grow_r = Research(holder_count=80, raw_json=json.dumps({"holders": growing}))
    shrink_r = Research(holder_count=50, raw_json=json.dumps({"holders": shrinking}))
    nina_r = Research(holder_count=630, raw_json=json.dumps({"holders": nina}))
    grow = holder_tape(grow_r, now=now)
    fade = holder_tape(shrink_r, now=now)
    hold = holder_tape(nina_r, now=now)
    assert grow["growing"] is True and grow["prev"] == 40
    assert fade["shrinking"] is True and fade["prev"] == 80
    assert hold["growing"] is False and hold["shrinking"] is False
    stamp = {"holder_count": 40}
    append_holder_history(stamp, 40, now=now)
    append_holder_history(stamp, 80, now=now + timedelta(minutes=8))
    assert [row["n"] for row in stamp["history"]] == [40, 80]


def test_holders_from_gmgn_keeps_empty_wallet_list():
    out = holders_from_gmgn({"holder_count": 98, "top10_pct": 10.98})
    assert out["top_wallets"] == []
    assert out["source"] == "gmgn"


def test_get_token_hydrates_rh_wallet_map(monkeypatch):
    from launchfinder.app import get_token

    init_db()
    session = SessionLocal()
    mint = "0xac292fda653018e459d69db1c88f11565e551e1a"
    token = Token(
        mint=mint,
        symbol="INVDETAIL",
        chain="robinhood",
        source="rh_trenches",
        first_seen_at=utcnow(),
        migrated_at=utcnow(),
    )
    token.research = Research(
        p_good=0.92,
        heuristic_p=0.92,
        holder_count=98,
        features_json="{}",
        raw_json='{"holders":{"holder_count":98,"source":"gmgn"}}',
    )
    session.add(token)
    session.commit()
    session.close()

    async def fake_stats(mint_arg, creator="", pool_address="", chain="sol"):
        return {
            "holder_count": 68,
            "top10_pct": 2.42,
            "creator_hold_pct": 0.0,
            "top_wallets": [
                {"owner": "0x4fe6C9D986D30791d87692490a4c0C280ad90ABE", "pct": 1.29, "label": ""},
            ],
            "source": "blockscout",
        }

    monkeypatch.setattr("launchfinder.research.holders.holder_stats", fake_stats)
    card = asyncio.run(get_token(mint))
    assert card["symbol"] == "INVDETAIL"
    assert card["top_wallets"]
    assert card["top_wallets"][0]["owner"].startswith("0x4fe6")
    assert card["holder_source"] == "blockscout"


def test_token_detail_shows_gmgn_holders_on_lp_open():
    # Live MEME: Blockscout froze 4 pool rows; stored GMGN has 15k+.
    from launchfinder.serialize import token_detail

    init_db()
    session = SessionLocal()
    token = Token(
        mint="0x385f4f8ae47651ce5f58f5265395a669f8281e18",
        symbol="MEME",
        chain="robinhood",
        source="rh_trenches",
        first_seen_at=utcnow(),
    )
    token.research = Research(
        p_good=0.0279,
        holder_count=4,
        features_json="{}",
        raw_json=json.dumps(
            {
                "holders": {
                    "holder_count": 4,
                    "source": "blockscout",
                    "top_wallets": [
                        {"pct": 98.8, "label": "pool"},
                        {"pct": 1.2, "label": ""},
                    ],
                },
                "gmgn": {
                    "source": "gmgn",
                    "holder_count": 15942,
                    "top10_pct": 10.33,
                    "fresh_wallet_pct": 13.33,
                },
            }
        ),
    )
    session.add(token)
    session.commit()
    session.refresh(token)
    card = token_detail(token)
    assert card["holder_count"] == 15942
    assert card["holder_source"] == "gmgn"
    assert abs(card["top10_pct"] - 10.33) < 1e-6
    session.close()
