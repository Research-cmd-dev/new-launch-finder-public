"""Same-day miss / loser cohort for desk-era Decisions.

Pairs winners (hit2×/5×) with same-UTC-day losers in a similar entry-mcap
band. Offline learning — no new launches required. Does not rewrite Decisions.

From stack-v203 every paperV1 taxonomy skip also feeds Learn: Sol names
first-seen early (wide gated fill or ``first_seen``) that later print ≥5×
join the paper-miss runner cohort. Side keys on Decision.features_json
auto-join future skips — no manual paste. Book/holders/wallets beat social.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import Decision, Outcome, PaperFill, Research, Token
from .early_book import (
    EARLY_BOOK_KEY,
    EARLY_BOOK_OPEN,
    early_runner_book,
    would_have_early_book,
)
from .meme_quality import PAPER_V1_SKIP_MEME_QUALITY
from .paper_v1 import (
    PAPER_V1_LEFTOVER_MCAP,
    PAPER_V1_LINE,
    PAPER_V1_SHADOW_LINE,
    PAPER_V1_SKIP_EARLY_BOOK,
    PAPER_V1_SKIP_LATE_CHASE,
    PAPER_V1_SKIP_LIVE_COLD,
    PAPER_V1_SKIP_NO_THESIS,
    PAPER_V1_SKIP_SCORE_MISS,
    close_reason_label,
    is_copycat_skip,
    v1_skip_family,
    v1_thesis_from_features,
)
from .thesis_weights import load_thesis_weights

BAND_LO = 10_000.0
BAND_HI = 500_000.0
WIN_MULT = 2.0
LOSS_MULT = 1.5

# Paper-miss runner cohort (Learn). Skip taxonomy + first-seen early + ≥5×.
PAPER_MISS_WIN_MULT = 5.0
PAPER_MISS_DUD_MULT = 1.5
PAPER_MISS_JOIN_KEY = "paper_miss_join"
PAPER_MISS_REASON_KEY = "paper_miss_reason"
PAPER_MISS_SNAP_KEY = "paper_miss_snap"
PAPER_MISS_AT_KEY = "paper_miss_at"
PAPER_MISS_SKIP_REASONS = frozenset(
    {
        PAPER_V1_SKIP_LIVE_COLD,
        PAPER_V1_SKIP_LATE_CHASE,
        PAPER_V1_SKIP_SCORE_MISS,
        PAPER_V1_SKIP_NO_THESIS,
        PAPER_V1_SKIP_MEME_QUALITY,
        PAPER_V1_SKIP_EARLY_BOOK,
    }
)
# Frozen at first skip. Prefer book/holders/wallets over social.
PAPER_MISS_SNAP_KEYS = (
    "holder_n",
    "top10_inv",
    "top1_inv",
    "creator_hold_inv",
    "fresh_wallet_n",
    "buy_pressure",
    "liquidity_n",
    "volume_n",
    "organic_book",
    "migrate_speed",
    "mcap_per_holder_n",
)
PAPER_MISS_RAW_KEYS = (
    "holders",
    "top10_pct",
    "top1_pct",
    "creator_hold_pct",
    "fresh_wallet_pct",
    "liq",
    "vol_h1",
    "volume_m5",
    "migrate_min",
    "mcap_per_holder",
)
PAPER_MISS_SOCIAL_KEYS = (
    "twitter_followers",
    "twitter_age_days",
    "twitter_tweets",
)
# Social is recorded on the autopsy, not used as a separator.
PAPER_MISS_SEPARATOR_KEYS = PAPER_MISS_SNAP_KEYS
PAPER_LINE_GATED = "gated90"

# Tonight's Sol late-chase 10× — book/holders were the tell, social was empty.
PAPER_MISS_FIXTURE_MINT = "7cYaQc21w9dzKkGP6LgkqtmL5yzoK5UJE1GeamTRHwny"
PAPER_MISS_FIXTURE: dict[str, Any] = {
    "id": "sol-test-late-chase-7cYa",
    "mint": PAPER_MISS_FIXTURE_MINT,
    "symbol": "test",
    "name": "test",
    "chain": "sol",
    "skip_reason": PAPER_V1_SKIP_LATE_CHASE,
    "t0_mcap": 39_500.0,
    "peak_mcap": 395_000.0,
    "multiple": 10.0,
    "first_seen": "migrate",
    "broadcast": "8472016",
    "social_is_tell": False,
    "note": (
        "First-seen at migrate ~$39.5k t0; sat, then ran ~10× while paperV1 "
        "waited Live/thesis. Labeled v1 late-chase. Social empty (12 fol, "
        "brand-new X, 0 tweets) — not the tell. Book/holders were interesting "
        "early: organic_book, ~$16k liq, m5 vol≈mcap, buys>sells, top10~24%, "
        "creator~2%, fresh wallets ~82%, migrate ~21m."
    ),
    "early": {
        "holder_n": 0.5933,
        "holders": 90,
        "top10_inv": 0.76,
        "top10_pct": 24.0,
        "top1_inv": 0.84,
        "top1_pct": 8.0,
        "creator_hold_inv": 0.975,
        "creator_hold_pct": 2.0,
        "fresh_wallet_n": 0.82,
        "fresh_wallet_pct": 82.0,
        "buy_pressure": 0.62,
        "liquidity_n": 0.8574,
        "liq": 16_000.0,
        "volume_n": 0.9782,
        "vol_h1": 39_500.0,
        "volume_m5": 39_500.0,
        "organic_book": 1.0,
        "migrate_speed": 0.9,
        "migrate_min": 21.0,
        "mcap_per_holder_n": 0.011,
        "mcap_per_holder": 439.0,
        "twitter_followers": 12,
        "twitter_age_days": 0.0,
        "twitter_tweets": 0,
    },
}


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _day(ts: datetime | None) -> str:
    at = _aware(ts)
    return at.date().isoformat() if at else ""


def _feat(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def paper_miss_reason(exit_reason: str | None) -> str:
    """Canonical skip label if it belongs to the paper-miss taxonomy."""
    label = close_reason_label(exit_reason or "")
    if label in PAPER_MISS_SKIP_REASONS:
        return label
    for reason in PAPER_MISS_SKIP_REASONS:
        if label.startswith(reason):
            return reason
    return ""


def is_paper_miss_reason(exit_reason: str | None) -> bool:
    return bool(paper_miss_reason(exit_reason))


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def freeze_paper_miss_features(
    feat: dict[str, Any] | None,
    *,
    decision: Decision | None = None,
    research: Research | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write-once-shaped early snapshot. Book/holders/wallets over social."""
    bits = feat if isinstance(feat, dict) else {}
    extra = extra if isinstance(extra, dict) else {}
    snap: dict[str, Any] = {}
    for key in PAPER_MISS_SNAP_KEYS:
        val = _num(bits.get(key) if bits.get(key) is not None else extra.get(key))
        if val is not None:
            snap[key] = round(val, 4)
    holders = None
    if decision is not None and int(decision.holders or 0) > 0:
        holders = int(decision.holders)
    elif research is not None and int(research.holder_count or 0) > 0:
        holders = int(research.holder_count)
    elif extra.get("holders") is not None:
        holders = int(extra["holders"] or 0)
    if holders:
        snap["holders"] = holders
    top10 = _num(
        extra.get("top10_pct")
        or (research.top10_pct if research is not None else None)
    )
    if top10 is None and _num(bits.get("top10_inv")) is not None:
        inv = float(bits["top10_inv"])
        if inv != 0.45:
            top10 = round((1.0 - inv) * 100.0, 2)
    if top10 is not None:
        snap["top10_pct"] = round(top10, 2)
    top1 = _num(extra.get("top1_pct"))
    if top1 is None and _num(bits.get("top1_inv")) is not None:
        inv = float(bits["top1_inv"])
        if inv != 0.5:
            top1 = round((1.0 - inv) * 50.0, 2)
    if top1 is not None:
        snap["top1_pct"] = round(top1, 2)
    creator = _num(
        extra.get("creator_hold_pct")
        or (research.creator_hold_pct if research is not None else None)
    )
    if creator is None and _num(bits.get("creator_hold_inv")) is not None:
        inv = float(bits["creator_hold_inv"])
        if inv != 0.5:
            creator = round((1.0 - inv) * 80.0, 2)
    if creator is not None:
        snap["creator_hold_pct"] = round(creator, 2)
    fresh = _num(extra.get("fresh_wallet_pct"))
    if fresh is None and _num(bits.get("fresh_wallet_n")) is not None:
        fresh = round(float(bits["fresh_wallet_n"]) * 100.0, 2)
    if fresh is not None:
        snap["fresh_wallet_pct"] = round(fresh, 2)
    liq = _num(
        extra.get("liq")
        or (decision.liq if decision is not None else None)
    )
    if liq is not None:
        snap["liq"] = round(liq, 1)
    vol = _num(
        extra.get("vol_h1")
        or (decision.vol_h1 if decision is not None else None)
    )
    if vol is not None:
        snap["vol_h1"] = round(vol, 1)
    vol_m5 = _num(extra.get("volume_m5"))
    if vol_m5 is not None:
        snap["volume_m5"] = round(vol_m5, 1)
    migrate = _num(
        extra.get("migrate_min")
        or (research.time_to_migrate_min if research is not None else None)
    )
    if migrate is not None:
        snap["migrate_min"] = round(migrate, 1)
    mph = _num(extra.get("mcap_per_holder"))
    if mph is None and holders and extra.get("t0_mcap"):
        t0 = _num(extra.get("t0_mcap"))
        if t0:
            mph = t0 / float(holders)
    if mph is not None:
        snap["mcap_per_holder"] = round(mph, 1)
    for key in PAPER_MISS_SOCIAL_KEYS:
        val = extra.get(key)
        if val is None and research is not None:
            val = {
                "twitter_followers": research.twitter_followers,
                "twitter_age_days": research.twitter_age_days,
                "twitter_tweets": research.twitter_tweets,
            }.get(key)
        if val is not None:
            try:
                snap[key] = int(val) if key != "twitter_age_days" else round(float(val), 1)
            except (TypeError, ValueError):
                continue
    return snap


