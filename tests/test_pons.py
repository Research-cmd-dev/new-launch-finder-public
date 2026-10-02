import asyncio
import json
import time
from types import SimpleNamespace

from launchfinder.db import init_db
from launchfinder.models import Token
from launchfinder.research.pons import (
    TOKEN_LAUNCHED_TOPIC,
    V2_FACTORY,
    V2_TOKEN_LAUNCHED_TOPIC,
    decode_abi_string,
    decode_token_launched,
    launch_to_coin,
    pons_token_url,
)


# Official docs reference token — receipt of 0x1f54f25f… on the legacy factory.
_PONS_LOG = {
    "address": "0x0c37a24f5d23a486fa692d1500881d698b1f77a4",
    "topics": [
        TOKEN_LAUNCHED_TOPIC,
        "0x00000000000000000000000039dbed3a2bd333467115de45665cc57f813c4571",
        "0x000000000000000000000000b9f5f4ea1af1f5d3678470eb98e8fbdcadeb24b0",
        "0x0000000000000000000000001f7d7550b1b028f7571e69a784071f0205fd2efa",
    ],
    "data": (
        "0x0000000000000000000000000bd7d308f8e1639fab988df18a8011f41eacad73"
        "00000000000000000000000010cc6bd38112cac182db90b6a71d8bb5939526ba"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000000001aaa0"
        "0000000000000000000000000000000000000000000000000000000001858104"
        "000000000000000000000000000000000000000000000000016345785d8a0000"
    ),
    "blockNumber": "0x88c44e",
    "transactionHash": "0x1f54f25fec2d963dcb338ecb8b46a6eb123198a5c7a746d34cb2dbe78d074af8",
    "blockTimestamp": "0x6a554dad",
}

# Live 2026-09-02 ~13:14 UTC, V2 factory tip ~52603216.
_PONS_V2_LOG = {
    "address": V2_FACTORY,
    "topics": [
        V2_TOKEN_LAUNCHED_TOPIC,
        "0x000000000000000000000000b09a9185ab7965a8708051a7483491994c0e6e85",
        "0x00000000000000000000000059f833a653602814a40aaa2db5198f1cc6d35b89",
        "0x000000000000000000000000e58c4fcadaf2805c38d8c19e1bfd0a634a67a00d",
    ],
    "data": (
        "0x0000000000000000000000005fc5360d0400a0fd4f2af552add042d716f1d168"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000000000000000000001e2854d00"
    ),
    "blockNumber": "0x322a94d",
    "transactionHash": "0xa71a738a7c8ee07489776c0fc907c5fa4c67f5de23c3a2c5ba6e3a759ad796cd",
    "blockTimestamp": "0x68b6c4c0",
}


def test_decode_official_pons_token_launched():
    event = decode_token_launched(_PONS_LOG)
    assert event is not None
    assert event["mint"] == "0x39dbed3a2bd333467115de45665cc57f813c4571"
    assert event["creator"] == "0xb9f5f4ea1af1f5d3678470eb98e8fbdcadeb24b0"
    assert event["pool"] == "0x10cc6bd38112cac182db90b6a71d8bb5939526ba"
    assert event["pair_token"] == "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    assert event["launchpad"] == "pons"
    assert event["created_at"] is not None
    assert event["pons_version"] == 1


def test_decode_ignores_zero_block_timestamp():
    log = dict(_PONS_V2_LOG)
    log["blockTimestamp"] = "0x0"
    event = decode_token_launched(log)
    assert event is not None
    assert event["created_at"] is None
    coin = launch_to_coin(event, {"name": "v2", "symbol": "V2"})
    assert coin["created_at"] is not None
    assert coin["created_at"].year >= 2026


def test_decode_v2_token_launched():
    event = decode_token_launched(_PONS_V2_LOG)
    assert event is not None
    assert event["mint"] == "0xb09a9185ab7965a8708051a7483491994c0e6e85"
    assert event["creator"] == "0xe58c4fcadaf2805c38d8c19e1bfd0a634a67a00d"
    assert event["pool"] == "0x59f833a653602814a40aaa2db5198f1cc6d35b89"
    assert event["curve"] == "0x59f833a653602814a40aaa2db5198f1cc6d35b89"
    assert event["pair_token"] == "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
    assert event["graduation_threshold"] == 8_095_354_112
    assert event["launchpad"] == "pons"
    assert event["pons_version"] == 2
    assert event["block"] == 52_603_213


