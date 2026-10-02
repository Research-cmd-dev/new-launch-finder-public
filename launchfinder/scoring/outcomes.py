from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, not_, or_
from sqlalchemy.orm import Session

from ..chains import graduation_mcap, normalize_chain, token_chain
from ..config import GRADUATION_MCAP_USD, settings
from ..db import apply_report_guards
from ..models import ModelState, Outcome, Research, ScanState, Snapshot, Token, utcnow
from ..research import dexscreener, pumpfun
from .features import FEATURE_NAMES, stall_honesty_cap
from .model import train_pending

log = logging.getLogger("launchfinder.outcomes")

HORIZONS = {
    "t15m": timedelta(minutes=15),
    "t1h": timedelta(hours=1),
    "t6h": timedelta(hours=6),
    "t24h": timedelta(hours=24),
}
# Within the first hour, record a snapshot + re-score roughly every 5 minutes
# — the window where runners and rugs diverge deserves dense data.
EARLY_SCAN_WINDOW = timedelta(hours=1)
EARLY_SCAN_GAP = timedelta(minutes=5)
# After the hour-one tape, keep last_mcap on the live Dex book so
# a 15h-old early snap cannot freeze the card (live MEME $175k vs $82M).
LIVE_LAST_SCAN_GAP = timedelta(minutes=15)

RELABEL_KEY = "relabel_v4_win5x"
REPAIR_T0_KEY = "repair_t0_v1"
REPAIR_MCAP_KEY = "repair_mcap_v6"
REPAIR_RH_FLOOR_KEY = "repair_rh_entry_collapse_v1"
REPAIR_RH_HOLDERS_KEY = "repair_rh_holders_from_gmgn_v1"
REPAIR_RH_INSTANT_KEY = "repair_rh_instant_fill_v1"
REPAIR_RH_BLEND_KEY = "repair_rh_young_model_floor_v1"
REPAIR_RH_SMALL_BOOK_KEY = "repair_rh_small_book_collapse_v1"
REPAIR_ORGANIC_SCORE_KEY = "repair_organic_book_score_v1"
REPAIR_ORGANIC_WIDE_KEY = "repair_organic_wide_book_score_v1"
REPAIR_RH_THIN_BOOK_KEY = "repair_rh_thin_book_score_v2"
REPAIR_RH_EMPTY_BOOK_KEY = "repair_rh_empty_book_from_last_liq_v1"
REPAIR_HUNT_MINT_ALIGN_KEY = "repair_hunt_mint_case_v1"
REPAIR_RH_EMPTY_SCORE_KEY = "repair_rh_empty_book_score_v3"
REPAIR_RH_LEFTOVER_FDV_KEY = "repair_rh_leftover_fdv_v1"
# Live 20:00: /health sat 28–34s while leftover-FDV repair loaded every
# RH row (4400+) each cycle. Quiet ghosts still need a pass, but a
# 40-row batch clears the desk without blocking FastAPI.
QUIET_GHOST_BATCH = 40
MATURE_SNAPSHOT_KINDS = ("t6h", "t24h", "post")
RUNNER_MULTIPLE = 10.0
# A day-old Pump.fun migration above this is a data error, not a moonshot.
MAX_SANE_MCAP = 500_000_000.0
# Within the first hours, even monster launches are single-digit millions.
# Flash prints of $250M+ minutes after migration (NTDA case) are manipulated
# pools; a real runner that big will still be big at mature snapshots.
EARLY_SANE_MCAP = 20_000_000.0
EARLY_WINDOW = timedelta(hours=6)
# The desk hunts 5-50x. A 5000x print is a Dex artifact, not a runner.
MAX_HONEST_MULTIPLE = 80.0


def honest_tracked_peak(t0: float, current_max: float, candidate: float) -> tuple[float, float]:
    """Keep a post-label peak unless the new print is an >80x Dex artifact.

    Live BABYSOL: labeled, then Dex printed 4871x and the 72h tracker
    wrote it onto outcome.multiple and credited alpha. The desk already
    hides those; do not store them.
    """
    t0 = float(t0 or 0.0)
    current_max = float(current_max or 0.0)
    candidate = float(candidate or 0.0)
    new_max = max(current_max, candidate)
    if t0 <= 0 or new_max <= 0:
        return current_max, 0.0
    if new_max / t0 > MAX_HONEST_MULTIPLE:
        return current_max, (current_max / t0) if current_max > 0 else 0.0
    return new_max, new_max / t0
# Watch / conviction are the climb toward 5x, same band as approaching.
# Live Sol watch still held ~3200 sub-2x leftovers; RH top-30 was A 1.62 /
# CHARLES 1.46 / HOTDOG 1.32 after the 5x names were already removed.
WATCH_MIN_MULTIPLE = 2.0
MIN_RUNNER_LIQ = 5_000.0
# A 4-wallet Dex wick (live RH: SHORT 3.9x / PLUMBED 3.2x) is a print, not
# a runner. Unknown (0) holders are left alone so Solana rows without a
# Helius fill still count.
MIN_RUNNER_HOLDERS = 20
# Solana Helius samples can be thin on real runners (Pappy 13). A 5-wallet
# print (live USMS 5.8x, $251k t0, one wallet huge) is a wick, not a
# thin sample. Floor 8 keeps Pappy 13 / ILM 10 and drops USMS 5 / MRHATE 6.
MIN_RUNNER_HOLDERS_SOL = 8
# Approaching is a climb toward 5x, not a confirmed runner. A 7-wallet
# Solana print (live WWR/WOFI) is not "about to 5x" even if Helius is thin
# enough that we still allow those names on the runners board later.
MIN_APPROACHING_HOLDERS_SOL = 15
# Leftover graduation LP on Dex is not a live market. After 6h a RH
# token that never printed 2x and has no hourly volume leaves the live
# board — still unlabeled, so a late 5x can still be judged at 24h.
# Ghost / Dex-miss books (liq < $800) do not wait that long: leftover
# pairs often keep reporting residual volume (live TAM $10k vol / $204
# liq), which blocked the 6h rule. Two hours with no live book is enough.
QUIET_VOL_USD = 100.0
DEAD_POOL_LIQ = 800.0
# Live ANGRYCATS sat conviction B at $2.1k after the book dumped.
# A is already $20k; B must not be a drained pool. Runner score
# already penalizes liq_now < $3k.
CONVICTION_MIN_LIQ = 3_000.0
# Sol hunt: cryptobros $2.2k sat above NASA $120k / OnlyUpBOT $222k
# because both cleared last_liq ≥ $800 and recency won. Skinny real
# books stay on the desk, just below fat ones. Live 16:20 /rh:
# StocksCow $9.2k sat above HO $35k after both cleared the $8k
# line. Live 20:00: BYTECAT $10.8k / VEED $10.1k sat #1/#2 after
# the $10k line. RH airdrop dust cap stays hardcoded at $8k.
SKINNY_HUNT_LIQ = 12_000.0
# Live 11:11: TriplePONS 4.9h / 2.40× / $4.2k sat Sol approaching
# #7 and runnerwatch #7. The 8h skinny clock left a dumped $4k
# climb on the 5–50x strip. This-window skinny (PSYOPED $400 /
# Bunny $3.3k) stays. Fat 8h+ climbs stay.
SKINNY_APPROACH_HOURS = 4.0
# Live 11:21: MONK 17.7h / 2.69× sat Sol approaching + conviction B,
# MEMECITY 12.8h / 2.27× and RICHDEBT 9h / 2.31× sat the climb
# strip. An 8h+ book still under 3× is leftover tape, not a 5–50x
# approach. This-window 2× (BEN) stays. Honest 3×+ (007) stays.
# Hunt 2×+ (HA / HMSTR / MONK) stay on the hunt desk.
STALE_APPROACH_HOURS = 8.0
STALE_APPROACH_MULTIPLE = 3.0
# Live 20:00 /rh: SHIT 22w / DIC 24w / JOHN 22w sat #3/#8/#11 above
# LOVEP 166w. Live 03:20: CACHECAT 25w / $14k sat #5. Hunt sort
# uses 25 inclusive; win/label stay at MIN_RUNNER_HOLDERS.
RH_HUNT_THIN_HOLDERS = 25
# Live 21:20: LQX $12.5M / 2.80× / created 05:21 sat #45 because
# leftover-size required multiple <1.2. An 8h-old $1.5M+ major is
# leftover tape. DICKBUTT $1M / 2.13× stays (under the mcap cut).
# Live 04:20: WOTF $11.7M / 2.61× / 7h is an hour from that cut.
# Do not leftover-sort 2×+ (WOTF / BISON). Flat 8h $1.5M+ still
# ranks down.
SOL_LEFTOVER_MAJOR_MCAP = 1_500_000.0
SOL_LEFTOVER_MAJOR_HOURS = 8.0


def is_sol_old_leftover_major(
    mcap: float,
    launched: datetime | None,
    now: datetime | None = None,
    multiple: float | None = None,
) -> bool:
    if launched is None:
        return False
    if launched.tzinfo is None:
        launched = launched.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_h = (now - launched).total_seconds() / 3600.0
    if multiple is not None and float(multiple) >= 2.0:
        return False
    return float(mcap or 0) >= SOL_LEFTOVER_MAJOR_MCAP and age_h >= SOL_LEFTOVER_MAJOR_HOURS
# Live 08:20 Sol hunt: 7 real graduates then NTDA / ClaudeAI /
# CLAWDHOOD / NIKE / HOOD $4–15M / ~1.0× filled the 4h 80 after
# leftover-size sorted them down. Stay on the desk (search / higher
# limit / historical); cap how many occupy the live page.
# Do not leftover-sort 2×+. Do not change leftover refresh chairs.
SOL_HUNT_FAT_LEFTOVER_CAP = 8


def is_sol_fat_leftover_hunt_card(card: dict) -> bool:
    multiple = float(card.get("multiple") or 0.0)
    mcap = float(card.get("max_mcap") or 0.0)
    if multiple >= 2.0:
        return False
    if multiple < 1.8 and mcap >= SOL_LEFTOVER_MAJOR_MCAP:
        return True
    if multiple < 1.9 and mcap >= 5_000_000.0:
        return True
    return False


def cap_sol_live_hunt_fat_leftovers(cards: list[dict]) -> list[dict]:
    """Keep leftover-size rank; only drop extras after the cap.

    Size leftovers still sit after fat hunts and above factory stubs.
    """
    out: list[dict] = []
    leftover_n = 0
    for card in cards:
        if is_sol_fat_leftover_hunt_card(card):
            if leftover_n >= SOL_HUNT_FAT_LEFTOVER_CAP:
                continue
            leftover_n += 1
        out.append(card)
    return out
# Young refresh chairs. One trench poll can ingest 60 leftover RH
# names; unordered limit(25) then spends every Dex call on last_liq=0
# infants. Keep 8 chairs for POV-class Dex-miss; the rest go to Sol
# t15m and RH books we have actually seen.
YOUNG_REFRESH = 25
YOUNG_RH_DEXMISS = 8
# Live Goldinu: 1148 wallets / last_liq $4k / p=0.92 / 1.68x, t15m still
# None after hours. Random + 6h-due never visited, so second-look never
# wrote the 0.48 airdrop cap. Eight chairs, same size as Dex-miss.
RH_AIRDROP_REFRESH = 8
# Live NIKE / LAPTOP: stored last $17M–$295M leftover FDV while Dex
# PumpSwap is $2k. Random unlabeled / post-label sample never visited,
# so hunt kept the ghost. Six chairs, do not raise MAX_HONEST_MULTIPLE.
SOL_LEFTOVER_FDV_REFRESH = 6
# Live 05:00: MSFT / fih / STONKLESS / WORKS sat on Doing well at
# $1M–$14M last while Dex PumpSwap was $2k. leftover-FDV chairs
# only visit last/t0>80. Eight 2×+ chairs rewrite last. Do not
# change leftover chairs. Do not leftover-sort 2×+.
SOL_DOING_WELL_REFRESH = 8
# Live ETAC: desk last $224k / live 92 while Dex WETH is $11k after
# an 87% dump. Sol already has doing-well chairs. RH reserved 16
# rank by multiple so NeMo 500× eats them. Eight smallest 2×+ last
# so a mid-tape dump rewrites. Do not leftover-sort. Do not apply
# Sol leftover-FDV to RH MEME.
RH_DOING_WELL_REFRESH = 8
# Live 09:10: Laptop last $77k / 1.89× sat on hunt while Dex
# PumpSwap was $279k (6.8×). 2×+ chairs never visit under 2.0.
# Eight chairs for the 1.5–2.0 hunt band so last can catch Dex.
# Do not change leftover chairs. Do not leftover-sort 2×+.
SOL_NEAR_WELL_REFRESH = 8
# Extra this-window hunt ticks. Do not change leftover chairs.
HUNT_TICK_REFRESH = 8
# Live AI MEME / ETAC: Hunt 90+ that fade after fill. Hunt-tick chairs
# order by ATH multiple and can return labeled 26× cards that this
# unlabeled pool then drops. Eight chairs so a fresh 90+ gets Dex +
# second-look before it is leftover dust. Do not change leftover chairs.
HIGH_SCORE_REVIEW_REFRESH = 8
NEAR_WELL_MIN_MULTIPLE = 1.5
# Live KFC: leftover t0 $146k / p=0.82 sat for an hour because the
# airdrop pool wants last_liq $800–$8k and start-high $40k books
# never won a reserved chair. Eight chairs, same size as airdrop.
RH_START_HIGH_REFRESH = 8
# Live 06:10: n_train 983 / floor 1080 but DCE 0.078 vs hp 0.47,
# CASHTOPUS 0.11 vs 0.65, QQQSAHUR 0.08 vs 0.49 stayed pulled from
# the 960 lapse. Airdrop chairs want 200+ / $800–$8k; start-high
# wants leftover t0. Mid-books never won a reserved chair.
RH_FLOOR_RESTORE_REFRESH = 8
# Live THEINVESTOR: 7h / $35k / 98 holders, wallet table still empty
# because RH holder_stats was a no-op. Eight chairs so a 6h-old
# graduating book gets a Blockscout map without waiting on random 30.
RH_WALLET_MAP_REFRESH = 8
# Live MEME: paper window ~6 min. Wallet-map chairs hydrate Blockscout
# but do not rescore; young 25 floods with leftover RH. Eight chairs
# so an unlabeled pool-open under 2× / p<0.50 gets a second look
# after the map (or late GMGN in raw_json) lands.
RH_LP_OPEN_REFRESH = 8
# Live 13:50: TEAL 0.87 / XAI 0.92 sat 1.0x for hours. Floor-restore
# chairs lift toward heuristic; these need the opposite write.
RH_STALL_REFRESH = 8
QUIET_GHOST_AGE = timedelta(hours=2)
# Catch-up ingest stamps migrated_at from the trench open time (hours ago)
# with last_liq still 0. Ghost-retire then parked ZAZU/ATHENIX before Dex
# got a refresh. Stay on the live desk for 30m after we first saw them.
QUIET_INGEST_GRACE = timedelta(minutes=30)
# Arriving at 4x+ the graduation floor means the curve was already bought.
# That is a pre-pumped entry (USWS $2.1M @ 40s), not a 5-50x from launch.
MAX_HONEST_ENTRY_MULTIPLE = 4.0
# RH typical books are $20–40k. Live KWAK/ROBUX printed ~$120k t0 (3× the
# $40k floor) — under the Solana 4× line, but already a start-high fill.
MAX_HONEST_ENTRY_MULTIPLE_RH = 2.5
PREPUMP_FLAG_NEEDLES = ("start-high rug", "pre-pumped")
# Live GPRO 71x / SOLLY 13x: instant curve + same-ticker flood. Heuristic
# is 0.02 but they still sat on /api/runners above CAC.
BUNDLE_COPYCAT_NEEDLES = (
    "bonding curve filled almost instantly",
    "same ticker launched repeatedly",
)
# Dex ticks that write outcome.max_mcap can land between snapshot slots
# (live SSB: early snap $113k = 4.95x, Dex $132k = 5.8x, no snap;
# live Maxi: early $96k, Dex $159k = 1.66× the last book). Credit that
# peak only as a gap above an existing liquid snapshot — leftover FDV
# 10x above the last book is still an artifact.
INTER_SCAN_PEAK_SLACK = 2.0
# RH Dex is sparser (live POV: t1h $45k = 3.47x, Dex $121k = 9.23x,
# last_liq $55k / 62 holders — 121/45 = 2.66 rejected at 2.0). Slack 3
# still drops leftover 10x+ FDV above the last book.
INTER_SCAN_PEAK_SLACK_RH = 3.0


def is_rh_leftover_fdv(outcome: Outcome, holders: int = 0, *, chain: str = "robinhood") -> bool:
    """Graduation leftover reported as both t0 and last_liq, tiny holder book.

    Live MORDOR: t0 $160,918 / last_liq $160,920 / 5 holders / 1.0x. Dex is
    echoing leftover FDV, not a market. That 24h loss poisoned the RH model.
    HSH ($21k / 29 holders) and ROCK (5.38x / 79 holders) stay honest.
    """
    if normalize_chain(chain) != "robinhood":
        return False
    t0 = float(outcome.t0_mcap or 0.0)
    floor = graduation_mcap("robinhood")
    if t0 < MAX_HONEST_ENTRY_MULTIPLE_RH * floor:
        return False
    multiple = float(outcome.multiple or 0.0)
    if multiple <= 0 and t0 > 0:
        multiple = float(outcome.max_mcap or 0.0) / t0
    if multiple >= 2.0:
        return False
    if int(holders or 0) >= MIN_RUNNER_HOLDERS:
        return False
    last = float(outcome.last_liq or 0.0)
    if last >= DEAD_POOL_LIQ:
        return abs(last - t0) / t0 <= 0.20
    # last_liq already parked (ghost-high repair) but t0 is still leftover FDV.
    return True


def is_thin_holder_print(holders: int | None, chain: str | None = None) -> bool:
    """Known holder count below the runner floor.

    None / omitted chain: unknown, not thin (Sol Helius-miss stays 0).
    Robinhood 0 is zero wallets — live HIMSTER twin sat on /rh first
    page next to a 29-wallet book because 0 was treated as unknown.
    Robinhood floor: 20 (SHORT/PLUMBED 4-wallet prints). Solana: 8 —
    Pappy-class 13-wallet Helius samples still count; USMS-class
    5-wallet 5.8x does not.
    """
    if holders is None:
        return False
    n = int(holders)
    if n <= 0:
        return chain is not None and normalize_chain(chain) == "robinhood"
    if chain is not None and normalize_chain(chain) != "robinhood":
        return n < MIN_RUNNER_HOLDERS_SOL
    return n < MIN_RUNNER_HOLDERS


def is_thin_approaching_print(holders: int | None, chain: str | None = None) -> bool:
    """Split 4-wallet / 7-wallet Dex wicks off the approaching board.

    Runner confirmation uses 8 wallets on Solana / 20 on Robinhood.
    Approaching is a weaker claim, so Solana uses a 15-wallet line.
    Robinhood 0-wallet leftover (HIMSTER 2.96x twin) is thin, not unknown.
    """
    if holders is None:
        return False
    n = int(holders)
    if n <= 0:
        return normalize_chain(chain) == "robinhood"
    if normalize_chain(chain) == "robinhood":
        return n < MIN_RUNNER_HOLDERS
    return n < MIN_APPROACHING_HOLDERS_SOL


def research_holder_count(token: Token) -> int:
    research = getattr(token, "research", None)
    return int(getattr(research, "holder_count", 0) or 0)


def _research_raw(research: Research | None) -> dict:
    raw_json = getattr(research, "raw_json", None) if research is not None else None
    try:
        raw = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _mark_rh_lp_open(research: Research | None, features: dict) -> bool:
    """Reuse stored LP-open or re-detect from frozen Blockscout rows.

    Live MEME features_json froze before rh_lp_open_book existed.
    Second-look must not recap those 4 pool rows as a thin wick.
    """
    if features.get("rh_lp_open_book"):
        return True
    holders = _research_raw(research).get("holders")
    from .features import rh_lp_open_book

    if isinstance(holders, dict) and rh_lp_open_book(holders):
        features["rh_lp_open_book"] = 1.0
        return True
    return False


