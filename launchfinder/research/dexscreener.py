from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ..chains import normalize_chain, normalize_mint, profile
from ..config import DEX_API
from ..httputil import get_json

log = logging.getLogger("launchfinder.dexscreener")

# Dex `/latest/dex/tokens` accepts a comma list. 30 keeps the URL short
# and one Hunt tape cycle under a handful of HTTP calls.
DEX_TOKEN_BATCH = 30

# Uniswap V4 pool ids are 32-byte hex (0x + 64), not 20-byte addresses.
# DexScreener RH pair URLs use that id — BONER's deep HIMS book is one.
def is_dex_pair_id(value: str | None) -> bool:
    raw = (value or "").strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    return len(raw) == 64 and all(c in "0123456789abcdef" for c in raw)


# Pump.fun curve leftovers keep a pair after migrate. Live LAPTOP /
# NIKE: that leftover printed $17M–$295M FDV (sometimes with fat fake
# liq) while PumpSwap was $2k. Do not use deepest-liq on Sol when a
# real AMM book exists.
_SOL_CURVE_DEX = frozenset({"pumpfun", "pump.fun"})


def _pair_liq_usd(pair: dict[str, Any]) -> float:
    return float((pair.get("liquidity") or {}).get("usd") or 0.0)


def _pair_volume_usd(pair: dict[str, Any]) -> float:
    vol = pair.get("volume") or {}
    return float(vol.get("h1") or 0.0) + float(vol.get("m5") or 0.0)


# Match outcomes.DEAD_POOL_LIQ — Sol tape must price a real book, not a wash pair.
_SOL_TAPE_MIN_LIQ_USD = 800.0


def _is_sol_curve_pair(pair: dict[str, Any]) -> bool:
    return str(pair.get("dexId") or "").strip().lower() in _SOL_CURVE_DEX


def _best_pair(pairs: list[dict[str, Any]], chain: str = "sol") -> dict[str, Any] | None:
    wanted = profile(chain).dex
    matched = [p for p in pairs if (p.get("chainId") or "").lower() == wanted]
    # Solana may fall back to any pair if DexScreener omits chainId; other
    # chains must not pick a lookalike 0x token on a different EVM.
    pool = matched or (pairs if normalize_chain(chain) == "sol" else [])
    if not pool:
        return None
    if normalize_chain(chain) == "sol":
        live = [p for p in pool if not _is_sol_curve_pair(p)]
        chosen = live or pool
        # Deepest live pool first (RESI-class: wash volume on a $5 liq
        # pair stamped ~$1.7M while Raydium ~$86k liq / ~$700k mcap).
        # Among liquid books, prefer volume so a $2k PumpSwap still beats silence.
        liquid = [p for p in chosen if _pair_liq_usd(p) >= _SOL_TAPE_MIN_LIQ_USD]
        ranked = liquid or chosen
        return max(ranked, key=lambda p: (_pair_liq_usd(p), _pair_volume_usd(p)))
    # BONER-class: many leftover V4 books print a huge FDV on $50 liq.
    # The live market is the deepest robinhood pair (HIMS $2.8M vs dust).
    return max(pool, key=_pair_liq_usd)