def test_abi_string_and_token_url():
    # offset 32, length 4, "PONS"
    encoded = (
        "0x"
        + "0" * 62
        + "20"
        + "0" * 63
        + "4"
        + "504f4e53"
        + "0" * 56
    )
    assert decode_abi_string(encoded) == "PONS"
    assert pons_token_url("0x39dbed3a2bd333467115de45665cc57f813c4571") == (
        "https://www.ponsfamily.com/launchpad/0x39dbed3a2bd333467115de45665cc57f813c4571"
    )


def test_factories_at_tip_skips_getlogs():
    from launchfinder.ingest.pons_poll import TIP_SLACK_BLOCKS, factories_at_tip
    from launchfinder.research import pons

    tip = 51_560_000
    behind = {name: tip - 5_000 for name, _, _ in pons.FACTORIES}
    at = {name: tip for name, _, _ in pons.FACTORIES}
    slack = {name: tip - TIP_SLACK_BLOCKS for name, _, _ in pons.FACTORIES}
    ping_pong = {name: tip for name, _, _ in pons.FACTORIES}
    ping_pong["legacy"] = tip - 500
    missing_v2 = {"active": tip, "legacy": tip}
    assert factories_at_tip(behind, tip) is False
    assert factories_at_tip({}, tip) is False
    assert factories_at_tip(at, tip) is True
    assert factories_at_tip(slack, tip) is True
    assert factories_at_tip(ping_pong, tip) is True
    assert factories_at_tip(missing_v2, tip) is False


def test_first_factory_scan_stays_near_tip():
    from launchfinder.ingest.pons_poll import scan_from_block
    from launchfinder.research import pons

    tip = 51_000_000
    start = pons.ACTIVE_START
    first = scan_from_block(0, start, tip)
    assert first >= tip - pons.LOOKBACK_BLOCKS
    assert first > start
    assert scan_from_block(50_990_000, start, tip) == 50_990_001


def test_launch_to_coin_skips_gmgn():
    event = decode_token_launched(_PONS_LOG)
    coin = launch_to_coin(event, {"name": "pons", "symbol": "PONS", "pool_address": event["pool"]})
    assert coin["skip_gmgn"] is True
    assert coin["launchpad"] == "pons"
    assert coin["chain"] == "robinhood"
    assert coin["symbol"] == "PONS"