def _overlay_stored_gmgn(research: Research | None, features: dict) -> bool:
    """Apply a late GMGN snapshot already sitting in raw_json.

    Live MEME's paper window was ~6 minutes. Ingest can freeze
    gmgn_present=0, then the trench card lands in raw without a
    features rewrite. Overlay safety fields onto the live score
    only — features_json stays the at-entry vector. No extra HTTP.
    """
    if features.get("gmgn_present"):
        return True
    gmgn = _research_raw(research).get("gmgn")
    if not isinstance(gmgn, dict) or not gmgn.get("source"):
        return False
    from .features import _clip01

    features["gmgn_present"] = 1.0
    features["gmgn_honeypot"] = 1.0 if gmgn.get("honeypot") else 0.0
    features["gmgn_bundled"] = 1.0 if gmgn.get("bundled") else 0.0
    features["gmgn_rug_n"] = _clip01(float(gmgn.get("rug_risk") or 0) / 100.0)
    return True


RH_LATE_DEX_CATCHUP_HOURS = 4.0
RH_LATE_DEX_MIN_CHANGE_PCT = 100.0
# Live ROUTE +1053% is a real 1h tape. EDOG +9.9e21% / UGLY +18377%
# are Dex garbage — do not invent a $40k open from those.
RH_LATE_DEX_MAX_CHANGE_PCT = 2500.0


def _rh_market_change_pct(research: Research | None) -> float | None:
    if research is None:
        return None
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        return None
    market = raw.get("market") if isinstance(raw, dict) else {}
    market = market if isinstance(market, dict) else {}
    chg = market.get("price_change_h1")
    try:
        chg_n = float(chg) if chg is not None else 0.0
    except (TypeError, ValueError):
        chg_n = 0.0
    if chg_n == 0.0:
        chg = market.get("price_change_h24")
        try:
            chg_n = float(chg) if chg is not None else 0.0
        except (TypeError, ValueError):
            return None
    if chg_n == 0.0:
        return None
    return chg_n


def rh_implied_open_mcap(
    mcap: float,
    price_change_pct: float | None,
    floor: float,
) -> float | None:
    """Back out the Dex window open when we arrived after the first run.

    Live ROUTE: first print $804k with h1 +1053% → ~$70k open, not t0.
    Caller must keep this this-window only — do not invent a $40k
    launch on week-old fat books (LEGS / BONER / ZCAT class).
    Garbage Dex percentages return None; do not floor-clamp them.
    """
    mcap = float(mcap or 0.0)
    floor = float(floor or 0.0)
    if mcap <= 0 or floor <= 0 or price_change_pct is None:
        return None
    try:
        pct = float(price_change_pct)
    except (TypeError, ValueError):
        return None
    if pct < RH_LATE_DEX_MIN_CHANGE_PCT or pct > RH_LATE_DEX_MAX_CHANGE_PCT:
        return None
    factor = 1.0 + pct / 100.0
    if factor < 2.0:
        return None
    implied = mcap / factor
    if implied < 0.4 * floor:
        return None
    if implied >= 0.85 * mcap:
        return None
    return implied


def rh_ingest_entry(
    mcap: float,
    liquidity_usd: float,
    volume_h1: float,
    migration_mcap: float,
    floor: float,
    price_change_pct: float | None = None,
) -> tuple[float, float]:
    """RH outcome entry + max seed. Leftover-LP FDV is not a price.

    Live AIAIAI: Dex printed $285k on $148 liq. That leftover pair is not
    an entry. CATTIES $3.3k / $3.7k liq / $63k vol is a real small book.
    Live ROUTE: real book, but h1 +1053% means t0 is the implied open.
    """
    mig = float(migration_mcap or 0.0)
    if is_ghost_book(liquidity_usd, volume_h1):
        entry = mig or float(floor)
        return entry, entry
    if mcap > 0:
        implied = rh_implied_open_mcap(mcap, price_change_pct, floor)
        if implied is not None and implied < float(mcap):
            return implied, float(mcap)
        return float(mcap), float(mcap)
    entry = mig or float(floor)
    return entry, entry


def is_ghost_book(liquidity_usd: float | None, volume_h1: float | None) -> bool:
    """Leftover graduation LP or an empty Dex pair — not a live market.

    Live METH: t0 printed $34k mcap on $105 liq / $8 vol, then Dex found
    the real pool ($38k liq). That leftover print is not an entry price.
    FAT/PENIS: t0 was a Dex miss (0/0/0) anchored at the $40k floor.
    """
    liq = float(liquidity_usd or 0.0)
    vol = float(volume_h1 or 0.0)
    if 0 < liq < DEAD_POOL_LIQ:
        return True
    return vol < QUIET_VOL_USD


def t0_snapshot_is_ghost(session: Session, token_id: int) -> bool:
    snap = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token_id, Snapshot.kind == "t0")
        .order_by(Snapshot.taken_at.asc())
        .first()
    )
    if snap is None:
        return False
    return is_ghost_book(snap.liquidity_usd, snap.volume_h1)


def last_live_snapshot_mcap(session: Session, token_id: int) -> float:
    """Most recent non-ghost print. Dex miss at 24h still has these stored."""
    snaps = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token_id)
        .order_by(Snapshot.taken_at.desc())
        .all()
    )
    for snap in snaps:
        if is_ghost_book(snap.liquidity_usd, snap.volume_h1):
            continue
        m = float(snap.mcap_usd or 0.0)
        if m > 0:
            return m
    return 0.0


def approaching_live_multiple(session: Session, token: Token, outcome: Outcome) -> float:
    """Current climb vs t0, not a dumped wick.

    Live FOMODOG: max $196k / t0 $50k = 3.87x, then the book dumped to
    $3.4k. Approaching is names still climbing toward 5x.
    Live discat: Dex last rewrote to $3.5k but the last non-ghost snap
    was still $274k (2.01×). Prefer the Dex-written last when we have
    one so a dump does not sit on the climb list.
    """
    t0 = float(outcome.t0_mcap or 0.0)
    raw = float(outcome.multiple or 0.0)
    if t0 <= 0:
        return raw
    stored = float(outcome.last_mcap or 0.0)
    last = stored if stored > 0 else last_live_snapshot_mcap(session, token.id)
    if last <= 0:
        return raw
    return last / t0


def seed_rh_t24h_from_snaps(session: Session, token: Token, outcome: Outcome) -> bool:
    """Fill t24h when Dex misses the 24h tick so paper does not exit -85%.

    First RH 24h wave (FAT-class ~$20k flat books) would otherwise close
    as drained-pool rugs if the judgment poll is a Dex miss.
    """
    return seed_rh_horizons_from_snaps(session, token, outcome, HORIZONS["t24h"]) > 0


def seed_rh_horizons_from_snaps(
    session: Session, token: Token, outcome: Outcome, age: timedelta
) -> int:
    """Fill due horizon mcaps from the last live snap when Dex misses.

    Catch-up leftovers keep t6h None forever and hog the 6h-due reserved
    slots. A real book we already snapped should not wait for another
    live Dex tick just to mark t6h/t24h.
    """
    if token_chain(token) != "robinhood":
        return 0
    last = last_live_snapshot_mcap(session, token.id)
    if last <= 0 and float(outcome.last_liq or 0.0) >= DEAD_POOL_LIQ:
        last = float(outcome.max_mcap or 0.0)
    if last <= 0:
        return 0
    filled = 0
    for key, delta in HORIZONS.items():
        if age >= delta and getattr(outcome, f"{key}_mcap") is None:
            setattr(outcome, f"{key}_mcap", last)
            filled += 1
    return filled


def _earliest_live_print(
    session: Session,
    token_id: int,
    current: tuple[float, float, float] | None = None,
) -> tuple[float, float, float] | None:
    """Earliest non-ghost (mcap, liq, vol). Ghost t0 is ignored.

    FAT/BAG: Dex is quiet now but an early snapshot already saw the real
    pool. Prefer that stored print over waiting for another live tick.
    """
    chosen: tuple[datetime, float, float, float] | None = None
    snaps = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token_id)
        .order_by(Snapshot.taken_at.asc())
        .all()
    )
    for snap in snaps:
        if snap.kind == "t0" and is_ghost_book(snap.liquidity_usd, snap.volume_h1):
            continue
        mcap = float(snap.mcap_usd or 0.0)
        if mcap <= 0 or is_ghost_book(snap.liquidity_usd, snap.volume_h1):
            continue
        taken = snap.taken_at or datetime.min.replace(tzinfo=timezone.utc)
        if chosen is None or taken < chosen[0]:
            chosen = (taken, mcap, float(snap.liquidity_usd or 0.0), float(snap.volume_h1 or 0.0))
    if chosen:
        return chosen[1], chosen[2], chosen[3]
    if current:
        mcap, liq, vol = current
        if mcap > 0 and not is_ghost_book(liq, vol):
            return current
    return None


def reanchor_ghost_robinhood(
    session: Session,
    token: Token,
    outcome: Outcome,
    mcap: float,
    liquidity_usd: float,
    volume_h1: float,
) -> bool:
    """Replace a leftover-LP / empty-Dex t0 with the first live RH print.

    Uses stored live snapshots when the current Dex tick is leftover LP
    again (FAT/BAG sat on the $40k floor for hours after early snaps).
    Quiet-retired rows stay eligible so 24h labels do not train on the floor.
    """
    if token_chain(token) != "robinhood":
        return False
    if outcome.label is not None:
        return False
    t0 = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token.id, Snapshot.kind == "t0")
        .order_by(Snapshot.taken_at.asc())
        .first()
    )
    if t0 is None or not is_ghost_book(t0.liquidity_usd, t0.volume_h1):
        return False
    live = _earliest_live_print(session, token.id, (mcap, liquidity_usd, volume_h1))
    if live is None:
        return False
    live_mcap, live_liq, live_vol = live
    old = float(outcome.t0_mcap or 0.0)
    t0.mcap_usd = live_mcap
    t0.liquidity_usd = live_liq
    t0.volume_h1 = live_vol
    live_max = live_mcap
    for snap in session.query(Snapshot).filter(Snapshot.token_id == token.id):
        if is_ghost_book(snap.liquidity_usd, snap.volume_h1):
            continue
        live_max = max(live_max, float(snap.mcap_usd or 0.0))
    outcome.t0_mcap = live_mcap
    outcome.max_mcap = live_max
    outcome.last_liq = live_liq
    outcome.multiple = live_max / live_mcap if live_mcap else 0.0
    for key in HORIZONS:
        setattr(outcome, f"{key}_mcap", None)
    log.info(
        "RH re-anchor t0 %s ghost $%.0f -> live $%.0f liq=%.0f vol=%.0f",
        token.symbol,
        old,
        live_mcap,
        live_liq,
        live_vol,
    )
    _rescore_after_rh_reanchor(session, token, outcome, live_vol)
    return True


def _rescore_after_rh_reanchor(
    session: Session, token: Token, outcome: Outcome, volume_h1: float = 0.0
) -> bool:
    """Unstick a 0.48 empty-book score once ghost t0 becomes a real book.

    Live CASHBIRD ingested at last_liq=0 (empty cap) then climbed to 3.4x
    before second-look could write. The 2x lift-block froze p at 0.48 —
    a 6.7x capture miss. Re-score here while multiple is still < 2.
    Do not lift CASHBIRD now. features_json stays at-entry.
    """
    research = getattr(token, "research", None)
    if research is None:
        return False
    if float(outcome.last_liq or 0.0) < DEAD_POOL_LIQ:
        return False
    multiple = float(outcome.multiple or 0.0)
    if multiple >= 2.0:
        return False
    import json as _json

    from .features import _log_norm, is_organic_book, is_rh_airdrop_book, is_rh_bot_dump_book, is_rh_empty_book
    from .model import predict

    try:
        features = _json.loads(research.features_json or "{}")
    except _json.JSONDecodeError:
        return False
    if not features:
        return False
    last_liq = float(outcome.last_liq or 0.0)
    holders = research_holder_count(token)
    features["liquidity_n"] = _log_norm(last_liq, 80_000)
    vol = float(volume_h1 or 0.0)
    if vol > 0:
        features["volume_n"] = _log_norm(vol, 50_000)
    features["rh_empty_book"] = (
        1.0
        if is_rh_empty_book(
            "robinhood",
            last_liq,
            last_liq=last_liq,
            age_min=0.0,
            holders=holders,
        )
        else 0.0
    )
    from .features import RH_FAT_BOOK_LIQ

    pool_open = _mark_rh_lp_open(research, features)
    features["rh_thin_book"] = (
        1.0 if holders < 20 and last_liq < RH_FAT_BOOK_LIQ and not pool_open else 0.0
    )
    features["rh_airdrop_book"] = (
        1.0 if is_rh_airdrop_book("robinhood", holders, last_liq, liq=last_liq) else 0.0
    )
    if holders > 0:
        features["holder_n"] = _log_norm(float(holders), 2_000)
    features["organic_book"] = 1.0 if is_organic_book(features) else 0.0
    features["rh_bot_dump"] = 1.0 if is_rh_bot_dump_book("robinhood", features) else 0.0
    old = float(research.p_good or 0.0)
    scored = predict(session, features, chain="robinhood")
    if float(scored["p_good"]) <= old + 1e-4:
        return False
    lifted = _commit_live_score(research, outcome, scored, scored["p_good"])
    if lifted:
        log.info(
            "RH re-anchor rescore %s %.2f -> %.2f liq=%.0f holders=%s",
            token.symbol,
            old,
            float(research.p_good or 0.0),
            last_liq,
            holders,
        )
    return lifted


def _watched_label_is_historical(token: Token) -> bool:
    """Quiet-retire only hides the live desk. Watched RH rows still have a real t0."""
    if token.source == "backfill":
        return True
    if token_chain(token) == "robinhood":
        return False
    return bool(token.is_historical)


def repair_rh_ghost_peak(session: Session) -> int:
    """Drop leftover-Dex peaks so an empty $28M pair cannot mint a 710x."""
    rows = (
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter(Token.chain == "robinhood", Outcome.label.is_(None))
        .limit(80)
        .all()
    )
    # Live 05:40: /health hit 3.5s while this loop issued one Snapshot
    # query per unlabeled row. One IN() load keeps the event loop free.
    snaps_by_token: dict[int, list] = {}
    ids = [t.id for _, t in rows]
    if ids:
        for snap in session.query(Snapshot).filter(Snapshot.token_id.in_(ids)).all():
            snaps_by_token.setdefault(snap.token_id, []).append(snap)
    fixed = 0
    for outcome, token in rows:
        live_max = 0.0
        live_liq = 0.0
        for snap in snaps_by_token.get(token.id, ()):
            if is_ghost_book(snap.liquidity_usd, snap.volume_h1):
                continue
            m = float(snap.mcap_usd or 0.0)
            if m > live_max:
                live_max = m
                live_liq = float(snap.liquidity_usd or 0.0)
        t0 = float(outcome.t0_mcap or 0.0)
        current = float(outcome.max_mcap or 0.0)
        if live_max <= 0:
            last = float(outcome.last_liq or 0.0)
            # Dex-miss climber: last_liq is a real pool, peak is well above
            # it (live SHARK 3.4x). Leftover FDV prints last_liq≈max
            # (STLSPCXMTGIN $28M / $28M) and must still be stripped.
            if last >= DEAD_POOL_LIQ and current > last * 1.5:
                continue
            target = t0 if t0 > 0 else 0.0
            dirty_liq = last >= DEAD_POOL_LIQ
            if current > target + 1.0:
                outcome.max_mcap = target
                outcome.multiple = 1.0 if target else 0.0
                outcome.last_liq = 0.0
                fixed += 1
            elif dirty_liq:
                # Park already floored max_mcap but left leftover-FDV as last_liq.
                outcome.last_liq = 0.0
                fixed += 1
            continue
        if current > live_max + 1.0:
            outcome.max_mcap = live_max
            outcome.last_liq = live_liq
            if t0 > 0:
                outcome.multiple = live_max / t0
            fixed += 1
    if fixed:
        log.info("RH ghost peak repair: stripped leftover-Dex highs on %s tokens", fixed)
    return fixed


def repair_rh_ghost_t0(session: Session) -> int:
    """DB-only: re-anchor unlabeled RH ghosts from snapshots already stored."""
    rows = (
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter(Token.chain == "robinhood", Outcome.label.is_(None))
        .limit(80)
        .all()
    )
    # Live 05:40: ghost-peak N+1 stalled /health. Warm the session so
    # reanchor / park / adopt do not issue one Snapshot query per row.
    ids = [t.id for _, t in rows]
    if ids:
        session.query(Snapshot).filter(Snapshot.token_id.in_(ids)).all()
    fixed = 0
    for outcome, token in rows:
        if reanchor_ghost_robinhood(session, token, outcome, 0.0, 0.0, 0.0):
            fixed += 1
        elif adopt_rh_floor_when_t0_is_live(session, token, outcome):
            fixed += 1
        elif park_rh_ghost_high_t0(session, token, outcome):
            fixed += 1
    if fixed:
        log.info("RH ghost t0 repair: re-anchored %s tokens from stored live prints", fixed)
    return fixed


def _rh_late_dex_age_h(token: Token) -> float | None:
    start = token.created_at_chain or token.migrated_at
    seen = token.first_seen_at
    if start is None or seen is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return max(0.0, (seen - start).total_seconds() / 3600.0)


def restore_rh_late_dex_junk_t0(session: Session) -> int:
    """Undo floor-invented late-Dex t0s from garbage Dex percentages.

    Live EDOG/UGLY: +9.9e21% / +18377% collapsed a real arrival print
    to the $40k floor. The t0 snap still has the arrival. Restore that
    print when implied-open would now return None.
    """
    floor = graduation_mcap("robinhood")
    start_high = MAX_HONEST_ENTRY_MULTIPLE_RH * floor
    rows = (
        session.query(Outcome, Token, Research)
        .join(Token, Token.id == Outcome.token_id)
        .join(Research, Research.token_id == Token.id)
        .filter(
            Token.chain == "robinhood",
            Token.source != "backfill",
            Token.source != "rh_backfill",
            Token.is_historical.is_(False),
            Outcome.label.is_(None),
            Outcome.t0_mcap > 0,
            Outcome.t0_mcap <= floor + 1.0,
        )
        .limit(80)
        .all()
    )
    ids = [t.id for _, t, _ in rows]
    if ids:
        session.query(Snapshot).filter(Snapshot.token_id.in_(ids)).all()
    fixed = 0
    for outcome, token, research in rows:
        t0 = (
            session.query(Snapshot)
            .filter(Snapshot.token_id == token.id, Snapshot.kind == "t0")
            .order_by(Snapshot.taken_at.asc())
            .first()
        )
        if t0 is None:
            continue
        snap_mcap = float(t0.mcap_usd or 0.0)
        if snap_mcap < start_high:
            continue
        if is_ghost_book(t0.liquidity_usd, t0.volume_h1):
            continue
        chg = _rh_market_change_pct(research)
        if rh_implied_open_mcap(snap_mcap, chg, floor) is not None:
            continue
        if chg is None or chg < RH_LATE_DEX_MIN_CHANGE_PCT:
            continue
        old = float(outcome.t0_mcap or 0.0)
        peak = max(float(outcome.max_mcap or 0.0), float(outcome.last_mcap or 0.0), snap_mcap)
        outcome.t0_mcap = snap_mcap
        outcome.max_mcap = peak
        outcome.multiple = peak / snap_mcap if snap_mcap else 0.0
        log.info(
            "RH late-Dex junk restore %s $%.0f -> snap $%.0f (change=%s)",
            token.symbol,
            old,
            snap_mcap,
            chg,
        )
        fixed += 1
    if fixed:
        log.info("RH late-Dex junk restore: put back %s arrival prints", fixed)
    return fixed


