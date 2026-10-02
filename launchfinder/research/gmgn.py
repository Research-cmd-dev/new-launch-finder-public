from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ..chains import gmgn_token_url, normalize_chain, profile
from ..config import settings
from ..httputil import client

log = logging.getLogger("launchfinder.gmgn")

GMGN_HOST = "https://openapi.gmgn.ai"
PUMP_PLATFORMS = ["Pump.fun", "pump_mayhem", "pump_mayhem_agent", "pump_agent"]
# Same Sol trenches POST — not a second GMGN call. LaunchLab deploys /
# graduates only (`ray_launchpad`). Do not add `pool_ray` (every AMM).
SOL_PLATFORMS = [*PUMP_PLATFORMS, "ray_launchpad"]
# GMGN cooking/trenches id for Robinhood's main launchpad. Keep the
# unfiltered RH request (platforms=None) so Flap/Klik still arrive;
# we just have to ask for new_creation — PONS deploys straight to
# Uniswap V3 and never shows up as a bonding-curve "completed" row.
PONS_PLATFORMS = ("pons", "Pons", "PONS")


def is_pons_platform(value: str | None) -> bool:
    return (value or "").strip().lower() in {p.lower() for p in PONS_PLATFORMS}


# Sol new_creation we keep (LaunchLab deploy). Pump creates stay on Watch
# until complete=true — do not dump the bonding-curve firehose onto Hunt.
SOL_CREATE_PADS = {"ray_launchpad", "raydium_launchpad", "launchlab"}


def is_sol_gmgn_create_pad(value: str | None) -> bool:
    return (value or "").strip().lower() in SOL_CREATE_PADS

_last_call = 0.0
_min_interval = 4.0
# Wall-clock time before which no GMGN call may be made. GMGN *extends* a
# rate-limit ban by 5s per violating call, so continuing to poll through a
# 429 turns a 5-minute ban into a permanent one.
_cooldown_until = 0.0
# Weight-5 / token/info stay dark after a BANNED so the first trenches
# POST after sit-out is not immediately followed by holder bursts.
_deep_until = 0.0


async def _rate_limit() -> None:
    global _last_call
    now = time.monotonic()
    wait = _min_interval - (now - _last_call)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call = time.monotonic()


def gmgn_available() -> bool:
    return bool(settings.gmgn_api_key) and time.time() >= _cooldown_until


def gmgn_cooldown_remaining_s() -> float:
    """Seconds until shallow GMGN calls may resume (0 when ready)."""
    if not settings.gmgn_api_key:
        return 0.0
    return max(0.0, _cooldown_until - time.time())


def gmgn_deep_available() -> bool:
    """Trenches (weight-3) may resume; token/info + top_holders may not."""
    return gmgn_available() and time.time() >= _deep_until


def gmgn_deep_cooldown_remaining_s() -> float:
    """Seconds until token/info + security may resume (0 when ready)."""
    if not settings.gmgn_api_key or gmgn_cooldown_remaining_s() > 0:
        return max(gmgn_cooldown_remaining_s(), max(0.0, _deep_until - time.time()))
    return max(0.0, _deep_until - time.time())


def _ban_pause_seconds(body: dict[str, Any]) -> float:
    """Honor reset_at, but a BANNED account needs a long sit-out.
    Poking every 60s extends the ban (each 429 adds ~5s)."""
    reset = float(body.get("reset_at") or 0)
    pause = max(60.0, reset - time.time()) if reset else 300.0
    err = str(body.get("error") or "").upper()
    if "BANNED" in err:
        # Live 07:01 then 07:15: 600s sit-out, then Sol near+completed
        # POSTs plus runner-watch weight-5 re-banned immediately.
        pause = max(pause, 900.0)
    return min(pause, 900.0)


