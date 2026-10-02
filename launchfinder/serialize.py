from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .chains import chain_links, token_chain
from .desk_lines import lines_for_scorer
from .models import Outcome, Research, Token
from .scoring.features import ath_dump_honesty_cap, stall_honesty_cap
from .scoring.model import YOUNG_RH_N_TRAIN


def apply_young_rh_desk_floor(card: dict[str, Any], n_train: int | None) -> dict[str, Any]:
    """Show the young RH heuristic floor on the hunt desk after a lapse.

    Live DCE/CASHTOPUS/QQQSAHUR: stored p 0.08–0.11 vs heuristic
    0.47–0.65 after n_train crossed 960. Second-look writes the DB;
    the card must not keep showing the pulled score. Do not lift
    2x+ (YOLO / GUH / CASHBIRD) or the 0.48 veto shelf (Claude).
    """
    if n_train is None or int(n_train) >= YOUNG_RH_N_TRAIN:
        return card
    if card.get("chain") != "robinhood":
        return card
    if card.get("label") is not None:
        return card
    if float(card.get("multiple") or 0.0) >= 2.0:
        return card
    # Live 11:21: LONGCOIN / OILGANG 2–8w factory stubs showed
    # floor 48–59 on the thin band. The young RH floor is for
    # crowded this-window books (CASHBIRD 87w). Thin prints stay
    # on the desk, just without the restored heuristic.
    from .scoring.outcomes import RH_HUNT_THIN_HOLDERS

    holders = card.get("holder_count")
    if holders is not None and int(holders) <= RH_HUNT_THIN_HOLDERS:
        return card
    p = float(card.get("p_good") or 0.0)
    hp = float(card.get("heuristic_p") or 0.0)
    if hp <= p + 1e-4:
        return card
    if abs(p - 0.48) < 1e-3 and hp > 0.48:
        return card
    card["p_good"] = hp
    card["score"] = round(hp * 100.0, 1)
    return card


def _card_age_min(card: dict[str, Any]) -> float:
    raws = [card.get("created_at"), card.get("first_seen_at"), card.get("migrated_at")]
    best = None
    for raw in raws:
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if best is None or t < best:
            best = t
    if best is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - best).total_seconds() / 60.0)


def apply_stall_honesty(card: dict[str, Any]) -> dict[str, Any]:
    """Show the faded % on the card before second-look writes the DB."""
    if card.get("label") is not None:
        return card
    p = float(card.get("p_good") or 0.0)
    faded = stall_honesty_cap(
        p,
        age_min=_card_age_min(card),
        multiple=float(card.get("multiple") or 0.0),
    )
    if faded + 1e-4 < p:
        card["p_good"] = faded
        card["score"] = round(faded * 100.0, 1)
    return card


def _outcome_last_mcap(outcome: Outcome | None) -> float:
    """Latest horizon print. Skip t15m — that is often the ATH wick."""
    if outcome is None:
        return 0.0
    for key in ("t24h_mcap", "t6h_mcap", "t1h_mcap"):
        raw = getattr(outcome, key, None)
        if raw is not None and float(raw) > 0:
            return float(raw)
    return 0.0


def _snap_last_mcap(token: Token) -> float:
    """Most recent positive print when snapshots are already loaded.

    Do not lazy-load here — hunt desk token_card is 2N without a
    preload, and /health sat 9–18s the last time that path widened.
    """
    snaps = token.__dict__.get("snapshots")
    if not snaps:
        return 0.0
    best: tuple[datetime | None, float] | None = None
    for snap in snaps:
        mcap = float(getattr(snap, "mcap_usd", 0.0) or 0.0)
        if mcap <= 0:
            continue
        taken = getattr(snap, "taken_at", None)
        if best is None or (taken is not None and (best[0] is None or taken > best[0])):
            best = (taken, mcap)
    return float(best[1]) if best else 0.0


def _fat_start_high_display_last(
    last: float,
    mx: float,
    t0: float,
    liq: float,
    chain: str | None = None,
) -> float:
    """Show ATH when a fat 2×+ book froze last on an hour-one dump.

    Live CRCL: stored last was 0 (labeled at t1h) so the card used
    the $159k snap while ATH is $1.38M / Dex ~$1.12M on $102k liq.
    The unfreeze used to require outcome.last_mcap > 0 and missed it.
    Live ICEMAN: Dex PumpSwap $1.7M / t0 $3.7M after a dump, leftover
    last_liq still $110k+. Unfreeze re-showed ATH $13M. Sol does not
    unfreeze. Missing chain (RH MEME / CRCL fixtures) still can.
    """
    if chain == "sol":
        return last
    if (
        last > 0
        and mx > 0
        and t0 > 0
        and liq >= 50_000
        and last < 0.40 * mx
        and last / t0 < 2.0
        and mx / t0 >= 2.0
    ):
        return mx
    return last


# Live RWA: Dex last $958k / 36× / Live 95 while the latest snap
# was $64k at 06:30Z and Pair was an empty 1m candle. last was
# written without a matching snap (record_live_last updates last
# even when the 15m late-snap gap is not due). MEME last $82M vs
# an hour-one $175k snap is the opposite — Dex later pair, stored
# max still 80×-capped at $1.07M, so last > stored max. FEATURE_NAMES 66.
UNBACKED_LAST_RATIO = 3.0
UNBACKED_SNAP_AGE_MIN = 45.0
UNBACKED_LAST_FLAG = "Last print is unbacked — stored tape never printed this market cap"


