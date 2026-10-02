"""Late-bloom funnel: watch a migrate after the first score.

Entry ``research.p_good`` stays frozen for paper and training. This
module writes a separate promise score from live tape (holders, liq,
volume, multiple) for up to a week, then Telegram + thesis when a
weak start starts looking like a runner. A near-line bloom hunts a
public X/dev once and folds that into the thesis — not into entry p.

FEATURE_NAMES stays 66. No extra GMGN HTTP. Do not raise
MAX_HONEST_MULTIPLE. Leftover FDV / dead pools never bloom.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Outcome, Research, ScanState, Token
from ..research.dev_dive import dive_developer, should_dive_public_dev
from ..research.github import launch_signal, lookup_repo
from ..social import extract_github

log = logging.getLogger("launchfinder.bloom")

# Keep in sync with outcomes.MAX_HONEST_MULTIPLE / DEAD_POOL_LIQ.
MAX_HONEST_MULTIPLE = 80.0
DEAD_POOL_LIQ = 800.0
BLOOM_KEY_PREFIX = "bloom:"
BLOOM_MIN_AGE = timedelta(minutes=20)
BLOOM_MIN_MULTIPLE = 1.4
BLOOM_MIN_LIQ = 3_000.0
BLOOM_MIN_HOLDERS_SOL = 40
BLOOM_MIN_HOLDERS_RH = 30
# Same under-t0 cut as hunt pick_live_conviction. A 0.06× rug is not a bloom.
BLOOM_UNDER_T0 = 0.85
# Doing well uses 40% of ATH. A 9× wick now sitting at 1.5× / 16% of
# peak (HOUSECAT / APU) is a recap, not a live book.
BLOOM_ATH_HOLD = 0.40
# Copycat spam is a brand/ticker flood, not a late bloom. Hijack is a
# stolen celebrity X. Both can still print a 1.4× book with fat liq.
BLOOM_SCAM_NEEDLES = (
    "honeypot",
    "wash trading",
    "hijack",
    "banned",
    "start-high",
    "pre-pumped",
    "copycat spam",
)
# Live USWR: 691 wallets / 96% top10. The <80-holder cut treated
# that sybil farm as a broad book and printed Bloom 58.
BLOOM_BUNDLE_TOP10 = 90.0
# Live bubble: leftover July pair, 1090 dust wallets, top10 67%,
# one EOA 64.5%. Hunt live printed 71–80 on a two-point Dex line.
# Need both the huge-share flag and a still-tight top10 so a later
# distribute does not stay capped on a stale flag. FEATURE_NAMES 66.
BLOOM_WHALE_TOP10 = 55.0


def bloom_scam_book(flags: list[str] | None) -> bool:
    joined = " | ".join(str(f) for f in (flags or [])).lower()
    return any(stop in joined for stop in BLOOM_SCAM_NEEDLES)


def bloom_bundle_farm(top10_pct: float, holders: float = 0.0) -> bool:
    """Top10 still a bundle even when holder count is padded."""
    del holders  # count is the lie; top10 is the book.
    return float(top10_pct or 0.0) >= BLOOM_BUNDLE_TOP10


def bloom_whale_book(top10_pct: float, flags: list[str] | None = None) -> bool:
    """One EOA is the book. Live bubble leftover whale tape.

    RH MEME fair LP and NINA do not carry this flag. Do not use the
    pair clock — RH leftover chairs stay on first_seen.
    """
    if float(top10_pct or 0.0) < BLOOM_WHALE_TOP10:
        return False
    joined = " | ".join(str(f) for f in (flags or [])).lower()
    return "one wallet holds a huge share" in joined


def bloom_key(mint: str) -> str:
    return f"{BLOOM_KEY_PREFIX}{mint}"[:64]


def _age(token: Token, now: datetime | None = None) -> timedelta | None:
    start = token.migrated_at or token.first_seen_at
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - start


def _runner_row(session: Session, mint: str) -> dict:
    row = session.query(ScanState).filter(ScanState.key == f"runnerp:{mint}").one_or_none()
    if not row:
        return {}
    try:
        return json.loads(row.value or "{}")
    except json.JSONDecodeError:
        return {}


def _load_bloom(session: Session, mint: str) -> tuple[ScanState | None, dict]:
    key = bloom_key(mint)
    row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
    if row is None:
        # autoflush is off — a row added earlier in this session is in
        # session.new, not yet visible to a fresh SELECT.
        for obj in list(session.new) + list(session.dirty):
            if isinstance(obj, ScanState) and obj.key == key:
                row = obj
                break
    if not row:
        return None, {}
    try:
        return row, json.loads(row.value or "{}")
    except json.JSONDecodeError:
        return row, {}


# First boot after bloom-dev tried to dive every near-line tape in one
# refresh and held the event loop. Cap HTTP hunts per cycle.
MAX_DIVES_PER_CYCLE = 2
MAX_GH_PER_CYCLE = 2
_dives_left = MAX_DIVES_PER_CYCLE
_gh_left = MAX_GH_PER_CYCLE


def begin_bloom_cycle() -> None:
    global _dives_left, _gh_left
    _dives_left = MAX_DIVES_PER_CYCLE
    _gh_left = MAX_GH_PER_CYCLE


def leftover_fdv(chain: str, t0: float, last: float) -> bool:
    if chain != "sol" or t0 <= 0 or last <= 0:
        return False
    return (last / t0) > MAX_HONEST_MULTIPLE


def bloom_tape_alive(chain: str, last_mcap: float, t0_mcap: float, last_liq: float) -> bool:
    """Bloom is a live-promise tab. Dumps, dead pools, leftover FDV do not stay."""
    if leftover_fdv(chain, t0_mcap, last_mcap):
        return False
    if 0 < last_liq < DEAD_POOL_LIQ:
        return False
    if t0_mcap > 0 and last_mcap > 0 and last_mcap / t0_mcap < BLOOM_UNDER_T0:
        return False
    return True


def promise_score(
    *,
    chain: str,
    entry_p: float,
    multiple: float,
    last_mcap: float,
    t0_mcap: float,
    max_mcap: float,
    last_liq: float,
    holders: int,
    top10_pct: float,
    vol_h1: float,
    runner_p: float | None,
    second_leg: bool,
    label: int | None,
    flags: list[str],
    holder_prev: int | None = None,
    holders_age_min: float | None = None,
    mode: str = "bloom",
    entry_legacy: float | None = None,
) -> tuple[float, list[str]]:
    """Live promise in [0, 0.95]. Does not touch FEATURE_NAMES.

    ``entry_legacy`` is the Entry read on the legacy 0.70 / 0.90 scale
    (``legacy_equivalent``) for the weak-entry test; ``entry_p`` stays the
    number the card shows. A first-sight 0.45 on RH is not a weak entry.

    ``mode="bloom"`` is the late-bloomer tab: last < 0.85× t0 is 0.
    ``mode="hunt"`` is Hunt Live: a fat Sol sitter under 0.85× still
    scores; collapsed dust still zeros. Thin-watch leftover caps live
    in ``conviction_from_tape``, not here.

    ``holder_prev`` is a stamp at least a few minutes old. Growing
    wallets can lift Live; shrinking wallets can fade it. A flat
    NINA-class hold does not fade. Stale counts (>45m) do not earn
    the holder bonuses.
    """
    reasons: list[str] = []
    hunt = mode == "hunt"
    joined = " | ".join(flags).lower()
    if leftover_fdv(chain, t0_mcap, last_mcap):
        return 0.0, ["Leftover FDV tape — not a real book"]
    if last_liq < DEAD_POOL_LIQ:
        return 0.0, ["Pool looks dead"]
    if multiple > MAX_HONEST_MULTIPLE:
        return 0.0, ["Multiple past the honest 80× cap"]
    if label == 0:
        return 0.0, ["Already labeled a loss"]
    if bloom_scam_book(flags):
        return 0.0, ["Hard-stop flag"]
    if bloom_bundle_farm(top10_pct, holders):
        return 0.0, ["Top10 still a bundle — not a bloom"]
    if bloom_whale_book(top10_pct, flags):
        return 0.0, ["One wallet is the book — not a bloom"]

    p = 0.22
    from_t0 = 0.0
    if t0_mcap > 0 and last_mcap > 0:
        from_t0 = last_mcap / t0_mcap
    elif multiple > 0:
        from_t0 = multiple
    if 0 < from_t0 < BLOOM_UNDER_T0:
        # Collapsed floors stay $25k / 0.50× — do not invent a new cut.
        if hunt:
            if 0 < last_mcap < 25_000 and from_t0 < 0.50:
                return 0.0, ["Collapsed / dust tape — not a live book"]
        else:
            return 0.0, ["Trading under entry — not a bloom"]
    live_x = from_t0 if from_t0 > 0 else multiple
    peak = max(float(max_mcap or 0.0), float(last_mcap or 0.0))
    held = (last_mcap / peak) if peak > 0 and last_mcap > 0 else 1.0
    # ATH multiple bonuses used to keep HOUSECAT at 76 after a 9× wick
    # dumped to 16% of peak. Runner bars need the live tape.
    if held < BLOOM_ATH_HOLD and 0 < live_x < 2.0:
        return 0.28, ["Dumped off ATH — recap, not a live book"]
    if live_x >= BLOOM_MIN_MULTIPLE:
        p += 0.08
        reasons.append(f"Live tape is {live_x:.2f}× from t0")
    if live_x >= 2.0:
        p += 0.10
        reasons.append("Held a 2× print")
    if live_x >= 3.0:
        p += 0.07
    if live_x >= 5.0:
        p += 0.05
        reasons.append("Crossed the 5× runner bar")
    if last_liq >= BLOOM_MIN_LIQ:
        p += 0.06
        reasons.append(f"Liquidity ${last_liq:,.0f}")
    if last_liq >= 20_000:
        p += 0.06
        reasons.append("Book has real depth ($20k+ liq)")
    min_holders = BLOOM_MIN_HOLDERS_RH if chain == "robinhood" else BLOOM_MIN_HOLDERS_SOL
    holders_fresh = holders_age_min is None or float(holders_age_min) <= 45.0
    if holders_fresh and holders >= min_holders:
        p += 0.06
        reasons.append(f"{holders} holders")
    if holders_fresh and holders >= 100:
        p += 0.05
        reasons.append("Holder count is broadening")
    prev_n = int(holder_prev or 0)
    if holders_fresh and prev_n > 0:
        if holders >= prev_n + 15 and holders >= prev_n * 1.12:
            p += 0.06
            reasons.append(f"Holders growing {prev_n}→{holders}")
        elif prev_n >= 20 and holders < prev_n * 0.85:
            p -= 0.08
            reasons.append("Holder count is shrinking")
    if 0 < top10_pct < 40:
        p += 0.06
        reasons.append(f"Top10 {top10_pct:.0f}% — supply not a bundle")
    elif 0 < top10_pct < 55:
        p += 0.03
    elif top10_pct >= 85 and holders < 80:
        p -= 0.12
        reasons.append("Supply still concentrated")
    if vol_h1 >= 10_000:
        p += 0.05
        reasons.append(f"1h volume ${vol_h1:,.0f}")
    if vol_h1 >= 50_000:
        p += 0.04
    if peak > 0 and last_mcap > 0:
        if held >= 0.55:
            p += 0.05
            reasons.append("Still holding most of the ATH")
        elif held < 0.35:
            p -= 0.14
            reasons.append("Dumped off ATH")
    if "dumping on real volume" in joined:
        p -= 0.20
        reasons.append("First-hour tape is dumping")
    if "wallet behind prior rugs" in joined or "serial rug" in joined:
        p -= 0.10
        reasons.append("Funder has prior rugs")
    if second_leg:
        p += 0.08
        reasons.append("Second-leg recovery after a dip")
    if runner_p is not None and runner_p >= 0.6:
        p += 0.08
        reasons.append(f"Runner trajectory {runner_p:.0%}")
    if (
        not hunt
        and (entry_legacy if entry_legacy is not None else entry_p) < 0.45
        and live_x >= BLOOM_MIN_MULTIPLE
        and last_liq >= BLOOM_MIN_LIQ
    ):
        p += 0.05
        reasons.append(f"Entry score was only {entry_p:.0%} — tape improved after migrate")
    return max(0.0, min(0.95, round(p, 4))), reasons


def bloom_thesis(
    *,
    symbol: str,
    chain: str,
    entry_p: float,
    promise_p: float,
    age: timedelta,
    multiple: float,
    last_mcap: float,
    t0_mcap: float,
    last_liq: float,
    holders: int,
    top10_pct: float,
    reasons: list[str],
    flags: list[str],
    dev_line: str = "",
) -> str:
    hours = max(0.0, age.total_seconds() / 3600.0)
    age_s = f"{hours:.1f}h" if hours >= 1 else f"{age.total_seconds() / 60.0:.0f}m"
    bits = [
        f"{symbol} launched on {chain} with entry p(good)={entry_p:.0%}.",
        f"After {age_s} the live promise is {promise_p:.0%} on a {multiple:.2f}× tape "
        f"(${last_mcap:,.0f} vs t0 ${t0_mcap:,.0f}).",
        f"Liquidity ${last_liq:,.0f}, {holders} holders, top10 {top10_pct:.0f}%.",
    ]
    if reasons:
        bits.append("Why now: " + "; ".join(reasons[:8]) + ".")
    if (dev_line or "").strip():
        bits.append(dev_line.strip())
    risks = [f for f in flags if f][:3]
    if risks:
        bits.append("Still watching: " + "; ".join(risks) + ".")
    else:
        bits.append("No hard-stop flags on the stored card.")
    return " ".join(bits)


def should_alert(
    entry_p: float,
    promise_p: float,
    already: bool,
    min_promise: float,
    *,
    multiple: float = 0.0,
    last_liq: float = 0.0,
) -> bool:
    if already or promise_p < min_promise:
        return False
    # Late bloomer (the requested funnel) or a clear live upgrade.
    if entry_p < min_promise or promise_p >= entry_p + 0.12:
        return True
    # High-entry names never reach entry+0.12 because promise caps at 0.95
    # (live MIZO 17× / 0.92 entry never Telegram'd). A real 5×+ book after
    # migrate is still a bloom ping. Do not rewrite entry p_good.
    return multiple >= 5.0 and last_liq >= BLOOM_MIN_LIQ


async def consider_bloom(
    session: Session,
    token: Token,
    outcome: Outcome,
    market: dict | None = None,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Refresh promise. Telegram once when the live tape turns promising.

    Never writes ``research.p_good``.
    """
    if token.is_historical or token.source == "backfill" or not token.research:
        return None
    age = _age(token, now)
    if age is None:
        return None
    watch = timedelta(hours=float(settings.bloom_watch_hours or 168))
    if age > watch or age < BLOOM_MIN_AGE:
        return None

    research: Research = token.research
    chain = (token.chain or "sol").strip().lower() or "sol"
    entry_p = float(research.p_good or 0.0)
    from ..desk_lines import legacy_equivalent, lines_for_scorer

    # Bloom compares live promise (legacy-ish scale) with Entry: read a
    # first-sight Entry through its own lines or every card is a "late bloomer".
    entry_legacy = legacy_equivalent(entry_p, lines_for_scorer(research.scorer, chain))
    t0 = float(outcome.t0_mcap or 0.0)
    last = float(outcome.last_mcap or outcome.max_mcap or 0.0)
    mx = float(outcome.max_mcap or last or 0.0)
    multiple = float(outcome.multiple or 0.0)
    if multiple <= 0 and t0 > 0 and last > 0:
        multiple = last / t0
    liq = float((market or {}).get("liquidity_usd") or outcome.last_liq or 0.0)
    from ..research.holders import holder_tape

    tape_h = holder_tape(research)
    holders = int(tape_h.get("n") or research.holder_count or 0)
    top10 = float(research.top10_pct or 0.0)
    vol = float((market or {}).get("volume_h1") or 0.0)
    try:
        flags = json.loads(research.risk_flags_json or "[]")
    except json.JSONDecodeError:
        flags = []
    runner = _runner_row(session, token.mint)
    runner_p = runner.get("runner_p")
    runner_p = float(runner_p) if runner_p is not None else None
    second_leg = bool(runner.get("second_leg"))

    leftover = leftover_fdv(chain, t0, last)
    promise, reasons = promise_score(
        chain=chain,
        entry_p=entry_p,
        multiple=multiple,
        last_mcap=last,
        t0_mcap=t0,
        max_mcap=mx,
        last_liq=liq,
        holders=holders,
        top10_pct=top10,
        vol_h1=vol,
        runner_p=runner_p,
        second_leg=second_leg,
        label=outcome.label,
        holder_prev=tape_h.get("prev"),
        holders_age_min=tape_h.get("age_min"),
        flags=[str(f) for f in flags],
        entry_legacy=entry_legacy,
    )
    row, stored = _load_bloom(session, token.mint)
    extra_line = str(stored.get("dev_line") or "")
    min_promise = float(settings.bloom_min_promise or 0.58)
    global _dives_left, _gh_left
    # Hunt a public X/dev only when this tick is about to (or already
    # did) look like a bloom — not every 0.40 tape in the refresh chair.
    dive_floor = max(0.50, min_promise - 0.06)
    if (
        not leftover
        and not stored.get("dev_dived")
        and promise >= dive_floor
        and _dives_left > 0
    ):
        if not should_dive_public_dev(token, research):
            stored["dev_dived"] = True
            stored["dev_handle"] = ""
            stored["dev_tier"] = "skip"
            stored["dev_github"] = ""
            stored["dev_line"] = ""
        else:
            _dives_left -= 1
            try:
                dive = await dive_developer(token, research)
            except Exception:
                log.exception("dev dive failed for %s", token.mint)
                dive = {}
            await asyncio.sleep(0)
            stored["dev_dived"] = True
            stored["dev_handle"] = dive.get("handle") or ""
            stored["dev_tier"] = dive.get("tier") or "none"
            stored["dev_github"] = dive.get("github") or ""
            stored["dev_line"] = dive.get("line") or ""
            extra_line = str(stored["dev_line"] or "")
            promise = max(0.0, min(0.95, promise + float(dive.get("delta") or 0.0)))
            reasons.extend(dive.get("reasons") or [])
    gh_url = (token.github_url or str(stored.get("dev_github") or "")).strip()
    if (
        not leftover
        and not stored.get("gh_refreshed")
        and promise >= dive_floor
        and _gh_left > 0
        and gh_url
    ):
        _url, ref = extract_github(gh_url)
        if ref:
            _gh_left -= 1
            stored["gh_refreshed"] = True
            try:
                gh = await lookup_repo(ref)
            except Exception:
                log.exception("bloom github refresh failed for %s", token.mint)
                gh = {}
            await asyncio.sleep(0)
            sig = launch_signal(gh, launched_at=token.migrated_at or token.first_seen_at)
            stored["gh_line"] = sig.get("line") or ""
            promise = max(0.0, min(0.95, promise + float(sig.get("delta") or 0.0)))
            reasons.extend(sig.get("reasons") or [])
            if stored["gh_line"]:
                extra_line = " ".join(part for part in (extra_line, stored["gh_line"]) if part)
        else:
            stored["gh_refreshed"] = True
    elif stored.get("gh_line") and stored["gh_line"] not in extra_line:
        extra_line = " ".join(part for part in (extra_line, stored.get("gh_line")) if part)
    from ..research.live_social import fold_live_social

    promise, social_reasons = fold_live_social(session, token.mint, promise)
    reasons.extend(social_reasons)
    thesis = bloom_thesis(
        symbol=token.symbol or token.mint[:8],
        chain=chain,
        entry_p=entry_p,
        promise_p=promise,
        age=age,
        multiple=multiple,
        last_mcap=last,
        t0_mcap=t0,
        last_liq=liq,
        holders=holders,
        top10_pct=top10,
        reasons=reasons,
        flags=[str(f) for f in flags],
        dev_line=extra_line,
    )
    already = bool(stored.get("alerted"))
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    payload = {
        "mint": token.mint,
        "chain": chain,
        "promise_p": promise,
        "entry_p": entry_p,
        "multiple": round(multiple, 4),
        "last_mcap": last,
        "t0_mcap": t0,
        "last_liq": liq,
        "thesis": thesis,
        "reasons": reasons[:10],
        "alerted": already,
        "alerted_at": stored.get("alerted_at"),
        "updated_at": stamp,
        "leftover_fdv": leftover,
        "dev_dived": bool(stored.get("dev_dived")),
        "dev_handle": stored.get("dev_handle") or "",
        "dev_tier": stored.get("dev_tier") or "",
        "dev_line": extra_line,
        "dev_github": stored.get("dev_github") or "",
        "gh_refreshed": bool(stored.get("gh_refreshed")),
        "gh_line": stored.get("gh_line") or "",
    }
    fire = should_alert(
        entry_legacy, promise, already, min_promise, multiple=multiple, last_liq=liq
    )
    if fire:
        from .alert_tiers import claim_ping
        from ..alerts import notify_bloom

        try:
            if not claim_ping(session, "bloom", token.mint):
                sent = False
            else:
                sent = await notify_bloom(
                    symbol=token.symbol or "",
                    name=token.name or "",
                    mint=token.mint,
                    promise_p=promise,
                    entry_p=entry_p,
                    mcap_usd=last,
                    thesis=thesis,
                    reasons=reasons,
                    flags=[str(f) for f in flags],
                    chain=chain,
                )
        except Exception:
            log.exception("bloom alert failed for %s", token.mint)
            sent = False
        if sent:
            payload["alerted"] = True
            payload["alerted_at"] = stamp
    if row is None:
        row = ScanState(key=bloom_key(token.mint))
        session.add(row)
    row.value = json.dumps(payload)
    row.updated_at = now or datetime.now(timezone.utc)
    return payload


def list_blooms(session: Session, chain: str, *, limit: int = 40) -> list[dict]:
    prefix = BLOOM_KEY_PREFIX
    rows = (
        session.query(ScanState)
        .filter(ScanState.key.startswith(prefix))
        .order_by(ScanState.updated_at.desc())
        .limit(200)
        .all()
    )
    out: list[dict] = []
    min_p = float(settings.bloom_min_promise or 0.58)
    for row in rows:
        try:
            data = json.loads(row.value or "{}")
        except json.JSONDecodeError:
            continue
        if (data.get("chain") or "sol") != chain:
            continue
        if float(data.get("promise_p") or 0.0) < min_p:
            continue
        if not bloom_tape_alive(
            chain,
            float(data.get("last_mcap") or 0.0),
            float(data.get("t0_mcap") or 0.0),
            float(data.get("last_liq") or 0.0),
        ):
            continue
        out.append(data)
        if len(out) >= limit:
            break
    return out
