"""First-sight model: what the desk knew at the moment it first scored a name.

Fitted on every resolved entry decision — seed and live — because none of
its inputs come from ``features_json``. Each one is a column that is
frozen at first sight: the decision row (mcap, liq, vol, holders, at), the
t0 snapshot (5-minute buys / sells / volume), the token (creation and
migration times, socials, description) and the Sol research row (holder
concentration, creator history, X account), which is written once.

Why it exists: the honest board (v75 judge) shows the old Entry score
separating dead from viable and then nothing — 43-52% 2x in every bin from
0.2 to 0.9+, the 90 line at 35%. Refit on the same 21k rows with a 7-day
out-of-time split, these inputs rank.

Label (``LABEL_VERSION`` 3): 2x within 24h **from the first fillable
print to a print we saw** — the sight print when it sat on a sellable
pool, else the first sellable print after it, the way the paper desk buys
(v79); and the peak is a post-decision snapshot, tape bar or last Dex look
on a sellable pool, never ``Outcome.max_mcap`` (v80). The v75-v78 label
re-based curve entries to the migration t0 and so paid a curve token for
graduating (2,373 Sol curve "wins", 432 still 2x from the pool open). The
v79 label still let the outcome's lifetime high stand in for a peak, and
on Sol that credited year-old tokens' ATHs to flat tapes: rows older than
a week at sight read 42% 2x, 1.9% from their prints; 2,888 Sol positives
became 1,050, and the whole >=0.30 bucket of the v79 fit was those rows —
zero live decisions in it, live Sol names never reached the line.

On label 3 the Sol fit is honest and modest: AUC 0.78, top decile 13% 2x /
5% 5x against a 6% base, calibrated p plateauing near 0.15. v86 buys that
plateau on sight (Sol 0.10 watch / 0.14 buy) and can dial the lines back.
Robinhood keeps ranking: AUC 0.91; the live artifact's >=0.30 bucket is
~24/day at 42% 2x, so v86 RH lines are 0.25 / 0.30.

The fit reuses the batch machinery (Newton ridge logistic, isotonic on the
validation tail, promote only if better). Robinhood is fitted on the
frozen-only columns (see ``RH_MUTABLE_FEATURES``).
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import Decision, ModelArtifact, Outcome, Research, Snapshot, Token, utcnow
from .batch_fit import (
    MIN_POSITIVES,
    MIN_VALID,
    _sigmoid,
    apply_calibration,
    fit_logistic,
    isotonic_fit,
    metrics,
    should_promote,
)

log = logging.getLogger("launchfinder.first_sight")

KIND = "first_sight"
# Offline label: did a sellable print reach 1.5×? Never promoted. The 2×
# label pays a wick; the paper loss is the grave that never ran.
RAN_KIND = "ran_shadow"
RAN_MULTIPLE = 1.5
# What the 2x label is measured from. 1: the sight print (curve entries
# re-based to the migration t0 — graduation alone read as a 2x). 2: the
# first fillable print, as the paper desk buys (ledger.decision_result).
# 3: same base, but the peak is a print we saw — Outcome.max_mcap (a
# lifetime high, no timestamp) never vouches. An incumbent fitted on
# another label is not "the same model, refit".
LABEL_VERSION = 3
# On a label change the incumbent is re-judged on the new label from the
# same validation rows, so a candidate that merely ties it (RH: Brier
# 0.0309 vs 0.0308 on the v79 label) is the same ranking with an honest
# card and no hourly refit churn: promote when within these of the
# incumbent. Ranking slack is should_promote's; this is the Brier one.
LABEL_CHANGE_BRIER_SLACK = 0.02
# Chains whose Entry is the first-sight probability once an artifact is promoted.
# Robinhood joined at stack-v78 on frozen-only columns (no holder /
# concentration inputs — its research row is rewritten by the 2-minute
# Blockscout refresh, so those seed columns are not first sight): the
# shadow fit read AUC 0.92 against 0.53 for the legacy blend, 25% 2x in
# the top decile against 6%, 33% at >=0.30 against 15% for the legacy 90.
FIRST_SIGHT_CHAINS: tuple[str, ...] = ("sol", "robinhood")
# Chains the worker fits (a chain can be fitted in shadow before it scores).
FIRST_SIGHT_FIT_CHAINS: tuple[str, ...] = ("sol", "robinhood")
MIN_ROWS = 2_000
VALID_DAYS = 7.0
L2 = 1.0
ARTIFACT_CACHE_SECONDS = 300.0
# A fresh incumbent is not refit every hour: the isotonic map is fitted on
# the validation tail, so a same-window refit always "wins" on Brier.
REFIT_HOURS = 6.0
# Timing sentinel when a chain timestamp is missing (flagged separately).
MISSING_MINUTES = 60.0
# Fit only on books we scored while they were still a launch, not a
# late-discovery leftover (age>1d was 20% 2x vs <10m 1.6% — selection).
TRAIN_MAX_AGE_SIGHT_MIN = 60.0
AWAITING_FILL_KEY = "awaiting_fill"
FILL_FINALIZED_KEY = "fill_finalized"

FEATURES: tuple[str, ...] = (
    "log_mcap",
    "log_liq",
    "liq_over_mcap",
    "curve_entry",
    "log_vol_h1",
    "vol_over_mcap",
    "log_vol_m5",
    "log_buys_m5",
    "log_sells_m5",
    "buy_share_m5",
    "no_m5",
    "log_age_sight",
    "log_ttm",
    "log_sight_after_mig",
    "instant_fill",
    "no_created",
    "log_holders",
    "top10",
    "top10_full",
    "creator_hold",
    "log_mcap_per_holder",
    "log_prior_launches",
    "has_twitter",
    "has_website",
    "has_telegram",
    "log_tw_followers",
    "log_tw_age",
    "tw_verified",
    "log_desc_len",
    "holders_unknown",
)

# Holder-derived columns. A holder count of 0 at first sight is a data gap
# (Helius plan cap, GMGN cooldown, public RPC miss), never a real book with
# nobody in it: these columns are imputed to the training mean for such rows
# and ``holders_unknown`` carries the fact instead.
HOLDER_FEATURES: tuple[str, ...] = ("log_holders", "log_mcap_per_holder")
# Columns that are not first sight on Robinhood (rewritten by the holder refresh).
RH_MUTABLE_FEATURES: frozenset[str] = frozenset({"log_holders", "log_mcap_per_holder", "top10", "top10_full", "creator_hold", "holders_unknown"})


def chain_features(chain: str) -> tuple[str, ...]:
    """The feature list a chain is fitted and scored on."""
    if normalize_chain(chain) == "robinhood":
        return tuple(n for n in FEATURES if n not in RH_MUTABLE_FEATURES)
    return FEATURES


_cache: dict[str, tuple[float, ModelArtifact | None]] = {}


def reset_cache() -> None:
    _cache.clear()


def _lg(v: Any) -> float:
    try:
        return math.log1p(max(0.0, float(v or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (a - b).total_seconds() / 60.0


def first_sight_features(
    *,
    mcap: float,
    liq: float,
    vol_h1: float,
    vol_m5: float,
    buys_m5: int,
    sells_m5: int,
    at: datetime,
    created_at: datetime | None,
    migrated_at: datetime | None,
    holders: int,
    top10_pct: float,
    creator_hold_pct: float,
    prior_launches: int,
    twitter: str,
    website: str,
    telegram: str,
    tw_followers: int,
    tw_age_days: float,
    tw_verified: bool,
    desc_len: int,
) -> dict[str, float]:
    """The frozen inputs, one dict, identical for training and live scoring."""
    from ..ledger import LEDGER_DEAD_LIQ

    mcap = float(mcap or 0.0)
    liq = float(liq or 0.0)
    tot = int(buys_m5 or 0) + int(sells_m5 or 0)
    age = _minutes(at, created_at)
    ttm = _minutes(migrated_at, created_at)
    sam = _minutes(at, migrated_at)
    hc = int(holders or 0)
    return {
        "log_mcap": _lg(mcap),
        "log_liq": _lg(liq),
        "liq_over_mcap": min(5.0, liq / mcap) if mcap > 0 else 0.0,
        "curve_entry": float(liq < LEDGER_DEAD_LIQ),
        "log_vol_h1": _lg(vol_h1),
        "vol_over_mcap": min(20.0, float(vol_h1 or 0.0) / mcap) if mcap > 0 else 0.0,
        "log_vol_m5": _lg(vol_m5),
        "log_buys_m5": _lg(buys_m5),
        "log_sells_m5": _lg(sells_m5),
        "buy_share_m5": (int(buys_m5 or 0) / tot) if tot else 0.5,
        "no_m5": float(tot == 0),
        "log_age_sight": _lg(max(0.0, age) if age is not None else MISSING_MINUTES),
        "log_ttm": _lg(max(0.0, ttm) if ttm is not None else MISSING_MINUTES),
        "log_sight_after_mig": _lg(max(0.0, sam) if sam is not None else MISSING_MINUTES),
        "instant_fill": float(ttm is not None and 0.0 <= ttm <= 1.0),
        "no_created": float(created_at is None),
        "log_holders": _lg(hc),
        "top10": min(1.0, float(top10_pct or 0.0) / 100.0),
        "top10_full": float(float(top10_pct or 0.0) >= 99.9),
        "creator_hold": min(1.0, float(creator_hold_pct or 0.0) / 100.0),
        "log_mcap_per_holder": _lg(mcap / hc) if hc > 0 and mcap > 0 else 0.0,
        "log_prior_launches": _lg(prior_launches),
        "has_twitter": float(bool(twitter)),
        "has_website": float(bool(website)),
        "has_telegram": float(bool(telegram)),
        "log_tw_followers": _lg(tw_followers),
        "log_tw_age": _lg(tw_age_days),
        "tw_verified": float(bool(tw_verified)),
        "log_desc_len": _lg(desc_len),
        "holders_unknown": float(hc <= 0),
    }


def book_is_fillable(liq: float | None) -> bool:
    """A buyer could pay this print. Same $5k floor the ledger uses."""
    from ..ledger import LEDGER_SELLABLE_LIQ

    return float(liq or 0.0) >= LEDGER_SELLABLE_LIQ


def _research_features(research: Research | None) -> dict[str, Any]:
    if research is None:
        return {}
    try:
        feats = json.loads(research.features_json or "{}")
    except json.JSONDecodeError:
        return {}
    return feats if isinstance(feats, dict) else {}


def research_awaiting_fill(research: Research | None) -> bool:
    return bool(_research_features(research).get(AWAITING_FILL_KEY))


def entry_is_fill_finalized(research: Research | None) -> bool:
    return bool(_research_features(research).get(FILL_FINALIZED_KEY))


def hunt_entry_locked(research: Research | None, stored_entry: float, stored_scorer: str | None) -> bool:
    """True when Hunt must keep the frozen Entry (a fillable first sight)."""
    from ..desk_lines import SCORER_FIRST_SIGHT

    stored = float(stored_entry or 0.0)
    if research_awaiting_fill(research):
        return False
    scorer = str(stored_scorer or (getattr(research, "scorer", None) or "") or "")
    # v80 wall: first-sight stamped 0.01 on an empty book. Unlock once so
    # the first fillable print can replace it even after fill_finalized.
    if scorer == SCORER_FIRST_SIGHT and 0 < stored <= 0.02:
        return False
    if entry_is_fill_finalized(research):
        return stored > 0
    return stored > 0


def frozen_decision_entry(session, token_id: int | None) -> tuple[float, str] | None:
    """Fillable frozen Entry from the ledger Decision, or None.

    Hunt cards and paper used HuntCard.entry_p. After a fillable freeze
    that number can drift (NBS ledger 0.24 / Hunt 0.63; JERRY 0.16 / 0.79)
    and paper fills the drifted hi. A Decision at the empty-book 0.01
    floor is not frozen — same unlock as hunt_entry_locked.
    Does not rewrite research.p_good.
    """
    from ..models import Decision

    if not token_id:
        return None
    dec = (
        session.query(Decision)
        .filter(Decision.token_id == int(token_id), Decision.kind == "entry")
        .order_by(Decision.at.desc())
        .first()
    )
    if dec is None:
        return None
    p = float(dec.entry_p or 0.0)
    if p <= 0.02:
        return None
    return p, str(dec.scorer or "")


def mark_awaiting_fill(research: Research) -> None:
    feats = _research_features(research)
    feats[AWAITING_FILL_KEY] = 1
    feats.pop(FILL_FINALIZED_KEY, None)
    research.features_json = json.dumps(feats, default=str)


def mark_fill_finalized(research: Research) -> None:
    feats = _research_features(research)
    feats[AWAITING_FILL_KEY] = 0
    feats[FILL_FINALIZED_KEY] = 1
    research.features_json = json.dumps(feats, default=str)


def finalize_first_sight_entry(session: Session, token: Token, market: dict[str, Any] | None) -> bool:
    """One-time: stamp first-sight Entry on the first sellable print.

    Pre-book ingest leaves awaiting_fill and does not write a Decision.
    Prod cards already frozen at 0.01 get the same one rewrite. After
    this, Entry stays frozen. Does not grow FEATURE_NAMES.
    """
    from ..desk_lines import SCORER_FIRST_SIGHT, scorer_for
    from ..ledger import record_entry_decision

    research = token.research
    market = market or {}
    if research is None or token.is_historical or (token.source or "") in ("backfill", "rh_backfill"):
        return False
    if not book_is_fillable(market.get("liquidity_usd")):
        return False
    awaiting = research_awaiting_fill(research)
    locked_empty = (getattr(research, "scorer", None) == SCORER_FIRST_SIGHT) and float(research.p_good or 0.0) <= 0.02
    # v80 stamped fill_finalized on a 0.01 empty-book Entry. That is not a
    # fillable freeze — rewrite once. A real fillable stamp stays locked.
    if not awaiting and not locked_empty:
        return False
    chain = normalize_chain(token.chain or "sol")
    holder_info = {
        "holder_count": int(research.holder_count or 0),
        "top10_pct": float(research.top10_pct or 0.0),
        "creator_hold_pct": float(research.creator_hold_pct or 0.0),
    }
    creator_stats = {"launches": int(research.creator_prior_launches or 0)}
    tw = {
        "followers": int(research.twitter_followers or 0),
        "age_days": float(research.twitter_age_days or 0.0),
        "verified": bool(research.twitter_verified),
    }
    fs_feats = features_from_live(
        token,
        market=market,
        holder_info=holder_info,
        creator_stats=creator_stats,
        twitter=tw,
        twitter_url=research.twitter_handle or token.twitter or "",
        website=token.website or "",
        telegram=token.telegram or "",
    )
    fs_p = first_sight_p(session, chain, fs_feats)
    if fs_p is None:
        return False
    scored = {
        "p_good": fs_p,
        "first_sight_p": fs_p,
        "heuristic_p": float(research.heuristic_p or 0.0),
        "model_p": float(research.model_p or 0.0),
        "risk_flags": [],
        "awaiting_fill": False,
    }
    try:
        scored["risk_flags"] = json.loads(research.risk_flags_json or "[]")
    except json.JSONDecodeError:
        scored["risk_flags"] = []
    research.p_good = fs_p
    research.scorer = scorer_for(scored)
    mark_fill_finalized(research)
    # Recompute thesis keys from stored raw before the one-shot Decision stamp.
    try:
        from .thesis_enrich import sync_thesis_from_raw

        sync_thesis_from_raw(research, token)
    except Exception:
        log.exception("thesis sync before finalize failed for %s", token.mint[:12])
    record_entry_decision(
        session,
        token,
        research,
        scored,
        market=market,
        holder_count=int(research.holder_count or 0),
    )
    log.info("first-sight Entry finalized %s p=%.4f liq=%.0f", token.mint[:12], fs_p, float(market.get("liquidity_usd") or 0.0))
    return True


def vector(feats: dict[str, float], names: tuple[str, ...] = FEATURES) -> list[float]:
    return [float(feats.get(name, 0.0) or 0.0) for name in names]


def features_from_rows(decision: Decision, token: Token, research: Research | None, t0: Snapshot | None) -> dict[str, float]:
    """Training-time features from the frozen columns of one decision."""
    from ..ledger import _aware

    return first_sight_features(
        mcap=float(decision.entry_mcap or 0.0),
        liq=float(decision.liq or 0.0),
        vol_h1=float(decision.vol_h1 or 0.0),
        vol_m5=float(t0.volume_m5 or 0.0) if t0 else 0.0,
        buys_m5=int(t0.buys_m5 or 0) if t0 else 0,
        sells_m5=int(t0.sells_m5 or 0) if t0 else 0,
        at=_aware(decision.at) or utcnow(),
        created_at=_aware(token.created_at_chain),
        migrated_at=_aware(token.migrated_at),
        holders=int(decision.holders or 0),
        top10_pct=float(research.top10_pct or 0.0) if research else 0.0,
        creator_hold_pct=float(research.creator_hold_pct or 0.0) if research else 0.0,
        prior_launches=int(research.creator_prior_launches or 0) if research else 0,
        twitter=token.twitter or "",
        website=token.website or "",
        telegram=token.telegram or "",
        tw_followers=int(research.twitter_followers or 0) if research else 0,
        tw_age_days=float(research.twitter_age_days or 0.0) if research else 0.0,
        tw_verified=bool(research.twitter_verified) if research else False,
        desc_len=len(token.description or ""),
    )


def features_from_live(
    token: Token,
    *,
    market: dict[str, Any],
    holder_info: dict[str, Any],
    creator_stats: dict[str, Any],
    twitter: dict[str, Any],
    twitter_url: str,
    website: str,
    telegram: str,
    at: datetime | None = None,
) -> dict[str, float]:
    """Live features at research time, from the same facts the research row
    and t0 snapshot are about to be written from."""
    from ..ledger import _aware

    return first_sight_features(
        mcap=float(market.get("mcap_usd") or 0.0),
        liq=float(market.get("liquidity_usd") or 0.0),
        vol_h1=float(market.get("volume_h1") or 0.0),
        vol_m5=float(market.get("volume_m5") or 0.0),
        buys_m5=int(market.get("buys_m5") or 0),
        sells_m5=int(market.get("sells_m5") or 0),
        at=at or utcnow(),
        created_at=_aware(token.created_at_chain),
        migrated_at=_aware(token.migrated_at),
        holders=int(holder_info.get("holder_count") or holder_info.get("holder_sample") or 0),
        top10_pct=float(holder_info.get("top10_pct") or 0.0),
        creator_hold_pct=float(holder_info.get("creator_hold_pct") or 0.0),
        prior_launches=int(creator_stats.get("launches") or 0),
        twitter=twitter_url or token.twitter or "",
        website=website or token.website or "",
        telegram=telegram or token.telegram or "",
        tw_followers=int(twitter.get("followers") or 0),
        tw_age_days=float(twitter.get("age_days") or 0.0),
        tw_verified=bool(twitter.get("verified")),
        desc_len=len(token.description or ""),
    )


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


def training_rows(session: Session, chain: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every resolved entry decision with its frozen first-sight inputs."""
    from ..ledger import resolve_entry_decisions

    now = now or utcnow()
    resolved = [(d, o, r) for d, o, r in resolve_entry_decisions(session, chain, now=now) if r is not None]
    if not resolved:
        return []
    ids = sorted({d.token_id for d, _o, _r in resolved})
    toks: dict[int, Token] = {}
    res: dict[int, Research] = {}
    t0s: dict[int, Snapshot] = {}
    from sqlalchemy.orm import load_only

    for i in range(0, len(ids), 5000):
        chunk = ids[i : i + 5000]
        for t in session.query(Token).options(load_only(Token.id, Token.created_at_chain, Token.migrated_at, Token.twitter, Token.website, Token.telegram, Token.description)).filter(Token.id.in_(chunk)).all():
            toks[t.id] = t
        for r in (
            session.query(Research)
            .options(load_only(Research.id, Research.token_id, Research.top10_pct, Research.creator_hold_pct, Research.creator_prior_launches, Research.twitter_followers, Research.twitter_age_days, Research.twitter_verified))
            .filter(Research.token_id.in_(chunk))
            .all()
        ):
            res[r.token_id] = r
        for sn in session.query(Snapshot).filter(Snapshot.token_id.in_(chunk), Snapshot.kind == "t0").all():
            t0s[sn.token_id] = sn
    names = chain_features(chain)
    out: list[dict[str, Any]] = []
    for d, _o, r in resolved:
        t = toks.get(d.token_id)
        if t is None:
            continue
        feats = features_from_rows(d, t, res.get(d.token_id), t0s.get(d.token_id))
        age_sight = math.expm1(float(feats.get("log_age_sight") or 0.0))
        if TRAIN_MAX_AGE_SIGHT_MIN and age_sight >= TRAIN_MAX_AGE_SIGHT_MIN:
            continue
        out.append(
            {
                "at": d.at,
                "x": vector(feats, names),
                "y": 1 if r["hit2x"] else 0,
                "y5": 1 if r["hit5x"] else 0,
                "y_ran": 1 if float(r.get("multiple") or 0.0) >= RAN_MULTIPLE and not r.get("dead") else 0,
                "entry_p": float(d.entry_p or 0.0),
                "source": d.source,
                "unfilled": bool(r.get("unfilled")),
                "filled_later": bool(r.get("fill_multiple")),
            }
        )
    out.sort(key=lambda row: row["at"])
    return out