def is_unbacked_last_card(card: dict[str, Any] | None) -> bool:
    """Dex last ran away from the stored tape and became the ATH.

    Live RWA: last $958k == max $958k vs snap $64k / 6h. MEME last
    $82M over a 80×-capped max $1.07M is a later pair — keep last.
    Sitting RH / NINA (last ≈ snap) stay open. A snap younger than
    45m may still catch a live MEME-class Dex tick.
    """
    row = card or {}
    # A fat start-high the desk unfroze to its stored ATH (CRCL: labeled
    # 5x at t1h, last 0) is backed by outcome.max_mcap, not a flash Dex tick.
    if row.get("last_unfrozen"):
        return False
    last = float(row.get("last_mcap") or 0.0)
    snap = float(row.get("snap_mcap") or 0.0)
    if last <= 0 or snap <= 0:
        return False
    if last < UNBACKED_LAST_RATIO * snap:
        return False
    if float(row.get("snap_age_min") or 0.0) < UNBACKED_SNAP_AGE_MIN:
        return False
    stored_max = float(row.get("stored_max_mcap") or 0.0)
    if stored_max > 0 and last > stored_max * 1.05:
        return False
    return True


def apply_unbacked_last_honesty(card: dict[str, Any]) -> dict[str, Any]:
    """Show the snap-backed last on Hunt / detail. Does not write p_good."""
    if not is_unbacked_last_card(card):
        return card
    snap = float(card.get("snap_mcap") or 0.0)
    snap_liq = float(card.get("snap_liq") or 0.0)
    snap_max = float(card.get("snap_max_mcap") or 0.0)
    t0 = float(card.get("t0_mcap") or 0.0)
    card["last_mcap"] = snap
    card["max_mcap"] = max(snap, snap_max)
    if snap_liq > 0:
        card["last_liq"] = snap_liq
    if t0 > 0:
        card["multiple"] = snap / t0
    flags = list(card.get("risk_flags") or [])
    if not any("unbacked" in str(flag).lower() for flag in flags):
        flags.append(UNBACKED_LAST_FLAG)
    card["risk_flags"] = flags
    return card


def attach_snap_tape(card: dict[str, Any], tape: dict[str, Any] | None) -> dict[str, Any]:
    """Copy latest-snap fields onto a Hunt card, then unback a flash last."""
    row = tape or {}
    if row.get("snap_mcap"):
        card["snap_mcap"] = float(row.get("snap_mcap") or 0.0)
        card["snap_liq"] = float(row.get("snap_liq") or 0.0)
        card["snap_max_mcap"] = float(row.get("snap_max_mcap") or 0.0)
        card["snap_age_min"] = float(row.get("snap_age_min") or 0.0)
        if "volume_h1" in row:
            card["volume_h1"] = row.get("volume_h1")
    if card.get("stored_max_mcap") is None:
        card["stored_max_mcap"] = float(card.get("max_mcap") or 0.0)
    apply_unbacked_last_honesty(card)
    return card


def _latest_snap_tape(token: Token) -> dict[str, Any]:
    """Latest positive snap when snapshots are already loaded.

    Do not lazy-load — Hunt token_card is 2N without a preload.
    """
    snaps = token.__dict__.get("snapshots")
    if not snaps:
        return {}
    best: tuple[datetime | None, float, Any] | None = None
    peak = 0.0
    for snap in snaps:
        mcap = float(getattr(snap, "mcap_usd", 0.0) or 0.0)
        if mcap > peak:
            peak = mcap
        if mcap <= 0:
            continue
        taken = getattr(snap, "taken_at", None)
        if best is None or (taken is not None and (best[0] is None or taken > best[0])):
            best = (taken, mcap, snap)
    if best is None:
        return {}
    taken, mcap, snap = best
    age = 0.0
    if taken is not None:
        ts = taken if taken.tzinfo else taken.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 60.0)
    return {
        "snap_mcap": mcap,
        "snap_liq": float(getattr(snap, "liquidity_usd", 0.0) or 0.0),
        "snap_max_mcap": peak,
        "snap_age_min": age,
        "volume_h1": float(getattr(snap, "volume_h1", 0.0) or 0.0),
    }


def _card_last_mcap(token: Token, outcome: Outcome | None) -> float:
    """Live Dex print wins over a frozen hour-one snapshot.

    Live MEME: latest early snap stayed $175k at 21:29 while Dex
    printed $82M on a later pair. Hunt has no snaps loaded, so
    outcome.last_mcap is the desk last after a refresh.
    Live CRCL: t1h dump froze last at $159k while stored ATH is
    $1.38M and Dex recovered to $1.2M on a $105k book. A fat
    start-high that already printed 2× should not show the dump
    as last — that hid it from Doing well (0.58× vs 5×).
    """
    stored = float(getattr(outcome, "last_mcap", 0) or 0.0) if outcome else 0.0
    mx = float(getattr(outcome, "max_mcap", 0) or 0.0) if outcome else 0.0
    t0 = float(getattr(outcome, "t0_mcap", 0) or 0.0) if outcome else 0.0
    liq = float(getattr(outcome, "last_liq", 0) or 0.0) if outcome else 0.0
    snap = _snap_last_mcap(token)
    effective = stored if stored > 0 else snap
    unfrozen = _fat_start_high_display_last(effective, mx, t0, liq, token_chain(token))
    if unfrozen != effective:
        return unfrozen
    if stored > 0:
        return stored
    return snap or _outcome_last_mcap(outcome)


def _card_last_was_unfrozen(token: Token, outcome: Outcome | None, last: float) -> bool:
    stored = float(getattr(outcome, "last_mcap", 0) or 0.0) if outcome else 0.0
    mx = float(getattr(outcome, "max_mcap", 0) or 0.0) if outcome else 0.0
    effective = stored if stored > 0 else _snap_last_mcap(token)
    return mx > 0 and last == mx and effective > 0 and effective != mx


