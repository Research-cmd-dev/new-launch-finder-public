"""paperV1: the daily short list — the paper buy surface.

The wide gate still fills every name that passes (training ocean). This
list keeps independent Sol and RH lanes (3/day each, 6 total) — the action
surface for the 1–5/day goal. Paper only; no ticket, no auto-buy.

Qualify:
  - Score path: Solana buy line + Live >= 0.50; Robinhood Entry >= 0.40
  - Thesis path: softer floors only when frozen thesis clears with a
    *hard* tag (GitHub / real project / CTO). Meme-alone does not soft-pass.

Ranking prefers cohort signal (buy pressure / holders / liq / creator /
early wallets), then Live/Entry, then hard thesis. Immediate open before
lock: Live >= 0.70, a strong thesis, or a queued sellable peak >= 2×.
Book locks at 23:00 UTC.
Entry p_good is not rewritten. FEATURE_NAMES stays 66.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

PAPER_V1_LINE = "paper_v1"
# Independent lanes so RH cannot silence Sol under a shared day cap.
PAPER_V1_CAP_PER_CHAIN = 3
PAPER_V1_CAP = PAPER_V1_CAP_PER_CHAIN * 2  # total display (3+3)
PAPER_V1_LOCK_HOUR = 23
PAPER_V1_SOL_LIVE = 0.50
PAPER_V1_RH_ENTRY = 0.40
PAPER_V1_NOW_LIVE = 0.70
# Midday promote: queued name already printed a sellable 2× from entry.
PAPER_V1_PROMOTE_MULT = 2.0
PAPER_V1_OPEN_VIA_IMMEDIATE = "immediate"
PAPER_V1_OPEN_VIA_PEAK = "peak"
# Thin-facts score-path queue — lock/promote still require a hard tag.
PAPER_V1_OPEN_VIA_THIN = "thin"
# Soft floors when frozen thesis clears the gate (hard tags only).
PAPER_V1_SOL_LIVE_THESIS = 0.40
PAPER_V1_RH_ENTRY_THESIS = 0.30
PAPER_V1_THESIS_MIN = 0.25
PAPER_V1_THESIS_STRONG = 0.70  # needs multi-tag strength (e.g. github+dev+cto)
PAPER_V1_QUEUED = "queued"
PAPER_V1_SKIPPED = "skipped"
PAPER_V1_SHADOW_LINE = "paper_v1_shadow"
# Skip / near-miss taxonomy for Learn review (honest daily labels).
PAPER_V1_SKIP_CAP = "v1 cap"
PAPER_V1_SKIP_NO_THESIS = "v1 no-thesis"
PAPER_V1_SKIP_SCORE_MISS = "v1 score-miss"
PAPER_V1_SKIP_VETO = "v1 veto"
PAPER_V1_SKIP_LEFTOVER = "v1 leftover"
# Reconsider window ended and Live never reached the floor (Sol was silent
# because qualify ran once, at the first liquid print, when Live is cold).
PAPER_V1_SKIP_LIVE_COLD = "v1 live-cold"
# Reconsider found Live warm but the print already ran >= PAPER_CHASE_MULT × t0
# without a quality bypass.
PAPER_V1_SKIP_LATE_CHASE = "v1 late-chase"
# Learn label when a quality name opened despite chase (not a skip).
PAPER_V1_LATE_OK = "v1 late-ok"
# First-seen mcap at or above this is not a zero-hour short-list name.
PAPER_V1_LEFTOVER_MCAP = 1_000_000.0
# Reconsider-on-paper-sync (cycle 4 knob): which chains and for how long
# after the wide fill a Live-cold name is re-evaluated for the short list.
# RH qualifies on Entry, not Live, so RH is not in the list.
PAPER_V1_RECONSIDER_CHAINS = frozenset({"sol"})
PAPER_V1_RECONSIDER_HOURS = 3.0
# Learn labels for hard copycat-veto runners (SI cohort) — shadow only, never opens.
PAPER_V1_COPYCAT_VETO = "copycat spam"
# Stamped on PaperFill.exit_reason (varchar 32) — not the gate veto string.
PAPER_V1_COPYCAT_VETO_STAMP = "copycat"
# FOMO-board / desk-flag copycat (no gate Decision). Same hard family; Learn carve.
PAPER_V1_COPYCAT_FOMO_STAMP = "cc-fomo"
PAPER_V1_COPYCAT_VETO_LOOKBACK_DAYS = 14
PAPER_V1_EXIT_REASON_MAX = 32
# Research risk_flags_json needle (FEATURE_NAMES copycat flood).
PAPER_V1_COPYCAT_RISK_NEEDLE = "copycat spam"
# Would-have early-book label (stack-v204). Side key + Learn table; never opens.
PAPER_V1_SKIP_EARLY_BOOK = "v1 early-book"
# Reserved when a single shared pool is still used (legacy choose path).
PAPER_V1_MIN_PER_CHAIN = 1
# Hard thesis tags — meme alone does not open the soft path (miss-cohort
# showed meme/CTO rates do not lift winner precision).
THESIS_HARD_TAGS = frozenset({"github", "dev", "cto"})
# Side keys stamped into Decision.features_json (not FEATURE_NAMES).
LIVE_AT_ENTRY_KEY = "live_p_at_entry"
LIVE_AT_ENTRY_SRC_KEY = "live_at_entry_src"
LIVE_AT_ENTRY_AT_KEY = "live_at_entry_at"


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def v1_day(ts: datetime) -> str:
    return _utc(ts).date().isoformat()


def v1_thesis_from_features(
    features: dict[str, Any] | None,
    *,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Score frozen entry features for short-list ranking. Paper only.

    Uses existing FEATURE_NAMES columns — does not grow the 66-vector.
    Optional ``weights`` come from ``thesis_weights`` refit (ScanState).
    """
    from .thesis_weights import DEFAULT_WEIGHTS, normalize_weights

    feat = features if isinstance(features, dict) else {}
    w = normalize_weights(weights) if weights is not None else dict(DEFAULT_WEIGHTS)
    auth = float(feat.get("github_auth_n") or 0.0)
    real = float(feat.get("real_project") or 0.0)
    cto = float(feat.get("gmgn_cto") or 0.0)
    name_q = float(feat.get("name_quality") or 0.0)
    score = (
        float(w["github_auth_n"]) * auth
        + float(w["real_project"]) * max(0.0, real)
        + float(w["gmgn_cto"]) * cto
        + float(w["name_quality"]) * max(0.0, name_q)
    )
    tags: list[str] = []
    if auth >= 0.6:
        tags.append("github")
    if real >= 0.5:
        tags.append("dev")
    if cto >= 0.5:
        tags.append("cto")
    if name_q >= 0.6:
        tags.append("meme")
    hard = [t for t in tags if t in THESIS_HARD_TAGS]
    return {
        "score": round(score, 4),
        "github_auth": round(auth, 4),
        "real_project": round(real, 4),
        "cto": round(cto, 4),
        "name_quality": round(name_q, 4),
        "tags": tags,
        "hard_tags": hard,
        "weights": w,
    }