def stamp_paper_miss_join(
    features: dict[str, Any] | None,
    *,
    reason: str,
    snap: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Write-once side keys so a skip auto-joins the paper-miss cohort.

    Does not grow FEATURE_NAMES. Returns None when the snap is already frozen.
    """
    feat = dict(features) if isinstance(features, dict) else {}
    if feat.get(PAPER_MISS_JOIN_KEY) and isinstance(feat.get(PAPER_MISS_SNAP_KEY), dict):
        return None
    label = paper_miss_reason(reason)
    if not label:
        return None
    feat[PAPER_MISS_JOIN_KEY] = True
    feat[PAPER_MISS_REASON_KEY] = label
    if snap:
        feat[PAPER_MISS_SNAP_KEY] = dict(snap)
    when = at or datetime.now(timezone.utc)
    feat[PAPER_MISS_AT_KEY] = when.isoformat()
    return feat


def has_paper_miss_join(features: dict[str, Any] | None) -> bool:
    feat = features if isinstance(features, dict) else {}
    return bool(feat.get(PAPER_MISS_JOIN_KEY))


def is_first_seen_early(
    token: Token | None,
    *,
    t0_mcap: float = 0.0,
    has_wide_fill: bool = False,
) -> bool:
    """True when the desk saw the name at migrate / first print, not leftover."""
    if t0_mcap >= PAPER_V1_LEFTOVER_MCAP:
        return False
    if has_wide_fill:
        return True
    if token is None:
        return False
    if token.first_seen_at is not None:
        return True
    if token.migrated_at is not None:
        return True
    return False


def paper_miss_multiple(
    *,
    t0_mcap: float,
    peak_mcap: float,
    last_liq: float = 0.0,
    sellable_liq: float = 5_000.0,
) -> float:
    """Peak / t0. Sellable ≥5× from t0 counts even when last is thin."""
    t0 = float(t0_mcap or 0.0)
    peak = float(peak_mcap or 0.0)
    if t0 <= 0 or peak <= 0:
        return 0.0
    mult = peak / t0
    if last_liq >= float(sellable_liq) or peak >= PAPER_MISS_WIN_MULT * t0:
        return mult
    return mult


def paper_miss_membership(
    *,
    chain: str,
    skip_reason: str | None,
    features: dict[str, Any] | None,
    t0_mcap: float,
    peak_mcap: float,
    last_liq: float = 0.0,
    token: Token | None = None,
    has_wide_fill: bool = False,
    require_runner: bool = True,
) -> bool:
    """Sol paper skip (taxonomy or side-key) first-seen early; optional ≥5×."""
    if normalize_chain(chain) != "sol":
        return False
    joined = has_paper_miss_join(features) or is_paper_miss_reason(skip_reason)
    if not joined:
        return False
    if not is_first_seen_early(token, t0_mcap=t0_mcap, has_wide_fill=has_wide_fill):
        return False
    if not require_runner:
        return True
    return paper_miss_multiple(t0_mcap=t0_mcap, peak_mcap=peak_mcap, last_liq=last_liq) >= PAPER_MISS_WIN_MULT


def paper_miss_autopsy(
    *,
    mint: str,
    symbol: str = "",
    skip_reason: str = "",
    t0_mcap: float = 0.0,
    peak_mcap: float = 0.0,
    multiple: float | None = None,
    early: dict[str, Any] | None = None,
    note: str = "",
    chain: str = "sol",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-name Learn card. Book/holders first; social is a weak contrast."""
    snap = dict(early) if isinstance(early, dict) else {}
    t0 = float(t0_mcap or 0.0)
    peak = float(peak_mcap or 0.0)
    mult = float(multiple) if multiple is not None else (
        (peak / t0) if t0 > 0 and peak > 0 else 0.0
    )
    social = {
        "twitter_followers": snap.get("twitter_followers"),
        "twitter_age_days": snap.get("twitter_age_days"),
        "twitter_tweets": snap.get("twitter_tweets"),
        "is_tell": False,
    }
    book = {k: snap.get(k) for k in (
        "organic_book",
        "liq",
        "liquidity_n",
        "volume_n",
        "vol_h1",
        "volume_m5",
        "buy_pressure",
        "holders",
        "holder_n",
        "top10_pct",
        "top1_pct",
        "creator_hold_pct",
        "fresh_wallet_pct",
        "migrate_min",
        "migrate_speed",
        "mcap_per_holder",
    ) if snap.get(k) is not None}
    card = {
        "mint": mint,
        "symbol": symbol or "",
        "chain": normalize_chain(chain),
        "skip_reason": paper_miss_reason(skip_reason) or skip_reason,
        "t0_mcap": round(t0) if t0 else None,
        "peak_mcap": round(peak) if peak else None,
        "multiple": round(mult, 2) if mult else None,
        "hit5x": bool(mult >= PAPER_MISS_WIN_MULT),
        "early": snap,
        "book": book,
        "social": social,
        "side_key": PAPER_MISS_JOIN_KEY,
        "paper_only": True,
        "note": note or (
            "Paper miss autopsy. Book/holders/wallets over social. Not a buy."
        ),
    }
    if extra:
        card.update(extra)
    return card


def paper_miss_fixture_autopsy() -> dict[str, Any]:
    fx = PAPER_MISS_FIXTURE
    return paper_miss_autopsy(
        mint=str(fx["mint"]),
        symbol=str(fx["symbol"]),
        skip_reason=str(fx["skip_reason"]),
        t0_mcap=float(fx["t0_mcap"]),
        peak_mcap=float(fx["peak_mcap"]),
        multiple=float(fx["multiple"]),
        early=dict(fx["early"]),
        note=str(fx["note"]),
        extra={
            "fixture": True,
            "first_seen": fx["first_seen"],
            "broadcast": fx["broadcast"],
            "social_is_tell": False,
        },
    )


def _separator_rows(
    winners: list[dict[str, Any]],
    duds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mean delta of frozen early keys. Book/holders rank above social."""
    from .signal_score import feature_means

    keys = PAPER_MISS_SEPARATOR_KEYS
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
                "prefer": "book_holders_wallets",
            }
        )
    rows.sort(key=lambda r: -float(r["abs_delta"]))
    return rows


def paper_miss_learn(
    session: Session,
    chain: str = "sol",
    *,
    days: int = 14,
    now: datetime | None = None,
    autopsy_limit: int = 24,
) -> dict[str, Any]:
    """PaperV1 skip → 5×+ runner cohort + winner-vs-dud separators.

    Learn / shadow only. Does not open fills. Sol first-seen early names
    with taxonomy skip (or ``paper_miss_join``) and later peak ≥5×.
    """
    chain = normalize_chain(chain)
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=max(1, int(days)))
    fills = (
        session.query(PaperFill, Token, Outcome, Research)
        .join(Token, Token.id == PaperFill.token_id)
        .outerjoin(Outcome, Outcome.token_id == Token.id)
        .outerjoin(Research, Research.token_id == Token.id)
        .filter(
            PaperFill.chain == chain,
            PaperFill.line.in_((PAPER_V1_LINE, PAPER_V1_SHADOW_LINE)),
            PaperFill.opened_at >= since,
        )
        .order_by(PaperFill.id.desc())
        .limit(1200)
        .all()
    )
    wide_mints = {
        mint
        for (mint,) in session.query(PaperFill.mint)
        .filter(
            PaperFill.chain == chain,
            PaperFill.line == PAPER_LINE_GATED,
            PaperFill.opened_at >= since,
        )
        .all()
    }
    skip_counts: dict[str, int] = defaultdict(int)
    winners_feat: list[dict[str, Any]] = []
    duds_feat: list[dict[str, Any]] = []
    autopsies: list[dict[str, Any]] = []
    would_have_items: list[dict[str, Any]] = []
    n_joined = 0
    n_runners = 0
    n_duds = 0
    n_copycat_suppressed = 0
    n_si_suppressed = 0
    for fill, token, outcome, research in fills:
        decision = None
        if fill.decision_id:
            decision = session.get(Decision, fill.decision_id)
        if decision is None:
            decision = (
                session.query(Decision)
                .filter(Decision.token_id == token.id, Decision.kind == "entry")
                .order_by(Decision.id.desc())
                .first()
            )
        feat = _feat(decision.features_json if decision is not None else None)
        if research is not None and not feat:
            feat = _feat(research.features_json)
        snap = feat.get(PAPER_MISS_SNAP_KEY)
        if not isinstance(snap, dict):
            snap = freeze_paper_miss_features(feat, decision=decision, research=research)
        reason = paper_miss_reason(fill.exit_reason) or str(
            feat.get(PAPER_MISS_REASON_KEY) or ""
        )
        family = v1_skip_family(fill.exit_reason or reason)
        if family == "copycat" or is_copycat_skip(fill.exit_reason):
            n_copycat_suppressed += 1
            continue
        if family == "si_printer":
            n_si_suppressed += 1
            continue
        t0 = float(
            (outcome.t0_mcap if outcome else 0.0)
            or (decision.entry_mcap if decision is not None else 0.0)
            or fill.entry_mcap
            or 0.0
        )
        peak = float(
            (outcome.max_mcap if outcome else 0.0)
            or fill.max_mcap
            or 0.0
        )
        last_liq = float(
            (outcome.last_liq if outcome else 0.0) or fill.last_liq or 0.0
        )
        has_wide = token.mint in wide_mints
        joined = paper_miss_membership(
            chain=chain,
            skip_reason=fill.exit_reason or reason,
            features=feat,
            t0_mcap=t0,
            peak_mcap=peak,
            last_liq=last_liq,
            token=token,
            has_wide_fill=has_wide,
            require_runner=False,
        )
        if not joined:
            continue
        n_joined += 1
        if reason:
            skip_counts[reason] += 1
        mult = paper_miss_multiple(t0_mcap=t0, peak_mcap=peak, last_liq=last_liq)
        if mult >= PAPER_MISS_WIN_MULT:
            n_runners += 1
            winners_feat.append(snap)
            card = paper_miss_autopsy(
                mint=token.mint,
                symbol=token.symbol or "",
                skip_reason=reason or fill.exit_reason or "",
                t0_mcap=t0,
                peak_mcap=peak,
                multiple=mult,
                early=snap,
            )
            if len(autopsies) < int(autopsy_limit):
                autopsies.append(card)
            if early_runner_book(snap, t0_mcap=t0).get("ok") and len(would_have_items) < 12:
                card = dict(card)
                card["would_have"] = True
                card[EARLY_BOOK_KEY] = True
                would_have_items.append(card)
        elif 0 < mult < PAPER_MISS_DUD_MULT:
            n_duds += 1
            duds_feat.append(snap)

    fixture = paper_miss_fixture_autopsy()
    fixture_in = any(a.get("mint") == PAPER_MISS_FIXTURE_MINT for a in autopsies)
    if not fixture_in:
        autopsies.insert(0, fixture)
        # Fixture is a winner example even when the live row is absent.
        winners_feat.append(dict(PAPER_MISS_FIXTURE["early"]))
        n_runners += 1
        n_joined += 1
        skip_counts[PAPER_V1_SKIP_LATE_CHASE] += 1

    if early_runner_book(fixture.get("early"), t0_mcap=float(fixture.get("t0_mcap") or 0)).get("ok"):
        if not any(a.get("mint") == PAPER_MISS_FIXTURE_MINT for a in would_have_items):
            fx_card = dict(fixture)
            fx_card["would_have"] = True
            fx_card[EARLY_BOOK_KEY] = True
            would_have_items.insert(0, fx_card)

    from .fomo_trend_no_hunt import FOMO_TREND_NO_HUNT_KEY, fomo_trend_no_hunt_learn

    from .copycat_learn import copycat_veto_cohort

    copycat = copycat_veto_cohort(session, chain, days=days, now=now, win_mult=PAPER_MISS_WIN_MULT)
    fomo_no_hunt = fomo_trend_no_hunt_learn(session, chain, days=days, now=now)
    fomo_runner_mints = {
        str(r.get("mint") or "")
        for r in (fomo_no_hunt.get("runners") or [])
        if r.get("mint")
    }
    for card in autopsies:
        if card.get("mint") in fomo_runner_mints:
            card[FOMO_TREND_NO_HUNT_KEY] = True
            card["fomo_no_hunt_cross_link"] = True
    extra_fomo = [
        dict(r, paper_miss_cross_link=True)
        for r in (fomo_no_hunt.get("runners") or [])
        if r.get("mint") and r.get("mint") != PAPER_MISS_FIXTURE_MINT
        and not any(a.get("mint") == r.get("mint") for a in autopsies)
    ]
    for card in extra_fomo:
        if len(autopsies) >= int(autopsy_limit) + 8:
            break
        autopsies.append(card)

    separators = _separator_rows(winners_feat, duds_feat)
    would_have = would_have_early_book(winners_feat, duds_feat)
    would_have["items"] = would_have_items
    would_have["open"] = bool(EARLY_BOOK_OPEN)
    return {
        "paper_only": True,
        "side_key": PAPER_MISS_JOIN_KEY,
        "early_book_key": EARLY_BOOK_KEY,
        "chain": chain,
        "days": int(days),
        "win_multiple": PAPER_MISS_WIN_MULT,
        "dud_multiple": PAPER_MISS_DUD_MULT,
        "skip_reasons": sorted(PAPER_MISS_SKIP_REASONS),
        "n_joined": n_joined,
        "n_runners": n_runners,
        "n_duds": n_duds,
        "n_copycat_suppressed": n_copycat_suppressed,
        "n_si_suppressed": n_si_suppressed,
        "by_skip": dict(sorted(skip_counts.items(), key=lambda kv: -kv[1])),
        "separators": separators[:8],
        "autopsies": autopsies,
        "would_have": would_have,
        "fixture": fixture,
        "fomo_trend_no_hunt": fomo_no_hunt,
        "copycat_veto": copycat,
        "note": (
            "Every paperV1 taxonomy skip stamps paper_miss_join so it auto-joins "
            "this Learn cohort. Runners = first-seen early Sol names that later "
            "printed ≥5× after v1 live-cold / late-chase / score-miss / "
            "no-thesis / meme-q / early-book. Copycat and SI-printer shadows "
            "are excluded from runner/dud denominators. Separators prefer "
            "book/holders/wallets; social is not the tell. early_runner_book "
            "is a would-have shadow (open=false). FOMO trending→no-Hunt names "
            "that later print ≥5× cross-link this same miss loop "
            "(fomo_trend_no_hunt; open=false). Copycat-vetoed winners are a "
            "separate grain (copycat_veto / gate_veto=copycat; www is "
            "evidence #1; would-have is shadow only). Not a buy list."
        ),
    }


def miss_cohort(
    session: Session,
    chain: str = "sol",
    *,
    days: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Winners vs losers vs paper skips on desk-era UTC days."""
    chain = normalize_chain(chain)
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=max(1, int(days)))
    weights = load_thesis_weights(session)

    from .copycat_learn import (
        GATE_VETO_COPYCAT,
        copycat_veto_cohort,
        copycat_veto_mints,
        feat_gate_veto,
    )

    copycat_mints = copycat_veto_mints(session, chain, since=since)
    copycat = copycat_veto_cohort(session, chain, days=days, now=now, win_mult=WIN_MULT)

    decisions = (
        session.query(Decision, Outcome, Token, Research)
        .join(Token, Token.id == Decision.token_id)
        .outerjoin(Outcome, Outcome.token_id == Decision.token_id)
        .outerjoin(Research, Research.token_id == Decision.token_id)
        .filter(
            Decision.chain == chain,
            Decision.kind == "entry",
            Decision.source == "live",
            Decision.at >= since,
            Token.source.notin_(("backfill", "rh_backfill")),
        )
        .order_by(Decision.at.desc())
        .limit(4000)
        .all()
    )

    by_day: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"winners": [], "losers": [], "flat": []}
    )
    tag_w: dict[str, int] = defaultdict(int)
    tag_l: dict[str, int] = defaultdict(int)
    feat_w: list[dict[str, Any]] = []
    feat_l: list[dict[str, Any]] = []
    hard_w = hard_l = 0

    for decision, outcome, token, research in decisions:
        day = _day(decision.at)
        if not day:
            continue
        entry_m = float(decision.entry_mcap or 0.0)
        if entry_m < BAND_LO or entry_m > BAND_HI:
            continue
        feat = _feat(decision.features_json)
        feat_gate = feat_gate_veto(feat)
        thesis = v1_thesis_from_features(feat, weights=weights)
        peak = float((outcome.max_mcap if outcome else 0.0) or 0.0)
        mult = (peak / entry_m) if entry_m > 0 and peak > 0 else 0.0
        row = {
            "symbol": token.symbol or "",
            "mint": token.mint,
            "day": day,
            "entry_p": round(float(decision.entry_p or 0.0), 4),
            "entry_mcap": round(entry_m),
            "multiple": round(mult, 2) if mult else None,
            "tags": thesis.get("tags") or [],
            "thesis_score": thesis.get("score"),
            "holders": int(decision.holders or 0),
            "liq": round(float(decision.liq or 0.0)),
        }
        if token.mint in copycat_mints or feat_gate:
            row["gate_veto"] = (
                GATE_VETO_COPYCAT
                if (token.mint in copycat_mints or feat_gate == GATE_VETO_COPYCAT)
                else feat_gate
            )
        if mult >= WIN_MULT:
            by_day[day]["winners"].append(row)
            for t in row["tags"]:
                tag_w[t] += 1
            if thesis.get("hard_tags"):
                hard_w += 1
            feat_w.append(feat)
        elif mult > 0 and mult < LOSS_MULT:
            by_day[day]["losers"].append(row)
            for t in row["tags"]:
                tag_l[t] += 1
            if thesis.get("hard_tags"):
                hard_l += 1
            feat_l.append(feat)
        else:
            by_day[day]["flat"].append(row)

    # Paper skips / shadow on same window
    paper = (
        session.query(PaperFill, Token)
        .join(Token, Token.id == PaperFill.token_id)
        .filter(
            PaperFill.chain == chain,
            PaperFill.line.in_((PAPER_V1_LINE, PAPER_V1_SHADOW_LINE)),
            PaperFill.opened_at >= since,
        )
        .limit(800)
        .all()
    )
    skip_n = 0
    shadow_n = 0
    for fill, _token in paper:
        if fill.line == PAPER_V1_SHADOW_LINE:
            shadow_n += 1
        elif fill.status == "skipped":
            skip_n += 1

    days_out = []
    n_w = n_l = 0
    for day in sorted(by_day.keys(), reverse=True)[: max(1, int(days))]:
        bucket = by_day[day]
        n_w += len(bucket["winners"])
        n_l += len(bucket["losers"])
        days_out.append(
            {
                "day": day,
                "winners": len(bucket["winners"]),
                "losers": len(bucket["losers"]),
                "flat": len(bucket["flat"]),
                "winner_sample": bucket["winners"][:5],
                "loser_sample": bucket["losers"][:5],
            }
        )

    def _rate(tags: dict[str, int], n: int) -> dict[str, float]:
        if n <= 0:
            return {}
        return {k: round(v / n, 3) for k, v in sorted(tags.items())}

    from .signal_score import feature_means, v1_signal_from_features

    means_w = feature_means(feat_w)
    means_l = feature_means(feat_l)
    signal_w = [
        float(v1_signal_from_features(f).get("score") or 0.0) for f in feat_w[:400]
    ]
    signal_l = [
        float(v1_signal_from_features(f).get("score") or 0.0) for f in feat_l[:400]
    ]

    return {
        "chain": chain,
        "days": int(days),
        "band_usd": {"lo": BAND_LO, "hi": BAND_HI},
        "win_multiple": WIN_MULT,
        "loss_multiple": LOSS_MULT,
        "n_winners": n_w,
        "n_losers": n_l,
        "paper_skipped": skip_n,
        "paper_shadow": shadow_n,
        "tag_rate_winners": _rate(tag_w, n_w),
        "tag_rate_losers": _rate(tag_l, n_l),
        "hard_tag_rate_winners": round(hard_w / n_w, 3) if n_w else None,
        "hard_tag_rate_losers": round(hard_l / n_l, 3) if n_l else None,
        "signal_means_winners": means_w,
        "signal_means_losers": means_l,
        "signal_score_avg_winners": round(sum(signal_w) / len(signal_w), 4) if signal_w else None,
        "signal_score_avg_losers": round(sum(signal_l) / len(signal_l), 4) if signal_l else None,
        "by_day": days_out,
        "paper_misses": paper_miss_learn(session, chain, days=days, now=now),
        "copycat_veto": copycat,
        "n_copycat_veto_winners": int(copycat.get("n_winners") or 0),
        "n_would_have_copycat": int(copycat.get("n_would_have") or 0),
        "evidence_rows": list(copycat.get("evidence_rows") or []),
        "note": (
            "Desk-era entry Decisions only (source=live). Same UTC day, "
            f"entry mcap ${BAND_LO:,.0f}–${BAND_HI:,.0f}. "
            "Hard tags = github/dev/cto (meme alone excluded). "
            "signal_means_* are cohort separators for paper ranking. "
            "paper_misses = Sol taxonomy skips that later ran ≥5× "
            "(book/holders over social). Side-key paper_miss_join auto-joins. "
            "Copycat/SI shadows stay out of the runner/dud denominators. "
            "would_have = early_runner_book shadow (open=false). "
            "fomo_trend_no_hunt = FOMO trending on-desk/caught with on_hunt=false "
            "(plus door misses) that later ran ≥5× — same miss loop, open=false. "
            "copycat_veto / evidence_rows: copycat-vetoed winners tagged "
            "gate_veto=copycat (www is evidence #1). Hard skip stays. "
            "would-have copycat is shadow only (armed=false)."
        ),
    }
