"""Paper fill gate: instant vetoes, no 15-minute wait.

Does not rewrite entry p_good. FEATURE_NAMES stays 66. Desk Paper
tab passes gated=1; classic leftover paper stays ungated.
"""

from __future__ import annotations

from typing import Iterable

from ..research.twitter import is_bought_aged_token_x, is_brand_x_account
from ..social import is_official_brand_website, is_staged_social_pair

PAPER_HOLD_T0 = 0.85
PAPER_CONFIRM_LIQ = 5_000.0
# v87 used 1.8× (Tilcayo filled at 63× t0). v202: a high floor for every
# paper fill so SAPLING-class ~2.2× is not a veto. Thin / unknown books
# still refuse past this. Quality names bypass the veto entirely.
PAPER_CHASE_MULT = 10.0
# Fresh-fat watch bypass and meme-quality "late" mark stay at the old
# first-book band — a 5× climber is not a new book.
PAPER_CHASE_FRESH_MULT = 1.8
# 0 = no ceiling: paper_chase_quality ok skips late-chase entirely.
PAPER_CHASE_QUALITY_MULT = 0.0
# Side keys on Decision/Research features_json — not FEATURE_NAMES.
PAPER_LATE_OK_KEY = "paper_late_ok"
PAPER_LATE_OK_WHY_KEY = "paper_late_ok_why"
PAPER_LATE_OK_MULT_KEY = "paper_chase_mult"
PAPER_PROVISIONAL_KEY = "paper_provisional"
PAPER_OPEN_VIA_LATE_OK = "late_ok"
PAPER_OPEN_VIA_THIN = "thin"
# PaperFill.exit_reason is varchar 32. Honest cancel after enrich hard-veto.
PAPER_ENRICH_VETO = "enrich veto"
# Fresh fat Sol watch can skip the Live gate (more tickets on new books).
PAPER_FRESH_LIQ = 8_000.0
PAPER_FRESH_HOLDERS = 26
# Sol matches Hunt 18h. RH paper stays 12h even though Hunt is 24h —
# a 13h DAM is watch, not a late paper fill. Desk Paper is the next
# 90+ that land, not a leftover replay (live CATCOIN / SANTA / Faucet).
PAPER_WINDOW_SOL = 18.0
PAPER_WINDOW_RH = 12.0
# Live SALARY: t0 scored 0.92 with $0 liq; first sellable book was +5m.
# Use that print as the fill. A book that only appears hours later is not
# a graduation fill. This is not a wait — we never hold for the clock.
PAPER_FIRST_BOOK_GRACE_MIN = 20.0
# Graves that never printed 1.5× cannot arm live dump. Close after 1h
# once last has given back half of peak (CATFLIX / fomocoin / Prism).
# Do not close a 10-minute wick. Do not close a still-alive 1.2× book.
# Runners (≥1.5×) stay on live dump. FEATURE_NAMES stays 66.
PAPER_NO_RUN_HOURS = 1.0
# Late books the live book refuses. A shadow fill records the moonbag
# they would have paid. It never opens a ticket and never joins gated90.
# Robinhood paper skips start-high / pre-pumped (those still shadow on
# Solana). Late chase stays a veto on both chains.
PAPER_SHADOW_VETOES = frozenset({"pre-pumped", "prepumped", "start-high", "late chase"})
# Needles Robinhood paper books. Solana still returns them. Not late chase.
PAPER_RH_SKIP_NEEDLES = frozenset({"start-high", "pre-pumped", "prepumped"})

PAPER_HARD_NEEDLES = (
    "honeypot",
    "wash trading",
    "hijack",
    "banned",
    "start-high",
    "pre-pumped",
    "prepumped",
    "copycat spam",
    "celebrity/brand",
    "prior rugs",
    "pasted tweet",
    "generated website",
)

PAPER_TAPE_DUMP_NEEDLES = (
    "first-hour tape is dumping on real volume",
    "sellers already dominate",
    "dollar-weighted selling dominates",
)


