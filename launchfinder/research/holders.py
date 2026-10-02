from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..httputil import get_json, post_json

log = logging.getLogger("launchfinder.holders")

# Blockscout 403s the default httputil UA (`new-launch-finder/0.1`).
# Live THEINVESTOR sat 7h with "No wallet map yet" because RH
# holder_stats was a no-op and holders_from_gmgn has no addresses.
BLOCKSCOUT_RH = "https://robinhoodchain.blockscout.com"
BLOCKSCOUT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewLaunchFinder/1.0)",
    "Accept": "application/json",
}

# Helius DAS (`getTokenAccounts`) is 10 credits a page. The shared key used
# to 429 (`max usage reached`, v68) and ingest-time Sol holders went blank.
# The account is now a 10M-credit Developer plan, so Hunt can refresh any
# fillable book — not only legacy 0.70 desk-line cards — inside an hourly
# page budget that still leaves most of the month for ingest / RPC / WS.
# A 429 still parks the optional Hunt refresh 30 min so ingest keeps the key.
SOL_HOLDERS_REFRESH_SECONDS = 300
SOL_HOLDER_MIN_ENTRY = 0.10  # Sol first-sight watch line
SOL_HOLDER_FILLABLE_LIQ = 5_000.0  # same bar as LEDGER_SELLABLE_LIQ
SOL_HOLDER_RUNNER_MULTIPLE = 2.0
SOL_HOLDER_TAPE_PAGES = 2
# 2× runners spend more DAS pages so unique owners can leave a 2-page
# sample (TIPPED sat at 522). Hourly cap stays 300 — do not raise it.
SOL_HOLDER_TAPE_PAGES_RUNNER = 5
# Ingest / paper-open: one DAS page, then background enrich. Full
# pagination blocked SAPLING-class opens (~90s) into a late chase.
SOL_HOLDER_OPEN_PAGES = 1
HELIUS_CAPPED_BACKOFF_SECONDS = 30 * 60
_helius_capped_until: float = 0.0
_das_budget: dict[str, float] = {"hour": -1.0, "used": 0.0}


def _note_helius_status(status: int, body: str) -> None:
    """post_json hook: a 429 parks the optional Hunt refresh for a while."""
    global _helius_capped_until
    if status == 429:
        _helius_capped_until = time.monotonic() + HELIUS_CAPPED_BACKOFF_SECONDS
        log.warning("Helius 429 (%s): Sol Hunt holder refresh parked %ss", (body or "").strip()[:60], HELIUS_CAPPED_BACKOFF_SECONDS)


def helius_capped() -> bool:
    return time.monotonic() < _helius_capped_until


def historical_holder_count_at(mint: str, at: datetime | None) -> None:
    """Helius DAS / getTokenLargestAccounts are a *current* snapshot.

    Stamping today's holder_count onto a t+15 print from last week would
    invent ``holder_growth``. The live Hunt tape still refreshes DAS now;
    historical Live samples leave holders at 0. ``mint`` / ``at`` are
    accepted so callers can write the honest no-op in one place.
    """
    _ = (mint, at)
    return None


def sol_holder_refresh_due(research: Any, outcome: Any) -> bool:
    """True when this Sol Hunt card should spend DAS pages.

    The 0.70 legacy desk-line gate left first-sight Sol (ceiling ~0.15)
    never refreshing. With a 10M-credit Helius plan we refresh any
    fillable book, any card at the Sol watch line, or a 2× runner.
    Thin dust still skips. The hourly page budget is the real cap.
    """
    p = float(getattr(research, "p_good", 0.0) or 0.0)
    if p >= SOL_HOLDER_MIN_ENTRY:
        return True
    liq = float(getattr(outcome, "last_liq", 0.0) or 0.0) if outcome is not None else 0.0
    if liq >= SOL_HOLDER_FILLABLE_LIQ:
        return True
    return sol_holder_is_runner(outcome)