async def _request(method: str, path: str, *, params: dict[str, Any], json_body: dict | None = None) -> dict[str, Any]:
    global _cooldown_until, _deep_until
    if not gmgn_available():
        return {}
    await _rate_limit()
    try:
        resp = await client().request(
            method,
            f"{GMGN_HOST}{path}",
            headers=_headers(),
            params=params,
            json=json_body,
        )
        if resp.status_code == 429:
            body = {}
            try:
                body = resp.json()
            except Exception:
                pass
            pause = _ban_pause_seconds(body)
            _cooldown_until = time.time() + pause
            if "BANNED" in str(body.get("error") or "").upper():
                _deep_until = _cooldown_until + 600.0
            log.warning("GMGN rate limited (%s); cooling down %.0fs", str(body.get("error") or "")[:40], pause)
            return {}
        resp.raise_for_status()
        parsed = resp.json()
        if isinstance(parsed, dict):
            err = str(parsed.get("error") or "").upper()
            try:
                api_code = int(parsed.get("code") or 0)
            except (TypeError, ValueError):
                api_code = 0
            if api_code == 429 or err in {"RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED"}:
                pause = _ban_pause_seconds(parsed)
                _cooldown_until = time.time() + pause
                if "BANNED" in err:
                    _deep_until = _cooldown_until + 600.0
                log.warning(
                    "GMGN rate limited in body (%s); cooling down %.0fs",
                    str(parsed.get("error") or "")[:40],
                    pause,
                )
                return {}
        return _payload(parsed)
    except Exception as exc:
        log.debug("GMGN %s %s failed: %s", method, path, exc)
        return {}


def _headers() -> dict[str, str]:
    return {"X-APIKEY": settings.gmgn_api_key, "Accept": "application/json"}


def _params(extra: dict[str, Any] | None = None, *, chain: str = "sol") -> dict[str, Any]:
    params = {
        "chain": profile(chain).gmgn,
        "timestamp": str(int(time.time())),
        "client_id": str(uuid.uuid4()),
    }
    if extra:
        params.update(extra)
    return params


def _payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    if isinstance(inner, dict):
        return inner
    return data


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "high"}
    return False


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(value: Any) -> float:
    n = _num(value)
    if 0 < n <= 1:
        n *= 100.0
    return n


def _ts(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, str) and not value.replace(".", "", 1).lstrip("-").isdigit():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    try:
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


async def _get(path: str, mint: str, *, chain: str = "sol") -> dict[str, Any]:
    if not mint:
        return {}
    return await _request("GET", path, params=_params({"address": mint}, chain=chain))


def _dig(obj: dict[str, Any], *path: str) -> Any:
    """Read a nested field, falling back to the flat key at each level."""
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if cur is not None:
        return cur
    return obj.get(path[-1]) if isinstance(obj, dict) else None


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", []):
            return value
    return None


