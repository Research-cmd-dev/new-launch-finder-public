"""Wallet clustering across launches — same actors behind different tickers.

Uses EarlyWalletHit + FomoWalletHit (already stored). Learn/shadow only.
Serial deployer is a PRIOR, never a hard block (SI Super Intelligence).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from ..models import EarlyWallet, EarlyWalletHit, FomoWallet, FomoWalletHit, Research, Token


def _creator_priors(research: Research | None) -> dict[str, Any]:
    if research is None:
        return {
            "creator_prior_launches": 0,
            "creator_prior_wins": 0,
            "creator_prior_rugs": 0,
            "serial": False,
        }
    launches = int(research.creator_prior_launches or 0)
    wins = int(research.creator_prior_wins or 0)
    rugs = int(research.creator_prior_rugs or 0)
    return {
        "creator_prior_launches": launches,
        "creator_prior_wins": wins,
        "creator_prior_rugs": rugs,
        "serial": launches >= 5 or rugs >= 3,
    }


def early_buyer_overlap(
    session: Session,
    token: Token,
    *,
    min_shared: int = 2,
    limit_peers: int = 12,
) -> dict[str, Any]:
    """Wallets that bought this mint early and also hit other mints."""
    hits = (
        session.query(EarlyWalletHit, EarlyWallet)
        .join(EarlyWallet, EarlyWallet.id == EarlyWalletHit.wallet_id)
        .filter(EarlyWalletHit.token_id == token.id)
        .limit(200)
        .all()
    )
    if not hits:
        return {
            "n_early_buyers": 0,
            "multi_mint_wallets": 0,
            "peer_mints": [],
            "shared_owners": [],
            "paper_only": True,
            "side_key": "wallet_clusters",
        }

    wallet_ids = [w.id for _, w in hits]
    other = (
        session.query(EarlyWalletHit, EarlyWallet, Token)
        .join(EarlyWallet, EarlyWallet.id == EarlyWalletHit.wallet_id)
        .join(Token, Token.id == EarlyWalletHit.token_id)
        .filter(
            EarlyWalletHit.wallet_id.in_(wallet_ids),
            EarlyWalletHit.token_id != token.id,
        )
        .limit(500)
        .all()
    )
    by_mint: dict[str, set[str]] = defaultdict(set)
    owner_mints: dict[str, set[str]] = defaultdict(set)
    for hit, wallet, peer in other:
        by_mint[peer.mint].add(wallet.owner)
        owner_mints[wallet.owner].add(peer.mint)

    multi = [o for o, mints in owner_mints.items() if len(mints) >= 1]
    peers = sorted(
        (
            {
                "mint": mint,
                "symbol": "",
                "shared_n": len(owners),
                "owners_sample": sorted(owners)[:6],
            }
            for mint, owners in by_mint.items()
            if len(owners) >= min_shared
        ),
        key=lambda r: -int(r["shared_n"]),
    )[:limit_peers]
    # Fill symbols
    if peers:
        mints = [p["mint"] for p in peers]
        toks = session.query(Token).filter(Token.mint.in_(mints)).all()
        sym = {t.mint: t.symbol or "" for t in toks}
        for p in peers:
            p["symbol"] = sym.get(p["mint"], "")

    return {
        "n_early_buyers": len(hits),
        "multi_mint_wallets": len(multi),
        "peer_mints": peers,
        "shared_owners": sorted(multi)[:20],
        "paper_only": True,
        "side_key": "wallet_clusters",
    }


def fomo_overlap(session: Session, token: Token, *, limit: int = 10) -> dict[str, Any]:
    """Repeat FOMO wallets also on this mint (if hit rows exist)."""
    rows = (
        session.query(FomoWalletHit, FomoWallet)
        .join(FomoWallet, FomoWallet.id == FomoWalletHit.wallet_id)
        .filter(FomoWalletHit.token_id == token.id)
        .order_by(FomoWallet.n_wins.desc())
        .limit(limit)
        .all()
    )
    return {
        "n_fomo_hits": len(rows),
        "wallets": [
            {
                "owner": w.owner,
                "n_tokens": int(w.n_tokens or 0),
                "n_wins": int(w.n_wins or 0),
                "n_rugs": int(w.n_rugs or 0),
                "best_multiple": float(w.best_multiple or 0.0),
            }
            for _, w in rows
        ],
    }


def wallet_clusters(
    session: Session,
    token: Token,
    research: Research | None = None,
) -> dict[str, Any]:
    """Cluster + creator serial prior — never a hard block."""
    if research is None:
        research = (
            session.query(Research).filter(Research.token_id == token.id).one_or_none()
        )
    priors = _creator_priors(research)
    early = early_buyer_overlap(session, token)
    fomo = fomo_overlap(session, token)
    # Pattern-break residual is scored upstream (early_book + top10); here we only flag prior.
    return {
        "creator": priors,
        "serial_prior": bool(priors["serial"]),
        "serial_hard_block": False,  # explicit policy
        "early_overlap": early,
        "fomo_overlap": fomo,
        "cluster_heat": min(
            1.0,
            0.15 * float(early.get("multi_mint_wallets") or 0)
            + 0.1 * float(len(early.get("peer_mints") or [])),
        ),
        "paper_only": True,
        "side_key": "wallet_clusters",
        "note": "Serial deployer = prior only. Never veto from cluster heat alone.",
    }