def _flag_blob(flags: Iterable[str] | None) -> str:
    return " | ".join(str(flag).lower() for flag in (flags or []))


def paper_hard_veto(
    flags: Iterable[str] | None,
    *,
    chain: str = "sol",
    website: str = "",
    twitter: str = "",
    twitter_handle: str = "",
    twitter_followers: float = 0.0,
    twitter_age_days: float = 0.0,
    twitter_verified: bool = False,
) -> str:
    """Why this 90 print must not fill. Empty string = no veto.

    Default chain is Solana, so callers that omit chain keep the old
    veto. Robinhood skips start-high and pre-pumped only. Prior rugs,
    hijack, copycat, and the rest still fire. Does not rewrite p_good.
    """
    from ..chains import normalize_chain

    skip = PAPER_RH_SKIP_NEEDLES if normalize_chain(chain) == "robinhood" else frozenset()
    blob = _flag_blob(flags)
    for needle in PAPER_HARD_NEEDLES:
        if needle in skip:
            continue
        if needle in blob:
            return needle
    if is_official_brand_website(website):
        return "official brand website"
    if is_staged_social_pair(twitter, website):
        return "staged social"
    if is_brand_x_account(twitter_followers, twitter_age_days, twitter_verified):
        return "celebrity/brand x"
    if is_bought_aged_token_x(twitter_handle, twitter_age_days):
        return "bought aged x"
    return ""


def paper_window_hours(chain: str) -> float:
    from ..chains import normalize_chain

    return PAPER_WINDOW_RH if normalize_chain(chain) == "robinhood" else PAPER_WINDOW_SOL


def paper_entry_p(hunt_p: float, t0_p: float, live_p: float) -> float:
    """Graduation score Hunt froze. Do not use a later repair or fade."""
    if hunt_p > 0:
        return hunt_p
    if t0_p > 0:
        return t0_p
    return live_p


def paper_tape_dump(flags: Iterable[str] | None) -> bool:
    blob = _flag_blob(flags)
    return any(needle in blob for needle in PAPER_TAPE_DUMP_NEEDLES)


def paper_no_run_exit(
    *,
    entry_mcap: float,
    peak_mcap: float,
    last_mcap: float,
    age_hours: float,
) -> bool:
    """True when a fill never printed 1.5× and already gave back half of peak.

    Live dump owns names that ran (CATFLIX-class graves never hit 1.5× so
    dump cannot fire). Confirmation age is PAPER_NO_RUN_HOURS. A missing
    last print does not close. Does not rewrite entry p_good.
    """
    from .live_fit import LIVE_EXIT_GIVEBACK, LIVE_EXIT_RAN

    entry = float(entry_mcap or 0.0)
    peak = float(peak_mcap or 0.0)
    last = float(last_mcap or 0.0)
    age = float(age_hours or 0.0)
    if entry <= 0 or peak <= 0 or last <= 0:
        return False
    if age < PAPER_NO_RUN_HOURS:
        return False
    if peak >= LIVE_EXIT_RAN * entry:
        return False
    return last < LIVE_EXIT_GIVEBACK * peak


def paper_live_dump(
    *,
    entry_mcap: float,
    peak_mcap: float,
    last_mcap: float,
    live_p: float | None,
) -> bool:
    """Exit a run that gave back half its peak while Live is dead.

    Same cuts on the Hunt board and after the card leaves the window
    (OAK 2.95× peak, last 0.15×). The caller passes tape Live, so a
    stored Hunt conviction of 0.90 does not hold a dust book. Does not
    fire on a never-1.5× grave (that is no-run) or a book still holding
    half its peak. Does not rewrite entry p_good. FEATURE_NAMES stays 66.
    """
    from .live_fit import LIVE_EXIT_GIVEBACK, LIVE_EXIT_P, LIVE_EXIT_RAN

    if live_p is None:
        return False
    entry = float(entry_mcap or 0.0)
    peak = float(peak_mcap or 0.0)
    last = float(last_mcap or 0.0)
    if entry <= 0 or peak <= 0 or last <= 0:
        return False
    return (
        float(live_p) < LIVE_EXIT_P
        and peak >= LIVE_EXIT_RAN * entry
        and last < LIVE_EXIT_GIVEBACK * peak
    )