def sol_holder_book_multiple(outcome: Any) -> float:
    if outcome is None:
        return 0.0
    multiple = float(getattr(outcome, "multiple", 0.0) or 0.0)
    last = float(getattr(outcome, "last_mcap", 0.0) or 0.0)
    t0 = float(getattr(outcome, "t0_mcap", 0.0) or 0.0)
    if t0 > 0 and last > 0:
        multiple = max(multiple, last / t0)
    return multiple


def sol_holder_is_runner(outcome: Any) -> bool:
    return sol_holder_book_multiple(outcome) >= SOL_HOLDER_RUNNER_MULTIPLE


def sol_holder_tape_pages(research: Any, outcome: Any) -> int:
    """2 pages for a watch/fillable book; 5 for a 2× runner.

    DAS unique-owners from two 1000-row pages can freeze a fat runner
    at its ingest print (TIPPED 522). Extra pages stay inside the
    hourly 300-page cap. ``research`` is accepted so the call site
    matches the other due/rank helpers.
    """
    _ = research
    if sol_holder_is_runner(outcome):
        return SOL_HOLDER_TAPE_PAGES_RUNNER
    return SOL_HOLDER_TAPE_PAGES


def sol_holder_refresh_rank(token: Any) -> tuple[float, float]:
    """Stale 2× first, then Entry. A TIPPED-class runner beats a 0.91 dust."""
    research = getattr(token, "research", None)
    outcome = getattr(token, "outcome", None)
    p = float(getattr(research, "p_good", 0.0) or 0.0) if research is not None else 0.0
    runner = 1.0 if sol_holder_is_runner(outcome) else 0.0
    return (runner, p)


def reset_helius_budget() -> None:
    global _helius_capped_until
    _helius_capped_until = 0.0
    _das_budget["hour"] = -1.0
    _das_budget["used"] = 0.0


def take_das_pages(n: int, *, limit: int | None = None) -> bool:
    """Reserve ``n`` DAS pages from this hour's Hunt budget. False = skip."""
    cap = int(settings.sol_holder_das_pages_per_hour if limit is None else limit)
    if cap <= 0:
        return False
    hour = time.monotonic() // 3600
    if _das_budget["hour"] != hour:
        _das_budget["hour"] = hour
        _das_budget["used"] = 0.0
    if _das_budget["used"] + n > cap:
        return False
    _das_budget["used"] += n
    return True


async def holder_stats(
    mint: str,
    creator: str = "",
    pool_address: str = "",
    chain: str = "sol",
    *,
    max_pages: int | None = None,
    annotate: bool = False,
) -> dict[str, Any]:
    if not mint:
        return {}
    from ..chains import normalize_chain

    if normalize_chain(chain) == "robinhood":
        return await _blockscout_holders(mint, creator, pool_address)
    if normalize_chain(chain) != "sol":
        return {}
    pages = SOL_HOLDER_OPEN_PAGES if max_pages is None else max(1, int(max_pages))
    if settings.helius_api_key and not helius_capped():
        das = await _das_owners(mint, creator, pool_address, max_pages=pages)
        if das:
            if annotate:
                das["top_wallets"] = await _annotate_wallets(das.get("top_wallets") or [])
            das["fresh_wallet_pct"] = _fresh_share(das.get("top_wallets") or [])
            das["open_pages"] = pages
            return das
    return await _largest_accounts_fallback(mint)


def holders_from_gmgn(gmgn: dict[str, Any] | None) -> dict[str, Any]:
    """Helius/Solana RPC do not exist on Robinhood. GMGN already fetched
    holder_count / top10 / creator hold on the research call — reuse them
    so the RH desk is not stuck at 0 holders."""
    if not gmgn:
        return {}
    n = int(gmgn.get("holder_count") or 0)
    top10 = float(gmgn.get("top10_pct") or 0.0)
    if n <= 0 and top10 <= 0:
        return {}
    return {
        "holder_count": n,
        "holder_sample": n,
        "top10_pct": top10,
        "top1_pct": 0.0,
        "creator_hold_pct": float(gmgn.get("dev_hold_pct") or 0.0),
        "fresh_wallet_pct": float(gmgn.get("fresh_wallet_pct") or 0.0),
        "top_wallets": list(gmgn.get("top_wallets") or []),
        "source": "gmgn",
    }


