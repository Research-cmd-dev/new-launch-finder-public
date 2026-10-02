"""FOMO trending but not on Hunt — Learn labels (stack-v206).

Dave: a name that makes FOMO Tokens→Trending and is *not* on Hunt is a
definite issue. Capture those rows (on_desk/caught + on_hunt=false, plus
true door misses) as write-once Learn side keys. Not a buy path.

stack-v206 runs granular Learn on that store: actionable why-not-Hunt
beats blanket ``historical``, early snaps carry miss-cohort book/vol
fields, and the cohort emits runner-vs-dud separators. Autopsy list
is 32+. Still does **not** open paperV1.

``FOMO_NO_HUNT_OPEN = False``. FEATURE_NAMES stays 66.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain, normalize_mint
from ..desk_lines import lines_for_scorer
from ..models import Decision, HuntCard, Outcome, ScanState, Token, utcnow
from .hunt import hunt_fat_live_board_pin, hunt_hours, sol_launch_at
from .miss_cohort import (
    PAPER_MISS_SOCIAL_KEYS,
    PAPER_MISS_WIN_MULT,
    freeze_paper_miss_features,
    paper_miss_multiple,
)
from .outcomes import DEAD_POOL_LIQ, is_sol_old_leftover_major

# Decision.features_json side keys — not FEATURE_NAMES.
FOMO_TREND_NO_HUNT_KEY = "fomo_trend_no_hunt"
FOMO_TREND_NO_HUNT_WHY_KEY = "fomo_trend_no_hunt_why"
FOMO_TREND_NO_HUNT_SNAP_KEY = "fomo_trend_no_hunt_snap"
FOMO_TREND_NO_HUNT_AT_KEY = "fomo_trend_no_hunt_at"
FOMO_NO_HUNT_STATE_KEY = "fomo_trend_no_hunt"
FOMO_NO_HUNT_OPEN = False
FOMO_NO_HUNT_AUTOPSY_LIMIT = 32
# Runner-vs-dud separators. Holders / vol / book over social. No thesis tags.
FOMO_NO_HUNT_SEPARATOR_KEYS = (
    "holders",
    "liq",
    "entry_p",
    "age_hours",
    "vol_h1",
    "volume_m5",
    "volume_n",
    "liquidity_n",
    "holder_n",
    "buy_pressure",
    "organic_book",
    "mcap_per_holder",
    "top10_pct",
)
# Never invent thesis tags onto the frozen snap.
_FOMO_NO_HUNT_THESIS_KEYS = frozenset(
    {"github_auth_n", "real_project", "gmgn_cto", "name_quality", "thesis"}
)

# Why-not-Hunt taxonomy. First *actionable* match wins (see why_not_hunt).
# ``historical`` is a secondary snap flag / last resort — not first.
WHY_DOOR_MISS = "door_miss"
WHY_AGED_OFF = "aged_off"
WHY_SCORE_FLOOR = "score_floor"
WHY_GATE = "gate"
WHY_NEVER_CARDED = "never_carded"
WHY_LEFTOVER = "leftover"
WHY_THIN = "thin"
WHY_NO_DEX = "no_dex"
WHY_DEAD_POOL = "dead_pool"
WHY_HISTORICAL = "historical"
WHY_FOMO_STUB = "fomo_stub"

FOMO_NO_HUNT_WHYS = frozenset(
    {
        WHY_DOOR_MISS,
        WHY_AGED_OFF,
        WHY_SCORE_FLOOR,
        WHY_GATE,
        WHY_NEVER_CARDED,
        WHY_LEFTOVER,
        WHY_THIN,
        WHY_NO_DEX,
        WHY_DEAD_POOL,
        WHY_HISTORICAL,
        WHY_FOMO_STUB,
    }
)

# Capture: on_desk/caught but not on Hunt, plus true door misses.
# leftover/thin/no_dex *off* desk stay out (not Dave's issue).
FOMO_NO_HUNT_CAPTURE_STATUSES = frozenset({"caught", "miss"})


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _iso(ts: datetime | str | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    at = _aware(ts)
    return at.isoformat() if at is not None else None


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _feat(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _token_age_hours(token: Token | None, *, now: datetime | None = None) -> float | None:
    if token is None:
        return None
    now = _aware(now) or utcnow()
    chain = normalize_chain(token.chain or "sol")
    if chain == "sol":
        launched = sol_launch_at(token)
    else:
        launched = token.migrated_at or token.first_seen_at or token.created_at_chain
    launched = _aware(launched)
    if launched is None:
        return None
    return max(0.0, (now - launched).total_seconds() / 3600.0)


def is_fomo_no_hunt_item(item: dict[str, Any] | None) -> bool:
    """True when this FOMO board row belongs on the Learn no-Hunt cohort."""
    if not isinstance(item, dict):
        return False
    if item.get("on_hunt"):
        return False
    status = str(item.get("status") or "")
    if status == "miss":
        return True
    if item.get("on_desk") or status == "caught":
        return True
    return False


def why_not_hunt(
    item: dict[str, Any] | None,
    *,
    token: Token | None = None,
    now: datetime | None = None,
) -> str:
    """Canonical why-not-Hunt label. Empty when the name is on Hunt."""
    row = item if isinstance(item, dict) else {}
    if row.get("on_hunt"):
        return ""
    now = _aware(now) or utcnow()
    status = str(row.get("status") or "")
    if not row.get("on_desk"):
        if status in ("leftover", "thin", "no_dex"):
            return status
        return WHY_DOOR_MISS
    if status in ("leftover", "thin", "no_dex"):
        return status

    outcome = token.outcome if token is not None else None
    research = token.research if token is not None else None
    chain = normalize_chain(
        (token.chain if token is not None else None) or row.get("chain") or "sol"
    )
    if token is not None:
        from .high_si_learn import fomo_board_stub_unactionable

        if fomo_board_stub_unactionable(token, outcome):
            return WHY_FOMO_STUB
        # is_historical is a secondary flag (snap["historical"]), not first why.

    t0 = float(
        (outcome.t0_mcap if outcome is not None else 0.0)
        or _num(row.get("t0_mcap"))
        or 0.0
    )
    last = float(
        (outcome.last_mcap if outcome is not None else 0.0)
        or _num(row.get("last_mcap"))
        or _num(row.get("mcap_usd"))
        or 0.0
    )
    liq = float(
        (outcome.last_liq if outcome is not None else 0.0)
        or _num(row.get("last_liq"))
        or 0.0
    )
    multiple = float((outcome.multiple if outcome is not None else 0.0) or 0.0)
    if multiple <= 0 and t0 > 0 and last > 0:
        multiple = last / t0

    age_h = _token_age_hours(token, now=now)
    pin = bool(token is not None and hunt_fat_live_board_pin(token, outcome, now=now))
    if age_h is not None and age_h > hunt_hours(chain) and not pin:
        return WHY_AGED_OFF

    from .bloom import leftover_fdv

    if leftover_fdv(chain, t0, last):
        return WHY_LEFTOVER
    if chain == "sol" and token is not None and is_sol_old_leftover_major(
        max(last, float((outcome.max_mcap if outcome is not None else 0.0) or 0.0)),
        sol_launch_at(token),
        now=now,
        multiple=multiple,
    ):
        return WHY_LEFTOVER
    if 0 < liq < DEAD_POOL_LIQ:
        return WHY_DEAD_POOL

    veto = str(row.get("gate_veto") or "").strip()
    if veto:
        return WHY_GATE

    entry_p = _num(row.get("entry_p"))
    if entry_p is None and research is not None:
        entry_p = _num(research.p_good)
    scorer = None
    if research is not None:
        scorer = research.scorer
    lines = lines_for_scorer(scorer, chain)
    if entry_p is not None and entry_p < float(lines.thin):
        return WHY_SCORE_FLOOR

    if row.get("on_desk") or token is not None:
        return WHY_NEVER_CARDED
    if token is not None and bool(getattr(token, "is_historical", False)):
        return WHY_HISTORICAL
    return WHY_DOOR_MISS


def item_from_fomo_card(
    card: dict[str, Any] | None,
    early: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild a coverage-shaped row from a frozen ScanState / autopsy card."""
    row = card if isinstance(card, dict) else {}
    snap = early if isinstance(early, dict) else (
        row.get("early") if isinstance(row.get("early"), dict) else {}
    )
    return {
        "on_desk": bool(snap.get("on_desk", True)),
        "on_hunt": False,
        "status": str(snap.get("status") or row.get("status") or "caught"),
        "gate_veto": str(snap.get("gate_veto") or row.get("gate_veto") or ""),
        "entry_p": snap.get("entry_p") if snap.get("entry_p") is not None else row.get("entry_p"),
        "chain": row.get("chain") or snap.get("chain") or "sol",
        "last_mcap": snap.get("mcap") or row.get("mcap"),
        "t0_mcap": snap.get("t0_mcap") or row.get("t0_mcap"),
        "last_liq": snap.get("liq") or row.get("liq"),
        "vol_h1": snap.get("vol_h1") or row.get("vol_h1"),
        "volume_h1": snap.get("vol_h1") or row.get("volume_h1"),
        "volume_m5": snap.get("volume_m5") or row.get("volume_m5"),
    }