def repair_rh_late_dex_t0(session: Session) -> int:
    """This-window late Dex catch-up: t0 is the implied open, not arrival.

    Live ROUTE ingested 1.8h after pair create at $804k. Dex h1 +1053%
    was already on the stored market blob. Week-old fat books stay.
    Newest first-seen first so a late this-window catch-up is not
    starved by an older unlabeled start-high batch.
    """
    floor = graduation_mcap("robinhood")
    start_high = MAX_HONEST_ENTRY_MULTIPLE_RH * floor
    rows = (
        session.query(Outcome, Token, Research)
        .join(Token, Token.id == Outcome.token_id)
        .join(Research, Research.token_id == Token.id)
        .filter(
            Token.chain == "robinhood",
            Token.source != "backfill",
            Token.source != "rh_backfill",
            Token.is_historical.is_(False),
            # Live ROUTE labeled 1 at 8.8× vs the $804k arrival.
            # Skip labeled rugs — do not turn a 0 into a 100× training row.
            or_(Outcome.label.is_(None), Outcome.label == 1),
            Outcome.t0_mcap >= start_high,
            Token.first_seen_at >= datetime.now(timezone.utc) - timedelta(hours=12),
        )
        .order_by(Token.first_seen_at.desc())
        .limit(80)
        .all()
    )
    fixed = 0
    for outcome, token, research in rows:
        age_h = _rh_late_dex_age_h(token)
        if age_h is None or age_h > RH_LATE_DEX_CATCHUP_HOURS:
            continue
        chg = _rh_market_change_pct(research)
        arrival = float(outcome.t0_mcap or 0.0)
        t0 = (
            session.query(Snapshot)
            .filter(Snapshot.token_id == token.id, Snapshot.kind == "t0")
            .order_by(Snapshot.taken_at.asc())
            .first()
        )
        if t0 is not None and float(t0.mcap_usd or 0.0) > 0:
            arrival = float(t0.mcap_usd)
        implied = rh_implied_open_mcap(arrival, chg, floor)
        if implied is None or implied >= float(outcome.t0_mcap or 0.0) * 0.85:
            continue
        old = float(outcome.t0_mcap or 0.0)
        peak = max(float(outcome.max_mcap or 0.0), float(outcome.last_mcap or 0.0), arrival)
        outcome.t0_mcap = implied
        outcome.max_mcap = peak
        outcome.multiple = peak / implied if implied else 0.0
        log.info(
            "RH late-Dex t0 %s $%.0f -> implied open $%.0f (change=%s age=%.1fh)",
            token.symbol,
            old,
            implied,
            chg,
            age_h,
        )
        fixed += 1
    if fixed:
        log.info("RH late-Dex t0 repair: re-anchored %s this-window catch-ups", fixed)
    return fixed


def park_rh_ghost_high_t0(session: Session, token: Token, outcome: Outcome) -> bool:
    """AIAIAI-class: leftover LP printed $285k on $148 liq. Park at the floor
    until a live book exists (reanchor then takes the first real print)."""
    if token_chain(token) != "robinhood" or outcome.label is not None:
        return False
    t0 = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token.id, Snapshot.kind == "t0")
        .order_by(Snapshot.taken_at.asc())
        .first()
    )
    if t0 is None or not is_ghost_book(t0.liquidity_usd, t0.volume_h1):
        return False
    if _earliest_live_print(session, token.id, (0.0, 0.0, 0.0)) is not None:
        return False
    floor = graduation_mcap("robinhood")
    old = float(outcome.t0_mcap or 0.0)
    peak = float(outcome.max_mcap or 0.0)
    if old <= floor + 1.0 and peak <= floor + 1.0:
        return False
    outcome.t0_mcap = floor
    outcome.max_mcap = floor
    outcome.multiple = 1.0
    # Leftover FDV (live STLSPCXMTGIN $28M "liq") is not a book. Parking
    # that figure blocked the leftover-LP quiet path (needs last_liq < $800).
    outcome.last_liq = 0.0
    log.info("RH park ghost-high t0 %s $%.0f -> floor $%.0f (ghost liq=%.0f)", token.symbol, old, floor, t0.liquidity_usd)
    return True


def adopt_rh_floor_when_t0_is_live(session: Session, token: Token, outcome: Outcome) -> bool:
    """CATTIES-class: live t0 snap at $3.3k, outcome still parked on the $40k floor.

    The 0.4×-floor collapse guard is a Solana start-high-and-dump rule. A
    real small RH book must not train or paper-trade as a $40k entry.
    """
    if token_chain(token) != "robinhood" or outcome.label is not None:
        return False
    floor = graduation_mcap("robinhood")
    t0_mcap = float(outcome.t0_mcap or 0.0)
    if abs(t0_mcap - floor) > 1.0:
        return False
    t0 = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token.id, Snapshot.kind == "t0")
        .order_by(Snapshot.taken_at.asc())
        .first()
    )
    if t0 is None or is_ghost_book(t0.liquidity_usd, t0.volume_h1):
        return False
    snap_mcap = float(t0.mcap_usd or 0.0)
    if snap_mcap <= 0 or abs(snap_mcap - floor) < 1.0:
        return False
    live_max = snap_mcap
    for snap in session.query(Snapshot).filter(Snapshot.token_id == token.id):
        if is_ghost_book(snap.liquidity_usd, snap.volume_h1):
            continue
        live_max = max(live_max, float(snap.mcap_usd or 0.0))
    old = t0_mcap
    outcome.t0_mcap = snap_mcap
    outcome.max_mcap = live_max
    outcome.last_liq = float(t0.liquidity_usd or 0.0)
    outcome.multiple = live_max / snap_mcap if snap_mcap else 0.0
    log.info(
        "RH adopt live t0 over floor %s $%.0f -> $%.0f liq=%.0f vol=%.0f",
        token.symbol,
        old,
        snap_mcap,
        t0.liquidity_usd,
        t0.volume_h1,
    )
    return True


def retire_quiet_robinhood(
    token: Token,
    outcome: Outcome,
    age: timedelta,
    volume_h1: float,
    *,
    seen_book: bool = True,
) -> bool:
    """Take a dead RH book off the live desk without minting a rug label.

    A Dex miss is not a dead book. Live SSB-class names (last_liq still
    $5k–$30k, multiple parked at 1.0 this tick) were leaving the desk at
    6h because the miss path passed vol=0. Only leftover LP / empty Dex
    (liq < $800) retires without a live tick.
    """
    if token_chain(token) != "robinhood" or token.is_historical:
        return False
    if outcome.label == 1:
        return False
    if float(outcome.multiple or 0.0) >= 2.0:
        return False
    last_liq = float(outcome.last_liq or 0.0)
    # Leftover LP / Dex miss: residual volume is not a live market.
    if last_liq < DEAD_POOL_LIQ:
        if age < QUIET_GHOST_AGE:
            return False
        seen = token.first_seen_at
        if seen is not None:
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if utcnow() - seen < QUIET_INGEST_GRACE:
                return False
        token.is_historical = True
        return True
    if not seen_book:
        return False
    if age < HORIZONS["t6h"]:
        return False
    if float(volume_h1 or 0.0) >= QUIET_VOL_USD:
        return False
    token.is_historical = True
    return True


def repair_rh_leftover_fdv(session: Session) -> int:
    """Park leftover-FDV last_liq on rows labeled before the 24h skip.

    Live MORDOR kept last_liq $160,920 after the train skip shipped —
    `_label` only parks on the next judgment. Same detector: t0 ≥ 2.5×
    the $40k floor, last≈t0, multiple < 2, holders < 20.

    One-shot: new leftovers are parked in `_label`. Re-scanning the
    whole RH book every cycle blocked /health for 30s at 4400 tokens.
    """
    if session.query(ScanState).filter(ScanState.key == REPAIR_RH_LEFTOVER_FDV_KEY).one_or_none():
        return 0
    rows = (
        session.query(Outcome, Token, Research)
        .join(Token, Token.id == Outcome.token_id)
        .outerjoin(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .all()
    )
    fixed = 0
    for outcome, _token, research in rows:
        holders = int(getattr(research, "holder_count", 0) or 0)
        if not is_rh_leftover_fdv(outcome, holders, chain="robinhood"):
            continue
        if outcome.label is not None:
            outcome.used_for_train = True
        if float(outcome.last_liq or 0.0) <= 0:
            continue
        outcome.last_liq = 0.0
        fixed += 1
    session.add(ScanState(key=REPAIR_RH_LEFTOVER_FDV_KEY, value=f"parked {fixed}"))
    session.flush()
    if fixed:
        log.info("RH leftover FDV repair: parked %s leftover books", fixed)
    return fixed


def repair_rh_quiet_ghosts(session: Session) -> int:
    """Retire leftover-LP / Dex-miss RH rows refresh has not visited.

    AIAIAI sat live at $148 liq for 3h because the 6h rule needed a Dex
    tick and leftover volume kept it off the quiet path. Run every cycle
    so ghosts leave without waiting for a refresh slot.
    """
    now = utcnow()
    due = now - QUIET_INGEST_GRACE
    rows = (
        session.query(Token, Outcome)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(
            Token.chain == "robinhood",
            Token.is_historical.is_(False),
            Token.source != "backfill",
            Outcome.multiple < 2.0,
            Outcome.last_liq < DEAD_POOL_LIQ,
            Token.first_seen_at.is_not(None),
            Token.first_seen_at <= due,
        )
        .order_by(Token.first_seen_at.asc())
        .limit(QUIET_GHOST_BATCH)
        .all()
    )
    fixed = 0
    for token, outcome in rows:
        start = token.migrated_at or token.first_seen_at
        if start is None:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if retire_quiet_robinhood(token, outcome, now - start, 0.0):
            fixed += 1
    if fixed:
        session.flush()
        log.info("RH quiet ghost repair: retired %s leftover/Dex-miss rows", fixed)
    return fixed


BONER_REFERENCE_MINT = "0x98096d17e191b3da1d5f99a6d7b3584351b11e18"


def park_boner_reference(session: Session) -> int:
    """BONER is an 11-day Long/HIMS runner, not a new $40k launch.

    Seed used source=rh_backfill so ingest-grace unparked it onto the live
    desk with a $40k floor t0. Park it as a true backfill reference.
    """
    token = session.query(Token).filter(Token.mint == BONER_REFERENCE_MINT).one_or_none()
    if token is None:
        return 0
    if token.source == "backfill" and token.is_historical:
        return 0
    token.source = "backfill"
    token.is_historical = True
    session.flush()
    log.info("parked BONER reference as historical backfill")
    return 1


def restore_rh_ingest_grace(session: Session) -> int:
    """Undo a same-cycle ghost-retire of names we just ingested."""
    cutoff = utcnow() - QUIET_INGEST_GRACE
    rows = (
        session.query(Token)
        .filter(
            Token.chain == "robinhood",
            Token.is_historical.is_(True),
            Token.source.notin_(("backfill", "rh_backfill")),
            Token.first_seen_at >= cutoff,
        )
        .all()
    )
    fixed = 0
    for token in rows:
        token.is_historical = False
        fixed += 1
    if fixed:
        session.flush()
        log.info("RH ingest grace: restored %s just-seen rows to the live desk", fixed)
    return fixed


def is_prepumped_entry(session: Session, token: Token, research: Research, outcome: Outcome) -> bool:
    """True when the 'run' started from a bundle-owned price, not migration.

    Outcome.t0 is often re-anchored at the graduation floor, which turns a
    $5M first print into a fake 70x (live USWS). Believe the early snapshots.
    """
    flags = (research.risk_flags_json or "").lower()
    if any(needle in flags for needle in PREPUMP_FLAG_NEEDLES):
        return True
    try:
        feats = json.loads(research.features_json or "{}")
    except json.JSONDecodeError:
        feats = {}
    if feats.get("entry_premium"):
        return True
    chain = token_chain(token)
    floor = graduation_mcap(chain)
    cap_mult = (
        MAX_HONEST_ENTRY_MULTIPLE_RH
        if normalize_chain(chain) == "robinhood"
        else MAX_HONEST_ENTRY_MULTIPLE
    )
    t0 = float(outcome.t0_mcap or 0.0)
    if floor > 0 and t0 > cap_mult * floor:
        return True
    if floor <= 0:
        return False
    ceiling = cap_mult * floor
    early = (
        session.query(Snapshot.mcap_usd)
        .filter(Snapshot.token_id == token.id, Snapshot.kind == "t0", Snapshot.mcap_usd > ceiling)
        .first()
    )
    return early is not None


def is_bundle_copycat_run(research: Research | None) -> bool:
    """Instant-fill copycat that printed a multiple anyway.

    Live GPRO: 258 holders / $197k liq / 71x, but brand-new X, instant
    curve, 20+ snipers, bundler tape, same ticker 24h. Rules already
    floor heuristic at 0.02. The board still listed it as the top
    'runner'. Pappy-class lottery tickets do not have both needles.
    """
    if research is None:
        return False
    flags = (research.risk_flags_json or "").lower()
    return all(needle in flags for needle in BUNDLE_COPYCAT_NEEDLES)


def confirmed_runner_multiple(session: Session, token: Token, outcome: Outcome) -> float:
    """Highest multiple we can defend: snapshot mcap with real liquidity,
    passed through sane_mcap, and capped so flash prints cannot mint a
    5000x 'runner'. Returns 0 if nothing is confirmed."""
    entry = float(outcome.t0_mcap or 0.0)
    if entry <= 0:
        return 0.0
    if is_thin_holder_print(research_holder_count(token), token_chain(token)):
        return 0.0
    # FRUG-class: outcome.t0 is the $69k floor but the t0 snap already
    # traded at $264k on a live pool. That is the real entry.
    t0 = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token.id, Snapshot.kind == "t0")
        .order_by(Snapshot.taken_at.asc())
        .first()
    )
    if (
        t0
        and float(t0.liquidity_usd or 0.0) >= MIN_RUNNER_LIQ
        and float(t0.mcap_usd or 0.0) > entry
    ):
        entry = float(t0.mcap_usd)
    start = token.migrated_at or token.first_seen_at
    rows = (
        session.query(Snapshot.mcap_usd, Snapshot.liquidity_usd, Snapshot.taken_at, Snapshot.kind)
        .filter(Snapshot.token_id == token.id, Snapshot.liquidity_usd >= MIN_RUNNER_LIQ, Snapshot.mcap_usd > 0)
        .all()
    )
    best = 0.0
    mature_best = 0.0
    for mcap, liq, taken, kind in rows:
        age = None
        if start and taken:
            a = taken if taken.tzinfo else taken.replace(tzinfo=timezone.utc)
            s = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            age = a - s
        v = sane_mcap(mcap, liq, age)
        if v > best:
            best = v
        if kind in MATURE_SNAPSHOT_KINDS and v > mature_best:
            mature_best = v
    # Inter-scan Dex peak: outcome.max_mcap can print between snapshot slots.
    # Only fill a gap above existing snapshot peaks — never invent a runner
    # from leftover FDV with no live book snaps. Current last_liq can dump
    # under $5k after the run (live SSB $4k) — that must not un-confirm.
    last_liq = float(outcome.last_liq or 0.0)
    max_mcap = float(outcome.max_mcap or 0.0)
    peak_liq = max([float(liq or 0.0) for _, liq, _, _ in rows] + [last_liq, 0.0])
    slack = (
        INTER_SCAN_PEAK_SLACK_RH
        if token_chain(token) == "robinhood"
        else INTER_SCAN_PEAK_SLACK
    )
    if best > 0 and max_mcap > best and max_mcap <= best * slack:
        age = None
        if start:
            now = utcnow()
            s = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            n = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
            age = n - s
        v = sane_mcap(max_mcap, peak_liq, age)
        if v > best:
            best = v
    if best <= 0:
        return 0.0
    multiple = best / entry
    if multiple > MAX_HONEST_MULTIPLE:
        if mature_best < RUNNER_MULTIPLE * entry:
            return 0.0
        multiple = mature_best / entry
        # A "mature" Dex pair can still print $100M+ on thin liquidity.
        # The desk hunts 5-50x; anything above 80x is treated as an artifact.
        if multiple > MAX_HONEST_MULTIPLE:
            return 0.0
    return multiple


def sane_mcap(
    value: float | None,
    liquidity: float | None = None,
    age: timedelta | None = None,
    ceiling: float | None = None,
) -> float:
    """Reject garbage market-cap readings: above the hard ceiling, wildly out
    of proportion to the pool that supposedly prices it, or a flash print of
    tens of millions within hours of migration (manipulated pool)."""
    v = float(value or 0.0)
    cap = ceiling if ceiling is not None else (EARLY_SANE_MCAP if (age is not None and age < EARLY_WINDOW) else MAX_SANE_MCAP)
    if not 0.0 < v <= cap:
        return 0.0
    liq = float(liquidity or 0.0)
    if liq > 0 and v > 2_000.0 * liq:
        return 0.0
    return v


def repair_rh_graduation_floor(session: Session) -> None:
    """Robinhood tokens were scored against Solana's $69k graduation floor.
    A normal ~$20k RH launch looked like a start-high rug (entry_collapse)
    and the whole desk sat at p=0.02. Re-score those false collapses."""
    if session.query(ScanState).filter(ScanState.key == REPAIR_RH_FLOOR_KEY).one_or_none():
        return
    from .model import predict

    rows = (
        session.query(Token, Research)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .all()
    )
    floor = graduation_mcap("robinhood")
    fixed = 0
    for token, research in rows:
        try:
            feats = json.loads(research.features_json or "{}")
        except json.JSONDecodeError:
            continue
        if not feats.get("entry_collapse"):
            continue
        mcap = float((token.outcome.t0_mcap if token.outcome else 0.0) or 0.0)
        if mcap < 0.4 * floor:
            continue
        feats["entry_collapse"] = 0.0
        research.features_json = json.dumps(feats)
        scored = predict(session, feats, chain="robinhood")
        research.p_good = scored["p_good"]
        research.heuristic_p = scored["heuristic_p"]
        research.model_p = scored["model_p"]
        research.risk_flags_json = json.dumps(scored["risk_flags"])
        research.reasons_json = json.dumps(scored["reasons"])
        fixed += 1
    session.add(ScanState(key=REPAIR_RH_FLOOR_KEY, value=f"rescored {fixed}"))
    session.flush()
    log.info("RH graduation floor: cleared false entry_collapse on %s tokens", fixed)