THESIS_COVERAGE_DEF = "hard tag (github/dev/cto) on opens (picked + closed)"


def v1_row_has_hard_tag(row: dict[str, Any] | None) -> bool:
    """True when a review row / thesis dict carries a github / dev / cto tag."""
    bits = row if isinstance(row, dict) else {}
    thesis = bits.get("thesis") if isinstance(bits.get("thesis"), dict) else {}
    tags = set(map(str, bits.get("hard_tags") or thesis.get("hard_tags") or []))
    tags |= set(map(str, bits.get("tags") or thesis.get("tags") or []))
    return bool(tags & THESIS_HARD_TAGS)


def v1_thesis_coverage(opens: list[dict[str, Any]]) -> dict[str, Any]:
    """The one production-gate coverage number: hard tag on opens.

    FORWARD bars thesis coverage at 0.80 on the names the desk actually
    took (picked + closed), not on queued / skipped / shadow labels and
    not on `meme` alone — meme stopped opening the soft path in v124.
    ``any_tag`` rides along as a labelled secondary, never as the gate.
    """
    n = len(opens)
    hard = sum(1 for r in opens if v1_row_has_hard_tag(r))
    anyt = sum(1 for r in opens if (r.get("tags") or ((r.get("thesis") or {}) if isinstance(r.get("thesis"), dict) else {}).get("tags")))
    return {
        "coverage": round(hard / n, 4) if n else None,
        "hard_tag_n": hard,
        "any_tag_coverage": round(anyt / n, 4) if n else None,
        "n": n,
        "definition": THESIS_COVERAGE_DEF,
    }