def _market_from_pair(pair: dict[str, Any], chain: str = "sol") -> dict[str, Any]:
    txns = pair.get("txns") or {}
    m5 = txns.get("m5") or {}
    h1 = txns.get("h1") or {}
    info = pair.get("info") or {}
    socials = info.get("socials") or []
    websites = info.get("websites") or []
    twitter = next((s.get("url") for s in socials if (s.get("type") or "").lower() in {"twitter", "x"}), "")
    telegram = next((s.get("url") for s in socials if (s.get("type") or "").lower() == "telegram"), "")
    website = next((w.get("url") for w in websites if w.get("url")), "")
    github = next((s.get("url") for s in socials if "github" in (s.get("url") or "").lower()), "")
    if not github:
        github = next((w.get("url") for w in websites if "github.com" in (w.get("url") or "").lower()), "")
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    labels = pair.get("labels") or []
    created_ms = pair.get("pairCreatedAt")
    created = None
    if created_ms:
        try:
            created = datetime.fromtimestamp(float(created_ms) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            created = None
    mint = normalize_mint(base.get("address") or "", chain)
    image = str(info.get("imageUrl") or info.get("header") or "").strip()
    liq_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
    mcap_raw = float(pair.get("marketCap") or 0.0)
    fdv_raw = float(pair.get("fdv") or 0.0)
    if normalize_chain(chain) == "sol":
        # Sol uses marketCap only; when Dex inflates mc vs fdv on a thin book,
        # take the conservative print (FOMO board tracks the liquid pool).
        mcap_usd = mcap_raw
        if mcap_raw > 0 and fdv_raw > 0:
            if liq_usd < 20_000.0 and mcap_raw > fdv_raw * 1.15:
                mcap_usd = min(mcap_raw, fdv_raw)
            elif mcap_raw > fdv_raw * 1.35:
                mcap_usd = fdv_raw
    else:
        mcap_usd = float(pair.get("marketCap") or pair.get("fdv") or 0.0)
    return {
        "mint": mint,
        "name": base.get("name") or "",
        "symbol": base.get("symbol") or "",
        "image_url": image,
        "quote_symbol": quote.get("symbol") or "",
        "quote_mint": normalize_mint(quote.get("address") or "", chain),
        "dex_id": pair.get("dexId") or "",
        "dex_labels": labels if isinstance(labels, list) else [],
        "pair_url": pair.get("url") or "",
        "pair_address": pair.get("pairAddress") or "",
        "price_usd": float(pair.get("priceUsd") or 0.0),
        "price_native": float(pair.get("priceNative") or 0.0),
        "mcap_usd": mcap_usd,
        "fdv": fdv_raw,
        "liquidity_usd": liq_usd,
        "volume_m5": float((pair.get("volume") or {}).get("m5") or 0.0),
        "volume_h1": float((pair.get("volume") or {}).get("h1") or 0.0),
        "volume_h24": float((pair.get("volume") or {}).get("h24") or 0.0),
        "buys_m5": int(m5.get("buys") or 0),
        "sells_m5": int(m5.get("sells") or 0),
        "buys_h1": int(h1.get("buys") or 0),
        "sells_h1": int(h1.get("sells") or 0),
        "price_change_h1": float((pair.get("priceChange") or {}).get("h1") or 0.0),
        "pair_created_at": created_ms,
        "created_at": created,
        "twitter": twitter or "",
        "telegram": telegram or "",
        "website": website or "",
        "github": github or "",
        "boosts": int((pair.get("boosts") or {}).get("active") or 0) if isinstance(pair.get("boosts"), dict) else 0,
        "chain": normalize_chain(chain),
    }


async def token_market(mint: str, chain: str = "sol") -> dict[str, Any]:
    markets = await token_markets([mint], chain)
    return markets.get(normalize_mint(mint, chain)) or {}


def market_has_quote(market: dict[str, Any] | None) -> bool:
    """True when Dex returned a usable last print (mcap or liq)."""
    if not isinstance(market, dict) or not market:
        return False
    liq = float(market.get("liquidity_usd") or 0.0)
    mcap = float(market.get("mcap_usd") or 0.0)
    return liq > 0 or mcap > 0


async def enrich_markets_with_pool_fallback(
    markets: dict[str, dict[str, Any]],
    tokens: list[Any],
    chain: str = "robinhood",
) -> dict[str, dict[str, Any]]:
    """When /dex/tokens misses a V4 book, resolve the stored Uniswap pool id."""
    from ..models import Token

    chain = normalize_chain(chain)
    for token in tokens:
        if not isinstance(token, Token):
            continue
        mint = normalize_mint(token.mint, chain)
        cur = markets.get(mint) or {}
        if market_has_quote(cur):
            continue
        pool = str(getattr(token, "pool_address", "") or "").strip()
        if not is_dex_pair_id(pool):
            continue
        try:
            pm = await pair_market(pool, chain)
        except Exception:
            pm = {}
        if market_has_quote(pm):
            markets[mint] = pm
    return markets


async def token_market_for_token(token: Any, chain: str | None = None) -> dict[str, Any]:
    """Single-token Dex last with V4 pool-id fallback (rh_bitquery rows)."""
    from ..chains import token_chain
    from ..models import Token

    if not isinstance(token, Token):
        return {}
    chain = normalize_chain(chain or token_chain(token))
    mint = normalize_mint(token.mint, chain)
    market = await token_market(mint, chain)
    if market_has_quote(market):
        return market
    pool = str(token.pool_address or "").strip()
    if is_dex_pair_id(pool):
        try:
            pm = await pair_market(pool, chain)
        except Exception:
            pm = {}
        if market_has_quote(pm):
            return pm
    return market or {}


async def token_markets(mints: list[str], chain: str = "sol") -> dict[str, dict[str, Any]]:
    """Batch Dex last for this-window Hunt tape. No extra GMGN."""
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in mints:
        mint = normalize_mint(raw, chain)
        if mint and mint not in seen:
            seen.add(mint)
            wanted.append(mint)
    out: dict[str, dict[str, Any]] = {}
    if not wanted:
        return out
    for i in range(0, len(wanted), DEX_TOKEN_BATCH):
        chunk = wanted[i : i + DEX_TOKEN_BATCH]
        data = await get_json(f"{DEX_API}/latest/dex/tokens/{','.join(chunk)}")
        pairs = data.get("pairs") if isinstance(data, dict) else None
        if not isinstance(pairs, list):
            await asyncio.sleep(0)
            continue
        grouped: dict[str, list[dict[str, Any]]] = {}
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            base = normalize_mint((pair.get("baseToken") or {}).get("address") or "", chain)
            if base in seen:
                grouped.setdefault(base, []).append(pair)
        for mint, rows in grouped.items():
            best = _best_pair(rows, chain)
            if best:
                out[mint] = _market_from_pair(best, chain)
        await asyncio.sleep(0)
    return out


async def pair_market(pair_id: str, chain: str = "sol") -> dict[str, Any]:
    """Resolve a DexScreener pair URL / Uniswap V4 pool id to the base token."""
    pair_id = (pair_id or "").strip()
    if not pair_id:
        return {}
    data = await get_json(f"{DEX_API}/latest/dex/pairs/{profile(chain).dex}/{pair_id}")
    if not isinstance(data, dict):
        return {}
    pair = data.get("pair")
    if not isinstance(pair, dict):
        rows = data.get("pairs")
        pair = _best_pair(rows, chain) if isinstance(rows, list) else None
    if not isinstance(pair, dict):
        return {}
    return _market_from_pair(pair, chain)


# Robinhood quote tokens that Long.xyz / Uniswap V4 launches pair against.
# BONER's runner book is HIMS, not the PONS WETH pool.
# Live 01:37: HOTDOG 0x1c1dae… $5M / ~140x was a COST book. Discovery
# never polled COST, and USDG was the dead 0xf052… leftover (one
# 791h pair). Live USDG is 0x5fc5…. Stock quotes are the same door.
RH_QUOTE_TOKENS: tuple[tuple[str, str], ...] = (
    ("HIMS", "0xccee82fe024c36fa15e1005ede3e9e4787e23d09"),
    ("WETH", "0x0bd7d308f8e1639fab988df18a8011f41eacad73"),
    ("USDG", "0x5fc5360d0400a0fd4f2af552add042d716f1d168"),
    ("COST", "0x4ea005168d7f09a7a0ba9d1def21a479950e44c2"),
    ("NVDA", "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"),
    ("TSLA", "0x322f0929c4625ed5bad873c95208d54e1c003b2d"),
    ("MSTR", "0xec262a75e413fafd0df80480274532c79d42da09"),
    ("PLTR", "0x894e1ec2d74ffe5aef8dc8a9e84686accb964f2a"),
    ("COIN", "0x6330d8c3178a418788df01a47479c0ce7ccf450b"),
    ("CRCL", "0xdf0992e440dd0be65bd8439b609d6d4366bf1cb5"),
    # Live 02:41: KEYCAT / Keyboard Cat 0x45ea…d8d8 was a GME book.
    # Boost latest-first caught it; the GME quote page was never polled.
    # Same COST/HOTDOG-class ingest door. Do not leftover KEYCAT.
    ("GME", "0x1b0e319c6a659f002271b69db8a7df2f911c153e"),
)
RH_QUOTE_SET = {addr.lower() for _, addr in RH_QUOTE_TOKENS}

# Live 2026-09-19: WOJAK DUe1qhee…redsRC was a Raydium CPMM vs PEPE
# (mint does not end in pump). Sol doors were Pump-family only, so
# FOMO stamped it 13h late. Poll the PEPE quote page — not WSOL,
# not every new pair. Do not leftover-sort the $1.5M print.
SOL_PEPE_MINT = "PEPEqnuuCDbBC89p1u9vpnP1KQ2oj1xTcQBsjt9X55m"
SOL_QUOTE_TOKENS: tuple[tuple[str, str], ...] = (
    ("PEPE", SOL_PEPE_MINT),
)
SOL_QUOTE_SET = {addr for _, addr in SOL_QUOTE_TOKENS}
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Live 02:40: LEGS $2.1M / Uniswap V4 vs native ETH never appeared on
# HIMS/WETH/USDG token pages (30-pair popularity lists). Dex
# token-boosts already had the mint. Live 02:33: RIG / Stock Miner
# was #0 on latest and only #12 on top — stale top boosts ate the
# 8-lookup cap. Poll latest first so a new native-ETH V4 launch is
# not starved. Cap the extra token lookups so a 12s poll does not
# fan out 20 Dex calls. Do not recap LEGS late-ingest.
BOOST_MARKET_CAP = 8


def _rh_profile_mints(rows: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (row.get("chainId") or "").lower() != "robinhood":
            continue
        mint = normalize_mint(row.get("tokenAddress") or "", "robinhood")
        if not mint or mint in seen or mint in RH_QUOTE_SET:
            continue
        seen.add(mint)
        out.append(mint)
    return out


async def profiled_rh_mints() -> list[str]:
    """Robinhood mints on Dex token-profiles/latest.

    Live 07:15: CLAWDHOOD 0x50ec…db9b / NVDA V4 / $53k liq never
    appeared on the NVDA quote page (30 pairs, all NVDA as base)
    or token-boosts. It was #12 on profiles/latest. Do not raise
    BOOST_MARKET_CAP. Do not recap LEGS late-ingest.
    """
    data = await get_json(f"{DEX_API}/token-profiles/latest/v1")
    rows = data if isinstance(data, list) else []
    return _rh_profile_mints(rows)


async def boosted_rh_mints() -> list[str]:
    """Robinhood mints currently boosted on DexScreener."""
    seen: set[str] = set()
    out: list[str] = []
    try:
        for mint in await profiled_rh_mints():
            if mint in seen:
                continue
            seen.add(mint)
            out.append(mint)
    except Exception:
        pass
    for path in ("token-boosts/latest/v1", "token-boosts/top/v1"):
        data = await get_json(f"{DEX_API}/{path}")
        rows = data if isinstance(data, list) else []
        for mint in _rh_profile_mints(rows):
            if mint in seen:
                continue
            seen.add(mint)
            out.append(mint)
    return out


async def boosted_rh_markets(*, limit: int = BOOST_MARKET_CAP) -> list[dict[str, Any]]:
    """Deepest live book for boosted RH mints (LEGS ETH V4)."""
    cap = max(0, int(limit))
    mints = await boosted_rh_mints()
    # Live 03:12: MEMEFLIX 0xBb6e…B3d4 / $17k / 4.7h / ETH sat #10
    # on latest. KEYCAT / RIG / Stocker / NVDA $BRRR already in the
    # Token table ate the 8-lookup cap. Skip known mints so the cap
    # hits unknown boosts. Do not raise BOOST_MARKET_CAP. Do not
    # recap LEGS late-ingest.
    known: set[str] = set()
    if mints:
        try:
            from ..db import session_scope
            from ..models import Token

            with session_scope() as session:
                known = {
                    row.mint
                    for row in session.query(Token.mint).filter(Token.mint.in_(mints)).all()
                }
        except Exception:
            known = set()
    out: list[dict[str, Any]] = []
    for mint in mints:
        if mint in known:
            continue
        if len(out) >= cap:
            break
        try:
            row = await token_market(mint, chain="robinhood")
        except Exception:
            continue
        if row and row.get("mint"):
            out.append(row)
    return out


def _is_pump_mint(mint: str) -> bool:
    return bool(mint) and mint.endswith("pump")


def _sol_profile_mints(rows: list[Any]) -> list[str]:
    """Sol mints on Dex profiles/boosts. Skip Pump.fun — those doors exist."""
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (row.get("chainId") or "").lower() != "solana":
            continue
        mint = normalize_mint(row.get("tokenAddress") or "", "sol")
        if not mint or mint in seen or mint in SOL_QUOTE_SET or mint == WSOL_MINT:
            continue
        if _is_pump_mint(mint):
            continue
        seen.add(mint)
        out.append(mint)
    return out


async def profiled_sol_mints() -> list[str]:
    """Sol mints on Dex token-profiles/latest. Skip pump. Do not raise BOOST_MARKET_CAP."""
    data = await get_json(f"{DEX_API}/token-profiles/latest/v1")
    rows = data if isinstance(data, list) else []
    return _sol_profile_mints(rows)


async def boosted_sol_mints() -> list[str]:
    """Non-pump Sol mints currently profiled or boosted on DexScreener."""
    seen: set[str] = set()
    out: list[str] = []
    try:
        for mint in await profiled_sol_mints():
            if mint in seen:
                continue
            seen.add(mint)
            out.append(mint)
    except Exception:
        pass
    for path in ("token-boosts/latest/v1", "token-boosts/top/v1"):
        data = await get_json(f"{DEX_API}/{path}")
        rows = data if isinstance(data, list) else []
        for mint in _sol_profile_mints(rows):
            if mint in seen:
                continue
            seen.add(mint)
            out.append(mint)
    return out


async def boosted_sol_markets(*, limit: int = BOOST_MARKET_CAP) -> list[dict[str, Any]]:
    """Live book for profiled/boosted non-pump Sol mints (WOJAK-class).

    Do not raise BOOST_MARKET_CAP. Skip known so the 8-lookup cap
    hits unknown profiles. Pump mints stay on the Pump doors.
    """
    cap = max(0, int(limit))
    mints = await boosted_sol_mints()
    known: set[str] = set()
    if mints:
        try:
            from ..db import session_scope
            from ..models import Token

            with session_scope() as session:
                known = {
                    row.mint
                    for row in session.query(Token.mint).filter(Token.mint.in_(mints)).all()
                }
        except Exception:
            known = set()
    out: list[dict[str, Any]] = []
    for mint in mints:
        if mint in known:
            continue
        if len(out) >= cap:
            break
        try:
            row = await token_market(mint, chain="sol")
        except Exception:
            continue
        if row and row.get("mint") and not _is_sol_curve_pair({"dexId": row.get("dex_id") or ""}):
            out.append(row)
    return out


def _dex_row_chain_id(row: dict[str, Any]) -> str:
    return str(row.get("chainId") or row.get("chain") or "").strip().lower()


def _dex_row_token_address(row: dict[str, Any]) -> str:
    addr = str(row.get("tokenAddress") or row.get("token_address") or "").strip()
    if addr:
        return addr
    base = row.get("baseToken")
    if isinstance(base, dict):
        return str(base.get("address") or "").strip()
    return ""


def _dex_row_symbol(row: dict[str, Any]) -> str:
    sym = str(row.get("symbol") or "").strip()
    if sym:
        return sym[:32]
    desc = str(row.get("description") or "").strip()
    if not desc:
        return ""
    return desc.split("\n", 1)[0].strip()[:48]


def _sol_dex_trending_rows_from_api(rows: list[Any], *, cap: int) -> list[dict[str, Any]]:
    """Sol mints from Dex profiles/boosts — compare-only (includes pump mints)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        chain_id = _dex_row_chain_id(row)
        if chain_id not in ("solana", "sol"):
            continue
        mint = normalize_mint(_dex_row_token_address(row), "sol")
        if not mint or mint in seen or mint in SOL_QUOTE_SET or mint == WSOL_MINT:
            continue
        seen.add(mint)
        out.append(
            {
                "mint": mint,
                "chain": "sol",
                "symbol": _dex_row_symbol(row),
                "rank": len(out) + 1,
            }
        )
        if len(out) >= cap:
            break
    return out


async def sol_trending_boost_rows(*, limit: int = 50) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """DexScreener profiles + token-boosts — not FOMO trending rank."""
    from ..models import utcnow

    now = utcnow()
    cap = min(80, max(1, limit))
    meta: dict[str, Any] = {
        "board_kind": "dexscreener_trending",
        "api_source": "dexscreener",
        "captured_at": now.isoformat(),
        "capture_age_hours": 0.0,
        "board_live": False,
        "board_stale": False,
    }
    merged: list[Any] = []
    fetch_paths: list[dict[str, Any]] = []
    for path in ("token-boosts/latest/v1", "token-boosts/top/v1", "token-profiles/latest/v1"):
        url = f"{DEX_API}/{path}"
        data = await get_json(url)
        n_raw = len(data) if isinstance(data, list) else 0
        sol_n = 0
        if isinstance(data, list):
            merged.extend(data)
            sol_n = sum(1 for r in data if isinstance(r, dict) and _dex_row_chain_id(r) in ("solana", "sol"))
        fetch_paths.append(
            {
                "path": path,
                "ok": data is not None,
                "raw_n": n_raw,
                "solana_n": sol_n,
            }
        )
    meta["fetch_paths"] = fetch_paths
    rows = _sol_dex_trending_rows_from_api(merged, cap=cap)
    meta["board_live"] = bool(rows)
    meta["item_count"] = len(rows)
    meta["merged_raw_n"] = len(merged)
    if not rows:
        if not any(p.get("ok") for p in fetch_paths):
            meta["empty_reason"] = "dex_fetch_failed"
            meta["error"] = True
        elif not merged:
            meta["empty_reason"] = "dex_paths_returned_empty"
        else:
            meta["empty_reason"] = "no_solana_mints_after_filter"
            sample = next((r for r in merged if isinstance(r, dict)), None)
            if isinstance(sample, dict):
                meta["sample_keys"] = ",".join(sorted(sample.keys())[:12])
        log.warning(
            "Dex trending empty merged_raw=%s paths=%s reason=%s",
            len(merged),
            fetch_paths,
            meta.get("empty_reason"),
        )
    return rows, meta


async def quoted_pairs(quote_mint: str, chain: str = "robinhood") -> list[dict[str, Any]]:
    """Pairs where `quote_mint` is the quote side (launches, not the quote token itself)."""
    quote_mint = normalize_mint(quote_mint, chain)
    data = await get_json(f"{DEX_API}/latest/dex/tokens/{quote_mint}")
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(pairs, list):
        return []
    out: list[dict[str, Any]] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if (pair.get("chainId") or "").lower() != profile(chain).dex:
            continue
        quote = normalize_mint((pair.get("quoteToken") or {}).get("address") or "", chain)
        base = normalize_mint((pair.get("baseToken") or {}).get("address") or "", chain)
        if quote != quote_mint or not base or base == quote_mint:
            continue
        if base in RH_QUOTE_SET or base in SOL_QUOTE_SET or base == WSOL_MINT:
            continue
        out.append(_market_from_pair(pair, chain))
    return out