def repair_rh_small_book_collapse(session: Session) -> None:
    """Clear false start-high-rug flags on live RH books below 0.4×$40k.

    Live JJJACKET: t0 $12.9k / $11.6k liq / 186 holders / early $181k =
    14x, but entry_collapse branded it a dump and /api/runners dropped it.
    The v1 floor repair skipped mcap < $16k. Rescore live leftover-free books.
    """
    if session.query(ScanState).filter(ScanState.key == REPAIR_RH_SMALL_BOOK_KEY).one_or_none():
        return
    from .model import predict

    rows = (
        session.query(Token, Research, Outcome)
        .join(Research, Research.token_id == Token.id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .all()
    )
    fixed = 0
    for _token, research, outcome in rows:
        try:
            feats = json.loads(research.features_json or "{}")
        except json.JSONDecodeError:
            continue
        if not feats.get("entry_collapse"):
            continue
        if float(outcome.last_liq or 0.0) < DEAD_POOL_LIQ:
            continue
        feats["entry_collapse"] = 0.0
        research.features_json = json.dumps(feats)
        try:
            flags = json.loads(research.risk_flags_json or "[]")
        except json.JSONDecodeError:
            flags = []
        research.risk_flags_json = json.dumps(
            [f for f in flags if isinstance(f, str) and "start-high rug" not in f.lower()]
        )
        if "has_twitter" in feats:
            scored = predict(session, feats, chain="robinhood")
            research.p_good = scored["p_good"]
            research.heuristic_p = scored["heuristic_p"]
            research.model_p = scored["model_p"]
            research.risk_flags_json = json.dumps(scored["risk_flags"])
            research.reasons_json = json.dumps(scored["reasons"])
        fixed += 1
    session.add(ScanState(key=REPAIR_RH_SMALL_BOOK_KEY, value=f"rescored {fixed}"))
    session.flush()
    log.info("RH small-book collapse: cleared false start-high on %s tokens", fixed)


def repair_rh_instant_fill(session: Session) -> None:
    """Robinhood trench rows have created≈opened, so every token got
    migrate_speed=0.05 and the 'instant fill' bundle flag. Neutralize it."""
    if session.query(ScanState).filter(ScanState.key == REPAIR_RH_INSTANT_KEY).one_or_none():
        return
    from .model import predict

    rows = (
        session.query(Token, Research)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .all()
    )
    fixed = 0
    for _token, research in rows:
        try:
            feats = json.loads(research.features_json or "{}")
        except json.JSONDecodeError:
            continue
        if float(feats.get("migrate_speed") or 0) >= 0.15:
            continue
        feats["migrate_speed"] = 0.4
        research.features_json = json.dumps(feats)
        scored = predict(session, feats, chain="robinhood")
        research.p_good = scored["p_good"]
        research.heuristic_p = scored["heuristic_p"]
        research.model_p = scored["model_p"]
        research.risk_flags_json = json.dumps(scored["risk_flags"])
        research.reasons_json = json.dumps(scored["reasons"])
        fixed += 1
    session.add(ScanState(key=REPAIR_RH_INSTANT_KEY, value=f"rescored {fixed}"))
    session.flush()
    log.info("RH instant-fill: neutralized false bundle flag on %s tokens", fixed)


def repair_rh_young_model_floor(session: Session) -> None:
    """Re-score RH rows so the young-model heuristic floor lands on the desk.

    SANDIH sat at blend 0.43 after leftover-FDV losses dragged model_p to
    0.22. After predict() floors p at heuristic, rewrite stored p_good so
    paper and capture see 0.51 without waiting for the next research pass.
    """
    if session.query(ScanState).filter(ScanState.key == REPAIR_RH_BLEND_KEY).one_or_none():
        return
    from .model import predict

    rows = (
        session.query(Token, Research)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .all()
    )
    fixed = 0
    for _token, research in rows:
        try:
            feats = json.loads(research.features_json or "{}")
        except json.JSONDecodeError:
            continue
        if not feats:
            continue
        scored = predict(session, feats, chain="robinhood")
        if abs(float(research.p_good or 0.0) - scored["p_good"]) < 1e-4:
            continue
        research.p_good = scored["p_good"]
        research.heuristic_p = scored["heuristic_p"]
        research.model_p = scored["model_p"]
        research.risk_flags_json = json.dumps(scored["risk_flags"])
        research.reasons_json = json.dumps(scored["reasons"])
        fixed += 1
    session.add(ScanState(key=REPAIR_RH_BLEND_KEY, value=f"rescored {fixed}"))
    session.flush()
    log.info("RH young-model floor: rescored %s tokens", fixed)


def repair_organic_book_scores(session: Session) -> None:
    """One-shot rescore after the organic-book / serial-launch heuristic.

    Stored p_good still has the old stacked penalties (no-smart-money,
    launch-count serial, fresh wallets on a live book). Replay predict()
    so capture and paper see the new scores without waiting for research.
    """
    if session.query(ScanState).filter(ScanState.key == REPAIR_ORGANIC_SCORE_KEY).one_or_none():
        return
    from .model import predict

    rows = session.query(Token, Research).join(Research, Research.token_id == Token.id).all()
    fixed = 0
    for token, research in rows:
        try:
            feats = json.loads(research.features_json or "{}")
        except json.JSONDecodeError:
            continue
        if not feats:
            continue
        chain = token_chain(token)
        scored = predict(session, feats, chain=chain)
        old_p = float(research.p_good or 0.0)
        new_p = float(scored["p_good"])
        # Lift only. Stored p_good is often a model score on a richer snapshot
        # than features_json. Replaying heuristic on stale keys would floor
        # CAC/BEAST-class 60x winners (0.79 → 0.02) and kill Sol capture.
        if new_p <= old_p + 0.03:
            continue
        research.p_good = new_p
        research.heuristic_p = scored["heuristic_p"]
        research.model_p = scored["model_p"]
        research.risk_flags_json = json.dumps(scored["risk_flags"])
        research.reasons_json = json.dumps(scored["reasons"])
        fixed += 1
    session.add(ScanState(key=REPAIR_ORGANIC_SCORE_KEY, value=f"lifted {fixed}"))
    session.flush()
    log.info("organic-book heuristic: lifted %s tokens", fixed)


def repair_organic_wide_book_scores(session: Session) -> None:
    """One-shot Sol lift after the wide-liquid organic OR (PVP-class).

    Stored features already have PVP's 138 wallets / $15k liq / $1.1k vol.
    The old AND required vol_n 0.74; replay now flags the book and floors
    Sol p at 0.60 (capture line). Lift only. Skip Robinhood — paper reads
    research.p_good and must not buy a 3x climber after the fact.
    """
    if session.query(ScanState).filter(ScanState.key == REPAIR_ORGANIC_WIDE_KEY).one_or_none():
        return
    from .features import is_organic_book
    from .model import predict

    rows = (
        session.query(Token, Research)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "sol")
        .all()
    )
    fixed = 0
    for _token, research in rows:
        try:
            feats = json.loads(research.features_json or "{}")
        except json.JSONDecodeError:
            continue
        if not feats:
            continue
        scored = predict(session, feats, chain="sol")
        old_p = float(research.p_good or 0.0)
        new_p = float(scored["p_good"])
        if new_p <= old_p + 0.03:
            continue
        research.p_good = new_p
        research.heuristic_p = scored["heuristic_p"]
        research.model_p = scored["model_p"]
        research.risk_flags_json = json.dumps(scored["risk_flags"])
        research.reasons_json = json.dumps(scored["reasons"])
        feats["organic_book"] = 1.0 if is_organic_book(feats) else float(feats.get("organic_book") or 0)
        research.features_json = json.dumps(feats)
        fixed += 1
    session.add(ScanState(key=REPAIR_ORGANIC_WIDE_KEY, value=f"lifted {fixed}"))
    session.flush()
    log.info("organic-wide book: lifted %s Sol tokens", fixed)


def repair_rh_thin_book_scores(session: Session) -> None:
    """Cap stored RH scores on sub-20-holder books.

    Live desk had JOHNAPPL/LTRL/Clanker at 62–67% with 2–6 wallets —
    socials + verified contract, no market. RH paper buys at 0.50, so
    those filled. Lower only; do not replay Sol or wide RH books.
    """
    if session.query(ScanState).filter(ScanState.key == REPAIR_RH_THIN_BOOK_KEY).one_or_none():
        return
    rows = (
        session.query(Token, Research)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .all()
    )
    fixed = 0
    for _token, research in rows:
        holders = int(research.holder_count or 0)
        old_p = float(research.p_good or 0.0)
        if holders >= 20 or old_p < 0.50:
            continue
        research.p_good = 0.48
        research.heuristic_p = min(float(research.heuristic_p or old_p), 0.48)
        try:
            flags = json.loads(research.risk_flags_json or "[]")
        except json.JSONDecodeError:
            flags = []
        if "Robinhood book is too thin to be a market" not in flags:
            flags.append("Robinhood book is too thin to be a market")
        research.risk_flags_json = json.dumps(flags)
        fixed += 1
    session.add(ScanState(key=REPAIR_RH_THIN_BOOK_KEY, value=f"capped {fixed}"))
    session.flush()
    log.info("RH thin-book cap: lowered %s tokens", fixed)