def paper_chase_multiple(entry_mcap: float, last_mcap: float) -> float:
    """last ÷ entry. 0 when either print is missing."""
    entry = float(entry_mcap or 0.0)
    last = float(last_mcap or 0.0)
    if entry <= 0 or last <= 0:
        return 0.0
    return last / entry


def paper_chase_quality(
    *,
    features: dict | None = None,
    live_p: float | None = None,
    flags: Iterable[str] | None = None,
    chain: str = "sol",
    website: str = "",
    twitter: str = "",
    twitter_handle: str = "",
    twitter_followers: float = 0.0,
    twitter_age_days: float = 0.0,
    twitter_verified: bool = False,
    entry_mcap: float = 0.0,
    multiple: float | None = None,
) -> dict:
    """Whether existing thesis helpers clear a raised late-chase ceiling.

    Uses FEATURE_NAMES columns + ``v1_thesis_from_features`` / ``meme_quality_score``
    only — does not invent tags. Thin / unknown books stay on 1.8×.
    """
    from .meme_quality import MEME_QUALITY_WRITE_FLOOR, meme_quality_score
    from .paper_v1 import PAPER_V1_NOW_LIVE, v1_thesis_from_features, v1_thesis_ok

    feat = features if isinstance(features, dict) else {}
    why: list[str] = []
    thesis = v1_thesis_from_features(feat)
    if v1_thesis_ok(thesis):
        why.append("hard_thesis")
    try:
        live = float(live_p) if live_p is not None else 0.0
    except (TypeError, ValueError):
        live = 0.0
    if live >= float(PAPER_V1_NOW_LIVE):
        why.append("strong_live")
    mq = meme_quality_score(
        feat,
        risk_flags=list(flags or []),
        chain=chain,
        multiple=multiple,
        entry_mcap=entry_mcap,
        website=website,
        twitter=twitter,
        twitter_handle=twitter_handle,
        twitter_followers=twitter_followers,
        twitter_age_days=twitter_age_days,
        twitter_verified=twitter_verified,
    )
    if (
        mq.get("eligible")
        and not mq.get("hard_veto")
        and float(mq.get("score") or 0.0) >= float(MEME_QUALITY_WRITE_FLOOR)
    ):
        why.append("meme_quality")
    ok = bool(why)
    return {
        "ok": ok,
        "why": why,
        "bypass": ok,
        "ceiling": 0.0 if ok else PAPER_CHASE_MULT,
        "thesis": thesis,
        "meme_quality": mq,
        "live_p": live if live_p is not None else None,
    }


def paper_chase_ceiling(*, quality_ok: bool = False) -> float:
    """Hard floor, or 0 when quality bypasses late-chase entirely."""
    return 0.0 if quality_ok else PAPER_CHASE_MULT


def paper_is_chase(entry_mcap: float, last_mcap: float, *, ceiling: float | None = None) -> bool:
    """Already ran past the paper-fill chase floor (default 10×).

    Fresh-fat callers pass ``ceiling=PAPER_CHASE_FRESH_MULT`` (1.8).
    ``ceiling=0`` means no chase (quality bypass).
    """
    bar = float(PAPER_CHASE_MULT if ceiling is None else ceiling)
    if bar <= 0:
        return False
    return paper_chase_multiple(entry_mcap, last_mcap) >= bar


def stamp_paper_late_ok(
    features: dict | None,
    *,
    why: Iterable[str],
    multiple: float,
) -> dict:
    """Side-key stamp so Learn can see late-but-good. Not FEATURE_NAMES."""
    feat = dict(features) if isinstance(features, dict) else {}
    feat[PAPER_LATE_OK_KEY] = True
    feat[PAPER_LATE_OK_WHY_KEY] = [str(item) for item in why if item]
    feat[PAPER_LATE_OK_MULT_KEY] = round(float(multiple or 0.0), 4)
    return feat