def _twitter_handle(value: Any) -> str:
    """Accept a handle, profile URL, or a tweet URL like `i/status/123`."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "status/" in raw or "x.com" in raw.lower() or "twitter.com" in raw.lower() or raw.startswith("http"):
        if not raw.startswith("http"):
            raw = "https://x.com/" + raw.lstrip("/")
        from ..social import extract_twitter_handle

        return extract_twitter_handle(raw)
    from ..social import extract_twitter_handle

    return extract_twitter_handle(raw)


def _socials_from_row(row: dict[str, Any]) -> dict[str, str]:
    """Trenches put socials at the top level; token/info nests them under link."""
    links = row.get("social_links") if isinstance(row.get("social_links"), dict) else {}
    nested = row.get("link") if isinstance(row.get("link"), dict) else {}

    def pick(*keys: str) -> str:
        for key in keys:
            value = _first(row.get(key), links.get(key), nested.get(key))
            if value:
                return str(value).strip()
        return ""

    twitter = pick("twitter", "twitter_username")
    handle = _twitter_handle(pick("twitter_username") or twitter)
    return {
        "twitter": twitter if twitter.startswith("http") or handle else "",
        "twitter_username": handle,
        "website": pick("website"),
        "telegram": pick("telegram"),
    }


def _progress(value: Any) -> float:
    n = _num(value)
    if n > 1:
        n /= 100.0
    return max(0.0, min(1.0, n))


def _burned(info: dict[str, Any], security: dict[str, Any]) -> bool:
    status = str(_first(info.get("burn_status"), security.get("burn_status")) or "").lower()
    if status in {"yes", "burn", "burned", "true", "1"}:
        return True
    return _num(info.get("dev_token_burn_ratio") or info.get("burn_ratio") or security.get("burn_ratio")) > 0


def summarize(info: dict[str, Any], security: dict[str, Any], mint: str, chain: str = "sol") -> dict[str, Any]:
    if not info and not security:
        return {}
    stat = info.get("stat") if isinstance(info.get("stat"), dict) else {}
    dev = info.get("dev") if isinstance(info.get("dev"), dict) else {}
    tags = info.get("wallet_tags_stat") if isinstance(info.get("wallet_tags_stat"), dict) else {}
    price = info.get("price") if isinstance(info.get("price"), dict) else {}
    socials = _socials_from_row(info)

    honeypot = _flag(security.get("is_honeypot") or security.get("honeypot") or info.get("honeypot") or info.get("is_honeypot"))
    bundled = _flag(security.get("is_bundled") or security.get("bundled") or info.get("bundled"))
    if not bundled and _num(info.get("bundler_rate") or security.get("bundler_rate")) > 0.25:
        bundled = True
    insider = _pct(
        security.get("suspected_insider_hold_rate")
        or security.get("insider_percent")
        or security.get("insider_ratio")
        or info.get("suspected_insider_hold_rate")
        or info.get("insider_ratio")
        or info.get("insider_percent")
    )
    sniper = _pct(security.get("sniper_percent") or info.get("sniper_percent") or info.get("top70_sniper_hold_rate"))
    rug = _pct(security.get("rug_ratio") or security.get("rug_risk") or info.get("rug_ratio"))
    holders = _num(_dig(info, "stat", "holder_count") or info.get("holder_count") or info.get("holders"))

    creator_status = str(
        _first(
            _dig(info, "dev", "creator_token_status"),
            security.get("creator_token_status"),
            info.get("creator_token_status"),
        )
        or ""
    ).lower()
    if _flag(info.get("creator_close")) and not creator_status:
        creator_status = "creator_close"
    renounced_mint = security.get("renounced_mint")
    if renounced_mint is None:
        renounced_mint = info.get("renounced_mint")
    renounced_freeze = security.get("renounced_freeze_account")
    if renounced_freeze is None:
        renounced_freeze = info.get("renounced_freeze_account")
    buy_tax = _pct(_first(info.get("buy_tax"), security.get("buy_tax")))
    sell_tax = _pct(_first(info.get("sell_tax"), security.get("sell_tax")))
    handle = socials["twitter_username"] or _twitter_handle(_first(info.get("twitter_username"), info.get("twitter")))
    # Tweet URLs often carry impression/view counts in x_user_follower.
    from_status = "status/" in str(info.get("twitter") or "") or "status/" in str(info.get("twitter_username") or "")
    followers = int(_num(_first(info.get("x_user_follower"), info.get("twitter_follower"), info.get("twitter_followers"))))
    if not handle or from_status or followers > 80_000_000:
        followers = 0
    return {
        "source": "gmgn",
        "url": gmgn_token_url(mint, chain),
        "honeypot": honeypot,
        "bundled": bundled,
        "insider_pct": round(insider, 2),
        "sniper_pct": round(sniper, 2),
        "rug_risk": round(rug, 2),
        "holder_count": int(holders) if holders else 0,
        "smart_degen": int(_num(tags.get("smart_wallets") or info.get("smart_degen_count") or security.get("smart_degen_count"))),
        "renowned": int(_num(tags.get("renowned_wallets") or info.get("renowned_count") or security.get("renowned_count"))),
        "bundler_rate": round(_pct(info.get("bundler_rate") or security.get("bundler_rate")), 2),
        "fresh_wallet_pct": round(_pct(stat.get("fresh_wallet_rate") or info.get("fresh_wallet_rate")), 2),
        "top10_pct": round(_pct(stat.get("top_10_holder_rate") or info.get("top_10_holder_rate") or info.get("top10_holder_rate")), 2),
        # Research-backed extras (arXiv 2602.14860: bot share lowers success;
        # GMGN DD card: wash trading / sniper count / dev status hard stops).
        "bot_rate": round(_pct(stat.get("bot_degen_rate") or info.get("bot_degen_rate")), 2),
        "wash_trading": _flag(security.get("is_wash_trading") or info.get("is_wash_trading")),
        "sniper_count": int(_num(security.get("sniper_count") or info.get("sniper_count") or tags.get("sniper_wallets"))),
        "bundler_vol_pct": round(_pct(
            security.get("bundler_trader_amount_rate")
            or info.get("bundler_trader_amount_rate")
            or stat.get("top_bundler_trader_percentage")
        ), 2),
        "rat_vol_pct": round(_pct(
            security.get("rat_trader_amount_rate")
            or info.get("rat_trader_amount_rate")
            or stat.get("top_rat_trader_percentage")
        ), 2),
        "dev_sold": creator_status in {"creator_close", "sell", "sold"},
        "dev_hold_pct": round(_pct(
            stat.get("creator_hold_rate")
            or security.get("creator_balance_rate")
            or info.get("creator_balance_rate")
            or info.get("dev_team_hold_rate")
        ), 2),
        "renounced": bool(renounced_mint) and bool(renounced_freeze) if renounced_mint is not None else None,
        "creator_open_count": int(_num(
            _first(_dig(info, "dev", "creator_open_count"), info.get("creator_created_open_count"), info.get("creator_created_count"))
        )),
        "creator_ath_mc": _num(_dig(info, "dev", "ath_token_info", "ath_mc")),
        "fund_from": str(_first(_dig(info, "dev", "fund_from"), info.get("fund_from_address"), info.get("fund_from")) or ""),
        "dexscr_paid": _flag(dev.get("dexscr_ad") or info.get("dexscr_ad")) or _flag(dev.get("dexscr_boost_fee") or info.get("dexscr_boost_fee")),
        "migration_mcap": _num(info.get("migration_market_cap")),
        "buy_vol_1h": _num(_first(price.get("buy_volume_1h"), info.get("buy_volume_1h"))),
        "sell_vol_1h": _num(_first(price.get("sell_volume_1h"), info.get("sell_volume_1h"))),
        # Trenches card fields we already download and were dropping.
        "og": _flag(info.get("og")),
        "buy_tax_pct": round(buy_tax, 2),
        "sell_tax_pct": round(sell_tax, 2),
        "progress": _progress(_first(info.get("launchpad_progress"), info.get("progress"))),
        "lock_pct": round(_pct(_first(info.get("lock_percent"), info.get("locked_ratio"), security.get("lock_percent"))), 2),
        "burned": _burned(info, security),
        "burn_pct": round(_pct(_first(info.get("dev_token_burn_ratio"), info.get("burn_ratio"), security.get("burn_ratio"))), 2),
        "hot_level": int(_num(_first(info.get("hot_level"), price.get("hot_level")))),
        "swaps_1h": int(_num(_first(info.get("swaps_1h"), price.get("swaps_1h")))),
        "volume_1h": _num(_first(info.get("volume_1h"), price.get("volume_1h"))),
        "net_buy_24h": _num(info.get("net_buy_24h")),
        "cto": _flag(info.get("cto_flag") or dev.get("cto_flag")),
        "open_source": _flag(_first(info.get("is_open_source"), info.get("open_source"), security.get("open_source"))),
        "callout_count": int(_num(info.get("callout_count"))),
        "image_dup": int(_num(info.get("image_dup"))),
        "creator_status": creator_status,
        "twitter_username": handle,
        "twitter_followers": followers,
        "launchpad": str(_first(info.get("launchpad_platform"), info.get("launchpad")) or ""),
        "liquidity": _num(info.get("liquidity")),
    }


def summarize_trench_row(row: dict[str, Any], mint: str = "", chain: str = "sol") -> dict[str, Any]:
    addr = mint or row.get("address") or ""
    out = summarize(row, {}, addr, chain=chain)
    if out:
        out["from_trenches"] = True
    return out


def trench_to_coin(row: dict[str, Any], chain: str = "sol") -> dict[str, Any]:
    mint = row.get("address") or ""
    created = _ts(
        row.get("created_timestamp")
        or row.get("create_timestamp")
        or row.get("created_at")
        or row.get("create_time")
    )
    opened = _ts(
        row.get("open_timestamp")
        or row.get("complete_timestamp")
        or row.get("migrated_timestamp")
        or row.get("open_time")
    )
    socials = _socials_from_row(row)
    twitter = socials["twitter"]
    if socials["twitter_username"]:
        twitter = (
            twitter
            if twitter.startswith("http") and "status/" not in twitter
            else f"https://x.com/{socials['twitter_username']}"
        )
    elif "status/" in twitter:
        twitter = ""
    return {
        "mint": mint,
        "name": row.get("name") or "",
        "symbol": row.get("symbol") or "",
        "description": "",
        "image_url": row.get("logo") or row.get("image") or "",
        "twitter": twitter,
        "website": socials["website"],
        "telegram": socials["telegram"],
        "creator": row.get("creator_address") or row.get("creator") or "",
        "complete": True,
        "nsfw": False,
        "banned": False,
        "reply_count": 0,
        "created_at": created,
        "updated_at": opened or created,
        "mcap_usd": _num(row.get("usd_market_cap") or row.get("market_cap") or row.get("mc")),
        "ath_mcap": _num(row.get("ath_market_cap") or row.get("ath_mc")),
        "pool_address": row.get("biggest_pool_address") or row.get("pool_address") or "",
        "gmgn_row": row,
        "raw": row,
        "chain": normalize_chain(chain),
        "launchpad": row.get("launchpad_platform") or row.get("launchpad") or "",
    }


def _trench_section(limit: int, platforms: list[str] | None) -> dict[str, Any]:
    section = {
        "filters": ["offchain", "onchain"],
        "launchpad_platform_v2": True,
        "limit": min(80, max(1, limit)),
    }
    # Omit launchpad_platform to use every platform on the chain (Robinhood
    # PONS + Flap/Klik/Noxa). An empty/missing list must not fall back to Pump.fun.
    if platforms:
        section["launchpad_platform"] = platforms
    return section


def _trenches_body(limit: int, platforms: list[str] | None) -> dict[str, Any]:
    # One POST. new_creation is how PONS (instant Uniswap V3) shows up;
    # completed/near still cover Flap-style bonding-curve graduates.
    return {
        "version": "v2",
        "completed": _trench_section(limit, platforms),
        "near_completion": _trench_section(limit, platforms),
        "new_creation": _trench_section(limit, platforms),
    }


def _platforms_for(chain: str, platforms: list[str] | None) -> list[str] | None:
    if platforms is not None:
        return platforms
    return SOL_PLATFORMS if normalize_chain(chain) == "sol" else None


async def completed_trenches(*, limit: int = 40, platforms: list[str] | None = None, chain: str = "sol") -> list[dict[str, Any]]:
    """Official GMGN 'Migrated Tokens' skill: POST /v1/trenches type=completed."""
    platforms = _platforms_for(chain, platforms)
    payload = await _request(
        "POST",
        "/v1/trenches",
        params=_params(chain=chain),
        json_body={"version": "v2", "completed": _trench_section(limit, platforms)},
    )
    rows = payload.get("completed") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("address")]


async def near_completion_trenches(*, limit: int = 40, platforms: list[str] | None = None, chain: str = "sol") -> list[dict[str, Any]]:
    platforms = _platforms_for(chain, platforms)
    payload = await _request(
        "POST",
        "/v1/trenches",
        params=_params(chain=chain),
        json_body={"version": "v2", "near_completion": _trench_section(limit, platforms)},
    )
    rows = (payload.get("pump") or payload.get("near_completion")) if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("address")]


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        if key not in payload:
            continue
        rows = payload.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict) and r.get("address")]
    return []


async def trenches(*, chain: str = "sol", limit: int = 40, platforms: list[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """One weight-3 POST: completed + near-completion + new_creation."""
    platforms = _platforms_for(chain, platforms)
    payload = await _request(
        "POST",
        "/v1/trenches",
        params=_params(chain=chain),
        json_body=_trenches_body(limit, platforms),
    )
    if not isinstance(payload, dict):
        return [], [], []
    completed = _rows(payload, "completed")
    graduating = _rows(payload, "pump", "near_completion")
    created = _rows(payload, "new_creation")
    return completed, graduating, created


async def top_holders(mint: str, limit: int = 50, *, chain: str = "sol") -> list[dict[str, Any]]:
    """Per-wallet holder rows (weight-5 endpoint — call only when gated)."""
    if not gmgn_deep_available():
        return []
    payload = await _request(
        "GET",
        "/v1/market/token_top_holders",
        params=_params(
            {"address": mint, "limit": min(100, max(1, limit)), "orderby": "amount_percentage", "direction": "desc"},
            chain=chain,
        ),
    )
    rows = payload.get("list") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def analyze_holders(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Wallet forensics on top-holder rows: the early structure that
    separates organic coins from coordinated dumps.

    - funding_cluster: share of wallets funded by the single most common
      funding source (native_transfer.address). Organic books are diverse;
      scams fund many "buyers" from one or two wallets.
    - early_exit: fraction that already sold >=90% of what they bought.
    - sniper_retention: snipers still holding vs all snipers.
    - diamond: holders who kept >=70% and are still in.
    - suspicious / fresh counts from GMGN's own wallet flags.
    """
    wallets = [r for r in rows if r.get("addr_type") != 2]  # exclude pools/exchanges
    n = len(wallets)
    if n == 0:
        return {}
    funders: dict[str, int] = {}
    early_exit = diamond = suspicious = fresh = snipers = snipers_holding = 0
    for w in wallets:
        nt = w.get("native_transfer") or {}
        src = nt.get("address") if isinstance(nt, dict) else None
        if src:
            funders[src] = funders.get(src, 0) + 1
        sold = float(w.get("sell_amount_percentage") or 0.0)
        holding = w.get("end_holding_at") in (None, 0, "")
        if sold >= 0.9:
            early_exit += 1
        if sold <= 0.3 and holding:
            diamond += 1
        if w.get("is_suspicious"):
            suspicious += 1
        if w.get("is_new"):
            fresh += 1
        tags = (w.get("tags") or []) + (w.get("maker_token_tags") or [])
        if "sniper" in tags:
            snipers += 1
            if holding:
                snipers_holding += 1
    top_cluster = max(funders.values()) if funders else 0
    return {
        "n": n,
        "funding_cluster": round(top_cluster / n, 3),
        "funding_sources": len(funders),
        "early_exit": round(early_exit / n, 3),
        "diamond": round(diamond / n, 3),
        "suspicious": suspicious,
        "fresh": fresh,
        "snipers": snipers,
        "sniper_retention": round(snipers_holding / snipers, 3) if snipers else None,
        # remembered so runners can credit these wallets as proven-early
        "top_addresses": [str(w.get("address")) for w in wallets[:15] if w.get("address")],
    }