def v1_thesis_ok(thesis: dict[str, Any] | None) -> bool:
    """True when frozen thesis clears the soft short-list gate.

    Requires score floor plus at least one hard tag (github/dev/cto).
    Meme-alone is not enough — miss-cohort showed no precision lift.
    """
    bits = thesis if isinstance(thesis, dict) else {}
    hard = bits.get("hard_tags")
    if hard is None:
        tags = bits.get("tags") or []
        hard = [t for t in tags if t in THESIS_HARD_TAGS]
    return float(bits.get("score") or 0.0) >= PAPER_V1_THESIS_MIN and bool(hard)


def v1_thesis_strong(thesis: dict[str, Any] | None) -> bool:
    bits = thesis if isinstance(thesis, dict) else {}
    hard = bits.get("hard_tags")
    if hard is None:
        tags = bits.get("tags") or []
        hard = [t for t in tags if t in THESIS_HARD_TAGS]
    return float(bits.get("score") or 0.0) >= PAPER_V1_THESIS_STRONG and bool(hard)


def v1_leftover_reject(entry_mcap: float | None) -> bool:
    """True when first-seen mcap is too fat for a zero-hour short-list pick."""
    return float(entry_mcap or 0.0) >= PAPER_V1_LEFTOVER_MCAP


def v1_score_path(
    chain: str,
    entry_p: float,
    live_p: float | None,
    *,
    hi: float,
) -> bool:
    """Documented score path — buy line + Live, no thesis wait.

    Used for a provisional paperV1 queue on migrate + Dex facts. Soft
    thesis floors still need a hard tag. Lock / midday promote still
    require ``v1_hard_tag_ready``.
    """
    from ..chains import normalize_chain

    entry = float(entry_p or 0.0)
    live = float(live_p or 0.0)
    if normalize_chain(chain) == "robinhood":
        return entry >= PAPER_V1_RH_ENTRY
    return entry >= float(hi) and live >= PAPER_V1_SOL_LIVE


def v1_qualifies(
    chain: str,
    entry_p: float,
    live_p: float | None,
    *,
    hi: float,
    thesis: dict[str, Any] | None = None,
) -> bool:
    from ..chains import normalize_chain

    entry = float(entry_p or 0.0)
    live = float(live_p or 0.0)
    if not v1_thesis_ok(thesis):
        return False
    if normalize_chain(chain) == "robinhood":
        if entry >= PAPER_V1_RH_ENTRY:
            return True
        return entry >= PAPER_V1_RH_ENTRY_THESIS
    if entry >= float(hi) and live >= PAPER_V1_SOL_LIVE:
        return True
    return entry >= float(hi) * 0.75 and live >= PAPER_V1_SOL_LIVE_THESIS


def v1_immediate(live_p: float | None, thesis: dict[str, Any] | None = None) -> bool:
    if v1_thesis_strong(thesis):
        return True
    if float(live_p or 0.0) >= PAPER_V1_NOW_LIVE and v1_thesis_ok(thesis):
        return True
    return False


def v1_peak_ok(entry_mcap: float | None, max_mcap: float | None, *, mult: float = PAPER_V1_PROMOTE_MULT) -> bool:
    """True when the queued fill already printed a sellable peak ≥ mult× entry."""
    entry = float(entry_mcap or 0.0)
    peak = float(max_mcap or 0.0)
    if entry <= 0 or peak <= 0:
        return False
    return peak >= float(mult) * entry


def v1_peak_multiple(entry_mcap: float | None, max_mcap: float | None) -> float:
    entry = float(entry_mcap or 0.0)
    peak = float(max_mcap or 0.0)
    if entry <= 0 or peak <= 0:
        return 0.0
    return peak / entry


def v1_peak_promoted_open(fill: Any) -> bool:
    """Midday peak promote — not an organic skill open (null via = legacy organic)."""
    return (getattr(fill, "open_via", None) or "") == PAPER_V1_OPEN_VIA_PEAK


def apply_v1_peak_promote_entry(fill: Any) -> None:
    """Re-base entry at the promote print so forward hit2× is honest."""
    promote_mcap = float(getattr(fill, "last_mcap", 0.0) or 0.0)
    if promote_mcap <= 0:
        promote_mcap = float(getattr(fill, "max_mcap", 0.0) or 0.0)
    if promote_mcap <= 0:
        return
    fill.entry_mcap = promote_mcap
    fill.max_mcap = promote_mcap
    fill.min_mcap = promote_mcap


