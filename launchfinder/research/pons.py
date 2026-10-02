"""Official pons launchpad on Robinhood Chain.

The site (https://www.ponsfamily.com/launchpad) is a Next.js view over
the factory. Docs say to index TokenLaunched — there is no public token
list API. This module talks to the public RH RPC only (no GMGN).

V1 (active + legacy) never migrates — same V3 pool for life. V2 bonds
on a curve and graduates to Uniswap V4; that is the migration stream
the desk was missing while we only subscribed to the V1 topic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..httputil import client

log = logging.getLogger("launchfinder.pons")

CHAIN = "robinhood"
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
LAUNCHPAD_URL = "https://www.ponsfamily.com/launchpad"

# docs.ponsfamily.com — active factory from block 8991118, legacy from 8600612.
ACTIVE_FACTORY = "0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb"
ACTIVE_START = 8_991_118
LEGACY_FACTORY = "0x0c37a24f5d23a486fa692d1500881d698b1f77a4"
LEGACY_START = 8_600_612
TOKEN_LAUNCHED_TOPIC = "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"

# Live 2026-09-02: V1 TokenLaunched has been silent for ~44h. V2 factory
# 0x7ed5…ec7e emits a different TokenLaunched (~100+ launches / 3k blocks).
# Start at 0 so the first scan uses tip - LOOKBACK (live window, not 5k leftovers).
V2_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
V2_START = 0
V2_TOKEN_LAUNCHED_TOPIC = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"

# keccak256 selectors (hardcoded — no extra crypto dep)
SEL_NAME = "0x06fdde03"
SEL_SYMBOL = "0x95d89b41"
SEL_LOGO = "0xfb7f21eb"
SEL_DESCRIPTION = "0x7284e416"
SEL_POOL = "0x665a11ca"
SEL_SOCIALS = "0x53cd512a"
SEL_GRADUATION = "0x98d652f1"

FACTORIES: tuple[tuple[str, str, int], ...] = (
    ("v2", V2_FACTORY, V2_START),
    ("active", ACTIVE_FACTORY, ACTIVE_START),
    ("legacy", LEGACY_FACTORY, LEGACY_START),
)

# Public RH RPC is ~0.1s/block and 429s on wide getLogs. Bound every scan.
# First scan stays near the tip so we do not spend the budget on old blocks
# while new TokenLaunched events are still landing.
CHUNK_BLOCKS = 8_000
MAX_CHUNKS_PER_POLL = 1
LOOKBACK_BLOCKS = CHUNK_BLOCKS * MAX_CHUNKS_PER_POLL  # ~0.2h at 0.1s
MAX_META_PER_POLL = 8
_RPC_GAP = 1.0
# Live 10:28 / 10:36 / 10:39: 180s was not enough — the next getLogs
# 429'd again as soon as the sit-out ended. Live 15:27 / 15:34: 360s
# expired and the next scan 429'd immediately. Live 23:06 / 23:19:
# 720s expired and the next getLogs 429'd immediately. Match the
# GMGN BANNED sit-out.
_BAN_SITOUT = 900.0

_last_rpc = 0.0
_cooldown_until = 0.0


def pons_available() -> bool:
    return time.time() >= _cooldown_until


def pons_token_url(mint: str) -> str:
    mint = (mint or "").strip()
    if not mint:
        return LAUNCHPAD_URL
    return f"{LAUNCHPAD_URL}/{mint}"


def _addr_from_word(word: str) -> str:
    hexpart = (word or "").lower().replace("0x", "")
    if len(hexpart) < 40:
        return ""
    return "0x" + hexpart[-40:]


def _int_word(word: str) -> int:
    hexpart = (word or "0").replace("0x", "") or "0"
    return int(hexpart, 16)


def _words(data: str) -> list[str]:
    raw = (data or "").lower().replace("0x", "")
    return [raw[i : i + 64] for i in range(0, len(raw), 64) if len(raw[i : i + 64]) == 64]


def decode_abi_string(data_hex: str) -> str:
    raw = bytes.fromhex((data_hex or "").replace("0x", ""))
    if len(raw) < 64:
        return ""
    offset = int.from_bytes(raw[0:32], "big")
    if offset + 32 > len(raw):
        return ""
    length = int.from_bytes(raw[offset : offset + 32], "big")
    start = offset + 32
    return raw[start : start + length].decode("utf-8", "replace")


def decode_abi_strings(data_hex: str, count: int) -> list[str]:
    raw = bytes.fromhex((data_hex or "").replace("0x", ""))
    if len(raw) < 32 * count:
        return [""] * count
    out: list[str] = []
    for i in range(count):
        offset = int.from_bytes(raw[i * 32 : (i + 1) * 32], "big")
        if offset + 32 > len(raw):
            out.append("")
            continue
        length = int.from_bytes(raw[offset : offset + 32], "big")
        start = offset + 32
        out.append(raw[start : start + length].decode("utf-8", "replace"))
    return out


def launched_topic(address: str) -> str:
    if (address or "").lower() == V2_FACTORY:
        return V2_TOKEN_LAUNCHED_TOPIC
    return TOKEN_LAUNCHED_TOPIC


def decode_token_launched(log: dict[str, Any]) -> dict[str, Any] | None:
    topics = log.get("topics") or []
    if len(topics) < 4:
        return None
    token = _addr_from_word(topics[1])
    if not token:
        return None
    words = _words(log.get("data") or "")
    topic0 = (topics[0] or "").lower()
    factory = (log.get("address") or "").lower()
    is_v2 = topic0 == V2_TOKEN_LAUNCHED_TOPIC or factory == V2_FACTORY
    ts = None
    raw_ts = log.get("blockTimestamp") or log.get("timeStamp")
    if raw_ts:
        try:
            n = int(raw_ts, 16) if isinstance(raw_ts, str) and raw_ts.startswith("0x") else int(raw_ts)
            # Public RH getLogs often sends blockTimestamp 0x0. Epoch
            # stamps rank as leftover on the hunt desk (stale=1).
            if n >= 1_700_000_000:
                ts = datetime.fromtimestamp(n, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            ts = None
    block = 0
    try:
        block = int(log.get("blockNumber") or "0x0", 16)
    except (TypeError, ValueError):
        block = 0
    if is_v2:
        # TokenLaunched(address indexed token, address indexed curve,
        # address indexed deployer, address pairToken, uint256 launchConfigId,
        # uint256 graduationThreshold). Curve is the live pool until migrate.
        curve = _addr_from_word(topics[2])
        pair = _addr_from_word("0x" + words[0]) if words else ""
        return {
            "mint": token,
            "creator": _addr_from_word(topics[3]),
            "dex_factory": "",
            "pair_token": pair,
            "pool": curve,
            "curve": curve,
            "factory": factory,
            "tx": (log.get("transactionHash") or "").lower(),
            "block": block,
            "created_at": ts,
            "initial_buy": 0,
            "launch_config_id": _int_word("0x" + words[1]) if len(words) > 1 else 0,
            "graduation_threshold": _int_word("0x" + words[2]) if len(words) > 2 else 0,
            "launchpad": "pons",
            "pons_version": 2,
            "chain": CHAIN,
        }
    pair = _addr_from_word("0x" + words[0]) if words else ""
    pool = _addr_from_word("0x" + words[1]) if len(words) > 1 else ""
    return {
        "mint": token,
        "creator": _addr_from_word(topics[2]),
        "dex_factory": _addr_from_word(topics[3]),
        "pair_token": pair,
        "pool": pool,
        "factory": factory,
        "tx": (log.get("transactionHash") or "").lower(),
        "block": block,
        "created_at": ts,
        "initial_buy": _int_word("0x" + words[6]) if len(words) > 6 else 0,
        "launchpad": "pons",
        "pons_version": 1,
        "chain": CHAIN,
    }


def _image_url(logo: str) -> str:
    logo = (logo or "").strip()
    if logo.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + logo[7:]
    return logo


async def _rate_limit() -> None:
    global _last_rpc
    now = time.monotonic()
    wait = _RPC_GAP - (now - _last_rpc)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_rpc = time.monotonic()


async def rpc(method: str, params: list[Any]) -> Any:
    global _cooldown_until
    if not pons_available():
        return None
    await _rate_limit()
    try:
        resp = await client().post(
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code == 429:
            _cooldown_until = time.time() + _BAN_SITOUT
            log.warning("Robinhood RPC rate limited; cooling down %.0fs", _BAN_SITOUT)
            return None
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        log.debug("pons rpc %s failed: %s", method, exc)
        return None
    if not isinstance(body, dict):
        return None
    if body.get("error"):
        log.debug("pons rpc %s error: %s", method, str(body.get("error"))[:160])
        return None
    return body.get("result")


async def latest_block() -> int:
    raw = await rpc("eth_blockNumber", [])
    if not raw:
        return 0
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return 0


async def get_logs(
    *,
    address: str,
    from_block: int,
    to_block: int,
    topic: str | None = None,
) -> list[dict[str, Any]] | None:
    """None = RPC failed (do not advance the cursor). [] = no events in range."""
    raw = await rpc(
        "eth_getLogs",
        [
            {
                "address": address,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "topics": [topic or launched_topic(address)],
            }
        ],
    )
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    return [row for row in raw if isinstance(row, dict)]


def _call_data(selector: str, address: str | None = None) -> str:
    if not address:
        return selector
    return selector + address.lower().replace("0x", "").rjust(64, "0")


async def eth_call(to: str, data: str) -> str:
    raw = await rpc("eth_call", [{"to": to, "data": data}, "latest"])
    return raw if isinstance(raw, str) else ""


async def token_metadata(mint: str) -> dict[str, Any]:
    mint = mint.lower()
    # Live 15:18 boot: gather() fired 6 eth_calls at once and the public
    # RPC 429'd all six in the same second. Sit-out only helps the *next*
    # call — serialize and stop after the first cooldown.
    calls = (SEL_NAME, SEL_SYMBOL, SEL_LOGO, SEL_DESCRIPTION, SEL_POOL, SEL_SOCIALS)
    hexes: list[str] = []
    for sel in calls:
        if not pons_available():
            hexes.extend([""] * (len(calls) - len(hexes)))
            break
        hexes.append(await eth_call(mint, sel))
    name_h, symbol_h, logo_h, desc_h, pool_h, socials_h = hexes
    socials = decode_abi_strings(socials_h, 5) if socials_h and len(socials_h) > 10 else ["", "", "", "", ""]
    pool = _addr_from_word(pool_h) if pool_h else ""
    return {
        "name": decode_abi_string(name_h),
        "symbol": decode_abi_string(symbol_h),
        "image_url": _image_url(decode_abi_string(logo_h)),
        "description": decode_abi_string(desc_h),
        "pool_address": pool,
        "twitter": socials[0] if socials else "",
        "telegram": socials[1] if len(socials) > 1 else "",
        "website": socials[3] if len(socials) > 3 else "",
    }


async def graduation_status(factory: str, mint: str) -> dict[str, Any]:
    raw = await eth_call(factory, _call_data(SEL_GRADUATION, mint))
    words = _words(raw)
    if len(words) < 3:
        return {}
    paired = _int_word("0x" + words[0])
    threshold = _int_word("0x" + words[1]) or 1
    graduated = _int_word("0x" + words[2]) > 0
    return {
        "paired_principal": paired,
        "threshold": threshold,
        "graduated": graduated,
        "progress": min(1.0, paired / threshold),
    }


def launch_to_coin(event: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = meta or {}
    created = event.get("created_at") or datetime.now(timezone.utc)
    pool = meta.get("pool_address") or event.get("pool") or ""
    website = meta.get("website") or ""
    return {
        "mint": event["mint"],
        "name": meta.get("name") or "",
        "symbol": meta.get("symbol") or "",
        "description": meta.get("description") or "",
        "image_url": meta.get("image_url") or "",
        "twitter": meta.get("twitter") or "",
        "website": website,
        "telegram": meta.get("telegram") or "",
        "creator": event.get("creator") or "",
        "complete": True,
        "nsfw": False,
        "banned": False,
        "reply_count": 0,
        "created_at": created,
        "updated_at": created,
        "mcap_usd": 0.0,
        "ath_mcap": 0.0,
        "pool_address": pool,
        "chain": CHAIN,
        "launchpad": "pons",
        "skip_gmgn": True,
        "pons_url": pons_token_url(event["mint"]),
        "raw": event,
    }