def test_poll_pons_ingests_unknown_factory_launch(monkeypatch):
    from launchfinder.ingest import pons_poll

    event = decode_token_launched(_PONS_LOG)
    ingested: list[tuple[str, str]] = []

    async def fake_latest():
        # Live RH tip is ~51M; a genesis-era tip would sit below the
        # active factory start and skip the first scan window.
        return 51_360_000

    async def fake_logs(*, address, from_block, to_block, topic=None):
        # One factory is scanned per poll; serve the official log from either.
        return [_PONS_LOG]

    async def fake_meta(mint):
        return {"name": "pons", "symbol": "PONS", "pool_address": event["pool"]}

    async def fake_grad(factory, mint):
        return {"progress": 1.0, "graduated": True, "paired_principal": 1, "threshold": 1}

    async def fake_ingest(session, **kw):
        ingested.append((kw["mint"], kw["source"]))
        return Token(mint=kw["mint"], symbol="PONS", chain="robinhood", source=kw["source"])

    monkeypatch.setattr(pons_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(pons_poll.pons, "pons_available", lambda: True)
    monkeypatch.setattr(pons_poll.pons, "latest_block", fake_latest)
    monkeypatch.setattr(pons_poll.pons, "get_logs", fake_logs)
    monkeypatch.setattr(pons_poll.pons, "token_metadata", fake_meta)
    monkeypatch.setattr(pons_poll.pons, "graduation_status", fake_grad)
    monkeypatch.setattr(pons_poll, "ingest_and_research", fake_ingest)

    async def no_backfill():
        return 0

    monkeypatch.setattr(pons_poll, "_backfill_blank_pons_meta", no_backfill)

    init_db()
    pons_poll._quiet_until = 0.0
    found = asyncio.run(pons_poll.poll_pons_launches())
    assert ingested == [("0x39dbed3a2bd333467115de45665cc57f813c4571", "rh_pons")]
    assert found == ["0x39dbed3a2bd333467115de45665cc57f813c4571"]


def test_poll_pons_quiet_when_cursors_at_tip(monkeypatch):
    from launchfinder.db import session_scope
    from launchfinder.ingest import pons_poll
    from launchfinder.models import ScanState

    async def fake_latest():
        return 51_360_000

    async def boom_logs(**_kw):
        raise AssertionError("getLogs must not run when both factories are at the tip")

    monkeypatch.setattr(pons_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(pons_poll.pons, "pons_available", lambda: True)
    monkeypatch.setattr(pons_poll.pons, "latest_block", fake_latest)
    monkeypatch.setattr(pons_poll.pons, "get_logs", boom_logs)

    async def no_backfill():
        return 0

    monkeypatch.setattr(pons_poll, "_backfill_blank_pons_meta", no_backfill)

    init_db()
    pons_poll._quiet_until = 0.0
    with session_scope() as session:
        row = session.query(ScanState).filter(ScanState.key == pons_poll.CURSOR_KEY).one_or_none()
        if row is None:
            row = ScanState(key=pons_poll.CURSOR_KEY)
            session.add(row)
        row.value = json.dumps({"active": 51_360_000, "legacy": 51_360_000, "v2": 51_360_000})
    assert asyncio.run(pons_poll.poll_pons_launches()) == []

    async def boom_tip():
        raise AssertionError("latest_block must not run during the at-tip quiet window")

    monkeypatch.setattr(pons_poll.pons, "latest_block", boom_tip)
    assert asyncio.run(pons_poll.poll_pons_launches()) == []


def test_poll_pons_quiets_after_catchup_scan(monkeypatch):
    # Live 09:13: one factory was ~1.8k behind, we scanned, then 24s later
    # tip had moved past slack and getLogs 429'd. Sit out after every scan.
    from launchfinder.db import session_scope
    from launchfinder.ingest import pons_poll
    from launchfinder.models import ScanState

    logs_calls = {"n": 0}

    async def fake_latest():
        return 51_360_000

    async def fake_logs(**_kw):
        logs_calls["n"] += 1
        return []

    monkeypatch.setattr(pons_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(pons_poll.pons, "pons_available", lambda: True)
    monkeypatch.setattr(pons_poll.pons, "latest_block", fake_latest)
    monkeypatch.setattr(pons_poll.pons, "get_logs", fake_logs)

    async def no_backfill():
        return 0

    monkeypatch.setattr(pons_poll, "_backfill_blank_pons_meta", no_backfill)

    init_db()
    pons_poll._quiet_until = 0.0
    with session_scope() as session:
        row = session.query(ScanState).filter(ScanState.key == pons_poll.CURSOR_KEY).one_or_none()
        if row is None:
            row = ScanState(key=pons_poll.CURSOR_KEY)
            session.add(row)
        row.value = json.dumps({"active": 51_350_000, "legacy": 51_360_000, "v2": 51_360_000})
    assert asyncio.run(pons_poll.poll_pons_launches()) == []
    assert logs_calls["n"] == 1

    async def boom_tip():
        raise AssertionError("latest_block must not run during the post-scan quiet window")

    monkeypatch.setattr(pons_poll.pons, "latest_block", boom_tip)
    assert asyncio.run(pons_poll.poll_pons_launches()) == []
    assert logs_calls["n"] == 1


def test_rh_poll_keeps_pons_when_gmgn_banned(monkeypatch):
    from launchfinder.ingest import rh_poll

    async def fake_pons():
        return ["0xponsfactory0000000000000000000000000001"]

    async def fake_trenches(*, chain="sol", limit=40):
        raise AssertionError("GMGN must not be called while banned")

    async def fake_dex():
        return []

    monkeypatch.setattr(rh_poll, "settings", SimpleNamespace(robinhood_enabled=True, has_gmgn=True))
    monkeypatch.setattr(rh_poll.gmgn, "gmgn_available", lambda: False)
    monkeypatch.setattr(rh_poll.gmgn, "trenches", fake_trenches)
    monkeypatch.setattr(rh_poll, "poll_pons_launches", fake_pons)
    monkeypatch.setattr(rh_poll, "poll_rh_dex", fake_dex)

    init_db()
    found = asyncio.run(rh_poll.poll_robinhood())
    assert found == ["0xponsfactory0000000000000000000000000001"]


def test_pons_rpc_sitout_covers_repeat_429s():
    # Live 15:27 / 15:34: 360s sit-out ended and the next getLogs
    # 429'd again. Live 23:06 / 23:19: 720s expired and the next
    # scan 429'd immediately. Hold the public RPC for fifteen minutes.
    from launchfinder.ingest.pons_poll import QUIET_AT_TIP_S
    from launchfinder.research.pons import _BAN_SITOUT

    assert _BAN_SITOUT >= 900.0
    assert QUIET_AT_TIP_S >= 120.0


def _v2_log(mint_n: int, block: int) -> dict:
    hex_n = f"{mint_n:040x}"
    return {
        "address": V2_FACTORY,
        "topics": [
            V2_TOKEN_LAUNCHED_TOPIC,
            "0x000000000000000000000000" + hex_n,
            "0x00000000000000000000000059f833a653602814a40aaa2db5198f1cc6d35b89",
            "0x000000000000000000000000e58c4fcadaf2805c38d8c19e1bfd0a634a67a00d",
        ],
        "data": _PONS_V2_LOG["data"],
        "blockNumber": hex(block),
        "transactionHash": "0x" + f"{mint_n:064x}",
        "blockTimestamp": "0x68b6c4c0",
    }


def test_poll_pons_rewinds_cursor_when_v2_chunk_exceeds_meta(monkeypatch):
    from launchfinder.db import session_scope
    from launchfinder.ingest import pons_poll
    from launchfinder.models import ScanState
    from launchfinder.research import pons

    logs = [_v2_log(0xA100 + i, 51_360_100 + i) for i in range(pons.MAX_META_PER_POLL + 1)]
    ingested: list[str] = []
    log_calls = {"n": 0}

    async def fake_latest():
        return 51_360_000 + 8_000

    async def fake_logs(**_kw):
        log_calls["n"] += 1
        return logs

    async def fake_meta(mint):
        return {"name": "v2", "symbol": "V2", "pool_address": ""}

    async def fake_grad(factory, mint):
        return {}

    async def fake_ingest(session, **kw):
        token = Token(mint=kw["mint"], symbol="V2", chain="robinhood", source=kw["source"])
        session.add(token)
        ingested.append(kw["mint"])
        return token

    monkeypatch.setattr(pons_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(pons_poll.pons, "pons_available", lambda: True)
    monkeypatch.setattr(pons_poll.pons, "latest_block", fake_latest)
    monkeypatch.setattr(pons_poll.pons, "get_logs", fake_logs)
    monkeypatch.setattr(pons_poll.pons, "token_metadata", fake_meta)
    monkeypatch.setattr(pons_poll.pons, "graduation_status", fake_grad)
    monkeypatch.setattr(pons_poll, "ingest_and_research", fake_ingest)

    async def no_backfill():
        return 0

    monkeypatch.setattr(pons_poll, "_backfill_blank_pons_meta", no_backfill)

    init_db()
    pons_poll._quiet_until = 0.0
    tip = 51_360_000 + 8_000
    with session_scope() as session:
        row = session.query(ScanState).filter(ScanState.key == pons_poll.CURSOR_KEY).one_or_none()
        if row is None:
            row = ScanState(key=pons_poll.CURSOR_KEY)
            session.add(row)
        # Isolate from earlier tests that leave V1/V2 cursors at ~51.36M.
        row.value = json.dumps({"active": tip, "legacy": tip, "v2": 0})
    found = asyncio.run(pons_poll.poll_pons_launches())
    assert len(found) == pons.MAX_META_PER_POLL
    assert len(ingested) == pons.MAX_META_PER_POLL
    leftover_mint = "0x" + f"{0xA100 + pons.MAX_META_PER_POLL:040x}"
    assert leftover_mint not in ingested

    with session_scope() as session:
        row = session.query(ScanState).filter(ScanState.key == pons_poll.CURSOR_KEY).one()
        cursors = json.loads(row.value)
    leftover_block = 51_360_100 + pons.MAX_META_PER_POLL
    assert cursors["v2"] == leftover_block - 1

    # Still draining — next poll must getLogs again, not sit in the 120s quiet.
    found2 = asyncio.run(pons_poll.poll_pons_launches())
    assert log_calls["n"] == 2
    assert leftover_mint in found2


def test_poll_pons_skips_nameless_meta_and_rewinds(monkeypatch):
    from launchfinder.db import session_scope
    from launchfinder.ingest import pons_poll
    from launchfinder.models import ScanState

    async def fake_latest():
        return 51_368_000

    async def fake_logs(**_kw):
        return [_v2_log(0xB200, 51_360_200)]

    async def empty_meta(mint):
        return {}

    async def fake_grad(factory, mint):
        return {}

    async def boom_ingest(**_kw):
        raise AssertionError("nameless V2 must not ingest")

    async def no_backfill():
        return 0

    monkeypatch.setattr(pons_poll, "settings", SimpleNamespace(robinhood_enabled=True))
    monkeypatch.setattr(pons_poll.pons, "pons_available", lambda: True)
    monkeypatch.setattr(pons_poll.pons, "latest_block", fake_latest)
    monkeypatch.setattr(pons_poll.pons, "get_logs", fake_logs)
    monkeypatch.setattr(pons_poll.pons, "token_metadata", empty_meta)
    monkeypatch.setattr(pons_poll.pons, "graduation_status", fake_grad)
    monkeypatch.setattr(pons_poll, "ingest_and_research", boom_ingest)
    monkeypatch.setattr(pons_poll, "_backfill_blank_pons_meta", no_backfill)

    init_db()
    pons_poll._quiet_until = 0.0
    with session_scope() as session:
        row = session.query(ScanState).filter(ScanState.key == pons_poll.CURSOR_KEY).one_or_none()
        if row is None:
            row = ScanState(key=pons_poll.CURSOR_KEY)
            session.add(row)
        row.value = json.dumps({"active": 51_368_000, "legacy": 51_368_000, "v2": 0})
    assert asyncio.run(pons_poll.poll_pons_launches()) == []
    with session_scope() as session:
        cursors = json.loads(
            session.query(ScanState).filter(ScanState.key == pons_poll.CURSOR_KEY).one().value
        )
    assert cursors["v2"] == 51_360_199


def test_poll_pons_repairs_epoch_created_during_rpc_sitout():
    from datetime import datetime, timezone

    from launchfinder.db import session_scope
    from launchfinder.ingest import pons_poll
    from launchfinder.models import Token

    init_db()
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    seen = datetime(2026, 9, 2, 13, 25, tzinfo=timezone.utc)
    with session_scope() as session:
        session.add(
            Token(
                mint="0xcccccccccccccccccccccccccccccccccccccccc",
                chain="robinhood",
                source="rh_pons",
                symbol="",
                created_at_chain=epoch,
                first_seen_at=seen,
            )
        )
    pons_poll._quiet_until = 0.0
    # RPC sitting out — repair must still run.
    from launchfinder.research import pons as pons_mod

    old = pons_mod._cooldown_until
    pons_mod._cooldown_until = time.time() + 60
    try:
        assert asyncio.run(pons_poll.poll_pons_launches()) == []
    finally:
        pons_mod._cooldown_until = old
    with session_scope() as session:
        token = session.query(Token).filter(Token.mint == "0xcccccccccccccccccccccccccccccccccccccccc").one()
        assert token.created_at_chain.year == 2026


def test_pons_metadata_stops_after_first_cooldown(monkeypatch):
    # Live 15:18: gather() 429'd name+symbol+logo+desc+pool+socials in
    # one second. One sit-out must stop the rest of the burst.
    from launchfinder.research import pons

    n = {"n": 0}

    async def fake_call(to, data):
        n["n"] += 1
        pons._cooldown_until = time.time() + 60
        return ""

    monkeypatch.setattr(pons, "eth_call", fake_call)
    pons._cooldown_until = 0.0
    meta = asyncio.run(pons.token_metadata("0xcccccccccccccccccccccccccccccccccccccccc"))
    assert n["n"] == 1
    assert meta["name"] == ""
    assert meta["symbol"] == ""


def test_launch_to_coin_v2_keeps_curve_as_pool():
    event = decode_token_launched(_PONS_V2_LOG)
    coin = launch_to_coin(event, {"name": "v2live", "symbol": "V2L"})
    assert coin["skip_gmgn"] is True
    assert coin["launchpad"] == "pons"
    assert coin["pool_address"] == "0x59f833a653602814a40aaa2db5198f1cc6d35b89"