async def token_research(mint: str, seed: dict[str, Any] | None = None, *, chain: str = "sol") -> dict[str, Any]:
    """Read-only GMGN token + security snapshot. No private key / no swaps."""
    # Trench seed is free. Extra /token/info + /security on every RH launch
    # helped burn the account into RATE_LIMIT_BANNED. Holders still come
    # from the seed via holders_from_gmgn.
    if seed and seed.get("source") == "gmgn":
        return seed
    if not settings.gmgn_api_key or not gmgn_deep_available():
        return seed or {}
    info = await _get("/v1/token/info", mint, chain=chain)
    security = await _get("/v1/token/security", mint, chain=chain)
    out = summarize(info, security, mint, chain=chain)
    return out or (seed or {})


def _unix(ts: datetime | int | float | None) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int(ts.timestamp())
    try:
        n = float(ts)
    except (TypeError, ValueError):
        return None
    if n > 1e12:
        n /= 1000.0
    return int(n)


def parse_kline_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize GMGN kline rows to ``{time, open, high, low, close, volume}``.

    ``volume`` is USD. Holders and liquidity are not on this endpoint —
    do not invent them from the current token/info snapshot.
    """
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        inner = payload.get("list") or payload.get("kline") or payload.get("items") or []
        rows = inner if isinstance(inner, list) else []
    else:
        rows = []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _ts(row.get("time") or row.get("t") or row.get("timestamp"))
        close = _num(row.get("close") or row.get("c") or row.get("price"))
        if ts is None or close <= 0:
            continue
        high = _num(row.get("high") or row.get("h")) or close
        low = _num(row.get("low") or row.get("l")) or close
        opened = _num(row.get("open") or row.get("o")) or close
        out.append(
            {
                "time": ts,
                "open": opened,
                "high": high,
                "low": low,
                "close": close,
                "volume": _num(row.get("volume") or row.get("v")),
            }
        )
    out.sort(key=lambda c: c["time"])
    return out


async def token_kline(
    mint: str,
    *,
    chain: str = "sol",
    resolution: str = "1m",
    from_ts: datetime | int | None = None,
    to_ts: datetime | int | None = None,
) -> list[dict[str, Any]]:
    """Historical OHLCV. Weight-2. Empty when GMGN is dark or the mint has no book."""
    if not mint or not gmgn_available():
        return []
    extra: dict[str, Any] = {"address": mint, "resolution": resolution}
    start = _unix(from_ts)
    end = _unix(to_ts)
    if start is not None:
        extra["from"] = str(start)
    if end is not None:
        extra["to"] = str(end)
    payload = await _request("GET", "/v1/market/token_kline", params=_params(extra, chain=chain))
    return parse_kline_rows(payload)


def _gmgn_rank_list_from_payload(payload: dict[str, Any]) -> tuple[list[Any], list[str]]:
    """GMGN market rank: usually ``data.rank``; unwrap nested ``data`` layers."""
    keys_trace: list[str] = []
    cur: Any = payload
    for _ in range(5):
        if not isinstance(cur, dict):
            break
        keys_trace.append(",".join(sorted(str(k) for k in cur.keys())[:10]))
        for key in ("rank", "list", "tokens", "coins"):
            rows = cur.get(key)
            if isinstance(rows, list):
                return rows, keys_trace
        nxt = cur.get("data")
        if isinstance(nxt, dict):
            cur = nxt
            continue
        break
    return [], keys_trace


def _mint_from_gmgn_rank_row(row: dict[str, Any]) -> str:
    for key in ("address", "token_address", "mint", "tokenAddress"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    token = row.get("token")
    if isinstance(token, dict):
        return str(token.get("address") or token.get("token_address") or "").strip()
    return ""


async def sol_trending_rank_rows(
    *,
    limit: int = 40,
    interval: str = "1h",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """GMGN swap-trending rank (weight-1). Not FOMO app Tokens→Trending."""
    from ..models import utcnow

    now = utcnow()
    meta: dict[str, Any] = {
        "board_kind": "gmgn_trending",
        "api_source": "gmgn",
        "captured_at": now.isoformat(),
        "capture_age_hours": 0.0,
        "board_live": False,
        "board_stale": False,
        "interval": interval,
    }
    if not gmgn_available():
        meta["skipped"] = True
        meta["skip_reason"] = "cooldown_or_no_key"
        meta["empty_reason"] = meta["skip_reason"]
        meta["cooldown_s"] = round(gmgn_cooldown_remaining_s(), 1)
        return [], meta
    payload = await _request(
        "GET",
        "/v1/market/rank",
        params=_params(
            {
                "interval": interval,
                "limit": min(80, max(1, limit)),
                "orderby": "volume",
            },
            chain="sol",
        ),
    )
    if not payload:
        meta["skipped"] = True
        meta["skip_reason"] = (
            "rate_limit_or_cooldown" if gmgn_cooldown_remaining_s() > 0 else "gmgn_empty_response"
        )
        meta["empty_reason"] = meta["skip_reason"]
        meta["error"] = True
        meta["cooldown_s"] = round(gmgn_cooldown_remaining_s(), 1)
        return [], meta
    raw, keys_trace = _gmgn_rank_list_from_payload(payload)
    meta["response_keys"] = keys_trace
    if not raw:
        meta["empty_reason"] = "no_rank_list_in_payload"
        meta["payload_sample_keys"] = keys_trace[-1] if keys_trace else ""
        log.warning(
            "GMGN rank empty — payload key trace %s",
            keys_trace,
        )
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(raw[: max(1, limit)], start=1):
        if not isinstance(row, dict):
            continue
        mint = _mint_from_gmgn_rank_row(row)
        if not mint:
            continue
        sym = str(row.get("symbol") or row.get("name") or "")
        rows.append(
            {
                "mint": mint,
                "chain": "sol",
                "symbol": sym,
                "rank": int(row.get("rank") or i),
                "mcap_usd": _num(row.get("market_cap") or row.get("marketcap") or row.get("mc")),
            }
        )
    meta["board_live"] = bool(rows)
    meta["item_count"] = len(rows)
    if not rows and not meta.get("empty_reason"):
        meta["empty_reason"] = "rank_rows_had_no_mints"
    return rows, meta