def resolve_fomo_no_hunt_why(
    stored: str | None,
    *,
    item: dict[str, Any] | None = None,
    token: Token | None = None,
    now: datetime | None = None,
) -> str:
    """Keep a frozen actionable why; recompute when the stamp is historical."""
    label = str(stored or "").strip()
    if label and label != WHY_HISTORICAL:
        return label
    live = why_not_hunt(item, token=token, now=now)
    if live and live != WHY_HISTORICAL:
        return live
    return label or live or WHY_HISTORICAL


def _latest_snapshot(token: Token | None) -> Any:
    if token is None:
        return None
    try:
        snaps = list(getattr(token, "snapshots", None) or [])
    except Exception:
        return None
    live = [s for s in snaps if s is not None]
    if not live:
        return None

    def _taken(snap: Any) -> datetime:
        at = _aware(getattr(snap, "taken_at", None))
        return at or datetime.min.replace(tzinfo=timezone.utc)

    return max(live, key=_taken)


def freeze_fomo_no_hunt_snap(
    item: dict[str, Any] | None,
    *,
    token: Token | None = None,
    decision: Decision | None = None,
    why: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Early facts frozen at first capture. Book/vol/holders over social."""
    row = item if isinstance(item, dict) else {}
    now = _aware(now) or utcnow()
    outcome = token.outcome if token is not None else None
    research = token.research if token is not None else None
    first_seen = None
    if token is not None:
        first_seen = token.first_seen_at or token.migrated_at
    if first_seen is None:
        first_seen = row.get("first_seen_at")
    paper = row.get("paper") if isinstance(row.get("paper"), dict) else {}
    t0 = float(
        (decision.entry_mcap if decision is not None else 0.0)
        or (outcome.t0_mcap if outcome is not None else 0.0)
        or 0.0
    )
    last = float(
        (outcome.last_mcap if outcome is not None else 0.0)
        or _num(row.get("last_mcap"))
        or _num(row.get("mcap_usd"))
        or 0.0
    )
    liq = float(
        (decision.liq if decision is not None else 0.0)
        or (outcome.last_liq if outcome is not None else 0.0)
        or _num(row.get("last_liq"))
        or 0.0
    )
    holders = int(
        (decision.holders if decision is not None else 0)
        or (research.holder_count if research is not None else 0)
        or 0
    )
    entry_p = _num(row.get("entry_p"))
    if entry_p is None and decision is not None:
        entry_p = _num(decision.entry_p)
    if entry_p is None and research is not None:
        entry_p = _num(research.p_good)
    scorer = (decision.scorer if decision is not None else None) or (
        research.scorer if research is not None else None
    )
    chain = normalize_chain(
        (token.chain if token is not None else None) or row.get("chain") or "sol"
    )
    lines = lines_for_scorer(scorer, chain)
    age_h = _token_age_hours(token, now=now)
    vol_h1 = _num(row.get("vol_h1") if row.get("vol_h1") is not None else row.get("volume_h1"))
    if vol_h1 is None and decision is not None:
        vol_h1 = _num(decision.vol_h1)
    vol_m5 = _num(row.get("volume_m5"))
    latest = _latest_snapshot(token)
    if latest is not None:
        if vol_h1 is None:
            vol_h1 = _num(getattr(latest, "volume_h1", None))
        if vol_m5 is None:
            vol_m5 = _num(getattr(latest, "volume_m5", None))
        if not liq:
            liq = float(_num(getattr(latest, "liquidity_usd", None)) or 0.0)
    feat: dict[str, Any] = {}
    if decision is not None:
        feat = _feat(decision.features_json)
    elif research is not None:
        feat = _feat(research.features_json)
    extra = {
        "holders": holders or None,
        "liq": liq or None,
        "vol_h1": vol_h1,
        "volume_m5": vol_m5,
        "t0_mcap": t0 or None,
    }
    book = freeze_paper_miss_features(
        feat,
        decision=decision,
        research=research,
        extra={k: v for k, v in extra.items() if v is not None},
    )
    snap: dict[str, Any] = {
        "why": why or "",
        "first_seen": _iso(first_seen),
        "age_hours": round(age_h, 2) if age_h is not None else None,
        "hunt_hours": hunt_hours(chain),
        "mcap": round(last) if last else None,
        "t0_mcap": round(t0) if t0 else None,
        "liq": round(liq) if liq else None,
        "holders": holders or None,
        "entry_p": round(float(entry_p), 4) if entry_p is not None else None,
        "vol_h1": round(float(vol_h1), 1) if vol_h1 is not None else None,
        "volume_m5": round(float(vol_m5), 1) if vol_m5 is not None else None,
        "gate_veto": str(row.get("gate_veto") or "").strip(),
        "paper": dict(paper),
        "desk_lines": {"lo": lines.lo, "hi": lines.hi, "thin": lines.thin, "scorer": lines.scorer},
        "source": (token.source if token is not None else None) or row.get("source") or "",
        "status": str(row.get("status") or ""),
        "on_desk": bool(row.get("on_desk")),
        "on_hunt": bool(row.get("on_hunt")),
    }
    if token is not None:
        snap["historical"] = bool(token.is_historical)
    core = set(snap)
    for key, val in book.items():
        if key in core or key in _FOMO_NO_HUNT_THESIS_KEYS:
            continue
        snap[key] = val
    # Social is recorded as weak contrast only — separators ignore it.
    for key in PAPER_MISS_SOCIAL_KEYS:
        if key in book and key not in core:
            snap[key] = book[key]
    return {k: v for k, v in snap.items() if v is not None and v != "" and v != {}}


def stamp_fomo_trend_no_hunt(
    features: dict[str, Any] | None,
    *,
    why: str,
    snap: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Write-once side keys so a no-Hunt FOMO name auto-joins Learn.

    Does not grow FEATURE_NAMES. Returns None when already frozen.
    """
    feat = dict(features) if isinstance(features, dict) else {}
    if feat.get(FOMO_TREND_NO_HUNT_KEY) and isinstance(feat.get(FOMO_TREND_NO_HUNT_SNAP_KEY), dict):
        return None
    label = str(why or "").strip()
    if label not in FOMO_NO_HUNT_WHYS:
        return None
    feat[FOMO_TREND_NO_HUNT_KEY] = True
    feat[FOMO_TREND_NO_HUNT_WHY_KEY] = label
    if snap:
        feat[FOMO_TREND_NO_HUNT_SNAP_KEY] = dict(snap)
    when = at or datetime.now(timezone.utc)
    feat[FOMO_TREND_NO_HUNT_AT_KEY] = when.isoformat()
    return feat


def has_fomo_trend_no_hunt(features: dict[str, Any] | None) -> bool:
    feat = features if isinstance(features, dict) else {}
    return bool(feat.get(FOMO_TREND_NO_HUNT_KEY))


def _early_for_sep(row: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten early snap + top-level book fields for separator means."""
    row = row if isinstance(row, dict) else {}
    early = row.get("early") if isinstance(row.get("early"), dict) else {}
    snap = dict(early)
    for key in FOMO_NO_HUNT_SEPARATOR_KEYS:
        if snap.get(key) is None and row.get(key) is not None:
            snap[key] = row.get(key)
    return snap


def _fomo_separator_rows(
    winners: list[dict[str, Any]],
    duds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mean delta of frozen early keys. Holders / vol / book over social."""
    from .signal_score import feature_means

    keys = FOMO_NO_HUNT_SEPARATOR_KEYS
    means_w = feature_means(winners, keys)
    means_d = feature_means(duds, keys)
    rows: list[dict[str, Any]] = []
    for key in keys:
        w = float(means_w.get(key) or 0.0)
        d = float(means_d.get(key) or 0.0)
        delta = round(w - d, 4)
        rows.append(
            {
                "feature": key,
                "winner_mean": w,
                "dud_mean": d,
                "delta": delta,
                "abs_delta": round(abs(delta), 4),
                "prefer": "holders_vol_book",
            }
        )
    rows.sort(key=lambda r: -float(r["abs_delta"]))
    return rows


def _split_runner_dud_snaps(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    winners: list[dict[str, Any]] = []
    duds: list[dict[str, Any]] = []
    n_ran = 0
    n_dud = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        mult = _num(row.get("later_multiple") if row.get("later_multiple") is not None else row.get("multiple"))
        ran = bool(row.get("ran") or row.get("hit5x") or ((mult or 0.0) >= PAPER_MISS_WIN_MULT))
        snap = _early_for_sep(row)
        if ran:
            n_ran += 1
            winners.append(snap)
        else:
            n_dud += 1
            duds.append(snap)
    return winners, duds, n_ran, n_dud


def fomo_no_hunt_autopsy(
    *,
    mint: str,
    symbol: str = "",
    why: str = "",
    chain: str = "sol",
    t0_mcap: float = 0.0,
    peak_mcap: float = 0.0,
    multiple: float | None = None,
    early: dict[str, Any] | None = None,
    note: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-name Learn card. Trending → no Hunt → (optional) later run."""
    snap = dict(early) if isinstance(early, dict) else {}
    t0 = float(t0_mcap or snap.get("t0_mcap") or 0.0)
    peak = float(peak_mcap or 0.0)
    mult = float(multiple) if multiple is not None else (
        (peak / t0) if t0 > 0 and peak > 0 else 0.0
    )
    card = {
        "mint": mint,
        "symbol": symbol or "",
        "chain": normalize_chain(chain),
        "why": why or snap.get("why") or "",
        "first_seen": snap.get("first_seen"),
        "mcap": snap.get("mcap"),
        "t0_mcap": round(t0) if t0 else snap.get("t0_mcap"),
        "peak_mcap": round(peak) if peak else None,
        "multiple": round(mult, 2) if mult else None,
        "hit5x": bool(mult >= PAPER_MISS_WIN_MULT),
        "gate_veto": snap.get("gate_veto") or "",
        "paper": snap.get("paper") if isinstance(snap.get("paper"), dict) else {},
        "desk_lines": snap.get("desk_lines") or {},
        "early": snap,
        "side_key": FOMO_TREND_NO_HUNT_KEY,
        "paper_only": True,
        "open": FOMO_NO_HUNT_OPEN,
        "note": note or (
            "FOMO trending but not on Hunt. Learn / shadow only. Not a buy."
        ),
    }
    if extra:
        card.update(extra)
    return card


def load_fomo_no_hunt_state(session: Session) -> dict[str, Any]:
    row = session.query(ScanState).filter(ScanState.key == FOMO_NO_HUNT_STATE_KEY).one_or_none()
    if row is None or not row.value:
        return {"cards": {}, "side_key": FOMO_TREND_NO_HUNT_KEY, "paper_only": True}
    try:
        data = json.loads(row.value)
    except Exception:
        return {"cards": {}, "side_key": FOMO_TREND_NO_HUNT_KEY, "paper_only": True}
    if not isinstance(data, dict):
        return {"cards": {}, "side_key": FOMO_TREND_NO_HUNT_KEY, "paper_only": True}
    cards = data.get("cards")
    if not isinstance(cards, dict):
        cards = {}
    data["cards"] = cards
    data.setdefault("side_key", FOMO_TREND_NO_HUNT_KEY)
    data.setdefault("paper_only", True)
    return data


def _save_fomo_no_hunt_state(session: Session, payload: dict[str, Any], *, now: datetime) -> None:
    raw = json.dumps(payload)
    row = session.query(ScanState).filter(ScanState.key == FOMO_NO_HUNT_STATE_KEY).one_or_none()
    if row is None:
        session.add(ScanState(key=FOMO_NO_HUNT_STATE_KEY, value=raw, updated_at=now))
    else:
        row.value = raw
        row.updated_at = now
    session.flush()


def _stamp_entry_fomo_no_hunt(
    session: Session,
    token: Token | None,
    *,
    why: str,
    snap: dict[str, Any],
    now: datetime,
) -> bool:
    """Write-once Decision side keys. Returns True when a new stamp landed."""
    if token is None or not token.id:
        return False
    entry = (
        session.query(Decision)
        .filter(Decision.token_id == int(token.id), Decision.kind == "entry")
        .order_by(Decision.id.desc())
        .first()
    )
    if entry is None:
        return False
    feat = _feat(entry.features_json)
    merged = stamp_fomo_trend_no_hunt(feat, why=why, snap=snap, at=now)
    if merged is None:
        return False
    from ..ledger import features_hash

    entry.features_json = json.dumps(merged)
    entry.features_hash = features_hash(entry.features_json)
    session.flush()
    return True


def capture_fomo_trend_no_hunt(
    session: Session,
    items: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Stamp write-once Learn cards from a FOMO trending coverage pass.

    Shadow / Learn only. Does not open paperV1. Does not ingest.
    """
    now = _aware(now) or utcnow()
    state = load_fomo_no_hunt_state(session)
    cards: dict[str, Any] = dict(state.get("cards") or {})
    n_new = 0
    n_stamped_decision = 0
    live_items: list[dict[str, Any]] = []
    by_why: Counter[str] = Counter()

    for raw in items or []:
        if not is_fomo_no_hunt_item(raw):
            continue
        chain = normalize_chain(raw.get("chain") or "sol")
        mint = normalize_mint(str(raw.get("mint") or ""), chain)
        if not mint:
            continue
        token = (
            session.query(Token)
            .filter(Token.mint == mint)
            .one_or_none()
        )
        if token is not None and normalize_chain(token.chain or "") != chain:
            token = None
        why = why_not_hunt(raw, token=token, now=now)
        if not why:
            continue
        why = resolve_fomo_no_hunt_why(why, item=raw, token=token, now=now)
        entry = None
        if token is not None:
            entry = (
                session.query(Decision)
                .filter(Decision.token_id == token.id, Decision.kind == "entry")
                .order_by(Decision.id.desc())
                .first()
            )
        snap = freeze_fomo_no_hunt_snap(
            raw, token=token, decision=entry, why=why, now=now
        )
        key = f"{chain}:{mint}"
        existing = cards.get(key)
        if not isinstance(existing, dict):
            card = {
                "mint": mint,
                "chain": chain,
                "symbol": (token.symbol if token is not None else None) or raw.get("symbol") or "",
                "why": why,
                "at": now.isoformat(),
                "early": snap,
                "side_key": FOMO_TREND_NO_HUNT_KEY,
                "paper_only": True,
                "open": FOMO_NO_HUNT_OPEN,
            }
            cards[key] = card
            n_new += 1
            existing = card
        else:
            stored_why = str(existing.get("why") or "")
            resolved = resolve_fomo_no_hunt_why(
                stored_why, item=raw, token=token, now=now
            )
            if stored_why == WHY_HISTORICAL and resolved != WHY_HISTORICAL:
                existing["why"] = resolved
                early = existing.get("early")
                if isinstance(early, dict):
                    early["why"] = resolved
                    early["historical"] = True
        # Later-run fields may update; early facts stay frozen.
        outcome = token.outcome if token is not None else None
        t0 = float(existing.get("early", {}).get("t0_mcap") or snap.get("t0_mcap") or 0.0)
        peak = float((outcome.max_mcap if outcome is not None else 0.0) or 0.0)
        last_liq = float((outcome.last_liq if outcome is not None else 0.0) or 0.0)
        if peak > 0 and t0 > 0:
            mult = paper_miss_multiple(t0_mcap=t0, peak_mcap=peak, last_liq=last_liq)
            existing["later_multiple"] = round(mult, 2)
            existing["peak_mcap"] = round(peak)
            existing["ran"] = bool(mult >= PAPER_MISS_WIN_MULT)
        if token is not None and _stamp_entry_fomo_no_hunt(
            session, token, why=str(existing.get("why") or why), snap=dict(existing.get("early") or snap), now=now
        ):
            n_stamped_decision += 1
        by_why[str(existing.get("why") or why)] += 1
        live_items.append(
            {
                "mint": mint,
                "chain": chain,
                "symbol": existing.get("symbol") or "",
                "why": existing.get("why") or why,
                "on_desk": bool(raw.get("on_desk")),
                "on_hunt": False,
                "status": str(raw.get("status") or ""),
                "first_seen": (existing.get("early") or {}).get("first_seen"),
                "mcap": (existing.get("early") or {}).get("mcap"),
                "gate_veto": (existing.get("early") or {}).get("gate_veto") or raw.get("gate_veto") or "",
                "paper": (existing.get("early") or {}).get("paper") or raw.get("paper") or {},
                "ran": bool(existing.get("ran")),
                "later_multiple": existing.get("later_multiple"),
                "peak_mcap": existing.get("peak_mcap"),
                "early": dict(existing.get("early") or snap),
                "side_key": FOMO_TREND_NO_HUNT_KEY,
            }
        )

    state["cards"] = cards
    state["updated_at"] = now.isoformat()
    state["side_key"] = FOMO_TREND_NO_HUNT_KEY
    state["paper_only"] = True
    state["open"] = FOMO_NO_HUNT_OPEN
    _save_fomo_no_hunt_state(session, state, now=now)
    return fomo_no_hunt_summary(
        cards,
        live_items=live_items,
        n_new=n_new,
        n_stamped_decision=n_stamped_decision,
        by_why=dict(by_why),
    )


def fomo_no_hunt_summary(
    cards: dict[str, Any],
    *,
    live_items: list[dict[str, Any]] | None = None,
    n_new: int = 0,
    n_stamped_decision: int = 0,
    by_why: dict[str, int] | None = None,
    autopsy_limit: int = FOMO_NO_HUNT_AUTOPSY_LIMIT,
) -> dict[str, Any]:
    rows = live_items if live_items is not None else []
    if not rows:
        for card in cards.values():
            if not isinstance(card, dict):
                continue
            early = card.get("early") if isinstance(card.get("early"), dict) else {}
            rows.append(
                {
                    "mint": card.get("mint"),
                    "chain": card.get("chain"),
                    "symbol": card.get("symbol") or "",
                    "why": card.get("why") or early.get("why") or "",
                    "first_seen": early.get("first_seen"),
                    "mcap": early.get("mcap"),
                    "gate_veto": early.get("gate_veto") or "",
                    "paper": early.get("paper") or {},
                    "ran": bool(card.get("ran")),
                    "later_multiple": card.get("later_multiple"),
                    "peak_mcap": card.get("peak_mcap"),
                    "early": early,
                    "side_key": FOMO_TREND_NO_HUNT_KEY,
                }
            )
    reasons = by_why if by_why is not None else dict(Counter(str(r.get("why") or "") for r in rows if r.get("why")))
    autopsies = [
        fomo_no_hunt_autopsy(
            mint=str(r.get("mint") or ""),
            symbol=str(r.get("symbol") or ""),
            why=str(r.get("why") or ""),
            chain=str(r.get("chain") or "sol"),
            t0_mcap=float((r.get("early") or {}).get("t0_mcap") or 0.0) if isinstance(r.get("early"), dict) else 0.0,
            peak_mcap=float(r.get("peak_mcap") or 0.0),
            multiple=r.get("later_multiple"),
            early=r.get("early") if isinstance(r.get("early"), dict) else {
                "first_seen": r.get("first_seen"),
                "mcap": r.get("mcap"),
                "gate_veto": r.get("gate_veto") or "",
                "paper": r.get("paper") or {},
                "why": r.get("why") or "",
            },
            extra={"ran": bool(r.get("ran"))},
        )
        for r in rows[: int(autopsy_limit)]
        if r.get("mint")
    ]
    n_door = int(reasons.get(WHY_DOOR_MISS) or 0)
    n_desk = sum(int(v) for k, v in reasons.items() if k != WHY_DOOR_MISS)
    winners, duds, n_ran_sep, n_dud = _split_runner_dud_snaps(
        [
            {
                **(r if isinstance(r, dict) else {}),
                "early": r.get("early") if isinstance((r or {}).get("early"), dict) else {
                    "first_seen": (r or {}).get("first_seen"),
                    "mcap": (r or {}).get("mcap"),
                    "gate_veto": (r or {}).get("gate_veto") or "",
                    "paper": (r or {}).get("paper") or {},
                    "why": (r or {}).get("why") or "",
                    "holders": (r or {}).get("holders"),
                    "liq": (r or {}).get("liq"),
                    "entry_p": (r or {}).get("entry_p"),
                    "age_hours": (r or {}).get("age_hours"),
                    "vol_h1": (r or {}).get("vol_h1"),
                },
            }
            for r in rows
            if isinstance(r, dict)
        ]
    )
    n_ran = sum(1 for r in rows if r.get("ran")) or n_ran_sep
    limit = max(int(autopsy_limit), FOMO_NO_HUNT_AUTOPSY_LIMIT)
    return {
        "paper_only": True,
        "open": FOMO_NO_HUNT_OPEN,
        "side_key": FOMO_TREND_NO_HUNT_KEY,
        "n": len(rows),
        "n_joined": len(cards),
        "n_total": len(cards),
        "n_new": n_new,
        "n_stamped_decision": n_stamped_decision,
        "n_desk_no_hunt": n_desk,
        "n_door_miss": n_door,
        "n_ran": n_ran,
        "n_runners": n_ran,
        "n_duds": n_dud,
        "by_why": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "separators": _fomo_separator_rows(winners, duds)[:8],
        "items": rows[: max(40, limit)],
        "autopsies": autopsies[:limit],
        "autopsy_limit": limit,
        "note": (
            "FOMO trending names that are on the desk but not on Hunt "
            "(plus true door misses). Write-once fomo_trend_no_hunt side "
            "keys. Actionable why beats blanket historical. Runner-vs-dud "
            "separators prefer holders/vol/book. Learn / shadow only — "
            "does not open paperV1. Auto-repair already owns ingest miss."
        ),
    }


def annotate_fomo_items(items: list[dict[str, Any]] | None, learn: dict[str, Any] | None) -> None:
    """Attach why_not_hunt onto coverage rows (in place)."""
    by_mint = {
        str(r.get("mint") or ""): r
        for r in (learn or {}).get("items") or []
        if r.get("mint")
    }
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("on_hunt"):
            item.setdefault("why_not_hunt", "")
            continue
        hit = by_mint.get(str(item.get("mint") or ""))
        if hit:
            item["why_not_hunt"] = hit.get("why") or ""
            item["fomo_trend_no_hunt"] = True
        elif is_fomo_no_hunt_item(item):
            item["why_not_hunt"] = why_not_hunt(item)
            item["fomo_trend_no_hunt"] = True


def fomo_trend_no_hunt_learn(
    session: Session,
    chain: str = "sol",
    *,
    days: int = 14,
    now: datetime | None = None,
    autopsy_limit: int = FOMO_NO_HUNT_AUTOPSY_LIMIT,
) -> dict[str, Any]:
    """Durable Learn cohort from ScanState + Decision stamps.

    When a stamped name later prints ≥5×, it is a runner for the same
    miss loop (cross-link, not a paperV1 open).
    """
    chain = normalize_chain(chain)
    now = _aware(now) or utcnow()
    since = now - timedelta(days=max(1, int(days)))
    state = load_fomo_no_hunt_state(session)
    cards = state.get("cards") or {}
    by_why: Counter[str] = Counter()
    autopsies: list[dict[str, Any]] = []
    runners: list[dict[str, Any]] = []
    winners_feat: list[dict[str, Any]] = []
    duds_feat: list[dict[str, Any]] = []
    n_joined = 0
    n_ran = 0
    n_dud = 0
    n_total = sum(
        1
        for card in cards.values()
        if isinstance(card, dict) and normalize_chain(card.get("chain") or "sol") == chain
    )
    limit = max(int(autopsy_limit), FOMO_NO_HUNT_AUTOPSY_LIMIT)
    seen_mints: set[str] = set()
    repaired = False
    for key, card in cards.items():
        if not isinstance(card, dict):
            continue
        if normalize_chain(card.get("chain") or "sol") != chain:
            continue
        at = _aware(None)
        try:
            raw_at = card.get("at")
            if raw_at:
                at = datetime.fromisoformat(str(raw_at).replace("Z", "+00:00"))
                at = _aware(at)
        except ValueError:
            at = None
        if at is not None and at < since:
            continue
        n_joined += 1
        mint = str(card.get("mint") or "")
        token = session.query(Token).filter(Token.mint == mint).one_or_none() if mint else None
        outcome = token.outcome if token is not None else None
        early = card.get("early") if isinstance(card.get("early"), dict) else {}
        why = resolve_fomo_no_hunt_why(
            str(card.get("why") or early.get("why") or ""),
            item=item_from_fomo_card(card, early),
            token=token,
            now=now,
        )
        if why:
            by_why[why] += 1
        if str(card.get("why") or "") == WHY_HISTORICAL and why != WHY_HISTORICAL:
            card["why"] = why
            if early:
                early["why"] = why
                early["historical"] = True
            repaired = True
        t0 = float(early.get("t0_mcap") or (outcome.t0_mcap if outcome is not None else 0.0) or 0.0)
        peak = float(
            card.get("peak_mcap")
            or (outcome.max_mcap if outcome is not None else 0.0)
            or 0.0
        )
        last_liq = float((outcome.last_liq if outcome is not None else 0.0) or 0.0)
        mult = paper_miss_multiple(t0_mcap=t0, peak_mcap=peak, last_liq=last_liq) if t0 and peak else float(card.get("later_multiple") or 0.0)
        ran = bool(mult >= PAPER_MISS_WIN_MULT or card.get("ran"))
        snap_sep = _early_for_sep({"early": early, "entry_p": early.get("entry_p"), "holders": early.get("holders"), "liq": early.get("liq")})
        if ran:
            n_ran += 1
            winners_feat.append(snap_sep)
        else:
            n_dud += 1
            duds_feat.append(snap_sep)
        autopsy = fomo_no_hunt_autopsy(
            mint=mint,
            symbol=str(card.get("symbol") or (token.symbol if token is not None else "") or ""),
            why=why,
            chain=chain,
            t0_mcap=t0,
            peak_mcap=peak,
            multiple=mult,
            early=early,
            extra={
                "ran": ran,
                "paper_miss_join": False,
                "at": card.get("at"),
            },
        )
        if token is not None:
            entry = (
                session.query(Decision)
                .filter(Decision.token_id == token.id, Decision.kind == "entry")
                .order_by(Decision.id.desc())
                .first()
            )
            feat = _feat(entry.features_json if entry is not None else None)
            if has_fomo_trend_no_hunt(feat) or feat.get("paper_miss_join"):
                autopsy["paper_miss_join"] = bool(feat.get("paper_miss_join"))
                autopsy["decision_stamped"] = has_fomo_trend_no_hunt(feat)
        if ran:
            runners.append(autopsy)
        if len(autopsies) < limit:
            autopsies.append(autopsy)
        if mint:
            seen_mints.add(mint)

    # Decision-only stamps (ScanState miss) still join.
    decisions = (
        session.query(Decision, Token, Outcome)
        .join(Token, Token.id == Decision.token_id)
        .outerjoin(Outcome, Outcome.token_id == Token.id)
        .filter(
            Decision.chain == chain,
            Decision.kind == "entry",
            Decision.at >= since,
        )
        .order_by(Decision.id.desc())
        .limit(800)
        .all()
    )
    for decision, token, outcome in decisions:
        feat = _feat(decision.features_json)
        if not has_fomo_trend_no_hunt(feat):
            continue
        if token.mint in seen_mints:
            continue
        snap = feat.get(FOMO_TREND_NO_HUNT_SNAP_KEY)
        snap = snap if isinstance(snap, dict) else {}
        why = resolve_fomo_no_hunt_why(
            str(feat.get(FOMO_TREND_NO_HUNT_WHY_KEY) or snap.get("why") or ""),
            item=item_from_fomo_card({"chain": chain, "early": snap}, snap),
            token=token,
            now=now,
        )
        t0 = float(snap.get("t0_mcap") or decision.entry_mcap or (outcome.t0_mcap if outcome else 0.0) or 0.0)
        peak = float((outcome.max_mcap if outcome else 0.0) or 0.0)
        last_liq = float((outcome.last_liq if outcome else 0.0) or 0.0)
        mult = paper_miss_multiple(t0_mcap=t0, peak_mcap=peak, last_liq=last_liq)
        ran = bool(mult >= PAPER_MISS_WIN_MULT)
        n_joined += 1
        if why:
            by_why[why] += 1
        snap_sep = _early_for_sep({"early": snap})
        if ran:
            n_ran += 1
            winners_feat.append(snap_sep)
        else:
            n_dud += 1
            duds_feat.append(snap_sep)
        autopsy = fomo_no_hunt_autopsy(
            mint=token.mint,
            symbol=token.symbol or "",
            why=why,
            chain=chain,
            t0_mcap=t0,
            peak_mcap=peak,
            multiple=mult,
            early=snap,
            extra={
                "ran": ran,
                "paper_miss_join": bool(feat.get("paper_miss_join")),
                "decision_stamped": True,
            },
        )
        if ran:
            runners.append(autopsy)
        if len(autopsies) < limit:
            autopsies.append(autopsy)
        seen_mints.add(token.mint)

    if repaired:
        state["cards"] = cards
        state["updated_at"] = now.isoformat()
        _save_fomo_no_hunt_state(session, state, now=now)

    return {
        "paper_only": True,
        "open": FOMO_NO_HUNT_OPEN,
        "side_key": FOMO_TREND_NO_HUNT_KEY,
        "chain": chain,
        "days": int(days),
        "win_multiple": PAPER_MISS_WIN_MULT,
        "n_joined": n_joined,
        "n_total": n_total,
        "n_runners": n_ran,
        "n_duds": n_dud,
        "n_desk_no_hunt": n_joined - int(by_why.get(WHY_DOOR_MISS) or 0),
        "n_door_miss": int(by_why.get(WHY_DOOR_MISS) or 0),
        "by_why": dict(sorted(by_why.items(), key=lambda kv: -kv[1])),
        "separators": _fomo_separator_rows(winners_feat, duds_feat)[:8],
        "autopsies": autopsies,
        "runners": runners[:limit],
        "autopsy_limit": limit,
        "note": (
            "FOMO trending → not on Hunt. Write-once fomo_trend_no_hunt. "
            "Actionable why beats blanket historical. Runner-vs-dud "
            "separators prefer holders/vol/book over social. Names that "
            "later print ≥5× cross-link the paper-miss / runners loop. "
            "Does not open paperV1. Not a buy list."
        ),
    }


def has_hunt_card(session: Session, chain: str, mint: str) -> bool:
    chain = normalize_chain(chain)
    mint = normalize_mint(mint, chain)
    if not mint:
        return False
    return (
        session.query(HuntCard.id)
        .filter(HuntCard.chain == chain, HuntCard.mint == mint)
        .first()
        is not None
    )
