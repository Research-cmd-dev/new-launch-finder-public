"""Early-buyer wallets on analytics runners.

Helius parsed swaps in the first hour of the first pool. No extra GMGN.
Creator and pool addresses are dropped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..config import settings
from ..httputil import client
from ..models import EarlyWallet, EarlyWalletHit, FomoWallet, Outcome, ScanState, Token, utcnow

SKIP_OWNERS = frozenset(
    {
        "1nc1nerator11111111111111111111111111111111",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    }
)
EARLY_WINDOW_S = 3600.0
MIN_SIZED_SOL = 0.1
PROFIT_X = 5.0
STAMP_KEY = "early_wallets_v2"
SKIP_HARVEST_SOURCES = frozenset({"backfill", "rh_backfill"})
HARVEST_ATTEMPT_PREFIX = "eh:"
HARVEST_RETRY_S = 6 * 3600
HARVEST_LIMIT = 6


def parse_early_buys(
    txs: list[dict[str, Any]],
    *,
    mint: str,
    pool: str,
    creator: str = "",
    launched: datetime | None = None,
    mark_price: float = 0.0,
) -> list[dict[str, Any]]:
    """First inbound token transfer per wallet in the launch window."""
    mint = (mint or "").strip()
    pool = (pool or "").strip()
    creator = (creator or "").strip()
    skip = {pool, creator} | set(SKIP_OWNERS)
    first: dict[str, dict[str, Any]] = {}
    ordered = list(txs)
    ordered.sort(key=lambda t: int(t.get("timestamp") or 0))
    launch_ts = int(launched.timestamp()) if launched else 0
    for tx in ordered:
        try:
            ts = int(tx.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0
        if launch_ts and ts and ts < launch_ts:
            continue
        if launch_ts and ts and ts - launch_ts > EARLY_WINDOW_S:
            continue
        transfers = tx.get("tokenTransfers") if isinstance(tx.get("tokenTransfers"), list) else []
        for tr in transfers:
            if not isinstance(tr, dict):
                continue
            if str(tr.get("mint") or "").strip() != mint:
                continue
            dest = str(tr.get("toUserAccount") or "").strip()
            src = str(tr.get("fromUserAccount") or "").strip()
            if not dest or dest in skip:
                continue
            if src and src != pool:
                continue
            if dest in first:
                continue
            try:
                amount = float(tr.get("tokenAmount") or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            price = 0.0
            native = _native_sol(tx)
            if native > 0 and amount > 0:
                price = native / amount
            age = float(ts - launch_ts) if launch_ts and ts else 0.0
            mark_mult = (mark_price / price) if price > 0 and mark_price > 0 else 0.0
            first[dest] = {
                "owner": dest,
                "first_buy_ts": datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
                "age_at_buy_s": age,
                "sol_spent": native,
                "token_amount": amount,
                "entry_price": price,
                "mark_multiple": mark_mult,
            }
    return list(first.values())


def _native_sol(tx: dict[str, Any]) -> float:
    native = tx.get("nativeInput") if isinstance(tx.get("nativeInput"), dict) else {}
    if not native:
        events = tx.get("events") if isinstance(tx.get("events"), dict) else {}
        swap = events.get("swap") if isinstance(events.get("swap"), dict) else {}
        native = swap.get("nativeInput") if isinstance(swap.get("nativeInput"), dict) else {}
    try:
        return float(native.get("amount") or 0.0) / 1e9
    except (TypeError, ValueError, AttributeError):
        return 0.0


def extract_rpc_signatures(payload: Any) -> tuple[list[str], str]:
    """Signatures + pagination token from getTransactionsForAddress."""
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return [], ""
    data = result.get("data") or []
    sigs: list[str] = []
    for row in data if isinstance(data, list) else []:
        if isinstance(row, dict):
            sig = str(row.get("signature") or "").strip()
        else:
            sig = str(row or "").strip()
        if sig:
            sigs.append(sig)
    token = str(result.get("paginationToken") or "").strip()
    return sigs, token


async def fetch_early_swaps(pool: str, *, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """First-hour pool txs via Helius RPC, then enhanced parse. Never log the key.

    REST /v0/addresses newest-first ignores a week-old window. RPC
    getTransactionsForAddress walks oldest-first inside blockTime.
    """
    key = (settings.helius_api_key or "").strip()
    if not key or not pool:
        return []
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    sigs = await _first_hour_signatures(pool, key, start_ts, end_ts)
    if not sigs:
        return []
    return await _enhanced_txs(key, sigs)


async def _first_hour_signatures(pool: str, key: str, start_ts: int, end_ts: int) -> list[str]:
    sigs: list[str] = []
    cursor = ""
    rpc = f"https://mainnet.helius-rpc.com/?api-key={key}"
    for _ in range(8):
        opts: dict[str, Any] = {
            "transactionDetails": "signatures",
            "sortOrder": "asc",
            "limit": 100,
            "filters": {
                "blockTime": {"gte": start_ts, "lte": end_ts},
                "status": "succeeded",
            },
        }
        if cursor:
            opts["paginationToken"] = cursor
        try:
            resp = await client().post(
                rpc,
                json={"jsonrpc": "2.0", "id": "early", "method": "getTransactionsForAddress", "params": [pool, opts]},
            )
        except Exception:
            break
        if resp.status_code >= 400:
            break
        try:
            body = resp.json()
        except Exception:
            break
        if isinstance(body, dict) and body.get("error"):
            break
        chunk, cursor = extract_rpc_signatures(body)
        sigs.extend(chunk)
        if not chunk or not cursor:
            break
    return sigs


async def _enhanced_txs(key: str, sigs: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(0, len(sigs), 100):
        chunk = sigs[i : i + 100]
        try:
            resp = await client().post(
                "https://api.helius.xyz/v0/transactions",
                params={"api-key": key},
                json={"transactions": chunk},
            )
        except Exception:
            break
        if resp.status_code >= 400:
            break
        try:
            payload = resp.json()
        except Exception:
            break
        if isinstance(payload, list):
            out.extend(row for row in payload if isinstance(row, dict))
    return out


def record_early_buys(
    session: Session,
    token: Token,
    buys: list[dict[str, Any]],
    *,
    still_in: set[str] | None = None,
) -> int:
    still_in = still_in or set()
    chain = normalize_chain(token.chain or "sol")
    wrote = 0
    for row in buys:
        owner = str(row.get("owner") or "").strip()
        if not owner or owner in SKIP_OWNERS:
            continue
        wallet = (
            session.query(EarlyWallet)
            .filter(EarlyWallet.chain == chain, EarlyWallet.owner == owner)
            .one_or_none()
        )
        if wallet is None:
            wallet = EarlyWallet(chain=chain, owner=owner)
            session.add(wallet)
            session.flush()
        hit = (
            session.query(EarlyWalletHit)
            .filter(EarlyWalletHit.wallet_id == wallet.id, EarlyWalletHit.token_id == token.id)
            .one_or_none()
        )
        if hit is None:
            hit = EarlyWalletHit(wallet_id=wallet.id, token_id=token.id, mint=token.mint)
            session.add(hit)
        hit.symbol = token.symbol or ""
        hit.first_buy_ts = row.get("first_buy_ts")
        hit.age_at_buy_s = float(row.get("age_at_buy_s") or 0.0)
        hit.sol_spent = float(row.get("sol_spent") or 0.0)
        hit.token_amount = float(row.get("token_amount") or 0.0)
        hit.entry_price = float(row.get("entry_price") or 0.0)
        hit.mark_multiple = float(row.get("mark_multiple") or 0.0)
        hit.still_holding = owner in still_in
        wrote += 1
    session.flush()
    _rollup(session, chain)
    stamp = session.query(ScanState).filter(ScanState.key == STAMP_KEY).one_or_none()
    if stamp is None:
        session.add(ScanState(key=STAMP_KEY, value=str(wrote), updated_at=utcnow()))
    else:
        stamp.value = str(wrote)
        stamp.updated_at = utcnow()
    return wrote


def apply_still_holding(session: Session, token: Token, still_in: set[str]) -> int:
    hits = session.query(EarlyWalletHit).filter(EarlyWalletHit.token_id == token.id).all()
    changed = 0
    for hit in hits:
        wallet = session.query(EarlyWallet).filter(EarlyWallet.id == hit.wallet_id).one_or_none()
        holding = bool(wallet and wallet.owner in still_in)
        if hit.still_holding != holding:
            hit.still_holding = holding
            changed += 1
    session.flush()
    _rollup(session, normalize_chain(token.chain or "sol"))
    return changed


def _rollup(session: Session, chain: str) -> None:
    fomo = {
        row.owner
        for row in session.query(FomoWallet.owner).filter(FomoWallet.chain == chain).all()
    }
    wallets = session.query(EarlyWallet).filter(EarlyWallet.chain == chain).all()
    for wallet in wallets:
        hits = list(wallet.hits or [])
        wallet.n_runners = len(hits)
        wallet.n_still_in = sum(1 for h in hits if h.still_holding)
        wallet.n_profitable = sum(1 for h in hits if float(h.mark_multiple or 0.0) >= PROFIT_X)
        wallet.n_sized = sum(1 for h in hits if float(h.sol_spent or 0.0) >= MIN_SIZED_SOL)
        wallet.sum_sol_spent = sum(float(h.sol_spent or 0.0) for h in hits)
        ages = [float(h.age_at_buy_s or 0.0) for h in hits if h.age_at_buy_s]
        wallet.fastest_buy_s = min(ages) if ages else 0.0
        wallet.fomo_hits = 1 if wallet.owner in fomo else 0
        best = max(hits, key=lambda h: float(h.mark_multiple or 0.0), default=None)
        if best is not None:
            wallet.best_symbol = best.symbol or ""
            wallet.best_mint = best.mint or ""
            wallet.best_mark_multiple = float(best.mark_multiple or 0.0)
        wallet.updated_at = utcnow()
    session.flush()


def _live_early_hits(session: Session, wallet: EarlyWallet) -> list[tuple[EarlyWalletHit, Token]]:
    return (
        session.query(EarlyWalletHit, Token)
        .join(Token, Token.id == EarlyWalletHit.token_id)
        .filter(EarlyWalletHit.wallet_id == wallet.id, Token.is_historical.is_(False))
        .all()
    )


def _early_card(wallet: EarlyWallet, live_hits: list[tuple[EarlyWalletHit, Token]] | None = None) -> dict[str, Any]:
    live_rows = live_hits if live_hits is not None else []
    live_hit_ids = {hit.id for hit, _token in live_rows}
    if live_rows:
        n_early = len(live_rows)
        n_sized = sum(1 for hit, _token in live_rows if float(hit.sol_spent or 0.0) >= MIN_SIZED_SOL)
        n_prof = sum(1 for hit, _token in live_rows if float(hit.mark_multiple or 0.0) >= PROFIT_X)
        n_still = sum(1 for hit, _token in live_rows if hit.still_holding)
        sum_sol = sum(float(hit.sol_spent or 0.0) for hit, _token in live_rows)
        ages = [float(hit.age_at_buy_s or 0.0) for hit, _token in live_rows if hit.age_at_buy_s]
        best_sym = ""
        best_mint = ""
        best_mult = 0.0
        for hit, token in live_rows:
            mult = float(hit.mark_multiple or 0.0)
            if mult >= best_mult:
                best_mult = mult
                best_sym = hit.symbol or token.symbol or ""
                best_mint = hit.mint or token.mint or ""
    else:
        n_early = int(wallet.n_runners or 0)
        n_sized = int(wallet.n_sized or 0)
        n_prof = int(wallet.n_profitable or 0)
        n_still = int(wallet.n_still_in or 0)
        sum_sol = float(wallet.sum_sol_spent or 0.0)
        ages = [float(wallet.fastest_buy_s or 0.0)] if wallet.fastest_buy_s else []
        best_sym = wallet.best_symbol or ""
        best_mint = wallet.best_mint or ""
        best_mult = float(wallet.best_mark_multiple or 0.0)
    hits = []
    for hit in wallet.hits or []:
        hits.append(
            {
                "mint": hit.mint,
                "symbol": hit.symbol,
                "age_at_buy_s": hit.age_at_buy_s,
                "sol_spent": hit.sol_spent,
                "token_amount": hit.token_amount,
                "entry_price": hit.entry_price,
                "mark_multiple": hit.mark_multiple,
                "still_holding": hit.still_holding,
                "historical": bool(live_hit_ids) and hit.id not in live_hit_ids,
            }
        )
    hits.sort(key=lambda row: (bool(row.get("historical")), -float(row.get("mark_multiple") or 0.0)))
    return {
        "owner": wallet.owner,
        "chain": wallet.chain,
        "n_runners": n_early,
        "n_still_in": n_still,
        "n_profitable": n_prof,
        "n_sized": n_sized,
        "sum_sol_spent": sum_sol,
        "fastest_buy_s": min(ages) if ages else float(wallet.fastest_buy_s or 0.0),
        "fomo": bool(wallet.fomo_hits),
        "best_symbol": best_sym,
        "best_mint": best_mint,
        "best_mark_multiple": best_mult,
        "live": bool(live_rows),
        "hits": hits,
    }


def list_early_wallets(session: Session, chain: str, *, limit: int = 100) -> dict[str, Any]:
    """This-window first-hour buyers first. ZCAT stays parked analytics."""
    chain = normalize_chain(chain)
    cap = max(1, min(int(limit), 200))
    q = session.query(EarlyWallet).filter(EarlyWallet.chain == chain)
    live_ids = [
        wid
        for (wid,) in (
            session.query(EarlyWalletHit.wallet_id)
            .join(Token, Token.id == EarlyWalletHit.token_id)
            .join(EarlyWallet, EarlyWallet.id == EarlyWalletHit.wallet_id)
            .filter(EarlyWallet.chain == chain, Token.is_historical.is_(False))
            .distinct()
            .all()
        )
    ]
    live_cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    if live_ids:
        live_wallets = q.filter(EarlyWallet.id.in_(live_ids)).all()
        for wallet in live_wallets:
            live_hits = _live_early_hits(session, wallet)
            if not live_hits:
                continue
            card = _early_card(wallet, live_hits)
            live_cards.append(card)
            seen.add(wallet.owner)
        live_cards.sort(
            key=lambda c: (
                int(c.get("n_profitable") or 0),
                int(c.get("n_sized") or 0),
                int(bool(c.get("fomo"))),
                float(c.get("best_mark_multiple") or 0.0),
            ),
            reverse=True,
        )
    items = live_cards[:cap]
    if len(items) < cap:
        ranked = q.order_by(
            EarlyWallet.n_profitable.desc(),
            EarlyWallet.n_sized.desc(),
            EarlyWallet.fomo_hits.desc(),
            EarlyWallet.best_mark_multiple.desc(),
        )
        sized = ranked.filter(EarlyWallet.n_sized > 0).limit(cap + len(seen)).all()
        pad = sized or ranked.limit(cap + len(seen)).all()
        for wallet in pad:
            if wallet.owner in seen:
                continue
            items.append(_early_card(wallet, None))
            seen.add(wallet.owner)
            if len(items) >= cap:
                break
    live_sized = sum(1 for c in live_cards if int(c.get("n_sized") or 0) > 0)
    total = q.count()
    still = q.filter(EarlyWallet.n_still_in > 0).count()
    fomo_n = q.filter(EarlyWallet.fomo_hits > 0).count()
    return {
        "items": items,
        "wallets": len(items),
        "tracked": total,
        "sized": live_sized or q.filter(EarlyWallet.n_sized > 0).count(),
        "sized_live": live_sized,
        "sized_all": q.filter(EarlyWallet.n_sized > 0).count(),
        "still_in": still,
        "fomo_overlap": fomo_n,
        "min_sol": MIN_SIZED_SOL,
        "chain": chain,
        "source": "helius_early_swaps",
    }


async def holder_owners(mint: str) -> set[str]:
    """Current non-zero owners from Helius DAS. Never log the key."""
    key = (settings.helius_api_key or "").strip()
    if not key or not mint:
        return set()
    owners: set[str] = set()
    rpc = f"https://mainnet.helius-rpc.com/?api-key={key}"
    for page in range(1, 9):
        try:
            resp = await client().post(
                rpc,
                json={
                    "jsonrpc": "2.0",
                    "id": "hold",
                    "method": "getTokenAccounts",
                    "params": {
                        "mint": mint,
                        "page": page,
                        "limit": 1000,
                        "options": {"showZeroBalance": False},
                    },
                },
            )
        except Exception:
            break
        if resp.status_code >= 400:
            break
        try:
            body = resp.json()
        except Exception:
            break
        result = body.get("result") if isinstance(body, dict) else None
        accounts = (result or {}).get("token_accounts") if isinstance(result, dict) else None
        if not isinstance(accounts, list) or not accounts:
            break
        for acc in accounts:
            if not isinstance(acc, dict):
                continue
            owner = str(acc.get("owner") or "").strip()
            try:
                amt = float(acc.get("amount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if owner and amt > 0:
                owners.add(owner)
        if len(accounts) < 1000:
            break
    return owners


def _harvest_attempted_ids(session: Session) -> set[int]:
    rows = session.query(ScanState).filter(ScanState.key.like(f"{HARVEST_ATTEMPT_PREFIX}%")).all()
    out: set[int] = set()
    now = utcnow()
    for row in rows:
        try:
            tid = int(str(row.key or "").split(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            continue
        stamp = row.updated_at
        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if (now - stamp).total_seconds() < HARVEST_RETRY_S:
            out.add(tid)
    return out


def stamp_harvest_attempt(session: Session, token_id: int) -> None:
    key = f"{HARVEST_ATTEMPT_PREFIX}{int(token_id)}"[:64]
    row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
    now = utcnow()
    if row is None:
        session.add(ScanState(key=key, value="1", updated_at=now))
    else:
        row.value = str(int(row.value or 0) + 1) if str(row.value or "").isdigit() else "1"
        row.updated_at = now
    session.flush()


def harvest_candidate_tokens(session: Session, *, limit: int = HARVEST_LIMIT) -> list[Token]:
    """This-window confirmed 5×+ first, then other honest 5× Sol books.

    Newest Outcome.multiple 5× can be prepumped / empty-pool and block
    the confirmed board FOMO already mapped — gold then stays 0.
    Empty Helius harvests are stamped so the queue walks on.
    Backfill / ZCAT stay on their own seed.
    """
    from ..scoring.outcomes import MAX_HONEST_MULTIPLE, is_bundle_copycat_run, is_prepumped_entry
    from ..models import Research
    from .fomo_wallets import confirmed_top_runners

    done = {tid for (tid,) in session.query(EarlyWalletHit.token_id).distinct()}
    attempted = _harvest_attempted_ids(session)
    picked: list[Token] = []
    seen: set[int] = set()
    cap = max(1, int(limit))

    def consider(token: Token, research: Research | None, outcome: Outcome | None) -> None:
        if token.id in done or token.id in attempted or token.id in seen:
            return
        if token.is_historical or (token.source or "") in SKIP_HARVEST_SOURCES:
            return
        if not (token.pool_address or "").strip() or token.migrated_at is None:
            return
        if research is not None and outcome is not None:
            if is_bundle_copycat_run(research) or is_prepumped_entry(session, token, research, outcome):
                return
        picked.append(token)
        seen.add(token.id)

    for token, research, outcome, _mult in confirmed_top_runners(session, "sol", limit=100):
        consider(token, research, outcome)
        if len(picked) >= cap:
            return picked

    rows = (
        session.query(Token, Research, Outcome)
        .join(Research, Research.token_id == Token.id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(
            Token.chain == "sol",
            Token.is_historical.is_(False),
            Token.pool_address != "",
            Token.migrated_at.is_not(None),
            Token.source.notin_(tuple(SKIP_HARVEST_SOURCES)),
            Outcome.multiple >= PROFIT_X,
            Outcome.multiple <= MAX_HONEST_MULTIPLE,
            (Outcome.label.is_(None)) | (Outcome.label == 1),
        )
        .order_by(Token.first_seen_at.desc())
        .limit(200)
        .all()
    )
    for token, research, outcome in rows:
        consider(token, research, outcome)
        if len(picked) >= cap:
            break
    return picked


async def harvest_confirmed_early(*, limit: int = HARVEST_LIMIT) -> int:
    """First-hour buys on confirmed 5×+ Sol books already in the DB.

    Does not ingest new names. Backfill / ZCAT stay on their own seed.
    """
    from ..db import ingest_lock, session_scope
    from ..research.dexscreener import token_market

    snaps: list[dict[str, Any]] = []
    async with ingest_lock:
        with session_scope() as session:
            for token in harvest_candidate_tokens(session, limit=limit):
                snaps.append(
                    {
                        "id": token.id,
                        "mint": token.mint,
                        "pool": token.pool_address,
                        "creator": token.creator or "",
                        "launched": token.migrated_at,
                    }
                )
    wrote = 0
    for snap in snaps:
        launched = snap["launched"]
        if launched.tzinfo is None:
            launched = launched.replace(tzinfo=timezone.utc)
        try:
            txs = await fetch_early_swaps(snap["pool"], start=launched, end=launched + timedelta(hours=1))
        except Exception:
            txs = []
        mark = 0.0
        try:
            market = await token_market(snap["mint"], chain="sol")
            mark = float((market or {}).get("price_native") or 0.0)
        except Exception:
            mark = 0.0
        try:
            still = await holder_owners(snap["mint"])
        except Exception:
            still = set()
        buys = parse_early_buys(
            txs,
            mint=snap["mint"],
            pool=snap["pool"],
            creator=snap["creator"],
            launched=launched,
            mark_price=mark,
        )
        async with ingest_lock:
            with session_scope() as session:
                token = session.query(Token).filter(Token.id == snap["id"]).one_or_none()
                stamp_harvest_attempt(session, snap["id"])
                if token is None:
                    continue
                wrote += record_early_buys(session, token, buys, still_in=still)
    return wrote


async def refresh_tracked_still_holding(*, limit: int = 3) -> int:
    from ..db import ingest_lock, session_scope

    snaps: list[tuple[int, str]] = []
    async with ingest_lock:
        with session_scope() as session:
            rows = (
                session.query(EarlyWalletHit.token_id, Token.mint)
                .join(Token, Token.id == EarlyWalletHit.token_id)
                .filter(Token.is_historical.is_(False), Token.source.notin_(tuple(SKIP_HARVEST_SOURCES)))
                .distinct()
                .limit(20)
                .all()
            )
            snaps = [(int(tid), str(mint)) for tid, mint in rows[:limit]]
    changed = 0
    for token_id, mint in snaps:
        try:
            still = await holder_owners(mint)
        except Exception:
            still = set()
        async with ingest_lock:
            with session_scope() as session:
                token = session.query(Token).filter(Token.id == token_id).one_or_none()
                if token is None:
                    continue
                changed += apply_still_holding(session, token, still)
    return changed