def repair_rh_empty_book_scores(session: Session) -> None:
    """Cap stored RH scores when there is no live pool.

    Live RETAILS: 36 holders / p=0.92 / last_liq=0. Thin-book only
    fires under 20 wallets, so leftover FDV still cleared the 0.50
    paper line. Lower only; leave real books and Solana alone.
    """
    if session.query(ScanState).filter(ScanState.key == REPAIR_RH_EMPTY_SCORE_KEY).one_or_none():
        return
    rows = (
        session.query(Token, Research, Outcome)
        .join(Research, Research.token_id == Token.id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .all()
    )
    fixed = 0
    for _token, research, outcome in rows:
        last_liq = float(outcome.last_liq or 0.0)
        old_p = float(research.p_good or 0.0)
        if last_liq >= DEAD_POOL_LIQ or old_p < 0.50:
            continue
        research.p_good = 0.48
        research.heuristic_p = min(float(research.heuristic_p or old_p), 0.48)
        try:
            flags = json.loads(research.risk_flags_json or "[]")
        except json.JSONDecodeError:
            flags = []
        if "Robinhood book has no live liquidity" not in flags:
            flags.append("Robinhood book has no live liquidity")
        research.risk_flags_json = json.dumps(flags)
        fixed += 1
    session.add(ScanState(key=REPAIR_RH_EMPTY_SCORE_KEY, value=f"capped {fixed}"))
    session.flush()
    log.info("RH empty-book cap: lowered %s tokens", fixed)


def hydrate_rh_empty_book(
    session: Session,
    token: Token,
    outcome: Outcome,
    market: dict | None = None,
) -> bool:
    """Fill stored RH features from last_liq when Dex snapshot was empty.

    Live BB/LOOONG: last_liq $17k/$6k, 193/56 holders, raw.market all
    zeros so liquidity_n stays 0. Patch the stored tape. Lift
    research.p_good only while multiple < 2 so paper does not buy a
    name that already ran. Thin books (<20 wallets) stay capped
    unless the sample is an LP open or Dex last_liq is already fat
    (live MEME: 4 Blockscout rows / 98.8% pool / later $1.3M liq).
    """
    if token_chain(token) != "robinhood" or token.research is None:
        return False
    research = token.research
    holders = int(research.holder_count or 0)
    try:
        raw_preview = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        raw_preview = {}
    if not isinstance(raw_preview, dict):
        raw_preview = {}
    gmgn_n = int(((raw_preview.get("gmgn") or {}) if isinstance(raw_preview.get("gmgn"), dict) else {}).get("holder_count") or 0)
    from .features import RH_FAT_BOOK_LIQ, rh_lp_open_book

    pool_open = rh_lp_open_book(raw_preview.get("holders") if isinstance(raw_preview.get("holders"), dict) else {})
    live_liq = float((market or {}).get("liquidity_usd") or 0.0) or float(outcome.last_liq or 0.0)
    if holders < 20:
        if gmgn_n >= 20:
            holders = gmgn_n
            research.holder_count = gmgn_n
        elif live_liq < RH_FAT_BOOK_LIQ and not pool_open:
            return False
    market = market or {}
    liq = float(market.get("liquidity_usd") or 0.0)
    vol = float(market.get("volume_h1") or market.get("volume_m5") or 0.0)
    if liq < DEAD_POOL_LIQ:
        liq = float(outcome.last_liq or 0.0)
    if liq < DEAD_POOL_LIQ:
        return False
    try:
        feats = json.loads(research.features_json or "{}")
    except json.JSONDecodeError:
        feats = {}
    if not isinstance(feats, dict):
        feats = {}
    if float(feats.get("liquidity_n") or 0.0) >= 0.45:
        return False
    from .features import _log_norm, is_organic_book

    feats["liquidity_n"] = _log_norm(liq, 80_000)
    if vol > 0:
        feats["volume_n"] = _log_norm(vol, 50_000)
    if holders >= 20:
        feats["holder_n"] = _log_norm(float(holders), 2_000)
        feats["rh_thin_book"] = 0.0
    elif liq >= RH_FAT_BOOK_LIQ or pool_open:
        feats["rh_thin_book"] = 0.0
        if pool_open:
            feats["rh_lp_open_book"] = 1.0
    feats["rh_empty_book"] = 0.0
    feats["organic_book"] = 1.0 if is_organic_book(feats) else float(feats.get("organic_book") or 0)
    research.features_json = json.dumps(feats)
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    stored = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    if float(stored.get("liquidity_usd") or 0.0) <= 0:
        stored = dict(stored)
        stored["liquidity_usd"] = liq
        if vol > 0:
            stored["volume_h1"] = vol
        raw["market"] = stored
        research.raw_json = json.dumps(raw, default=str)
    # Already ran — keep the entry score so paper does not fill a 3x.
    if float(outcome.multiple or 0.0) >= 2.0 or outcome.label is not None:
        return True
    from .model import predict

    scored = predict(session, feats, chain="robinhood")
    old_p = float(research.p_good or 0.0)
    new_p = float(scored["p_good"])
    if new_p > old_p + 0.03:
        research.p_good = new_p
        research.heuristic_p = scored["heuristic_p"]
        research.model_p = scored["model_p"]
        research.risk_flags_json = json.dumps(scored["risk_flags"])
        research.reasons_json = json.dumps(scored["reasons"])
    return True


def repair_hunt_mint_case(session: Session) -> None:
    """Align HuntCard.mint with Token.mint (lowercase RH). Tape batch lookup missed mixed case.

    One SQL UPDATE on Postgres — never load the full hunt_cards table on boot.
    """
    if session.query(ScanState).filter(ScanState.key == REPAIR_HUNT_MINT_ALIGN_KEY).one_or_none():
        return
    from sqlalchemy import text

    from ..models import HuntCard, Token

    apply_report_guards(session, timeout_ms=120_000)
    dialect = session.get_bind().dialect.name
    fixed = 0
    if dialect == "postgresql":
        res = session.execute(
            text(
                """
                UPDATE hunt_cards AS h
                SET mint = t.mint
                FROM tokens AS t
                WHERE h.token_id = t.id AND h.mint IS DISTINCT FROM t.mint
                """
            )
        )
        fixed = int(res.rowcount or 0)
    else:
        batch = 500
        while True:
            rows = (
                session.query(HuntCard, Token)
                .join(Token, Token.id == HuntCard.token_id)
                .filter(HuntCard.mint != Token.mint)
                .limit(batch)
                .all()
            )
            if not rows:
                break
            for card, token in rows:
                card.mint = token.mint
                fixed += 1
            session.flush()
            if len(rows) < batch:
                break
    session.add(ScanState(key=REPAIR_HUNT_MINT_ALIGN_KEY, value=f"aligned {fixed}"))
    session.flush()
    if fixed:
        log.info("hunt mint case: aligned %s cards", fixed)


def repair_rh_empty_book_from_last_liq(session: Session) -> None:
    """One-shot hydrate of empty RH Dex snapshots from last_liq."""
    if session.query(ScanState).filter(ScanState.key == REPAIR_RH_EMPTY_BOOK_KEY).one_or_none():
        return
    rows = (
        session.query(Token, Research, Outcome)
        .join(Research, Research.token_id == Token.id)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .all()
    )
    fixed = 0
    lifted = 0
    for token, research, outcome in rows:
        before = float(research.p_good or 0.0)
        if hydrate_rh_empty_book(session, token, outcome, None):
            fixed += 1
            if float(research.p_good or 0.0) > before + 0.03:
                lifted += 1
    session.add(ScanState(key=REPAIR_RH_EMPTY_BOOK_KEY, value=f"hydrated {fixed} lifted {lifted}"))
    session.flush()
    log.info("RH empty-book hydrate: patched %s tokens, lifted %s under 2x", fixed, lifted)


def repair_rh_holders_from_gmgn(session: Session) -> None:
    """Copy GMGN holder_count / top10 already sitting in raw_json onto
    Robinhood research rows that were stored with empty Helius holders."""
    if session.query(ScanState).filter(ScanState.key == REPAIR_RH_HOLDERS_KEY).one_or_none():
        return
    from .features import _log_norm
    from .model import predict

    rows = (
        session.query(Token, Research)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .all()
    )
    fixed = 0
    for token, research in rows:
        if (research.holder_count or 0) > 0:
            continue
        try:
            raw = json.loads(research.raw_json or "{}")
            feats = json.loads(research.features_json or "{}")
        except json.JSONDecodeError:
            continue
        gmgn = raw.get("gmgn") if isinstance(raw, dict) else {}
        n = int((gmgn or {}).get("holder_count") or 0)
        if n <= 0:
            continue
        top10 = float((gmgn or {}).get("top10_pct") or 0.0)
        research.holder_count = n
        if top10:
            research.top10_pct = top10
        dev = float((gmgn or {}).get("dev_hold_pct") or 0.0)
        if dev:
            research.creator_hold_pct = dev
        feats["holder_n"] = _log_norm(n, 2_000)
        if top10:
            feats["top10_inv"] = max(0.0, min(1.0, 1.0 - top10 / 100.0))
        research.features_json = json.dumps(feats)
        if isinstance(raw, dict):
            raw["holders"] = {
                "holder_count": n,
                "top10_pct": top10,
                "creator_hold_pct": dev,
                "source": "gmgn",
            }
            research.raw_json = json.dumps(raw)
        scored = predict(session, feats, chain="robinhood")
        research.p_good = scored["p_good"]
        research.heuristic_p = scored["heuristic_p"]
        research.model_p = scored["model_p"]
        research.risk_flags_json = json.dumps(scored["risk_flags"])
        research.reasons_json = json.dumps(scored["reasons"])
        fixed += 1
    session.add(ScanState(key=REPAIR_RH_HOLDERS_KEY, value=f"filled {fixed}"))
    session.flush()
    log.info("RH holders: filled %s rows from stored GMGN snapshots", fixed)


def repair_entry_prices(session: Session) -> None:
    """One-time repair: outcomes whose entry mcap was captured mid-collapse
    (t0 far below the graduation floor) carry fake 30x+ multiples. Re-anchor
    t0 at the graduation baseline, recompute the multiple, and clear any
    label so the row is re-judged with honest numbers."""
    if session.query(ScanState).filter(ScanState.key == REPAIR_T0_KEY).one_or_none():
        return
    bad = session.query(Outcome).filter(Outcome.t0_mcap < 0.4 * GRADUATION_MCAP_USD).all()
    for outcome in bad:
        outcome.t0_mcap = GRADUATION_MCAP_USD
        outcome.multiple = (outcome.max_mcap or 0.0) / GRADUATION_MCAP_USD
        outcome.label = None
        outcome.labeled_at = None
        outcome.used_for_train = False
    session.add(ScanState(key=REPAIR_T0_KEY, value=f"repaired {len(bad)} entries"))
    session.flush()
    log.info("entry price repair: re-anchored %s collapsed t0s", len(bad))


def repair_corrupt_mcaps(session: Session) -> None:
    """One-time repair: occasional garbage market-cap readings (wrong pair /
    bad units) got maxed into outcomes as absurd multiples (240M x). Rebuild
    max_mcap from sane snapshot history and re-judge the row."""
    if session.query(ScanState).filter(ScanState.key == REPAIR_MCAP_KEY).one_or_none():
        return
    # Multiples computed before the flash-print defenses (16:41) are suspect
    # in the whole 5x+ band, not just the absurd tail. Rebuild each from its
    # snapshot history under the full liquidity + age sanity rules; wins that
    # no longer qualify get re-judged.
    bad = (
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter((Outcome.max_mcap > MAX_SANE_MCAP) | (Outcome.multiple >= 5.0))
        .all()
    )
    for outcome, token in bad:
        start = token.migrated_at or token.first_seen_at
        if start is not None and start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        snaps = (
            session.query(Snapshot.taken_at, Snapshot.mcap_usd, Snapshot.liquidity_usd)
            .filter(Snapshot.token_id == outcome.token_id)
            .all()
        )
        best = 0.0
        for taken_at, mcap, liq in snaps:
            age = None
            if start is not None and taken_at is not None:
                ta = taken_at if taken_at.tzinfo else taken_at.replace(tzinfo=timezone.utc)
                age = ta - start
            best = max(best, sane_mcap(mcap, liq, age))
        outcome.max_mcap = max(best, outcome.t0_mcap or 0.0)
        outcome.multiple = outcome.max_mcap / outcome.t0_mcap if outcome.t0_mcap else 0.0
        if outcome.label == 1 and outcome.multiple < settings.win_multiple:
            outcome.label = None
            outcome.labeled_at = None
            outcome.used_for_train = False
    session.add(ScanState(key=REPAIR_MCAP_KEY, value=f"repaired {len(bad)} mcaps"))
    session.flush()
    log.info("mcap repair v6: rebuilt %s suspect multiples", len(bad))


def apply_label_fixes(session: Session) -> None:
    """One-time migration: the win definition is now WIN_MULTIPLE (default
    5x — the desk hunts runners, not bounces). Clear every label, the model,
    and the funder tallies built from old labels; all rebuilds under the
    new rule."""
    if session.query(ScanState).filter(ScanState.key == RELABEL_KEY).one_or_none():
        return
    outcomes = session.query(Outcome).filter(Outcome.label.is_not(None)).all()
    for outcome in outcomes:
        outcome.label = None
        outcome.labeled_at = None
        outcome.used_for_train = False
    for model in session.query(ModelState).all():
        model.weights_json = json.dumps({n: 0.0 for n in FEATURE_NAMES})
        model.bias = -0.4
        model.n_train = 0
        model.n_correct = 0
        model.version += 1
        model.updated_at = utcnow()
    stale_funders = session.query(ScanState).filter(ScanState.key.like("funder:%")).all()
    for row in stale_funders:
        session.delete(row)
    session.add(ScanState(key=RELABEL_KEY, value=f"cleared {len(outcomes)} labels, {len(stale_funders)} funders"))
    log.info(
        "relabel migration: win multiple now %.1fx — cleared %s labels, reset model, dropped %s funder tallies",
        settings.win_multiple,
        len(outcomes),
        len(stale_funders),
    )


def repair_sol_tiny_t0(session: Session) -> int:
    """Re-anchor Solana t0 below 0.4× the $69k floor.

    Live ARROW/RST/WWR stored GMGN's ~411 SOL graduation as USD, then
    printed a 4.5x on an $1.8k Dex book. RH CATTIES-class small books
    are not touched.
    """
    floor = graduation_mcap("sol")
    cutoff = 0.4 * floor
    rows = (
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter(
            Token.chain == "sol",
            Token.source != "backfill",
            Token.is_historical.is_(False),
            Outcome.t0_mcap > 0,
            Outcome.t0_mcap < cutoff,
        )
        .limit(200)
        .all()
    )
    fixed = 0
    for outcome, _token in rows:
        outcome.t0_mcap = floor
        peak = float(outcome.max_mcap or 0.0)
        outcome.multiple = peak / floor if floor else 0.0
        if outcome.label == 1 and outcome.multiple < settings.win_multiple:
            outcome.label = None
            outcome.labeled_at = None
            outcome.used_for_train = False
        fixed += 1
    if fixed:
        log.info("Sol tiny-t0 repair: re-anchored %s entries at $%.0f", fixed, floor)
    return fixed


def _young_refresh_rows(base, young_cutoff: datetime) -> list:
    """Young window: Sol + RH real books first; Dex-miss RH capped.

    Live 18:00: one trench poll ingested 64 leftover RH names.
    `limit(25)` with no order spent every young Dex call on last_liq=0
    infants while BUILDERMAN-class books and Sol t15m waited on
    reserved/random. POV-class Dex-miss still gets chairs so we do
    not skip the first minutes of a book Dex has not indexed yet.
    """
    young_base = base.filter(Token.migrated_at.is_not(None), Token.migrated_at >= young_cutoff)
    real_book = or_(
        Token.chain != "robinhood",
        Outcome.last_liq >= DEAD_POOL_LIQ,
    )
    young_real = (
        young_base.filter(real_book)
        .order_by(Token.migrated_at.desc())
        .limit(YOUNG_REFRESH)
        .all()
    )
    young_miss = (
        young_base.filter(
            Token.chain == "robinhood",
            or_(Outcome.last_liq.is_(None), Outcome.last_liq < DEAD_POOL_LIQ),
        )
        .order_by(Token.migrated_at.desc())
        .limit(YOUNG_RH_DEXMISS)
        .all()
    )
    reserved_real = YOUNG_REFRESH - YOUNG_RH_DEXMISS
    rows: list = []
    seen: set[int] = set()
    for row in young_real[:reserved_real] + young_miss + young_real[reserved_real:]:
        oid = row[0].id
        if oid in seen:
            continue
        seen.add(oid)
        rows.append(row)
        if len(rows) >= YOUNG_REFRESH:
            break
    return rows


def _sol_leftover_fdv_refresh_rows(base) -> list:
    """Unlabeled Sol books whose stored last is leftover FDV vs t0.

    Live NIKE `pr9pv…pump`: desk last $17.5M / t0 $69k / multiple 1.0.
    Dex PumpSwap is $2.6k. Deepest-liq used to prefer the leftover;
    even after pair-pick, the row needs a reserved tick.
    """
    return (
        base.filter(Token.chain == "sol")
        .filter(Token.is_historical.is_(False))
        .filter(Outcome.t0_mcap > 0)
        .filter(Outcome.last_mcap > MAX_HONEST_MULTIPLE * Outcome.t0_mcap)
        .order_by(Outcome.last_mcap.desc())
        .limit(SOL_LEFTOVER_FDV_REFRESH)
        .all()
    )


def _sol_doing_well_refresh_rows(base) -> list:
    """Sol 2×+ last vs t0 — Doing well tape freezes without a reserved tick.

    Live 06:40: NamekCoin last $713k / Dex $2.3k sat on Doing well
    because historical leftovers (fih $2.1M) ate the 8 chairs and
    never rewrote. Doing well already drops historical Sol. These
    chairs follow the tab. Leftover-FDV chairs stay as-is.
    """
    return (
        base.filter(Token.chain == "sol")
        .filter(Token.is_historical.is_(False))
        .filter(Outcome.t0_mcap > 0)
        .filter(Outcome.last_mcap >= WATCH_MIN_MULTIPLE * Outcome.t0_mcap)
        .order_by(Outcome.last_mcap.desc())
        .limit(SOL_DOING_WELL_REFRESH)
        .all()
    )


def _rh_doing_well_refresh_rows(base) -> list:
    """RH 2×+ last vs t0 — Doing well 90+ freezes without a reserved tick.

    Live ETAC: desk last $224k / live 92 while Dex WETH is $11k after
    an 87% dump. Sol chairs already follow the tab. RH reserved 16
    go to the fattest multiples. 90+ first, then smallest last, so a
    mid-tape dump rewrites before leftover 2× dust. Do not
    leftover-sort. Do not apply Sol leftover-FDV to RH MEME.
    """
    return (
        base.filter(Token.chain == "robinhood")
        .filter(Token.is_historical.is_(False))
        .filter(Outcome.t0_mcap > 0)
        .filter(Outcome.last_mcap >= WATCH_MIN_MULTIPLE * Outcome.t0_mcap)
        .outerjoin(Research, Research.token_id == Token.id)
        .order_by(func.coalesce(Research.p_good, 0.0).desc(), Outcome.last_mcap.asc())
        .limit(RH_DOING_WELL_REFRESH)
        .all()
    )


def _sol_near_well_refresh_rows(base) -> list:
    """Sol hunt books stuck at 1.5–2.0× stored last.

    Doing well chairs need last/t0 >= 2. Leftover-FDV chairs need >80.
    A live PumpSwap run (Laptop $77k → $279k) never gets a reserved
    tick in between. Skip leftover-size last so $5M / 1.08× USWR
    does not eat the band. Do not change leftover chairs.
    """
    return (
        base.filter(Token.chain == "sol")
        .filter(Token.is_historical.is_(False))
        .filter(Outcome.t0_mcap > 0)
        .filter(Outcome.last_mcap >= NEAR_WELL_MIN_MULTIPLE * Outcome.t0_mcap)
        .filter(Outcome.last_mcap < WATCH_MIN_MULTIPLE * Outcome.t0_mcap)
        .filter(Outcome.last_mcap < SOL_LEFTOVER_MAJOR_MCAP)
        .filter(Outcome.last_liq >= 8_000.0)
        .order_by(Token.migrated_at.desc())
        .limit(SOL_NEAR_WELL_REFRESH)
        .all()
    )


def _rh_airdrop_refresh_rows(base) -> list:
    """Unlabeled RH airdrop-class: 200+ wallets on a $800–$8k book, under 2x.

    Live Goldinu sat at p=0.92 for hours because t15m was still None and
    the random / 6h-due pool never visited. Second-look writes the 0.48
    cap only when the row is in the refresh set. Do not one-shot recap.
    """
    return (
        base.join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .filter(Research.holder_count >= 200)
        .filter(Outcome.last_liq >= DEAD_POOL_LIQ)
        .filter(Outcome.last_liq < 8_000.0)
        .filter(or_(Outcome.multiple < WATCH_MIN_MULTIPLE, Outcome.multiple.is_(None)))
        .order_by(Token.first_seen_at.desc())
        .limit(RH_AIRDROP_REFRESH)
        .all()
    )


def _rh_airdrop_needs_second_look(token: Token, outcome: Outcome, liq: float = 0.0) -> bool:
    """Goldinu-class still unlabeled and under 2x — rescore every visit."""
    if token_chain(token) != "robinhood":
        return False
    if outcome.label is not None or token.is_historical:
        return False
    if not token.research:
        return False
    if float(outcome.multiple or 0.0) >= WATCH_MIN_MULTIPLE:
        return False
    from .features import is_rh_airdrop_book

    return is_rh_airdrop_book(
        "robinhood",
        research_holder_count(token),
        outcome.last_liq,
        liq=liq,
    )


def _rh_start_high_stale_last(outcome: Outcome) -> bool:
    """True when a start-high fat book froze last on an hour-one dump.

    Live CRCL: t0 $272k / max $1.38M / last $159k / last_liq $119k.
    Crossing 2× dropped the reserved start-high chair, so last never
    tracked the Dex recovery to $1.2M. FEATURE_NAMES stays 66.
    """
    from .features import RH_FAT_BOOK_LIQ

    t0 = float(outcome.t0_mcap or 0.0)
    cutoff = MAX_HONEST_ENTRY_MULTIPLE_RH * graduation_mcap("robinhood")
    if t0 < cutoff:
        return False
    last = float(getattr(outcome, "last_mcap", 0) or 0.0)
    mx = float(outcome.max_mcap or 0.0)
    liq = float(outcome.last_liq or 0.0)
    if liq < RH_FAT_BOOK_LIQ or mx <= 0:
        return False
    if last <= 0:
        # Live CRCL: labeled at t1h so last_mcap was never written.
        return True
    return last < 0.50 * mx


def _rh_start_high_refresh_rows(base) -> list:
    """Unlabeled RH start-high leftover t0, still under 2x.

    Live KFC: t0 $146k (3.7× floor) / p=0.82 / 1.0x. Airdrop chairs
    skipped it ($40k last_liq). Second-look writes entry_premium only
    when the row is in the refresh set. Do not one-shot recap.
    Live CRCL: after 5× the under-2× cut dropped the chair and last
    froze at the t1h dump. Keep fat stale-last start-highs reserved.
    """
    from .features import RH_FAT_BOOK_LIQ

    cutoff = MAX_HONEST_ENTRY_MULTIPLE_RH * graduation_mcap("robinhood")
    return (
        base.filter(Token.chain == "robinhood")
        .filter(Outcome.t0_mcap >= cutoff)
        .filter(
            or_(
                Outcome.multiple < WATCH_MIN_MULTIPLE,
                Outcome.multiple.is_(None),
                and_(
                    Outcome.last_liq >= RH_FAT_BOOK_LIQ,
                    Outcome.last_mcap > 0,
                    Outcome.max_mcap > 0,
                    Outcome.last_mcap < 0.50 * Outcome.max_mcap,
                ),
            )
        )
        .order_by(Token.first_seen_at.desc())
        .limit(RH_START_HIGH_REFRESH)
        .all()
    )


def _rh_start_high_needs_second_look(token: Token, outcome: Outcome) -> bool:
    if token_chain(token) != "robinhood":
        return False
    if outcome.label is not None or token.is_historical:
        return False
    if not token.research:
        return False
    if float(outcome.multiple or 0.0) >= WATCH_MIN_MULTIPLE and not _rh_start_high_stale_last(outcome):
        return False
    t0 = float(outcome.t0_mcap or 0.0)
    return t0 >= MAX_HONEST_ENTRY_MULTIPLE_RH * graduation_mcap("robinhood")


def _rh_floor_gap(p_good: float, heuristic_p: float) -> bool:
    """Stored p sat under heuristic after a young-floor lapse.

    Skip the 0.48 veto shelf (Claude / LIVOCAT) — that cap is honest.
    """
    p = float(p_good or 0.0)
    hp = float(heuristic_p or 0.0)
    if hp - p < 0.02:
        return False
    if abs(p - 0.48) < 0.005 and hp > 0.48:
        return False
    return True


def _rh_floor_restore_refresh_rows(base) -> list:
    """Unlabeled RH mid-books whose stored p is still under heuristic.

    Live DCE/CASHTOPUS never matched airdrop or start-high chairs, so
    second-look never re-applied the 1080 floor. Do not one-shot recap.
    2x+ (YOLO / GUH / CASHBIRD) stay frozen.
    """
    return (
        base.join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .filter(Outcome.last_liq >= DEAD_POOL_LIQ)
        .filter(or_(Outcome.multiple < WATCH_MIN_MULTIPLE, Outcome.multiple.is_(None)))
        .filter(Research.p_good < Research.heuristic_p - 0.02)
        .filter(or_(Research.p_good < 0.475, Research.p_good > 0.485))
        # First-sight Entries are frozen at second look: nothing to restore.
        .filter(or_(Research.scorer.is_(None), Research.scorer != "first_sight"))
        .order_by((Research.heuristic_p - Research.p_good).desc())
        .limit(RH_FLOOR_RESTORE_REFRESH)
        .all()
    )


def _rh_wallet_map_refresh_rows(base) -> list:
    """Unlabeled RH real books whose detail card has no top_wallets.

    Live THEINVESTOR sat 7h on graduating with the GMGN count but no
    address table. Blockscout fills the map; do not add GMGN HTTP.
    """
    return (
        base.join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .filter(Outcome.last_liq >= DEAD_POOL_LIQ)
        .filter(
            or_(
                not_(Research.raw_json.contains("top_wallets")),
                Research.raw_json.contains('"top_wallets": []'),
                Research.raw_json.contains('"top_wallets":[]'),
            )
        )
        .order_by(Token.first_seen_at.desc())
        .limit(RH_WALLET_MAP_REFRESH)
        .all()
    )


def _rh_pool_open_on_row(token: Token) -> bool:
    research = getattr(token, "research", None)
    if research is None:
        return False
    try:
        feats = json.loads(research.features_json or "{}")
    except json.JSONDecodeError:
        feats = {}
    if isinstance(feats, dict) and feats.get("rh_lp_open_book"):
        return True
    holders = _research_raw(research).get("holders")
    from .features import rh_lp_open_book

    return isinstance(holders, dict) and rh_lp_open_book(holders)


def _rh_lp_open_refresh_rows(base) -> list:
    """Unlabeled RH pool-opens still under paper and under 2×.

    Live MEME sat at 0.03 while the book ran 2.8× in six minutes.
    Wallet-map chairs hydrate addresses but do not rescore. Young
    25 floods. Do not one-shot recap. FEATURE_NAMES stays 66.
    """
    return (
        base.join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .filter(Outcome.last_liq >= DEAD_POOL_LIQ)
        .filter(or_(Outcome.multiple < WATCH_MIN_MULTIPLE, Outcome.multiple.is_(None)))
        .filter(Research.p_good < 0.50)
        # First-sight Entries are frozen at second look: a rescore chair is wasted.
        .filter(or_(Research.scorer.is_(None), Research.scorer != "first_sight"))
        .filter(
            or_(
                Research.features_json.contains('"rh_lp_open_book": 1'),
                Research.features_json.contains('"rh_lp_open_book": 1.0'),
                Research.raw_json.contains('"label": "pool"'),
                Research.raw_json.contains('"label":"pool"'),
            )
        )
        .order_by(Token.first_seen_at.desc())
        .limit(RH_LP_OPEN_REFRESH)
        .all()
    )


def _rh_lp_open_needs_second_look(token: Token, outcome: Outcome) -> bool:
    """MEME-class still unlabeled and under 2× — rescore after the map."""
    if token_chain(token) != "robinhood":
        return False
    if outcome.label is not None or token.is_historical:
        return False
    if not token.research:
        return False
    if float(outcome.multiple or 0.0) >= WATCH_MIN_MULTIPLE:
        return False
    if float(outcome.last_liq or 0.0) < DEAD_POOL_LIQ:
        return False
    if not _rh_pool_open_on_row(token):
        return False
    p = float(token.research.p_good or 0.0)
    try:
        feats = json.loads(token.research.features_json or "{}")
    except json.JSONDecodeError:
        feats = {}
    if not isinstance(feats, dict):
        feats = {}
    raw_gmgn = _research_raw(token.research).get("gmgn")
    late_gmgn = isinstance(raw_gmgn, dict) and bool(raw_gmgn.get("source")) and not feats.get("gmgn_present")
    return p < 0.50 or late_gmgn or bool(feats.get("rh_thin_book"))


def _token_age_min(token: Token) -> float:
    start = token.created_at_chain or token.first_seen_at or token.migrated_at
    if start is None:
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - start).total_seconds() / 60.0)


def _outcome_multiple(outcome: Outcome) -> float:
    multiple = float(outcome.multiple or 0.0)
    t0 = float(outcome.t0_mcap or 0.0)
    mx = float(outcome.max_mcap or 0.0)
    if multiple <= 0 and t0 > 0 and mx > 0:
        return mx / t0
    return multiple