def v1_near_qualify(
    chain: str,
    entry_p: float,
    live_p: float | None,
    *,
    hi: float,
    thesis: dict[str, Any] | None = None,
) -> bool:
    """Almost cleared the short list — useful negative label, not a pick."""
    from ..chains import normalize_chain

    if v1_qualifies(chain, entry_p, live_p, hi=hi, thesis=thesis):
        return False
    entry = float(entry_p or 0.0)
    live = float(live_p or 0.0)
    thesis_ok = v1_thesis_ok(thesis)
    if normalize_chain(chain) == "robinhood":
        # Within 0.05 of soft or hard Entry floors.
        return entry >= PAPER_V1_RH_ENTRY_THESIS - 0.05 or (
            thesis_ok and entry >= PAPER_V1_RH_ENTRY_THESIS - 0.08
        )
    # Sol: near buy-line + Live band, or thesis soft band.
    return (
        (entry >= float(hi) * 0.85 and live >= PAPER_V1_SOL_LIVE - 0.10)
        or (entry >= float(hi) * 0.70 and live >= PAPER_V1_SOL_LIVE_THESIS - 0.08)
        or (thesis_ok and entry >= float(hi) * 0.65 and live >= PAPER_V1_SOL_LIVE_THESIS - 0.12)
    )


def v1_miss_reason(
    chain: str,
    entry_p: float,
    live_p: float | None,
    *,
    hi: float,
    thesis: dict[str, Any] | None = None,
) -> str:
    """Why a near-miss did not enter the short list."""
    bits = thesis if isinstance(thesis, dict) else {}
    if not v1_thesis_ok(bits):
        # Would have needed thesis soft path (or clearer thesis) to get in.
        from ..chains import normalize_chain

        entry = float(entry_p or 0.0)
        live = float(live_p or 0.0)
        if normalize_chain(chain) == "robinhood":
            if entry < PAPER_V1_RH_ENTRY:
                return PAPER_V1_SKIP_NO_THESIS
        elif entry < float(hi) or live < PAPER_V1_SOL_LIVE:
            if entry >= float(hi) * 0.70 or live >= PAPER_V1_SOL_LIVE_THESIS - 0.05:
                return PAPER_V1_SKIP_NO_THESIS
    return PAPER_V1_SKIP_SCORE_MISS


def v1_skip_stamp(reason: str, day: str) -> str:
    """Skip reason + book day so review can bucket the UTC day."""
    base = (reason or PAPER_V1_SKIP_CAP).strip() or PAPER_V1_SKIP_CAP
    return f"{base}|{v1_day_reason(day)}"


def research_risk_flags_copycat_spam(risk_flags_json: str | None) -> bool:
    """True when Research flags carry the hard copycat spam needle."""
    try:
        flags = json.loads((risk_flags_json or "[]") or "[]")
    except Exception:
        return False
    if not isinstance(flags, list):
        return False
    blob = " | ".join(str(flag).lower() for flag in flags)
    return PAPER_V1_COPYCAT_RISK_NEEDLE in blob


def v1_veto_skip_stamp(veto_detail: str, day: str) -> str:
    """Hard gate veto shadow skip — fits ``PaperFill.exit_reason`` (32 chars).

    Example: ``v1 veto|copycat|d:2026-09-30`` (gate veto stays ``copycat spam``).
    """
    raw = (veto_detail or "").strip() or PAPER_V1_COPYCAT_VETO
    detail = PAPER_V1_COPYCAT_VETO_STAMP if raw == PAPER_V1_COPYCAT_VETO else raw
    stamp = f"{PAPER_V1_SKIP_VETO}|{detail}|{v1_day_reason(day)}"
    if len(stamp) > PAPER_V1_EXIT_REASON_MAX:
        budget = PAPER_V1_EXIT_REASON_MAX - len(f"{PAPER_V1_SKIP_VETO}||{v1_day_reason(day)}")
        detail = detail[: max(0, budget)]
        stamp = f"{PAPER_V1_SKIP_VETO}|{detail}|{v1_day_reason(day)}"
    if len(stamp) > PAPER_V1_EXIT_REASON_MAX:
        raise ValueError(f"paperV1 veto skip stamp too long ({len(stamp)}): {stamp!r}")
    return stamp


def v1_strength(chain: str, entry_p: float, live_p: float | None) -> tuple[float, float]:
    """How far this name sits above its own floor. Higher sorts first."""
    from ..chains import normalize_chain

    entry = float(entry_p or 0.0)
    live = float(live_p or 0.0)
    if normalize_chain(chain) == "robinhood":
        return (entry - PAPER_V1_RH_ENTRY, live)
    return (live - PAPER_V1_SOL_LIVE, entry)