def _card_max_mcap(outcome: Outcome | None, last: float) -> float:
    """Display ATH at least as high as the live print.

    honest_tracked_peak still caps stored max at 80×. Live MEME last
    $82M vs stored ATH $1.07M would otherwise look broken and hide a
    still-many-multiples book from Doing well.
    """
    stored = float(outcome.max_mcap or 0.0) if outcome else 0.0
    return max(stored, float(last or 0.0))


# Same retain floor as ath_dump_honesty_cap. Doing well uses it to
# drop the highlight reel; hunt/runners still keep membership.
ATH_HOLD_RETAIN = 0.40
DOING_WELL_LIVE_MULTIPLE = 2.0
# Live MEME: $22k → $74M (thousands of ×) after a 40%+ fade from the
# chart peak. BELIEVE 3.6× live / 13% of ATH is still a recap. A fat
# book that is still many multiples from t0 stays on Doing well.
DOING_WELL_HELD_MULTIPLE = 10.0
DOING_WELL_HELD_LIQ = 50_000.0
# Live Laptop GsewXp / TNT: labeled 5.2× / 6.7× this-window, $40k–$51k
# liq, 89–95% of ATH. Runners never confirm (t0 snap already the live
# book, or snaps with last_liq wiped to 0). The 10× / $50k held door
# misses them. Do not leftover-sort 2×+; do not raise MAX_HONEST.
DOING_WELL_LABELED_LIVE_MULTIPLE = 5.0
DOING_WELL_LABELED_LIQ = 20_000.0
# A labeled winner with this many wallets is not a POKEMON bundle even
# when its t0 carried a pre-pump / start-high flag.
DOING_WELL_LABELED_HOLDERS = 100


def is_sol_leftover_fdv_doing_well(card: dict[str, Any]) -> bool:
    """Frozen $69k t0 + leftover hundreds-of-millions tape.

    Live LAPTOP `76cJTCcy…pump`: desk 4279× / $295M / 57 wallets /
    100% top10 / prepumped. Dex PumpSwap is $2.3k mcap / $2.3k liq —
    rugged. The 10× held-launch exception was for RH MEME, not this.
    Do not raise MAX_HONEST_MULTIPLE; Sol Doing well just refuses it.
    """
    from .chains import graduation_mcap
    from .scoring.outcomes import MAX_HONEST_MULTIPLE

    # Explicit sol only. normalize_chain("") is sol, and the RH MEME
    # held-run fixture has no chain key — that path must stay open.
    if card.get("chain") != "sol":
        return False
    t0 = float(card.get("t0_mcap") or 0.0)
    last = float(card.get("last_mcap") or 0.0)
    if t0 <= 0 or last <= 0:
        return False
    live = last / t0
    if live > MAX_HONEST_MULTIPLE:
        return True
    floor = graduation_mcap("sol")
    if floor <= 0:
        return False
    t0_floor = abs(t0 - floor) / floor <= 0.05
    if not t0_floor or live < DOING_WELL_HELD_MULTIPLE:
        return False
    holders = float(card.get("holder_count") or 0.0)
    top10 = float(card.get("top10_pct") or 0.0)
    flags = " ".join(
        str(flag).lower()
        for flag in (card.get("risk_flags") or card.get("flags") or [])
    )
    concentrated = (0 < holders < 80 and top10 >= 90) or "tiny holder base" in flags
    prepumped = any(
        needle in flags
        for needle in ("pre-pumped", "prepumped", "start-high rug", "bundle-owned price")
    )
    return concentrated or prepumped


def doing_well_scam_book(card: dict[str, Any]) -> bool:
    """Copycat / pre-pump / 32-wallet bundle is not a held launch.

    Live POKEMON: official @pokemon (8M, 17y), copycat spam, 32 holders /
    95% top10, pre-pumped 2.5×. Hunt already zeros it. Doing well was
    still listing any 2× tape. RH MEME is the exception: a copied
    ticker that already ran 10×+ on fat liq stays.
    """
    from .scoring.bloom import bloom_scam_book

    flags = [str(f) for f in (card.get("risk_flags") or card.get("flags") or [])]
    last = float(card.get("last_mcap") or 0.0)
    t0 = float(card.get("t0_mcap") or 0.0)
    liq = float(card.get("last_liq") or 0.0)
    live = (last / t0) if t0 > 0 and last > 0 else 0.0
    held = live >= DOING_WELL_HELD_MULTIPLE and liq >= DOING_WELL_HELD_LIQ
    # Live CRCL: labeled 5x winner, 438 wallets, $95k book, still 3.6x —
    # the pre-pump flag describes its t0, not a POKEMON 32-wallet bundle.
    holders = float(card.get("holder_count") or card.get("holders") or 0.0)
    labeled_broad = (
        card.get("label") == 1
        and live >= DOING_WELL_LIVE_MULTIPLE
        and liq >= DOING_WELL_LABELED_LIQ
        and holders >= DOING_WELL_LABELED_HOLDERS
    )
    if held or labeled_broad:
        # 10×+ fat book (RH MEME / late-Dex CRCL) is not a bloom-scam
        # just because the ticker was copied or t0 was a Dex catch-up.
        slim = [
            flag
            for flag in flags
            if "copycat" not in flag.lower()
            and "start-high" not in flag.lower()
            and not (labeled_broad and "pre-pumped" in flag.lower())
        ]
        return bloom_scam_book(slim)
    if bloom_scam_book(flags):
        return True
    top10 = float(card.get("top10_pct") or 0.0)
    # Live USWR: 691 wallets / 96% top10. The <80 cut treated a
    # padded sybil farm as a broad book.
    if top10 >= 90:
        return True
    return False


