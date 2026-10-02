"""FOMO-wallet database from stored holder maps of the top 100 runners.

Uses research.raw_json holders.top_wallets already written by Helius /
Blockscout. Does not call GMGN. Creator and pool labels are dropped.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import EarlyWallet, FomoWallet, FomoWalletHit, Outcome, Research, ScanState, Token, utcnow

TOP_N = 100
SKIP_LABELS = frozenset(
    {
        "creator",
        "pool",
        "dev",
        "burn",
        "dex",
        "contract",
        "router",
        "aggregator",
        "cex",
        "bridge",
        "vault",
    }
)
# Live Sol FOMO #1 had 797 books — a venue/router on every map, not a person.
VENUE_TOKEN_CUT = 80
SKIP_OWNERS = frozenset(
    {
        "0x000000000000000000000000000000000000dead",
        "0x0000000000000000000000000000000000000000",
        "1nc1nerator11111111111111111111111111111111",
    }
)
REBUILD_EVERY_S = 300


def live_fomo_stats(session: Session, fomo: FomoWallet | None) -> tuple[int, int, int, str, float]:
    """This-window honest FOMO counts. Parked / >80× books do not rank."""
    from ..scoring.outcomes import MAX_HONEST_MULTIPLE

    if fomo is None:
        return 0, 0, 0, "", 0.0
    rows = (
        session.query(FomoWalletHit, Token)
        .join(Token, Token.id == FomoWalletHit.token_id)
        .filter(FomoWalletHit.wallet_id == fomo.id)
        .all()
    )
    live = [
        (hit, token)
        for hit, token in rows
        if (not token.is_historical) and float(hit.multiple or 0.0) <= MAX_HONEST_MULTIPLE
    ]
    hist = sum(1 for _hit, token in rows if token.is_historical)
    if not live and hist == 0:
        return (
            int(fomo.n_tokens or 0),
            int(fomo.n_wins or 0),
            int(fomo.n_rugs or 0),
            fomo.best_symbol or "",
            float(fomo.best_multiple or 0.0),
        )
    n_fomo = len(live)
    n_wins = sum(1 for hit, _token in live if hit.is_win)
    n_rugs = n_fomo - n_wins
    best_sym = ""
    best_mult = 0.0
    for hit, token in live:
        if not hit.is_win:
            continue
        mult = float(hit.multiple or 0.0)
        if mult >= best_mult:
            best_mult = mult
            best_sym = hit.symbol or token.symbol or ""
    return n_fomo, n_wins, n_rugs, best_sym, best_mult


def _norm_owner(owner: str, chain: str) -> str:
    raw = (owner or "").strip()
    if not raw:
        return ""
    if normalize_chain(chain) == "robinhood":
        return raw.lower()
    return raw


def _stored_top_wallets(research: Research | None) -> list[dict[str, Any]]:
    if research is None:
        return []
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, dict):
        return []
    holders = raw.get("holders") if isinstance(raw.get("holders"), dict) else {}
    wallets = holders.get("top_wallets") if isinstance(holders, dict) else None
    if not isinstance(wallets, list):
        return []
    out: list[dict[str, Any]] = []
    for row in wallets:
        if not isinstance(row, dict):
            continue
        owner = str(row.get("owner") or "").strip()
        if not owner:
            continue
        label = str(row.get("label") or "").strip().lower()
        try:
            pct = float(row.get("pct") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        out.append({"owner": owner, "pct": pct, "label": label})
    return out


def confirmed_top_runners(session: Session, chain: str, *, limit: int = TOP_N) -> list[tuple[Token, Research, Outcome, float]]:
    """Same confirmed 5×+ board /api/runners uses, capped at top 100."""
    from ..scoring.outcomes import (
        DEAD_POOL_LIQ,
        MAX_HONEST_MULTIPLE,
        confirmed_runner_multiple,
        is_bundle_copycat_run,
        is_prepumped_entry,
    )
    from ..chains import graduation_mcap

    chain = normalize_chain(chain)
    filters = [
        Outcome.multiple >= 5.0,
        Outcome.multiple <= MAX_HONEST_MULTIPLE,
        Token.source != "backfill",
        Token.chain == chain,
        (Outcome.label.is_(None)) | (Outcome.label == 1),
        Outcome.last_liq >= DEAD_POOL_LIQ,
        ~Research.risk_flags_json.like("%start-high rug%"),
        ~Research.risk_flags_json.like("%Pre-pumped%"),
    ]
    if chain == "sol":
        filters.append(Outcome.t0_mcap >= 0.4 * graduation_mcap("sol"))
    filters.append(Token.is_historical.is_(False))
    rows = (
        session.query(Token, Research, Outcome)
        .join(Research, Research.token_id == Token.id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(*filters)
        .order_by(Outcome.multiple.desc())
        .limit(800)
        .all()
    )
    ranked: list[tuple[Token, Research, Outcome, float]] = []
    for token, research, outcome in rows:
        if is_prepumped_entry(session, token, research, outcome):
            continue
        if is_bundle_copycat_run(research):
            continue
        confirmed = confirmed_runner_multiple(session, token, outcome)
        if confirmed < 5.0:
            continue
        ranked.append((token, research, outcome, confirmed))
    ranked.sort(key=lambda r: r[3], reverse=True)
    return ranked[:limit]


def rebuild_fomo_wallets(session: Session, chain: str, *, limit: int = TOP_N) -> dict[str, Any]:
    """Upsert confirmed 5×+ maps, then accumulate other labeled books.

    Does not wipe history. No extra GMGN HTTP.
    """
    chain = normalize_chain(chain)
    runners = confirmed_top_runners(session, chain, limit=limit)
    out = rebuild_from_runners(session, chain, runners)
    extra = accumulate_labeled_maps(session, chain, limit=80)
    out["accumulated"] = extra
    return out


def rebuild_from_runners(
    session: Session,
    chain: str,
    runners: list[tuple[Token, Research, Outcome, float]],
) -> dict[str, Any]:
    """Upsert stored holder maps. Same book twice does not duplicate hits."""
    chain = normalize_chain(chain)
    mapped = _upsert_books(session, chain, runners)
    now = utcnow()
    n = session.query(FomoWallet).filter(FomoWallet.chain == chain).count()
    _touch_rebuild(session, chain, now, mapped=mapped, top_n=len(runners))
    return {
        "chain": chain,
        "top_n": len(runners),
        "mapped": mapped,
        "wallets": n,
        "updated_at": now.isoformat(),
        "source": "stored_holder_maps",
    }


def _upsert_books(
    session: Session,
    chain: str,
    books: list[tuple[Token, Research, Outcome, float]],
) -> int:
    mapped = 0
    now = utcnow()
    touched: set[int] = set()
    for token, research, outcome, multiple in books:
        wallets = _stored_top_wallets(research)
        if not wallets:
            continue
        mapped += 1
        label = getattr(outcome, "label", None)
        if label is None:
            is_win = float(multiple or 0.0) >= 5.0
        else:
            is_win = label == 1
        seen: set[str] = set()
        for row in wallets:
            if row["label"] in SKIP_LABELS:
                continue
            owner = _norm_owner(row["owner"], chain)
            if not owner or owner in seen or owner.lower() in SKIP_OWNERS:
                continue
            seen.add(owner)
            wallet = (
                session.query(FomoWallet)
                .filter(FomoWallet.chain == chain, FomoWallet.owner == owner)
                .one_or_none()
            )
            if wallet is None:
                wallet = FomoWallet(chain=chain, owner=owner)
                session.add(wallet)
                session.flush()
            hit = (
                session.query(FomoWalletHit)
                .filter(FomoWalletHit.wallet_id == wallet.id, FomoWalletHit.token_id == token.id)
                .one_or_none()
            )
            if hit is None:
                hit = FomoWalletHit(wallet_id=wallet.id, token_id=token.id, mint=token.mint)
                session.add(hit)
            hit.symbol = token.symbol or ""
            hit.multiple = float(multiple or 0.0)
            hit.pct = float(row["pct"] or 0.0)
            hit.is_win = is_win
            touched.add(wallet.id)
            if float(multiple or 0.0) >= float(wallet.best_multiple or 0.0):
                wallet.best_multiple = float(multiple or 0.0)
                wallet.best_symbol = token.symbol or ""
                wallet.best_mint = token.mint
                wallet.last_pct = float(row["pct"] or 0.0)
            wallet.updated_at = now
    session.flush()
    if touched:
        wallets = session.query(FomoWallet).filter(FomoWallet.id.in_(touched)).all()
        for wallet in wallets:
            _rollup_fomo(session, wallet)
    session.flush()
    return mapped


def _rollup_fomo(session: Session, wallet: FomoWallet) -> None:
    hits = session.query(FomoWalletHit).filter(FomoWalletHit.wallet_id == wallet.id).all()
    wallet.n_tokens = len(hits)
    wallet.n_wins = sum(1 for h in hits if h.is_win)
    wallet.n_rugs = wallet.n_tokens - wallet.n_wins
    wallet.sum_multiple = sum(float(h.multiple or 0.0) for h in hits)


def accumulate_labeled_maps(session: Session, chain: str, *, limit: int = 80) -> int:
    """First-seen labeled wins/rugs that do not yet have FOMO hits."""
    from ..scoring.outcomes import MAX_HONEST_MULTIPLE

    chain = normalize_chain(chain)
    done = {tid for (tid,) in session.query(FomoWalletHit.token_id).distinct()}
    win_q = (
        session.query(Token, Research, Outcome)
        .join(Research, Research.token_id == Token.id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(
            Token.chain == chain,
            Token.source != "backfill",
            Token.is_historical.is_(False),
            Outcome.label == 1,
            Outcome.multiple >= 5.0,
            Outcome.multiple <= MAX_HONEST_MULTIPLE,
        )
        .order_by(Token.first_seen_at.desc())
    )
    rug_q = (
        session.query(Token, Research, Outcome)
        .join(Research, Research.token_id == Token.id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(
            Token.chain == chain,
            Token.source != "backfill",
            Token.is_historical.is_(False),
            Outcome.label == 0,
        )
        .order_by(Token.first_seen_at.desc())
    )
    if done:
        win_q = win_q.filter(~Token.id.in_(done))
        rug_q = rug_q.filter(~Token.id.in_(done))
    win_rows = win_q.limit(max(1, limit // 2)).all()
    rug_rows = rug_q.limit(max(1, limit // 2)).all()
    books: list[tuple[Token, Research, Outcome, float]] = []
    for token, research, outcome in list(win_rows) + list(rug_rows):
        if token.id in done:
            continue
        books.append((token, research, outcome, float(outcome.multiple or 0.0)))
    return _upsert_books(session, chain, books)


def _touch_rebuild(
    session: Session,
    chain: str,
    when: datetime,
    *,
    mapped: int = 0,
    top_n: int = TOP_N,
) -> None:
    key = f"fomo_wallets_v3_{chain}"
    value = json.dumps({"mapped": int(mapped), "top_n": int(top_n), "at": when.isoformat()})
    row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
    if row is None:
        row = ScanState(key=key, value=value, updated_at=when)
        session.add(row)
    else:
        row.value = value
        row.updated_at = when
    session.flush()


def _stale(session: Session, chain: str) -> bool:
    key = f"fomo_wallets_v3_{chain}"
    row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
    if row is None or not row.updated_at:
        return True
    stamp = row.updated_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (utcnow() - stamp).total_seconds() >= REBUILD_EVERY_S


def list_fomo_wallets(
    session: Session,
    chain: str,
    *,
    limit: int = 100,
    rebuild: bool = True,
) -> dict[str, Any]:
    chain = normalize_chain(chain)
    n = session.query(FomoWallet).filter(FomoWallet.chain == chain).count()
    if rebuild and n == 0:
        runners = confirmed_top_runners(session, chain)
        rebuild_from_runners(session, chain, runners)
    wallets = (
        session.query(FomoWallet)
        .filter(FomoWallet.chain == chain, FomoWallet.n_tokens < VENUE_TOKEN_CUT)
        .order_by(FomoWallet.n_wins.desc(), FomoWallet.n_tokens.desc(), FomoWallet.best_multiple.desc())
        .limit(400)
        .all()
    )
    stamp = session.query(ScanState).filter(ScanState.key == f"fomo_wallets_v3_{chain}").one_or_none()
    meta: dict[str, Any] = {}
    if stamp and stamp.value:
        try:
            parsed = json.loads(stamp.value)
            if isinstance(parsed, dict):
                meta = parsed
        except json.JSONDecodeError:
            meta = {}
    early_owners = {
        row.owner
        for row in session.query(EarlyWallet.owner).filter(EarlyWallet.chain == chain).all()
    }
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for w in wallets:
        live_n, live_wins, live_rugs, live_best, live_mult = live_fomo_stats(session, w)
        hits = sorted(w.hits, key=lambda h: float(h.multiple or 0.0), reverse=True)
        scored.append(
            (
                live_wins,
                int(w.n_wins or 0),
                {
                    "owner": w.owner,
                    "early": w.owner in early_owners,
                    "n_tokens": w.n_tokens,
                    "n_wins": w.n_wins,
                    "n_rugs": w.n_rugs,
                    "n_live_wins": live_wins,
                    "n_live": live_n,
                    "best_multiple": round(float(live_mult or w.best_multiple or 0.0), 2),
                    "sum_multiple": round(float(w.sum_multiple or 0.0), 2),
                    "best_symbol": live_best or w.best_symbol,
                    "best_mint": w.best_mint,
                    "last_pct": round(float(w.last_pct or 0.0), 2),
                    "hits": [
                        {
                            "symbol": h.symbol,
                            "mint": h.mint,
                            "multiple": round(float(h.multiple or 0.0), 2),
                            "pct": round(float(h.pct or 0.0), 2),
                            "win": bool(h.is_win),
                        }
                        for h in hits
                    ],
                },
            )
        )
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    items = [row[2] for row in scored[: max(1, min(int(limit), 200))]]
    try:
        mapped = int(meta.get("mapped") or 0)
    except (TypeError, ValueError):
        mapped = 0
    try:
        top_n = int(meta.get("top_n") or TOP_N)
    except (TypeError, ValueError):
        top_n = TOP_N
    return {
        "chain": chain,
        "top_n": top_n,
        "mapped": mapped,
        "wallets": len(items),
        "updated_at": stamp.updated_at.isoformat() if stamp and stamp.updated_at else None,
        "source": "stored_holder_maps",
        "window": "this",
        "items": items,
    }