def choose_v1(
    rows: list[dict[str, Any]],
    *,
    already: int,
    cap: int = PAPER_V1_CAP_PER_CHAIN,
    policy: str = "signal",
    min_per_chain: int = 0,
) -> list[Any]:
    """Ids to take from today's queue. ``already`` slots are spent.

    Rank policy ``signal`` (default): cohort signal, then Live/Entry, then thesis.
    Policy ``live``: Live/Entry margin first, then signal/thesis.
    Policy ``thesis``: frozen thesis first (legacy A/B).
    When both Sol and RH are in the queue and ``min_per_chain`` > 0, reserve
    slots so one chain cannot silence the other (legacy shared-cap path).
    Per-chain caps normally call this once per chain with ``min_per_chain=0``.
    """
    room = max(0, int(cap) - int(already))
    if room <= 0 or not rows:
        return []
    live_first = policy == "live"
    thesis_first = policy == "thesis"

    def sort_key(row: dict[str, Any]) -> tuple[float, float, float, float, str]:
        thesis = float(row.get("thesis_score") or 0.0)
        if "thesis_score" not in row and isinstance(row.get("thesis"), dict):
            thesis = float(row["thesis"].get("score") or 0.0)
        signal = float(row.get("signal_score") or 0.0)
        if "signal_score" not in row and isinstance(row.get("signal"), dict):
            signal = float(row["signal"].get("score") or 0.0)
        primary, secondary = v1_strength(
            str(row.get("chain") or ""),
            float(row.get("entry_p") or 0.0),
            row.get("live_p"),
        )
        opened = row.get("opened_at")
        stamp = _utc(opened).isoformat() if isinstance(opened, datetime) else ""
        if live_first:
            return (-primary, -secondary, -signal, -thesis, stamp)
        if thesis_first:
            return (-thesis, -signal, -primary, -secondary, stamp)
        return (-signal, -primary, -secondary, -thesis, stamp)

    ordered = sorted(rows, key=sort_key)
    chains = {str(r.get("chain") or "") for r in ordered}
    both = "sol" in chains and "robinhood" in chains
    if not both or int(min_per_chain) <= 0 or room < 2:
        return [row.get("id") for row in ordered[:room]]

    # Reserve one (or min_per_chain) from each chain first, then fill by rank.
    picked: list[Any] = []
    seen: set[Any] = set()
    for chain_name in ("sol", "robinhood"):
        need = int(min_per_chain)
        for row in ordered:
            if len(picked) >= room or need <= 0:
                break
            if str(row.get("chain") or "") != chain_name:
                continue
            rid = row.get("id")
            if rid in seen:
                continue
            picked.append(rid)
            seen.add(rid)
            need -= 1
    for row in ordered:
        if len(picked) >= room:
            break
        rid = row.get("id")
        if rid in seen:
            continue
        picked.append(rid)
        seen.add(rid)
    return picked


def v1_queue_reason(live_p: float | None, day: str) -> str:
    """Qualify-time Live and the UTC book day, while the row is still queued."""
    return f"q:{float(live_p or 0.0):.4f}|{day}"


def v1_day_reason(day: str) -> str:
    """Book day stamped on an open paperV1 row."""
    return f"d:{day}"


def v1_close_reason(reason: str, day: str) -> str:
    """Close reason plus the book day, so the daily cap still counts."""
    return f"{reason}|{v1_day_reason(day)}"


def book_day_from_reason(reason: str) -> str:
    _live, day = v1_parse_queue(reason or "")
    if day:
        return day
    raw = reason or ""
    if "|d:" in raw:
        tail = raw.split("|d:", 1)[1][:10]
        if len(tail) == 10:
            return tail
    if raw.startswith("d:") and len(raw) >= 12:
        return raw[2:12]
    return ""


def close_reason_label(reason: str) -> str:
    """Strip the book-day suffix for display and scorecard buckets."""
    raw = reason or ""
    if "|d:" in raw:
        return raw.split("|d:", 1)[0]
    return raw