def still_doing_well(card: dict[str, Any]) -> bool:
    """True when the book is still up now — not a dumped-off-ATH recap.

    Live BELIEVE 28× then $89k (13% of ATH) and Sol CAC $2k after
    70× are runners, not Doing well. Need a live print and ≥2× vs t0.
    Ordinary climbs also need 40% of ATH. A still-many-multiples
    launch (MEME thousands of × from t0) stays even after a 40%+ fade.
    Sol leftover FDV (LAPTOP $295M vs Dex $2.3k) does not.
    """
    last = float(card.get("last_mcap") or 0.0)
    mx = float(card.get("max_mcap") or 0.0)
    t0 = float(card.get("t0_mcap") or 0.0)
    liq = float(card.get("last_liq") or 0.0)
    last = _fat_start_high_display_last(last, mx, t0, liq, card.get("chain"))
    if mx <= 0 or t0 <= 0:
        return False
    if last <= 0:
        last = mx
    live = last / t0
    if live < DOING_WELL_LIVE_MULTIPLE:
        return False
    # Live fih / SAAR / KAT: gmgn_trenches historical catch-up still
    # listed on Doing well with frozen ATH last. RH MEME held-run is
    # not Sol historical. Require chain == "sol" explicitly.
    if card.get("historical") is True and card.get("chain") == "sol":
        return False
    patched = dict(card)
    patched["last_mcap"] = last
    if is_sol_leftover_fdv_doing_well(patched):
        return False
    if doing_well_scam_book(card):
        return False
    if live >= DOING_WELL_HELD_MULTIPLE:
        return True
    if last / mx < ATH_HOLD_RETAIN:
        return False
    return True


def live_doing_well_multiple(card: dict[str, Any]) -> float:
    """Current multiple vs t0 so the tab does not rank by a dumped ATH."""
    last = float(card.get("last_mcap") or 0.0)
    t0 = float(card.get("t0_mcap") or 0.0)
    mx = float(card.get("max_mcap") or 0.0)
    liq = float(card.get("last_liq") or 0.0)
    last = _fat_start_high_display_last(last, mx, t0, liq, card.get("chain"))
    if last <= 0:
        last = mx
    if last > 0 and t0 > 0:
        return last / t0
    return float(card.get("multiple") or 0.0)


def apply_held_ingest_miss_note(card: dict[str, Any]) -> dict[str, Any]:
    """Keep the ingest % — label it as the t0 miss on a held 10×+ book.

    Live MEME: stored p 0.0279 / model 0.006 because Blockscout froze
    4 LP rows. Paper stays on that p. The desk must not read 2.8% as
    “this is not a runner” next to 3898×. Do not rewrite p_good.
    """
    last = float(card.get("last_mcap") or 0.0)
    t0 = float(card.get("t0_mcap") or 0.0)
    liq = float(card.get("last_liq") or 0.0)
    p = float(card.get("p_good") or 0.0)
    if t0 <= 0 or last / t0 < DOING_WELL_HELD_MULTIPLE:
        return card
    if is_sol_leftover_fdv_doing_well(card):
        return card
    if liq < DOING_WELL_HELD_LIQ or p >= 0.50:
        return card
    flags = list(card.get("risk_flags") or [])
    note = "Ingest score is the t0 miss (frozen LP sample) — book already ran"
    if not any("t0 miss" in str(flag) for flag in flags):
        flags.append(note)
    card["risk_flags"] = flags
    card["ingest_miss"] = True
    return card


BRAND_CLONE_ENTRY_CAP = 0.48


def is_brand_clone_card(card: dict[str, Any] | None) -> bool:
    """Desk-side twin of is_brand_clone_book — uses card fields, not features.

    Hunt freezes entry_p at first insert. Live Google Gemini kept
    showing Entry 98% after copycat already zeroed live conviction.
    """
    row = card or {}
    flags = " ".join(str(flag).lower() for flag in (row.get("risk_flags") or row.get("flags") or []))
    if "hijack" in flags or "celebrity/brand" in flags:
        return True
    from .social import is_official_brand_website

    if is_official_brand_website(str(row.get("website") or "")):
        return True
    followers = float(row.get("twitter_followers") or 0)
    return "copycat" in flags and followers >= 100_000


def frozen_entry_p(token: Token, hunt_entry_p: float | None = None) -> float:
    """At-entry score, the same on every desk tab.

    Live TTNB: t0 snap scored 0.85 (Hunt froze it), then an organic-book
    repair lifted research.p_good to 0.92 and Bloom showed Entry 92 next
    to Hunt's 85. Hunt's frozen entry first, then the t0 snap, then live
    p_good only when nothing was frozen. Never rewrites research.p_good.
    """
    if hunt_entry_p and float(hunt_entry_p) > 0:
        return float(hunt_entry_p)
    t0_p = 0.0
    t0_at = None
    for snap in token.snapshots or []:
        if snap.kind != "t0" or not snap.p_good:
            continue
        if t0_at is None or (snap.taken_at is not None and snap.taken_at < t0_at):
            t0_at = snap.taken_at
            t0_p = float(snap.p_good)
    if t0_p > 0:
        return t0_p
    return float((token.research.p_good if token.research else 0.0) or 0.0)


def is_copycat_dump_card(card: dict[str, Any] | None) -> bool:
    """Desk-side twin of is_copycat_dump_book.

    Hunt froze JubJub entry_p at 0.99. Copycat + first-hour dump is not
    a 90+ launch — show the 0.48 cap. RH MEME copycat-only stays open.
    """
    flags = " ".join(str(flag).lower() for flag in ((card or {}).get("risk_flags") or (card or {}).get("flags") or []))
    if "copycat" not in flags:
        return False
    return any(
        needle in flags
        for needle in (
            "dumping on real volume",
            "dollar-weighted selling",
            "sellers already dominate",
        )
    )