def _rh_stall_refresh_rows(base) -> list:
    """Unlabeled RH real books whose stored p is still high at 1.x."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    return (
        base.join(Research, Research.token_id == Token.id)
        .filter(Token.chain == "robinhood")
        .filter(Outcome.last_liq >= DEAD_POOL_LIQ)
        .filter(or_(Outcome.multiple < WATCH_MIN_MULTIPLE, Outcome.multiple.is_(None)))
        .filter(Research.p_good >= 0.50)
        .filter(
            or_(
                Token.first_seen_at <= cutoff,
                Token.created_at_chain <= cutoff,
            )
        )
        .order_by(Research.p_good.desc())
        .limit(RH_STALL_REFRESH)
        .all()
    )


def _rh_stall_needs_second_look(token: Token, outcome: Outcome) -> bool:
    if token_chain(token) != "robinhood":
        return False
    if outcome.label is not None or token.is_historical:
        return False
    if not token.research:
        return False
    multiple = _outcome_multiple(outcome)
    if multiple >= WATCH_MIN_MULTIPLE:
        return False
    if float(outcome.last_liq or 0.0) < DEAD_POOL_LIQ:
        return False
    p = float(token.research.p_good or 0.0)
    if p < 0.50:
        return False
    faded = stall_honesty_cap(p, age_min=_token_age_min(token), multiple=multiple)
    return faded + 0.02 < p


def _rh_floor_restore_needs_second_look(token: Token, outcome: Outcome) -> bool:
    if token_chain(token) != "robinhood":
        return False
    if outcome.label is not None or token.is_historical:
        return False
    if not token.research:
        return False
    if float(outcome.multiple or 0.0) >= WATCH_MIN_MULTIPLE:
        return False
    if float(outcome.last_liq or 0.0) < DEAD_POOL_LIQ:
        return False
    hp = float(token.research.heuristic_p or 0.0)
    multiple = _outcome_multiple(outcome)
    if stall_honesty_cap(hp, age_min=_token_age_min(token), multiple=multiple) + 0.02 < hp:
        return False
    return _rh_floor_gap(token.research.p_good, token.research.heuristic_p)


def _high_score_needs_second_look(
    token: Token, outcome: Outcome, high_score_mints: set[str] | None = None
) -> bool:
    """Re-score a 90+ on the reserved review tick.

    Does not rewrite Hunt entry_p. _commit_live_score still blocks a
    live lift after 2× and only lowers on the existing vetoes. The
    Dex last/t0 write is what lets desk_entry_cap see two-tick /
    collapsed / phantom garbage. FEATURE_NAMES stays 66.
    """
    if outcome.label is not None or token.is_historical:
        return False
    if not token.research:
        return False
    if high_score_mints and token.mint in high_score_mints:
        return True
    from ..desk_lines import lines_for_scorer

    return float(token.research.p_good or 0.0) >= lines_for_scorer(token.research.scorer, token_chain(token)).hi


async def refresh_outcomes(session: Session) -> int:
    from .bloom import begin_bloom_cycle

    begin_bloom_cycle()
    try:
        from ..research.live_social import refresh_live_social

        await refresh_live_social(session)
    except Exception:
        log.exception("live social refresh failed")
    # Live 05:40: /health sat 3.5s while these sync repairs ran as one
    # block before the per-token sleep(0). Yield between each.
    for repair in (
        repair_rh_ghost_t0,
        restore_rh_late_dex_junk_t0,
        repair_rh_late_dex_t0,
        repair_rh_ghost_peak,
        repair_sol_tiny_t0,
        repair_rh_leftover_fdv,
        repair_rh_quiet_ghosts,
        restore_rh_ingest_grace,
    ):
        await asyncio.sleep(0)
        repair(session)
    # Drop the write txn before the Dex/Pump tick loop so a 70-name
    # refresh cannot hold row locks (or ingest_lock, if the caller
    # took it) across minutes of HTTP. Live v61: hunt froze 78m.
    session.commit()
    now = datetime.now(timezone.utc)
    base = (
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter(Outcome.label.is_(None))
    )
    # Two pools so nothing starves: young tokens get t15m on time, and
    # the rest rotate randomly. A plain unordered limit(25) first let
    # the oldest 25 pending monopolize the window; later a 64-name RH
    # trench flood spent it on Dex-miss leftovers. Real books / Sol
    # first; Dex-miss infants are capped.
    young_cutoff = now - timedelta(minutes=35)
    young = _young_refresh_rows(base, young_cutoff)
    leftover_fdv_rows = _sol_leftover_fdv_refresh_rows(base)
    doing_well_rows = _sol_doing_well_refresh_rows(base)
    rh_doing_well_rows = _rh_doing_well_refresh_rows(base)
    near_well_rows = _sol_near_well_refresh_rows(base)
    from .hunt import high_score_mints_needing_review, hunt_mints_needing_tick

    hunt_mints = hunt_mints_needing_tick(session, limit=HUNT_TICK_REFRESH)
    hunt_tick_rows = (
        base.filter(Token.mint.in_(hunt_mints)).all() if hunt_mints else []
    )
    high_score_mints = high_score_mints_needing_review(session, limit=HIGH_SCORE_REVIEW_REFRESH)
    high_score_rows = (
        base.filter(Token.mint.in_(high_score_mints)).all() if high_score_mints else []
    )
    high_score_set = set(high_score_mints)
    airdrop_rows = _rh_airdrop_refresh_rows(base)
    start_high_rows = _rh_start_high_refresh_rows(base)
    floor_restore_rows = _rh_floor_restore_refresh_rows(base)
    stall_rows = _rh_stall_refresh_rows(base)
    wallet_map_rows = _rh_wallet_map_refresh_rows(base)
    lp_open_rows = _rh_lp_open_refresh_rows(base)
    young_ids = {o.id for o, _ in young}
    rest = (
        base.filter((Token.migrated_at.is_(None)) | (Token.migrated_at < young_cutoff))
        .order_by(func.random())
        .limit(30)
        .all()
    )
    # Solana unlabeled (~600) would otherwise crowd RH out of the random 30.
    # Prefer 24h-due real books (final rug/win label) then 6h-due then
    # 2x+ climbers so the RH desk actually gets labels instead of a random 8.
    # Catch-up stamps trench open as migrated_at (hours ago) with last_liq
    # still 0 — those leftovers were priority-0 and starved WOODY-class
    # books we have actually watched.
    six_h_ago = now - HORIZONS["t6h"]
    day_ago = now - HORIZONS["t24h"]
    watched_six_h = or_(
        Token.first_seen_at <= six_h_ago,
        and_(
            Token.first_seen_at.is_(None),
            Token.migrated_at.is_not(None),
            Token.migrated_at <= six_h_ago,
        ),
    )
    watched_day = or_(
        Token.first_seen_at <= day_ago,
        and_(
            Token.first_seen_at.is_(None),
            Token.migrated_at.is_not(None),
            Token.migrated_at <= day_ago,
        ),
    )
    rh_priority = case(
        (
            and_(
                Outcome.t24h_mcap.is_(None),
                Outcome.last_liq >= DEAD_POOL_LIQ,
                watched_day,
            ),
            0,
        ),
        (
            Outcome.multiple >= WATCH_MIN_MULTIPLE,
            1,
        ),
        (
            and_(
                Outcome.t6h_mcap.is_(None),
                Outcome.last_liq >= DEAD_POOL_LIQ,
                watched_six_h,
            ),
            2,
        ),
        (
            and_(Outcome.t6h_mcap.is_(None), watched_six_h),
            3,
        ),
        else_=4,
    )
    rh_reserved = (
        base.filter(Token.chain == "robinhood")
        .order_by(rh_priority, Outcome.multiple.desc(), Token.migrated_at.asc())
        .limit(16)
        .all()
    )
    from .hunt import historical_hydrate_refresh_rows

    seen = set(young_ids)
    pending: list[tuple[Outcome, Token]] = list(young)
    for outcome, token in historical_hydrate_refresh_rows(session, limit_per_chain=8):
        if outcome.id in seen:
            continue
        seen.add(outcome.id)
        pending.insert(0, (outcome, token))
    for row in leftover_fdv_rows + doing_well_rows + rh_doing_well_rows + near_well_rows + hunt_tick_rows + high_score_rows + airdrop_rows + start_high_rows + floor_restore_rows + stall_rows + wallet_map_rows + lp_open_rows + rest + rh_reserved:
        if row[0].id in seen:
            continue
        seen.add(row[0].id)
        pending.append(row)
    updated = 0
    newly_labeled: list[tuple[Token, int]] = []
    for tick_idx, (outcome, token) in enumerate(pending):
        # Live 18:40: /health hit 5–8s and 502 while this loop held the
        # event loop across ~70 sequential Dex ticks. Yield so desk
        # reads can finish between tokens.
        await asyncio.sleep(0)
        # Commit the previous tick so live poll/WS can write new mints
        # while this awaits Dex. expire_on_commit reloads outcome/token.
        try:
            session.commit()
        except Exception as exc:
            from .hunt import recover_session_after_lock

            if recover_session_after_lock(session, exc, outer=True):
                log.warning(
                    "refresh_outcomes pre-tick lock defer %s (%s)",
                    tick_idx,
                    type(exc).__name__,
                )
                continue
            raise
        if tick_idx and tick_idx % 12 == 0:
            try:
                from ..ledger import beat

                beat(session, "tape_refresh", note=f"refresh progress {tick_idx}")
                session.commit()
            except Exception as exc:
                from .hunt import recover_session_after_lock

                if recover_session_after_lock(session, exc, outer=True):
                    log.warning(
                        "tape_refresh progress lock defer %s (%s)",
                        tick_idx,
                        type(exc).__name__,
                    )
                    continue
                log.exception("tape_refresh progress heartbeat failed")
        prior_label = outcome.label
        start = token.migrated_at or token.first_seen_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        chain = token_chain(token)
        if chain == "robinhood" and token.research:
            from ..research.holders import hydrate_rh_wallet_map

            # Detail page also hydrates. Chairs pull 7h-old graduating
            # books that already spent t15m/t1h/t6h and never got a map.
            await hydrate_rh_wallet_map(session, token)
        market = await dexscreener.token_market_for_token(token, chain=chain)
        if token.is_historical:
            from .hunt_tape import apply_hunt_tape_market

            if apply_hunt_tape_market(session, token, market or {}, now=now):
                updated += 1
                continue
        coin = await pumpfun.get_coin(token.mint) if chain == "sol" else {}
        liq = float((market or {}).get("liquidity_usd") or 0.0)
        age_now = now - start
        vol_h1 = float((market or {}).get("volume_h1") or 0.0)
        mcap = sane_mcap((market or {}).get("mcap_usd"), liq, age_now) or sane_mcap((coin or {}).get("mcap_usd"), liq, age_now)
        ghost_tick = chain == "robinhood" and is_ghost_book(liq, vol_h1)
        if ghost_tick:
            # Live RWA: flash FDV last $958k stuck after the pair went
            # quiet — ghost ticks skipped the write, so last never
            # reverted to the $64k snap. FEATURE_NAMES stays 66.
            revert_unbacked_last(session, token, outcome)
        if mcap > 0 and not ghost_tick:
            parked_liq = float(outcome.last_liq or 0.0)
            reanchor_ghost_robinhood(session, token, outcome, mcap, liq, vol_h1)
            t0_now = float(outcome.t0_mcap or 0.0)
            if t0_now > 0:
                # Live 𝕏LIFE: t0 $69k floor, Dex $8.3M / 9 wallets / 120x,
                # then _early_label wrote a win. Post-label already uses
                # honest_tracked_peak; the live unlabeled path did not.
                peak, tracked = honest_tracked_peak(t0_now, float(outcome.max_mcap or 0.0), mcap)
                outcome.max_mcap = peak
                outcome.multiple = tracked
            else:
                outcome.max_mcap = max(outcome.max_mcap or 0.0, mcap)
            if chain == "robinhood":
                from .features import prefer_rh_dust_liq

                # Re-anchor can write leftover Dex onto last_liq. Keep a
                # parked dust book (Goldinu $4k) so airdrop second-look sees it.
                outcome.last_liq = prefer_rh_dust_liq(parked_liq, outcome.last_liq)
                outcome.last_liq = prefer_rh_dust_liq(outcome.last_liq, liq)
            elif liq > 0:
                outcome.last_liq = liq
            # Live 10:00: Laptop GsewXp last $209k / Dex $43k liq
            # sat last_liq=0 after a Dex tick with mcap and no liq.
            # Runners need last_liq≥$800 so Doing well dropped a
            # labeled 5×. A Dex miss is not a dead book.
            record_live_last(session, token, outcome, market or {}, mcap, now, start=start)
            from .hunt_tape import write_tape_bar

            write_tape_bar(session, token, market or {}, mcap=mcap, liq=float(outcome.last_liq or liq or 0.0), now=now)
            age = now - start
            for key, delta in HORIZONS.items():
                if age >= delta and getattr(outcome, f"{key}_mcap") is None:
                    setattr(outcome, f"{key}_mcap", mcap)
                    p_now = token.research.p_good if token.research else 0.0
                    if key in ("t15m", "t6h") and token.research and not token.is_historical:
                        p_now = await _second_look(session, token, outcome, market or {})
                    # t15m too: live HELLO 2.38 / BUILDERMAN 2.76 sat on
                    # approaching for an hour while watch waited for t1h.
                    if key in ("t15m", "t1h", "t6h") and token.research and token.source != "backfill":
                        await _runner_watch(session, token, outcome, market or {})
                    session.add(
                        Snapshot(
                            token=token,
                            kind=key,
                            mcap_usd=mcap,
                            price_usd=float((market or {}).get("price_usd") or 0.0),
                            volume_h1=float((market or {}).get("volume_h1") or 0.0),
                            liquidity_usd=liq,
                            p_good=p_now,
                            score=p_now * 100.0,
                        )
                    )
            if (
                age < EARLY_SCAN_WINDOW
                and token.research
                and token.source != "backfill"
                and not token.is_historical
                and _early_scan_due(session, token.id, now)
            ):
                p_early = await _second_look(session, token, outcome, market or {})
                if token.source != "backfill":
                    await _runner_watch(session, token, outcome, market or {})
                session.add(
                    Snapshot(
                        token=token,
                        kind="early",
                        mcap_usd=mcap,
                        price_usd=float((market or {}).get("price_usd") or 0.0),
                        volume_h1=float((market or {}).get("volume_h1") or 0.0),
                        volume_m5=float((market or {}).get("volume_m5") or 0.0),
                        liquidity_usd=liq,
                        buys_m5=int((market or {}).get("buys_m5") or 0),
                        sells_m5=int((market or {}).get("sells_m5") or 0),
                        p_good=p_early,
                        score=p_early * 100.0,
                    )
                )
            if outcome.t0_mcap > 0 and float(outcome.multiple or 0.0) <= 0:
                outcome.multiple = (outcome.max_mcap or 0.0) / outcome.t0_mcap
            # Horizons only fire once. BUILDERMAN climbed 1.x → 2.76 after
            # t1h already wrote (or skipped); approaching saw the snap,
            # watch did not. Re-score every live refresh; out-of-band
            # ratios still delete the row.
            if token.research and token.source != "backfill" and not token.is_historical:
                await _runner_watch(session, token, outcome, market or {})
                from .bloom import consider_bloom
                from .hunt import upsert_hunt
                from .alert_tiers import maybe_lift_ping

                bloom_row = await consider_bloom(session, token, outcome, market or {}, now=now)
                conv = float((bloom_row or {}).get("promise_p") or 0.0)
                upsert_hunt(session, token, conviction_p=conv or None, now=now)
                flags_live = []
                if token.research:
                    try:
                        flags_live = json.loads(token.research.risk_flags_json or "[]")
                    except json.JSONDecodeError:
                        flags_live = []
                await maybe_lift_ping(
                    session,
                    token,
                    entry_p=float(token.research.p_good or 0.0) if token.research else 0.0,
                    conviction_p=conv,
                    multiple=float(outcome.multiple or 0.0),
                    last_liq=float(outcome.last_liq or 0.0),
                    last_mcap=float(outcome.last_mcap or 0.0),
                    t0_mcap=float(outcome.t0_mcap or 0.0),
                    flags=flags_live,
                    chain=chain,
                )
            # Horizons fire once. Goldinu's t15m was still None after hours
            # of desk time; once it is written, airdrop-class still needs
            # a live write so the 0.48 cap lands without a one-shot recap.
            if (
                _rh_airdrop_needs_second_look(token, outcome, liq)
                or _rh_start_high_needs_second_look(token, outcome)
                or _rh_floor_restore_needs_second_look(token, outcome)
                or _rh_stall_needs_second_look(token, outcome)
                or _rh_lp_open_needs_second_look(token, outcome)
                or _high_score_needs_second_look(token, outcome, high_score_set)
            ):
                await _second_look(session, token, outcome, market or {})
            if retire_quiet_robinhood(token, outcome, age, vol_h1):
                log.info(
                    "RH quiet retire %s age=%.1fh mult=%.2f vol_h1=%.0f",
                    token.symbol,
                    age.total_seconds() / 3600.0,
                    outcome.multiple or 0.0,
                    vol_h1,
                )
            # Tokens we watched from migration can't have a real 9-figure ath
            # within days; Pump.fun's ath_mcap echoes manipulated flash prints.
            ath_cap = EARLY_SANE_MCAP if token.source != "backfill" else MAX_SANE_MCAP
            holders = research_holder_count(token)
            if age >= HORIZONS["t24h"]:
                _label(
                    outcome, coin or {},
                    historical=_watched_label_is_historical(token),
                    ath_ceiling=ath_cap, holders=holders, chain=chain,
                )
            elif token.is_historical and (coin or {}).get("ath_mcap"):
                _label(outcome, coin or {}, historical=True, ath_ceiling=ath_cap, holders=holders, chain=chain)
            else:
                _early_label(outcome, age, holders=holders, chain=chain)
            updated += 1
        elif chain == "robinhood":
            # Dex miss or leftover-LP ghost: still quiet-retire and, at 24h,
            # judge from stored numbers so the first RH labels are not skipped.
            age = age_now
            if mcap > 0:
                reanchor_ghost_robinhood(session, token, outcome, mcap, liq, vol_h1)
                if outcome.t0_mcap > 0:
                    outcome.multiple = (outcome.max_mcap or 0.0) / outcome.t0_mcap
            if retire_quiet_robinhood(token, outcome, age, 0.0, seen_book=False):
                log.info(
                    "RH quiet retire %s age=%.1fh mult=%.2f vol_h1=%.0f (no live book)",
                    token.symbol,
                    age.total_seconds() / 3600.0,
                    outcome.multiple or 0.0,
                    vol_h1,
                )
            if age >= HORIZONS["t24h"]:
                ath_cap = EARLY_SANE_MCAP if token.source != "backfill" else MAX_SANE_MCAP
                seed_rh_t24h_from_snaps(session, token, outcome)
                _label(
                    outcome, coin or {},
                    historical=_watched_label_is_historical(token),
                    ath_ceiling=ath_cap,
                    holders=research_holder_count(token),
                    chain=chain,
                )
            elif float(outcome.last_liq or 0.0) >= DEAD_POOL_LIQ:
                # Real stored book, Dex just missed this poll. Seed due
                # horizons so 6h-due clears, and label a 5x from tape.
                seed_rh_horizons_from_snaps(session, token, outcome, age)
                if token.research and outcome.label is None and not token.is_historical:
                    await _second_look(session, token, outcome, market or {})
                    if token.source != "backfill":
                        await _runner_watch(session, token, outcome, market or {})
                _early_label(
                    outcome,
                    age,
                    holders=research_holder_count(token),
                    chain=chain,
                )
            updated += 1
        elif token.is_historical and (coin or {}).get("ath_mcap"):
            ath_cap = EARLY_SANE_MCAP if token.source != "backfill" else MAX_SANE_MCAP
            outcome.max_mcap = max(outcome.max_mcap or 0.0, sane_mcap(coin.get("ath_mcap"), ceiling=ath_cap))
            _label(
                outcome, coin or {}, historical=True, ath_ceiling=ath_cap,
                holders=research_holder_count(token), chain=token_chain(token),
            )
            updated += 1
        if chain == "robinhood" and token.research and not token.is_historical:
            hydrate_rh_empty_book(session, token, outcome, market or {})
        # Early 5x (live ROCK: labeled in minutes) leaves the unlabeled
        # pool before t1h, so runnerwatch never saw it. Write the row now.
        if (
            prior_label is None
            and outcome.label == 1
            and token.research
            and token.source != "backfill"
        ):
            try:
                await _runner_watch(session, token, outcome, market or {})
            except Exception:
                log.exception("early-win runner watch failed for %s", token.mint)
        if outcome.label is not None:
            newly_labeled.append((token, int(outcome.label)))
    for token, label in newly_labeled:
        _update_funder_stats(session, token, label)
    await _track_post_label(session, now)
    session.flush()
    trained = train_pending(session)
    if trained:
        log.info("model learned from %s new labels", trained)
    from .model import snapshot_evaluation

    if snapshot_evaluation(session, chain="sol") or snapshot_evaluation(session, chain="robinhood"):
        log.info("hourly evaluation snapshot recorded")
    return updated


async def _track_post_label(session: Session, now: datetime) -> None:
    """Runners make their 10-50x over days, not the 24h label window. Keep
    refreshing labeled, still-alive tokens for 72h so true runner multiples
    are recorded. Never relabels — only max_mcap/multiple and a snapshot."""
    cutoff = now - timedelta(hours=72)
    labeled = (
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter(
            Outcome.label.is_not(None),
            Outcome.last_liq >= 800,
            Token.source != "backfill",
            Token.first_seen_at >= cutoff,
        )
    )
    existing = {
        key.split(":", 1)[1]
        for (key,) in session.query(ScanState.key).filter(ScanState.key.like("runnerp:%")).all()
        if ":" in key
    }
    missing = [
        (outcome, token)
        for outcome, token in labeled.filter(Outcome.label == 1, Outcome.multiple >= settings.win_multiple)
        .order_by(Outcome.multiple.desc())
        .limit(16)
        .all()
        if token.mint not in existing
    ][:4]
    stale_high = [
        (outcome, token)
        for outcome, token in labeled.filter(Token.chain == "robinhood")
        .order_by(Token.first_seen_at.desc())
        .limit(24)
        .all()
        if _rh_start_high_stale_last(outcome)
    ][:4]
    leftover_fdv = (
        labeled.filter(Token.chain == "sol")
        .filter(Outcome.t0_mcap > 0)
        .filter(Outcome.last_mcap > MAX_HONEST_MULTIPLE * Outcome.t0_mcap)
        .order_by(Outcome.last_mcap.desc())
        .limit(SOL_LEFTOVER_FDV_REFRESH)
        .all()
    )
    # Live 10:00: Laptop GsewXp labeled 5× / last $209k sat last_liq=0.
    # The 72h labeled pool and Doing well chairs both require
    # last_liq≥$800, so it never got a tick to restore Dex $43k.
    # Do not change leftover chairs.
    liq_miss = (
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter(Outcome.label.is_not(None))
        .filter(Token.chain == "sol")
        .filter(Token.is_historical.is_(False))
        .filter(Token.source != "backfill")
        .filter(Outcome.t0_mcap > 0)
        .filter(Outcome.last_mcap >= WATCH_MIN_MULTIPLE * Outcome.t0_mcap)
        .filter(or_(Outcome.last_liq.is_(None), Outcome.last_liq < DEAD_POOL_LIQ))
        .order_by(Outcome.last_mcap.desc())
        .limit(8)
        .all()
    )
    # Doing well lists confirmed 5× with no 72h cap. Live fih / SAAR /
    # KAT first_seen Sep 3–4 still showed $0.6M–$2.1M last after Dex
    # dumped to $2k. Do not reuse the 72h labeled window.
    doing_well = _sol_doing_well_refresh_rows(
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter(Outcome.label.is_not(None))
        .filter(Token.source != "backfill")
        .filter(Outcome.last_liq >= 800)
    )
    rh_doing_well = _rh_doing_well_refresh_rows(
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter(Outcome.label.is_not(None))
        .filter(Token.source != "backfill")
        .filter(Outcome.last_liq >= 800)
    )
    near_well = _sol_near_well_refresh_rows(
        session.query(Outcome, Token)
        .join(Token, Token.id == Outcome.token_id)
        .filter(Outcome.label.is_not(None))
        .filter(Token.source != "backfill")
        .filter(Outcome.last_liq >= 800)
    )
    rest = labeled.order_by(func.random()).limit(10).all()
    seen_ids = {outcome.id for outcome, _ in missing}
    rows = list(missing)
    for outcome, token in leftover_fdv:
        if outcome.id in seen_ids:
            continue
        seen_ids.add(outcome.id)
        rows.append((outcome, token))
    for outcome, token in liq_miss:
        if outcome.id in seen_ids:
            continue
        seen_ids.add(outcome.id)
        rows.append((outcome, token))
    for outcome, token in doing_well:
        if outcome.id in seen_ids:
            continue
        seen_ids.add(outcome.id)
        rows.append((outcome, token))
    for outcome, token in rh_doing_well:
        if outcome.id in seen_ids:
            continue
        seen_ids.add(outcome.id)
        rows.append((outcome, token))
    for outcome, token in near_well:
        if outcome.id in seen_ids:
            continue
        seen_ids.add(outcome.id)
        rows.append((outcome, token))
    for outcome, token in stale_high:
        if outcome.id in seen_ids:
            continue
        seen_ids.add(outcome.id)
        rows.append((outcome, token))
    for outcome, token in rest:
        if outcome.id in seen_ids:
            continue
        seen_ids.add(outcome.id)
        rows.append((outcome, token))
        if len(rows) >= 14:
            break
    for outcome, token in rows:
        market = await dexscreener.token_market_for_token(token, chain=token_chain(token))
        if token.is_historical:
            from .hunt_tape import apply_hunt_tape_market

            if apply_hunt_tape_market(session, token, market or {}, now=now):
                continue
        mcap = sane_mcap((market or {}).get("mcap_usd"), (market or {}).get("liquidity_usd"))
        if mcap <= 0:
            continue
        was = outcome.multiple or 0.0
        peak, multiple = honest_tracked_peak(float(outcome.t0_mcap or 0.0), float(outcome.max_mcap or 0.0), mcap)
        outcome.max_mcap = peak
        live_liq = float((market or {}).get("liquidity_usd") or 0.0)
        if token_chain(token) == "robinhood":
            from .features import prefer_rh_dust_liq

            outcome.last_liq = prefer_rh_dust_liq(outcome.last_liq, live_liq)
        elif live_liq > 0:
            outcome.last_liq = live_liq
        # Sol: keep stored last_liq when Dex omits liq (Laptop GsewXp).
        if outcome.t0_mcap > 0:
            outcome.multiple = multiple
        record_live_last(session, token, outcome, market or {}, mcap, now, start=token.migrated_at or token.first_seen_at)
        from .hunt_tape import write_tape_bar

        write_tape_bar(
            session,
            token,
            market or {},
            mcap=mcap,
            liq=float(outcome.last_liq or 0.0),
            now=now,
        )
        session.add(
            Snapshot(
                token=token,
                kind="post",
                mcap_usd=mcap,
                price_usd=float((market or {}).get("price_usd") or 0.0),
                volume_h1=float((market or {}).get("volume_h1") or 0.0),
                liquidity_usd=outcome.last_liq,
                p_good=token.research.p_good if token.research else 0.0,
                score=(token.research.p_good * 100.0) if token.research else 0.0,
            )
        )
        # Keep the runner trajectory score live for the whole 72h window,
        # not just the 1h/6h horizons.
        if token.research:
            try:
                await _runner_watch(session, token, outcome, market or {})
            except Exception:
                log.exception("post-label runner watch failed for %s", token.mint)
            try:
                from .bloom import consider_bloom
                from .hunt import upsert_hunt

                bloom_row = await consider_bloom(session, token, outcome, market or {}, now=now)
                conv = float((bloom_row or {}).get("promise_p") or 0.0)
                upsert_hunt(session, token, conviction_p=conv or None, now=now)
            except Exception:
                log.exception("post-label bloom failed for %s", token.mint)
        if was < RUNNER_MULTIPLE <= (outcome.multiple or 0.0):
            log.info("RUNNER: %s crossed 10x (%.1fx) entry score %.2f", token.symbol, outcome.multiple, token.research.p_good if token.research else 0.0)
            _credit_alpha_wallets(session, token)


def retarget_live_pool(token: Token, market: dict) -> bool:
    """Point the Dex card link at the book we just priced.

    Live MEME: ingest stored the first V4 pair; the user chart and
    the deepest live book were later pairs. _best_pair already picks
    deepest liq — persist that pair_address.
    """
    pair = str((market or {}).get("pair_address") or "").strip()
    if not dexscreener.is_dex_pair_id(pair):
        return False
    clipped = pair[:128]
    if (token.pool_address or "") == clipped:
        return False
    token.pool_address = clipped
    return True


def revert_unbacked_last(session: Session, token: Token, outcome: Outcome) -> bool:
    """Put last back on the stored tape when a flash FDV went quiet.

    Live RWA: last $958k / no late snap after 06:30Z. Does not rewrite
    research.p_good. MEME last $82M over an 80×-capped max stays.
    """
    from ..serialize import is_unbacked_last_card

    snap = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token.id, Snapshot.mcap_usd > 0)
        .order_by(Snapshot.taken_at.desc())
        .first()
    )
    if snap is None:
        return False
    taken = snap.taken_at
    if taken is not None and taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age = max(0.0, (now - taken).total_seconds() / 60.0) if taken is not None else 0.0
    last = float(outcome.last_mcap or 0.0)
    if not is_unbacked_last_card(
        {
            "last_mcap": last,
            "snap_mcap": float(snap.mcap_usd or 0.0),
            "snap_age_min": age,
            "stored_max_mcap": float(outcome.max_mcap or 0.0),
        }
    ):
        return False
    peak = (
        session.query(func.max(Snapshot.mcap_usd))
        .filter(Snapshot.token_id == token.id)
        .scalar()
    )
    snap_mcap = float(snap.mcap_usd or 0.0)
    outcome.last_mcap = snap_mcap
    if float(snap.liquidity_usd or 0.0) > 0:
        outcome.last_liq = float(snap.liquidity_usd)
    outcome.max_mcap = max(snap_mcap, float(peak or 0.0))
    t0 = float(outcome.t0_mcap or 0.0)
    if t0 > 0:
        outcome.multiple = snap_mcap / t0
    return True


def record_live_last(
    session: Session,
    token: Token,
    outcome: Outcome,
    market: dict,
    mcap: float,
    now: datetime,
    *,
    start: datetime,
) -> None:
    """Write last_mcap even when honest_tracked_peak caps ATH at 80x.

    Live MEME: t0 $22k → Dex $82M is 3700x, so max stayed $1.07M.
    The card still needs the live print. Late snaps refresh the
    tape after EARLY_SCAN_WINDOW without extra Dex HTTP.
    """
    if mcap <= 0:
        return
    outcome.last_mcap = float(mcap)
    retarget_live_pool(token, market)
    if start is None:
        return
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = now - start
    if age < EARLY_SCAN_WINDOW:
        return
    if token.source == "backfill" or token.is_historical:
        return
    if not _live_last_scan_due(session, token.id, now):
        return
    p_now = token.research.p_good if token.research else 0.0
    session.add(
        Snapshot(
            token=token,
            kind="late",
            mcap_usd=mcap,
            price_usd=float((market or {}).get("price_usd") or 0.0),
            volume_h1=float((market or {}).get("volume_h1") or 0.0),
            liquidity_usd=float((market or {}).get("liquidity_usd") or 0.0),
            p_good=p_now,
            score=p_now * 100.0,
        )
    )


def _live_last_scan_due(session: Session, token_id: int, now: datetime) -> bool:
    last = (
        session.query(func.max(Snapshot.taken_at))
        .filter(Snapshot.token_id == token_id)
        .scalar()
    )
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= LIVE_LAST_SCAN_GAP


def _early_scan_due(session: Session, token_id: int, now: datetime) -> bool:
    last = (
        session.query(func.max(Snapshot.taken_at))
        .filter(Snapshot.token_id == token_id, Snapshot.kind.in_(("early", "t0", "t15m")))
        .scalar()
    )
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= EARLY_SCAN_GAP


def detect_second_leg(points: list[tuple[float, float]]) -> bool:
    """True when the mcap series shows dip-then-recovery: a trough below the
    first observation followed by a recovery at least 20% above the trough
    and above the first print. That shape — survivors of the post-migration
    dump finding new buyers — precedes most multi-day runs."""
    if len(points) < 3:
        return False
    series = [m for _, m in points if m > 0]
    if len(series) < 3:
        return False
    first = series[0]
    trough_idx = min(range(len(series)), key=lambda i: series[i])
    if trough_idx in (0, len(series) - 1):
        return False
    trough = series[trough_idx]
    last = series[-1]
    return trough < first * 0.9 and last >= trough * 1.2 and last > first


def apply_holder_forensics(runner_p: float, forensics: dict) -> tuple[float, list[str]]:
    """Adjust runner potential with deep wallet structure. A coordinated
    funding cluster is close to disqualifying; diamond hands with retained
    snipers strengthen the case."""
    if not forensics or not forensics.get("n"):
        return runner_p, []
    flags: list[str] = []
    cluster = float(forensics.get("funding_cluster") or 0.0)
    early_exit = float(forensics.get("early_exit") or 0.0)
    diamond = float(forensics.get("diamond") or 0.0)
    retention = forensics.get("sniper_retention")
    suspicious = int(forensics.get("suspicious") or 0)
    if cluster >= 0.4:
        runner_p *= 0.45
        flags.append(f"{cluster:.0%} of top holders funded by one wallet (coordinated cluster)")
    elif cluster >= 0.25:
        runner_p -= 0.1
        flags.append("Notable shared-funding cluster among top holders")
    if early_exit >= 0.6:
        runner_p -= 0.15
        flags.append("Most early buyers have fully exited")
    if suspicious >= 5:
        runner_p -= 0.1
        flags.append(f"{suspicious} top wallets flagged suspicious by GMGN")
    if diamond >= 0.5 and (retention is None or retention >= 0.5):
        runner_p += 0.1
        flags.append("Top holders are holding (diamond structure)")
    return max(0.0, min(1.0, runner_p)), flags


def runner_potential_score(
    *,
    mcap_ratio: float,
    liq_ratio: float,
    liq_now: float,
    vol_now: float,
    entry_p: float,
    smart_delta: int | None = None,
    second_leg: bool = False,
) -> float:
    """Trajectory-based 10-50x potential at the 1h mark. What separates
    runners from 3x bounces is the second leg: price holding above entry
    after the post-migration dump, liquidity deepening, volume persisting,
    and smart money adding instead of exiting."""
    s = 0.2 + 0.25 * max(0.0, min(1.0, entry_p))
    if mcap_ratio >= 1.5:
        s += 0.35
    elif mcap_ratio >= 1.1:
        s += 0.2
    elif mcap_ratio < 0.7:
        s -= 0.3
    if liq_ratio >= 1.3 and liq_now >= 10_000:
        s += 0.2
    elif liq_now < 3_000:
        s -= 0.2
    if vol_now >= 20_000:
        s += 0.15
    if smart_delta is not None:
        s += 0.1 if smart_delta > 0 else (-0.1 if smart_delta < 0 else 0.0)
    if second_leg:
        s += 0.15
    return max(0.0, min(1.0, s))


async def _runner_watch(session: Session, token: Token, outcome: Outcome, market: dict) -> None:
    """Score runner potential at the 1h snapshot and persist it for the
    /api/runnerwatch board. One extra GMGN call only for promising tokens."""
    import json as _json

    from ..alerts import notify_high_score

    research = token.research
    # Live SHARK 4.11× / JOHNAPPLE 2.47× / WATER 2.5× were 24h
    # losses. Post-label tracking kept rewriting runnerp for 72h.
    # Confirmed 5× already drop below; labeled flats belong off watch.
    if outcome.label is not None:
        key = f"runnerp:{token.mint}"
        prev_row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if prev_row is not None:
            session.delete(prev_row)
            session.flush()
        return
    # Live Bros / stonkape: approaching already drops last_liq $0.
    # Post-refresh rewrite kept them on watch (Bros as Tier B).
    if float(outcome.last_liq or 0.0) <= 0:
        key = f"runnerp:{token.mint}"
        prev_row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if prev_row is not None:
            session.delete(prev_row)
            session.flush()
        return
    # Live NTDA copycat / PIG start-high rug / LQX pre-pumped sat
    # on watch after approaching already dropped them. Same needles.
    flags = ((research.risk_flags_json if research else "") or "").lower()
    if is_bundle_copycat_run(research) or any(n in flags for n in PREPUMP_FLAG_NEEDLES):
        key = f"runnerp:{token.mint}"
        prev_row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if prev_row is not None:
            session.delete(prev_row)
            session.flush()
        return
    # Live Dancedoge: t0 $8.5M sat watch C after approaching already
    # dropped is_prepumped_entry. Flag needles miss a huge t0.
    if research is not None and is_prepumped_entry(session, token, research, outcome):
        key = f"runnerp:{token.mint}"
        prev_row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if prev_row is not None:
            session.delete(prev_row)
            session.flush()
        return
    # Live Ignoring: 1131 wallets / $3.3k airdrop sat watch B / 3.23×.
    from .features import is_rh_airdrop_book

    if is_rh_airdrop_book(token_chain(token), research_holder_count(token), outcome.last_liq):
        key = f"runnerp:{token.mint}"
        prev_row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if prev_row is not None:
            session.delete(prev_row)
            session.flush()
        return
    # CYBERCAB / DESPONSITO scored runner_p=1.0 on 4 wallets and sat next
    # to ROCK. Approaching already splits them to thin; skip the write so
    # we also do not spend a weight-5 GMGN call on a wick.
    if is_thin_holder_print(research_holder_count(token), token_chain(token)):
        key = f"runnerp:{token.mint}"
        prev_row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if prev_row is not None:
            session.delete(prev_row)
            session.flush()
        return
    mcap_now = sane_mcap(market.get("mcap_usd"), market.get("liquidity_usd"), timedelta(hours=1))
    # Dex miss / softer live tick: approaching already uses the last
    # liquid snap (HELLO 2.38 / BUILDERMAN 2.76). Watch should match.
    snap = last_live_snapshot_mcap(session, token.id)
    if snap > mcap_now:
        mcap_now = snap
    if mcap_now <= 0 or (outcome.t0_mcap or 0) <= 0:
        return
    t0_snap = (
        session.query(Snapshot)
        .filter(Snapshot.token_id == token.id, Snapshot.kind == "t0")
        .order_by(Snapshot.id.asc())
        .first()
    )
    liq_t0 = float(t0_snap.liquidity_usd) if t0_snap and t0_snap.liquidity_usd else 0.0
    liq_now = float(market.get("liquidity_usd") or 0.0)
    vol_now = float(market.get("volume_h1") or 0.0)
    mcap_ratio = mcap_now / outcome.t0_mcap
    # Already a confirmed runner, or still a flat 1x leftover — neither
    # belongs on watch. Drop the row so we do not spend a weight-5 GMGN
    # call on USMS/ROCK or on CHARLES-class 1.4x tape.
    if mcap_ratio >= float(settings.win_multiple) or mcap_ratio < WATCH_MIN_MULTIPLE:
        key = f"runnerp:{token.mint}"
        prev_row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        if prev_row is not None:
            session.delete(prev_row)
            session.flush()
        return
    liq_ratio = (liq_now / liq_t0) if liq_t0 > 0 else 1.0
    from ..desk_lines import legacy_equivalent, lines_for_scorer

    # Runner tiers think in legacy thresholds (0.55 / 0.65): read a
    # first-sight Entry through its own lines so 0.45 on RH is strong.
    entry_p = legacy_equivalent(research.p_good or 0.0, lines_for_scorer(research.scorer, token_chain(token)))

    history = (
        session.query(Snapshot.taken_at, Snapshot.mcap_usd)
        .filter(Snapshot.token_id == token.id, Snapshot.mcap_usd > 0)
        .order_by(Snapshot.taken_at.asc())
        .all()
    )
    second_leg = detect_second_leg([(0.0, float(m or 0.0)) for _, m in history] + [(0.0, mcap_now)])

    base = runner_potential_score(
        mcap_ratio=mcap_ratio, liq_ratio=liq_ratio, liq_now=liq_now, vol_now=vol_now, entry_p=entry_p, second_leg=second_leg
    )
    key = f"runnerp:{token.mint}"
    prev_row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
    prev: dict = {}
    if prev_row:
        try:
            prev = json.loads(prev_row.value or "{}")
        except json.JSONDecodeError:
            prev = {}
    smart_delta = prev.get("smart_delta")
    forensics: dict = prev.get("forensics") or {}
    # GMGN spend is budgeted: deep calls at most once per token per 6h;
    # in-between passes reuse the cached forensics.
    gmgn_fresh = False
    if prev.get("gmgn_at"):
        try:
            gmgn_at = datetime.fromisoformat(prev["gmgn_at"])
            gmgn_fresh = datetime.now(timezone.utc) - gmgn_at < timedelta(hours=6)
        except ValueError:
            gmgn_fresh = False
    gmgn_at_iso = prev.get("gmgn_at")
    # 0.62 gate: deep GMGN spend only for tokens already showing real
    # trajectory — the plan's rate budget cannot fund forensics on maybes.
    if base >= 0.62 and not gmgn_fresh:
        from ..research import gmgn as _gmgn

        if _gmgn.gmgn_deep_available():
            info = await _gmgn.token_research(token.mint, chain=token_chain(token))
            try:
                raw = _json.loads(research.raw_json or "{}")
            except _json.JSONDecodeError:
                raw = {}
            smart_entry = int(((raw.get("gmgn") or {}).get("smart_degen")) or 0)
            smart_now = int((info or {}).get("smart_degen") or 0)
            smart_delta = smart_now - smart_entry
            # Deep wallet forensics: one weight-5 call per budget window.
            forensics = _gmgn.analyze_holders(await _gmgn.top_holders(token.mint, 50, chain=token_chain(token)))
            gmgn_at_iso = datetime.now(timezone.utc).isoformat()
    runner_p = runner_potential_score(
        mcap_ratio=mcap_ratio, liq_ratio=liq_ratio, liq_now=liq_now, vol_now=vol_now, entry_p=entry_p, smart_delta=smart_delta, second_leg=second_leg
    )
    runner_p, forensic_flags = apply_holder_forensics(runner_p, forensics)

    # Our own proven-early wallets showing up is the strongest positive
    # signal we have — it's verified against our book, not a vendor tag.
    alpha_n = alpha_wallet_count(session, (forensics or {}).get("top_addresses") or [])
    if alpha_n:
        runner_p = min(1.0, runner_p + min(0.15, 0.05 * alpha_n))
        forensic_flags.append(f"{alpha_n} proven alpha wallet(s) from prior runners in the book")

    from ..alerts import is_hard_stopped

    try:
        risk_flags = _json.loads(research.risk_flags_json or "[]")
    except _json.JSONDecodeError:
        risk_flags = []
    tier = conviction_tier(
        entry_p=entry_p,
        runner_p=runner_p,
        second_leg=second_leg,
        liq_now=liq_now,
        hard_stopped=is_hard_stopped(risk_flags + forensic_flags),
        forensics=forensics,
        mcap_ratio=mcap_ratio,
        prepumped=is_prepumped_entry(session, token, research, outcome),
    )

    payload = {
        "runner_p": round(runner_p, 3),
        "mcap_ratio": round(mcap_ratio, 2),
        "liq_now": round(liq_now),
        "vol_1h": round(vol_now),
        "smart_delta": smart_delta,
        "second_leg": second_leg,
        "entry_p": round(entry_p, 3),
        "symbol": token.symbol,
        "forensics": forensics or None,
        "forensic_flags": forensic_flags or None,
        "tier": tier,
        "gmgn_at": gmgn_at_iso,
        "chain": token_chain(token),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    row = prev_row
    if row is None:
        row = ScanState(key=key)
        session.add(row)
    row.value = json.dumps(payload)
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    # prune entries older than 3 days so the table stays small
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    for stale in session.query(ScanState).filter(ScanState.key.like("runnerp:%"), ScanState.updated_at < stale_cutoff).all():
        session.delete(stale)
    if tier == "A":
        # The only ping that should reach a human: every independent system
        # agrees. Expect a handful per day, not a feed.
        log.info(
            "TIER A: %s entry_p=%.2f runner_p=%.2f mcap_ratio=%.2f liq=%.0f",
            token.symbol, entry_p, runner_p, mcap_ratio, liq_now,
        )
        from .alert_tiers import claim_ping

        if claim_ping(session, "runner", token.mint):
            await notify_high_score(
                symbol=token.symbol,
                name=f"{token.name} — TIER A conviction ({mcap_ratio:.1f}x from entry, all gates pass)",
                mint=token.mint,
                p_good=runner_p,
                mcap_usd=mcap_now,
                flags=risk_flags,
                reasons=[
                    f"entry {entry_p:.0%} + trajectory {runner_p:.0%} agree",
                    f"mcap {mcap_ratio:.1f}x entry, liq ${liq_now:,.0f}",
                    "wallet structure clean",
                ],
                chain=token_chain(token),
            )
    elif runner_p >= 0.75:
        log.info("runner watch: %s runner_p=%.2f mcap_ratio=%.2f tier=%s", token.symbol, runner_p, mcap_ratio, tier)


META_STOPWORDS = {
    "coin", "token", "the", "and", "for", "with", "this", "that", "official",
    "sol", "solana", "pump", "fun", "meme", "crypto", "not", "its", "was",
}


def extract_meta_words(name: str, symbol: str) -> set[str]:
    import re as _re

    blob = f"{name or ''} {symbol or ''}".lower()
    words = set(_re.findall(r"[a-z]{3,12}", blob))
    return words - META_STOPWORDS


_meta_cache: dict[str, tuple[float, set[str]]] = {}


def hot_metas(session: Session, chain: str = "sol") -> set[str]:
    """Words appearing in the names of 2+ distinct confirmed winners in the
    last 48h — the narrative waves the market is paying for right now."""
    import time as _time

    chain = normalize_chain(chain)
    cached = _meta_cache.get(chain)
    if cached and _time.monotonic() - cached[0] < 600:
        return cached[1]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    rows = (
        session.query(Token.name, Token.symbol)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(
            Outcome.label == 1,
            Outcome.multiple >= settings.win_multiple,
            Token.source != "backfill",
            Token.first_seen_at >= cutoff,
            Token.chain == chain,
        )
        .all()
    )
    counts: dict[str, int] = {}
    for name, symbol in rows:
        for word in extract_meta_words(name or "", symbol or ""):
            counts[word] = counts.get(word, 0) + 1
    hot = {w for w, c in counts.items() if c >= 2}
    _meta_cache[chain] = (_time.monotonic(), hot)
    return hot


ALPHA_MIN_RUNS = 2


def alpha_wallet_count(session: Session, addresses: list[str]) -> int:
    """How many of these wallets sat early in 2+ of OUR confirmed runners."""
    if not addresses:
        return 0
    count = 0
    for addr in addresses[:15]:
        row = session.query(ScanState).filter(ScanState.key == f"alpha:{addr}").one_or_none()
        if not row:
            continue
        try:
            runs = int(json.loads(row.value or "{}").get("runs") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            runs = 0
        if runs >= ALPHA_MIN_RUNS:
            count += 1
    return count


def _credit_alpha_wallets(session: Session, token: Token) -> None:
    """A confirmed runner credits its early top holders: wallets that keep
    showing up before runs are the desk's own proven smart money."""
    row = session.query(ScanState).filter(ScanState.key == f"runnerp:{token.mint}").one_or_none()
    if not row:
        return
    try:
        forensics = (json.loads(row.value or "{}").get("forensics")) or {}
    except json.JSONDecodeError:
        return
    for addr in (forensics.get("top_addresses") or [])[:15]:
        key = f"alpha:{addr}"
        arow = session.query(ScanState).filter(ScanState.key == key).one_or_none()
        stats = {}
        if arow:
            try:
                stats = json.loads(arow.value or "{}")
            except json.JSONDecodeError:
                stats = {}
        mints = set(stats.get("mints") or [])
        if token.mint in mints:
            continue
        mints.add(token.mint)
        stats["mints"] = sorted(mints)[-20:]
        stats["runs"] = int(stats.get("runs") or 0) + 1
        if arow is None:
            arow = ScanState(key=key)
            session.add(arow)
        arow.value = json.dumps(stats)
        arow.updated_at = datetime.now(timezone.utc)
    session.flush()


