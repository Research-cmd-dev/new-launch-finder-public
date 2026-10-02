"""Unified wallet score from stored Early + FOMO + alpha maps.

No extra GMGN HTTP. Heuristic only — do not add to FEATURE_NAMES.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import EarlyWallet, EarlyWalletHit, FomoWallet, Outcome, ScanState, Token
from .early_wallets import MIN_SIZED_SOL, PROFIT_X
from .fomo_wallets import VENUE_TOKEN_CUT, _norm_owner, live_fomo_stats

# RH has no Helius first-hour tape. A stored FOMO-map share at this
# size is the sized analog so gold can form without extra HTTP.
# Live 13:25: 0.4% minted gold on 162/199 RH wallets. Dust on one
# book is not first-hour size. Need 1% and a repeat.
RH_SIZED_PCT = 1.0
RH_GOLD_SIZED = 2


def compute_wallet_score(
    *,
    n_sized: int = 0,
    n_early: int = 0,
    n_early_profitable: int = 0,
    n_still_in: int = 0,
    n_fomo_wins: int = 0,
    n_rugs: int = 0,
    alpha_runs: int = 0,
    n_created: int = 0,
    created_wins: int = 0,
) -> tuple[float, str]:
    """0–100 research score. Gold = sized first-hour ∩ FOMO win."""
    score = 0.0
    score += min(35.0, float(n_sized) * 10.0 + float(n_early_profitable) * 5.0)
    # Live 15:10: 3 this-window UZBK wins scored 21 / grade —. That is
    # the best Sol map we have until sized ∩ FOMO mints gold.
    score += min(30.0, float(n_fomo_wins) * 10.0)
    score -= min(20.0, float(n_rugs) * 5.0)
    if n_sized and n_fomo_wins:
        score += 20.0
    elif n_early and n_fomo_wins:
        score += 15.0
    if n_fomo_wins >= 3:
        score += 8.0
    if alpha_runs >= 2:
        score += 15.0
    score += min(10.0, float(n_still_in) * 5.0)
    if n_created >= 3:
        rate = float(created_wins) / float(n_created) if n_created else 0.0
        if rate < 0.15:
            score -= 15.0
    score = max(0.0, min(100.0, score))
    if score >= 70:
        grade = "A"
    elif score >= 50:
        grade = "B"
    elif score >= 30:
        grade = "C"
    else:
        grade = "—"
    return score, grade


def _alpha_runs(session: Session, owner: str) -> int:
    row = session.query(ScanState).filter(ScanState.key == f"alpha:{owner}").one_or_none()
    if row is None:
        return 0
    try:
        return int(json.loads(row.value or "{}").get("runs") or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def _creator_stats(session: Session, owner: str, chain: str) -> tuple[int, int]:
    rows = (
        session.query(Token, Outcome)
        .outerjoin(Outcome, Outcome.token_id == Token.id)
        .filter(Token.chain == chain, Token.creator == owner)
        .all()
    )
    n = len(rows)
    wins = sum(1 for _token, outcome in rows if outcome is not None and outcome.label == 1)
    return n, wins


def _live_early_stats(
    session: Session, early: EarlyWallet | None
) -> tuple[int, int, int, int, str, float]:
    """Sized / early counts that ignore parked historical books (ZCAT)."""
    if early is None:
        return 0, 0, 0, 0, "", 0.0
    hits = (
        session.query(EarlyWalletHit, Token)
        .join(Token, Token.id == EarlyWalletHit.token_id)
        .filter(EarlyWalletHit.wallet_id == early.id, Token.is_historical.is_(False))
        .all()
    )
    hist = (
        session.query(EarlyWalletHit.id)
        .join(Token, Token.id == EarlyWalletHit.token_id)
        .filter(EarlyWalletHit.wallet_id == early.id, Token.is_historical.is_(True))
        .count()
    )
    if not hits and hist == 0:
        return (
            int(early.n_sized or 0),
            int(early.n_runners or 0),
            int(early.n_profitable or 0),
            int(early.n_still_in or 0),
            early.best_symbol or "",
            float(early.best_mark_multiple or 0.0),
        )
    n_early = len(hits)
    n_sized = sum(1 for hit, _token in hits if float(hit.sol_spent or 0.0) >= MIN_SIZED_SOL)
    n_prof = sum(1 for hit, _token in hits if float(hit.mark_multiple or 0.0) >= PROFIT_X)
    n_still = sum(1 for hit, _token in hits if hit.still_holding)
    best_sym = ""
    best_mult = 0.0
    for hit, token in hits:
        mult = float(hit.mark_multiple or 0.0)
        if mult >= best_mult:
            best_mult = mult
            best_sym = hit.symbol or token.symbol or ""
    return n_sized, n_early, n_prof, n_still, best_sym, best_mult


def _rh_sized_fomo(fomo: FomoWallet | None) -> int:
    if fomo is None:
        return 0
    return sum(
        1
        for hit in (fomo.hits or [])
        if hit.is_win and float(hit.pct or 0.0) >= RH_SIZED_PCT
    )


def score_wallet(session: Session, owner: str, chain: str = "sol", *, include_hits: bool = True) -> dict[str, Any]:
    chain = normalize_chain(chain)
    owner = _norm_owner(owner, chain)
    early = (
        session.query(EarlyWallet)
        .filter(EarlyWallet.chain == chain, EarlyWallet.owner == owner)
        .one_or_none()
    )
    fomo = (
        session.query(FomoWallet)
        .filter(FomoWallet.chain == chain, FomoWallet.owner == owner)
        .one_or_none()
    )
    alpha_runs = _alpha_runs(session, owner)
    n_created, created_wins = _creator_stats(session, owner, chain)
    n_sized, n_early, n_prof, n_still, live_best_sym, live_best_mult = _live_early_stats(
        session, early
    )
    live_n_fomo, live_n_wins, live_n_rugs, live_fomo_sym, live_fomo_mult = live_fomo_stats(
        session, fomo
    )
    n_fomo = live_n_fomo
    n_wins = live_n_wins
    n_rugs = live_n_rugs
    n_rh_sized = _rh_sized_fomo(fomo) if chain == "robinhood" else 0
    stored_n_sized = int(early.n_sized or 0) if early else 0
    stored_n_early = int(early.n_runners or 0) if early else 0
    stored_n_wins = int(fomo.n_wins or 0) if fomo else 0
    stored_n_fomo = int(fomo.n_tokens or 0) if fomo else 0
    stored_n_rugs = int(fomo.n_rugs or 0) if fomo else 0
    live = bool((n_sized or n_rh_sized) or n_wins or n_early or n_fomo)
    score_sized = n_sized or n_rh_sized
    score, grade = compute_wallet_score(
        n_sized=score_sized,
        n_early=n_early or n_rh_sized,
        n_early_profitable=n_prof,
        n_still_in=n_still,
        n_fomo_wins=n_wins,
        n_rugs=n_rugs,
        alpha_runs=alpha_runs,
        n_created=n_created,
        created_wins=created_wins,
    )
    hits: list[dict[str, Any]] = []
    if include_hits:
        if early:
            for h in early.hits or []:
                hits.append(
                    {
                        "kind": "early",
                        "mint": h.mint,
                        "symbol": h.symbol,
                        "multiple": h.mark_multiple,
                        "sol_spent": h.sol_spent,
                        "age_at_buy_s": h.age_at_buy_s,
                        "still_holding": h.still_holding,
                        "win": float(h.mark_multiple or 0.0) >= 5.0,
                    }
                )
        if fomo:
            for h in fomo.hits or []:
                hits.append(
                    {
                        "kind": "fomo",
                        "mint": h.mint,
                        "symbol": h.symbol,
                        "multiple": h.multiple,
                        "pct": h.pct,
                        "win": bool(h.is_win),
                    }
                )
        hits.sort(key=lambda r: float(r.get("multiple") or 0.0), reverse=True)
    return {
        "owner": owner,
        "chain": chain,
        "score": round(score, 1),
        "grade": grade,
        "n_early": (n_early or n_rh_sized) if live else stored_n_early,
        "n_sized": (n_sized or n_rh_sized) if live else stored_n_sized,
        "n_profitable": n_prof,
        "n_still_in": n_still,
        "n_fomo": n_fomo if live else stored_n_fomo,
        "n_wins": n_wins if live else stored_n_wins,
        "n_rugs": n_rugs if live else stored_n_rugs,
        "alpha_runs": alpha_runs,
        "n_created": n_created,
        "created_wins": created_wins,
        "gold": bool(
            (n_sized and n_wins)
            if chain != "robinhood"
            else (n_rh_sized >= RH_GOLD_SIZED and n_wins)
        ),
        "live": live,
        "parked": (not live) and bool(stored_n_sized or stored_n_wins),
        "stored_n_sized": stored_n_sized,
        "stored_n_early": stored_n_early,
        "stored_n_wins": stored_n_wins,
        "stored_n_fomo": stored_n_fomo,
        "stored_n_rugs": stored_n_rugs,
        "best_symbol": live_best_sym
        or live_fomo_sym
        or (early.best_symbol if early and early.best_symbol else "")
        or (fomo.best_symbol if fomo else ""),
        "best_multiple": max(
            live_best_mult,
            live_fomo_mult,
            float(early.best_mark_multiple or 0.0) if early and not live_best_sym else 0.0,
            float(fomo.best_multiple or 0.0) if fomo and not live_fomo_sym else 0.0,
        ),
        "sum_sol_spent": float(early.sum_sol_spent or 0.0) if early else 0.0,
        "hits": hits,
        "n_hits": len(hits),
        "source": "stored_maps_and_early_swaps",
    }


def list_scored_wallets(session: Session, chain: str, *, limit: int = 100) -> dict[str, Any]:
    chain = normalize_chain(chain)
    cap = max(1, min(int(limit), 200))
    # Do not score 400 parked maps — the desk aborts at 8s and looks empty.
    pool = max(cap, min(80, cap * 2))
    owners: set[str] = set()
    for row in (
        session.query(FomoWallet.owner)
        .filter(FomoWallet.chain == chain, FomoWallet.n_tokens < VENUE_TOKEN_CUT)
        .order_by(FomoWallet.n_wins.desc(), FomoWallet.n_tokens.desc())
        .limit(pool)
        .all()
    ):
        owners.add(row.owner)
    live_early_ids = [
        wid
        for (wid,) in (
            session.query(EarlyWalletHit.wallet_id)
            .join(Token, Token.id == EarlyWalletHit.token_id)
            .join(EarlyWallet, EarlyWallet.id == EarlyWalletHit.wallet_id)
            .filter(EarlyWallet.chain == chain, Token.is_historical.is_(False))
            .distinct()
            .limit(pool)
            .all()
        )
    ]
    if live_early_ids:
        for row in session.query(EarlyWallet.owner).filter(EarlyWallet.id.in_(live_early_ids)).all():
            owners.add(row.owner)
    for row in (
        session.query(EarlyWallet.owner)
        .filter(EarlyWallet.chain == chain)
        .order_by(EarlyWallet.n_sized.desc(), EarlyWallet.n_profitable.desc())
        .limit(pool)
        .all()
    ):
        owners.add(row.owner)
    cards = [score_wallet(session, owner, chain, include_hits=False) for owner in owners]
    cards = [
        c
        for c in cards
        if max(int(c.get("n_fomo") or 0), int(c.get("stored_n_fomo") or 0)) < VENUE_TOKEN_CUT
    ]

    def _list_card(card: dict[str, Any]) -> dict[str, Any]:
        out = dict(card)
        if out.get("live"):
            return out
        # Parked ZCAT-era maps do not mint gold. Still list the stored
        # Early / FOMO book so the desk is not a blank tab.
        out["n_sized"] = int(out.get("stored_n_sized") or 0)
        out["n_early"] = int(out.get("stored_n_early") or out.get("n_early") or 0)
        out["n_wins"] = int(out.get("stored_n_wins") or 0)
        out["n_fomo"] = int(out.get("stored_n_fomo") or 0)
        out["n_rugs"] = int(out.get("stored_n_rugs") or out.get("n_rugs") or 0)
        out["parked"] = True
        return out

    cards = [
        _list_card(c)
        for c in cards
        if c.get("live")
        or float(c.get("score") or 0.0) > 0
        or int(c.get("stored_n_sized") or 0) > 0
        or int(c.get("stored_n_wins") or 0) > 0
    ]
    cards.sort(
        key=lambda c: (
            bool(c.get("live")),
            float(c.get("score") or 0.0),
            int(c.get("n_wins") or 0),
            int(c.get("n_sized") or 0),
        ),
        reverse=True,
    )
    items = cards[: max(1, min(int(limit), 200))]
    return {
        "chain": chain,
        "wallets": len(items),
        "scored": len(cards),
        "gold": sum(1 for c in cards if c["gold"]),
        "parked": sum(1 for c in cards if c.get("parked")),
        "items": items,
        "source": "stored_maps_and_early_swaps",
    }