def is_first_sight_card(card: dict[str, Any] | None) -> bool:
    """Entry written by the calibrated first-sight model.

    Its scale tops out near 0.67, so the 0.48 "not a 90+ launch" display
    caps built for the over-confident legacy blend would sit above its
    own high line (0.50) and make the shown Entry disagree with the ledger.
    """
    return str((card or {}).get("scorer") or "") == "first_sight"


def brand_clone_entry_cap(p: float, card: dict[str, Any] | None) -> float:
    if is_first_sight_card(card):
        return float(p or 0.0)
    if is_brand_clone_card(card):
        return min(float(p or 0.0), BRAND_CLONE_ENTRY_CAP)
    return float(p or 0.0)


def is_two_tick_card(card: dict[str, Any] | None) -> bool:
    """2× wick fully given back to the open — Dex 1m is two candles.

    Live TWIN: t0 $41k / max $85k / last $40k / Entry 92 / no flags.
    NINA last >> t0. pisscoin never wicked off t0. FEATURE_NAMES stays 66.
    """
    row = card or {}
    if str(row.get("chain") or "sol").lower() in {"robinhood", "rh"}:
        return False
    t0 = float(row.get("t0_mcap") or 0.0)
    last = float(row.get("last_mcap") or 0.0)
    peak = float(row.get("max_mcap") or 0.0)
    if t0 < 20_000 or last <= 0 or peak <= 0:
        return False
    flags = " ".join(str(flag).lower() for flag in (row.get("risk_flags") or row.get("flags") or []))
    # Live ニーナ: snipers, 2.6× ATH, last still ~t0. That is a held
    # book, not TWIN's empty two-tick. Do not cap sniper-only holds.
    # Live HUGE: snipers, 9.3× ATH given back to t0 (11% of peak) /
    # Entry 92. A 5×+ wick dumped to the open is two-tick, not a hold.
    if "sniper" in flags and peak < 5.0 * t0:
        return False
    if last > 1.15 * t0:
        return False
    if peak < 1.8 * t0:
        return False
    return last < 0.65 * peak


def is_staged_social_card(card: dict[str, Any] | None) -> bool:
    """Pasted tweet + generated auction/tweet website. Live TWIN."""
    from .social import is_staged_social_pair

    row = card or {}
    return is_staged_social_pair(row.get("twitter"), row.get("website"))


def is_collapsed_book_card(card: dict[str, Any] | None) -> bool:
    """A real t0 open that is already leftover dust.

    Live [KAT]: t0 $37k / last $3.2k / frozen Entry 92 / snipers only.
    POOR: t0 $55k / last $12k. Live LOS: t0 $35k / last $12.6k
    (0.36×) still showed Entry 92. Live ETAC AM1SMh: t0 $393k /
    last $21k (0.05×) sat Entry 95.5 because $21k cleared the old
    $20k last floor. Live Peg: t0 $36k / last $17.5k (0.49×) /
    Entry 90. The 0.40× ratio missed a sub-$25k leftover that
    had already given back a 2.5× wick. NINA last >> t0 — not this.
    FEATURE_NAMES stays 66.
    """
    t0 = float((card or {}).get("t0_mcap") or 0.0)
    last = float((card or {}).get("last_mcap") or 0.0)
    if t0 < 20_000 or last <= 0:
        return False
    if last >= 25_000:
        return False
    return last < 0.50 * t0


def is_phantom_t0_card(card: dict[str, Any] | None) -> bool:
    """Dex/FDV t0 in the hundreds of thousands while last is dust.

    Live FUNICORN: t0 $2.77M / last $1.8k / frozen Entry 92.
    Time-to-migrate looked human so entry_premium never set.
    NINA t0 $43k / last $431k is a real open — not this.
    FEATURE_NAMES stays 66.
    """
    t0 = float((card or {}).get("t0_mcap") or 0.0)
    last = float((card or {}).get("last_mcap") or 0.0)
    if t0 < 500_000 or last <= 0:
        return False
    if last >= 25_000:
        return False
    return last < 0.05 * t0


def is_bought_aged_token_x_card(card: dict[str, Any] | None) -> bool:
    """Bought aged project X — live Metapad @metapadspace 3565d.

    Token X only (``twitter_handle`` after display_token_x). Empty
    handle is Dev X / no project account. Official brand X is a
    different veto. FEATURE_NAMES stays 66.
    """
    from .research.twitter import is_bought_aged_token_x

    row = card or {}
    return is_bought_aged_token_x(
        row.get("twitter_handle"),
        float(row.get("twitter_age_days") or 0),
        claimed_brand=bool(row.get("claimed_brand_x")),
    )


def desk_entry_cap(p: float, card: dict[str, Any] | None) -> float:
    """What Hunt / Bloom / Doing well show as Entry.

    Frozen hunt/t0 p, then the brand-clone, copycat-dump,
    start-high, social-without-tape, first-hour dump, and
    phantom-t0 display caps. Does not rewrite research.p_good.
    """
    capped = brand_clone_entry_cap(p, card)
    if is_first_sight_card(card):
        return capped
    flags = " ".join(str(flag).lower() for flag in ((card or {}).get("risk_flags") or (card or {}).get("flags") or []))
    if (
        "copycat" in flags
        or "start-high" in flags
        or "social card without a live tape" in flags
        or "first-hour tape is dumping" in flags
        or "funded by a wallet behind prior rugs" in flags
        or "pasted tweet" in flags
        or "generated website" in flags
        or "bought aged" in flags
    ):
        return min(capped, BRAND_CLONE_ENTRY_CAP)
    if (
        is_copycat_dump_card(card)
        or is_phantom_t0_card(card)
        or is_collapsed_book_card(card)
        or is_two_tick_card(card)
        or is_staged_social_card(card)
        or is_bought_aged_token_x_card(card)
    ):
        return min(capped, BRAND_CLONE_ENTRY_CAP)
    return capped