def conviction_tier(
    *,
    entry_p: float,
    runner_p: float | None,
    second_leg: bool,
    liq_now: float,
    hard_stopped: bool,
    forensics: dict | None,
    mcap_ratio: float = 1.0,
    prepumped: bool = False,
) -> str:
    """Stacked-gate conviction: independent systems must agree before a token
    claims attention. A = alert-worthy (all gates pass), B = watchlist,
    C = background noise.

    Live HeeHaw 98x / ARROW-class Dex artifacts were paging as Tier A
    because mcap_ratio is uncapped here while runners already stop at 80x.
    A start-high fill (USWS) is the same fake 70x from a $69k floor.
    """
    if hard_stopped or prepumped:
        return "C"
    if float(mcap_ratio or 0.0) > MAX_HONEST_MULTIPLE:
        return "C"
    f = forensics or {}
    cluster = float(f.get("funding_cluster") or 0.0)
    early_exit = float(f.get("early_exit") or 0.0)
    forensics_dirty = cluster >= 0.25 or early_exit >= 0.6
    if (
        entry_p >= 0.65
        and runner_p is not None
        and runner_p >= 0.75
        and liq_now >= 20_000
        and not forensics_dirty
    ):
        return "A"
    if (
        entry_p >= 0.55
        and ((runner_p is not None and runner_p >= 0.6) or second_leg)
        and liq_now >= CONVICTION_MIN_LIQ
        and not forensics_dirty
    ):
        return "B"
    return "C"