def v1_skip_family(reason: str | None) -> str:
    """Learn taxonomy family so copycat / SI labels do not drown signal misses.

    ``copycat`` — gate ``v1 veto|copycat`` or FOMO carve ``v1 veto|cc-fomo``.
    Hard veto stays hard; this is a display / denominator carve only.
    ``si_printer`` — high-SI FOMO shadow (never an un-veto).
    ``signal`` — live-cold / late-chase / score-miss / no-thesis / meme-q /
    early-book (the miss-cohort story).
    """
    label = close_reason_label(reason or "").strip().lower()
    if not label:
        return "other"
    if "copycat" in label or "cc-fomo" in label:
        return "copycat"
    if label.startswith("v1 si-pr"):
        return "si_printer"
    signal = (
        PAPER_V1_SKIP_LIVE_COLD,
        PAPER_V1_SKIP_LATE_CHASE,
        PAPER_V1_SKIP_SCORE_MISS,
        PAPER_V1_SKIP_NO_THESIS,
        PAPER_V1_SKIP_EARLY_BOOK,
        "v1 meme-q",
    )
    for prefix in signal:
        if label == prefix or label.startswith(prefix):
            return "signal"
    return "other"


def is_copycat_skip(reason: str | None) -> bool:
    return v1_skip_family(reason) == "copycat"


def v1_parse_queue(reason: str) -> tuple[float | None, str]:
    raw = reason or ""
    if not raw.startswith("q:") or "|" not in raw:
        return None, ""
    left, day = raw[2:].split("|", 1)
    try:
        live = float(left)
    except ValueError:
        return None, ""
    day = day.strip()[:10]
    if len(day) != 10:
        return None, ""
    return live, day


def next_v1_day(day: str) -> str:
    from datetime import date, timedelta

    year, month, dom = (int(part) for part in day.split("-"))
    return (date(year, month, dom) + timedelta(days=1)).isoformat()


def lock_due(day: str, now: datetime) -> bool:
    """True once that UTC date is at or past the lock hour."""
    now = _utc(now)
    if day < now.date().isoformat():
        return True
    if day > now.date().isoformat():
        return False
    return now.hour >= PAPER_V1_LOCK_HOUR


def v1_why(thesis: dict[str, Any] | None, *, entry_p: float, live_p: float | None, hi: float, chain: str) -> str:
    """One-line operator reason for picked / queued / skipped."""
    bits = thesis if isinstance(thesis, dict) else {}
    tags = ",".join(bits.get("hard_tags") or bits.get("tags") or []) or "no-hard-tags"
    ts = float(bits.get("score") or 0.0)
    path = "thesis" if v1_thesis_ok(bits) and not (
        (chain == "robinhood" and float(entry_p or 0) >= PAPER_V1_RH_ENTRY)
        or (chain != "robinhood" and float(entry_p or 0) >= float(hi) and float(live_p or 0) >= PAPER_V1_SOL_LIVE)
    ) else "score"
    live_s = "—" if live_p is None else f"{float(live_p):.2f}"
    return f"{path} · thesis {ts:.2f} [{tags}] · E {float(entry_p or 0):.2f} · L {live_s}"


def read_live_at_entry(features: dict[str, Any] | None) -> tuple[float | None, str]:
    """Frozen Live stamped on Decision.features_json (side keys, not FEATURE_NAMES)."""
    feat = features if isinstance(features, dict) else {}
    raw = feat.get(LIVE_AT_ENTRY_KEY)
    if raw is None:
        return None, ""
    try:
        live = float(raw)
    except (TypeError, ValueError):
        return None, ""
    if live <= 0:
        return None, str(feat.get(LIVE_AT_ENTRY_SRC_KEY) or "frozen")
    return live, str(feat.get(LIVE_AT_ENTRY_SRC_KEY) or "frozen")


def merge_live_at_entry(
    features: dict[str, Any] | None,
    live_p: float | None,
    *,
    src: str = "live_model",
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Return updated features dict if Live should be stamped; else None.

    Never overwrites an existing freeze. Side keys only — FEATURE_NAMES untouched.
    """
    feat = dict(features) if isinstance(features, dict) else {}
    if feat.get(LIVE_AT_ENTRY_KEY) is not None:
        return None
    if live_p is None:
        return None
    try:
        live = float(live_p)
    except (TypeError, ValueError):
        return None
    if live <= 0:
        return None
    feat[LIVE_AT_ENTRY_KEY] = round(live, 4)
    feat[LIVE_AT_ENTRY_SRC_KEY] = (src or "live_model")[:32]
    stamp = _utc(at or datetime.now(timezone.utc)).isoformat()
    feat[LIVE_AT_ENTRY_AT_KEY] = stamp
    return feat