def apply_desk_entry_honesty(card: dict[str, Any]) -> dict[str, Any]:
    """Show the desk Entry cap on the token card, not only Hunt.

    Live TWIN: /api/hunt entry_p was 0.48 but /api/tokens still
    printed p_good 92, so the research pane said Entry 92 frozen.
    Does not write research.p_good. FEATURE_NAMES stays 66.
    """
    p = float(card.get("p_good") or 0.0)
    if is_first_sight_card(card):
        # p_good is the frozen first-sight Entry (second look never moves
        # it); heuristic_p is the legacy blend on another scale.
        raw = float(card.get("entry_p") or p)
    else:
        raw = float(card.get("entry_p") or card.get("heuristic_p") or p)
    capped = desk_entry_cap(raw, card)
    card["entry_p"] = capped
    if capped + 1e-4 >= p:
        return card
    card["p_good"] = capped
    card["score"] = round(capped * 100.0, 1)
    flags = list(card.get("risk_flags") or [])
    if is_staged_social_card(card) and not any("pasted tweet" in str(flag).lower() for flag in flags):
        flags.append("Project X is a pasted tweet on a generated website")
    if is_two_tick_card(card) and not any("two-tick" in str(flag).lower() for flag in flags):
        flags.append("Two-tick tape — 2× wick given back to the open")
    if is_bought_aged_token_x_card(card) and not any("bought aged" in str(flag).lower() for flag in flags):
        flags.append("Project X is a bought aged account — usually purchased to scam")
    card["risk_flags"] = flags
    return card


def apply_brand_clone_honesty(card: dict[str, Any]) -> dict[str, Any]:
    """Show the 0.48 brand-clone cap on frozen hunt entry_p / p_good."""
    p = float(card.get("p_good") or 0.0)
    capped = brand_clone_entry_cap(p, card)
    if card.get("entry_p") is not None:
        card["entry_p"] = brand_clone_entry_cap(float(card.get("entry_p") or 0.0), card)
    if capped + 1e-4 >= p:
        return card
    card["p_good"] = capped
    card["score"] = round(capped * 100.0, 1)
    flags = list(card.get("risk_flags") or [])
    note = "Claimed celebrity/brand X — not this launch"
    if not any("celebrity/brand" in str(flag).lower() for flag in flags):
        flags.append(note)
    card["risk_flags"] = flags
    return card


def apply_ath_dump_honesty(card: dict[str, Any]) -> dict[str, Any]:
    """Show the dumped-off-ATH % even on labeled confirmed 5× cards.

    Stall honesty skips labeled rows, so BELIEVE stayed at 78 after
    giving back 87% of ATH. Hunt/runners keep membership; only the
    live score fades. Doing well drops these via still_doing_well.
    """
    p = float(card.get("p_good") or 0.0)
    faded = ath_dump_honesty_cap(
        p,
        max_mcap=float(card.get("max_mcap") or 0.0),
        last_mcap=float(card.get("last_mcap") or 0.0),
        multiple=float(card.get("multiple") or 0.0),
    )
    if faded + 1e-4 >= p:
        return card
    card["p_good"] = faded
    card["score"] = round(faded * 100.0, 1)
    flags = list(card.get("risk_flags") or [])
    mx = float(card.get("max_mcap") or 0.0)
    last = float(card.get("last_mcap") or 0.0)
    pct = int(round(100.0 * last / mx)) if mx > 0 else 0
    note = f"Dumped off ATH (now {pct}% of peak)"
    if not any("Dumped off ATH" in str(flag) for flag in flags):
        flags.append(note)
    card["risk_flags"] = flags
    return card


def _card_launchpad(research: Research | None, source: str) -> str:
    """Pad shown on the live book. Prefer the GMGN trenches field."""
    if research and research.raw_json:
        try:
            raw = json.loads(research.raw_json)
        except json.JSONDecodeError:
            raw = {}
        pad = str(((raw.get("gmgn") or {}).get("launchpad")) or "").strip()
        if pad:
            return pad
    if source in ("rh_pons",):
        return "pons"
    if source in ("rh_dex", "sol_dex"):
        return "dex"
    if source in ("rh_bitquery",):
        return "uniswap_v4"
    if source in ("rh_trenches",):
        return "trenches"
    return ""


def _card_multiple(outcome: Outcome | None) -> float:
    """Desk multiple. Ingest can leave 0 when t0 == max (live KFC)."""
    if outcome is None:
        return 0.0
    multiple = float(outcome.multiple or 0.0)
    t0 = float(outcome.t0_mcap or 0.0)
    mx = float(outcome.max_mcap or 0.0)
    if multiple <= 0 and t0 > 0 and mx > 0:
        return mx / t0
    return multiple


def _card_bio_match(token: Token, research: Research | None) -> bool:
    """Stored FxTwitter bio vs ticker. No extra HTTP. Not FEATURE_NAMES."""
    from .social import bio_hits_project

    bio = ""
    if research and research.raw_json:
        try:
            raw = json.loads(research.raw_json)
        except json.JSONDecodeError:
            raw = {}
        tw = raw.get("twitter") if isinstance(raw.get("twitter"), dict) else {}
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        bio = str(tw.get("bio") or user.get("bio") or "")
    return bio_hits_project(bio, token.symbol or "", token.name or "")