def mention_velocity_boost(p: float, baseline: int, now_count: int) -> float:
    """Small bump when ticker mentions are clearly accelerating post-migration.
    Requires real volume (>=10/hr) and at least a doubling of the entry
    baseline; capped so velocity never dominates fundamentals."""
    if now_count >= 10 and now_count >= 2 * max(1, int(baseline or 0)):
        return min(0.95, p + 0.05)
    return p


_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_funder_wallet(address: str | None) -> bool:
    """True when GMGN fund_from is a chain address, not a CEX hop label.

    Live Stamp / TIPPED / ZIP stored fund_from='Binance'; JEANPHISOL
    'Gate.io'; CHILLPHIL 'Kucoin Wallet'. Those shared keys inherited
    every 5×-miss on that CEX hop and paper-vetoed the next honest book.
    FEATURE_NAMES stays 66. Does not late-fill Stamp.
    """
    raw = str(address or "").strip()
    if not raw:
        return False
    if raw.startswith(("0x", "0X")) and len(raw) == 42:
        return all(ch in "0123456789abcdefABCDEF" for ch in raw[2:])
    return 32 <= len(raw) <= 44 and all(ch in _BASE58 for ch in raw)


def funder_key(address: str) -> str:
    return f"funder:{address}"


def funder_stats(session: Session, address: str) -> dict:
    if not is_funder_wallet(address):
        return {}
    row = session.query(ScanState).filter(ScanState.key == funder_key(address)).one_or_none()
    if not row:
        return {}
    try:
        return json.loads(row.value or "{}")
    except json.JSONDecodeError:
        return {}


def _update_funder_stats(session: Session, token: Token, label: int) -> None:
    """Learn which wallets bankroll winners vs serial rugs. The creator's
    funding wallet comes from GMGN dev.fund_from captured at research time.

    CEX labels are not keys — Stamp-class Binance hops must not share a
    factory tally. A real wallet still increments.
    """
    research = token.research
    if research is None:
        return
    try:
        raw = json.loads(research.raw_json or "{}")
    except json.JSONDecodeError:
        return
    gmgn = raw.get("gmgn") or {}
    funder = str(gmgn.get("fund_from") or "")
    addr = str(gmgn.get("fund_from_address") or "")
    if is_funder_wallet(addr):
        funder = addr
    if not is_funder_wallet(funder):
        return
    key = funder_key(funder)
    row = session.query(ScanState).filter(ScanState.key == key).one_or_none()
    stats = {}
    if row:
        try:
            stats = json.loads(row.value or "{}")
        except json.JSONDecodeError:
            stats = {}
    mints = set(stats.get("mints") or [])
    if token.mint in mints:
        return  # already counted
    mints.add(token.mint)
    stats["mints"] = sorted(mints)[-50:]
    stats["wins"] = int(stats.get("wins") or 0) + (1 if label == 1 else 0)
    stats["rugs"] = int(stats.get("rugs") or 0) + (1 if label == 0 else 0)
    if row is None:
        row = ScanState(key=key)
        session.add(row)
    row.value = json.dumps(stats)
    row.updated_at = datetime.now(timezone.utc)
    session.flush()  # autoflush is off; keep the row visible to later lookups


def _commit_live_score(
    research: Research,
    outcome: Outcome,
    scored: dict,
    p_new: float,
    *,
    veto: bool = False,
) -> bool:
    """Write the current-information score onto an unlabeled row.

    features_json stays the at-entry vector so training is not leaked.
    Sol paper still reads t0 snapshot p. RH paper reads research.p_good.
    Lifts only while multiple < 2 (IBUYMEMES / CASHBIRD / FOMODOG).
    Downward writes are open under 2x; at 2x+ only thin/empty/airdrop
    vetoes may drop a climber. Live GUH 2.53x / 105 / $41k wrote
    0.68→0.17 when the 480 floor lapsed — a real book, not a veto.
    Labeled rows stay frozen for evaluation.
    Returns True when p_good actually moved up (the only case that
    should fire a second-look upgrade alert).
    """
    if outcome.label is not None:
        return False
    if (research.scorer or "legacy") == "first_sight":
        # The legacy re-score is on another scale (0.85 there is not 0.85
        # here). Keep the calibrated first-sight Entry; refresh only the
        # tape-derived reasons / flags. Live is conviction_p, not this.
        if scored.get("reasons") is not None:
            research.reasons_json = json.dumps(scored["reasons"])
        if scored.get("risk_flags") is not None:
            research.risk_flags_json = json.dumps(scored["risk_flags"])
        return False
    old = float(research.p_good or 0.0)
    multiple = float(outcome.multiple or 0.0)
    t0 = float(outcome.t0_mcap or 0.0)
    mx = float(outcome.max_mcap or 0.0)
    if multiple <= 0 and t0 > 0 and mx > 0:
        multiple = mx / t0
    if multiple >= 2.0:
        if p_new > old + 1e-4:
            return False
        if p_new < old - 1e-4 and not veto:
            return False
    if abs(p_new - old) < 1e-4:
        return False
    research.p_good = round(float(p_new), 4)
    research.heuristic_p = scored.get("heuristic_p")
    research.model_p = scored.get("model_p")
    if scored.get("reasons") is not None:
        research.reasons_json = json.dumps(scored["reasons"])
    if scored.get("risk_flags") is not None:
        research.risk_flags_json = json.dumps(scored["risk_flags"])
    return float(research.p_good) > old + 1e-4


async def _second_look(session: Session, token: Token, outcome: Outcome, market: dict) -> float:
    """Re-score with every stored signal we have now.

    Launch-moment market features are noisy. Refresh pressure, liquidity,
    volume, holders, empty/thin-book caps, per-holder mcap, and the Dex
    tape we already fetched — then write the live %score for unlabeled
    names. features_json is not overwritten.
    """
    import json as _json

    from ..alerts import notify_high_score
    from ..config import settings
    from .features import (
        _clip01,
        _log_norm,
        _price_change_n,
        _vol_persist,
        is_organic_book,
    )
    from .model import predict

    research = token.research
    try:
        features = _json.loads(research.features_json or "{}")
    except _json.JSONDecodeError:
        return research.p_good or 0.0
    if not features:
        return research.p_good or 0.0
    chain = token_chain(token)
    buys = float(market.get("buys_m5") or 0) + float(market.get("buys_h1") or 0)
    sells = float(market.get("sells_m5") or 0) + float(market.get("sells_h1") or 0)
    if buys + sells > 0:
        features["buy_pressure"] = _clip01(buys / (buys + sells))
        features["usd_buy_pressure"] = features["buy_pressure"]
    # A Dex miss is not a dead book. Overwriting liq/vol with 0 wiped
    # CHAD/SLOP's stored tape (last_liq $14k / $4k) and kept them off
    # the organic line if they hit 5x.
    liq = float(market.get("liquidity_usd") or 0)
    if liq <= 0 and float(outcome.last_liq or 0) >= DEAD_POOL_LIQ:
        liq = float(outcome.last_liq)
    if liq > 0:
        features["liquidity_n"] = _log_norm(liq, 80_000)
    vol = float(market.get("volume_h1") or market.get("volume_m5") or 0)
    if vol > 0:
        features["volume_n"] = _log_norm(vol, 50_000)
    vol_h24 = float(market.get("volume_h24") or 0)
    if vol > 0 and vol_h24 > 0:
        features["vol_persist_n"] = _vol_persist(vol, vol_h24)
    if market.get("price_change_h1") is not None:
        features["price_change_h1_n"] = _price_change_n(float(market.get("price_change_h1") or 0))
    holders = research_holder_count(token)
    last_liq = float(outcome.last_liq or 0)
    pool_open = False
    if chain == "robinhood":
        pool_open = _mark_rh_lp_open(research, features)
        _overlay_stored_gmgn(research, features)
    if holders > 0:
        features["holder_n"] = _log_norm(holders, 2_000)
        from .features import RH_FAT_BOOK_LIQ

        features["rh_thin_book"] = (
            1.0
            if chain == "robinhood"
            and holders < 20
            and last_liq < RH_FAT_BOOK_LIQ
            and not pool_open
            else 0.0
        )
    tweets = int(getattr(research, "twitter_tweets", 0) or 0)
    if tweets > 0:
        features["twitter_tweets_n"] = _log_norm(float(tweets), 500)
    mcap_now = float(market.get("mcap_usd") or 0)
    if mcap_now > 0 and holders > 0:
        features["mcap_per_holder_n"] = _clip01((mcap_now / holders) / 40_000.0)
    start = token.migrated_at or token.first_seen_at
    age_min = 0.0
    if start is not None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - start).total_seconds() / 60.0
    last_liq = float(outcome.last_liq or 0)
    if chain == "robinhood":
        from .features import is_rh_airdrop_book, is_rh_bot_dump_book, is_rh_empty_book

        features["rh_empty_book"] = (
            1.0
            if is_rh_empty_book(
                chain,
                liq,
                last_liq=last_liq,
                age_min=age_min,
                holders=holders,
            )
            else 0.0
        )
        features["rh_airdrop_book"] = (
            1.0 if is_rh_airdrop_book(chain, holders, last_liq, liq=liq) else 0.0
        )
        # Live KFC: features_json froze entry_premium=0 because Dex
        # printed the $40k book while outcome.t0 was leftover $146k.
        if float(outcome.t0_mcap or 0.0) >= MAX_HONEST_ENTRY_MULTIPLE_RH * graduation_mcap("robinhood"):
            features["entry_premium"] = 1.0
    features["organic_book"] = 1.0 if is_organic_book(features) else 0.0
    if chain == "robinhood":
        from .features import is_rh_bot_dump_book

        features["rh_bot_dump"] = 1.0 if is_rh_bot_dump_book(chain, features) else 0.0
    multiple = _outcome_multiple(outcome)
    features["age_min"] = age_min
    features["live_multiple"] = multiple
    scored = predict(session, features, chain=chain)
    p_new = scored["p_good"]
    # Mention velocity is a paid counts call. Only spend it when the
    # re-score is in alert range *and* the account is a real mid-size
    # project X — not NASA / copycat / 5k-verified hijacks.
    if research.twitter_handle and token.symbol and p_new >= 0.75:
        from ..research import twitter as _twitter

        try:
            look_flags = _json.loads(research.risk_flags_json or "[]")
        except _json.JSONDecodeError:
            look_flags = []
        followers_n = int(research.twitter_followers or 0)
        if (
            (followers_n >= _twitter.PAID_MENTION_FOLLOWERS or research.twitter_verified)
            and _twitter.should_spend_x_credits(
                research.twitter_handle,
                followers=followers_n,
                age_days=float(research.twitter_age_days or 0),
                verified=bool(research.twitter_verified),
                flags=look_flags if isinstance(look_flags, list) else [],
            )
        ):
            mentions_now = await _twitter.mention_count(f"${token.symbol}")
            p_new = mention_velocity_boost(p_new, research.x_mentions_1h, mentions_now)
    threshold = settings.alert_min_p
    p_entry = research.p_good or 0.0
    veto = bool(
        features.get("rh_thin_book")
        or features.get("rh_empty_book")
        or features.get("rh_airdrop_book")
        or features.get("rh_bot_dump")
    )
    lifted = _commit_live_score(research, outcome, scored, p_new, veto=veto)
    # Live IBUYMEMES: 0.57 -> 0.71 logged every refresh because the
    # 2x lift-block left p_good at entry while this still compared
    # the computed live p to the frozen entry. Alert only when we
    # actually wrote a higher score.
    if lifted and p_new >= threshold > p_entry:
        try:
            flags = _json.loads(research.risk_flags_json or "[]")
        except _json.JSONDecodeError:
            flags = []
        try:
            reasons = _json.loads(research.reasons_json or "[]")
        except _json.JSONDecodeError:
            reasons = []
        await notify_high_score(
            symbol=token.symbol,
            name=f"{token.name} — second look at 15m (was {p_entry:.0%})",
            mint=token.mint,
            p_good=p_new,
            mcap_usd=float(market.get("mcap_usd") or 0.0),
            flags=flags,
            reasons=reasons,
            chain=chain,
        )
        log.info("second-look upgrade %s %.2f -> %.2f", token.symbol, p_entry, p_new)
    return p_new


def _early_label(outcome: Outcome, age: timedelta, *, holders: int = 0, chain: str = "sol") -> None:
    """Label live tokens before the 24h horizon when the outcome is already
    decided, so the model gets training data within hours instead of a day.

    Wins are monotone: multiple = max_mcap / t0 only grows, so a win-multiple
    hit with healthy liquidity is final. Rug labels wait for the 6h mark —
    except tokens sitting under half their entry with a drained pool, which
    are dead from the first refresh. A thin-holder 5x stays pending: four
    wallets printing a wick is not a runner we want the model to learn.
    """
    liq_dead = outcome.last_liq > 0 and outcome.last_liq < DEAD_POOL_LIQ
    if outcome.multiple >= settings.win_multiple and not liq_dead:
        if is_thin_holder_print(holders, chain):
            return
        outcome.label = 1
        outcome.labeled_at = datetime.now(timezone.utc)
    elif liq_dead and (age >= HORIZONS["t6h"] or (age >= HORIZONS["t15m"] and outcome.multiple < 0.5)):
        outcome.label = 0
        outcome.labeled_at = datetime.now(timezone.utc)


def _label(outcome: Outcome, coin: dict, *, historical: bool = False, ath_ceiling: float | None = None, holders: int = 0, chain: str = "sol") -> None:
    ath = sane_mcap(coin.get("ath_mcap"), ceiling=ath_ceiling)
    if ath:
        outcome.max_mcap = max(outcome.max_mcap or 0.0, ath)
    if historical or outcome.t0_mcap <= 0:
        # Backfilled tokens were ingested long after migration, so the mcap at
        # ingest is not an entry price. Post-collapse ingests have a tiny mcap
        # that turns ath/t0 into a fake 10x. Use the graduation baseline as the
        # buy-at-migration proxy instead — chain-aware so RH is not $69k.
        outcome.t0_mcap = graduation_mcap(chain)
    outcome.multiple = outcome.max_mcap / outcome.t0_mcap if outcome.t0_mcap else 0.0
    # A drained pool at judgment time overrides any paper multiple: nobody
    # exits a pool with no liquidity, so "started high and rugged" is a loss
    # no matter how high the wick looked.
    rugged = outcome.last_liq > 0 and outcome.last_liq < DEAD_POOL_LIQ
    winner = outcome.multiple >= settings.win_multiple and not rugged and not is_thin_holder_print(holders, chain)
    outcome.label = 1 if winner else 0
    outcome.labeled_at = datetime.now(timezone.utc)
    # Leftover FDV is not a live book and is not a training example.
    if is_rh_leftover_fdv(outcome, holders, chain=chain):
        outcome.last_liq = 0.0
        outcome.used_for_train = True