def stamp_paper_provisional(features: dict | None, provisional: bool = True) -> dict:
    """Mark a thin-facts paper open. Cleared after background enrich."""
    feat = dict(features) if isinstance(features, dict) else {}
    feat[PAPER_PROVISIONAL_KEY] = bool(provisional)
    return feat


def research_is_provisional(features: dict | None) -> bool:
    feat = features if isinstance(features, dict) else {}
    return bool(feat.get(PAPER_PROVISIONAL_KEY))


def paper_is_fresh_fat(
    *,
    entry_mcap: float,
    last_mcap: float,
    last_liq: float,
    holders: int = 0,
) -> bool:
    """New fillable Sol book that has not dumped or already run.

    A missing holder count is not a fat book. Unknown used to pass
    (``0 < n`` is false), which skipped the Live ≥ 0.50 watch gate on
    the exact rows the holder model cannot see. Known count must be at
    least PAPER_FRESH_HOLDERS. The buy line does not use this bypass.
    """
    entry = float(entry_mcap or 0.0)
    last = float(last_mcap or 0.0) or entry
    liq = float(last_liq or 0.0)
    n = int(holders or 0)
    if entry <= 0 or last / entry < PAPER_HOLD_T0:
        return False
    if paper_is_chase(entry, last, ceiling=PAPER_CHASE_FRESH_MULT):
        return False
    if liq < PAPER_FRESH_LIQ:
        return False
    if n < PAPER_FRESH_HOLDERS:
        return False
    return True


def paper_fill_verdict(
    *,
    entry_mcap: float,
    last_mcap: float,
    last_liq: float,
    max_mcap: float = 0.0,
    chain: str = "sol",
    flags: Iterable[str] | None = None,
    website: str = "",
    twitter: str = "",
    twitter_handle: str = "",
    twitter_followers: float = 0.0,
    twitter_age_days: float = 0.0,
    twitter_verified: bool = False,
    features: dict | None = None,
    live_p: float | None = None,
) -> str:
    """pass / fail on the current book. Never pending — fills at t0.

    Late chase is a 10× floor for every paper fill. Quality names
    (hard thesis / strong Live / meme_quality eligible) skip the veto
    entirely. Callers stamp ``paper_late_ok`` when quality bypasses a
    chase at or above the floor.
    """
    if paper_hard_veto(
        flags,
        chain=chain,
        website=website,
        twitter=twitter,
        twitter_handle=twitter_handle,
        twitter_followers=twitter_followers,
        twitter_age_days=twitter_age_days,
        twitter_verified=twitter_verified,
    ):
        return "fail"
    if paper_tape_dump(flags):
        return "fail"
    if entry_mcap <= 0:
        return "fail"
    if float(last_liq or 0.0) < PAPER_CONFIRM_LIQ:
        return "fail"
    last = float(last_mcap or 0.0)
    if last <= 0:
        last = entry_mcap
    if last / entry_mcap < PAPER_HOLD_T0:
        return "fail"
    from ..serialize import is_phantom_t0_card, is_two_tick_card

    tape = {
        "chain": chain,
        "t0_mcap": float(entry_mcap or 0.0),
        "last_mcap": last,
        "max_mcap": float(max_mcap or 0.0),
        "risk_flags": list(flags or []),
    }
    if is_two_tick_card(tape):
        return "two-tick"
    if is_phantom_t0_card(tape):
        return "phantom t0"
    mult = paper_chase_multiple(entry_mcap, last)
    if mult >= PAPER_CHASE_MULT:
        quality = paper_chase_quality(
            features=features,
            live_p=live_p,
            flags=flags,
            chain=chain,
            website=website,
            twitter=twitter,
            twitter_handle=twitter_handle,
            twitter_followers=twitter_followers,
            twitter_age_days=twitter_age_days,
            twitter_verified=twitter_verified,
            entry_mcap=entry_mcap,
            multiple=mult,
        )
        if quality["ok"]:
            return "pass"
        return "late chase"
    return "pass"