def display_token_x(token_handle: str, dev_handle: str) -> str:
    """Project main X. Empty when that handle is the personal developer.

    The Pump/PONS deployer X is a person working on the project, not
    the token's official account. Do not show or brand-check it as
    the main X. FEATURE_NAMES stays 66.
    """
    token = str(token_handle or "").lstrip("@")
    dev = str(dev_handle or "").lstrip("@")
    if token and dev and token.lower() == dev.lower():
        return ""
    return token


def associated_dev_handle(token: Token, research: Research | None) -> str:
    """Personal developer X from Pump ``user.x_username``. No extra GMGN HTTP.

    That handle is a person working on the project — not the token's
    main X. A profile URL / stored ``twitter_handle`` is the project's
    listed account and must not paint the tape as ``has_dev_x``.
    TWIN-class staged social is not a public dev unless the creator
    X differs. Official brand X (NASA, Google, …) is never the
    deployer. FEATURE_NAMES stays 66.
    """
    from .research.twitter import is_claimed_brand_x
    from .social import extract_twitter_handle, is_staged_social_pair

    raw: dict[str, Any] = {}
    if research and research.raw_json:
        try:
            raw = json.loads(research.raw_json)
        except json.JSONDecodeError:
            raw = {}
    flags: list[str] = []
    if research and research.risk_flags_json:
        try:
            loaded = json.loads(research.risk_flags_json)
            if isinstance(loaded, list):
                flags = [str(x) for x in loaded]
        except json.JSONDecodeError:
            flags = []
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    creator = extract_twitter_handle(str(user.get("x_username") or user.get("twitter") or ""))
    if not creator:
        return ""
    token_url = str(token.twitter or "")
    website = str(token.website or "")
    if is_staged_social_pair(token_url, website):
        token_h = extract_twitter_handle(token_url) or extract_twitter_handle(
            (research.twitter_handle if research else "") or ""
        )
        if token_h and creator.lower() == token_h.lower():
            return ""
    if is_claimed_brand_x(creator, flags=flags):
        return ""
    return creator


def token_card(token: Token) -> dict[str, Any]:
    research = token.research
    outcome = token.outcome
    last = _card_last_mcap(token, outcome)
    dev_handle = associated_dev_handle(token, research)
    from .research.twitter import is_claimed_brand_x
    from .social import extract_twitter_handle as _x_handle
    from .social import is_twitter_status_url as _is_status

    token_handle = ""
    if research:
        token_handle = research.twitter_handle or ""
    token_url = str(token.twitter or "")
    if token_url and _is_status(token_url):
        pasted = _x_handle(token_url)
        if token_handle and pasted and token_handle.lower() == pasted.lower():
            token_handle = ""
    if not token_handle and token_url and not _is_status(token_url):
        token_handle = _x_handle(token_url)
    token_handle = display_token_x(token_handle, dev_handle)
    brand_flags: list[str] = []
    if research and research.risk_flags_json:
        try:
            loaded = json.loads(research.risk_flags_json)
            if isinstance(loaded, list):
                brand_flags = [str(x) for x in loaded]
        except json.JSONDecodeError:
            brand_flags = []
    claimed_brand_x = is_claimed_brand_x(
        token_handle,
        followers=float(research.twitter_followers or 0) if research else 0.0,
        age_days=float(research.twitter_age_days or 0) if research else 0.0,
        verified=bool(research.twitter_verified) if research else False,
        flags=brand_flags,
    )
    card = {
        "mint": token.mint,
        "chain": token_chain(token),
        "name": token.name,
        "symbol": token.symbol,
        "creator": token.creator,
        "image_url": token.image_url,
        "twitter": token.twitter,
        "website": token.website,
        "telegram": token.telegram,
        "github_url": token.github_url,
        "created_at": token.created_at_chain.isoformat() if token.created_at_chain else None,
        "migrated_at": token.migrated_at.isoformat() if token.migrated_at else None,
        "first_seen_at": token.first_seen_at.isoformat() if token.first_seen_at else None,
        "source": token.source,
        "launchpad": _card_launchpad(research, token.source or ""),
        "historical": token.is_historical,
        "reply_count": token.reply_count,
        "p_good": research.p_good if research else 0.0,
        "score": round((research.p_good if research else 0.0) * 100.0, 1),
        "scorer": (research.scorer if research else None) or "legacy",
        "heuristic_p": research.heuristic_p if research else 0.0,
        "model_p": research.model_p if research else 0.0,
        "preview_p": research.preview_p if research else 0.0,
        "twitter_handle": token_handle,
        "dev_handle": dev_handle,
        "has_dev_x": bool(dev_handle),
        "claimed_brand_x": claimed_brand_x,
        "twitter_followers": research.twitter_followers if research else 0,
        "twitter_tweets": research.twitter_tweets if research else 0,
        "twitter_age_days": research.twitter_age_days if research else 0,
        "twitter_verified": bool(research.twitter_verified) if research else False,
        "twitter_bio_match": _card_bio_match(token, research),
        "github_stars": research.github_stars if research else 0,
        "github_age_days": research.github_age_days if research else 0,
        "holder_count": research.holder_count if research else 0,
        "top10_pct": research.top10_pct if research else 0,
        "time_to_migrate_min": research.time_to_migrate_min if research else 0,
        "thesis": research.thesis if research else "",
        "reasons": json.loads(research.reasons_json) if research and research.reasons_json else [],
        "risk_flags": json.loads(research.risk_flags_json) if research and research.risk_flags_json else [],
        "multiple": _card_multiple(outcome),
        "label": outcome.label if outcome else None,
        "t0_mcap": outcome.t0_mcap if outcome else 0.0,
        "max_mcap": _card_max_mcap(outcome, last),
        "last_liq": outcome.last_liq if outcome else 0.0,
        "last_mcap": last,
        "stored_max_mcap": float(outcome.max_mcap or 0.0) if outcome else 0.0,
        "last_unfrozen": _card_last_was_unfrozen(token, outcome, last),
        "links": chain_links(token.mint, token_chain(token), pair_address=token.pool_address or ""),
    }
    if token_chain(token) == "sol":
        from .research.holder_rewards import lookup as _holder_rewards

        paid = _holder_rewards(token.mint)
        if paid:
            card["holder_rewards"] = paid
    attach_snap_tape(card, _latest_snap_tape(token))
    card.pop("stored_max_mcap", None)
    card.pop("snap_max_mcap", None)
    card.pop("last_unfrozen", None)
    if is_first_sight_card(card):
        # Frozen Entry before the stall / ATH fades touch p_good.
        card["entry_p"] = float(research.p_good or 0.0) if research else 0.0
        card["desk_lines"] = lines_for_scorer(card["scorer"], token_chain(token)).as_dict()
    return apply_desk_entry_honesty(
        apply_brand_clone_honesty(
            apply_ath_dump_honesty(apply_stall_honesty(apply_held_ingest_miss_note(card)))
        )
    )