def rh_wallet_map_missing(research: Any) -> bool:
    """True when the detail card would still say 'No wallet map yet'."""
    if research is None:
        return False
    try:
        raw = json.loads(getattr(research, "raw_json", None) or "{}")
    except json.JSONDecodeError:
        raw = {}
    holders = raw.get("holders") if isinstance(raw, dict) else {}
    wallets = (holders or {}).get("top_wallets") if isinstance(holders, dict) else None
    return not wallets


HOLDERS_REFRESH_SECONDS = 120.0
HOLDERS_HISTORY_MAX = 8
HOLDERS_HISTORY_GAP_SEC = 120.0
HOLDERS_STALE_MIN = 45.0
HOLDERS_PREV_MIN_AGE_MIN = 5.0
HOLDERS_GROW_MIN = 15
HOLDERS_GROW_RATIO = 1.12
HOLDERS_SHRINK_RATIO = 0.85
HOLDERS_SHRINK_FLOOR = 20


def append_holder_history(
    holders: dict[str, Any],
    n: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Keep a short holder_count series so Live can see growth, not one print."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return holders
    rows = holders.get("history") if isinstance(holders.get("history"), list) else []
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            prev_n = int(row.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if prev_n <= 0:
            continue
        cleaned.append({"n": prev_n, "at": str(row.get("at") or "")})
    last = cleaned[-1] if cleaned else None
    skip = False
    if last is not None:
        if last["n"] == n:
            skip = True
        else:
            try:
                ts = datetime.fromisoformat(str(last.get("at") or "").replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (now - ts).total_seconds() < HOLDERS_HISTORY_GAP_SEC:
                    skip = True
                    cleaned[-1] = {"n": n, "at": now.isoformat()}
            except ValueError:
                skip = False
    if not skip:
        cleaned.append({"n": n, "at": now.isoformat()})
    holders["history"] = cleaned[-HOLDERS_HISTORY_MAX:]
    return holders


def holder_tape(research: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Live holder print + previous print. Missing history is not shrinking."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    n = int(getattr(research, "holder_count", 0) or 0) if research is not None else 0
    try:
        raw = json.loads(getattr(research, "raw_json", None) or "{}") if research is not None else {}
    except json.JSONDecodeError:
        raw = {}
    holders = raw.get("holders") if isinstance(raw, dict) else {}
    if not isinstance(holders, dict):
        holders = {}
    if n <= 0:
        try:
            n = int(holders.get("holder_count") or 0)
        except (TypeError, ValueError):
            n = 0
    age_min = None
    taken = holders.get("taken_at")
    if taken:
        age_min = holders_snapshot_age_sec(research, now=now) / 60.0
        if age_min >= 1e6:
            age_min = None
    prev = None
    history = holders.get("history") if isinstance(holders.get("history"), list) else []
    for row in reversed(history):
        if not isinstance(row, dict):
            continue
        try:
            prev_n = int(row.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if prev_n <= 0:
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        minutes = (now - ts).total_seconds() / 60.0
        if minutes >= HOLDERS_PREV_MIN_AGE_MIN:
            prev = prev_n
            break
    growing = bool(
        prev
        and n >= prev + HOLDERS_GROW_MIN
        and n >= prev * HOLDERS_GROW_RATIO
    )
    shrinking = bool(
        prev
        and prev >= HOLDERS_SHRINK_FLOOR
        and n < prev * HOLDERS_SHRINK_RATIO
    )
    return {
        "n": n,
        "prev": prev,
        "age_min": age_min,
        "growing": growing,
        "shrinking": shrinking,
    }


def holders_snapshot_age_sec(research: Any, *, now: datetime | None = None) -> float:
    """Seconds since raw.holders.taken_at. Missing stamp is stale."""
    if research is None:
        return 1e9
    try:
        raw = json.loads(getattr(research, "raw_json", None) or "{}")
    except json.JSONDecodeError:
        raw = {}
    holders = raw.get("holders") if isinstance(raw, dict) else {}
    taken = (holders or {}).get("taken_at") if isinstance(holders, dict) else None
    if not taken:
        return 1e9
    try:
        ts = datetime.fromisoformat(str(taken).replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds())


async def blockscout_token_meta(mint: str) -> dict[str, Any]:
    """RH token name + holders_count. One GET. No wallet map. No GMGN."""
    if not mint:
        return {}
    data = await get_json(f"{BLOCKSCOUT_RH}/api/v2/tokens/{mint}", headers=BLOCKSCOUT_HEADERS)
    if not isinstance(data, dict):
        return {}
    try:
        n = int(data.get("holders_count") or 0)
    except (TypeError, ValueError):
        n = 0
    return {
        "holder_count": n,
        "name": str(data.get("name") or "").strip(),
        "symbol": str(data.get("symbol") or "").strip(),
        "image_url": str(data.get("icon_url") or "").strip(),
        "source": "blockscout",
    }


def apply_rh_holder_meta(token: Any, meta: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    """Write Blockscout name + holder_count. Display only. Does not lift p."""
    from ..chains import token_chain
    from ..scoring.hunt_tape import apply_hunt_tape_identity

    research = getattr(token, "research", None)
    if token_chain(token) != "robinhood" or research is None:
        return False
    if not meta:
        return False
    now = now or datetime.now(timezone.utc)
    wrote = apply_hunt_tape_identity(token, meta)
    n = 0
    try:
        n = int(meta.get("holder_count") or 0)
    except (TypeError, ValueError):
        n = 0
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    prev = raw.get("holders") if isinstance(raw.get("holders"), dict) else {}
    next_holders = {**prev, "taken_at": now.isoformat()}
    if n > 0:
        research.holder_count = n
        next_holders["holder_count"] = n
        if not next_holders.get("source"):
            next_holders["source"] = "blockscout"
        append_holder_history(next_holders, n, now=now)
        wrote = True
    raw["holders"] = next_holders
    research.raw_json = json.dumps(raw, default=str)
    return wrote


async def refresh_rh_hunt_holder_meta(
    session: Any,
    tokens: list[Any],
    *,
    now: datetime | None = None,
    limit: int = 24,
) -> int:
    """Blockscout holders_count + name for due this-window RH Hunt cards."""
    from ..scoring.hunt import upsert_hunt

    now = now or datetime.now(timezone.utc)
    due: list[Any] = []
    for token in tokens:
        research = getattr(token, "research", None)
        if research is None:
            continue
        if holders_snapshot_age_sec(research, now=now) < HOLDERS_REFRESH_SECONDS:
            continue
        due.append(token)
        if len(due) >= max(0, int(limit)):
            break
    if not due:
        return 0
    sem = asyncio.Semaphore(8)

    async def _one(token: Any) -> tuple[Any, dict[str, Any]]:
        async with sem:
            try:
                meta = await blockscout_token_meta(token.mint)
            except Exception:
                meta = {}
            return token, meta

    rows = await asyncio.gather(*[_one(token) for token in due])
    wrote = 0
    for token, meta in rows:
        if apply_rh_holder_meta(token, meta, now=now):
            upsert_hunt(session, token, now=now, touch_updated=False)
            wrote += 1
    if wrote:
        log.info("RH hunt holders refreshed %s cards", wrote)
    return wrote


def apply_sol_holder_meta(token: Any, meta: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    """Write a fresh Helius holder print onto a Sol card. Display + Live tape only.
    Does not lift p. Entry stays frozen."""
    from ..chains import token_chain

    research = getattr(token, "research", None)
    if token_chain(token) != "sol" or research is None or not meta:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        n = int(meta.get("holder_count") or 0)
    except (TypeError, ValueError):
        n = 0
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    prev = raw.get("holders") if isinstance(raw.get("holders"), dict) else {}
    next_holders = {**prev, "taken_at": now.isoformat()}
    wrote = False
    if n > 0:
        research.holder_count = n
        next_holders["holder_count"] = n
        next_holders["source"] = str(meta.get("source") or "helius")
        top10 = float(meta.get("top10_pct") or 0.0)
        if top10 > 0:
            research.top10_pct = top10
            next_holders["top10_pct"] = top10
        if meta.get("top_wallets"):
            next_holders["top_wallets"] = list(meta.get("top_wallets") or [])[:12]
        append_holder_history(next_holders, n, now=now)
        wrote = True
    raw["holders"] = next_holders
    research.raw_json = json.dumps(raw, default=str)
    return wrote


async def refresh_sol_hunt_holder_meta(
    session: Any,
    tokens: list[Any],
    *,
    now: datetime | None = None,
    limit: int = 12,
) -> int:
    """Helius holder count for due this-window Sol Hunt cards.

    2× runners go first and spend 5 DAS pages so unique owners can leave
    a 2-page sample (TIPPED stayed 522). Watch/fillable books stay at 2
    pages. A 5-page card that does not fit the hourly cap yields to a
    cheaper 2-page book. Parked while the key is 429-capped. No GMGN.
    Does not lift p. Entry stays frozen.
    """
    from ..scoring.hunt import upsert_hunt

    if not settings.helius_api_key or helius_capped():
        return 0
    now = now or datetime.now(timezone.utc)
    due: list[tuple[Any, int]] = []
    ranked = sorted(tokens, key=sol_holder_refresh_rank, reverse=True)
    for token in ranked:
        research = getattr(token, "research", None)
        if research is None:
            continue
        outcome = getattr(token, "outcome", None)
        if not sol_holder_refresh_due(research, outcome):
            continue
        if holders_snapshot_age_sec(research, now=now) < SOL_HOLDERS_REFRESH_SECONDS:
            continue
        pages = sol_holder_tape_pages(research, outcome)
        if not take_das_pages(pages):
            if pages > SOL_HOLDER_TAPE_PAGES:
                continue
            break
        due.append((token, pages))
        if len(due) >= max(0, int(limit)):
            break
    if not due:
        return 0
    sem = asyncio.Semaphore(2)

    async def _one(token: Any, pages: int) -> tuple[Any, dict[str, Any]]:
        async with sem:
            if helius_capped():
                return token, {}
            try:
                meta = await _das_owners(token.mint, token.creator or "", token.pool_address or "", max_pages=pages)
            except Exception:
                meta = {}
            return token, meta

    rows = await asyncio.gather(*[_one(token, pages) for token, pages in due])
    wrote = 0
    for token, meta in rows:
        if apply_sol_holder_meta(token, meta, now=now):
            upsert_hunt(session, token, now=now, touch_updated=False)
            wrote += 1
    if wrote:
        log.info("Sol hunt holders refreshed %s cards", wrote)
    return wrote


def holders_from_blockscout(
    token_payload: dict[str, Any] | None,
    holders_payload: dict[str, Any] | None,
    *,
    creator: str = "",
    pool_address: str = "",
) -> dict[str, Any]:
    """Turn Blockscout token + holders JSON into the Sol holder_stats shape."""
    items = (holders_payload or {}).get("items") if isinstance(holders_payload, dict) else None
    if not isinstance(items, list) or not items:
        return {}
    creator_l = (creator or "").strip().lower()
    pool_l = (pool_address or "").strip().lower()
    rows: list[tuple[str, float, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        addr = item.get("address")
        owner = ""
        is_contract = False
        if isinstance(addr, dict):
            owner = str(addr.get("hash") or "")
            is_contract = bool(addr.get("is_contract"))
        elif addr:
            owner = str(addr)
        try:
            amt = float(item.get("value") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if not owner or amt <= 0:
            continue
        label = ""
        owner_l = owner.lower()
        if creator_l and owner_l == creator_l:
            label = "creator"
        elif pool_l and owner_l == pool_l:
            label = "pool"
        elif is_contract:
            label = "pool"
        rows.append((owner, amt, label))
    if not rows:
        return {}
    total = 0.0
    if isinstance(token_payload, dict):
        try:
            total = float(token_payload.get("total_supply") or 0)
        except (TypeError, ValueError):
            total = 0.0
    if total <= 0:
        total = sum(amt for _, amt, _ in rows)
    if total <= 0:
        return {}
    eoa = [(owner, amt) for owner, amt, label in rows if label != "pool"]
    top1 = (eoa[0][1] / total * 100.0) if eoa else 0.0
    top10 = sum(amt for _, amt in eoa[:10]) / total * 100.0 if eoa else 0.0
    creator_amt = 0.0
    if creator_l:
        for owner, amt, _label in rows:
            if owner.lower() == creator_l:
                creator_amt += amt
    wallets = [
        {"owner": owner, "pct": round(amt / total * 100.0, 2), "label": label}
        for owner, amt, label in rows[:12]
    ]
    n = 0
    if isinstance(token_payload, dict):
        try:
            n = int(token_payload.get("holders_count") or 0)
        except (TypeError, ValueError):
            n = 0
    if n <= 0:
        n = len(rows)
    return {
        "holder_count": n,
        "holder_sample": len(rows),
        "top1_pct": round(top1, 2),
        "top10_pct": round(top10, 2),
        "creator_hold_pct": round(creator_amt / total * 100.0, 2),
        "top_wallets": wallets,
        "source": "blockscout",
    }


async def _blockscout_holders(mint: str, creator: str, pool_address: str) -> dict[str, Any]:
    token_url = f"{BLOCKSCOUT_RH}/api/v2/tokens/{mint}"
    holders_url = f"{BLOCKSCOUT_RH}/api/v2/tokens/{mint}/holders"
    token_payload, holders_payload = await asyncio.gather(
        get_json(token_url, headers=BLOCKSCOUT_HEADERS),
        get_json(holders_url, headers=BLOCKSCOUT_HEADERS),
    )
    return holders_from_blockscout(
        token_payload if isinstance(token_payload, dict) else None,
        holders_payload if isinstance(holders_payload, dict) else None,
        creator=creator,
        pool_address=pool_address,
    )


async def hydrate_rh_wallet_map(session: Any, token: Any) -> bool:
    """Write Blockscout top wallets / holder_count onto a RH research row.

    Fills a missing wallet map, or refreshes a stale count. Display only.
    Does not rewrite features_json or lift p.
    """
    from ..chains import token_chain

    research = getattr(token, "research", None)
    if token_chain(token) != "robinhood" or research is None:
        return False
    missing = rh_wallet_map_missing(research)
    stale = holders_snapshot_age_sec(research) >= HOLDERS_REFRESH_SECONDS
    if not missing and not stale:
        return False
    stats = await holder_stats(
        token.mint,
        getattr(token, "creator", None) or "",
        getattr(token, "pool_address", None) or "",
        chain="robinhood",
    )
    if not stats.get("top_wallets"):
        meta = await blockscout_token_meta(token.mint)
        return apply_rh_holder_meta(token, meta)
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    prev = raw.get("holders") if isinstance(raw.get("holders"), dict) else {}
    now_stamp = datetime.now(timezone.utc)
    merged = {**prev, **stats, "taken_at": now_stamp.isoformat()}
    if int(stats.get("holder_count") or 0) > 0:
        append_holder_history(merged, int(stats["holder_count"]), now=now_stamp)
    raw["holders"] = merged
    research.raw_json = json.dumps(raw, default=str)
    if int(stats.get("holder_count") or 0) > 0:
        research.holder_count = int(stats["holder_count"])
    if float(stats.get("top10_pct") or 0) > 0:
        research.top10_pct = float(stats["top10_pct"])
    if stats.get("creator_hold_pct") is not None:
        research.creator_hold_pct = float(stats.get("creator_hold_pct") or 0)
    session.add(research)
    return True


PUBLIC_SOL_RPC = "https://api.mainnet-beta.solana.com"


async def _rpc(method: str, params: Any, *, url: str | None = None) -> dict[str, Any] | None:
    target = url or settings.rpc_url
    data = await post_json(
        target,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        on_status=_note_helius_status if (settings.helius_api_key and "helius" in target) else None,
    )
    return data if isinstance(data, dict) else None


async def _das_owners(mint: str, creator: str, pool_address: str, *, max_pages: int = 5) -> dict[str, Any]:
    rows: list[tuple[str, float]] = []
    page = 1
    while page <= max(1, int(max_pages)):
        data = await _rpc(
            "getTokenAccounts",
            {"mint": mint, "page": page, "limit": 1000, "options": {"showZeroBalance": False}},
        )
        result = (data or {}).get("result") if data else None
        accounts = (result or {}).get("token_accounts") if isinstance(result, dict) else None
        if not isinstance(accounts, list) or not accounts:
            break
        for acc in accounts:
            owner = acc.get("owner") or ""
            try:
                amt = float(acc.get("amount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if owner and amt > 0:
                rows.append((owner, amt))
        if len(accounts) < 1000:
            break
        page += 1
    if not rows:
        return {}
    merged: dict[str, float] = {}
    for owner, amt in rows:
        merged[owner] = merged.get(owner, 0.0) + amt
    ranked = sorted(merged.items(), key=lambda x: x[1], reverse=True)
    total = sum(merged.values())
    if total <= 0:
        return {}
    top1 = ranked[0][1] / total * 100.0
    top10 = sum(a for _, a in ranked[:10]) / total * 100.0
    creator_amt = merged.get(creator, 0.0)
    wallets = []
    for owner, amt in ranked[:12]:
        label = ""
        if creator and owner == creator:
            label = "creator"
        elif pool_address and owner == pool_address:
            label = "pool"
        wallets.append(
            {
                "owner": owner,
                "pct": round(amt / total * 100.0, 2),
                "label": label,
            }
        )
    return {
        "holder_count": len(merged),
        "holder_sample": len(merged),
        "top1_pct": round(top1, 2),
        "top10_pct": round(top10, 2),
        "creator_hold_pct": round(creator_amt / total * 100.0, 2),
        "top_wallets": wallets,
        "source": "helius",
    }


async def _largest_accounts_fallback(mint: str) -> dict[str, Any]:
    # A capped Helius key 429s plain RPC too. Best effort: ask the public
    # node (it throttles this call per IP) so a new launch may keep top10
    # instead of scoring blind. Empty on throttle; never trips the breaker.
    data = await _rpc("getTokenLargestAccounts", [mint], url=PUBLIC_SOL_RPC if helius_capped() else None)
    values = ((data or {}).get("result") or {}).get("value") if data else None
    if not isinstance(values, list) or not values:
        return {}
    amounts: list[float] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            amounts.append(float(item.get("uiAmount") or item.get("uiAmountString") or 0))
        except (TypeError, ValueError):
            continue
    total = sum(amounts)
    if total <= 0:
        return {"holder_sample": len(values), "source": "rpc_largest"}
    return {
        "holder_count": 0,
        "holder_sample": len(values),
        "top1_pct": round(amounts[0] / total * 100.0, 2),
        "top10_pct": round(sum(amounts[:10]) / total * 100.0, 2),
        "creator_hold_pct": 0.0,
        "top_wallets": [],
        "source": "rpc_largest",
    }


async def _annotate_wallets(wallets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not wallets:
        return []

    async def one(wallet: dict[str, Any]) -> dict[str, Any]:
        owner = wallet.get("owner") or ""
        if not owner or wallet.get("label") == "pool":
            return wallet
        data = await _rpc("getSignaturesForAddress", [owner, {"limit": 15}])
        sigs = (data or {}).get("result") if data else None
        if not isinstance(sigs, list) or not sigs:
            return {**wallet, "sigs": 0, "fresh": True, "age_hours": 0.0}
        now = time.time()
        newest = float(sigs[0].get("blockTime") or now)
        oldest = float(sigs[-1].get("blockTime") or newest)
        age_hours = max(0.0, (now - oldest) / 3600.0)
        fresh = len(sigs) <= 5 or age_hours < 6
        return {
            **wallet,
            "sigs": len(sigs),
            "fresh": fresh,
            "age_hours": round(age_hours, 1),
            "newest_hours": round(max(0.0, (now - newest) / 3600.0), 1),
        }

    return list(await asyncio.gather(*[one(w) for w in wallets]))


def _fresh_share(wallets: list[dict[str, Any]]) -> float:
    scored = [w for w in wallets if w.get("label") != "pool"]
    if not scored:
        return 0.0
    fresh = sum(1 for w in scored if w.get("fresh"))
    return round(100.0 * fresh / len(scored), 1)