def time_split_days(rows: list[dict[str, Any]], *, now: datetime, days: float = VALID_DAYS) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The newest ``days`` validate; a tape is never shuffled."""
    from ..ledger import _aware

    cut = now - timedelta(days=days)
    train = [r for r in rows if (_aware(r["at"]) or now) < cut]
    valid = [r for r in rows if (_aware(r["at"]) or now) >= cut]
    return train, valid


# --------------------------------------------------------------------------
# Fit / apply
# --------------------------------------------------------------------------


def _standardize(X: np.ndarray, mu: np.ndarray, sd: np.ndarray, names: tuple[str, ...] = FEATURES) -> np.ndarray:
    """z-score; holder columns sit at the mean (z = 0) when the count is unknown."""
    Z = (X - mu) / sd
    if "holders_unknown" in names:
        unknown = X[:, names.index("holders_unknown")] > 0.5
        for col in HOLDER_FEATURES:
            if col in names:
                Z[unknown, names.index(col)] = 0.0
    return Z


def _moments(X: np.ndarray, names: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
    """Training mean / sd, with the holder columns measured on known rows only."""
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    if "holders_unknown" in names:
        known = X[:, names.index("holders_unknown")] < 0.5
        if known.sum() >= 2:
            for col in HOLDER_FEATURES:
                if col in names:
                    j = names.index(col)
                    mu[j] = X[known, j].mean()
                    sd[j] = X[known, j].std()
    sd[sd < 1e-9] = 1.0
    return mu, sd


def _unknown_share(X: np.ndarray, names: tuple[str, ...]) -> float | None:
    if "holders_unknown" not in names or len(X) == 0:
        return None
    return round(float((X[:, names.index("holders_unknown")] > 0.5).mean()), 4)


def artifact_features(art_weights: dict[str, Any]) -> tuple[str, ...]:
    """The feature list an artifact was fitted on (pre-v77 artifacts: the
    then-current tuple, which is a prefix of today's)."""
    names = art_weights.get("features")
    if isinstance(names, list) and names:
        return tuple(str(n) for n in names)
    return tuple(n for n in FEATURES if n in (art_weights.get("w") or {}))


def _raw(art_weights: dict[str, Any], X: np.ndarray, bias: float, names: tuple[str, ...] | None = None) -> np.ndarray:
    names = names or artifact_features(art_weights)
    w = np.array([float(art_weights["w"].get(n, 0.0)) for n in names])
    mu = np.array([float(art_weights["mu"].get(n, 0.0)) for n in names])
    sd = np.array([float(art_weights["sd"].get(n, 1.0)) or 1.0 for n in names])
    return _sigmoid(_standardize(X, mu, sd, names) @ w + bias)


def latest_promoted(session: Session, chain: str) -> ModelArtifact | None:
    return (
        session.query(ModelArtifact)
        .filter(ModelArtifact.chain == normalize_chain(chain), ModelArtifact.kind == KIND, ModelArtifact.promoted.is_(True))
        .order_by(ModelArtifact.id.desc())
        .first()
    )


def promoted_artifact(session: Session, chain: str) -> ModelArtifact | None:
    key = normalize_chain(chain)
    hit = _cache.get(key)
    mono = time.monotonic()
    if hit and mono - hit[0] < ARTIFACT_CACHE_SECONDS:
        return hit[1]
    art = latest_promoted(session, chain)
    if art is not None:
        session.expunge(art)
    _cache[key] = (mono, art)
    return art


def first_sight_p(session: Session, chain: str, feats: dict[str, float]) -> float | None:
    """Calibrated probability from the promoted artifact, or None when the
    chain has none (the caller keeps the legacy Entry)."""
    chain = normalize_chain(chain)
    if chain not in FIRST_SIGHT_CHAINS:
        return None
    art = promoted_artifact(session, chain)
    if art is None:
        return None
    try:
        weights = json.loads(art.weights_json or "{}")
        cal = json.loads(art.calibration_json or "[]") or None
    except json.JSONDecodeError:
        return None
    if not isinstance(weights, dict) or "w" not in weights:
        return None
    names = artifact_features(weights)
    raw = float(_raw(weights, np.array([vector(feats, names)], dtype=float), float(art.bias or 0.0), names)[0])
    return round(min(0.99, max(0.01, apply_calibration(cal, raw))), 4)


def line_metrics(cal_p: np.ndarray, y2: np.ndarray, y5: np.ndarray, valid: list[dict[str, Any]], *, now: datetime, chain: str = "sol") -> dict[str, dict[str, Any]]:
    """What the two desk lines would have done on the validation tail:
    rows a day, forward 2x / 5x rate, share of validation above the line.
    This is the number the Board shows next to "Entry >= 50"."""
    from ..desk_lines import SCORER_FIRST_SIGHT, lines_for_scorer
    from ..ledger import _aware

    if len(valid) == 0:
        return {}
    dl = lines_for_scorer(SCORER_FIRST_SIGHT, chain)
    first = min((_aware(r["at"]) or now) for r in valid)
    days = max(1.0, (now - first).total_seconds() / 86400.0)
    out: dict[str, dict[str, Any]] = {}
    for slot, thr in (("lo", dl.lo), ("hi", dl.hi)):
        sel = cal_p >= thr
        n = int(sel.sum())
        out[slot] = {
            "threshold": thr,
            "n": n,
            "per_day": round(n / days, 1),
            "share": round(n / len(cal_p), 4),
            "hit2x": round(float(y2[sel].mean()), 4) if n else None,
            "hit5x": round(float(y5[sel].mean()), 4) if n else None,
        }
    return out


def held_ranking_on_new_label(cand: dict[str, Any], inc: dict[str, Any]) -> bool:
    """A candidate that ties the re-judged incumbent: same ranking within
    should_promote's slack and Brier within ``LABEL_CHANGE_BRIER_SLACK``
    (relative). Positives still gate. Used on a label change *and* on a
    same-label refit so RH cannot stall forever on a 0.0001 Brier miss."""
    from .batch_fit import PROMOTE_AUC_SLACK, PROMOTE_TOP_DECILE_SLACK

    if not cand.get("n") or cand.get("positives", 0) < MIN_POSITIVES:
        return False
    if inc.get("auc") is not None and cand.get("auc") is not None and cand["auc"] + PROMOTE_AUC_SLACK < inc["auc"]:
        return False
    if cand.get("precision_top_decile", 0.0) + PROMOTE_TOP_DECILE_SLACK < inc.get("precision_top_decile", 0.0):
        return False
    inc_brier = inc.get("brier")
    if inc_brier is None:
        return True
    return float(cand.get("brier", 1.0)) <= float(inc_brier) * (1.0 + LABEL_CHANGE_BRIER_SLACK)


def ranking_held_promote_reason(
    cand: dict[str, Any],
    inc: dict[str, Any],
    *,
    prev: Any,
    same_label: bool,
    prev_label: int,
) -> str | None:
    """Promote when ranking holds even if Brier is a hair worse.

    Live already re-scores the incumbent on the new slice. First-sight
    RH v7 kept failing should_promote on ~0.0001 Brier. FEATURE_NAMES
    stays 66. Do not raise Sol hi.
    """
    if prev is None or not held_ranking_on_new_label(cand, inc):
        return None
    brier = f"brier {cand.get('brier')} vs {inc.get('brier')}"
    if not same_label:
        return (
            f"label v{LABEL_VERSION}: incumbent v{prev.version} was fitted "
            f"on label v{prev_label}; ranking held ({brier})"
        )
    return f"ranking held ({brier})"


def _write_ran_shadow(
    session: Session,
    chain: str,
    train: list[dict[str, Any]],
    valid: list[dict[str, Any]],
    names: tuple[str, ...],
    mu: np.ndarray,
    sd: np.ndarray,
    now: datetime,
) -> dict[str, Any] | None:
    """Fit P(sellable peak ≥ 1.5×) on the same rows. Never promote it."""
    if not train or not valid or "y_ran" not in train[0]:
        return None
    yt = np.array([r["y_ran"] for r in train], dtype=float)
    yv = np.array([r["y_ran"] for r in valid], dtype=float)
    if float(yt.sum()) < MIN_POSITIVES or float(yv.sum()) < MIN_POSITIVES:
        return None
    xt = np.array([r["x"] for r in train], dtype=float)
    xv = np.array([r["x"] for r in valid], dtype=float)
    w, b = fit_logistic(_standardize(xt, mu, sd, names), yt, l2=L2)
    raw = _sigmoid(_standardize(xv, mu, sd, names) @ w + b)
    cal = isotonic_fit(raw, yv)
    cal_v = np.array([apply_calibration(cal, float(p)) for p in raw])
    cand = metrics(cal_v, yv)
    n_hi = int((cal_v >= 0.20).sum())
    cand["n_ge_020"] = n_hi
    cand["share_ge_020"] = round(n_hi / len(cal_v), 4) if len(cal_v) else 0.0
    prev = (
        session.query(ModelArtifact)
        .filter(ModelArtifact.chain == chain, ModelArtifact.kind == RAN_KIND)
        .order_by(ModelArtifact.id.desc())
        .first()
    )
    art = ModelArtifact(
        chain=chain,
        kind=RAN_KIND,
        created_at=now,
        version=(prev.version + 1) if prev else 1,
        weights_json=json.dumps({"features": list(names), "w": {n: float(x) for n, x in zip(names, w, strict=True)}}),
        bias=float(b),
        calibration_json=json.dumps(cal),
        n_train=len(train),
        n_valid=len(valid),
        metrics_json=json.dumps(
            {
                **cand,
                "promote_reason": "shadow only — does not become Entry",
                "label": "sellable peak >= 1.5x from the fillable print; lifetime high never counts",
            }
        ),
        incumbent_json="{}",
        promoted=False,
    )
    session.add(art)
    session.flush()
    log.info(
        "ran shadow %s v%s auc %s share>=0.20 %s (n %s)",
        chain,
        art.version,
        cand.get("auc"),
        cand["share_ge_020"],
        n_hi,
    )
    return {
        "promoted": False,
        "auc": cand.get("auc"),
        "n_ge_020": n_hi,
        "share_ge_020": cand["share_ge_020"],
        "version": art.version,
    }


def fit_first_sight(session: Session, chain: str, *, now: datetime | None = None, promote: bool = True) -> dict[str, Any]:
    """One fit. Writes an artifact row (promoted or not) whenever a fit ran."""
    from ..ledger import _aware

    chain = normalize_chain(chain)
    now = now or utcnow()
    prev = latest_promoted(session, chain)
    prev_label = LABEL_VERSION
    if prev is not None:
        try:
            prev_label = int((json.loads(prev.metrics_json or "{}") or {}).get("label_version") or 1)
        except (json.JSONDecodeError, TypeError, ValueError):
            prev_label = LABEL_VERSION
    same_label = prev_label == LABEL_VERSION
    if prev is not None and promote:
        age_h = (now - (_aware(prev.created_at) or now)).total_seconds() / 3600.0
        # A fit on a different column set or label is a new model, not a
        # same-window refit of the incumbent; the isotonic-on-validation
        # caveat behind REFIT_HOURS does not apply to it.
        try:
            same_columns = artifact_features(json.loads(prev.weights_json or "{}")) == chain_features(chain)
        except (json.JSONDecodeError, TypeError, ValueError):
            same_columns = True
        try:
            prev_age_cut = float((json.loads(prev.metrics_json or "{}") or {}).get("train_max_age_sight_min") or 0.0)
        except (json.JSONDecodeError, TypeError, ValueError):
            prev_age_cut = 0.0
        same_train = prev_age_cut == TRAIN_MAX_AGE_SIGHT_MIN
        if age_h < REFIT_HOURS and same_columns and same_label and same_train:
            return {"chain": chain, "kind": KIND, "fitted": False, "reason": f"incumbent v{prev.version} is {age_h:.1f}h old (< {REFIT_HOURS:.0f}h)"}
    rows = training_rows(session, chain, now=now)
    if len(rows) < MIN_ROWS:
        return {"chain": chain, "kind": KIND, "fitted": False, "reason": f"{len(rows)} rows < {MIN_ROWS}"}
    train, valid = time_split_days(rows, now=now)
    if len(valid) < MIN_VALID or sum(r["y"] for r in train) < MIN_POSITIVES or sum(r["y"] for r in valid) < MIN_POSITIVES:
        return {"chain": chain, "kind": KIND, "fitted": False, "reason": "not enough validation rows or positives"}
    names = chain_features(chain)
    Xt = np.array([r["x"] for r in train], dtype=float)
    yt = np.array([r["y"] for r in train], dtype=float)
    Xv = np.array([r["x"] for r in valid], dtype=float)
    yv = np.array([r["y"] for r in valid], dtype=float)
    y5v = np.array([r["y5"] for r in valid], dtype=float)
    mu, sd = _moments(Xt, names)
    w, b = fit_logistic(_standardize(Xt, mu, sd, names), yt, l2=L2)
    raw_v = _sigmoid(_standardize(Xv, mu, sd, names) @ w + b)
    cal = isotonic_fit(raw_v, yv)
    cal_v = np.array([apply_calibration(cal, float(p)) for p in raw_v])
    cand = metrics(cal_v, yv)
    cand["raw_auc"] = metrics(raw_v, yv)["auc"]
    k = max(1, len(yv) // 10)
    top = np.argsort(-raw_v, kind="stable")[:k]
    cand["hit5x_top_decile"] = round(float(y5v[top].mean()), 4)
    cand["hit2x_top_decile_raw"] = round(float(yv[top].mean()), 4)
    cand["lines"] = line_metrics(cal_v, yv, y5v, valid, now=now, chain=chain)
    baseline = metrics(np.array([r["entry_p"] for r in valid], dtype=float), yv)
    inc = baseline
    if prev is not None:
        try:
            pw = json.loads(prev.weights_json or "{}")
            pcal = json.loads(prev.calibration_json or "[]") or None
            pnames = artifact_features(pw)
            # The incumbent is scored on its own columns, from the same rows.
            pX = np.array([[float(x) for x in vector(dict(zip(names, r["x"], strict=True)), pnames)] for r in valid], dtype=float)
            praw = _raw(pw, pX, float(prev.bias or 0.0), pnames)
            inc = metrics(np.array([apply_calibration(pcal, float(p)) for p in praw]), yv)
            inc["source"] = f"first_sight v{prev.version}"
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            inc = baseline
    ok, why = should_promote(cand, inc) if promote else (False, "promotion disabled")
    if promote and not ok:
        held = ranking_held_promote_reason(
            cand, inc, prev=prev, same_label=same_label, prev_label=prev_label
        )
        if held:
            ok = True
            why = held
    art = ModelArtifact(
        chain=chain,
        kind=KIND,
        created_at=now,
        version=(prev.version + 1) if prev else 1,
        weights_json=json.dumps(
            {
                "features": list(names),
                "w": {n: float(x) for n, x in zip(names, w, strict=True)},
                "mu": {n: float(x) for n, x in zip(names, mu, strict=True)},
                "sd": {n: float(x) for n, x in zip(names, sd, strict=True)},
            }
        ),
        bias=float(b),
        calibration_json=json.dumps(cal),
        n_train=len(train),
        n_valid=len(valid),
        metrics_json=json.dumps(
            {
                **cand,
                "valid_from": valid[0]["at"].isoformat() if valid else None,
                "entry_p_baseline": baseline,
                "promote_reason": why,
                # Frozen columns, not features_json: honest for seed rows too.
                "live_only": True,
                "frozen_columns": True,
                "seed_rows": sum(1 for r in rows if r["source"] == "seed_t0"),
                "live_rows": sum(1 for r in rows if r["source"] == "live"),
                "n_features": len(names),
                "holders_unknown_valid": _unknown_share(Xv, names),
                "holders_unknown_train": _unknown_share(Xt, names),
                "label_version": LABEL_VERSION,
                "train_max_age_sight_min": TRAIN_MAX_AGE_SIGHT_MIN,
                "label": "2x within 24h from the first fillable print (liq >= sellable floor) to a print we saw; lifetime high never counts",
                # Validation rows the desk could never have bought (no
                # sellable print inside the horizon) and rows judged from a
                # later pool print rather than the sight print.
                "unfilled_valid": round(sum(1 for r in valid if r.get("unfilled")) / len(valid), 4),
                "filled_later_valid": round(sum(1 for r in valid if r.get("filled_later")) / len(valid), 4),
            }
        ),
        incumbent_json=json.dumps(inc),
        promoted=bool(ok),
    )
    session.add(art)
    session.flush()
    if ok:
        reset_cache()
        log.info("promoted %s first-sight v%s: %s (auc %.3f, top decile %.3f vs entry_p %.3f)", chain, art.version, why, cand.get("auc") or 0.0, cand["precision_top_decile"], baseline.get("precision_top_decile", 0.0))
    else:
        log.info("kept %s first-sight incumbent: %s", chain, why)
    ran = _write_ran_shadow(session, chain, train, valid, names, mu, sd, now)
    return {
        "chain": chain,
        "kind": KIND,
        "fitted": True,
        "promoted": bool(ok),
        "reason": why,
        "candidate": cand,
        "incumbent": inc,
        "entry_p_baseline": baseline,
        "n_train": len(train),
        "n_valid": len(valid),
        "version": art.version,
        "ran_shadow": ran,
    }