def token_detail(token: Token) -> dict[str, Any]:
    # Load snaps first so a labeled 5× that dumped (BELIEVE) can
    # fade the displayed score without a hunt-desk N+1.
    _ = token.snapshots
    card = token_card(token)
    research = token.research
    card["features"] = json.loads(research.features_json) if research and research.features_json else {}
    card["raw"] = json.loads(research.raw_json) if research and research.raw_json else {}
    card["description"] = token.description
    card["creator_prior_launches"] = research.creator_prior_launches if research else 0
    card["creator_prior_wins"] = research.creator_prior_wins if research else 0
    card["creator_prior_rugs"] = research.creator_prior_rugs if research else 0
    card["creator_hold_pct"] = research.creator_hold_pct if research else 0
    card["x_mentions_1h"] = research.x_mentions_1h if research else 0
    holders = (card.get("raw") or {}).get("holders") or {}
    card["top_wallets"] = holders.get("top_wallets") or []
    card["top1_pct"] = holders.get("top1_pct") or 0
    card["fresh_wallet_pct"] = holders.get("fresh_wallet_pct") or 0
    card["holder_source"] = holders.get("source") or ""
    # Live MEME: Blockscout froze 4 LP rows; stored GMGN has 15k+.
    # Show the live book on the detail card. Hunt still uses the
    # frozen sample so copycat-runner filters stay honest.
    raw_gmgn = (card.get("raw") or {}).get("gmgn") or {}
    gmgn_n = int(raw_gmgn.get("holder_count") or 0)
    stored_n = int(card.get("holder_count") or 0)
    if gmgn_n > stored_n:
        from .scoring.features import rh_lp_open_book

        if rh_lp_open_book(holders if isinstance(holders, dict) else {}):
            card["holder_count"] = gmgn_n
            card["holder_source"] = "gmgn"
            if raw_gmgn.get("top10_pct"):
                card["top10_pct"] = raw_gmgn["top10_pct"]
            if raw_gmgn.get("fresh_wallet_pct") is not None:
                card["fresh_wallet_pct"] = raw_gmgn.get("fresh_wallet_pct") or 0
    gmgn = (card.get("raw") or {}).get("gmgn") or {}
    card["gmgn"] = {
        "present": bool(gmgn.get("source")),
        "honeypot": bool(gmgn.get("honeypot")),
        "bundled": bool(gmgn.get("bundled")),
        "insider_pct": gmgn.get("insider_pct") or 0,
        "sniper_pct": gmgn.get("sniper_pct") or 0,
        "rug_risk": gmgn.get("rug_risk") or 0,
        "url": gmgn.get("url") or card["links"]["gmgn"],
        "og": bool(gmgn.get("og")),
        "buy_tax_pct": gmgn.get("buy_tax_pct") or 0,
        "sell_tax_pct": gmgn.get("sell_tax_pct") or 0,
        "progress": gmgn.get("progress") or 0,
        "lock_pct": gmgn.get("lock_pct") or 0,
        "burned": bool(gmgn.get("burned")),
        "dev_hold_pct": gmgn.get("dev_hold_pct") or 0,
        "top10_pct": gmgn.get("top10_pct") or 0,
        "dev_sold": bool(gmgn.get("dev_sold")),
        "creator_status": gmgn.get("creator_status") or "",
        "hot_level": gmgn.get("hot_level") or 0,
        "swaps_1h": gmgn.get("swaps_1h") or 0,
        "volume_1h": gmgn.get("volume_1h") or 0,
        "net_buy_24h": gmgn.get("net_buy_24h") or 0,
        "cto": bool(gmgn.get("cto")),
        "open_source": bool(gmgn.get("open_source")),
        "twitter_username": gmgn.get("twitter_username") or "",
        "twitter_followers": gmgn.get("twitter_followers") or 0,
        "launchpad": gmgn.get("launchpad") or "",
        "smart_degen": gmgn.get("smart_degen") or 0,
        "renowned": gmgn.get("renowned") or 0,
    }
    card["snapshots"] = [
        {
            "kind": s.kind,
            "taken_at": s.taken_at.isoformat() if s.taken_at else None,
            "mcap_usd": s.mcap_usd,
            "price_usd": s.price_usd,
            "volume_h1": s.volume_h1,
            "liquidity_usd": s.liquidity_usd,
            "p_good": s.p_good,
        }
        for s in sorted(token.snapshots, key=lambda x: x.taken_at)
    ]
    return card
