from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, or_
from sqlalchemy.orm import selectinload

from .chains import graduation_mcap, gmgn_token_url, normalize_chain, profile, token_chain
from .config import settings
from .db import init_db, session_scope
from .desk_lines import desk_lines, lines_for_scorer
from .httputil import close_client
from .ingest.webhooks import handle_webhook
from .models import HuntCard, Outcome, Research, Token
from .scoring.model import get_or_create_model, model_card
from .serialize import (
    DOING_WELL_HELD_LIQ,
    DOING_WELL_HELD_MULTIPLE,
    DOING_WELL_LABELED_LIQ,
    DOING_WELL_LABELED_LIVE_MULTIPLE,
    apply_ath_dump_honesty,
    apply_stall_honesty,
    apply_young_rh_desk_floor,
    attach_snap_tape,
    desk_entry_cap,
    frozen_entry_p,
    live_doing_well_multiple,
    still_doing_well,
    token_card,
    token_detail,
)
from .worker import restore_watch_boards, start_background, stop_background

log = logging.getLogger("launchfinder")
STATIC = Path(__file__).resolve().parent / "static"
# Live 20:40 /rh 12h limit=80: SHELLY (12:34 / $22k / 46w) fell out of
# the newest-2400 window after another PONS burst. limit=200 (3200)
# still ranked her #22. Live 16:00: CASHBIRD 87w / $23k fell off the
# 200 page at 3200; 6400 still missed after the next burst. Floor 12800.
RH_HUNT_FETCH_FLOOR = 12800


def rh_hunt_fetch(limit: int) -> int:
    return max(int(limit) * 16, RH_HUNT_FETCH_FLOOR)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Per-request httpx lines drown out real events (and leak keyed RPC URLs).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    from .worker import configure_worker_logging

    configure_worker_logging()
    init_db()
    # Restore graduating persist before the first request or poll so
    # /api/watch is not empty and RH first_seen clocks survive boot.
    restore_watch_boards()
    # Hunt rebuild lives in worker.loop() after boot repairs. API
    # replicas only read hunt_cards so they must not UPDATE the table.
    if settings.runs_worker():
        start_background()
    from .research.holder_rewards import start_holder_rewards

    start_holder_rewards()
    yield
    if settings.runs_worker():
        await stop_background()
    await close_client()


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response


app = FastAPI(title="New Launch Finder", version="0.1.0", lifespan=lifespan)
app.mount("/static", CachedStaticFiles(directory=STATIC), name="static")


@app.get("/")
@app.get("/ui")
@app.get("/desk")
async def index():
    """Oversight desk is the only UI. Classic boards are retired."""
    return FileResponse(STATIC / "desk.html")


# Back-compat aliases for older tests / bookmarks.
test_desk = index


@app.get("/rh")
@app.get("/robinhood")
@app.get("/ui/rh")
@app.get("/ui/robinhood")
@app.get("/desk/rh")
async def robinhood_index():
    """Robinhood oversight desk. Same surface as /; chain is RH."""
    return FileResponse(STATIC / "desk-rh.html")


test_desk_rh = robinhood_index


@app.get("/health")
async def health():
    from .alerts import alerts_configured
    from .image_rev import IMAGE_REV, deploy_stamp, running_git_sha

    def _ledger_health():
        from .ledger import heartbeats, ledger_counts
        from .risk import risk_status

        with session_scope() as session:
            return {
                "loops": heartbeats(session),
                "ledger": ledger_counts(session),
                "risk": risk_status(session),
            }

    extra: dict = {"loops": {}, "ledger": {}, "risk": {}}
    try:
        extra = await asyncio.wait_for(asyncio.to_thread(_ledger_health), timeout=3.0)
    except Exception:
        log.warning("health ledger probe skipped", exc_info=True)

    return {
        "ok": True,
        "loops": extra["loops"],
        "ledger": extra["ledger"],
        "risk": extra.get("risk") or {"armed": False, "kill_switch": False, "paper_only": True},
        "tickets": True,
        "helius": settings.has_helius,
        "x_api": settings.has_x,
        "gmgn": settings.has_gmgn,
        "fomo": settings.has_fomo,
        "fomo_alerts": settings.has_fomo,
        "fomo_rht": True,
        "fomo_trending": True,
        "early_wallets": True,
        "wallet_score": True,
        "fomo_poll_seconds": settings.fomo_poll_seconds,
        "ws": bool(settings.ws_url),
        "bitquery": settings.has_bitquery,
        "poll_seconds": settings.poll_seconds,
        "hunt_tape": True,
        "hunt_last_seconds": settings.hunt_last_seconds,
        "robinhood": settings.robinhood_enabled,
        "pons": True,
        "raydium": True,
        "prewarm": True,
        "bloom": True,
        "hunt": True,
        "preview": True,
        "live_social": True,
        "paper": True,
        "alerts": alerts_configured(),
        "github": bool(settings.github_token),
        "role": settings.role,
        "image_rev": IMAGE_REV,
        "git_sha": running_git_sha() or None,
        "deploy_stamp": deploy_stamp() or None,
    }


@app.get("/api/status")
async def status(chain: str = Query("sol")):
    return await asyncio.to_thread(_status_sync, chain)


def _status_sync(chain: str):
    chain = normalize_chain(chain)
    with session_scope() as session:
        q = session.query(Token).filter(Token.chain == chain)
        total = q.count()
        live = q.filter(Token.is_historical.is_(False)).count()
        labeled = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.chain == chain, Outcome.label.is_not(None))
            .count()
        )
        wins = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.chain == chain, Outcome.label == 1)
            .count()
        )
        card = model_card(session, chain=chain)
    return {
        "tokens": total,
        "live": live,
        "labeled": labeled,
        "wins": wins,
        "chain": chain,
        "chain_label": profile(chain).label,
        "model": card,
        "integrations": {
            "helius": settings.has_helius,
            "x_api": settings.has_x,
            "gmgn": settings.has_gmgn,
            "fomo": settings.has_fomo,
            "fomo_alerts": settings.has_fomo,
            "fomo_rht": True,
            "fomo_trending": True,
            "early_wallets": True,
            "wallet_score": True,
            "github": bool(settings.github_token),
            "solana_ws": bool(settings.ws_url),
            "bitquery": settings.has_bitquery,
            "robinhood": settings.robinhood_enabled,
            "pons": True,
            "raydium": True,
            "prewarm": True,
            "live_social": True,
        },
    }


@app.get("/api/tokens")
async def list_tokens(
    limit: int = Query(60, ge=1, le=200),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    historical: bool | None = None,
    labeled: bool | None = None,
    fresh_hours: float = Query(18.0, ge=0.0, le=168.0),
    chain: str = Query("sol"),
):
    # RH hunt builds 12,800 cards. Doing that on the event loop 499s /rh.
    return await asyncio.to_thread(
        _list_tokens_sync, limit, min_score, historical, labeled, fresh_hours, chain
    )


def _list_tokens_sync(
    limit: int,
    min_score: float,
    historical: bool | None,
    labeled: bool | None,
    fresh_hours: float,
    chain: str,
):
    chain = normalize_chain(chain)
    with session_scope() as session:
        # token_card reads research + outcome. Without a preload this is
        # 2N lazy queries on the event loop (limit*6 = 480 cards) and
        # /health sat 9–18s after the Sol desk sort widened the fetch.
        q = (
            session.query(Token)
            .options(selectinload(Token.research), selectinload(Token.outcome))
            .filter(Token.chain == chain)
        )
        if chain == "robinhood":
            # Catch-up stamps trench open as migrated_at (often >12h). Sort
            # by when we first saw the name so new ingest is not buried.
            q = q.order_by(Token.first_seen_at.desc(), Token.migrated_at.desc().nulls_last())
        else:
            q = q.order_by(Token.migrated_at.desc().nulls_last(), Token.first_seen_at.desc())
        if historical is True:
            q = q.filter(Token.is_historical.is_(True))
        elif historical is False:
            q = q.filter(Token.is_historical.is_(False))
        # Trench / migration floods mint leftover names in one poll.
        # Fetch a wider window so a real book from 20m ago is not cut
        # off by $0 clones or a 2-wallet $20k PONS burst, then prefer
        # live books on the card sort. Live 11:00 /rh: 53 of 80 were
        # 2–6 wallet prints because limit*6 only kept the newest 480.
        # Live 18:40 /rh 12h limit=80: HO / SHELLY (13:22 / 12:34) were
        # cut from the newest-960 window after a 1-wallet PONS burst.
        # *16 (1280) still missed them; limit=200 (3200) ranked HO #13.
        # Live 20:40: 2400 kept HO and cut SHELLY. Floor 3200.
        fetch = rh_hunt_fetch(limit) if chain == "robinhood" else limit * 12
        rows = q.limit(fetch).all()
        rh_n_train = (
            get_or_create_model(session, chain="robinhood").n_train
            if chain == "robinhood"
            else None
        )
        cards = [token_card(t) for t in rows]
        if rh_n_train is not None:
            cards = [apply_young_rh_desk_floor(c, rh_n_train) for c in cards]
            cards = [apply_stall_honesty(c) for c in cards]
        cards = [apply_ath_dump_honesty(c) for c in cards]
        cards = [c for c in cards if (c.get("p_good") or 0.0) * 100.0 >= min_score]
        if labeled is True:
            cards = [c for c in cards if c["label"] is not None]
        elif labeled is False:
            cards = [c for c in cards if c["label"] is None]

        def _parse_ts(raw) -> datetime | None:
            if not raw:
                return None
            t = datetime.fromisoformat(raw)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return t

        def _card_ts(card: dict) -> datetime | None:
            # Sol live 10:07: `: - )` created 2026-03-30 sat #1 after a
            # same-day migrate stamp. Live 03:40: BCD created 20:45 /
            # $295k sat #12 after a 02:10 migrate stamp. New-launch
            # age is created_at. RH catch-up still uses first_seen
            # (POW / Philosophoor).
            if chain == "robinhood":
                raws = [
                    card.get("created_at"),
                    card.get("migrated_at"),
                    card.get("first_seen_at"),
                ]
                parsed = [t for raw in raws if (t := _parse_ts(raw))]
                return max(parsed) if parsed else None
            return (
                _parse_ts(card.get("created_at"))
                or _parse_ts(card.get("first_seen_at"))
                or _parse_ts(card.get("migrated_at"))
            )

        def _fresh_ts(card: dict) -> datetime | None:
            return _card_ts(card)

        if historical is not True and fresh_hours > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=fresh_hours)
            cards = [c for c in cards if (t := _fresh_ts(c)) and t >= cutoff]
        if historical is not True and chain == "sol":
            from .scoring.outcomes import DEAD_POOL_LIQ, SKINNY_HUNT_LIQ

            # Live 11:00: Biu/Syoma/peppons sat at last_liq $0. Live
            # 11:20: BUD/$CAT $1–$100 leftover LP filled slots 52–68
            # after the $0 cut. Dust is not a new-launch card.
            # Dex-miss chairs stay RH-only.
            cards = [
                c for c in cards if float(c.get("last_liq") or 0) >= DEAD_POOL_LIQ
            ]
            # Live 12:00: "2" 71.5× / $3.2k sat in the 43. Live 09:40:
            # Jayden 39.7× / $1.9k / $2.7M sat #92. Runners already
            # hide >80x; a 30x+ skinny leftover is the same Dex wick.
            # Fat 5–50x books stay.
            cards = [
                c
                for c in cards
                if not (
                    float(c.get("multiple") or 0) > 30.0
                    and float(c.get("last_liq") or 0) < SKINNY_HUNT_LIQ
                )
            ]
        if historical is not True:
            # RH 20:40: TESTED/Fortnite $0 leftovers filled the first
            # page; ZOINBASE $34k sat under them. Sol 23:35: BABYFON /
            # CYBERLEEK $0 and Solana $265 sat above NASA $57k / MBS
            # $50k. Live 00:05 /rh: GLIZZY/NINA/BIMBO 2–6 wallets sat
            # above OWL 1214 / EYE 64. Real books first, then crowded
            # books (RH 20 / Sol 8), then newest. Thin and $0 rows
            # stay on the desk, just below.
            from .scoring.features import is_rh_airdrop_book, is_sol_airdrop_tape
            from .scoring.outcomes import (
                DEAD_POOL_LIQ,
                MAX_HONEST_MULTIPLE,
                RH_HUNT_THIN_HOLDERS,
                SKINNY_HUNT_LIQ,
                is_sol_old_leftover_major,
                is_thin_approaching_print,
                is_thin_holder_print,
            )

            def _desk_key(card: dict) -> tuple:
                # Live 𝕏LIFE: 120x / 9 wallets sat on the Sol first page
                # after a Dex wick early-labeled. Runners already hide >80x.
                # Live /rh 05:00: X 1111 / $3.4k airdrop-tape sat with AI
                # 1207 / $33k because both cleared crowded + last_liq ≥ $800.
                artifact = 1 if float(card.get("multiple") or 0) > MAX_HONEST_MULTIPLE else 0
                real = 0 if float(card.get("last_liq") or 0) >= DEAD_POOL_LIQ else 1
                # Live ROBIN 7.86× / label=1 sat with YOLO 2.8× on /rh.
                # Confirmed 5× belong on runners, not the hunt desk.
                already_won = (
                    1
                    if card.get("label") == 1 and float(card.get("multiple") or 0) >= 5.0
                    else 0
                )
                airdrop = 1 if (
                    is_rh_airdrop_book(
                        chain, card.get("holder_count"), card.get("last_liq")
                    )
                    or is_sol_airdrop_tape(
                        chain, card.get("holder_count"), card.get("last_liq")
                    )
                ) else 0
                # Live 07:00 Sol hunt: Trader 8 wallets / $95k sat with
                # PIMPAMPUMP2 77 / $40k. Runner floor stays 8; hunt uses
                # the approaching 15-wallet line so tight books sort down.
                # RH still uses the 20 / 0-wallet holder print.
                # Live 18:20: VIRTUE 76w / $31k collapsed to 1w and
                # sorted with ROBINROLL 4w stubs. A fat 1-wallet map is
                # a Dex-miss — not thin, but not a crowded book either.
                # Live 18:28: treating 1w as unknown put BEAVER/MEME
                # factory stubs #1–#7 above LOVEP 166w. Rank empty-map
                # fat books below real crowded books. HIMSTER 0w stays thin.
                holders = card.get("holder_count")
                empty_map = 0
                if (
                    chain == "robinhood"
                    and holders is not None
                    and int(holders) == 1
                    and float(card.get("last_liq") or 0) >= SKINNY_HUNT_LIQ
                ):
                    empty_map = 1
                    holders = None
                thin = 1 if (
                    is_thin_approaching_print(holders, chain)
                    if chain == "sol"
                    else (
                        is_thin_holder_print(holders, chain)
                        or (
                            holders is not None
                            and int(holders) <= RH_HUNT_THIN_HOLDERS
                        )
                    )
                ) else 0
                # Live 08:00 Sol hunt: cryptobros 77 / $2.2k sat above
                # NASA $120k / OnlyUpBOT $222k. Live 14:47 /rh: Memesock
                # $5k / 117w sat #1 above HO $35k. Skinny real books stay
                # on the desk, just below fat ones.
                # Live 00:20: GOOGL $60k / 12w and Claude $60k / 13w sat
                # #27–#28 under POJ / POLE $2k / 35–53w because thin
                # ranked before skinny. Fat books — even approaching-thin
                # — outrank crowded $2k prints. Empty-map still ranks
                # below crowded and above thin (VIRTUE-class).
                skinny = (
                    1
                    if DEAD_POOL_LIQ <= float(card.get("last_liq") or 0) < SKINNY_HUNT_LIQ
                    else 0
                )
                multiple = float(card.get("multiple") or 0)
                # Live 11:40: ROX 4.49× / 153w / $11.1k sat #120 under
                # factory stubs after liq dipped $855 under $12k. A
                # crowded 3×+ climb is not a $2k skinny dump. 8Bit
                # 2.18× / $2k and PUMP 3.68× / $2.1k stay skinny.
                if (
                    skinny
                    and multiple >= 3.0
                    and float(card.get("last_liq") or 0) >= 8_000
                    and holders is not None
                    and (
                        int(holders) > RH_HUNT_THIN_HOLDERS
                        if chain == "robinhood"
                        else not is_thin_approaching_print(holders, chain)
                    )
                ):
                    skinny = 0
                # Live /rh 10:22: PUFFLING created 2026-07-27 sat #1
                # after first_seen today. Keep POW / YOLO / leftover
                # PONS on the desk; rank this-window launches first.
                # Live 16:00: BONER July 29 / 935w sat above FIGGER
                # today / 6w because thin ranked before leftover-created.
                # This-window books — including thin factory prints —
                # outrank July leftovers. POW stays, just lower.
                stale = 0
                if chain == "robinhood" and fresh_hours > 0:
                    launched = _parse_ts(card.get("created_at"))
                    if launched is not None:
                        age_cut = datetime.now(timezone.utc) - timedelta(hours=fresh_hours)
                        stale = 1 if launched < age_cut else 0
                # Live 19:00 Sol hunt: GOAF $14.5M / 1.04× sat #3,
                # PUMPCADE $19.8M / 1.00× #8, USWR $7M / 1.04× #4,
                # USMS $6M / 1.03×, McRib $3.4M / 1.10× occupying
                # this-window chairs. Live 19:40: USMS dumped to
                # $1.65M / 1.08× and sat #18 under the $2M cut.
                # Sort leftover size ≥$1.5M AND multiple <1.8 below
                # this-window prints. Live 23:00: WOTF $6.1M / 1.38× /
                # $206k liq sat #7 under the old <1.2 cut. Live 23:40:
                # SPCX $2.1M / 1.59× sat #13. Live 00:00: WOTF ran to
                # 1.75× / $7.8M and sat #8 under the <1.6 cut. Stay on
                # the desk — do not apply is_historical. DICKBUTT 2.13×
                # / $1M and Home 1.59× / $76k stay up. GTA $526k / 1.13×
                # stays. Fat leftover LP still uses multiple <1.2.
                # Live 00:40: Gemini AI $575k / 1.26× / $1.8k and
                # POKEMON $516k / 1.14× / $1.8k sat #18/#23 after
                # leftover-size required $1.5M. A dumped $500k+ book
                # that collapsed to skinny LP is leftover tape, not a
                # this-window hunt. GTA $526k / $58k fat stays up.
                leftover_size = 0
                mcap = float(card.get("max_mcap") or 0)
                liq = float(card.get("last_liq") or 0)
                launched = _parse_ts(card.get("created_at")) or _parse_ts(
                    card.get("first_seen_at")
                )
                age_s = (
                    (datetime.now(timezone.utc) - launched).total_seconds()
                    if launched is not None
                    else 0.0
                )
                if chain == "sol":
                    leftover_size = (
                        1
                        if (
                            (multiple < 1.8 and mcap >= 1_500_000)
                            # Live 09:40: BUN $13.2M / 1.87× / 2.8h sat
                            # #5 after crossing the $1.5M / <1.8 cut.
                            # A $5M leftover that ticked under 1.9 is
                            # still leftover tape, not a 5–50x hunt.
                            # Magatard $1.67M / 1.92× stays. Do not
                            # leftover-sort 2×+ (USTF 2.70 / WOTF 2.61).
                            or (multiple < 1.9 and mcap >= 5_000_000)
                            # Live 19:20: LQX / NTDA / RST $69k mcap /
                            # $580k–$607k liq and WWR $410k liq sat
                            # #7–#16 above Home / SOLPOOP. Live 07:40:
                            # WOTF $69k / $201k liq / 1.00× sat #1 —
                            # the 3.0× cut missed 2.9× leftover LP.
                            # Fat leftover LP at the graduation print
                            # is not a new runner. Stay on the desk.
                            or (
                                multiple < 1.2
                                and liq >= 200_000
                                and mcap > 0
                                and liq > 2.8 * mcap
                            )
                            or (
                                multiple < 1.3
                                and mcap >= 500_000
                                and DEAD_POOL_LIQ <= liq < SKINNY_HUNT_LIQ
                            )
                            # Live 01:20: WWR $1.17M / 2w / $106k sat
                            # #14 under GOOGL 12w. A 2-wallet $500k+
                            # book is a sniper major, not a this-window
                            # hunt. Starbucks 18w / $504k stays up.
                            or (
                                holders is not None
                                and int(holders) <= 2
                                and mcap >= 500_000
                            )
                            # Live 08:40: BEAVER $861k / 1.00× / 11w /
                            # $63k sat #52. The 2-wallet $500k sniper
                            # cut missed approaching-thin majors.
                            # Steam 21w / $752k and HIERO 200w stay.
                            or (
                                is_thin_approaching_print(holders, chain)
                                and mcap >= 500_000
                                and multiple < 1.2
                            )
                            # Live 02:20: SOLCAT $1.37M / 1.01× / $97k
                            # sat #1 two minutes after ingest. Live
                            # 03:00: PONS $1.18M / 1.11× / $106k sat
                            # #33 under the $1.2M cut. Flat $1.1M+ is
                            # leftover tape, not a new hunt.
                            # Starbucks $504k and DICKBUTT 2.13× stay.
                            or (
                                multiple < 1.2
                                and mcap >= 1_100_000
                            )
                            # Live 12:40: TAURA 680w / $1.21M / 1.34× /
                            # 0.92h sat #1 above AFWOG 3.30× /
                            # Robinhood 2.07× / BOLD 4.66×. The $1.1M
                            # leftover needs <1.20. ChatGPT leftover
                            # needs 45–69w / $500k+ / 45m. A 650–749w
                            # $1.15–1.30M book still under 1.35 at 45m
                            # is leftover tape. FOMO $1.01M stays.
                            # Google AI $900k stays. Puggle $901k
                            # stays. GTA 38w stays. ChatGPT $565k
                            # stays. MINI $894k stays. XIAOMI $1.7M
                            # stays. Do not leftover 1.35×+ or $1.30M+
                            # or below $1.15M or under 650w or 750w+.
                            # Do not leftover FOMO $1.01M 1.14× /
                            # Google AI $900k 1.10×+ / Puggle $901k.
                            # Do not leftover GTA under 55w. Do not
                            # leftover under 45m. Do not leftover-sort
                            # 2×+.
                            or (
                                holders is not None
                                and 650 <= int(holders) <= 749
                                and 1_150_000 <= mcap < 1_300_000
                                and multiple < 1.35
                                and age_s >= 45 * 60
                            )
                            # Live 13:00: DANKFROGE 304w / $1.07M /
                            # 1.23× / last $578k (0.66× t0) / 0.34h
                            # / prepumped sat #2 above MEMES 4.73× /
                            # sapphy 2.49×. $1.1M leftover needs
                            # <1.20. 200w+ / $500k leftover needs
                            # <1.20. A 300–349w $1.05–1.15M book
                            # still under 1.25 at 15m is leftover
                            # tape. FOMO $1.01M stays. TAURA
                            # $1.21M stays on its own band.
                            # Puggle $901k stays. Google AI $900k
                            # stays. GTA 38w stays. ChatGPT $565k
                            # stays. Do not leftover 1.25×+ or
                            # $1.15M+ or below $1.05M or under
                            # 300w or 350w+. Do not leftover
                            # under 15m. Do not leftover-sort
                            # 2×+. Do not leftover TAURA 1.35×+
                            # / $1.30M+.
                            or (
                                holders is not None
                                and 300 <= int(holders) <= 349
                                and 1_050_000 <= mcap < 1_150_000
                                and multiple < 1.25
                                and age_s >= 15 * 60
                            )
                            # Live 04:00: GTA $517k / 1.12× / 8.3h sat
                            # #32 and TRUMP1 $551k / 1.16× / 11.7h sat
                            # #37. Live 04:10: MarsCoin / Max / BYTECAT
                            # $69k / 1.00× / 8h sat #68–#70 on the 80
                            # page. Live 04:40: SMOLAPE $61k / 1.21× /
                            # 10h and HOPE $189k / 1.28× / 12.8h sat
                            # #37/#40. Live 05:20: HAM $84k / 1.34× /
                            # 7.6h sat #33 (same band as RH LIZZZARD).
                            # Flat 8h+ / <1.4 is leftover. Starbucks
                            # $504k / 3h and Home 1.59× stay.
                            or (
                                multiple < 1.4
                                and age_s >= 8 * 3600
                            )
                            # Live 06:00: TikTok $524k / 1.16× / 6.5h
                            # sat #35 with a fat $59k book. Live 06:42:
                            # Redbull $553k / 1.22× / 6.6h sat #27 —
                            # the <1.2 cut missed it. Live 07:24:
                            # OpenAI $560k / 1.23× / 5.6h sat #23 and
                            # NASA $533k / 1.17× / 5.9h sat #24. Flat
                            # 5.5h+ / $500k+ / <1.25 is leftover.
                            # Claude $513k / 5.1h, Coca Cola
                            # this-window, and Home 1.59× stay. Do not
                            # lower the 8h clock. Do not leftover
                            # $900k this-window.
                            or (
                                multiple < 1.25
                                and mcap >= 500_000
                                and age_s >= 5.5 * 3600
                            )
                            # Live 07:24: DEAD $43k / 1.00× / 7.7h /
                            # 194w sat #27. Live 08:00: RICHDEBT $136k
                            # / 1.00× / 5.6h / 48w sat #22. Flat 5.5h+
                            # / <1.2 is leftover. The 8h / <1.4 clock
                            # stays. HIERO 1.61× / Magachud 1.88× /
                            # Home 1.59× stay.
                            or (
                                multiple < 1.2
                                and age_s >= 5.5 * 3600
                            )
                            # Live 12:00: DEXAI $499k / 1.00× / 0.8h /
                            # 292w sat #6 on the 4h desk after a
                            # pre-pump t0. Coca Cola 1.13× / Redbull
                            # 1.10× / LEGO 1.12× stay. Do not leftover
                            # $900k this-window climbs. POKEMON 1.36×
                            # waits for 8h / <1.4. Softcake start-high
                            # $85k waits for 8h / <1.4.
                            or (
                                multiple < 1.05
                                and mcap >= 400_000
                                and age_s >= 0.75 * 3600
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 17:00: SOLCAT $933k / 1.07× / 76w /
                            # 0.3h / prepumped sat #7 above potato
                            # 3.48× / HIERO 1.56×. The $1.1M leftover
                            # missed $900k. Prepumped $400k / <1.05 /
                            # 45m missed 1.07 and 20m. Coca Cola 72w /
                            # $526k / 1.13× stays. Redbull 73w /
                            # $931k / 1.10× stays. LEGO 1.41× stays.
                            # HIERO 1.56× stays. Do not lower the
                            # $1.1M leftover. Do not leftover $900k
                            # 1.10×+ this-window climbs.
                            or (
                                holders is not None
                                and int(holders) >= 70
                                and mcap >= 800_000
                                and multiple < 1.10
                                and age_s >= 0.25 * 3600
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 20:33: Anthropic 83w / $905k / 1.03× /
                            # 0.04h sat #2 above BEAVER 1.21× / GTA 6
                            # $983k / ONBOARDING 4.89×. The 70w+ /
                            # $800k leftover needs prepumped + 15m.
                            # A $900k 1.03× 70–99w book is leftover
                            # tape, not a new runner. Coca Cola 72w /
                            # $526k stays. HOOD 72w / $540k stays.
                            # Redbull 73w / $931k / 1.10× stays.
                            # SOLCAT 76w / $1.0M / 1.15× stays. GTA 6
                            # 42w stays. rusty 100w stays. Do not
                            # leftover $800k 1.05×+ or 100w+. Do not
                            # leftover $900k 1.10×+ this-window climbs.
                            or (
                                holders is not None
                                and 70 <= int(holders) <= 99
                                and mcap >= 800_000
                                and multiple < 1.05
                            )
                            # Live 17:00: ChatGPT $565k / 1.18× / 55w /
                            # 1.0h / prepumped sat #10 above ELON
                            # 1.48× / PC 2.56×. Prepumped $400k /
                            # <1.05 missed 1.18. Coca Cola 72w /
                            # $526k / 1.13× stays. Redbull 73w stays.
                            # Starbucks 18w / Kamala 16w stay. Do not
                            # leftover 70w+ Coca Cola.
                            or (
                                holders is not None
                                and 45 <= int(holders) <= 69
                                and mcap >= 500_000
                                and multiple < 1.20
                                and age_s >= 0.75 * 3600
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 04:20: POKEMON 54w / $507k / 1.07× /
                            # 0.07h / prepumped sat #1 above ponscoin
                            # 1.75× / Kimi 2.40×. ChatGPT leftover
                            # waits 45m and covers 1.18. Prepumped
                            # $400k / <1.05 waits 45m. A 45–69w
                            # $450–600k 1.07× prepumped book at 10m
                            # is leftover tape. ChatGPT 1.18× stays.
                            # Coca Cola 72w / $526k / 1.13× stays.
                            # Redbull 73w / 1.10× stays. Starbucks
                            # 18w stays. Do not leftover 1.08×+ or
                            # $600k+ or under 45w or 70w+. Do not
                            # cut the ChatGPT 45m clock.
                            or (
                                holders is not None
                                and 45 <= int(holders) <= 69
                                and 450_000 <= mcap < 600_000
                                and multiple < 1.08
                                and age_s >= 10 * 60
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 08:40: RUNNER 63w / $473k / 1.00× /
                            # 0.02h sat #1. ChatGPT leftover needs
                            # $500k+ / prepumped / 45m. Prepumped
                            # $400k / <1.05 waits 45m. A 60–69w
                            # $400–500k flat at 45m is leftover
                            # tape. ChatGPT $500k+ stays. Coca Cola
                            # 72w / $536k stays. MINI 57w stays.
                            # CASHDOG 68w stays. Redbull 73w stays.
                            # Do not leftover 1.05×+ or $500k+ or
                            # below $400k or under 60w or 70w+.
                            # Do not leftover MINI under 60w. Do
                            # not leftover PONS-named. Do not cut
                            # the ChatGPT 45m clock. Do not leftover
                            # under 45m. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 400_000 <= mcap < 500_000
                                and multiple < 1.05
                                and age_s >= 0.75 * 3600
                            )
                            # Live 09:00: USDP 62w / $49k / 1.00× /
                            # 2.1h sat among leftover tape after
                            # RUNNER 1.15× / CASHDOG 2.12×. RUNNER
                            # leftover is $400–500k / 45m. APE
                            # leftover is $80–150k. PONS $69k stays.
                            # A 60–69w $40–55k flat at 15m is
                            # leftover tape. CASHDOG 68w stays.
                            # Coca Cola 72w stays. HOTDOG $111k
                            # stays. CALLS 1.10× stays. AERO $32k
                            # stays. Do not leftover 1.05×+ or
                            # $55k+ or below $40k or under 60w
                            # or 70w+. Do not leftover PONS-named.
                            # Do not leftover RUNNER $400k+ re-open.
                            # Do not leftover HOTDOG $111k. Do not
                            # leftover under 15m. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 40_000 <= mcap < 55_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 09:20: CALLS 66w / $56k / 1.10× /
                            # 1.9h sat in leftover tape. USDP leftover
                            # is $40–55k. APE leftover is $80–150k.
                            # PONS $69k stays. A 60–69w $55–65k flat
                            # at 15m is leftover tape. CASHDOG 68w
                            # stays. Coca Cola 72w stays. CALLS
                            # 1.10× stays. HOTDOG $111k stays.
                            # USDP $49k stays on its own band. Do
                            # not leftover 1.05×+ or $65k+ or below
                            # $55k or under 60w or 70w+. Do not
                            # leftover PONS-named / $69k. Do not
                            # leftover USDP $40–55k re-open. Do not
                            # leftover HOTDOG $111k. Do not leftover
                            # under 15m. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 55_000 <= mcap < 65_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 19:54: ACC 61w / $66k / 1.33× /
                            # 0.40h sat #5 above MEMES 4.73×.
                            # CALLS leftover needs $55–65k / <1.05.
                            # FIGHT leftover is RH $70–90k / <1.10.
                            # Shapan leftover needs $170–200k.
                            # A 60–64w $65–72k book still 1.20–
                            # 1.40 at 20m is leftover tape. CALLS
                            # 1.10× stays. PONS $69k stays. FIGHT
                            # $77k stays. A 1.40× climb stays.
                            # Do not leftover 1.40×+ or $72k+ or
                            # below $65k or under 60w or 65w+.
                            # Do not leftover under 20m. Do not
                            # leftover CALLS / PONS $69k / FIGHT
                            # $77k / Shapan / Coca Cola 72w.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 64
                                and 65_000 <= mcap < 72_000
                                and 1.20 <= multiple < 1.40
                                and age_s >= 20 * 60
                            )
                            # Live 16:22: Shapan 62w / $184k / 1.22× /
                            # 0.59h sat #6 above MEMES 4.73× /
                            # sapphy 2.49×. RUNNER leftover needs
                            # $400–500k / <1.05. FIGHT leftover is
                            # $70–90k / <1.10. USDP leftover is
                            # $40–55k. A 60–69w $170–200k book
                            # still under 1.25 at 30m is leftover
                            # tape. UNSTABLE $163k stays. FIGHT
                            # $77k stays. RUNNER $400k+ stays.
                            # AFWOG 3.38× stays. Coca Cola 72w
                            # stays. A 1.25× climb stays. Do not
                            # leftover 1.25×+ or $200k+ or below
                            # $170k or under 60w or 70w+. Do not
                            # leftover under 30m. Do not leftover
                            # UNSTABLE $163k. Do not leftover
                            # Coca Cola 72w / 70w+. Do not leftover
                            # 26–35w this-window. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 170_000 <= mcap < 200_000
                                and multiple < 1.25
                                and age_s >= 30 * 60
                            )
                            # Live 12:21: BEN $894k / 1.03× / 19w / 0.2h
                            # / prepumped sat #3. The 15w approaching-
                            # thin $500k leftover missed 16–25w sniper
                            # majors. Starbucks 18w / $504k stays.
                            # Kamala 16w / $511k / 1.10× stays. AGI
                            # 37w / LEGO 76w stay. Do not leftover
                            # $900k this-window climbs (1.10×+).
                            or (
                                holders is not None
                                and 15 <= int(holders) <= 25
                                and mcap >= 800_000
                                and multiple < 1.05
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 22:48: BEAST 19w / $477k / 1.003× /
                            # 0.03h / prepumped sat #1. The 15–25w /
                            # $800k leftover missed $400–$800k sniper
                            # majors. Prepumped $400k / <1.05 waits
                            # 45m. Approaching-thin $500k leftover
                            # missed $477k. Starbucks 18w / $504k /
                            # 1.10× stays. Kamala 16w / $511k / 1.10×
                            # stays. Named BEAST 19w / $934k / 1.07×
                            # stays. SHITLESS 25w / $254k stays.
                            # Do not leftover 1.05×+ or below $400k.
                            or (
                                holders is not None
                                and 15 <= int(holders) <= 25
                                and mcap >= 400_000
                                and multiple < 1.05
                                and age_s >= 10 * 60
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 06:40: TRUMPLESS 17w / $201k / 1.04× /
                            # 0.37h sat #8 above CASHDOG 2.12× / PIPA
                            # 2.33×. The 15–25w / <$200k leftover
                            # missed $201k. The 15–25w / $400k
                            # leftover needs prepumped. A 15–25w
                            # $190–250k flat at 15m is leftover
                            # tape. IBM $143k stays. SHITLESS $254k
                            # stays. Starbucks $504k stays. Kamala
                            # $511k stays. SOLBULL $157k stays.
                            # Named BEAST $934k stays. Do not
                            # leftover 1.05×+ or $250k+ or below
                            # $190k or under 15w or 26w+. Do not
                            # leftover under 15m. Do not leftover
                            # IBM 15w. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 15 <= int(holders) <= 25
                                and 190_000 <= mcap < 250_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 13:32: BOYZBUTT 24w / $104k / 1.04×
                            # and SOLSHREK 19w / $134k / 1.03× sat #1/#2
                            # above ASTRA $1M / LEGO $79k. The $800k
                            # mid-thin leftover missed factory $100k
                            # books. Live 14:00: HACHI 18w / $55k /
                            # 1.18× sat #6 above LEGO. <1.05 missed
                            # the 1.10–1.19 factory band. Starbucks
                            # 18w / $504k stays. Kamala 16w / $511k /
                            # 1.10× stays. Do not leftover $500k+
                            # 1.10×+ climbs.
                            or (
                                holders is not None
                                and 15 <= int(holders) <= 25
                                and 0 < mcap < 200_000
                                and multiple < 1.20
                            )
                            # Live 15:46: TRUMPBEN 23w / $172k / 1.33×
                            # / 5.0h sat #15 above Kamala. The 15–25w
                            # / <1.20 factory leftover missed 1.20–1.34
                            # books that sat 4h+. BOYZBUTT 24w / 1.78×
                            # stays. Kamala 16w / $511k stays.
                            # Starbucks 18w / $504k stays. Steam 21w
                            # / $1.1M / 2.47× stays. Do not leftover
                            # 1.35×+ climbs or $200k+ mid-thin.
                            or (
                                holders is not None
                                and 15 <= int(holders) <= 25
                                and 0 < mcap < 200_000
                                and multiple < 1.35
                                and age_s >= 4 * 3600
                            )
                            # Live 20:11: SOLBROS 21w / $166k / 1.32× /
                            # 3.1h sat #15 above LIKESOL 2.09× / HIERO
                            # 1.56× / rusty 1.74×. The 15–25w / <1.20
                            # factory leftover missed 1.20–1.32. The
                            # 15–25w / <1.35 leftover waits 4h. 1.32×
                            # $166k mid-thin at 3h is leftover tape.
                            # BOYZBUTT 24w / 1.78× stays. Kamala 16w /
                            # $511k stays. Starbucks 18w / $504k stays.
                            # Steam 21w / $1.1M / 2.47× stays. Do not
                            # leftover 1.35×+ climbs or $200k+ mid-thin.
                            or (
                                holders is not None
                                and 15 <= int(holders) <= 25
                                and 0 < mcap < 200_000
                                and multiple < 1.33
                                and age_s >= 3 * 3600
                            )
                            # Live 21:34: SOLBULL 23w / $157k / 1.23× /
                            # 0.44h sat #2 above TikZ 2.30× / FOMO
                            # 2.12×. The 15–25w / <1.20 leftover missed
                            # 1.23. The 15–25w / <1.33 leftover waits
                            # 3h. 1.23× $157k mid-thin at 25m is
                            # leftover tape. SOLBROS 21w / 1.32× stays.
                            # BOYZBUTT 24w / 1.78× stays. Kamala 16w /
                            # $511k stays. Starbucks 18w / $504k stays.
                            # Steam 21w / $1.1M / 2.47× stays. UI 15w /
                            # 2.10× stays. Do not leftover 1.25×+
                            # climbs or $200k+ mid-thin.
                            or (
                                holders is not None
                                and 15 <= int(holders) <= 25
                                and 0 < mcap < 200_000
                                and multiple < 1.25
                                and age_s >= 25 * 60
                            )
                            # Live 19:09: STOCKCAT 18w / $125k / 1.37× /
                            # 0.38h sat #5 above MEMES 4.73× /
                            # sapphy 2.49×. The 15–25w / <1.25
                            # leftover missed 1.37. The 15–25w /
                            # <1.35 leftover waits 4h. TRUMPLESS
                            # leftover needs $190–250k / <1.05.
                            # A 16–19w $120–130k book still under
                            # 1.40 at 20m is leftover tape.
                            # SOLSHREK $134k stays. IBM $143k
                            # stays. Starbucks $504k stays.
                            # Kamala $511k stays. A 1.40× climb
                            # stays. Do not leftover 1.40×+ or
                            # $130k+ or below $120k or under 16w
                            # or 20w+. Do not leftover under 20m.
                            # Do not leftover TRUMPLESS $231k /
                            # IBM 15w / Starbucks / Kamala /
                            # SOLBULL $157k. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 16 <= int(holders) <= 19
                                and 120_000 <= mcap < 130_000
                                and multiple < 1.40
                                and age_s >= 20 * 60
                            )
                            # Live 19:51: BAPE 27w / $140k / 1.00× /
                            # 0.3h sat #2 above ONBOARDING 4.89× /
                            # gigacz 2.93×. The 26–45w / <1.15 leftover
                            # waits 30m. 1.00× $140k flats are leftover
                            # at 15m. HUGGY 37w / 1.12× stays on the
                            # 30m clock. Starbucks 18w / $504k stays.
                            # GTA 6 42w / $983k stays. GOOGL 30w /
                            # $529k stays. SOLDOG 40w / 1.16× stays.
                            # Do not leftover 1.05×+ or $200k+ mid-thin.
                            or (
                                holders is not None
                                and 26 <= int(holders) <= 45
                                and 0 < mcap < 200_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 15:10: HUGGY 37w / $155k / 1.12× sat
                            # #5 above Coca Cola. The 15–25w factory
                            # leftover missed 26–45w $100k books.
                            # Starbucks 18w / $504k stays. Kamala 16w
                            # / $511k stays. RJACK 54w stays.
                            # BOYZBUTT 24w / 1.78× stays. Do not
                            # leftover 1.15×+ climbs.
                            or (
                                holders is not None
                                and 26 <= int(holders) <= 45
                                and 0 < mcap < 200_000
                                and multiple < 1.15
                                and age_s >= 0.5 * 3600
                            )
                            # Live 05:20: NSB 37w / $58k / 1.24× /
                            # 1.7h sat #10 above ROBINAPE 2.56× /
                            # KIWI 3.27×. HUGGY leftover needs <1.15
                            # at 30m. BAPE leftover needs <1.05 at
                            # 15m. A 26–45w $40–70k book still under
                            # 1.25 at 90m is leftover tape. HUGGY
                            # $155k stays. SOLDOG 1.16× waits 90m.
                            # IBM 15w stays. HOODINU 46w / 1.25×
                            # stays. Kamala $511k stays. BOYZBUTT
                            # 1.78× stays. Do not leftover 1.25×+
                            # or $70k+ or below $40k or under 26w
                            # or 46w+. Do not leftover 1.15×+ under
                            # 90m. Do not cut the HUGGY 30m clock.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 26 <= int(holders) <= 45
                                and 40_000 <= mcap < 70_000
                                and multiple < 1.25
                                and age_s >= 1.5 * 3600
                            )
                            # Live 06:20: BNBCAT 38w / $251k / 1.51× /
                            # 13.3h sat #17 above ROBINAPE 2.56× /
                            # PUMPONS 2.15×. NSB leftover needs
                            # $40–70k / <1.25. HUGGY leftover needs
                            # <$200k / <1.15. A 26–45w $200–280k
                            # book still under 1.55 at 6h is leftover
                            # tape. HUGGY $155k stays. SOLBULL $67k
                            # stays. IBM 15w stays. SHITLESS 25w
                            # stays. GTA $983k stays. Google AI $953k
                            # stays. POKEMON $473k stays. Ovary 55w
                            # stays. PIPA $70k stays. Do not leftover
                            # 1.55×+ or $280k+ or below $200k or
                            # under 26w or 46w+. Do not leftover
                            # under 6h. Do not leftover 1.25×+ on
                            # the $40–70k band under 90m. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 26 <= int(holders) <= 45
                                and 200_000 <= mcap < 280_000
                                and multiple < 1.55
                                and age_s >= 6 * 3600
                            )
                            # Live 19:07: APE 48w / $131k / 1.00× /
                            # 0.4h sat #1 above gigacz 2.93× / HIERO
                            # 1.56×. The 26–45w leftover missed 46–69w
                            # $100k flats. RJACK 54w / $46k stays.
                            # ChatGPT 55w / $873k / 1.82× stays.
                            # HOODINU 46w / 1.25× stays. Coca Cola
                            # 72w stays. Do not leftover 1.05×+ or
                            # RJACK 54w.
                            or (
                                holders is not None
                                and 46 <= int(holders) <= 69
                                and 80_000 <= mcap < 150_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 10:00: Gao 49w / $42k / 1.00× /
                            # 0.35h sat in leftover tape. USO leftover
                            # is <$40k. PHOENIX leftover is $45–55k.
                            # APE leftover needs $80k. BAPE leftover
                            # needs 26–45w. A 45–49w $40–45k flat at
                            # 15m is leftover tape. PIPA $70k stays.
                            # RHC $50k stays. RJACK $46k stays.
                            # ponstrump $52k stays. Do not leftover
                            # 1.05×+ or $45k+ or below $40k or under
                            # 45w or 50w+. Do not leftover PIPA $70k.
                            # Do not leftover ponstrump $50–60k
                            # re-open. Do not leftover under 15m.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 45 <= int(holders) <= 49
                                and 40_000 <= mcap < 45_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 10:00: GTA 6 Coin 57w / $543k /
                            # 1.12× / 0.35h sat #4 above ALONROBIN
                            # 2.12× / CASHDOG 2.12×. ChatGPT leftover
                            # waits 45m / $500k+. RUNNER leftover is
                            # 60–69w / $400–500k. MINI leftover is
                            # under 60w / $894k. A 55–58w $520–560k
                            # book still under 1.15 at 20m is leftover
                            # tape. ChatGPT $565k stays. MINI $894k
                            # stays. RUNNER $542k stays. Coca Cola
                            # 72w stays. Do not leftover 1.15×+ or
                            # $560k+ or below $520k or under 55w or
                            # 59w+. Do not leftover ChatGPT $565k /
                            # $873k. Do not leftover MINI $894k. Do
                            # not leftover RUNNER $400–500k re-open.
                            # Do not leftover under 20m. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 55 <= int(holders) <= 58
                                and 520_000 <= mcap < 560_000
                                and multiple < 1.15
                                and age_s >= 20 * 60
                            )
                            # Live 11:00: PEEP 59w / $137k / 1.15× /
                            # 0.3h sat #2 above KIWI 3.27×. APE
                            # leftover needs <1.05. GTA leftover
                            # needs $520k+. ponstrump leftover
                            # needs <$60k. A 50–59w $120–150k book
                            # still under 1.20 at 15m is leftover
                            # tape. ZAPPI $69k stays. ChatGPT $565k
                            # stays. GTA $543k stays. PIKACHU 60w
                            # stays. Ovary $143k / 1.29× stays.
                            # ONLYMEME 49w stays on the APE band.
                            # Do not leftover 1.20×+ or $150k+ or
                            # below $120k or under 50w or 60w+.
                            # Do not leftover GTA $520–560k re-open.
                            # Do not leftover ChatGPT $565k / MINI
                            # $894k. Do not leftover under 15m. Do
                            # not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 120_000 <= mcap < 150_000
                                and multiple < 1.20
                                and age_s >= 15 * 60
                            )
                            # Live 05:40: ponstrump 55w / $52k / 1.045× /
                            # 0.02h sat #1 above PUMPONS 2.15× /
                            # ROBINAPE 2.56×. APE leftover needs
                            # $80–150k. A 50–59w $50–60k flat at 15m
                            # is leftover tape. APE $131k stays.
                            # ChatGPT $565k stays. POKEMON $473k
                            # stays. Ovary $143k / 1.29× stays.
                            # PIPA 45w stays. RJACK $46k stays.
                            # Do not leftover 1.05×+ or $60k+ or
                            # below $50k or under 50w or 60w+. Do
                            # not leftover under 15m. Do not leftover
                            # 46–49w. Do not leftover RJACK $46k.
                            # Do not raise PONS score. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 50_000 <= mcap < 60_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 14:32: POGGERS 380w / $572k / 1.16×
                            # sat #7 above Coca Cola 1.13× / Redbull
                            # 1.10× / ASTRA $1M. Prepumped leftover
                            # needs <1.05 so 1.16 missed. Airdrop tape
                            # needs <$45/w so $100/w missed. Crowded
                            # $500k+ still under 1.2 is leftover tape.
                            # Coca Cola 72w / ASTRA 104w / Starbucks
                            # 18w / Kamala 16w stay. HIERO 200w /
                            # 1.61× stays. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and int(holders) >= 200
                                and mcap >= 500_000
                                and multiple < 1.20
                            )
                            # Live 14:58: MvC 168w / $477k / 1.12× sat
                            # #9 above Coca Cola 1.13×. The 200w /
                            # $500k leftover missed 150–199w $400k
                            # books. Coca Cola 72w / ASTRA 104w /
                            # Starbucks 18w / Kamala 16w stay.
                            # PAIRLESS 233w / $97k stays. HIERO 200w /
                            # 1.61× stays. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and int(holders) >= 150
                                and mcap >= 400_000
                                and multiple < 1.15
                                and age_s >= 2 * 3600
                            )
                            # Live 07:00: UPTOOMUCH 184w / $893k /
                            # 1.00× / 0.08h sat #2 above CASHDOG
                            # 2.12× / PIPA 2.33×. The 200w / $500k
                            # leftover missed 184w. The 150w / $400k
                            # leftover waits 2h and covers 1.12×. A
                            # 150–199w $800k–$1.0M flat at 15m is
                            # leftover tape. MvC $477k stays. FOMO
                            # 131w stays. PAIRLESS $97k stays.
                            # HIERO 1.61× stays. Coca Cola 72w
                            # stays. ASTRA 104w stays. Do not
                            # leftover 1.05×+ or $1.0M+ or below
                            # $800k or under 150w or 200w+. Do not
                            # leftover under 15m. Do not leftover
                            # LEGS $2.1M. Do not leftover $900k
                            # 1.10×+ climbs. Do not leftover-sort
                            # 2×+.
                            or (
                                holders is not None
                                and 150 <= int(holders) <= 199
                                and 800_000 <= mcap < 1_000_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 15:46: BOG 157w / $59k / 1.38× /
                            # 1.8h sat #7 above Coca Cola. The 150w /
                            # $400k leftover missed crowded sub-$80k
                            # 1.3× books. PAIRLESS 233w / $97k /
                            # 1.40× stays. Magatard 3.03× stays.
                            # HIERO 200w / 1.61× stays. GoodBoy 236w
                            # / 1.63× stays. Do not leftover-sort
                            # 1.40×+ or PAIRLESS $97k.
                            or (
                                holders is not None
                                and int(holders) >= 150
                                and 0 < mcap < 80_000
                                and multiple < 1.40
                                and age_s >= 1.5 * 3600
                            )
                            # Live 22:28: BIRBANO 152w / $67k / 1.50× /
                            # 5.6h sat #14 above KAT 3.69× / Magatard
                            # 3.64×. The 150w+ / <$80k leftover missed
                            # 1.50. The 8h / <1.4 clock waits. 5.5h+ /
                            # <1.2 missed 1.50. Crowded sub-$80k 1.50×
                            # at 5.5h is leftover tape. PAIRLESS 233w /
                            # $97k / 1.40× stays. MPGA 1.69× stays.
                            # Lucia 2.45× stays. HIERO $673k / 1.56×
                            # stays. Do not leftover 1.55×+ or $80k+
                            # or PAIRLESS.
                            or (
                                holders is not None
                                and int(holders) >= 150
                                and 0 < mcap < 80_000
                                and multiple < 1.55
                                and age_s >= 5.5 * 3600
                            )
                            # Live 16:22: USELESSTROLL 193w / $80k /
                            # 1.47× / 1.68h sat #15 above MEMES
                            # 4.73× / sapphy 2.49×. The 150w+ /
                            # <$80k leftover waits 5.5h. CRCL
                            # leftover is $55–70k / 5.5h. A
                            # 180–199w $75–90k book still under
                            # 1.50 at 60m is leftover tape.
                            # PAIRLESS $97k stays. CRCL $63k
                            # stays. MEMES 4.73× stays. A 1.50×
                            # climb stays. Do not leftover 1.50×+
                            # or $90k+ or below $75k or under
                            # 180w or 200w+. Do not leftover
                            # under 60m. Do not leftover CRCL
                            # $55–70k re-open. Do not leftover
                            # PAIRLESS $97k. Do not leftover
                            # 26–35w this-window. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 180 <= int(holders) <= 199
                                and 75_000 <= mcap < 90_000
                                and multiple < 1.50
                                and age_s >= 60 * 60
                            )
                            # Live 19:30: WOOFUS 75w / $58k / 1.18× /
                            # 1.9h sat #9 above HUNTR 1.35× / Lucia
                            # 2.45× / MPGA 1.69×. The 90–149w /
                            # <$100k / <1.05 leftover missed 70–89w
                            # $50k books. Coca Cola 72w / $540k /
                            # 1.12× stays. HOOD 72w stays. LEGO 76w /
                            # 1.41× stays. HUNTR 72w / 1.35× stays.
                            # rusty 100w stays. Do not leftover
                            # 1.20×+ or Coca Cola.
                            or (
                                holders is not None
                                and 70 <= int(holders) <= 89
                                and 0 < mcap < 80_000
                                and multiple < 1.20
                                and age_s >= 1.5 * 3600
                            )
                            # Live 07:40: USMS 75w / $69k / 1.00× /
                            # 0.81h sat #29 above leftovered
                            # UPTOOMUCH / TRUMPLESS. WOOFUS leftover
                            # waits 1.5h / <1.20. A 70–79w $60–75k
                            # flat at 15m is leftover tape. Coca
                            # Cola 72w / $536k stays. HOMO 75w /
                            # 1.23× stays. wrldmeme $81k stays.
                            # rusty 90w stays. CASHDOG 68w stays.
                            # LEGO 76w / 1.41× stays. Do not
                            # leftover 1.05×+ or $75k+ or below
                            # $60k or under 70w or 80w+. Do not
                            # leftover HOMO 1.23× under 1.5h. Do
                            # not leftover Coca Cola. Do not
                            # leftover PONS-named. Do not leftover
                            # under 15m. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 70 <= int(holders) <= 79
                                and 60_000 <= mcap < 75_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 08:20: WOTF 89w / $69k / 1.00× /
                            # 0.73h sat #32 above leftovered USMS /
                            # UPTOOMUCH. USMS leftover is 70–79w.
                            # WOOFUS leftover waits 1.5h / <1.20.
                            # A 80–89w $60–75k flat at 15m is
                            # leftover tape. Coca Cola 72w / $536k
                            # stays. HOMO 75w / 1.23× stays. USMS
                            # 75w stays on its own band. rusty
                            # 90w stays. wrldmeme $81k stays.
                            # CASHDOG 68w stays. Do not leftover
                            # 1.05×+ or $75k+ or below $60k or
                            # under 80w or 90w+. Do not leftover
                            # HOMO 1.23× under 1.5h. Do not leftover
                            # USMS 70–79w re-open. Do not leftover
                            # Coca Cola. Do not leftover PONS-named.
                            # Do not leftover under 15m. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 80 <= int(holders) <= 89
                                and 60_000 <= mcap < 75_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 19:51: CATMAX 116w / $64k / 1.19× /
                            # 1.1h sat #6 above HOOD 1.12× / gigacz
                            # 2.93×. The 90–149w / <1.05 leftover
                            # missed 110–149w 1.15–1.19 books. rusty
                            # 100w / $49k / 1.12× stays. BIAO 101w
                            # already leftovered. Coca Cola 72w stays.
                            # HUNTR 72w / 1.35× stays. Magatard 281w
                            # / 1.15× stays. Do not leftover 1.20×+
                            # or rusty 100w.
                            or (
                                holders is not None
                                and 110 <= int(holders) <= 149
                                and 0 < mcap < 80_000
                                and multiple < 1.20
                                and age_s >= 0.75 * 3600
                            )
                            # Live 18:04: BIAO 101w / $88k / 1.00× /
                            # 0.8h sat #14 above potato 3.48× / HIERO
                            # 1.56×. The 150w+ / <$80k leftover missed
                            # 90–149w $80–100k flats. PAIRLESS 233w /
                            # $97k / 1.40× stays. rusty 100w / $49k /
                            # 1.12× stays. Coca Cola 72w stays. LEGO
                            # 76w / 1.41× stays. Magatard 281w /
                            # $493k / 1.15× stays. HIERO 1.56× stays.
                            # Do not leftover 1.05×+ or PAIRLESS $97k.
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 149
                                and 0 < mcap < 100_000
                                and multiple < 1.05
                                and age_s >= 0.75 * 3600
                            )
                            # Live 01:22: ROBINCAT 93w / $116k / 1.05× /
                            # 0.17h sat #1 above CHILLROBIN 1.50× /
                            # ORGY 1.53×. The 90–149w / <$100k leftover
                            # missed $116k. CATMAX leftover needs
                            # 110–149w / <$80k. A 90–109w $100–150k
                            # book still under 1.10 at 15m is leftover
                            # tape. PAIRLESS 233w / $97k stays. rusty
                            # 100w stays. MarsCoin $151k / 1.35× stays.
                            # Coca Cola 72w stays. CATMAX 116w stays.
                            # CHILLROBIN 1.50× stays. Redbull 1.10×
                            # stays. Do not leftover 1.10×+ or below
                            # $100k or $150k+ or under 90w or 110w+.
                            # Do not leftover PAIRLESS $97k.
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 109
                                and 100_000 <= mcap < 150_000
                                and multiple < 1.10
                                and age_s >= 15 * 60
                            )
                            # Live 05:40: MEME 95w / $128k / 1.184× /
                            # 0.14h sat #4 above PUMPONS 2.15× /
                            # ROBINAPE 2.56×. ROBINCAT leftover needs
                            # <1.10. A 90–99w $100–130k 1.10–1.20
                            # book at 15m is leftover tape. ROBINCAT
                            # 1.05 already leftovered. The 1.10×
                            # climb stays. PAIRLESS $97k stays.
                            # rusty 100w stays. MarsCoin $151k stays.
                            # Redbull 73w stays. CHILLROBIN 1.50×
                            # stays. Do not leftover 1.20×+ or
                            # 1.10×- or $130k+ or below $100k or
                            # under 90w or 100w+. Do not leftover
                            # PAIRLESS. Do not cut the ROBINCAT 15m
                            # / <1.10 clock. Do not leftover-sort
                            # 2×+.
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 99
                                and 100_000 <= mcap < 130_000
                                and 1.10 < multiple < 1.20
                                and age_s >= 15 * 60
                            )
                            # Live 17:30: Coca Cola 94w / $554k /
                            # 1.18× / 2.25h sat #17 above MEMES
                            # 4.73× / sapphy 2.49×. $500k leftover
                            # waits 5.5h / <1.25. ChatGPT leftover
                            # is 45–69w / $500k+ / prepumped.
                            # 90–99w leftover is $100–130k.
                            # Anthropic leftover needs $800k /
                            # <1.05. A 90–99w $540–570k book
                            # still under 1.20 at 2h is leftover
                            # tape. Coca Cola 72w / $526k stays.
                            # Aster 73w stays. GTA 81w stays.
                            # Redbull 73w stays. OpenAI $560k /
                            # 1.23× stays. FOMO $1.01M stays.
                            # Google AI $900k stays. Stonks 94w
                            # / 3.68× stays. BEAST 41w / $546k
                            # stays. A 1.20× climb stays. Do not
                            # leftover 1.20×+ or $570k+ or below
                            # $540k or under 90w or 100w+. Do
                            # not leftover under 2h. Do not leftover
                            # Coca Cola 72w / Aster 73w / GTA
                            # under 90w / OpenAI 1.23×+ / FOMO
                            # $1.01M. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 99
                                and 540_000 <= mcap < 570_000
                                and multiple < 1.20
                                and age_s >= 2 * 3600
                            )
                            # Live 18:25: CallMe 360w / $75k / 1.08× /
                            # 0.2h / start-high sat #1 above potato /
                            # HIERO. The 150w+ / <$80k leftover waits
                            # 1.5h. The 90–149w leftover missed 200w+.
                            # PAIRLESS 233w / $97k / 1.40× stays.
                            # Magatard 281w / $493k / 1.15× stays.
                            # rusty 100w / 1.12× stays. HIERO 1.56×
                            # stays. Do not leftover 1.10×+ or PAIRLESS.
                            or (
                                holders is not None
                                and int(holders) >= 200
                                and 0 < mcap < 100_000
                                and multiple < 1.10
                                and age_s >= 10 * 60
                            )
                            # Live 00:08: ironmike 249w / $49k / 1.17× /
                            # 0.16h sat #3 above CEUTA 1.60× / OpenAI
                            # $939k. CallMe leftover missed 1.17. The
                            # 150w+ / <$80k leftover waits 1.5h.
                            # Crowded sub-$80k 1.10–1.19 at 10m is
                            # leftover tape. PAIRLESS 233w / $97k
                            # stays. Guinness 350w / $96k stays.
                            # MPGA 1.69× stays. BIRBANO 152w stays.
                            # rusty 100w stays. Do not leftover
                            # 1.20×+ or $80k+ or under 200w.
                            or (
                                holders is not None
                                and int(holders) >= 200
                                and 0 < mcap < 80_000
                                and multiple < 1.20
                                and age_s >= 10 * 60
                            )
                            # Live 19:54: PSA 219w / $66k / 1.54× /
                            # 0.25h sat #3 above MEMES 4.73×.
                            # ironmike leftover needs <$80k / <1.20.
                            # FROGAS leftover needs 250w+ / $60–80k
                            # / 2h. A 215–225w $63–72k book still
                            # under 1.60 at 15m is leftover tape.
                            # ironmike $49k stays. mikedyson $64k
                            # / 234w stays. PAIRLESS $97k stays.
                            # A 1.60× climb stays. Do not leftover
                            # 1.60×+ or $72k+ or below $63k or
                            # under 215w or 226w+. Do not leftover
                            # under 15m. Do not leftover ironmike
                            # / mikedyson / FROGAS / BULLISHCAT /
                            # PAIRLESS. Do not widen ironmike
                            # 200w+ / <1.20. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 215 <= int(holders) <= 225
                                and 63_000 <= mcap < 72_000
                                and multiple < 1.60
                                and age_s >= 15 * 60
                            )
                            # Live 17:07: BULLISHCAT 234w / $179k /
                            # 1.47× / 0.99h sat #8 above MEMES
                            # 4.73× / sapphy 2.49×. ironmike
                            # leftover needs <$80k / <1.20.
                            # FROGAS leftover needs 250w+ /
                            # $60–80k / 2h. bullson leftover
                            # is 250–299w / $35–55k. A 230–249w
                            # $170–190k book still under 1.50 at
                            # 45m is leftover tape. mikedyson
                            # $64k / 1.51× stays. PAIRLESS $97k
                            # stays. FROGAS 287w stays. bullson
                            # 289w stays. NEKO 360w stays. A
                            # 1.50× climb stays. Do not leftover
                            # 1.50×+ or $190k+ or below $170k or
                            # under 230w or 250w+. Do not leftover
                            # under 45m. Do not widen the ironmike
                            # 200w+ / <1.20 clock. Do not leftover
                            # 26–35w this-window. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 230 <= int(holders) <= 249
                                and 170_000 <= mcap < 190_000
                                and multiple < 1.50
                                and age_s >= 45 * 60
                            )
                            # Live 04:20: TRIPLEP 437w / $83k / 1.55× /
                            # 0.51h sat #5 above Kimi 2.40× / KIWI
                            # 3.27×. CallMe leftover needs <$80k /
                            # <1.10. ironmike leftover needs <$80k /
                            # <1.20. A 400w+ $70–90k book still under
                            # 1.55 at 25m is leftover tape. HIERO
                            # 1.56× stays. PAIRLESS $97k stays. MPGA
                            # 1.69× stays. RIG 269w stays. MarsCoin
                            # 175w stays. Do not leftover 1.55×+ or
                            # $90k+ or under 400w or under 25m. Do
                            # not leftover-sort 2×+.
                            or (
                                holders is not None
                                and int(holders) >= 400
                                and 70_000 <= mcap < 90_000
                                and multiple < 1.55
                                and age_s >= 25 * 60
                            )
                            # Live 09:40: FROGAS 287w / $70k / 1.47× /
                            # 2.1h sat #8 above CASHDOG 2.12× / KIWI
                            # 3.27×. ironmike leftover needs <1.20.
                            # TRIPLEP leftover needs 400w+. A 250–349w
                            # $60–80k book still under 1.50 at 2h is
                            # leftover tape. YUGE $58k stays. Ben
                            # $107k stays. KIWI 3.27× stays. PAIRLESS
                            # 233w stays. Guinness 350w stays. Do not
                            # leftover 1.50×+ or $80k+ or below $60k
                            # or under 250w or 350w+. Do not leftover
                            # under 2h. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 250 <= int(holders) <= 349
                                and 60_000 <= mcap < 80_000
                                and multiple < 1.50
                                and age_s >= 2 * 3600
                            )
                            # Live 19:32: ideal life 266w / $81k /
                            # 1.54× / 0.53h sat #3 above MEMES
                            # 4.73×. FROGAS leftover needs
                            # $60–80k / <1.50 / 2h. bullson
                            # leftover needs $35–55k / <1.35.
                            # A 260–275w $80–90k book still
                            # under 1.60 at 30m is leftover
                            # tape. RUNIT 280w / 1.84× stays.
                            # FROGAS $70k stays. bullson $44k
                            # stays. PAIRLESS $97k stays. NEKO
                            # 360w stays. A 1.60× climb stays.
                            # Do not leftover 1.60×+ or $90k+
                            # or below $80k or under 260w or
                            # 276w+. Do not leftover under 30m.
                            # Do not leftover FROGAS / bullson /
                            # BULLISHCAT / RUNIT 280w / NEKO.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 260 <= int(holders) <= 275
                                and 80_000 <= mcap < 90_000
                                and multiple < 1.60
                                and age_s >= 30 * 60
                            )
                            # Live 11:41: MUMONO 336w / $63k / 1.21× /
                            # 0.05h sat #1; by 11:45 it was $70k /
                            # 1.34× still #1 above KIWI 3.27× /
                            # CASHDOG 2.12×. ironmike leftover needs
                            # <1.20. FROGAS leftover waits 2h / <1.50.
                            # The 150w+ / <$80k leftover waits 1.5h.
                            # A 300–349w $60–70k book still under
                            # 1.35 at 15m is leftover tape. Doogestar
                            # $77k / 1.67× stays. FROGAS 287w stays
                            # on its own band. YUGE $58k stays.
                            # Guinness 350w stays. KIWI 3.27× stays.
                            # CASHDOG 2.12× stays. Do not leftover
                            # 1.35×+ or $70k+ or below $60k or under
                            # 300w or 350w+. Do not leftover FROGAS
                            # $60–80k re-open / 1.50×+ / under 2h.
                            # Do not leftover Doogestar 1.67× / 328w.
                            # Do not leftover-sort 2×+. Do not widen
                            # ironmike 200w+ / <1.20.
                            or (
                                holders is not None
                                and 300 <= int(holders) <= 349
                                and 60_000 <= mcap < 70_000
                                and multiple < 1.35
                                and age_s >= 15 * 60
                            )
                            # Live 01:25: reclaim 163w / $50k / 1.16× /
                            # 0.04h sat #2 above CHILLROBIN 1.50×.
                            # ironmike leftover needs 200w+. The 150w+
                            # / <$80k leftover waits 1.5h. A 150–199w
                            # sub-$80k 1.10–1.19 book at 15m is
                            # leftover tape. PAIRLESS 233w / $97k
                            # stays. BIRBANO 152w / 1.50× stays.
                            # MPGA 1.69× stays. rusty 100w stays.
                            # CHILLROBIN 1.50× stays. ironmike 1.77×
                            # stays. Do not leftover 1.20×+ or $80k+
                            # or under 150w or 200w+. Do not leftover
                            # PAIRLESS. Do not cut the ironmike 10m
                            # clock.
                            or (
                                holders is not None
                                and 150 <= int(holders) <= 199
                                and 0 < mcap < 80_000
                                and multiple < 1.20
                                and age_s >= 15 * 60
                            )
                            # Live 21:34: Guinness 350w / $96k / 1.38× /
                            # 0.41h / start-high sat #1 above TikZ
                            # 2.30× / FOMO 2.12×. The 200w+ / <$100k
                            # leftover missed 1.38. The 150w+ / <$80k
                            # leftover waits 1.5h and misses $96k.
                            # Start-high 200w+ dumps under $150k /
                            # 1.40× at 15m are leftover tape. TikZ
                            # 2.30× / uselesscat 2.32× / Lucia 2.45×
                            # stay. MPGA 1.69× stays. PAIRLESS 233w /
                            # $97k / 1.40× stays. rusty 100w / 1.74×
                            # stays. Do not leftover 1.40×+ or books
                            # without the start-high needle.
                            or (
                                holders is not None
                                and int(holders) >= 200
                                and 0 < mcap < 150_000
                                and multiple < 1.40
                                and age_s >= 15 * 60
                                and any(
                                    "start-high" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 10:20: worthless 169w / $86k / 1.25× /
                            # 6.8h / start-high sat #16 above KIWI
                            # 3.27× / CASHDOG 2.12×. 150w+ leftover
                            # needs <$80k. Guinness leftover needs
                            # 200w+. A 150–199w $80–100k start-high
                            # dump still under 1.30 at 2h is leftover
                            # tape. PAIRLESS $97k stays. AMC $188k
                            # stays. Ajax $70k stays. Do not leftover
                            # 1.30×+ or $100k+ or below $80k or under
                            # 150w or 200w+. Do not leftover books
                            # without the start-high needle. Do not
                            # leftover under 2h. Do not leftover-sort
                            # 2×+.
                            or (
                                holders is not None
                                and 150 <= int(holders) <= 199
                                and 80_000 <= mcap < 100_000
                                and multiple < 1.30
                                and age_s >= 2 * 3600
                                and any(
                                    "start-high" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 10:20: AMC 163w / $188k / 1.39× /
                            # 2.5h sat #10 above CASHDOG 2.12× /
                            # KIWI 3.27×. 150w+ leftover needs <$80k
                            # or $400k+. Guinness leftover needs
                            # 200w+ / start-high. A 150–199w $170–200k
                            # book still under 1.40 at 2h is leftover
                            # tape. PAIRLESS $97k stays. worthless
                            # $86k stays on its own band. MvC $477k
                            # stays. NOMEME 136w stays. Do not leftover
                            # 1.40×+ or $200k+ or below $170k or under
                            # 150w or 200w+. Do not leftover under 2h.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 150 <= int(holders) <= 199
                                and 170_000 <= mcap < 200_000
                                and multiple < 1.40
                                and age_s >= 2 * 3600
                            )
                            # Live 16:43: のび子 / Long Cat 169w /
                            # $137k / 1.29× / 0.62h sat #8 above
                            # MEMES 4.73× / sapphy 2.49×. AMC
                            # leftover needs $170–200k / 2h.
                            # NOMEME leftover needs 110–149w /
                            # $170–200k. Shapan leftover is
                            # 60–69w / $170–200k. A 160–169w
                            # $130–150k book still under 1.32 at
                            # 30m is leftover tape. Shapan 174w
                            # / $129k stays. MarsCoin $151k
                            # stays. NOMEME $176k stays. A 1.32×
                            # climb stays. Do not leftover 1.32×+
                            # or $150k+ or below $130k or under
                            # 160w or 170w+. Do not leftover
                            # under 30m. Do not leftover the live
                            # Shapan 174w / $129k print. Do not
                            # leftover 26–35w this-window. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 160 <= int(holders) <= 169
                                and 130_000 <= mcap < 150_000
                                and multiple < 1.32
                                and age_s >= 30 * 60
                            )
                            # Live 17:53: CHROME 155w / $141k /
                            # 1.07× / 0.25h sat #1 above MEMES
                            # 4.73× / sapphy 2.49×. Long Cat
                            # leftover needs 160–169w / 30m.
                            # AMC leftover needs $170–200k /
                            # 2h. 150w+ leftover needs <$80k.
                            # A 150–159w $130–150k book still
                            # under 1.10 at 15m is leftover
                            # tape. Long Cat 169w stays.
                            # Shapan 174w / $129k stays.
                            # MarsCoin $151k stays. NOMEME
                            # $176k stays. PAIRLESS $97k
                            # stays. A 1.10× climb stays.
                            # Do not leftover 1.10×+ or
                            # $150k+ or below $130k or under
                            # 150w or 160w+. Do not leftover
                            # under 15m. Do not leftover Long
                            # Cat / Shapan 174w / MarsCoin
                            # $151k. Do not leftover RH
                            # CHROME 1.72×. Do not leftover
                            # 26–35w. Do not leftover-sort
                            # 2×+.
                            or (
                                holders is not None
                                and 150 <= int(holders) <= 159
                                and 130_000 <= mcap < 150_000
                                and multiple < 1.10
                                and age_s >= 15 * 60
                            )
                            # Live 18:16: CAT 178w / $168k / 1.31× /
                            # 0.38h sat #3 above MEMES 4.73× /
                            # sapphy 2.49×. Long Cat leftover
                            # needs 160–169w / $130–150k.
                            # AMC leftover needs $170–200k /
                            # 2h. CHROME leftover is 150–159w
                            # / $130–150k. A 175–185w $160–175k
                            # book still under 1.35 at 20m is
                            # leftover tape. Long Cat 169w
                            # stays. Shapan 174w / $129k stays.
                            # MarsCoin $151k stays. NOMEME
                            # $176k stays. AMC $188k stays.
                            # A 1.35× climb stays. Do not
                            # leftover 1.35×+ or $175k+ or
                            # below $160k or under 175w or
                            # 186w+. Do not leftover under
                            # 20m. Do not leftover Long Cat /
                            # Shapan 174w / MarsCoin $151k /
                            # NOMEME $176k. Do not leftover
                            # 26–35w. Do not leftover-sort
                            # 2×+.
                            or (
                                holders is not None
                                and 175 <= int(holders) <= 185
                                and 160_000 <= mcap < 175_000
                                and multiple < 1.35
                                and age_s >= 20 * 60
                            )
                            # Live 10:40: NOMEME 136w / $176k / 1.24× /
                            # 2.2h sat #15 above CASHDOG 2.12× /
                            # KIWI 3.27×. CATMAX leftover needs <$80k.
                            # AMC leftover needs 150w+. A 110–149w
                            # $170–200k book still under 1.30 at 2h
                            # is leftover tape. PAIRLESS $97k stays.
                            # MarsCoin $151k stays. MEME 45w stays.
                            # AMC $188k stays on its own band. Do not
                            # leftover 1.30×+ or $200k+ or below $170k
                            # or under 110w or 150w+. Do not leftover
                            # under 2h. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 110 <= int(holders) <= 149
                                and 170_000 <= mcap < 200_000
                                and multiple < 1.30
                                and age_s >= 2 * 3600
                            )
                            # Live 14:41: AMC 44w / $916k / 1.09× /
                            # 0.64h sat #5 above MEMES 4.73× /
                            # sapphy 2.49×. ChatGPT leftover needs
                            # 45–69w / $500k+ / 45m / prepumped.
                            # $900k 1.10×+ stays. The 150w AMC
                            # leftover is $170–200k / 2h. A 40–44w
                            # $850–980k book still under 1.10 at
                            # 30m is leftover tape. GTA 38w stays.
                            # Puggle $901k stays. FOMO $1.01M stays.
                            # ChatGPT 45w stays. A 1.10× climb
                            # stays. Do not leftover 1.10×+ or
                            # $980k+ or below $850k or under 40w
                            # or 45w+. Do not leftover under 30m.
                            # Do not leftover-sort 2×+. Do not
                            # leftover AMC-class 1.40×+ / 150w+.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 44
                                and 850_000 <= mcap < 980_000
                                and multiple < 1.10
                                and age_s >= 30 * 60
                            )
                            # Live 14:41: bullson 289w / $44k /
                            # 1.31× / 1.15h sat #10. ironmike
                            # leftover needs 200w+ / <$80k / <1.20
                            # at 10m. A 250–299w $35–55k book
                            # still under 1.35 at 60m is leftover
                            # tape. NEKO 360w stays. PAIRLESS
                            # $97k stays. ironmike <1.20 stays.
                            # A 1.35× climb stays. Do not leftover
                            # 1.35×+ or $55k+ or below $35k or
                            # under 250w or 300w+. Do not leftover
                            # under 60m. Do not leftover-sort 2×+.
                            # Do not leftover NEKO 360w. Do not
                            # widen ironmike 200w+ / <1.20.
                            or (
                                holders is not None
                                and 250 <= int(holders) <= 299
                                and 35_000 <= mcap < 55_000
                                and multiple < 1.35
                                and age_s >= 60 * 60
                            )
                            # Live 15:01: WTT 43w / $193k / 1.35× /
                            # 2.0h sat #9 above MEMES 4.73× /
                            # sapphy 2.49×. HUGGY leftover needs
                            # 26–45w / <$200k / <1.15 at 30m. NSB
                            # leftover is $40–70k. A 40–45w
                            # $170–200k book still under 1.40 at
                            # 2h is leftover tape. HUGGY $155k
                            # stays. A 1.40× climb stays. AMC
                            # $916k stays on its own band. MEME
                            # 45w stays. Do not leftover 1.40×+
                            # or $200k+ or below $170k or under
                            # 40w or 45w+. Do not leftover under
                            # 2h. Do not leftover-sort 2×+. Do
                            # not cut the HUGGY 30m / <1.15 clock.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 44
                                and 170_000 <= mcap < 200_000
                                and multiple < 1.40
                                and age_s >= 2 * 3600
                            )
                            # Live 18:44: StonkCat 44w / $144k / 1.48× /
                            # 0.39h sat #4 above MEMES 4.73× /
                            # sapphy 2.49×. AMC leftover needs
                            # $850–980k / <1.10. WTT leftover
                            # needs $170–200k / <1.40 / 2h.
                            # HUGGY leftover needs <1.15 at 30m.
                            # A 40–44w $130–145k book still under
                            # 1.50 at 20m is leftover tape. Corgi
                            # $117k stays. SOLDOG $150k stays.
                            # WTT $193k stays. BEAST $546k stays.
                            # AMC $916k stays. A 1.50× climb
                            # stays. Do not leftover 1.50×+ or
                            # $145k+ or below $130k or under 40w
                            # or 45w+. Do not leftover under 20m.
                            # Do not leftover WTT / AMC / BEAST
                            # $546k / Corgi $117k / SOLDOG $150k /
                            # GTA 38w. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+. Do not
                            # cut the HUGGY 30m / <1.15 clock.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 44
                                and 130_000 <= mcap < 145_000
                                and multiple < 1.50
                                and age_s >= 20 * 60
                            )
                            # Live 15:09: trollfone 53w / $152k /
                            # 1.32× / 1.07h sat #7 above MEMES
                            # 4.73× / sapphy 2.49×. PEEP leftover
                            # needs $120–150k / <1.20. APE leftover
                            # needs <1.05. HUGGY leftover needs
                            # 26–45w / <1.15 at 30m. A 50–55w
                            # $150–165k book still under 1.35 at
                            # 60m is leftover tape. PEEP $137k
                            # stays. HUGGY $155k stays. A 1.35×
                            # climb stays. MEME 45w stays. Do not
                            # leftover 1.35×+ or $165k+ or below
                            # $150k or under 50w or 56w+. Do not
                            # leftover under 60m. Do not leftover
                            # 26–35w this-window. Do not leftover
                            # -sort 2×+. Do not cut the HUGGY
                            # 30m / <1.15 clock.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 55
                                and 150_000 <= mcap < 165_000
                                and multiple < 1.35
                                and age_s >= 60 * 60
                            )
                            # Live 15:09: memestock 23w / $332k /
                            # 1.36× / 1.58h sat #11 above MEMES
                            # 4.73× / sapphy 2.49×. The 15–25w
                            # leftover is $190–250k. A 20–25w
                            # $300–360k book still under 1.40 at
                            # 60m is leftover tape. MEMESZN 16w
                            # stays. IBM 15w stays. A 1.40× climb
                            # stays. The $190–250k 15–25w band
                            # stays. Do not leftover 1.40×+ or
                            # $360k+ or below $300k or under 20w
                            # or 26w+. Do not leftover under 60m.
                            # Do not leftover 26–35w this-window.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 20 <= int(holders) <= 25
                                and 300_000 <= mcap < 360_000
                                and multiple < 1.40
                                and age_s >= 60 * 60
                            )
                            # Live 15:41: TESTUS 106w / $59k / 1.29× /
                            # 0.34h sat #5 above MEMES 4.73× /
                            # sapphy 2.49×. ROBINCAT leftover
                            # needs $100–150k / <1.10. CATMAX
                            # leftover needs 110w+ / <1.20 at
                            # 45m. A 100–109w $50–65k book still
                            # under 1.32 at 15m is leftover tape.
                            # rusty 100w stays. ROBINCAT $116k
                            # stays. A 1.32× climb stays. Do not
                            # leftover 1.32×+ or $65k+ or below
                            # $50k or under 100w or 110w+. Do not
                            # leftover under 15m. Do not leftover
                            # 26–35w this-window. Do not leftover
                            # -sort 2×+. Do not cut the ROBINCAT
                            # 15m / <1.10 clock.
                            or (
                                holders is not None
                                and 100 <= int(holders) <= 109
                                and 50_000 <= mcap < 65_000
                                and multiple < 1.32
                                and age_s >= 15 * 60
                            )
                            # Live 18:28: Kilo 106w / $53k / 1.63× /
                            # 0.92h sat #5 above MEMES 4.73× /
                            # sapphy 2.49×. TESTUS leftover needs
                            # $50–65k / <1.32. ROBINCAT leftover
                            # needs $100–150k / <1.10. A 100–109w
                            # $50–55k book still under 1.65 at 45m
                            # is leftover tape. TESTUS $59k stays.
                            # ROBINCAT $116k stays. DOGECAT $167k
                            # stays. A 1.65× climb stays. Do not
                            # leftover 1.65×+ or $55k+ or below
                            # $50k or under 100w or 110w+. Do not
                            # leftover under 45m. Do not leftover
                            # TESTUS $59k / ROBINCAT $116k /
                            # DOGECAT $167k / Coca Cola 72w.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+. Do not cut the
                            # ROBINCAT 15m / <1.10 clock.
                            or (
                                holders is not None
                                and 100 <= int(holders) <= 109
                                and 50_000 <= mcap < 55_000
                                and multiple < 1.65
                                and age_s >= 45 * 60
                            )
                            # Live 16:03: DOGECAT 101w / $167k /
                            # 1.35× / 0.27h sat #3 above MEMES
                            # 4.73× / sapphy 2.49×. ROBINCAT
                            # leftover needs $100–150k / <1.10.
                            # TESTUS leftover is $50–65k / <1.32.
                            # A 100–109w $155–180k book still
                            # under 1.38 at 15m is leftover tape.
                            # ROBINCAT $116k stays. TESTUS $59k
                            # stays. MarsCoin $151k stays. A
                            # 1.38× climb stays. Do not leftover
                            # 1.38×+ or $180k+ or below $155k or
                            # under 100w or 110w+. Do not leftover
                            # under 15m. Do not leftover 26–35w
                            # this-window. Do not leftover-sort
                            # 2×+. Do not cut the ROBINCAT 15m /
                            # <1.10 clock. Do not leftover Coca
                            # Cola 72w / 70w+.
                            or (
                                holders is not None
                                and 100 <= int(holders) <= 109
                                and 155_000 <= mcap < 180_000
                                and multiple < 1.38
                                and age_s >= 15 * 60
                            )
                            # Live 20:15: ELON💤 46w / $213k / 1.00× /
                            # 0.46h sat #4 above MEMES 4.73×.
                            # APE leftover needs $80–150k / <1.05.
                            # BNBCAT leftover needs 26–45w /
                            # $200–280k / 6h. ROBINRUD / DELIVERY
                            # are $30–42k. A 46–49w $200–225k
                            # book still under 1.05 at 20m is
                            # leftover tape. HOODINU 46w / 1.25×
                            # stays. MEME 45w stays. WTT $193k
                            # stays. APE $150k stays. GOAF $3M
                            # stays. LEGO 45w stays. A 1.05×
                            # climb stays. Do not leftover
                            # 1.05×+ or $225k+ or below $200k
                            # or under 46w or 50w+. Do not
                            # leftover under 20m. Do not leftover
                            # HOODINU 46w / 1.25× / MEME 45w /
                            # WTT $193k / APE / BNBCAT under 6h.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 46 <= int(holders) <= 49
                                and 200_000 <= mcap < 225_000
                                and multiple < 1.05
                                and age_s >= 20 * 60
                            )
                            # Live 20:15: Coca Cola 39w / $530k /
                            # 1.14× / 0.24h sat #3 above MEMES
                            # 4.73×. The 90–99w Coca Cola leftover
                            # needs $540–570k / 2h. ChatGPT leftover
                            # needs 45–69w / $500k+ / 45m. GTA
                            # leftover needs 55–58w / $520–560k.
                            # A 36–39w $520–540k book still under
                            # 1.20 at 15m is leftover tape. BEAST
                            # 41w / $546k stays. Coca Cola 72w
                            # stays. GTA 38w / $954k stays.
                            # SPIDERMAN 45w stays. A 1.20× climb
                            # stays. Do not leftover 1.20×+ or
                            # $540k+ or below $520k or under 36w
                            # or 40w+. Do not leftover under 15m.
                            # Do not leftover BEAST $546k / Coca
                            # Cola 72w / GTA 38w / ChatGPT $500k+
                            # / SPIDERMAN under ChatGPT 45m. Do
                            # not leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 36 <= int(holders) <= 39
                                and 520_000 <= mcap < 540_000
                                and multiple < 1.20
                                and age_s >= 15 * 60
                            )
                            # Live 20:13: CAB 67w / $74k / 1.45× /
                            # 0.16h sat #5 above MEMES 4.73×.
                            # FIGHT leftover needs $70–90k /
                            # <1.10. ACC leftover needs 60–64w
                            # / $65–72k / 1.20–1.40. CALLS
                            # leftover needs $55–65k / <1.05.
                            # A 65–69w $70–80k book still in
                            # 1.40–1.50 at 15m is leftover tape.
                            # FIGHT $77k / 1.10× stays. PONS
                            # $69k stays. CALLS 1.10× stays.
                            # ACC $66k stays. TMB 1.62× stays.
                            # A 1.50× climb stays. Do not leftover
                            # 1.50×+ or 1.40×- or $80k+ or below
                            # $70k or under 65w or 70w+. Do not
                            # leftover under 15m. Do not leftover
                            # FIGHT / PONS $69k / CALLS / ACC /
                            # TMB 1.62× / FROGAS / Shapan. Do
                            # not leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 65 <= int(holders) <= 69
                                and 70_000 <= mcap < 80_000
                                and 1.40 <= multiple < 1.50
                                and age_s >= 15 * 60
                            )
                            # Live 20:13: STONKLESS 80w / $169k /
                            # 1.39× / 0.16h sat #6 above MEMES
                            # 4.73×. WOTF leftover needs $60–75k
                            # / <1.05. CAT leftover needs 175–185w
                            # / $160–175k / <1.35. NOMEME leftover
                            # needs 110–149w / 2h. A 80–84w
                            # $160–175k book still under 1.40 at
                            # 15m is leftover tape. GTA6 83w /
                            # $1.07M stays. WOTF $69k stays.
                            # wrldmeme $81k stays. CAT 195w
                            # stays. A 1.40× climb stays. Do not
                            # leftover 1.40×+ or $175k+ or below
                            # $160k or under 80w or 85w+. Do not
                            # leftover under 15m. Do not leftover
                            # WOTF / CAT 175–185w / GTA6 83w /
                            # wrldmeme $81k / 70–79w $70k+. Do
                            # not leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 80 <= int(holders) <= 84
                                and 160_000 <= mcap < 175_000
                                and multiple < 1.40
                                and age_s >= 15 * 60
                            )
                            # Live 20:24: PUPS 395w / $235k / 1.70× /
                            # 0.29h sat #7 last=$141k. ideal life
                            # leftover needs 260–275w / $80–90k.
                            # bullson leftover needs 250–299w /
                            # $35–55k. The 400w leftover needs
                            # $70–90k / 1.54. A 390–399w
                            # $220–250k 1.60–1.80 dump at 15m
                            # after last print fell under $160k
                            # is leftover tape. A 1.80× climb
                            # stays. A last $160k+ hold stays.
                            # RUNIT 280w / 1.84× stays. FROGAS
                            # stays. BABYAI 700w stays. A 389w
                            # book stays. A 400w book stays.
                            # Do not leftover 1.80×+ or $250k+
                            # or below $220k or under 390w or
                            # 400w+. Do not leftover last $160k+.
                            # Do not leftover under 15m. Do not
                            # leftover RUNIT 280w / FROGAS /
                            # BABYAI 700w / bullson / ideal
                            # life / NEKO 360w. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 390 <= int(holders) <= 399
                                and 220_000 <= mcap < 250_000
                                and 1.60 <= multiple < 1.80
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0) < 160_000
                            )
                            # Live 20:47: Bufo 169w / $57k / 1.59× /
                            # 0.55h sat #6 above MEMES 4.73×. Long
                            # Cat leftover needs $130–150k / <1.32.
                            # The 150–199w / <$80k leftover needs
                            # <1.20. A 165–175w $50–65k book still
                            # 1.55–1.65 at 20m is leftover tape.
                            # Long Cat $137k stays. Shapan 174w /
                            # $129k stays. BIRBANO 1.50× stays.
                            # MPGA 1.69× stays. CHILLROBIN 1.50×
                            # stays. RUNIT 280w stays. A 1.65×
                            # climb stays. A 164w book stays. A
                            # 176w book stays. Do not leftover
                            # 1.65×+ or 1.55×- or $65k+ or below
                            # $50k or under 165w or 176w+. Do not
                            # leftover under 20m. Do not leftover
                            # Long Cat / Shapan 174w / USELESSTROLL
                            # / RUNIT 280w / BIRBANO / MPGA. Do
                            # not leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 165 <= int(holders) <= 175
                                and 50_000 <= mcap < 65_000
                                and 1.55 <= multiple < 1.65
                                and age_s >= 20 * 60
                            )
                            # Live 21:11: PUPS 37w / $161k / 1.18× /
                            # 0.64h sat #4 last=$109k. Coca Cola
                            # leftover needs 36–39w / $520–540k.
                            # STOCKCAT leftover needs 16–19w /
                            # $120–130k. WTT leftover needs 40–44w
                            # / $170–200k. A 36–39w $155–170k
                            # book still under 1.20 at 20m is
                            # leftover tape. A 1.20× climb stays.
                            # Coca Cola 72w stays. BEAST 41w /
                            # $546k stays. WTT $193k stays. A
                            # 35w book stays. A 40w book stays.
                            # Do not leftover 1.20×+ or $170k+
                            # or below $155k or under 36w or
                            # 40w+. Do not leftover under 20m.
                            # Do not leftover Coca Cola 72w /
                            # BEAST $546k / WTT $193k / the
                            # 395w PUPS recap / HOPELESS 26w.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 36 <= int(holders) <= 39
                                and 155_000 <= mcap < 170_000
                                and multiple < 1.20
                                and age_s >= 20 * 60
                            )
                            # Live 21:33: PUMPLIFE 129w / $186k /
                            # 1.52× / 0.14h sat #2. Shapan leftover
                            # needs 60–69w / $170–200k / <1.25.
                            # WTT leftover needs 40–44w /
                            # $170–200k. CHROME leftover needs
                            # 150–159w. A 125–135w $170–200k
                            # book still 1.40–1.60 at 15m is
                            # leftover tape. A 1.60× climb stays.
                            # LASTPONS 132w / $82k stays. WPONS
                            # 133w / $86k stays. Qq币 4.67×
                            # stays. Redbull $935k stays. A
                            # 124w book stays. A 136w book
                            # stays. Do not leftover 1.60×+
                            # or 1.40×- or $200k+ or below
                            # $170k or under 125w or 136w+.
                            # Do not leftover under 15m. Do
                            # not leftover LASTPONS / WPONS
                            # (PONS-named) / Qq币 4.67× /
                            # Redbull 1.10× / RUNIT 280w /
                            # CHROME 155w. Do not leftover
                            # 26–35w. Do not leftover-sort
                            # 2×+.
                            or (
                                holders is not None
                                and 125 <= int(holders) <= 135
                                and 170_000 <= mcap < 200_000
                                and 1.40 <= multiple < 1.60
                                and age_s >= 15 * 60
                            )
                            # Live 21:33: MEMELESS 44w / $146k /
                            # 1.32× / 0.11h sat #1. StonkCat
                            # leftover needs 40–44w / $130–145k.
                            # WTT leftover needs 40–44w /
                            # $170–200k. PUPS leftover needs
                            # 36–39w / $155–170k. A 40–44w
                            # $145–160k book still 1.20–1.40
                            # at 20m is leftover tape. A 1.40×
                            # climb stays. StonkCat $144k
                            # stays. WTT $193k stays. BEAST
                            # $546k stays. A 39w book stays.
                            # A 45w book stays. Do not leftover
                            # 1.40×+ or 1.20×- or $160k+ or
                            # below $145k or under 40w or
                            # 45w+. Do not leftover under 20m.
                            # Do not leftover StonkCat $144k /
                            # WTT $193k / BEAST $546k / Coca
                            # Cola 39w / Corgi $117k. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 44
                                and 145_000 <= mcap < 160_000
                                and 1.20 <= multiple < 1.40
                                and age_s >= 20 * 60
                            )
                            # Live 21:55: WAR 137w / $48k /
                            # 1.06× / 0.15h sat #1 then left
                            # the 200. PUMPLIFE leftover
                            # needs 125–135w / $170–200k.
                            # LASTPONS 132w / $82k stays.
                            # WPONS 133w / $86k stays. A
                            # 135–145w $40–55k book still
                            # under 1.10 at 15m is leftover
                            # tape. A 1.10× climb stays.
                            # A 134w book stays. A 146w
                            # book stays. Do not leftover
                            # 1.10×+ or $55k+ or below $40k
                            # or under 135w or 146w+. Do
                            # not leftover under 15m. Do
                            # not leftover LASTPONS / WPONS
                            # (PONS-named) / PUMPLIFE $186k
                            # / RUNIT 280w / CHROME 155w.
                            # Do not leftover 26–35w. Do
                            # not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 135 <= int(holders) <= 145
                                and 40_000 <= mcap < 55_000
                                and multiple < 1.10
                                and age_s >= 15 * 60
                            )
                            # Live 21:55: Lambo 148w / $49k /
                            # 1.40× / 5.1h sat leftover-cluster
                            # #63. CHROME leftover needs
                            # 150–159w / $130–150k. PUMPLIFE
                            # leftover needs 125–135w /
                            # $170–200k. A 145–155w $45–55k
                            # book still 1.30–1.50 at 2h is
                            # leftover tape. A 1.50× climb
                            # stays. CHROME $141k stays.
                            # RUNIT 280w stays. A 144w book
                            # stays. A 156w book stays. Do
                            # not leftover 1.50×+ or 1.30×-
                            # or $55k+ or below $45k or
                            # under 145w or 156w+. Do not
                            # leftover under 2h. Do not
                            # leftover CHROME $141k / RUNIT
                            # 280w / LASTPONS / WPONS
                            # (PONS-named) / Qq币 4.67×.
                            # Do not leftover 26–35w. Do
                            # not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 145 <= int(holders) <= 155
                                and 45_000 <= mcap < 55_000
                                and 1.30 <= multiple < 1.50
                                and age_s >= 2 * 3600
                            )
                            # Live 22:10: BONZI 47w / $167k / 1.26× /
                            # 0.10h sat #2. ELON leftover needs
                            # 46–49w / $200–225k / <1.05.
                            # DELIVERY leftover is $38–42k.
                            # ROBINRUD leftover is $30–38k.
                            # HUGGY leftover needs 26–45w /
                            # <1.15. A 46–49w $155–180k book
                            # still 1.20–1.40 at 15m is leftover
                            # tape. A 1.40× climb stays.
                            # HOODINU 46w / 1.25× stays. MEME
                            # 45w stays. WTT $193k stays. A
                            # 45w book stays. A 50w book stays.
                            # Do not leftover 1.40×+ or 1.20×-
                            # or $180k+ or below $155k or
                            # under 46w or 50w+. Do not leftover
                            # under 15m. Do not leftover HOODINU
                            # 46w / 1.25× / MEME 45w / WTT $193k
                            # / ELON $213k / LASTPONS / WPONS
                            # (PONS-named) / Qq币 4.67×. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 46 <= int(holders) <= 49
                                and 155_000 <= mcap < 180_000
                                and 1.20 <= multiple < 1.40
                                and age_s >= 15 * 60
                            )
                            # Live 22:10: BEAST 19w / $904k / 1.07× /
                            # 0.12h sat #3. AMC leftover needs
                            # 40–44w / $850–980k. STOCKCAT leftover
                            # needs 16–19w / $120–130k. ChatGPT
                            # leftover needs 45–69w / $500k+ /
                            # prepumped. The $934k / 18m BST107
                            # stay vs the $400k leftover waits
                            # 20m. A 16–19w $850–980k book
                            # still under 1.15 at 20m is leftover
                            # tape. A 1.15× climb stays. FOMO
                            # $1.01M stays. Puggle $901k stays.
                            # GTA 38w / $954k stays. BEAST 41w /
                            # $546k stays. A 15w book stays. A
                            # 20w book stays. Do not leftover
                            # 1.15×+ or $980k+ or below $850k
                            # or under 16w or 20w+. Do not leftover
                            # under 20m. Do not leftover FOMO
                            # $1.01M / Puggle $901k / GTA 38w /
                            # BEAST 41w / $546k / ChatGPT $500k+
                            # / AMC 40–44w / the $934k / 18m
                            # stay / LASTPONS. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 16 <= int(holders) <= 19
                                and 850_000 <= mcap < 980_000
                                and multiple < 1.15
                                and age_s >= 20 * 60
                            )
                            # Live 22:27: BULLAI 111w / $65k / 1.29× /
                            # 0.06h sat leftover_size=0 #1. CATMAX
                            # leftover needs 110–149w / <$80k /
                            # <1.20 / ≥45m. TESTUS leftover needs
                            # 100–109w / $50–65k / <1.32. A 110–
                            # 119w $60–70k book still 1.20–1.40
                            # at 15m is leftover tape. A 1.40×
                            # climb stays. A 109w / $66k book
                            # stays (TESTUS is $50–65k). A 120w
                            # book stays. ROBINCAT $116k stays.
                            # LASTPONS stays. Qq币 4.67× stays.
                            # CATMAX <1.20 stays. Do not leftover
                            # 1.40×+ or 1.20×- or $70k+ or below
                            # $60k or under 110w or 120w+. Do not
                            # leftover under 15m. Do not leftover
                            # TESTUS $59k / ROBINCAT $116k /
                            # LASTPONS / WPONS (PONS-named) /
                            # Qq币 4.67× / CATMAX <1.20. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 110 <= int(holders) <= 119
                                and 60_000 <= mcap < 70_000
                                and 1.20 <= multiple < 1.40
                                and age_s >= 15 * 60
                            )
                            # Live 22:27: apecat 205w / $56k / 1.51× /
                            # 0.56h sat leftover_size=0 #4 (still #3
                            # at 0.62h). ironmike leftover needs
                            # 200w+ / <$80k / <1.20. BIRBANO leftover
                            # needs 150w+ / <$80k / <1.55 / ≥5.5h.
                            # PSA leftover needs 215–225w / $63–72k.
                            # A 200–215w $50–65k book still 1.40–
                            # 1.60 at 30m is leftover tape. A 1.60×
                            # climb stays. PAIRLESS $97k stays.
                            # ironmike <1.20 stays. NEKO 360w stays.
                            # PSA $66k stays. WAR 1.34× stays.
                            # LASTPONS stays. Do not leftover
                            # 1.60×+ or 1.40×- or $65k+ or below
                            # $50k or under 200w or 216w+. Do not
                            # leftover under 30m. Do not leftover
                            # PAIRLESS $97k / ironmike <1.20 /
                            # NEKO 360w / PSA $66k / WAR 1.34× /
                            # LASTPONS. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+. Do not
                            # widen ironmike 200w+ / <1.20.
                            or (
                                holders is not None
                                and 200 <= int(holders) <= 215
                                and 50_000 <= mcap < 65_000
                                and 1.40 <= multiple < 1.60
                                and age_s >= 30 * 60
                            )
                            # Live 22:49: Murad 98w / $283k / 1.00× /
                            # 0.04h sat leftover_size=0 #1. Coca Cola
                            # leftover needs 90–99w / $540–570k.
                            # MEME leftover needs 90–99w / $100–
                            # 130k / 1.10–1.20. THEINVESTOR leftover
                            # is RH 50–59w / $250–290k. A 95–102w
                            # $260–300k book still under 1.10 at
                            # 15m is leftover tape. A 1.10× climb
                            # stays. Coca Cola $554k stays. MEME
                            # 95w / $128k stays. rusty 100w stays.
                            # TESTUS $59k stays. Do not leftover
                            # 1.10×+ or $300k+ or below $260k or
                            # under 95w or 103w+. Do not leftover
                            # under 15m. Do not leftover Coca Cola
                            # $554k / MEME 95w / rusty 100w /
                            # TESTUS $59k / LASTPONS / Qq币 /
                            # WAR 1.34×. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 95 <= int(holders) <= 102
                                and 260_000 <= mcap < 300_000
                                and multiple < 1.10
                                and age_s >= 15 * 60
                            )
                            # Live 23:12: SPX 119w / $168k / 1.00× /
                            # 0.39h sat leftover_size=0 #3 last=$57k.
                            # BULLAI leftover needs 110–119w / $60–
                            # 70k / 1.20–1.40. CATMAX leftover needs
                            # 110–149w / <$80k / <1.20 / ≥45m.
                            # NOMEME leftover needs 110–149w / $170–
                            # 200k / <1.30 / ≥2h. A 115–125w $155–
                            # 169k book still under 1.10 at 20m is
                            # leftover tape. A 1.10× climb stays.
                            # NOMEME $176k stays. MarsCoin $151k
                            # stays. BULLAI $65k stays. Do not
                            # leftover 1.10×+ or $169k+ or below
                            # $155k or under 115w or 126w+. Do not
                            # leftover under 20m. Do not leftover
                            # NOMEME $176k / MarsCoin $151k /
                            # BULLAI $65k / LASTPONS / Qq币 /
                            # WAR 1.34× / Murad 2.81×. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 115 <= int(holders) <= 125
                                and 155_000 <= mcap < 169_000
                                and multiple < 1.10
                                and age_s >= 20 * 60
                            )
                            # Live 23:12: ANSEM 84w / $126k / 1.16× /
                            # 0.45h sat leftover_size=0 #4. STONKLESS
                            # leftover needs 80–84w / $160–175k /
                            # <1.40. WOTF leftover needs 80–89w /
                            # $60–75k / <1.05. GTA6 83w / $1.07M
                            # stays. A 80–87w $115–140k book still
                            # 1.10–1.25 at 20m is leftover tape. A
                            # 1.25× climb stays. STONKLESS $169k
                            # stays. wrldmeme $81k stays. GTA6
                            # stays. Do not leftover 1.25×+ or
                            # 1.10×- or $140k+ or below $115k or
                            # under 80w or 88w+. Do not leftover
                            # under 20m. Do not leftover STONKLESS
                            # $169k / WOTF $69k / GTA6 83w /
                            # wrldmeme $81k / LASTPONS / Qq币.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 80 <= int(holders) <= 87
                                and 115_000 <= mcap < 140_000
                                and 1.10 <= multiple < 1.25
                                and age_s >= 20 * 60
                            )
                            # Live 23:33: POKEMON 139w / $901k /
                            # 1.05× / 0.21h sat leftover_size=0 #1.
                            # WAR leftover needs 135–145w / $40–
                            # 55k / <1.10. UPTOOMUCH leftover
                            # needs 150–199w / $800k–$1.0M /
                            # <1.05. AMC leftover needs 40–44w /
                            # $850–980k. BEAST leftover needs
                            # 16–19w / $850–980k. A 135–145w
                            # $850–980k book still under 1.08 at
                            # 15m is leftover tape. A 1.08× climb
                            # stays. FOMO $1.01M stays. Puggle
                            # $901k stays. GTA 38w stays. Do not
                            # leftover 1.08×+ or $980k+ or below
                            # $850k or under 135w or 146w+. Do
                            # not leftover under 15m. Do not
                            # leftover FOMO $1.01M / Puggle
                            # $901k / GTA 38w / ChatGPT $500k+
                            # / Coca Cola $554k / LASTPONS /
                            # Qq币 / WAR 1.34× / Murad 3.51×.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 135 <= int(holders) <= 145
                                and 850_000 <= mcap < 980_000
                                and multiple < 1.08
                                and age_s >= 15 * 60
                            )
                            # Live 23:33: CRC 208w / $215k /
                            # 1.30× / 0.45h sat leftover_size=0
                            # #2 last=$80k after a $165k / 1.00×
                            # print. apecat leftover needs 200–
                            # 215w / $50–65k / 1.40–1.60.
                            # ironmike leftover needs 200w+ /
                            # <$80k / <1.20. PSA leftover needs
                            # 215–225w / $63–72k. BULLISHCAT
                            # leftover needs 230–249w / $170–
                            # 190k. A 205–215w $190–230k book
                            # still 1.20–1.40 at 20m is leftover
                            # tape. A 1.40× climb stays. apecat
                            # $56k stays. PAIRLESS $97k stays.
                            # Do not leftover 1.40×+ or 1.20×-
                            # or $230k+ or below $190k or under
                            # 205w or 216w+. Do not leftover
                            # under 20m. Do not leftover apecat
                            # $56k / ironmike <1.20 / PSA $66k
                            # / PAIRLESS $97k / LASTPONS / Qq币
                            # / the RH CRC 3.19× / Sol CRCL
                            # 1.23×. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+. Do not
                            # widen ironmike 200w+ / <1.20.
                            or (
                                holders is not None
                                and 205 <= int(holders) <= 215
                                and 190_000 <= mcap < 230_000
                                and 1.20 <= multiple < 1.40
                                and age_s >= 20 * 60
                            )
                            # Live 01:08: TROBIN 128w / $82k / 1.70× /
                            # 1.4h sat leftover_size=0 last=$2k.
                            # PUMPLIFE leftover needs $170–200k /
                            # 1.40–1.60. CATMAX leftover needs
                            # <$80k / <1.20. LASTPONS 132w / $82k
                            # / 1.50× stays. WPONS 133w / $86k /
                            # 1.58× stays. A 125–135w $75–90k
                            # book still 1.60–1.80 at 60m is
                            # leftover tape. A 1.80× climb stays.
                            # 124w stays. 136w stays. Do not
                            # leftover 1.80×+ or 1.60×- or $90k+
                            # or below $75k or under 125w or
                            # 136w+. Do not leftover under 60m.
                            # Do not leftover LASTPONS / WPONS
                            # (PONS-named) / PUMPLIFE / CATMAX /
                            # Qq币 / Claude / BEAST 1.12× /
                            # POKEMON 1.13×. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 125 <= int(holders) <= 135
                                and 75_000 <= mcap < 90_000
                                and 1.60 <= multiple < 1.80
                                and age_s >= 60 * 60
                            )
                            # Live 01:29: VOF 41w / $104k / 1.74× /
                            # 0.42h sat leftover_size=0 last=$104k.
                            # StonkCat leftover needs $130–145k /
                            # <1.50. WTT leftover needs $170–200k.
                            # MEMELESS leftover needs $145–160k /
                            # 1.20–1.40. HUGGY leftover needs
                            # <1.15. Corgi $117k stays. A 40–44w
                            # $95–115k book still 1.65–1.80 at
                            # 20m is leftover tape. A 1.80× climb
                            # stays. 39w stays. 45w stays. Do not
                            # leftover 1.80×+ or 1.65×- or $115k+
                            # or below $95k or under 40w or 45w+.
                            # Do not leftover under 20m. Do not
                            # leftover Corgi $117k / StonkCat /
                            # WTT / MEMELESS / HUGGY / AMC /
                            # GTA 38w / Coca Cola 39w / MEME 45w
                            # / HOODINU / BEAST $546k. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 44
                                and 95_000 <= mcap < 115_000
                                and 1.65 <= multiple < 1.80
                                and age_s >= 20 * 60
                            )
                            # Live 01:50: OIL 186w / $110k / 1.00× /
                            # 0.37h sat leftover_size=0 #4 last=$74k.
                            # USELESSTROLL leftover needs $75–90k /
                            # <1.50 / ≥60m. PAIRLESS $97k stays
                            # (below $100k). CRCL $63k stays. ironmike
                            # leftover needs 200w+. A 180–199w
                            # $100–125k book still under 1.05 at
                            # 15m is leftover tape. A 1.05× climb
                            # stays. 179w stays. 200w stays. Do not
                            # leftover 1.05×+ or $125k+ or below
                            # $100k or under 180w or 200w+. Do not
                            # leftover under 15m. Do not leftover
                            # PAIRLESS $97k / USELESSTROLL / CRCL /
                            # ironmike / PSA $66k / Claude / BEAST
                            # 1.65× / POKEMON 1.13× / VOF 2.30× /
                            # PONANSEM / Redbull. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 180 <= int(holders) <= 199
                                and 100_000 <= mcap < 125_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 01:50: Hbros 141w / $73k / 1.62× /
                            # 0.14h sat leftover_size=0 #2 last=$71k.
                            # WAR leftover needs $40–55k / <1.10.
                            # POKEMON leftover needs $850–980k.
                            # Lambo leftover needs $45–55k /
                            # 1.30–1.50. CATMAX leftover needs
                            # <1.20. TROBIN leftover needs 125–135w
                            # / $75–90k. A 138–145w $65–80k book
                            # still 1.55–1.70 at 15m is leftover
                            # tape. A 1.70× climb stays. 137w stays.
                            # 146w stays. $64k stays. Do not leftover
                            # 1.70×+ or 1.55×- or $80k+ or below
                            # $65k or under 138w or 146w+. Do not
                            # leftover under 15m. Do not leftover
                            # WAR / POKEMON leftover / Lambo /
                            # CATMAX / TROBIN / CHROME / Long Cat /
                            # LASTPONS / Qq币 / Claude / BEAST.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 138 <= int(holders) <= 145
                                and 65_000 <= mcap < 80_000
                                and 1.55 <= multiple < 1.70
                                and age_s >= 15 * 60
                            )
                            # Live 02:13: HBros 63w / $127k / 1.16× /
                            # 0.15h sat leftover_size=0 #2 last=$103k.
                            # A different 141w Hbros leftover needs
                            # $65–80k / 1.55–1.70. APE leftover needs
                            # $80–150k / <1.05. ACC leftover needs
                            # $65–72k / 1.20–1.40. jerk leftover
                            # needs $185–200k / <1.05. Shapan leftover
                            # needs $170–200k / <1.25. CALLS leftover
                            # needs $55–65k / <1.05. A 60–64w
                            # $115–140k book still 1.10–1.25 at
                            # 15m is leftover tape. A 1.25× climb
                            # stays. 59w stays. 65w stays. $114k
                            # stays. UNSTABLE $163k stays. Do not
                            # leftover 1.25×+ or 1.10×- or $140k+
                            # or below $115k or under 60w or 65w+.
                            # Do not leftover under 15m. Do not
                            # leftover APE / ACC / jerk / Shapan /
                            # CALLS / FIGHT / PayPal / PONS $69k /
                            # UNSTABLE $163k / the 141w Hbros /
                            # CHARTX (ironmike) / Claude / BEAST /
                            # POKEMON / VOF 2.30× / PONANSEM /
                            # Redbull. Do not leftover 26–35w. Do
                            # not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 64
                                and 115_000 <= mcap < 140_000
                                and 1.10 <= multiple < 1.25
                                and age_s >= 15 * 60
                            )
                            # Live 02:13: APT 105w / $74k / 1.43× /
                            # 0.53h sat leftover_size=0 #3 last=$27k.
                            # TESTUS leftover needs $50–65k / <1.32.
                            # Kilo leftover needs $50–55k / <1.65.
                            # DOGECAT leftover needs $155–180k /
                            # <1.38. ROBINCAT leftover needs
                            # $100–150k / <1.10. BULLAI leftover
                            # needs 110–119w. A 100–109w $70–80k
                            # book still 1.35–1.50 at 15m is leftover
                            # tape. A 1.50× climb stays. 99w stays.
                            # 110w stays. $69k stays. TESTUS $59k
                            # stays. Do not leftover 1.50×+ or
                            # 1.35×- or $80k+ or below $70k or
                            # under 100w or 110w+. Do not leftover
                            # under 15m. Do not leftover TESTUS /
                            # Kilo / DOGECAT / ROBINCAT / BULLAI /
                            # Claude / BEAST / POKEMON / VOF 2.30×
                            # / PONANSEM / Redbull / CHARTX
                            # (ironmike). Do not leftover 26–35w.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 100 <= int(holders) <= 109
                                and 70_000 <= mcap < 80_000
                                and 1.35 <= multiple < 1.50
                                and age_s >= 15 * 60
                            )
                            # Live 03:30: GoMeme 222w / $159k / 1.55× /
                            # 1.14h sat leftover_size=0 #6 last=$149k.
                            # apecat leftover needs 200–215w / $50–65k
                            # / 1.40–1.60. BULLISHCAT leftover needs
                            # 230–249w / $170–190k / <1.50. PSA leftover
                            # needs 215–225w / $63–72k. Corgi $117k
                            # stays. StonkCat $144k stays. A 220–229w
                            # $150–165k book still 1.45–1.60 at 60m is
                            # leftover tape. A 1.60× climb stays. 219w
                            # stays. 230w stays. $149k stays. Do not
                            # leftover 1.60×+ or 1.45×- or $165k+ or
                            # below $150k or under 220w or 230w+. Do
                            # not leftover under 60m. Do not leftover
                            # apecat / BULLISHCAT / PSA $66k / Corgi
                            # $117k / StonkCat $144k / PAIRLESS $97k
                            # / ironmike / Claude / BEAST / POKEMON /
                            # VOF 2.30× / ANGRYFROG 2.61× / DRAGGO /
                            # BIKEANSON $94k. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 220 <= int(holders) <= 229
                                and 150_000 <= mcap < 165_000
                                and 1.45 <= multiple < 1.60
                                and age_s >= 60 * 60
                            )
                            # Live 03:30: memestonk 768w / $366k / 1.63× /
                            # 0.50h sat leftover_size=0 #2 last=$366k.
                            # The last=0 / $225k / under 15m print was
                            # FOMO last=0 / 750w+ leftover-in-waiting —
                            # do not leftover that print. BABYAI leftover
                            # needs under 700w / $280k+ / 1.10×+. PUPS
                            # leftover needs 390–399w / $220–250k.
                            # A 750–799w $340–390k book still 1.50–1.70
                            # at 25m is leftover tape. A 1.70× climb
                            # stays. 749w stays. 800w stays. $339k
                            # stays. FOMO last=0 stays. Do not leftover
                            # 1.70×+ or 1.50×- or $390k+ or below $340k
                            # or under 750w or 800w+. Do not leftover
                            # under 25m. Do not leftover FOMO last=0 /
                            # BABYAI / PUPS / the last=0 / $225k /
                            # under 15m memestonk print / Claude /
                            # BEAST / POKEMON / VOF 2.30× / ANGRYFROG
                            # 2.61× / BIKEANSON $94k. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 750 <= int(holders) <= 799
                                and 340_000 <= mcap < 390_000
                                and 1.50 <= multiple < 1.70
                                and age_s >= 25 * 60
                            )
                            # Live 04:07: SHROOM 159w / $136k / 1.12× /
                            # 7m sat leftover_size=0 #1 last=$104k.
                            # CHROME leftover needs 150–159w /
                            # $130–150k / <1.10. StonkCat leftover
                            # needs 40–44w / $130–145k. Long Cat
                            # leftover needs 160–169w / $130–150k.
                            # A 150–159w $125–145k book still
                            # 1.10×+–1.20 at 15m is leftover tape.
                            # CHROME $141k <1.10 stays leftovered
                            # on its own band. The CHROME 1.10×
                            # climb stay holds. StonkCat $144k / 44w
                            # stays. Long Cat 160w stays. Corgi
                            # $117k stays. A 1.20× climb stays.
                            # The 3.00× SHROOM stays (leftover-sort
                            # 2×+). Do not leftover 1.20×+ or
                            # 1.10×- or $145k+ or below $125k or
                            # under 150w or 160w+. Do not leftover
                            # under 15m. Do not leftover CHROME /
                            # StonkCat / Long Cat / Corgi $117k /
                            # SHROOM 3.00× / Claude / BEAST /
                            # POKEMON / VOF 2.30× / ANGRYFROG
                            # 2.61× / DRAGGO / BIKEANSON $94k.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 150 <= int(holders) <= 159
                                and 125_000 <= mcap < 145_000
                                and 1.10 < multiple < 1.20
                                and age_s >= 15 * 60
                            )
                            # Live 04:26: PFPP 49w / $52k / 1.03× /
                            # 4m sat leftover_size=0 #1 last=$44k
                            # liq=$16k. RH PHOENIX leftover is
                            # 40–49w / $45–55k / <1.10 — Sol
                            # 40–44w leftovers are StonkCat /
                            # MEMELESS / WTT. ELON leftover needs
                            # 46–49w / $200–225k. BONZI leftover
                            # needs 46–49w / $155–180k / 1.20–1.40.
                            # APE leftover needs $80–150k / <1.05.
                            # A 46–49w $45–55k book still under
                            # 1.05 at 15m is leftover tape.
                            # The Gao 1.05× climb stay holds.
                            # HOODINU 46w / 1.25× stays. MEME
                            # 45w stays. BONZI $167k stays.
                            # ELON $213k stays. APE $80k stays.
                            # OpenAI 51w stays. A 1.05× climb
                            # stays. Do not leftover 1.05×+ or
                            # $55k+ or below $45k or under 46w
                            # or 50w+. Do not leftover under 15m.
                            # Do not leftover HOODINU / MEME 45w /
                            # BONZI / ELON / APE / OpenAI 51w /
                            # itamae 2.94× / GTA 6 Coin / Claude /
                            # BEAST / POKEMON / VOF 2.30× /
                            # ANGRYFROG 2.61× / DRAGGO / BIKEANSON
                            # $94k / SHROOM recap. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            # Do not recap RH PHOENIX.
                            or (
                                holders is not None
                                and 46 <= int(holders) <= 49
                                and 45_000 <= mcap < 55_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 04:41: Pairz 241w / $73k / 1.47× /
                            # 26m sat leftover_size=0 #2 last=$73k
                            # liq=$21k. BULLISHCAT leftover is
                            # 230–249w / $170–190k / <1.50 / 45m.
                            # FROGAS leftover needs 250w+ /
                            # $60–80k / 2h. PSA leftover is
                            # 215–225w / $63–72k. TRIPLEP leftover
                            # needs 400w+ / $70–90k. ideal life
                            # leftover needs 260–275w / $80–90k.
                            # ironmike leftover needs <1.20.
                            # PAIRLESS $97k stays. BIKEANSON $94k
                            # stays. A 235–249w $68–80k book still
                            # between 1.40 and 1.50 at 15m is
                            # leftover tape. A 1.50× climb stays.
                            # 234w stays. 250w stays. $67k stays.
                            # Under 15m stays. Do not leftover
                            # 1.50×+ or 1.40×- or $80k+ or below
                            # $68k or under 235w or 250w+. Do not
                            # leftover under 15m. Do not leftover
                            # BULLISHCAT / FROGAS / PSA / TRIPLEP
                            # / ideal life / PAIRLESS $97k /
                            # BIKEANSON $94k / ironmike <1.20 /
                            # itamae 2.94× / PFPP recap / SHROOM
                            # recap / Claude / BEAST / ANGRYFROG
                            # 2.61× / DRAGGO / VOF 2.30×. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 235 <= int(holders) <= 249
                                and 68_000 <= mcap < 80_000
                                and 1.40 <= multiple < 1.50
                                and age_s >= 15 * 60
                            )
                            # Live 05:00: ChudVlad 148w / $49k / 1.21× /
                            # 8m sat leftover_size=0 #1 last=$49k
                            # liq=$17k. CATMAX leftover is 110–149w
                            # / <$80k / <1.20 / 45m. Lambo leftover
                            # is 145–155w / $45–55k / 1.30–1.50 / 2h.
                            # WAR leftover is 135–145w / $40–55k /
                            # <1.10. CHROME leftover needs 150w+ /
                            # $130–150k. PFPP leftover is 46–49w.
                            # Gao leftover needs $40–45k / <1.05.
                            # A 146–149w $45–55k book still between
                            # 1.20 and 1.30 at 15m is leftover tape.
                            # The CATMAX <1.20 stay holds. A 1.30×
                            # climb stays. 145w stays. 150w stays.
                            # $44k stays. Under 15m stays. Do not
                            # leftover 1.30×+ or 1.20×- or $55k+
                            # or below $45k or under 146w or 150w+.
                            # Do not leftover under 15m. Do not
                            # leftover CATMAX <1.20 / Lambo 1.30–
                            # 1.50 / WAR / CHROME / PFPP / Gao /
                            # PAIRLESS $97k / BIKEANSON $94k /
                            # Pairz recap / itamae 2.94× / Claude /
                            # BEAST / ANGRYFROG 2.61× / DRAGGO /
                            # VOF 2.30×. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+. Do not
                            # leftover last=0. Do not cut CATMAX
                            # 45m.
                            or (
                                holders is not None
                                and 146 <= int(holders) <= 149
                                and 45_000 <= mcap < 55_000
                                and 1.20 <= multiple < 1.30
                                and age_s >= 15 * 60
                            )
                            # Live 05:20: ANSEM 279w / $56k / 1.25× /
                            # 14m sat leftover_size=0 #1 last=$56k
                            # liq=$18k. ironmike leftover is 200w+
                            # / <$80k / <1.20 / 10m. bullson leftover
                            # is 250–299w / $35–55k / <1.35 / 60m.
                            # FROGAS leftover needs 250w+ / $60–80k
                            # / 2h. ideal life leftover is 260–275w
                            # / $80–90k. The 80–87w ANSEM leftover
                            # needs $115–140k. PAIRLESS $97k stays.
                            # BIKEANSON $94k stays. A 276–289w
                            # $55–65k book still between 1.20 and
                            # 1.35 at 15m is leftover tape. The
                            # ironmike <1.20 stay holds. A 1.35×
                            # climb stays. $54k stays. 275w stays.
                            # 290w stays. Under 15m stays. Do not
                            # leftover 1.35×+ or 1.20×- or $65k+
                            # or below $55k or under 276w or 290w+.
                            # Do not leftover under 15m. Do not
                            # leftover ironmike <1.20 / bullson
                            # $35–55k / FROGAS / ideal life / the
                            # 80–87w ANSEM / PAIRLESS $97k /
                            # BIKEANSON $94k / Pairz recap /
                            # ChudVlad recap / itamae 2.94× /
                            # POMP 2.18× / last=0 / CATSTRO last=0.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+. Do not cut the
                            # ironmike 10m clock.
                            or (
                                holders is not None
                                and 276 <= int(holders) <= 289
                                and 55_000 <= mcap < 65_000
                                and 1.20 <= multiple < 1.35
                                and age_s >= 15 * 60
                            )
                            # Live 06:00: PINU 62w / $63k / 1.33× /
                            # 4m sat leftover_size=0 #1 last=$63k
                            # liq=$20k. USDP leftover needs $40–55k
                            # / <1.05. CALLS leftover needs $55–65k
                            # / <1.05. ACC leftover needs $65–72k /
                            # 1.20–1.40 / 20m. HBros leftover needs
                            # $115–140k. APE leftover needs $80k.
                            # A 60–64w $55–65k book still 1.30–1.40
                            # at 15m is leftover tape. A 1.40×
                            # climb stays. $54k stays. 59w stays.
                            # 65w stays. CALLS <1.05 stays on its
                            # own band. ACC $65k stays on its own
                            # band. Do not leftover 1.40×+ or
                            # 1.30×- or $65k+ or below $55k or
                            # under 60w or 65w+. Do not leftover
                            # under 15m. Do not leftover USDP /
                            # CALLS / ACC / HBros / APE $80k /
                            # PONS $69k / IBRL (ironmike) / ANSEM
                            # recap / Pairz recap / itamae 2.94×.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+. Do not cut the
                            # ironmike 10m clock.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 64
                                and 55_000 <= mcap < 65_000
                                and 1.30 <= multiple < 1.40
                                and age_s >= 15 * 60
                            )
                            # Live 06:00: Juror 53w / $73k / 1.01× /
                            # 4m sat leftover_size=0 #2 last=$73k
                            # liq=$21k. APE leftover needs $80–150k
                            # / <1.05. ponstrump leftover needs
                            # $50–60k / <1.05. PEEP leftover needs
                            # $120–150k / <1.20. ZAPPI $69k stays.
                            # PONS $69k stays. A 50–59w $70–80k
                            # flat at 15m is leftover tape. A
                            # 1.05× climb stays. $69k stays. 49w
                            # stays. 60w stays. APE $80k stays.
                            # Do not leftover 1.05×+ or $80k+ or
                            # below $70k or under 50w or 60w+.
                            # Do not leftover under 15m. Do not
                            # leftover APE / ponstrump / PEEP /
                            # ZAPPI $69k / PONS $69k / OpenAI /
                            # itamae 2.94× / ANSEM recap / Pairz
                            # recap / IBRL (ironmike). Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+. Do not raise PONS score.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 70_000 <= mcap < 80_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 06:40: TRENCHER 152w / $38k /
                            # 1.43× / 17m sat leftover_size=0 #2
                            # last=$38k liq=$15k max=$65k. reclaim
                            # leftover needs 150–199w / <$80k /
                            # <1.20. CHROME leftover needs $130–
                            # 150k / <1.10. SHROOM leftover needs
                            # $125–145k / 1.10–1.20. Lambo leftover
                            # needs $45–55k / 1.30–1.50 / 2h. A
                            # 150–159w $60–70k book still 1.40–1.50
                            # at 15m is leftover tape. A 1.50×
                            # climb stays. $59k stays. $70k stays.
                            # 149w stays. 160w stays. Do not leftover
                            # 1.50×+ or 1.40×- or $70k+ or below
                            # $60k or under 150w or 160w+. Do not
                            # leftover under 15m. Do not leftover
                            # reclaim <1.20 / CHROME / SHROOM /
                            # Lambo / WAR / PAIRLESS $97k / PINU
                            # recap / ANSEM recap / Pairz recap /
                            # restless 2.01×. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 150 <= int(holders) <= 159
                                and 60_000 <= mcap < 70_000
                                and 1.40 <= multiple < 1.50
                                and age_s >= 15 * 60
                            )
                            # Live 06:40: Sol CATSTRO 53w / $132k /
                            # 1.22× / 33m sat leftover_size=0 #5
                            # last=$132k liq=$29k max=$136k. APE
                            # leftover needs $80–150k / <1.05. PEEP
                            # leftover needs $120–150k / <1.20.
                            # Juror leftover needs $70–80k / <1.05.
                            # Ovary $143k / 1.30× stays. The PEEP
                            # 1.20× climb stay holds. HBros 59w /
                            # $127k stays. A 50–56w $130–140k book
                            # still 1.20–1.25 at 15m is leftover
                            # tape. A 1.25× climb stays. $129k
                            # stays. $140k stays. 49w stays. 57w
                            # stays. Do not leftover 1.25×+ or
                            # 1.20×- or $140k+ or below $130k or
                            # under 50w or 57w+. Do not leftover
                            # under 15m. Do not leftover APE /
                            # PEEP / Juror recap / HBros / Ovary
                            # $143k / OpenAI / itamae 2.94× /
                            # PINU recap / restless 2.01× / RH
                            # CATSTRO recap. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+.
                            # Do not leftover the $2.47M RH
                            # CATSTRO mint.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 56
                                and 130_000 <= mcap < 140_000
                                and 1.20 < multiple < 1.25
                                and age_s >= 15 * 60
                            )
                            # Live 07:00: FOMO 55w / $72k / 1.31× /
                            # 7m sat leftover_size=0 #1 last=$72k
                            # liq=$21k max=$74k. Juror leftover
                            # needs $70–80k / <1.05. APE leftover
                            # needs $80–150k / <1.05. ACC leftover
                            # needs 60–64w / $65–72k / 1.20–1.40.
                            # PINU leftover needs 60–64w. FIGHT
                            # 61w / $77k stays. ZAPPI $69k stays.
                            # PONS $69k stays. A 50–59w $70–80k
                            # book still 1.30–1.40 at 15m is
                            # leftover tape. A 1.40× climb stays.
                            # $69k stays. $80k stays. 49w stays.
                            # 60w stays. 1.30×- stays. Do not
                            # leftover 1.40×+ or 1.30×- or $80k+
                            # or below $70k or under 50w or 60w+.
                            # Do not leftover under 15m. Do not
                            # leftover Juror recap / APE / ACC /
                            # PINU recap / FIGHT 61w / ZAPPI $69k
                            # / PONS $69k / OpenAI / itamae 2.94×
                            # / FOMO last=0 / restless 2.01× /
                            # IBRL 2.04× / StonkDoge 19w / LEGO
                            # under ChatGPT 45m / ponschan. Do
                            # not leftover 26–35w. Do not leftover-
                            # sort 2×+. Do not raise PONS score.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 70_000 <= mcap < 80_000
                                and 1.30 < multiple < 1.40
                                and age_s >= 15 * 60
                            )
                            # Live 08:00: GTA 6 Coin 21w / $957k /
                            # 1.13× / 15m sat leftover_size=0 #3
                            # last=$957k liq=$81k max=$957k. BEAST
                            # leftover needs 16–19w / $850–980k /
                            # <1.15 / ≥20m. A 20w book stays
                            # (FAT74). A 25w BEAST book stays. AMC
                            # leftover needs 40–44w / $850–980k.
                            # POKEMON leftover needs 135–145w /
                            # $850–980k. The 55–58w GTA leftover
                            # needs $520–560k. GTA 38w / $954k
                            # stays. Puggle 464w / $901k stays.
                            # FOMO $1.01M stays. STOCKCAT leftover
                            # needs $120–130k. memestock leftover
                            # needs $300–360k. The 9w GTA 1.16×
                            # print is under 21w and 1.16×. A
                            # 21–24w $900–980k book still under
                            # 1.15 at 15m is leftover tape. A
                            # 1.15× climb stays. $899k stays.
                            # $980k stays. 20w stays. 25w stays.
                            # Do not leftover 1.15×+ or $980k+
                            # or below $900k or under 21w or
                            # 25w+. Do not leftover under 15m.
                            # Do not leftover BEAST 16–19w /
                            # FAT74 20w / BEAST 25w / AMC 40–44w
                            # / GTA 38w / Puggle 464w / FOMO
                            # $1.01M / STOCKCAT / ChatGPT $500k+
                            # / POKEMON / the 9w GTA 1.16× / the
                            # 55–58w GTA leftover / 26–35w. Do
                            # not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 21 <= int(holders) <= 24
                                and 900_000 <= mcap < 980_000
                                and multiple < 1.15
                                and age_s >= 15 * 60
                            )
                            # Live 09:00: BRAINDEAD 407w / $183k /
                            # 1.12× / 14m sat leftover_size=0 #2
                            # last=$170k liq=$33k max=$183k.
                            # TRIPLEP leftover needs 400w+ / $70–
                            # 90k. PlutoCoin leftover needs $90–
                            # 120k. STOCKFATHER leftover needs
                            # $300–350k. MEME leftover needs
                            # $200k+ / prepumped. NOMEME leftover
                            # needs 110–149w / $170–200k. BABYAI
                            # leftover needs 700w+. NEKO 360w
                            # stays. Puggle $901k stays. A 400–
                            # 419w $170–195k book still under
                            # 1.15 at 15m is leftover tape. A
                            # 1.15× climb stays. $169k stays.
                            # $195k stays. 399w stays. 420w
                            # stays. Do not leftover 1.15×+ or
                            # $195k+ or below $170k or under
                            # 400w or 420w+. Do not leftover
                            # under 15m. Do not leftover TRIPLEP
                            # / PlutoCoin / STOCKFATHER / MEME
                            # $200k+ / NOMEME / BABYAI 700w /
                            # NEKO 360w / Puggle $901k / CALLS
                            # leftover-in-waiting / AI 64w /
                            # PONSJAK / RWA 2.06× / GTA $1.02M
                            # climb / 26–35w. Do not leftover-
                            # sort 2×+. Do not add RH 2h / 0.99
                            # (BROTHER / BRAIN).
                            or (
                                holders is not None
                                and 400 <= int(holders) <= 419
                                and 170_000 <= mcap < 195_000
                                and multiple < 1.15
                                and age_s >= 15 * 60
                            )
                            # Live 09:40: VOF 115w / $967k / 1.09× /
                            # 12m sat leftover_size=0 #2 last=$967k
                            # liq=$96k max=$967k mint ePCFv…pump.
                            # Different mint from the 2.30× VOF.
                            # BEAST leftover needs 16–19w /
                            # $850–980k. GTA leftover needs 21–
                            # 24w / $900–980k. AMC leftover needs
                            # 40–44w. POKEMON leftover needs 135–
                            # 145w / <1.08. BULLAI leftover needs
                            # 110–119w / $60–70k. CATMAX leftover
                            # needs <$80k. NOMEME leftover needs
                            # $170–200k. FAT74 20w stays. GTA 38w
                            # stays. Puggle 464w stays. A 110–
                            # 119w $900–980k book still under
                            # 1.15 at 15m is leftover tape. A
                            # 1.15× climb stays. $899k stays.
                            # $980k stays. 109w stays. 120w
                            # stays. Do not leftover 1.15×+ or
                            # $980k+ or below $900k or under
                            # 110w or 120w+. Do not leftover
                            # under 15m. Do not leftover BEAST
                            # 16–19w / FAT74 20w / GTA 21–24w /
                            # GTA 38w / AMC 40–44w / POKEMON /
                            # Puggle 464w / the live GTA 1.198×
                            # / $1.02M climb / VOF 2.30× /
                            # SLINK under ChatGPT 45m /
                            # Percolator under ChatGPT 45m /
                            # 26–35w. Do not leftover-sort 2×+.
                            # Do not leftover ASSDAQ leftover
                            # recap / the live ASSDAQ 1.30×
                            # climb.
                            or (
                                holders is not None
                                and 110 <= int(holders) <= 119
                                and 900_000 <= mcap < 980_000
                                and multiple < 1.15
                                and age_s >= 15 * 60
                            )
                            # Live 10:00: GTA 6 Coin 44w / $973k /
                            # 1.13× / 30m sat leftover_size=0 #4
                            # last=$973k liq=$82k max=$973k mint
                            # EpH36…pump. Different mint from the
                            # 21w $1.02M / 1.198× climb. AMC
                            # leftover needs 40–44w / $850–980k /
                            # <1.10 / 30m. GTA leftover needs 21–
                            # 24w / $900–980k / <1.15. BEAST
                            # leftover needs 16–19w. VOF leftover
                            # needs 110–119w. FAT74 20w stays.
                            # GTA 38w stays. Puggle 464w stays.
                            # A 40–44w $900–980k book still 1.10–
                            # 1.15 at 30m is leftover tape. A
                            # 1.15× climb stays. A 1.10× climb
                            # stays (AMC). $899k stays. $980k
                            # stays. 39w stays. 45w stays. Do
                            # not leftover 1.15×+ or 1.10×- or
                            # $980k+ or below $900k or under 40w
                            # or 45w+. Do not leftover under 30m.
                            # Do not leftover AMC leftover recap
                            # / GTA 21–24w leftover recap / the
                            # live GTA 1.198× / $1.02M climb /
                            # BEAST 16–19w / FAT74 20w / GTA 38w
                            # / POKEMON / Puggle 464w / VOF
                            # leftover recap / LEGO under
                            # ChatGPT 45m / SLINK under ChatGPT
                            # 45m / 26–35w. Do not leftover-sort
                            # 2×+. Do not leftover ASSDAQ
                            # leftover recap / the live ASSDAQ
                            # 1.52× climb.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 44
                                and 900_000 <= mcap < 980_000
                                and 1.10 < multiple < 1.15
                                and age_s >= 30 * 60
                            )
                            # Live 10:20: CHUMPWHALE 37w / $66k /
                            # 1.37× / 5m sat leftover_size=0 #1
                            # last=$66k liq=$20k max=$66k mint
                            # CiZxi…pump. HUGGY leftover needs
                            # 26–45w / <$200k / <1.15 / 30m.
                            # BAPE leftover needs <1.05. Coca
                            # Cola leftover needs 36–39w /
                            # $520–540k. 26–35w stays. A 36–
                            # 39w $60–70k book still 1.30–1.40
                            # at 15m is leftover tape. A 1.40×
                            # climb stays. $59k stays. $70k
                            # stays. 35w stays. 40w stays. Do
                            # not leftover 1.40×+ or 1.30×- or
                            # $70k+ or below $60k or under 36w
                            # or 40w+. Do not leftover under
                            # 15m. Do not leftover 26–35w /
                            # HUGGY <1.15 / BAPE <1.05 / Coca
                            # Cola 28w / LEGO under ChatGPT 45m
                            # / GTA 44w leftover recap / VOF
                            # leftover recap / leftover-sort
                            # 2×+. Do not leftover ASSDAQ
                            # leftover recap / the live ASSDAQ
                            # 2.01× climb.
                            or (
                                holders is not None
                                and 36 <= int(holders) <= 39
                                and 60_000 <= mcap < 70_000
                                and 1.30 < multiple < 1.40
                                and age_s >= 15 * 60
                            )
                            # Live 10:40: VibeCat 231w / $122k / 1.00× /
                            # 101m sat leftover_size=0 #9 last=$120k
                            # liq=$28k max=$121,667 mint 7derok…pump.
                            # The prior last=0 VibeCat chair stays —
                            # do not leftover last=0. GoMeme leftover
                            # needs 220–229w / $150–165k / 1.45–1.60.
                            # BULLISHCAT leftover needs 230–249w /
                            # $170–190k / <1.50. Pairz leftover needs
                            # $68–80k / 1.40–1.50. Corgi $117k stays.
                            # A 230–239w $118–130k book still under
                            # 1.05 at 60m with a real last print is
                            # leftover tape. A 1.05× climb stays.
                            # $117k stays. $130k stays. 229w stays.
                            # 240w stays. last=0 stays. Do not leftover
                            # 1.05×+ or $130k+ or below $118k or under
                            # 230w or 240w+. Do not leftover under 60m.
                            # Do not leftover last=0 / GoMeme / Pairz /
                            # BULLISHCAT / Corgi $117k / CHUMP 1.49× /
                            # LEGO under ChatGPT 45m / Coca Cola 28w /
                            # GTA 44w leftover recap / VOF leftover
                            # recap / leftover-sort 2×+. Do not leftover
                            # ASSDAQ leftover recap / the live ASSDAQ
                            # 2.01× climb / BROTHERHOOD leftover recap.
                            or (
                                holders is not None
                                and 230 <= int(holders) <= 239
                                and 118_000 <= mcap < 130_000
                                and multiple < 1.05
                                and age_s >= 60 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 11:00: IBRL 367w / $217k / 1.15× /
                            # 16m sat leftover_size=0 #2 last=$197k
                            # liq=$36k max=$216,685 mint DJ9Nx…pump.
                            # ironmike leftover needs <$80k / <1.20.
                            # FROGAS leftover needs 250–349w /
                            # $60–80k / 2h. MUMONO leftover needs
                            # 300–349w / $60–70k. PUPS leftover
                            # needs 390–399w / $220–250k / 1.60–
                            # 1.80. BRAINDEAD leftover needs 400–
                            # 419w / $170–195k. NEKO 360w / $44k
                            # stays. A 360–379w $205–220k book
                            # still under 1.20 at 15m is leftover
                            # tape. A 1.20× climb stays. $204k
                            # stays. $220k stays. 359w stays.
                            # 380w stays. Do not leftover 1.20×+
                            # or $220k+ or below $205k or under
                            # 360w or 380w+. Do not leftover under
                            # 15m. Do not leftover ironmike /
                            # FROGAS / MUMONO / PUPS / BRAINDEAD /
                            # NEKO 360w / IBRL 2.04× / VibeCat
                            # leftover recap / CHUMP 1.49× / LEGO
                            # under ChatGPT 45m / Coca Cola 28w /
                            # leftover-sort 2×+. Do not leftover
                            # ASSDAQ leftover recap / the live
                            # ASSDAQ 2.01× climb.
                            or (
                                holders is not None
                                and 360 <= int(holders) <= 379
                                and 205_000 <= mcap < 220_000
                                and multiple < 1.20
                                and age_s >= 15 * 60
                            )
                            # Live 11:20: WOFI 64w / $10.6M / 1.93× /
                            # 331m sat leftover_size=0 #10 last=$10.6M
                            # liq=$275k max=$10,622,742 mint
                            # kZbqh…pump. The $5M / <1.9 leftover
                            # missed 1.927. Magatard $1.67M / 1.92×
                            # stays. HOOD $14M / 30w stays on the
                            # $1.5M leftover. SLINK / LEGO $900k
                            # under ChatGPT 45m stay. A 60–69w
                            # $10–12M book still 1.90–1.95 at 15m
                            # is leftover tape. A 1.95× climb
                            # stays. $9.9M stays. $12M stays.
                            # 59w stays. 70w stays. Do not leftover
                            # 1.95×+ or 1.90×- or $12M+ or below
                            # $10M or under 60w or 70w+. Do not
                            # leftover under 15m. Do not leftover
                            # Magatard $1.67M / HOOD 30w / SLINK
                            # under ChatGPT 45m / LEGO under
                            # ChatGPT 45m / PONSGUY / IBRL leftover
                            # recap / VibeCat leftover recap /
                            # leftover-sort 2×+ / 26–35w. Do not
                            # leftover ASSDAQ leftover recap /
                            # the live ASSDAQ 2.01× climb.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 10_000_000 <= mcap < 12_000_000
                                and 1.90 < multiple < 1.95
                                and age_s >= 15 * 60
                            )
                            # Live 11:40: MINEFLY 304w / $184k / 1.00× /
                            # 5m sat leftover_size=0 #0 last=$124k
                            # liq=$28k max=$184,227 mint
                            # Egaiw…JH. MUMONO leftover needs 300–
                            # 349w / $60–70k. DANKFROGE leftover
                            # needs $1.05–1.15M. BRAINDEAD leftover
                            # needs 400–419w / $170–195k. NOMEME
                            # leftover needs 110–149w / $170–200k.
                            # ironmike leftover needs <$80k.
                            # IBRL leftover needs 360–379w /
                            # $205–220k. MEME leftover needs
                            # $200k+ / prepumped. A 300–319w
                            # $170–200k book still under 1.05 at
                            # 15m with a real last print is
                            # leftover tape. A 1.05× climb stays.
                            # $169k stays. $200k stays. 299w
                            # stays. 320w stays. last=0 stays.
                            # Do not leftover 1.05×+ or $200k+
                            # or below $170k or under 300w or
                            # 320w+. Do not leftover under 15m.
                            # Do not leftover last=0 / MUMONO
                            # $60–70k / DANKFROGE $1.05M /
                            # BRAINDEAD 400w / NOMEME 110–149w /
                            # ironmike / IBRL leftover recap /
                            # MEME $200k+ prepumped / MEMES $184k
                            # / CALLCAT ANSEM–STONKLESS stay /
                            # SLINK under ChatGPT 45m / LEGO
                            # under ChatGPT 45m / SolBull
                            # bullson 60m / WOFI leftover recap /
                            # VibeCat leftover recap / leftover-
                            # sort 2×+ / 26–35w. Do not leftover
                            # ASSDAQ leftover recap / the live
                            # ASSDAQ 2.01× climb.
                            or (
                                holders is not None
                                and 300 <= int(holders) <= 319
                                and 170_000 <= mcap < 200_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 12:00: PANDA 221w / $50k / 1.31× /
                            # 50m sat leftover_size=0 #3 last=$49k
                            # liq=$17k max=$49,742 mint
                            # CEe7G…vwbF. ironmike leftover needs
                            # <1.20. apecat leftover needs 200–
                            # 215w / $50–65k / 1.40–1.60. PSA
                            # leftover needs 215–225w / $63–72k.
                            # bullson leftover needs 250–299w /
                            # $35–55k / 60m. FROGAS leftover
                            # needs 250w+ / $60–80k. A 220–229w
                            # $45–52k book still 1.25–1.35 at 45m
                            # with a real last print is leftover
                            # tape. A 1.35× climb stays. A 1.25×
                            # climb stays. $44k stays. $52k
                            # stays. 219w stays. 230w stays.
                            # last=0 stays. Do not leftover
                            # 1.35×+ or 1.25×- or $52k+ or below
                            # $45k or under 220w or 230w+. Do
                            # not leftover under 45m. Do not
                            # leftover last=0 / ironmike <1.20 /
                            # apecat leftover recap / PSA
                            # leftover recap / bullson / FROGAS
                            # / PAIRLESS $97k / MINEFLY leftover
                            # recap / LEGO under ChatGPT 45m /
                            # VOF leftover recap / Yeet 2.11× /
                            # SLINK $997k / 1.18× climb / WOFI
                            # leftover recap / leftover-sort 2×+
                            # / 26–35w. Do not leftover ASSDAQ
                            # leftover recap / the live ASSDAQ
                            # 2.01× climb. Do not widen ironmike
                            # 200w+ / <1.20.
                            or (
                                holders is not None
                                and 220 <= int(holders) <= 229
                                and 45_000 <= mcap < 52_000
                                and 1.25 < multiple < 1.35
                                and age_s >= 45 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 12:40: MINI 131w / $231k / 1.25× /
                            # 8m sat leftover_size=0 #0 last=$159k
                            # liq=$32k max=$230,530 mint
                            # 8M5Q7…pump. NOMEME leftover needs
                            # 110–149w / $170–200k / <1.30 / 2h.
                            # PUMPLIFE leftover needs 125–135w /
                            # $170–200k / 1.40–1.60. CATMAX
                            # leftover needs <$80k / <1.20.
                            # TROBIN leftover needs $75–90k /
                            # 1.60–1.80. VOF leftover needs
                            # 110–119w / $900–980k. A 125–139w
                            # $220–250k book still 1.20–1.30 at
                            # 15m with a real last print is
                            # leftover tape. A 1.30× climb stays.
                            # A 1.20× climb stays. $219k stays.
                            # $250k stays. 124w stays. 140w
                            # stays. last=0 stays. Do not leftover
                            # 1.30×+ or 1.20×- or $250k+ or below
                            # $220k or under 125w or 140w+. Do
                            # not leftover under 15m. Do not
                            # leftover last=0 / NOMEME $176k /
                            # PUMPLIFE leftover recap / CATMAX /
                            # TROBIN / LASTPONS / WPONS
                            # (PONS-named) / VOF leftover recap /
                            # LEGO 115w / $995k / NVIDIH leftover
                            # recap / PT (PONS-named) / leftover-
                            # sort 2×+ / 26–35w. Do not leftover
                            # ASSDAQ leftover recap / the live
                            # ASSDAQ 2.01× climb. Do not leftover
                            # EXPO 35w / THEROCK 26w / FLUID
                            # 1.93× / CREO 2.16×.
                            or (
                                holders is not None
                                and 125 <= int(holders) <= 139
                                and 220_000 <= mcap < 250_000
                                and 1.20 < multiple < 1.30
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 13:00: PUMPCAT 44w / $169k / 1.27× /
                            # 17m sat leftover_size=0 #2 last=$161k
                            # liq=$33k max=$168,677 mint
                            # HtpQv…pump. StonkCat leftover needs
                            # $130–145k / <1.50 / 20m. MEMELESS
                            # leftover needs $145–160k / 1.20–
                            # 1.40 / 20m. WTT leftover needs
                            # $170–200k / <1.40 / 2h. AMC leftover
                            # needs $850–980k / <1.10 / 30m.
                            # HUGGY leftover needs <1.15 / 30m.
                            # A 40–44w $160–170k book still
                            # 1.20–1.35 at 15m with a real last
                            # print is leftover tape. A 1.35×
                            # climb stays. A 1.20× climb stays.
                            # $160k stays. $170k stays. 39w
                            # stays. 45w stays. last=0 stays.
                            # Do not leftover 1.35×+ or 1.20×-
                            # or $170k+ or $160k- or under 40w
                            # or 45w+. Do not leftover under
                            # 15m. Do not leftover last=0 /
                            # StonkCat leftover recap / MEMELESS
                            # leftover recap / WTT leftover
                            # recap / AMC leftover recap /
                            # HUGGY <1.15 / BEAST 41w / $890k
                            # (AMC leftover-in-waiting) / Amazon
                            # 17w / $943k (BEAST leftover-in-
                            # waiting) / MINI leftover recap /
                            # leftover-sort 2×+ / 26–35w. Do
                            # not leftover ASSDAQ leftover recap
                            # / the live ASSDAQ 2.01× climb. Do
                            # not cut the HUGGY 30m / <1.15
                            # clock. Do not leftover AMC 1.10×
                            # climb stay / WTT under 2h.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 44
                                and 160_000 < mcap < 170_000
                                and 1.20 < multiple < 1.35
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 13:40: CUPCAKE 559w / $179k / 1.59× /
                            # 14.7m sat leftover_size=0 #1 last=$179k
                            # liq=$35k max=$179,346 mint
                            # EHGG99…C6E. MINEFLY leftover needs
                            # 300–319w. BRAINDEAD leftover needs
                            # 400–419w. ironmike leftover needs
                            # <$80k / <1.20. MEME leftover needs
                            # $200k+ prepumped. THEBIGLONG 1.55×
                            # stays. A 540–579w $160–200k book
                            # still 1.55–1.70 at 15m with a real
                            # last print is leftover tape. A 1.70×
                            # climb stays. A 1.55× climb stays.
                            # $160k stays. $200k stays. 539w
                            # stays. 580w stays. last=0 stays.
                            # Do not leftover 1.70×+ or 1.55×-
                            # or $200k+ or $160k- or under 540w
                            # or 580w+. Do not leftover under
                            # 15m. Do not leftover last=0 /
                            # MINEFLY leftover recap / BRAINDEAD
                            # leftover recap / ironmike $80k+
                            # stay / MEME leftover recap /
                            # THEBIGLONG 1.55× / leftover-sort
                            # 2×+ / 26–35w. Do not leftover
                            # PUMPCAT leftover recap / MINI
                            # leftover recap / POINTLESS /
                            # BEAST $980k+ stay / IDIOT 29w.
                            or (
                                holders is not None
                                and 540 <= int(holders) <= 579
                                and 160_000 < mcap < 200_000
                                and 1.55 < multiple < 1.70
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 14:20: LQX 117w / $270k / 1.72× /
                            # 19.8m sat leftover_size=0 #0 last=$270k
                            # liq=$42k max=$270,353 mint
                            # tHjoz…SeC. NOMEME leftover needs
                            # 110–149w / $170–200k / <1.30 / 2h.
                            # MINI leftover needs 125–139w /
                            # $220–250k. PUMPLIFE leftover
                            # needs 125–135w / $170–200k. VOF
                            # leftover needs 110–119w /
                            # $900–980k. SPX leftover needs
                            # $155–169k / <1.10. CHROME 1.72×
                            # stays. A 110–124w $250–290k book
                            # still 1.72–1.80 at 15m with a real
                            # last print is leftover tape. A 1.80×
                            # climb stays. A 1.72× climb stays.
                            # $250k stays. $290k stays. 109w
                            # stays. 125w stays. last=0 stays.
                            # Do not leftover 1.80×+ or 1.72×-
                            # or $290k+ or $250k- or under 110w
                            # or 125w+. Do not leftover under
                            # 15m. Do not leftover last=0 /
                            # NOMEME leftover recap / MINI
                            # leftover recap / PUMPLIFE leftover
                            # recap / VOF leftover recap /
                            # CHROME 1.72× / SOLLESS 126w
                            # (NOMEME 2h leftover-in-waiting) /
                            # leftover-sort 2×+ / 26–35w. Do
                            # not leftover CUPCAKE leftover
                            # recap / POINTLESS / BEAST $980k+
                            # stay / PATRIOT under the RH 1h
                            # clock.
                            or (
                                holders is not None
                                and 110 <= int(holders) <= 124
                                and 250_000 < mcap < 290_000
                                and 1.72 < multiple < 1.80
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 15:00: TRUDY 502w / $166k / 1.50× /
                            # 19.6m sat leftover_size=0 #1 last=$126k
                            # liq=$28k max=$165,553 mint
                            # 3f3u6…XAy. CUPCAKE leftover needs
                            # 540–579w / $160–200k / 1.55–1.70.
                            # TRIPLEP leftover needs 400w+ /
                            # $70–90k. MEME leftover needs
                            # $200k+ prepumped. BRAINDEAD
                            # leftover needs 400–419w /
                            # $170–195k. THEBIGLONG 1.55×
                            # stays. A 490–519w $150–180k book
                            # still 1.40–1.55 at 15m with a real
                            # last print is leftover tape. A 1.55×
                            # climb stays. A 1.40× climb stays.
                            # $150k stays. $180k stays. 489w
                            # stays. 520w stays. last=0 stays.
                            # Do not leftover 1.55×+ or 1.40×-
                            # or $180k+ or $150k- or under 490w
                            # or 520w+. Do not leftover under
                            # 15m. Do not leftover last=0 /
                            # CUPCAKE leftover recap / TRIPLEP
                            # $90k+ stay / BIKEANSON $94k /
                            # MEME leftover recap / BRAINDEAD
                            # leftover recap / THEBIGLONG 1.55×
                            # / leftover-sort 2×+ / 26–35w. Do
                            # not leftover LQX leftover recap /
                            # AGIALPHA (MarsCoin $151k stay) /
                            # SOLLESS (NOMEME 2h) / LEGO 71w
                            # (70–79w $70k+) / ZBULL (Bufo
                            # $65k+ stay).
                            or (
                                holders is not None
                                and 490 <= int(holders) <= 519
                                and 150_000 < mcap < 180_000
                                and 1.40 < multiple < 1.55
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 16:00: ZRAC 82w / $57k / 1.456× /
                            # 42m sat leftover_size=0 #3 last=$41k
                            # liq=$15k max=$57,494 mint
                            # 4WZ8M…g4fY. STONKLESS leftover needs
                            # 80–84w / $160–175k / <1.40. ANSEM
                            # leftover needs 80–87w / $115–140k /
                            # 1.10–1.25. GEEG leftover needs
                            # 75–79w. CASHBIRD leftover is RH
                            # 80–89w / $35–41k. A 80–84w $50–65k
                            # book still 1.40–1.50 at 15m with a
                            # real last print is leftover tape. A
                            # 1.50× climb stays. A 1.40× climb
                            # stays. $50k stays. $65k stays. 79w
                            # stays. 85w stays. last=0 stays. Do
                            # not leftover 1.50×+ or 1.40×- or
                            # $65k+ or $50k- or under 80w or
                            # 85w+. Do not leftover under 15m.
                            # Do not leftover last=0 / STONKLESS
                            # leftover recap / ANSEM leftover
                            # recap / GEEG leftover recap /
                            # CASHBIRD / leftover-sort 2×+ /
                            # 26–35w. Do not leftover POMP (CAT
                            # $175k+ stay) / DANGER leftover
                            # recap / nip (ironmike $80k+ stay).
                            or (
                                holders is not None
                                and 80 <= int(holders) <= 84
                                and 50_000 < mcap < 65_000
                                and 1.40 < multiple < 1.50
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 16:40: STONKCHUMP 92w / $154k /
                            # 1.229× / 22m sat leftover_size=0 #0
                            # last=$148k liq=$31k max=$154,152
                            # mint 8Ge69…G2Uf. BoardAI leftover
                            # (RH) needs 90–96w / $125–145k /
                            # 1.55–1.70. WASTED leftover needs
                            # 90–96w / $80–90k / 1.30–1.40.
                            # Murad leftover needs 95–102w /
                            # $260–300k. ZRAC leftover needs
                            # 80–84w / $50–65k. A 90–96w $145–
                            # 165k book still 1.20–1.30 at 15m
                            # with a real last print is leftover
                            # tape. Exclusive of 1.55× so BoardAI
                            # $145k+ stay holds. A 1.30× climb
                            # stays. A 1.20× climb stays. $145k
                            # stays. $165k stays. 89w stays.
                            # 97w stays. last=0 stays. Do not
                            # leftover 1.30×+ or 1.20×- or
                            # $165k+ or $145k- or under 90w or
                            # 97w+. Do not leftover under 15m.
                            # Do not leftover last=0 / BoardAI
                            # leftover recap / WASTED leftover
                            # recap / Murad leftover recap /
                            # ZRAC leftover recap / leftover-sort
                            # 2×+ / 26–35w. Do not leftover
                            # TIKTOK / obsinu (70–79w $70k+) /
                            # Wojakus (CHROME 1.72× stay).
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 96
                                and 145_000 < mcap < 165_000
                                and 1.20 < multiple < 1.30
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 17:20: AGI 645w / $182k / 1.000× /
                            # 9.5m sat leftover_size=0 #1 last=$140k
                            # liq=$31k max=$182,134 mint
                            # 86Rt8…5E. TAURA leftover needs
                            # 650–749w / $1.15–1.30M / 45m.
                            # CUPCAKE leftover needs 540–579w /
                            # $160–200k / 1.55–1.70. MINEFLY
                            # leftover needs 300–319w / $170–
                            # 200k / <1.05. BRAINDEAD leftover
                            # needs 400–419w. TRUDY leftover
                            # needs 490–519w / $150–180k. A
                            # 630–649w $160–200k book still
                            # under 1.05 at 15m with a real last
                            # print is leftover tape. Exclusive
                            # of 650w / $1.15M so TAURA leftover
                            # holds. Exclusive of 540–579w so
                            # CUPCAKE leftover holds. Exclusive
                            # of $200k+ so MEME leftover holds.
                            # A 1.05× climb stays. $160k stays.
                            # $200k stays. 629w stays. 650w
                            # stays. last=0 stays. Do not
                            # leftover 1.05×+ or $200k+ or
                            # $160k- or under 630w or 650w+.
                            # Do not leftover under 15m. Do not
                            # leftover last=0 / TAURA leftover
                            # recap / CUPCAKE leftover recap /
                            # MINEFLY leftover recap / BRAINDEAD
                            # leftover recap / TRUDY leftover
                            # recap / MEME leftover recap /
                            # leftover-sort 2×+ / 26–35w. Do
                            # not leftover GTA 6 Coin (under
                            # 15m / GTA leftover recap /
                            # POKEMON 1.08×+ / Redbull 1.10×)
                            # / Maggie (LTPT 1.63× stay) /
                            # GPT6 leftover recap / THEBIGLONG
                            # 1.55×.
                            or (
                                holders is not None
                                and 630 <= int(holders) <= 649
                                and 160_000 < mcap < 200_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 17:40: GTA 6 Coin 84w / $541k /
                            # 1.137× / 27.5m sat leftover_size=0 #0
                            # last=$541k liq=$61k max=$541,286 mint
                            # FkgtW…g4fY. ANSEM leftover needs 80–
                            # 87w / $115–140k. STONKLESS leftover
                            # needs 80–84w / $160–175k. ZRAC leftover
                            # needs 80–84w / $50–65k. GTA leftover
                            # recap is 40–44w / $900–980k and 55–
                            # 58w / $520–560k. POKEMON leftover
                            # needs <1.08. Redbull leftover needs
                            # 1.10. Coca Cola leftover is 72w /
                            # 36–39w. A 80–87w $520–560k book
                            # still in 1.12–1.15 at 15m is leftover
                            # tape. A 1.15× climb stays. A 1.12×
                            # climb stays. $520k stays. $560k
                            # stays. 79w stays. 88w stays. last=0
                            # stays. Under 15m stays. Do not leftover
                            # 1.15×+ or 1.12×- or $560k+ or below
                            # $520k or under 80w or 88w+. Do not
                            # leftover under 15m / last=0. Do not
                            # leftover ANSEM leftover recap /
                            # STONKLESS leftover recap / ZRAC
                            # leftover recap / GTA leftover recap
                            # / GTA 1.15×+ / $560k+ / POKEMON
                            # 1.08× / Redbull 1.10× / Coca Cola
                            # 72w / THEBIGLONG 1.55× / leftover-
                            # sort 2×+ / 26–35w.
                            or (
                                holders is not None
                                and 80 <= int(holders) <= 87
                                and 520_000 < mcap < 560_000
                                and 1.12 < multiple < 1.15
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 18:20: NIKE TYSON 230w / $67k /
                            # 1.595× / 21.6m sat leftover_size=0 #3
                            # last=$67k liq=$20k max=$66,905 mint
                            # Cz16B…rphu. VibeCat leftover needs
                            # 230–239w / $118–130k / <1.05 / 60m.
                            # GoMeme leftover needs 220–229w /
                            # $150–165k. BULLISHCAT leftover
                            # needs 230–249w / $170–190k.
                            # Pairz leftover needs 235–249w /
                            # $68–80k / 1.40–1.50 ($67k stays
                            # on that chair). A 230–234w $55–
                            # 68k book still in 1.55–1.63 at
                            # 15m is leftover tape. Exclusive
                            # of 1.55× so THEBIGLONG 1.55×
                            # stays. Exclusive of 1.63× so
                            # LTPT 1.63× stays. Exclusive of
                            # 1.72× so CHROME 1.72× stays.
                            # Exclusive of 235w so Pairz $67k
                            # stay holds. A 1.63× climb stays.
                            # A 1.55× climb stays. $55k stays.
                            # $68k stays. 229w stays. 235w
                            # stays. last=0 stays. Under 15m
                            # stays. Do not leftover 1.63×+
                            # or 1.55×- or $68k+ or $55k- or
                            # under 230w or 235w+. Do not
                            # leftover under 15m / last=0.
                            # Do not leftover VibeCat leftover
                            # recap / GoMeme leftover recap /
                            # BULLISHCAT leftover recap / Pairz
                            # leftover recap / memestock /
                            # THEBIGLONG 1.55× / LTPT 1.63× /
                            # CHROME 1.72× / leftover-sort 2×+
                            # / 26–35w. Do not leftover POKEMONZ
                            # leftover recap / GTA leftover recap
                            # / AGI leftover recap / Juror leftover
                            # recap.
                            or (
                                holders is not None
                                and 230 <= int(holders) <= 234
                                and 55_000 < mcap < 68_000
                                and 1.55 < multiple < 1.63
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 18:40: Minecraft Fruit Fly 618w /
                            # $185k / 1.000× / 24.3m sat leftover_size=0
                            # #0 last=$168k liq=$33k max=$185,437 mint
                            # C6j4j…keC. AGI leftover needs 630–649w /
                            # $160–200k / <1.05 (629w stays). CUPCAKE
                            # leftover needs 540–579w. TAURA leftover
                            # needs 650–749w / $1.15M. MEME leftover
                            # needs $200k+. A 610–624w $160–200k book
                            # still under 1.05 at 15m with a real last
                            # print is leftover tape. Exclusive of 630w
                            # so AGI leftover / 629w stay holds.
                            # Exclusive of 651w so Juror leftover recap
                            # holds. A 1.05× climb stays. $160k stays.
                            # $200k stays. 609w stays. 625w stays.
                            # last=0 stays. Under 15m stays. Do not
                            # leftover 1.05×+ or $200k+ or $160k- or
                            # under 610w or 625w+. Do not leftover
                            # under 15m / last=0. Do not leftover AGI
                            # leftover recap / the live AGI 1.089×
                            # climb / CUPCAKE leftover recap / TAURA
                            # leftover recap / MEME leftover recap /
                            # Juror leftover recap / leftover-sort 2×+
                            # / 26–35w. Do not leftover NIKE leftover
                            # recap / the live NIKE 1.79× climb /
                            # STONKCAT (HODL 1.12× / Redbull 1.10×) /
                            # GTA leftover recap / POKEMONZ leftover
                            # recap.
                            or (
                                holders is not None
                                and 610 <= int(holders) <= 624
                                and 160_000 < mcap < 200_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 19:00: MrBeast 390w / $124k /
                            # 1.133× / 4.8m sat leftover_size=0 #0
                            # last=$124k liq=$28k max=$123,864 mint
                            # CeXHd…tER. PUPS leftover needs 390–
                            # 399w / $220–250k / 1.60–1.80. IBRL
                            # leftover needs 367w / $217k. BRAINDEAD
                            # leftover needs 400–419w. MINEFLY
                            # leftover needs 300–319w. ironmike
                            # leftover needs <$80k. A 385–399w
                            # $110–140k book still in 1.12–1.20 at
                            # 15m is leftover tape. Exclusive of
                            # 1.12× so HODL 1.12× stays. Exclusive
                            # of 1.10× so Redbull 1.10× stays.
                            # Exclusive of 1.23× so WARDOG 1.23×
                            # stays. Exclusive of $220k so PUPS
                            # leftover holds. A 1.20× climb stays.
                            # A 1.12× climb stays. $110k stays.
                            # $140k stays. 384w stays. 400w stays.
                            # last=0 stays. Under 15m stays. Do
                            # not leftover 1.20×+ or 1.12×- or
                            # $140k+ or $110k- or under 385w or
                            # 400w+. Do not leftover under 15m /
                            # last=0. Do not leftover PUPS leftover
                            # recap / IBRL leftover recap /
                            # BRAINDEAD leftover recap / MINEFLY
                            # leftover recap / HODL 1.12× / Redbull
                            # 1.10× / WARDOG 1.23× / ironmike $80k+
                            # / THEBIGLONG 1.55× / leftover-sort 2×+
                            # / 26–35w. Do not leftover PONS INU
                            # (PONS-named) / Fruit Fly leftover
                            # recap / NIKE leftover recap / the
                            # live NIKE 1.79× climb / AMC 2h Stonk
                            # Chump leftover-in-waiting / Juror
                            # leftover recap / STONKCAT / Cosmic
                            # Gregory 2.04× (leftover-sort 2×+).
                            or (
                                holders is not None
                                and 385 <= int(holders) <= 399
                                and 110_000 < mcap < 140_000
                                and 1.12 < multiple < 1.20
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 19:20: CHIKAI 133w / $59k /
                            # 1.967× / 25m sat leftover_size=0 #1
                            # last=$41k liq=$15k max=$59,216 mint
                            # H65nT…pump. PUMPLIFE leftover needs
                            # 125–135w / $170–200k. TROBIN leftover
                            # needs 125–135w / $75–90k. WAR leftover
                            # needs 135–145w. Hbros leftover needs
                            # 138–145w. TESTUS leftover needs 100–
                            # 109w. BULLAI leftover needs 110–119w.
                            # A 130–134w $50–65k book still in
                            # 1.90–2.00 at 15m is leftover tape.
                            # Exclusive of 2.00× so leftover-sort
                            # 2×+ stays. Exclusive of 1.72× so
                            # CHROME 1.72× stays. Exclusive of
                            # 1.63× so LTPT 1.63× stays. Exclusive
                            # of 1.55× so THEBIGLONG 1.55× stays.
                            # Exclusive of $75k so TROBIN leftover
                            # holds. Exclusive of $170k so
                            # PUMPLIFE leftover holds. Exclusive
                            # of 135w so WAR leftover holds. A
                            # 2.00× climb stays. A 1.90× climb
                            # stays. $50k stays. $65k stays. 129w
                            # stays. 135w stays. last=0 stays.
                            # Under 15m stays. Do not leftover
                            # 2.00×+ or 1.90×- or $65k+ or $50k-
                            # or under 130w or 135w+. Do not
                            # leftover under 15m / last=0. Do not
                            # leftover leftover-sort 2×+ / CHROME
                            # 1.72× / LTPT 1.63× / THEBIGLONG
                            # 1.55× / WAR leftover recap / TROBIN
                            # leftover recap / PUMPLIFE leftover
                            # recap / Hbros leftover recap / 26–
                            # 35w. Do not leftover MrBeast leftover
                            # recap / the live MrBeast 1.20× climb
                            # / ATLAS 1.39× (RTD 1.39× stay) /
                            # PONS INU (PONS-named) / Fruit Fly
                            # leftover recap / NIKE leftover recap.
                            or (
                                holders is not None
                                and 130 <= int(holders) <= 134
                                and 50_000 < mcap < 65_000
                                and 1.90 < multiple < 2.00
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 20:40: Coca-Cola 56w / $1.00M /
                            # 1.174× / 9.8m sat leftover_size=0 #0
                            # last=$1.00M liq=$84k max=$1,004,898
                            # mint Cz11h…pump. GTA leftover needs
                            # 55–58w / $520–560k / <1.15 / ≥20m.
                            # ChatGPT leftover needs 45–69w /
                            # $500k+ / <1.20 / 45m / prepumped.
                            # AMC leftover needs 40–44w / $850–
                            # 980k / <1.10. Coca Cola leftover
                            # needs 36–39w / $520–540k. The 90–
                            # 99w Coca Cola leftover needs $540–
                            # 570k / 2h. $1.1M leftover needs
                            # <1.20. A 55–58w $980k–$1.01M book
                            # still in 1.15–1.20 at 15m with a
                            # real last print is leftover tape.
                            # Exclusive of $560k so GTA leftover
                            # holds. Exclusive of prepumped so
                            # ChatGPT leftover holds. Exclusive
                            # of $980k so AMC leftover holds.
                            # Exclusive of $1.01M so FOMO $1.01M
                            # stays. Exclusive of $873k so
                            # ChatGPT $873k stays. Exclusive of
                            # $894k so MINI $894k stays.
                            # Exclusive of 1.12× so HODL 1.12×
                            # stays. Exclusive of 1.10× so
                            # Redbull 1.10× stays. Exclusive of
                            # 1.23× so WARDOG 1.23× stays.
                            # Exclusive of last=0. A 1.20× climb
                            # stays. A 1.15× climb stays. $980k
                            # stays. $1.01M stays. 54w stays.
                            # 59w stays. last=0 stays. Under
                            # 15m stays. Do not leftover 1.20×+
                            # or 1.15×- or $1.01M+ or $980k- or
                            # under 55w or 59w+. Do not leftover
                            # under 15m / last=0. Do not leftover
                            # GTA leftover recap / ChatGPT leftover
                            # recap / ChatGPT $565k / $873k / MINI
                            # $894k / FOMO $1.01M / AMC leftover
                            # recap / Coca Cola leftover recap
                            # (36–39w / 72w / 90–99w) / HODL
                            # 1.12× / Redbull 1.10× / WARDOG
                            # 1.23× / leftover-sort 2×+ / 26–
                            # 35w. Do not leftover MrBeast leftover
                            # recap / the live MrBeast 1.20× climb
                            # / CHIKAI leftover recap / NIKE leftover
                            # recap / STONKCAT / Magatard $1.67M /
                            # sixseven 1.66× (MSTR 1.40×+) /
                            # ROBINRACCON (ACC below $65k) /
                            # PONS-named / ironmike $80k+ /
                            # BEAST $980k+ / THEBIGLONG 1.55× /
                            # LTPT 1.63× / CHROME 1.72×.
                            or (
                                holders is not None
                                and 55 <= int(holders) <= 58
                                and 980_000 < mcap < 1_010_000
                                and 1.15 < multiple < 1.20
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 22:40: Tony 497w / $125k /
                            # 1.817× / 310m sat leftover_size=0
                            # #12 last=$98k liq=$27k max=$125,398
                            # mint 3sSbD…irh7. TRUDY leftover
                            # needs 490–519w / $150–180k /
                            # 1.40–1.55. PlutoCoin leftover
                            # needs 400w+ / $90–120k / <1.20.
                            # STOCKFATHER leftover needs 400–
                            # 430w / $300–350k. Fruit Fly
                            # leftover needs 610–624w. A 495–
                            # 504w $120–135k book still in
                            # 1.80–1.85 at 15m with a real last
                            # print is leftover tape. Exclusive
                            # of $150k so TRUDY leftover holds.
                            # Exclusive of $120k / <1.20 so
                            # PlutoCoin leftover holds.
                            # Exclusive of 430w so STOCKFATHER
                            # leftover holds. Exclusive of 610w
                            # so Fruit Fly leftover holds.
                            # Exclusive of 1.72× so CHROME
                            # 1.72× stays. Exclusive of 1.74×
                            # so BOMB 1.74× stays. Exclusive of
                            # 1.71× so ATM 1.71× stays.
                            # Exclusive of 1.55× so THEBIGLONG
                            # 1.55× stays. Exclusive of 2.00×
                            # so leftover-sort 2×+ stays.
                            # Exclusive of last=0. A 1.85×
                            # climb stays. A 1.80× climb stays.
                            # $120k stays. $135k stays. 494w
                            # stays. 505w stays. last=0 stays.
                            # Under 15m stays. Do not leftover
                            # 1.85×+ or 1.80×- or $135k+ or
                            # $120k- or under 495w or 505w+.
                            # Do not leftover under 15m /
                            # last=0. Do not leftover TRUDY
                            # leftover recap / PlutoCoin leftover
                            # recap / STOCKFATHER leftover recap
                            # / Fruit Fly leftover recap /
                            # CHROME 1.72× / ATM 1.71× / BOMB
                            # 1.74× / THEBIGLONG 1.55× / LTPT
                            # 1.63× / leftover-sort 2×+ /
                            # 26–35w. Do not leftover Coca-Cola
                            # leftover recap / the live Coca-Cola
                            # 1.21× climb / Windows leftover
                            # recap / the live Windows 1.773× /
                            # $797k climb / Agent OBS leftover
                            # recap / the live Agent OBS 1.742× /
                            # $544k climb / cats leftover recap /
                            # God's Eye leftover recap /
                            # Safemoon leftover recap / Claude /
                            # Palantir (USDCRACK 1.05×+) / PONS
                            # AI (PONS-named) / employim 1.555×
                            # (THEBIGLONG 1.55×) / AGI 64w $171k
                            # (Shapan leftover recap) / OpenAI /
                            # Coca Cola 72w / ROBGUY under 15m
                            # (Redbull 1.10× / CAT $160k+). Do
                            # not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 495 <= int(holders) <= 504
                                and 120_000 < mcap < 135_000
                                and 1.80 < multiple < 1.85
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 23:20: MICRO TYSON 59w / $89k /
                            # 1.885× / 12.4m sat leftover_size=0
                            # #1 last=$81k liq=$23k max=$88,980
                            # mint 537bL…pump. Coca-Cola leftover
                            # needs 55–58w / $980k–$1.01M /
                            # 1.15–1.20. PEEP leftover needs
                            # 50–59w / $120–150k / <1.20. FOMO
                            # leftover needs 50–59w / $70–80k /
                            # 1.30–1.40. ACC leftover needs 60–
                            # 64w / $65–72k. HOTAIR leftover
                            # needs 55–59w / $36–40k. NIKE leftover
                            # needs 230w / $67k. Tony leftover
                            # needs 495–504w / 1.80–1.85. CHIKAI
                            # leftover needs 1.90–2.00. A 56–59w
                            # $85–95k book still in 1.85–1.90 at
                            # 15m with a real last print is leftover
                            # tape. Exclusive of $980k so Coca-Cola
                            # leftover holds. Exclusive of $80k so
                            # FOMO leftover holds. Exclusive of
                            # $120k so PEEP leftover holds.
                            # Exclusive of 60w so ACC leftover
                            # holds. Exclusive of 1.85 so Tony
                            # leftover holds. Exclusive of 1.90
                            # so CHIKAI leftover holds. Exclusive
                            # of 2.00× so leftover-sort 2×+ stays.
                            # Exclusive of last=0. A 1.90× climb
                            # stays. A 1.85× climb stays. $85k
                            # stays. $95k stays. 55w stays. 60w
                            # stays. last=0 stays. Under 15m
                            # stays. Do not leftover 1.90×+ or
                            # 1.85×- or $95k+ or $85k- or under
                            # 56w or 60w+. Do not leftover under
                            # 15m / last=0. Do not leftover
                            # Coca-Cola leftover recap / the live
                            # Coca-Cola 1.21× climb / PEEP leftover
                            # recap / FOMO leftover recap / ACC
                            # leftover recap / ACC below $65k /
                            # HOTAIR leftover recap / NIKE leftover
                            # recap / the live NIKE 1.79× climb /
                            # Tony leftover recap / CHIKAI leftover
                            # recap / CHROME 1.72× / BOMB 1.74× /
                            # ATM 1.71× / THEBIGLONG 1.55× / MSTR
                            # 1.40×+ / leftover-sort 2×+ / 26–35w
                            # / 70–79w $70k+. Do not leftover
                            # Claude / Palantir / PONS AI /
                            # employim 1.555× / AGI 64w $171k /
                            # OpenAI / Coca Cola 72w / MrBeast
                            # leftover recap / the live MrBeast
                            # $1.22M climb. Do not leftover-sort
                            # 2×+.
                            or (
                                holders is not None
                                and 56 <= int(holders) <= 59
                                and 85_000 < mcap < 95_000
                                and 1.85 < multiple < 1.90
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                        )
                        else 0
                    )
                    # Live 21:20: LQX $12.5M / 2.80× / created 05:21 sat
                    # #45 after leftover-size required multiple <1.2.
                    # An 8h-old $1.5M+ major is leftover tape, not a
                    # this-window hunt. DICKBUTT $1M / 2.13× stays up.
                    # Live 04:20: WOTF $11.7M / 2.61× / 7h must stay
                    # when it crosses 8h. Do not leftover-sort 2×+.
                    if leftover_size == 0 and is_sol_old_leftover_major(
                        mcap,
                        _parse_ts(card.get("created_at"))
                        or _parse_ts(card.get("first_seen_at")),
                        multiple=multiple,
                    ):
                        leftover_size = 1
                elif chain == "robinhood":
                    # Live 02:00: HOTDOG $6.2M / 1.05× sat #5 after
                    # COST Dex catch-up. Same $1.5M / <1.8 leftover
                    # as Sol. Stay on the desk — do not recap, do
                    # not apply is_historical. KIRKLAND $572k stays.
                    # ROX 4.49× stays.
                    launched = _parse_ts(card.get("created_at"))
                    age_s = (
                        (datetime.now(timezone.utc) - launched).total_seconds()
                        if launched is not None
                        else 0.0
                    )
                    leftover_size = (
                        1
                        if (
                            (multiple < 1.8 and mcap >= 1_500_000)
                            # Live 09:40: BUN $13.2M / 1.87× / 2.8h sat
                            # #5 after crossing the $1.5M / <1.8 cut.
                            # Same $5M / <1.9 leftover as Sol. KIRKLAND
                            # $755k / 1.61× stays. Do not leftover-sort
                            # 2×+ (HA / ROX / 007).
                            or (multiple < 1.9 and mcap >= 5_000_000)
                            # Live 22:14: SHROOM 992w / $3.5M / 1.80× /
                            # 0.81h sat #5 above HARD / WARDOG / BBL
                            # 1.74× / C 2.00×. The $1.5M / <1.8 leftover
                            # missed 1.801. The $5M / <1.9 leftover
                            # missed $3.5M. $1.5M books that tick
                            # 1.80–1.84 after 30m are leftover tape.
                            # BUN 2.74× stays. 007 3.07× stays.
                            # KIRKLAND $755k / 1.61× stays. Do not
                            # leftover-sort 2×+. Do not leftover
                            # $1.5M 1.85×+.
                            or (
                                mcap >= 1_500_000
                                and multiple < 1.85
                                and age_s >= 0.5 * 3600
                            )
                            # Live 02:47: cbBTC $1.12M / 1.0× / $466k
                            # sat #6. Flat $1M+ RH tape is leftover,
                            # not a new hunt. KIRKLAND $755k / 1.61×
                            # stays.
                            or (multiple < 1.2 and mcap >= 1_000_000)
                            # Same approaching-thin $500k flat as Sol
                            # BEAVER. KIRKLAND 1761w / 1.61× stays.
                            or (
                                is_thin_approaching_print(holders, chain)
                                and mcap >= 500_000
                                and multiple < 1.2
                            )
                            # Live 03:20: COPY $44k / 1.00× / 9h and
                            # RIZZLER $36k / 1.00× / 9h sat #8/#9.
                            # Live 04:20: OILCOIN $57k / 1.23× / 9.6h
                            # sat #25. Live 05:00: LIZZZARD $27k /
                            # 1.34× / 9.7h sat #25. Flat 8h+ / <1.4
                            # is leftover. KIRKLAND 1.61× / 4h and
                            # GASOLINU 1.58× stay.
                            or (multiple < 1.4 and age_s >= 8 * 3600)
                            # Live 06:22: KIMCHI / APHYSICA / SYNTHESIS
                            # / WAIFU 1.00–1.03× / 7.3–7.5h sat #19–#22.
                            # Live 07:04: UPPIES $28k / 1.00× / 6.0h
                            # and RECT $45k / 1.00× / 6.3h sat #16/#17
                            # above HA 2.29×. Live 08:00: XPA $36k /
                            # 1.00× / 5.3h sat #11. Flat 5.5h+ / <1.2
                            # is leftover. The 8h / <1.4 clock stays —
                            # CATARM 1.25× waits. WAIFU 1.43× /
                            # KIRKLAND 1.61× / MU 1.59× / HA stay.
                            or (multiple < 1.2 and age_s >= 5.5 * 3600)
                            # Live 12:50: CHAD $47k / 1.22× / $14.5k /
                            # 5.4h sat #10 above BUN 2.74× / 007 3.07×.
                            # 5.5h / <1.2 missed 1.22. Softcake $34k /
                            # 1.24 and CATARM 1.25× wait for 8h / <1.4.
                            # Do not leftover-sort 2×+.
                            or (
                                multiple < 1.24
                                and age_s >= 5.5 * 3600
                                and DEAD_POOL_LIQ <= liq < 16_000
                            )
                            # Live 14:00: CIP $12.1k / 1.00× / 28w /
                            # 0.65h sat #4 above NANOCHICKLET $34k /
                            # TIQS $22k. Skinny is $12k so $70 over
                            # kept a fat chair. RAIN $13.7k stays.
                            # ANUS $22k waits 8h / <1.4. Do not
                            # leftover-sort 2×+.
                            or (
                                multiple < 1.1
                                and DEAD_POOL_LIQ <= liq < 13_000
                                and age_s >= 0.5 * 3600
                            )
                            # Live 14:32: P3NG 104w / $18.5k / 1.00× /
                            # 2.4h sat #8 above JACKET 1.34× / CATPUR
                            # 1.94×. CIP leftover is <$13k so $18.5k
                            # missed. 5.5h / <1.2 waits. Crowded 100w+
                            # still flat after 2h is leftover tape.
                            # CASHBIRD 87w stays. RAIN 83w stays.
                            # ANUS 1.21× / $22k waits 8h / <1.4.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and int(holders) >= 100
                                and multiple < 1.05
                                and age_s >= 2 * 3600
                            )
                            # Live 17:22: VIAGRA 374w / $109k / 1.00× /
                            # 0.9h sat #9 above PLTR 2.19× / GROYPER
                            # 1.61×. The 100w+ / 2h leftover waits
                            # another hour. Crowded 200w+ still flat
                            # under $150k after 45m is leftover tape.
                            # CASHBIRD 87w stays. RAIN 83w stays.
                            # biohacking 185w stays. MINOXIDIL 1.40×
                            # stays. BUN 2.74× / KIRKLAND 1.61× stay.
                            # Do not leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and int(holders) >= 200
                                and 0 < mcap < 150_000
                                and multiple < 1.05
                                and age_s >= 0.75 * 3600
                            )
                            # Live 12:04: PlutoCoin 602w / $111k /
                            # 1.19× / 0.66h sat #3 above DISP 2.32× /
                            # SHIT 2.33× / BIE 2.75×. VIAGRA leftover
                            # needs <1.05. RTD leftover needs <$80k.
                            # A 400w+ $90–120k book still under 1.20
                            # at 45m is leftover tape. VIAGRA 374w
                            # stays. RTD $48k stays. MSTR 220w /
                            # 1.30× stays. SHROOM 3.12× stays.
                            # Batman 2.21× stays. CASHBIRD $41k
                            # stays. Do not leftover 1.20×+ or
                            # $120k+ or below $90k or under 400w.
                            # Do not leftover $93k 1.00× under 45m
                            # (VIAGRA clock). Do not leftover
                            # under 45m. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and int(holders) >= 400
                                and 90_000 <= mcap < 120_000
                                and multiple < 1.20
                                and age_s >= 0.75 * 3600
                            )
                            # Live 00:53: RTD 203w / $48k / 1.19× /
                            # 0.06h sat #1 above FWEF 3.23×. VIAGRA
                            # leftover missed 1.19. biohacking leftover
                            # needs 150w+ / <1.05 / 1h. A 200w+
                            # sub-$80k 1.10–1.19 book at 10m is
                            # leftover tape. MINOXIDIL 145w stays.
                            # CASHBIRD 87w stays. Ladybonercoin 93w
                            # stays. VIAGRA $109k stays. Do not
                            # leftover 1.20×+ or $80k+ or under 200w.
                            # Do not leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and int(holders) >= 200
                                and 0 < mcap < 80_000
                                and multiple < 1.20
                                and age_s >= 10 * 60
                            )
                            # Live 17:43: biohacking 185w / $66k / 1.00× /
                            # 1.2h sat #12 above PLTR 2.19× / GROYPER
                            # 1.61×. The 200w+ / $150k leftover missed
                            # 150–199w. The 100w+ / 2h leftover waits.
                            # CASHBIRD 87w stays. RAIN 83w stays.
                            # MINOXIDIL 1.40× stays. Do not leftover
                            # $80k+ or CASHBIRD. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and int(holders) >= 150
                                and 0 < mcap < 80_000
                                and multiple < 1.05
                                and age_s >= 1 * 3600
                            )
                            # Live 07:40: CRCL 193w / $63k / 1.23× /
                            # 6.3h sat #23 above FWEF 3.23× / SHROOM
                            # 3.00×. 5.5h / <1.2 missed 1.228.
                            # 150w+ leftover needs <1.05. 8h / <1.4
                            # waits. A 180–199w $55–70k book still
                            # under 1.25 at 5.5h is leftover tape.
                            # MINOXIDIL 145w / 1.40× stays. RTD
                            # 203w / 1.39× stays. WARDOG 43w /
                            # 1.23× stays. TOKENIZED 100w stays.
                            # Sol CRCL stays. Do not leftover
                            # 1.25×+ or $70k+ or below $55k or
                            # under 180w or 200w+. Do not leftover
                            # under 5.5h. Do not leftover WARDOG
                            # 1.23×. Do not leftover Sol CRCL.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 180 <= int(holders) <= 199
                                and 55_000 <= mcap < 70_000
                                and multiple < 1.25
                                and age_s >= 5.5 * 3600
                            )
                            # Live 09:20: DISP 182w / $55k / 1.00× /
                            # 0.50h sat #1 above SHIT 2.33× / BIE
                            # 2.75×. biohacking leftover waits 1h /
                            # <$80k. CRCL leftover is $55–70k /
                            # 5.5h. A 180–199w $45–55k flat at
                            # 15m is leftover tape. biohacking
                            # $66k stays. CRCL $71k / 1.38× stays.
                            # AMCST 2.73× stays. MSTR 220w stays.
                            # TOKENIZED 100w stays. Do not leftover
                            # 1.05×+ or $55k+ or below $45k or
                            # under 180w or 200w+. Do not leftover
                            # CRCL $55–70k re-open. Do not leftover
                            # biohacking $66k re-open. Do not
                            # leftover DISP 1.55×. Do not leftover
                            # under 15m. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 180 <= int(holders) <= 199
                                and 45_000 <= mcap < 55_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 18:47: CLAY 50w / $202k / 1.00× /
                            # 0.2h / prepumped sat #1 above PLUTO
                            # 1.25× / PLTR 2.19×. The 200w+ / $150k
                            # leftover missed 50w $200k books. The
                            # 100w+ / 2h leftover waits. CASHBIRD 87w
                            # stays. RAIN 83w stays. MINOXIDIL 1.40×
                            # stays. Do not leftover-sort 2×+ or
                            # CASHBIRD. Do not leftover $150k- 1.05×+.
                            or (
                                multiple < 1.05
                                and mcap >= 150_000
                                and age_s >= 10 * 60
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 03:40: adum $103k / 1.00× / 32w /
                            # 0.27h / prepumped sat #3 above HOJAK
                            # 1.79× / JERKING. CLAY leftover needs
                            # $150k. UD leftover needs 100–149w.
                            # JERKING $68k is not prepumped. HJ $27k
                            # stays. A prepumped $80–150k flat at 15m
                            # is leftover tape. Do not leftover
                            # JERKING. Do not leftover HJ $27k. Do
                            # not leftover $80k 1.05×+ or $150k+ or
                            # under 15m. Do not leftover 26–35w
                            # this-window flats that are not
                            # prepumped. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                80_000 <= mcap < 150_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 23:09: UD 140w / $126k / 1.00× /
                            # 0.26h / prepumped sat #1 above CHROME
                            # 1.72× / FWEF 3.23× / BBL 1.74× / C
                            # 2.00×. CLAY leftover needs $150k. The
                            # 100w+ leftover waits 2h. 150w+ leftover
                            # needs <$80k. MINOXIDIL 145w / 1.40×
                            # stays. CASHBIRD 87w stays. ZOIN $60k
                            # stays. HARD 1.15× stays. WARDOG 1.23×
                            # stays. Do not leftover 1.05×+ or $150k+.
                            # Do not leftover 150w+ (biohacking).
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 100 <= int(holders) <= 149
                                and 0 < mcap < 150_000
                                and multiple < 1.05
                                and age_s >= 10 * 60
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 00:08: MEME 106w / $76k / 1.00× /
                            # 0.03h sat #1 (not prepumped). UD leftover
                            # needs prepumped. The 100w+ leftover
                            # waits 2h. A 100–149w sub-$80k flat at
                            # 15m is leftover tape. MINOXIDIL 145w /
                            # $96k / 1.40× stays. UD $126k stays.
                            # CATGIRL 152w stays. CASHBIRD 87w stays.
                            # Do not leftover 1.05×+ or $80k+ or
                            # 150w+. Do not leftover under 15m.
                            # Do not leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 100 <= int(holders) <= 149
                                and 0 < mcap < 80_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 00:36: Ladybonercoin 93w / $48k /
                            # 1.00× / 0.2h sat #1 above FWEF 3.23×.
                            # The 100w+ leftover waits 2h. New-MEME
                            # leftover needs 100w. CASHBIRD 87w stays.
                            # RAIN 83w stays. CHROME 74w / 1.72×
                            # stays. QUADRILLIONAIRE 51w stays.
                            # MINOXIDIL 145w stays. A 90–99w sub-$50k
                            # flat at 15m is leftover tape. Do not
                            # leftover 1.05×+ or $50k+ or under 90w
                            # or 100w+. Do not leftover-sort 2×+ or
                            # CASHBIRD.
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 99
                                and 0 < mcap < 50_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 06:00: BOB 89w / $40k / 1.00× /
                            # 0.04h sat #1 above ADAMBACK 2.03× /
                            # TAMCT 2.29×. CASHBIRD leftover waits
                            # 2h. Ladybonercoin leftover needs
                            # 90–99w. DUMBMONEY leftover needs
                            # 70–79w / $50–70k. A 80–89w $35–41k
                            # flat at 15m is leftover tape.
                            # CASHBIRD 87w / $23k and $41k stay.
                            # RAIN $14k stays. DUMBMONEY 1.37×
                            # stays. CHROME 74w / 1.72× stays.
                            # Do not leftover 1.05×+ or $41k+ or
                            # below $35k or under 80w or 90w+.
                            # Do not leftover CASHBIRD. Do not
                            # leftover 26–35w. Do not leftover
                            # under 15m. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and 80 <= int(holders) <= 89
                                and 35_000 <= mcap < 41_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 03:40: DUMBMONEY 79w / $55k / 1.00×
                            # / 0.09h sat #1 above AMC 1.79× / HOJAK
                            # 1.79×. CASHBIRD leftover is 80–99w / 2h.
                            # Ladybonercoin leftover is 90–99w / <$50k.
                            # A 70–79w $50–70k flat at 15m is leftover
                            # tape. Do not leftover CASHBIRD 80w+.
                            # Do not leftover JERKING. Do not leftover
                            # $70k+ or below $50k or under 70w or 80w+.
                            # Do not leftover 1.05×+. Do not cut the
                            # 15m clock. Do not leftover-sort 2×+ or
                            # CASHBIRD.
                            or (
                                holders is not None
                                and 70 <= int(holders) <= 79
                                and 50_000 <= mcap < 70_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 08:40: COIN 73w / $30k / 1.00× /
                            # 0.10h sat #1 above BIE 2.27× / AMCST
                            # 2.73×. DUMBMONEY leftover is $50–70k.
                            # USMS leftover is Sol. A 70–79w
                            # $25–40k flat at 15m is leftover tape.
                            # DUMBMONEY $131k stays. CASHBIRD 87w
                            # stays. HOJAK 67w / 1.79× stays. HARD
                            # $41k stays. Do not leftover 1.05×+
                            # or $40k+ or below $25k or under 70w
                            # or 80w+. Do not leftover DUMBMONEY
                            # $50–70k re-open. Do not leftover
                            # 26–35w. Do not leftover under 15m.
                            # Do not leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 70 <= int(holders) <= 79
                                and 25_000 <= mcap < 40_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 23:55: RAD 74w / $24k / 1.00× /
                            # 0.56h sat leftover_size=0 #1.
                            # COIN leftover needs $25–40k.
                            # DUMBMONEY leftover needs $50–70k.
                            # USMS leftover is Sol $60–75k.
                            # A 70–79w $20–25k flat at 20m is
                            # leftover tape. HJ $27k stays on
                            # the COIN band. COIN $30k stays
                            # on its own band. DUMBMONEY $55k
                            # stays. COINCAT stays. A 1.05×
                            # climb stays. Do not leftover
                            # 1.05×+ or $25k+ or below $20k
                            # or under 70w or 80w+. Do not
                            # leftover under 20m. Do not
                            # leftover HJ $27k / COIN $30k /
                            # DUMBMONEY $50–70k / COINCAT /
                            # KEKIUS / CASHBIRD / 70–79w
                            # $70k+. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 70 <= int(holders) <= 79
                                and 20_000 <= mcap < 25_000
                                and multiple < 1.05
                                and age_s >= 20 * 60
                            )
                            # Live 23:55: MDFK 54w / $51k / 1.66× /
                            # 11.8h sat leftover_size=0 #36.
                            # THEINVESTOR leftover needs $250–
                            # 290k / <1.15. HOTAIR leftover
                            # needs $36–40k / last < $25k.
                            # DELIVERY leftover needs 46–49w /
                            # $38–42k. TMB leftover needs 60–
                            # 69w / $45–50k / <1.20. A 50–59w
                            # $48–55k book still 1.60–1.70 at
                            # 6h is leftover tape. A 1.70×
                            # climb stays. ATM 1.71× stays.
                            # LTPT $46k stays. TMB $72k stays.
                            # BEANS $92k stays. BULLROPE $58k
                            # stays. HOTAIR $39k stays. Do not
                            # leftover 1.70×+ or 1.60×- or
                            # $55k+ or below $48k or under 50w
                            # or 60w+. Do not leftover under
                            # 6h. Do not leftover ATM 1.71× /
                            # LTPT $46k / TMB / THEINVESTOR /
                            # HOTAIR / COINCAT / KEKIUS /
                            # NETUSD / CASHBIRD. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 48_000 <= mcap < 55_000
                                and 1.60 <= multiple < 1.70
                                and age_s >= 6 * 3600
                            )
                            # Live 00:46: SHIVONGATE 50w / $44k /
                            # 1.22× / 0.47h sat leftover_size=0
                            # #1 last=$37k. EBGAMES leftover
                            # needs $30–40k / <1.20. AMCCCOIN
                            # leftover needs $40–45k / <1.05.
                            # MDFK leftover needs $48–55k /
                            # 1.60–1.70 / 6h. HOTAIR leftover
                            # needs $36–40k. HARD leftover
                            # stay is $41k / 1.15×. A 50–59w
                            # $42–48k book still 1.18–1.30 at
                            # 20m is leftover tape. A 1.30×
                            # climb stays. HARD $41k / 1.15×
                            # stays. RJACK $46k stays. BEANS
                            # $92k stays. BULLROPE $58k stays.
                            # CUMMIES $59k stays on its own
                            # band. Do not leftover 1.30×+ or
                            # 1.18×- or $48k+ or below $42k
                            # or under 50w or 60w+. Do not
                            # leftover under 20m. Do not
                            # leftover HARD $41k / EBGAMES /
                            # AMCCCOIN / MDFK / HOTAIR /
                            # THEINVESTOR / ATM / TMB /
                            # COINCAT / KEKIUS / CASHBIRD.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 42_000 <= mcap < 48_000
                                and 1.18 <= multiple < 1.30
                                and age_s >= 20 * 60
                            )
                            # Live 00:46: CUMMIES 50w / $59k /
                            # 1.44× / 0.51h sat leftover_size=0
                            # #3. THEINVESTOR leftover needs
                            # $250–290k. MDFK leftover needs
                            # $48–55k / 1.60–1.70. SHIVONGATE
                            # leftover needs $40–48k / 1.15–
                            # 1.30. CALLS leftover is 60–69w.
                            # A 50–59w $55–62k book still
                            # 1.40–1.50 at 20m is leftover
                            # tape. A 1.50× climb stays.
                            # BULLROPE $58k / 2.74× stays.
                            # BEANS $92k stays. TMB $72k
                            # stays. Do not leftover 1.50×+
                            # or 1.40×- or $62k+ or below
                            # $55k or under 50w or 60w+. Do
                            # not leftover under 20m. Do not
                            # leftover THEINVESTOR / MDFK /
                            # SHIVONGATE / BULLROPE / BEANS /
                            # TMB / ATM / COINCAT / KEKIUS /
                            # NETUSD / CASHBIRD. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 55_000 <= mcap < 62_000
                                and 1.40 <= multiple < 1.50
                                and age_s >= 20 * 60
                            )
                            # Live 01:08: jerk 63w / $192k / 1.00× /
                            # 0.20h sat leftover_size=0 last=$108k.
                            # Shapan leftover is Sol $170–200k /
                            # 30m / <1.25. FIGHT leftover needs
                            # $70–90k / <1.10. CALLS leftover
                            # needs $55–65k. ACC leftover is Sol
                            # $65–72k. UNSTABLE $163k stays. A
                            # 60–64w $185–200k book still under
                            # 1.05 at 15m is leftover tape. A
                            # 1.05× climb stays. 59w stays. 65w
                            # stays. Do not leftover 1.05×+ or
                            # $200k+ or below $185k or under 60w
                            # or 65w+. Do not leftover under 15m.
                            # Do not leftover UNSTABLE $163k /
                            # FIGHT / CALLS / ACC / Shapan /
                            # PONS $69k / HOTDOG $111k / TMB /
                            # COINCAT / KEKIUS / CASHBIRD. Do
                            # not leftover 26–35w. Do not
                            # leftover-sort 2×+.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 64
                                and 185_000 <= mcap < 200_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 01:29: BoardAI 94w / $132k / 1.63× /
                            # 0.28h sat leftover_size=0 last=$37k.
                            # HAGGLE leftover needs $40–50k /
                            # 1.80–2.00. MATRIX leftover needs
                            # $105–120k / <1.65 / 60m. Ladybonercoin
                            # leftover needs <$50k / <1.05. KEKIUS
                            # $65k stays. CHROME 97w stays. A
                            # 90–96w $125–145k 1.55–1.70 dump at
                            # 15m after last fell under $40k is
                            # leftover tape. A 1.70× climb stays.
                            # A last $40k+ hold stays. 89w stays.
                            # 97w stays. Do not leftover 1.70×+
                            # or 1.55×- or $145k+ or below $125k
                            # or under 90w or 97w+. Do not leftover
                            # last $40k+. Do not leftover under 15m.
                            # Do not leftover MATRIX $114k / HAGGLE
                            # / KEKIUS $65k / Ladybonercoin /
                            # CHROME 97w / CASHBIRD / Coca Cola
                            # $554k / NVDA $BRRR / jerk 1.21×
                            # $232k. Do not leftover 26–35w. Do
                            # not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 96
                                and 125_000 <= mcap < 145_000
                                and 1.55 <= multiple < 1.70
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0) < 40_000
                            )
                            # Live 23:36: MEME 414w / $368k / 1.29× /
                            # 0.14h / prepumped sat #3 above CHROME
                            # 1.72× / FWEF 3.23× / SHROOM 2.14× /
                            # BBL 1.74× / C 2.00×. CLAY leftover
                            # needs <1.05. VIAGRA leftover needs
                            # <$150k. SHROOM leftover needs <1.85
                            # at $1.5M. A 300w+ $200k 1.29×
                            # prepumped dump is leftover tape.
                            # SHROOM 2.14× stays. BUN 2.74× stays.
                            # 007 3.07× stays. MINOXIDIL 1.40×
                            # stays. CASHBIRD 87w stays. Do not
                            # leftover 1.30×+ or below $200k or
                            # below 300w. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and int(holders) >= 300
                                and mcap >= 200_000
                                and multiple < 1.30
                                and age_s >= 10 * 60
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                            )
                            # Live 04:12: BABYAI 818w / $227k / 1.72× /
                            # 1.35h / prepumped sat #6 above ARCHITECTS
                            # 2.75× / CRCL 3.96×. MEME leftover needs
                            # <1.30 ATH. Last print is back at t0
                            # ($132k). A 700w+ $180–280k prepumped
                            # book that gave the wick back at 1h is
                            # leftover tape. SHROOM 3.00× stays. BUN
                            # 2.74× stays. KIRKLAND $755k stays.
                            # MINOXIDIL 1.40× stays. A 1.10× live
                            # hold stays. Do not leftover $280k+ or
                            # under 700w or under 1h. Do not leftover
                            # JERKING. Do not leftover-sort 2×+ or
                            # CASHBIRD. Do not leftover 26–35w.
                            or (
                                holders is not None
                                and int(holders) >= 700
                                and 180_000 <= mcap < 280_000
                                and age_s >= 1 * 3600
                                and any(
                                    "pre-pumped" in str(flag).lower()
                                    for flag in (card.get("risk_flags") or [])
                                )
                                and (
                                    (
                                        float(card.get("last_mcap") or 0) > 0
                                        and float(card.get("t0_mcap") or 0) > 0
                                        and float(card.get("last_mcap") or 0)
                                        / float(card.get("t0_mcap") or 1)
                                        < 1.10
                                    )
                                    or (
                                        float(card.get("last_mcap") or 0) <= 0
                                        and multiple < 1.10
                                    )
                                )
                            )
                            # Live 19:07: THEROCK 44w / $34k / 1.00× /
                            # 1.8h sat #7 above PLTR 2.19× / GROYPER
                            # 1.61×. Live 19:30: CREATINE 37w / $23k /
                            # 1.00× / 2.5h sat #9 above MINOXIDIL
                            # 1.40× / PLTR 2.19×. The 40–59w leftover
                            # missed 36–39w flats. HJ 33w stays.
                            # BURST 29w stays. DEMENTIA 35w stays.
                            # TAIWAN 34w stays. NANOCHICKLET 39w /
                            # 1.70× stays. MAXXING 36w / 1.55× stays.
                            # CASHBIRD 87w stays. MINOXIDIL 1.40×
                            # stays. Do not leftover-sort 2×+ or
                            # CASHBIRD. Do not leftover 26–35w
                            # HJ/BURST-class.
                            or (
                                holders is not None
                                and 36 <= int(holders) <= 59
                                and multiple < 1.05
                                and age_s >= 1.5 * 3600
                            )
                            # Live 21:55: USO 50w / $34k / 1.00× /
                            # 0.28h sat #1 above SIGMAA / HARD /
                            # BBL 1.74× / C 2.00×. The 36–59w leftover
                            # waits 1.5h. A $34k 1.00× 36–59w flat at
                            # 15m is leftover tape. GOLDCODY 43w /
                            # $43k / 1.02× stays. WARDOG 43w / 1.23×
                            # stays. HARD 52w / $41k / 1.15× stays.
                            # HJ 33w / DEMENTIA 35w / TAIWAN 34w stay.
                            # CASHBIRD 87w stays. Do not leftover
                            # 1.02×+ or $40k+. Do not leftover 26–35w.
                            # Do not lower the 36–59w 1.5h clock
                            # (CHAD / WARDOG / GOLDCODY).
                            or (
                                holders is not None
                                and 36 <= int(holders) <= 59
                                and 0 < mcap < 40_000
                                and multiple < 1.02
                                and age_s >= 15 * 60
                            )
                            # Live 04:40: EBGAMES 53w / $36k / 1.18× /
                            # 0.07h sat #1 above FUD / LTPT 1.63× /
                            # HOJAK 1.79×. USO leftover needs <1.02.
                            # 36–59w leftover waits 1.5h / <1.05. A
                            # 50–59w $30–40k book still under 1.20 at
                            # 15m is leftover tape. HARD $41k / 1.15×
                            # stays. GOLDCODY 43w stays. WARDOG 1.23×
                            # stays. LTPT 1.63× stays. HJ 33w stays.
                            # FUD / JERKING 26–35w stay. Do not
                            # leftover 1.20×+ or $40k+ or under 50w
                            # or 60w+. Do not leftover 26–35w. Do
                            # not cut the USO 15m clock. Do not
                            # lower the 36–59w 1.5h clock. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 30_000 <= mcap < 40_000
                                and multiple < 1.20
                                and age_s >= 15 * 60
                            )
                            # Live 08:00: AMCCCOIN 50w / $41k / 1.00× /
                            # 0.30h sat #2 above AMCST 2.73× / BOB
                            # 2.28×. USO leftover is <$40k. EBGAMES
                            # leftover is $30–40k / <1.20. 36–59w
                            # leftover waits 1.5h / <1.05. A 50–59w
                            # $40–45k flat at 15m is leftover tape.
                            # RJACK $46k stays. QUADRILLIONAIRE
                            # $50k stays. HARD $41k / 1.15× stays.
                            # LOOOOOOOONG $66k stays. EBGAMES $36k
                            # stays on its own band. Do not leftover
                            # 1.05×+ or $45k+ or below $40k or
                            # under 50w or 60w+. Do not leftover
                            # RJACK $46k. Do not leftover 26–35w.
                            # Do not leftover under 15m. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 40_000 <= mcap < 45_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 18:16: THEINVESTOR 56w / $271k /
                            # 1.11× / 0.46h sat #5 last=$241k.
                            # EBGAMES leftover needs $30–40k /
                            # <1.20. AMCCCOIN leftover needs
                            # $40–45k / <1.05. USDCRACK leftover
                            # needs $70–100k / <1.05. ChatGPT
                            # leftover is 45–69w / $500k+. A
                            # 50–59w $250–290k book still under
                            # 1.15 at 15m is leftover tape.
                            # EBGAMES $36k stays. BEANS 2.85×
                            # stays. BULLROPE 2.58× stays.
                            # ChatGPT $500k+ stays. GTA $560k
                            # stays. BABYAI 700w stays. A
                            # 1.15× climb stays. Do not leftover
                            # 1.15×+ or $290k+ or below $250k
                            # or under 50w or 60w+. Do not
                            # leftover under 15m. Do not leftover
                            # EBGAMES / ChatGPT $500k+ / GTA
                            # $560k / BABYAI 700w. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 250_000 <= mcap < 290_000
                                and multiple < 1.15
                                and age_s >= 15 * 60
                            )
                            # Live 18:44: STOCKFATHER 412w / $325k /
                            # 1.35× / 0.47h sat #4. PlutoCoin
                            # leftover needs 400w+ / $90–120k /
                            # <1.20 / 45m. BABYAI leftover needs
                            # 700w+ / $180–280k / prepumped.
                            # TRIPLEP leftover is $70–90k. A
                            # 400–430w $300–350k book still under
                            # 1.40 at 20m is leftover tape. NEKO
                            # 360w stays. BABYAI 700w stays.
                            # PlutoCoin $111k stays. A 1.40×
                            # climb stays. Do not leftover 1.40×+
                            # or $350k+ or below $300k or under
                            # 400w or 431w+. Do not leftover
                            # under 20m. Do not leftover NEKO
                            # 360w / BABYAI 700w / PlutoCoin
                            # $111k / THEINVESTOR $271k. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 400 <= int(holders) <= 430
                                and 300_000 <= mcap < 350_000
                                and multiple < 1.40
                                and age_s >= 20 * 60
                            )
                            # Live 19:09: SHIBA 154w / $82k / 1.39× /
                            # 0.27h sat #5. biohacking leftover
                            # needs 150w+ / <$80k / <1.05 / 1h.
                            # CHROME leftover is Sol $130–150k /
                            # <1.10. STAPLER leftover needs
                            # $55–60k. A 150–159w $80–90k book
                            # still under 1.40 at 15m is leftover
                            # tape. PAIRLESS $97k stays. Long Cat
                            # 169w stays. MarsCoin $151k stays.
                            # A 1.40× climb stays. Do not leftover
                            # 1.40×+ or $90k+ or below $80k or
                            # under 150w or 160w+. Do not leftover
                            # under 15m. Do not leftover PAIRLESS
                            # $97k / Long Cat / MarsCoin / CHROME
                            # 97w / STAPLER. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and 150 <= int(holders) <= 159
                                and 80_000 <= mcap < 90_000
                                and multiple < 1.40
                                and age_s >= 15 * 60
                            )
                            # Live 19:32: HAGGLE 92w / $47k / 1.94× /
                            # 0.54h sat #1 last=$25k. Ladybonercoin
                            # leftover needs <1.05. MATRIX leftover
                            # needs $105–120k. Coca Cola leftover
                            # needs $540–570k. A 90–96w $40–50k
                            # 1.80–2.00 dump at 30m after last
                            # print fell under $40k is leftover
                            # tape. A 2.00× climb stays. A last
                            # $40k+ hold stays. KEKIUS $65k stays.
                            # CHROME 97w stays. CASHBIRD 87w stays.
                            # Do not leftover 2.00×+ or $50k+ or
                            # below $40k or under 90w or 97w+.
                            # Do not leftover under 30m. Do not
                            # leftover last $40k+. Do not leftover
                            # Ladybonercoin / MATRIX / Coca Cola /
                            # KEKIUS / CHROME 97w. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 96
                                and 40_000 <= mcap < 50_000
                                and 1.80 <= multiple < 2.00
                                and age_s >= 30 * 60
                                and 0 < float(card.get("last_mcap") or 0) < 40_000
                            )
                            # Live 05:00: PHOENIX 42w / $49k / 1.085× /
                            # 0.07h sat #1 above ADAMBACK 2.03× / TAMCT
                            # 2.29× / LTPT 1.63×. USO leftover is <$40k.
                            # 36–59w leftover waits 1.5h / <1.05. A
                            # 40–49w $45–55k book still under 1.10 at
                            # 15m is leftover tape. GOLDCODY $43k
                            # stays. WARDOG 1.23× stays. HARD $41k
                            # stays. LTPT 1.63× stays. HJ 33w stays.
                            # FUD / JERKING / MEOWINGCAT 26–35w stay.
                            # Do not leftover 1.10×+ or $55k+ or
                            # below $45k or under 40w or 50w+. Do
                            # not leftover 26–35w. Do not cut the
                            # USO 15m clock. Do not lower the 36–59w
                            # 1.5h clock. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 49
                                and 45_000 <= mcap < 55_000
                                and multiple < 1.10
                                and age_s >= 15 * 60
                            )
                            # Live 07:20: SERIOUSLY 45w / $69k / 1.00× /
                            # 0.40h sat #1 above AMCST 2.73× / BOB
                            # 2.28×. PHOENIX leftover is $45–55k /
                            # <1.10. 36–59w leftover waits 1.5h /
                            # <1.05. A 40–49w $65–75k flat at 15m
                            # is leftover tape. PHOENIX $49k stays
                            # on its own band. GOLDCODY $43k stays.
                            # ZOIN $60k stays. LOOOOOOOONG 50w /
                            # $66k stays. WARDOG 1.23× stays. ATM
                            # 1.71× stays. Do not leftover $55k+
                            # on the PHOENIX band. Do not leftover
                            # 1.05×+ or $75k+ or below $65k or
                            # under 40w or 50w+. Do not leftover
                            # 26–35w. Do not leftover under 15m.
                            # Do not leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 49
                                and 65_000 <= mcap < 75_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 01:40: USDCRACK 50w / $80k / 1.00× /
                            # 0.10h sat #2 above ARCHITECTS 2.75× /
                            # CRCL 3.96×. USO leftover is <$40k. The
                            # 36–59w leftover waits 1.5h.
                            # QUADRILLIONAIRE $50k stays. LOOOOOOOONG
                            # $66k stays. ZOIN $60k stays. HODL $56k
                            # / 1.12× stays. HARD 1.15× stays. WARDOG
                            # 1.23× stays. A 50–59w $70–100k flat at
                            # 15m is leftover tape. Do not leftover
                            # 1.05×+ or below $70k or $100k+ or under
                            # 50w or 60w+. Do not leftover
                            # QUADRILLIONAIRE $50k. Do not leftover
                            # 26–35w. Do not cut the 36–59w 1.5h
                            # clock. Do not leftover-sort 2×+ or
                            # CASHBIRD.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 70_000 <= mcap < 100_000
                                and multiple < 1.05
                                and age_s >= 15 * 60
                            )
                            # Live 23:52: GORILLAA 39w / $31k / 1.00× /
                            # 0.16h sat #1 above CHROME 1.72× / FWEF
                            # 3.23× / BBL 1.74× / C 2.00×. USO leftover
                            # waits 15m and covers $32–$40k. A $31k
                            # 36–59w flat at 10m is leftover tape
                            # below the USO $34k print. GOLDCODY
                            # $43k / 1.02× stays. ZOIN $60k stays.
                            # BOEJACK $42k stays. ALL $38k waits
                            # 15m. HARD 1.15× stays. WARDOG 1.23×
                            # stays. HJ 33w stays. Do not leftover
                            # $32k+ or 1.02×+. Do not leftover
                            # 26–35w. Do not cut the USO 15m clock
                            # for $32k+ books. Do not lower the
                            # 36–59w 1.5h clock (CHAD / WARDOG /
                            # GOLDCODY).
                            or (
                                holders is not None
                                and 36 <= int(holders) <= 59
                                and 0 < mcap < 32_000
                                and multiple < 1.02
                                and age_s >= 10 * 60
                            )
                            # Live 20:11: PLUTO 61w / $37k / 1.25× /
                            # 2.6h sat #6 above BBL 1.74× / C 2.00× /
                            # MINOXIDIL 1.40× / PLTR 2.19×. The 36–59w
                            # leftover missed 60–69w. 5.5h / <1.2 waits.
                            # Softcake 1.24 / CATARM 48w / 1.25× wait
                            # for 8h / <1.4. HJ 33w / DEMENTIA 35w /
                            # TAIWAN 34w stay. CASHBIRD 87w stays.
                            # NANOCHICKLET 39w / 1.70× stays. MAXXING
                            # 36w / 1.55× stays. Do not leftover
                            # 26–35w HJ/BURST-class. Do not leftover
                            # 1.28×+ or $50k+. Do not leftover-sort
                            # 2×+ or CASHBIRD. Do not lower the
                            # 36–59w 1.5h clock (CHAD / WARDOG).
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 0 < mcap < 50_000
                                and multiple < 1.28
                                and age_s >= 2.5 * 3600
                            )
                            # Live 20:58: TCOIN 61w / $39k / 1.00× /
                            # $11k / 0.63h sat #27 among skinny factory
                            # above leftover CHAD. PLUTO 60–69w waits
                            # 2.5h / <1.28. CIP leftover is liq <$13k
                            # so a $12k+ 60w flat keeps a fat chair.
                            # CATARM 48w stays. HJ 33w stays. CASHBIRD
                            # 87w stays. GOLDCODY 43w waits 1.5h.
                            # WARDOG 1.23× stays. Do not leftover
                            # 1.05×+ or $50k+. Do not leftover-sort
                            # 2×+ or CASHBIRD. Do not lower the
                            # 36–59w 1.5h clock (CHAD / WARDOG).
                            # Do not lower the PLUTO 2.5h clock.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 0 < mcap < 50_000
                                and multiple < 1.05
                                and age_s >= 0.5 * 3600
                            )
                            # Live 09:40: HOJAK 67w / $31k / 1.79× /
                            # 6.8h sat #26 above Batman 2.21× / CHROME
                            # 1.72×. PLUTO leftover needs <1.28 at
                            # 2.5h. TCOIN leftover needs <1.05. COIN
                            # leftover is 70–79w. A 60–69w $25–40k
                            # book still under 1.80 at 6h is leftover
                            # tape. Batman 2.21× stays. CASHBIRD 87w
                            # stays. HARD $41k stays. HOJAK under 6h
                            # stays. Do not leftover 1.80×+ or $40k+
                            # or below $25k or under 60w or 70w+. Do
                            # not leftover under 6h. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+ or
                            # CASHBIRD.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 25_000 <= mcap < 40_000
                                and multiple < 1.80
                                and age_s >= 6 * 3600
                            )
                            # Live 11:20: FIGHT 65w / $77k / 1.05× /
                            # 0.3h sat #3 above DISP 2.32× / BIE
                            # 2.75×. PLUTO leftover needs <$50k /
                            # 2.5h. TCOIN leftover needs <$50k.
                            # HOJAK leftover needs $25–40k / 6h.
                            # A 60–69w $70–90k book still under
                            # 1.10 at 15m is leftover tape. Batman
                            # 2.21× stays. HOJAK $31k stays.
                            # CASHBIRD $41k stays. HARD $41k stays.
                            # BOB 2.28× stays. Do not leftover
                            # 1.10×+ or $90k+ or below $70k or
                            # under 60w or 70w+. Do not leftover
                            # HOJAK $25–40k re-open. Do not leftover
                            # 26–35w. Do not leftover under 15m.
                            # Do not leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 70_000 <= mcap < 90_000
                                and multiple < 1.10
                                and age_s >= 15 * 60
                            )
                            # Live 12:21: TMB 64w / $50k / 1.12× /
                            # 0.19h sat #1 above DISP 2.32× / SHIT
                            # 2.33× / BIE 2.75×. TCOIN leftover
                            # needs <1.05 / 30m. PLUTO leftover
                            # waits 2.5h / <1.28. FIGHT leftover
                            # needs $70–90k / <1.10. A 60–69w
                            # $45–50k 1.05–1.20 book at 15m is
                            # leftover tape. TCOIN $39k stays on
                            # its own band. PONS $69k stays.
                            # FIGHT $77k stays on its own band.
                            # HOJAK $31k stays. CASHBIRD $41k
                            # stays. Batman 2.21× stays. Do not
                            # leftover 1.20×+ or $50k+ or below
                            # $45k or under 60w or 70w+. Do not
                            # leftover TCOIN <$50k / <1.05 re-open.
                            # Do not leftover FIGHT $70–90k re-open.
                            # Do not leftover HOJAK $25–40k re-open.
                            # Do not leftover PONS-named / $69k.
                            # Do not leftover under 15m. Do not
                            # leftover 26–35w. Do not leftover-sort
                            # 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 69
                                and 45_000 <= mcap < 50_000
                                and 1.05 < multiple < 1.20
                                and age_s >= 15 * 60
                            )
                            # Live 03:20: AGENTVLAD $37k / 1.00× / 34w /
                            # 4.6h sat #10 and FLACCIDS $38k / 1.00× /
                            # 33w / 4.9h sat #11 above CHROME 1.72× /
                            # FWEF 3.23×. The 36–59w leftover missed
                            # 26–35w. JERKING 31w / $68k / 1.6h stays
                            # (under 4h and $45k+). HJ $27k stays
                            # below $32k. BURST / DEMENTIA / TAIWAN
                            # this-window stay. TRICKSTER 1.52× stays.
                            # HODL 1.12× stays. THEBIGLONG 1.55× stays.
                            # A 26–35w / $32–45k / <1.05 book at 4h is
                            # leftover tape. Do not leftover JERKING.
                            # Do not leftover HJ $27k. Do not leftover
                            # $45k+ or below $32k or 1.05×+. Do not
                            # leftover 26–35w this-window. Do not cut
                            # the 4h clock. Do not leftover-sort 2×+.
                            or (
                                holders is not None
                                and 26 <= int(holders) <= 35
                                and 32_000 <= mcap < 45_000
                                and multiple < 1.05
                                and age_s >= 4 * 3600
                            )
                            # Live 15:21: ROBINRUD 48w / $35k /
                            # 1.18× / 1.53h sat #10. The 36–59w
                            # leftover needs <1.05 at 1.5h. USO
                            # leftover needs <1.02. EBGAMES
                            # leftover needs 50–59w / $30–40k.
                            # PHOENIX leftover needs $45–55k /
                            # <1.10. A 46–49w $30–38k book still
                            # under 1.20 at 1.5h is leftover tape.
                            # WARDOG 1.23× stays. HARD $41k stays.
                            # GOLDCODY $43k stays. HODL $56k stays.
                            # THEBIGLONG 1.55× stays. EBGAMES $36k
                            # stays on its own 50w band. A 1.20×
                            # climb stays. Do not leftover 1.20×+
                            # or $38k+ or below $30k or under 46w
                            # or 50w+. Do not leftover under 1.5h.
                            # Do not lower the 36–59w 1.5h / <1.05
                            # clock. Do not cut the USO 15m clock.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 46 <= int(holders) <= 49
                                and 30_000 <= mcap < 38_000
                                and multiple < 1.20
                                and age_s >= 1.5 * 3600
                            )
                            # Live 18:28: DELIVERY 46w / $39k / 1.68× /
                            # 0.64h sat #9. ROBINRUD leftover needs
                            # $30–38k / <1.20 / 1.5h. PHOENIX
                            # leftover needs $45–55k / <1.10.
                            # 36–59w leftover waits <1.05 at 1.5h.
                            # A 46–49w $38–42k book still in
                            # 1.50–1.70 at 30m is leftover tape.
                            # ROBINRUD $35k / 1.18× stays. HARD
                            # $41k stays. LTPT $46k / 1.63×
                            # stays. ATM 1.71× stays. WARDOG
                            # 1.23× stays. A 1.70× climb stays.
                            # A 1.18× $39k ROBINRUD stay stays.
                            # Do not leftover 1.70×+ or 1.50×-
                            # or $42k+ or below $38k or under
                            # 46w or 50w+. Do not leftover
                            # under 30m. Do not leftover
                            # ROBINRUD / HARD / LTPT 1.63× /
                            # ATM 1.71×. Do not lower the
                            # 36–59w 1.5h / <1.05 clock. Do not
                            # leftover 26–35w. Do not leftover-
                            # sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 46 <= int(holders) <= 49
                                and 38_000 <= mcap < 42_000
                                and 1.50 <= multiple < 1.70
                                and age_s >= 30 * 60
                            )
                            # Live 17:07: ZINGA 43w / $38k / 1.57× /
                            # 3.31h sat #20 last=$27k. ROBINRUD
                            # leftover needs 46–49w / $30–38k /
                            # <1.20. PHOENIX leftover needs
                            # $45–55k / <1.10. 36–59w leftover
                            # waits <1.05 at 1.5h. A 40–45w
                            # $35–40k dump still under 1.58 at
                            # 2.5h after last fell under $30k is
                            # leftover tape. THEBIGLONG 36w /
                            # 1.55× stays. GOLDCODY $43k stays.
                            # HARD $41k stays. WARDOG 1.23×
                            # stays. A last $30k+ hold stays.
                            # A 1.58× climb stays. Do not leftover
                            # 1.58×+ or $40k+ or below $35k or
                            # under 40w or 46w+. Do not leftover
                            # last $30k+. Do not leftover under
                            # 2.5h. Do not leftover THEBIGLONG
                            # 1.55×. Do not lower the 36–59w
                            # 1.5h / <1.05 clock. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 45
                                and 35_000 <= mcap < 40_000
                                and multiple < 1.58
                                and age_s >= 2.5 * 3600
                                and 0 < float(card.get("last_mcap") or 0) < 30_000
                            )
                            # Live 17:30: HOTAIR 58w / $39k /
                            # 1.66× / 3.74h sat #23 last=$21k.
                            # EBGAMES leftover needs 50–59w /
                            # $30–40k / <1.20. TMB leftover is
                            # 60–69w. 36–59w leftover waits
                            # <1.05 at 1.5h. A 55–59w $36–40k
                            # dump still 1.50–1.70 at 3h after
                            # last fell under $25k is leftover
                            # tape. EBGAMES $36k / 1.18× stays.
                            # MDFK 54w / $51k stays. TMB 64w
                            # stays. HARD $41k stays. LTPT
                            # 1.63× stays. THEBIGLONG 1.55×
                            # stays. ATM 1.71× stays. A last
                            # $25k+ hold stays. A 1.70× climb
                            # stays. Do not leftover 1.70×+ or
                            # $40k+ or below $36k or under 55w
                            # or 60w+. Do not leftover last
                            # $25k+. Do not leftover under 3h.
                            # Do not leftover EBGAMES 1.20×+ /
                            # $40k+ / under 50w / 60w+. Do not
                            # leftover THEBIGLONG 1.55× / LTPT
                            # 1.63× / ATM 1.71×. Do not lower
                            # the 36–59w 1.5h / <1.05 clock.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 55 <= int(holders) <= 59
                                and 36_000 <= mcap < 40_000
                                and 1.50 < multiple < 1.70
                                and age_s >= 3 * 3600
                                and 0 < float(card.get("last_mcap") or 0) < 25_000
                            )
                            # Live 17:53: STAPLER 117w / $57k /
                            # 1.46× / 8.8h sat #35 last=$39k.
                            # 8h leftover needs <1.40. SAFEMOON
                            # leftover is 100–109w / $110–130k.
                            # CASHBIRD leftover is 80–99w. A
                            # 115–125w $55–60k book still under
                            # 1.48 at 8h is leftover tape.
                            # MINOXIDIL 145w / 1.40× stays.
                            # CASHBIRD 87w stays. SAFEMOON
                            # 106w stays. HOTAIR 58w stays
                            # on its own band. A 1.48× climb
                            # stays. Do not leftover 1.48×+
                            # or $60k+ or below $55k or under
                            # 115w or 126w+. Do not leftover
                            # under 8h. Do not leftover
                            # MINOXIDIL 1.40× / CASHBIRD /
                            # SAFEMOON recap / HOTAIR recap.
                            # Do not lower the 8h / <1.40
                            # clock. Do not leftover 26–35w.
                            # Do not leftover-sort 2×+ or
                            # CASHBIRD.
                            or (
                                holders is not None
                                and 115 <= int(holders) <= 125
                                and 55_000 <= mcap < 60_000
                                and multiple < 1.48
                                and age_s >= 8 * 3600
                            )
                            # Live 15:41: SAFEMOON 106w / $120k /
                            # 1.38× / 1.96h sat #14 last=$23k.
                            # The 100w+ leftover needs <1.05 at
                            # 2h. New-MEME leftover needs <$80k /
                            # <1.05. A 100–109w $110–130k book
                            # still under 1.40 at 60m is leftover
                            # tape. MINOXIDIL 1.40× stays. UD
                            # $126k stays. CASHBIRD 87w stays.
                            # A 1.40× climb stays. Do not leftover
                            # 1.40×+ or $130k+ or below $110k or
                            # under 100w or 110w+. Do not leftover
                            # under 60m. Do not lower the 100w+
                            # 2h / <1.05 clock. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and 100 <= int(holders) <= 109
                                and 110_000 <= mcap < 130_000
                                and multiple < 1.40
                                and age_s >= 60 * 60
                            )
                            # Live 16:03: MATRIX 95w / $114k /
                            # 1.60× / 1.95h sat #10 last=$27k.
                            # SAFEMOON leftover needs 100w+ /
                            # $110–130k / <1.40. Ladybonercoin
                            # leftover needs <$50k / <1.05. A
                            # 90–99w $105–120k dump still under
                            # 1.65 at 60m after last print fell
                            # under $40k is leftover tape. A
                            # 1.60× hold at last $40k+ stays.
                            # SAFEMOON 106w stays. CASHBIRD 87w
                            # stays. Do not leftover 1.65×+ or
                            # $120k+ or below $105k or under 90w
                            # or 100w+. Do not leftover under
                            # 60m. Do not leftover last $40k+.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 99
                                and 105_000 <= mcap < 120_000
                                and multiple < 1.65
                                and age_s >= 60 * 60
                                and 0 < float(card.get("last_mcap") or 0) < 40_000
                            )
                            # Live 20:24: GEEG 77w / $46k / 1.42× /
                            # 18h sat #67. The 8h leftover needs
                            # <1.40. DUMBMONEY leftover needs
                            # 70–79w / $50–70k / <1.05. COIN
                            # leftover needs $25–40k / <1.05.
                            # COINCAT is 71w / $56k. A 75–79w
                            # $42–50k book still 1.40–1.50 at
                            # 8h is leftover tape. A 1.50× climb
                            # stays. COINCAT 71w stays. ROARY
                            # 70w / $44k stays. KEKIUS $65k
                            # stays. CASHBIRD 87w stays. A 74w
                            # book stays. An 80w book stays.
                            # Do not leftover 1.50×+ or 1.40×-
                            # or $50k+ or below $42k or under
                            # 75w or 80w+. Do not leftover
                            # under 8h. Do not leftover COINCAT
                            # / ROARY $44k / KEKIUS $65k /
                            # CASHBIRD / DUMBMONEY / COIN. Do
                            # not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 75 <= int(holders) <= 79
                                and 42_000 <= mcap < 50_000
                                and 1.40 <= multiple < 1.50
                                and age_s >= 8 * 3600
                            )
                            # Live 20:47: ZB 37w / $41k / 1.00× /
                            # 0.45h sat #1. USO leftover needs
                            # <$40k / <1.02. The 36–59w leftover
                            # waits 1.5h / <1.05. A 36–39w $40–45k
                            # book still under 1.05 at 20m is
                            # leftover tape. GOLDCODY $43k stays.
                            # HARD $41k stays. THEBIGLONG 1.55×
                            # stays. ATM 1.71× stays. WARDOG
                            # 1.23× stays. A 1.05× climb stays.
                            # A 35w book stays. A 40w book stays.
                            # Do not leftover 1.05×+ or $45k+ or
                            # below $40k or under 36w or 40w+.
                            # Do not leftover under 20m. Do not
                            # leftover GOLDCODY / HARD / THEBIGLONG
                            # 1.55× / ATM 1.71× / WARDOG. Do not
                            # lower the 36–59w 1.5h / <1.05 clock.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 36 <= int(holders) <= 39
                                and 40_000 <= mcap < 45_000
                                and multiple < 1.05
                                and age_s >= 20 * 60
                            )
                            # Live 02:33: INSIDER 38w / $29k / 1.07× /
                            # 0.38h sat leftover_size=0 #1. ZB leftover
                            # needs $40–45k / <1.05. The 36–59w leftover
                            # waits 1.5h / <1.05. HJ $27k is 70–79w.
                            # A 36–39w $26–34k book still 1.05–1.15 at
                            # 20m is leftover tape. A 1.15× climb stays.
                            # GOLDCODY $43k stays. HARD $41k stays.
                            # THEBIGLONG 1.55× stays. ATM 1.71× stays.
                            # WARDOG 1.23× stays. HJ $27k stays. A 35w
                            # book stays. A 40w book stays. $25k stays.
                            # Do not leftover 1.15×+ or 1.05×- or $34k+
                            # or below $26k or under 36w or 40w+. Do
                            # not leftover under 20m. Do not leftover
                            # ZB / GOLDCODY / HARD / THEBIGLONG 1.55× /
                            # ATM 1.71× / WARDOG / HJ $27k / GUARD /
                            # UNIFART / NVDIA / BENNY 2.50× / jerk
                            # 1.43×. Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 36 <= int(holders) <= 39
                                and 26_000 <= mcap < 34_000
                                and 1.05 <= multiple < 1.15
                                and age_s >= 20 * 60
                            )
                            # Live 21:11: WAAI 60w / $25k / 1.26× /
                            # 0.24h sat #1. TCOIN leftover needs
                            # <1.05 / 30m. PLUTO leftover waits
                            # 2.5h / <1.28. HOJAK leftover needs
                            # $25–40k / 6h. A 60–64w $22–28k
                            # book still 1.20–1.35 at 15m is
                            # leftover tape. A 1.35× climb stays.
                            # TCOIN $39k stays. HOJAK $31k /
                            # 1.79× under 6h stays. HJ $27k
                            # stays. CALLS $55k stays. FIGHT
                            # $77k stays. PONS $69k stays. A
                            # 59w book stays. A 65w book stays.
                            # Do not leftover 1.35×+ or 1.20×-
                            # or $28k+ or below $22k or under
                            # 60w or 65w+. Do not leftover
                            # under 15m. Do not leftover TCOIN
                            # / HOJAK under 6h / HJ $27k /
                            # CALLS / FIGHT / PONS $69k. Do
                            # not lower the PLUTO 2.5h clock.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 60 <= int(holders) <= 64
                                and 22_000 <= mcap < 28_000
                                and 1.20 <= multiple < 1.35
                                and age_s >= 15 * 60
                            )
                            # Live 22:49: USDC 188w / $122k / 1.49× /
                            # 0.34h sat leftover_size=0 #2 last=$77k.
                            # USELESSTROLL leftover is Sol 180–199w
                            # / $75–90k. CRCL leftover needs 180–
                            # 199w / $55–70k / 5.5h. SHIBA leftover
                            # needs 150–159w / $80–90k. Long Cat
                            # leftover is Sol $130–150k. A 185–195w
                            # $110–130k book still 1.40–1.55 at 15m
                            # is leftover tape. A 1.55× climb stays.
                            # SAFEMOON $120k stays. COINCAT stays.
                            # KEKIUS $65k stays. LASTPONS stays.
                            # Do not leftover 1.55×+ or 1.40×- or
                            # $130k+ or below $110k or under 185w
                            # or 196w+. Do not leftover under 15m.
                            # Do not leftover SAFEMOON $120k /
                            # COINCAT / KEKIUS $65k / FRAMON 3.42×
                            # / LIGERCOIN 3.21×. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and 185 <= int(holders) <= 195
                                and 110_000 <= mcap < 130_000
                                and 1.40 <= multiple < 1.55
                                and age_s >= 15 * 60
                            )
                            # Live 05:40: CATSTRO 50w / $74k / 1.24× /
                            # 30m sat leftover_size=0 #1 last=$74k
                            # liq=$25k. USDCRACK leftover needs
                            # 50–59w / $70–100k / <1.05. CUMMIES
                            # leftover needs $55–62k / 1.40–1.50.
                            # SHIVONGATE leftover needs $42–48k /
                            # 1.18–1.30. EBGAMES leftover needs
                            # $30–40k / <1.20. AMCCCOIN leftover
                            # needs $40–45k / <1.05. A 50–59w
                            # $70–80k book still 1.20–1.30 at 15m
                            # is leftover tape. A 1.30× climb
                            # stays. $69k stays. $80k stays.
                            # HARD $41k stays. TMB 64w / 1.62×
                            # stays. COINCAT 44w stays. PONS $69k
                            # stays. Do not leftover 1.30×+ or
                            # 1.20×- or $80k+ or below $70k or
                            # under 50w or 60w+. Do not leftover
                            # under 15m. Do not leftover USDCRACK
                            # <1.05 / CUMMIES / SHIVONGATE /
                            # EBGAMES / AMCCCOIN / HARD $41k /
                            # TMB / COINCAT / PONS $69k /
                            # THEINVESTOR / KEYCAT / BENNY 2.49×
                            # / NVDA $BRRR. Do not leftover the
                            # last=0 CATSTRO print. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+
                            # or CASHBIRD.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 70_000 <= mcap < 80_000
                                and 1.20 <= multiple < 1.30
                                and age_s >= 15 * 60
                            )
                            # Live 06:20: CATSTRO 50w / $78k / 1.48× /
                            # 70m sat leftover_size=0 #1 last=$78k
                            # liq=$26k max=$88k after leaving the
                            # 1.20–1.30 leftover. USDCRACK leftover
                            # needs <1.05. CUMMIES leftover needs
                            # $55–62k / 1.40–1.50. The 1.20–1.30
                            # leftover needs <1.30. A 50–59w
                            # $70–90k book still 1.40–1.50 at 15m
                            # is leftover tape. A 1.50× climb
                            # stays. $69k stays. $90k stays.
                            # HARD $41k stays. TMB 64w / 1.62×
                            # stays. COINCAT 44w stays. PONS $69k
                            # stays. Do not leftover 1.50×+ or
                            # 1.40×- or $90k+ or below $70k or
                            # under 50w or 60w+. Do not leftover
                            # under 15m. Do not leftover USDCRACK
                            # <1.05 / CUMMIES / the 1.20–1.30
                            # CATSTRO leftover / SHIVONGATE /
                            # EBGAMES / AMCCCOIN / HARD $41k /
                            # TMB / COINCAT / PONS $69k /
                            # THEINVESTOR / KEYCAT / BENNY 2.49×
                            # / NVDA $BRRR / the $2.47M CATSTRO.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 59
                                and 70_000 <= mcap < 90_000
                                and 1.40 <= multiple < 1.50
                                and age_s >= 15 * 60
                            )
                            # Live 07:20: SEXCOIN 43w / $41k / 1.00× /
                            # 22m sat leftover_size=0 #3 last=$21k
                            # liq=$21k max=$40,646. USO leftover
                            # needs <$40k / <1.02. PHOENIX leftover
                            # needs $45–55k / <1.10. AMCCCOIN
                            # leftover needs 50–59w / $40–45k.
                            # GOLDCODY 43w / $43k / 1.02× stays.
                            # HARD 52w / $41k / 1.15× stays.
                            # WARDOG 43w / 1.23× stays. BOEJACK
                            # $42k stays. COINCAT 44w / $89k stays.
                            # A 41–44w $40–42k flat at 15m is
                            # leftover tape. A 1.02× climb stays.
                            # $40k- stays. $42k stays. 40w stays.
                            # 45w stays. Do not leftover 1.02×+
                            # or $42k+ or below $40k or under 41w
                            # or 45w+. Do not leftover under 15m.
                            # Do not leftover USO recap / PHOENIX
                            # / AMCCCOIN / GOLDCODY $43k / HARD
                            # $41k / WARDOG 1.23× / BOEJACK $42k
                            # / COINCAT / KEYCAT / BENNY 2.49× /
                            # NVDA $BRRR / SEXCOlN 1.86× / havesex
                            # 26–35w / SEXCOIN last=0 under 15m.
                            # Do not leftover 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD. Do
                            # not cut the USO 15m clock. Do not
                            # lower the 36–59w 1.5h clock.
                            or (
                                holders is not None
                                and 41 <= int(holders) <= 44
                                and 40_000 <= mcap < 42_000
                                and multiple < 1.02
                                and age_s >= 15 * 60
                            )
                            # Live 07:40: SEXCOlN 67w / $24k / 1.86× /
                            # 40m sat leftover_size=0 #1 last=$24k
                            # liq=$24k max=$64k after the 1.86×
                            # wick dumped. FIGHT leftover needs
                            # $70–90k / <1.10. TMB leftover needs
                            # $45–50k / 1.05–1.20. HOJAK leftover
                            # needs $25–40k / <1.80 / 6h. TCOIN
                            # leftover needs <$50k / <1.05. A
                            # 65–69w $60–70k 1.80–2.00 dump at
                            # 30m after last fell under $40k is
                            # leftover tape. A 2.00× climb stays.
                            # A last $40k+ hold stays. HOJAK
                            # $32k / 1.79× stays. PONS $69k
                            # stays. FIGHT $77k stays. TMB $50k
                            # stays. $59k stays. $70k stays.
                            # 64w stays. 70w stays. 1.80×-
                            # stays. Do not leftover 2.00×+ or
                            # 1.80×- or $70k+ or below $60k or
                            # under 65w or 70w+. Do not leftover
                            # last $40k+. Do not leftover under
                            # 30m. Do not leftover FIGHT / TMB /
                            # HOJAK 1.79× / TCOIN / PONS $69k /
                            # SEXCOIN leftover recap / COINCAT /
                            # KEYCAT / BENNY 2.49× / NVDA $BRRR /
                            # havesex 26–35w. Do not leftover
                            # 26–35w. Do not leftover-sort 2×+
                            # or CASHBIRD. Do not raise PONS
                            # score. Do not cut the HOJAK 6h
                            # clock.
                            or (
                                holders is not None
                                and 65 <= int(holders) <= 69
                                and 60_000 <= mcap < 70_000
                                and 1.80 <= multiple < 2.00
                                and age_s >= 30 * 60
                                and 0 < float(card.get("last_mcap") or 0) < 40_000
                            )
                            # Live 08:20: ASSDAQ 200w / $137k / 1.06× /
                            # 81m sat leftover_size=0 #1 last=$87k
                            # liq=$24k max=$137k. VIAGRA leftover
                            # needs 200w+ / <$150k / <1.05 / 45m.
                            # RTD leftover needs 200w+ / <$80k /
                            # <1.20. PlutoCoin leftover needs 400w+
                            # / $90–120k. 100w+ leftover waits 2h /
                            # <1.05. NVDA $BRRR $119k / 1.59× stays.
                            # MSTR 220w / 1.30× stays. VIAGRA $109k
                            # stays. RTD $48k stays. A 200–219w
                            # $125–145k book still 1.05–1.10 at 60m
                            # is leftover tape. A 1.10× climb stays.
                            # $124k stays. $145k stays. 199w stays.
                            # 220w stays. 1.05×- stays. Do not
                            # leftover 1.10×+ or 1.05×- or $145k+
                            # or below $125k or under 200w or 220w+.
                            # Do not leftover under 60m. Do not
                            # leftover VIAGRA recap / RTD /
                            # PlutoCoin / NVDA $BRRR / MSTR 1.30×
                            # / CASHBIRD / KEYCAT / COINCAT /
                            # BENNY 2.49× / LDX / MOLLIE 2.16× /
                            # SEXCOlN leftover recap / SEXCOIN
                            # leftover recap / 26–35w. Do not
                            # leftover-sort 2×+ or CASHBIRD. Do
                            # not cut the VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 200 <= int(holders) <= 219
                                and 125_000 <= mcap < 145_000
                                and 1.05 < multiple < 1.10
                                and age_s >= 60 * 60
                            )
                            # Live 08:40: BROTHERHOOD 247w / $54k /
                            # 1.22× / 31m sat leftover_size=0 #1
                            # last=$54k liq=$19k max=$54k. RTD
                            # leftover needs 200w+ / <$80k / <1.20.
                            # VIAGRA leftover needs <1.05 / 45m.
                            # ASSDAQ leftover needs 200–219w /
                            # $125–145k / 1.05–1.10. PlutoCoin
                            # leftover needs 400w+. GASOLINU 284w
                            # stays. NVDA $119k stays. WARDOG
                            # 43w / 1.23× stays. A 240–259w $48–
                            # 60k book still 1.20–1.30 at 30m is
                            # leftover tape. A 1.30× climb stays.
                            # $47k stays. $60k stays. 239w stays.
                            # 260w stays. 1.20×- stays. Do not
                            # leftover 1.30×+ or 1.20×- or $60k+
                            # or below $48k or under 240w or 260w+.
                            # Do not leftover under 30m. Do not
                            # leftover RTD / VIAGRA recap /
                            # ASSDAQ leftover recap / PlutoCoin /
                            # GASOLINU 284w / NVDA $BRRR / WARDOG
                            # 1.23× / CASHBIRD / KEYCAT / COINCAT
                            # / BENNY 2.49× / LDX / MOLLIE 2.16× /
                            # 26–35w. Do not leftover-sort 2×+ or
                            # CASHBIRD. Do not add RH 2h / 0.99
                            # (BROTHER / BRAIN). Do not cut the
                            # VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 240 <= int(holders) <= 259
                                and 48_000 <= mcap < 60_000
                                and 1.20 < multiple < 1.30
                                and age_s >= 30 * 60
                            )
                            # Live 09:20: QUOTA 1291w / $212k / 1.14× /
                            # 110m sat leftover_size=0 #1 last=$185k
                            # liq=$40k max=$212k. Not prepumped, so
                            # MEME 300w+ / $200k+ / <1.30 misses.
                            # BABYAI leftover needs 700w+ /
                            # prepumped / last-back. VIAGRA leftover
                            # needs <$150k / <1.05. KEYCAT $272k
                            # stays. NVDA $119k stays. A 1200–
                            # 1399w $200–225k book still under
                            # 1.15 at 60m is leftover tape. A
                            # 1.15× climb stays. $199k stays.
                            # $225k stays. 1199w stays. 1400w
                            # stays. Do not leftover 1.15×+ or
                            # $225k+ or below $200k or under
                            # 1200w or 1400w+. Do not leftover
                            # under 60m. Do not leftover MEME
                            # $200k+ prepumped / BABYAI 700w /
                            # VIAGRA recap / KEYCAT / NVDA $BRRR
                            # / COINCAT / BENNY 2.49× / LDX /
                            # MOLLIE 2.16× / BROTHERHOOD leftover
                            # recap / ASSDAQ leftover recap /
                            # 26–35w. Do not leftover-sort 2×+
                            # or CASHBIRD. Do not add RH 2h /
                            # 0.99 (BROTHER / BRAIN). Do not cut
                            # the VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 1200 <= int(holders) <= 1399
                                and 200_000 <= mcap < 225_000
                                and multiple < 1.15
                                and age_s >= 60 * 60
                            )
                            # Live 12:20: NVIDIH 357w / $42k / 1.50× /
                            # 56m sat leftover_size=0 #0 last=$39k
                            # liq=$17k max=$41,553 mint
                            # 0x05e4…86b0. RTD leftover needs
                            # <1.20. VIAGRA leftover needs <1.05
                            # / 45m. PlutoCoin leftover needs
                            # 400w+ / $90–120k / <1.20. BROTHERHOOD
                            # leftover needs 240–259w / $48–60k.
                            # ASSDAQ leftover needs 200–219w /
                            # $125–145k. HOJAK leftover needs
                            # 60–69w. A 340–379w $35–48k book
                            # still 1.45–1.55 at 45m with a real
                            # last print is leftover tape. A
                            # 1.55× climb stays. A 1.45× climb
                            # stays. $34k stays. $48k stays.
                            # 339w stays. 380w stays. last=0
                            # stays. Do not leftover 1.55×+ or
                            # 1.45×- or $48k+ or below $35k or
                            # under 340w or 380w+. Do not
                            # leftover under 45m. Do not leftover
                            # last=0 / THEBIGLONG 1.55× / ATM
                            # 1.71× / PlutoCoin 1.20×+ / VIAGRA
                            # recap / RTD leftover recap /
                            # BROTHERHOOD leftover recap /
                            # ASSDAQ leftover recap / HOJAK /
                            # FLUID 1.93× / CREO 1.83× / KEYCAT
                            # / NETUSD / QUOTA leftover recap /
                            # leftover-sort 2×+ / 26–35w. Do not
                            # leftover CASHBIRD. Do not add RH
                            # 2h / 0.99 (BROTHER / BRAIN). Do
                            # not cut the VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 340 <= int(holders) <= 379
                                and 35_000 <= mcap < 48_000
                                and 1.45 < multiple < 1.55
                                and age_s >= 45 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 15:20: CYBR 309w / $113k / 1.24× /
                            # 99m sat leftover_size=0 #2 last=$113k
                            # liq=$47k max=$112,997 mint
                            # 0xb948…ac6c. VIAGRA leftover needs
                            # 200w+ / <$150k / <1.05 / 45m. MEME
                            # leftover needs 300w+ / $200k+ /
                            # <1.30 / prepumped. RTD leftover
                            # needs <$80k / <1.20. PlutoCoin
                            # leftover needs 400w+ / $90–120k.
                            # BROTHERHOOD leftover needs 240–
                            # 259w / $48–60k. A 300–319w $100–
                            # 120k book still 1.20–1.30 at 15m
                            # with a real last print is leftover
                            # tape. A 1.30× climb stays. A 1.20×
                            # climb stays. $100k stays. $120k
                            # stays. 299w stays. 320w stays.
                            # last=0 stays. Do not leftover
                            # 1.30×+ or 1.20×- or $120k+ or
                            # $100k- or under 300w or 320w+.
                            # Do not leftover under 15m. Do not
                            # leftover last=0 / VIAGRA recap /
                            # MEME leftover recap / RTD leftover
                            # recap / PlutoCoin leftover recap /
                            # BROTHERHOOD leftover recap /
                            # NVIDIH leftover recap / THEBIGLONG
                            # 1.55× / leftover-sort 2×+ / 26–35w.
                            # Do not leftover CASHBIRD. Do not
                            # add RH 2h / 0.99 (BROTHER / BRAIN).
                            # Do not cut the VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 300 <= int(holders) <= 319
                                and 100_000 < mcap < 120_000
                                and 1.20 < multiple < 1.30
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 15:40: DANGER 173w / $90k / 1.356× /
                            # 3.7h sat leftover_size=0 #3 last=$90k
                            # liq=$26k max=$89,559 mint
                            # 0x3420…b83a. Biohacking leftover needs
                            # 150w+ / <$80k / <1.05 / 1h. VIAGRA
                            # leftover needs 200w+ / <$150k / <1.05
                            # / 45m. CYBR leftover needs 300–319w
                            # / $100–120k / 1.20–1.30. ASSDAQ
                            # leftover needs 200–219w / $125–145k
                            # / 1.05–1.10. USDC leftover needs
                            # 185–195w / $110–130k / 1.40–1.55.
                            # Bufo leftover (Sol) needs $50–65k /
                            # 1.55–1.65. A 165–179w $80–100k book
                            # still 1.30–1.40 at 15m with a real
                            # last print is leftover tape. A 1.40×
                            # climb stays. A 1.30× climb stays.
                            # $80k stays. $100k stays. 164w stays.
                            # 180w stays. last=0 stays. Do not
                            # leftover 1.40×+ or 1.30×- or $100k+
                            # or $80k- or under 165w or 180w+.
                            # Do not leftover under 15m. Do not
                            # leftover last=0 / biohacking recap /
                            # VIAGRA recap / CYBR leftover recap /
                            # ASSDAQ leftover recap / USDC leftover
                            # recap / THEBIGLONG 1.55× /
                            # leftover-sort 2×+ / 26–35w. Do not
                            # leftover CASHBIRD. Do not add RH 2h
                            # / 0.99 (BROTHER / BRAIN). Do not cut
                            # the VIAGRA 45m clock. Do not cut the
                            # biohacking 1h clock.
                            or (
                                holders is not None
                                and 165 <= int(holders) <= 179
                                and 80_000 < mcap < 100_000
                                and 1.30 < multiple < 1.40
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 16:20: WASTED 94w / $85k / 1.317× /
                            # 2.7h sat leftover_size=0 #4 last=$56k
                            # liq=$20k max=$84,650 mint
                            # 0x2bbd…dc99. HAGGLE leftover needs
                            # 90–96w / $40–50k / 2.00× dump.
                            # BoardAI leftover needs 90–96w /
                            # $125–145k / 1.55–1.70. MATRIX
                            # leftover needs 90–99w / $105–120k.
                            # CASHBIRD leftover needs 80–89w /
                            # $35–41k. ZRAC leftover is Sol
                            # 80–84w / $50–65k. A 90–96w $80–90k
                            # book still 1.30–1.40 at 15m with a
                            # real last print is leftover tape. A
                            # 1.40× climb stays. A 1.30× climb
                            # stays. $80k stays. $90k stays. 89w
                            # stays. 97w stays. last=0 stays. Do
                            # not leftover 1.40×+ or 1.30×- or
                            # $90k+ or $80k- or under 90w or
                            # 97w+. Do not leftover under 15m.
                            # Do not leftover last=0 / HAGGLE
                            # leftover recap / BoardAI leftover
                            # recap / MATRIX leftover recap /
                            # CASHBIRD / THEBIGLONG 1.55× /
                            # leftover-sort 2×+ / 26–35w. Do not
                            # leftover CYBR leftover recap /
                            # DANGER leftover recap / ZRAC
                            # leftover recap. Do not add RH 2h /
                            # 0.99 (BROTHER / BRAIN). Do not cut
                            # the VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 96
                                and 80_000 < mcap < 90_000
                                and 1.30 < multiple < 1.40
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 17:00: GPT6 256w / $132k / 1.411× /
                            # 46m sat leftover_size=0 #0 last=$132k
                            # liq=$51k max=$131,756 mint
                            # 0xfc15…5557. BROTHERHOOD leftover
                            # needs 240–259w / $48–60k / 1.20–
                            # 1.30. Pairz leftover needs 235–
                            # 249w / $68–80k. CYBR leftover
                            # needs 300–319w / $100–120k.
                            # NVIDIH leftover needs 340–379w.
                            # ASSDAQ leftover needs 200–219w.
                            # USDC leftover needs 185–195w.
                            # A 250–269w $115–145k book still
                            # 1.35–1.50 at 15m with a real last
                            # print is leftover tape. Exclusive
                            # of $60k so BROTHERHOOD leftover
                            # holds. Exclusive of 249w so Pairz
                            # leftover holds. Exclusive of 1.55×
                            # so THEBIGLONG 1.55× stays. A 1.50×
                            # climb stays. A 1.35× climb stays.
                            # $115k stays. $145k stays. 249w
                            # stays. 270w stays. last=0 stays.
                            # Do not leftover 1.50×+ or 1.35×-
                            # or $145k+ or $115k- or under 250w
                            # or 270w+. Do not leftover under
                            # 15m. Do not leftover last=0 /
                            # BROTHERHOOD leftover recap /
                            # Pairz leftover recap / CYBR
                            # leftover recap / NVIDIH leftover
                            # recap / ASSDAQ leftover recap /
                            # USDC leftover recap / THEBIGLONG
                            # 1.55× / leftover-sort 2×+ /
                            # 26–35w. Do not leftover GPT6A
                            # (Pairz $80k+ stay) / STONKCHUMP
                            # leftover recap / WASTED leftover
                            # recap. Do not add RH 2h / 0.99
                            # (BROTHER / BRAIN). Do not cut the
                            # VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 250 <= int(holders) <= 269
                                and 115_000 < mcap < 145_000
                                and 1.35 < multiple < 1.50
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 18:00: POKEMONZ 94w / $48k /
                            # 1.615× / 23m sat leftover_size=0 #2
                            # last=$39k liq=$36k max=$47,550 mint
                            # 0xa99d…fb71. WASTED leftover needs
                            # 90–96w / $80–90k / 1.30–1.40.
                            # Ladyboner leftover needs 90–99w /
                            # <$50k / <1.05. CASHBIRD leftover
                            # needs 80–89w. BoardAI leftover
                            # needs $125–145k / 1.55–1.70. A
                            # 90–96w $40–55k book still in
                            # 1.58–1.63 at 15m is leftover tape.
                            # Exclusive of 1.55× so THEBIGLONG
                            # 1.55× stays. Exclusive of 1.63×
                            # so LTPT 1.63× stays. Exclusive of
                            # 1.72× so CHROME 1.72× stays. A
                            # 1.63× climb stays. A 1.58× climb
                            # stays. $40k stays. $55k stays.
                            # 89w stays. 97w stays. last=0
                            # stays. Under 15m stays. Do not
                            # leftover 1.63×+ or 1.58×- or
                            # $55k+ or $40k- or under 90w or
                            # 97w+. Do not leftover under 15m /
                            # last=0. Do not leftover WASTED
                            # leftover recap / Ladyboner leftover
                            # recap / CASHBIRD / BoardAI leftover
                            # recap / THEBIGLONG 1.55× / LTPT
                            # 1.63× / CHROME 1.72× / GPT6 leftover
                            # recap / leftover-sort 2×+ / 26–35w.
                            # Do not leftover GTA leftover recap /
                            # AGI leftover recap / the live AGI
                            # 1.089× climb. Do not add RH 2h /
                            # 0.99 (BROTHER / BRAIN).
                            or (
                                holders is not None
                                and 90 <= int(holders) <= 96
                                and 40_000 < mcap < 55_000
                                and 1.58 < multiple < 1.63
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 19:40: WARCAT 348w / $112k /
                            # 1.770× / 21m sat leftover_size=0 #0
                            # last=$52k liq=$19k max=$111,596 mint
                            # 0x3c9a…d921. NVIDIH leftover needs
                            # 340–379w / $35–48k / 1.45–1.55 /
                            # 45m. CYBR leftover needs 300–319w /
                            # $100–120k / 1.20–1.30. PlutoCoin
                            # leftover needs 400w+. GPT6 leftover
                            # needs 250–269w. FROGAS leftover
                            # needs 250–350w / $60–80k. A 345–
                            # 355w $100–125k book still in 1.75–
                            # 1.85 at 15m is leftover tape.
                            # Exclusive of $48k so NVIDIH leftover
                            # holds. Exclusive of 1.55× so
                            # THEBIGLONG 1.55× stays. Exclusive
                            # of 1.63× so LTPT 1.63× stays.
                            # Exclusive of 1.72× so CHROME 1.72×
                            # stays. Exclusive of 1.74× so BOMB
                            # 1.74× stays. Exclusive of 1.71× so
                            # ATM 1.71× stays. Exclusive of 319w
                            # so CYBR leftover holds. Exclusive
                            # of 400w so PlutoCoin leftover holds.
                            # A 1.85× climb stays. A 1.75× climb
                            # stays. $100k stays. $125k stays.
                            # 344w stays. 356w stays. last=0
                            # stays. Under 15m stays. Do not
                            # leftover 1.85×+ or 1.75×- or
                            # $125k+ or $100k- or under 345w or
                            # 356w+. Do not leftover under 15m /
                            # last=0. Do not leftover NVIDIH
                            # leftover recap / CYBR leftover
                            # recap / PlutoCoin leftover recap /
                            # GPT6 leftover recap / FROGAS leftover
                            # recap / CHROME 1.72× / ATM 1.71× /
                            # BOMB 1.74× / THEBIGLONG 1.55× /
                            # LTPT 1.63× / leftover-sort 2×+ /
                            # 26–35w. Do not leftover PONS INU
                            # (PONS-named) / ATLAS 1.39× (RTD
                            # 1.39× stay) / JACKMAN 1.39× (RTD
                            # 1.39× stay) / CHIKAI leftover recap
                            # / MrBeast leftover recap / the live
                            # MrBeast 1.20× climb. Do not add RH
                            # 2h / 0.99 (BROTHER / BRAIN).
                            or (
                                holders is not None
                                and 345 <= int(holders) <= 355
                                and 100_000 < mcap < 125_000
                                and 1.75 < multiple < 1.85
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 20:00: SLOP 203w / $181k /
                            # 1.111× / 60m sat leftover_size=0 #3
                            # last=$127k liq=$27k max=$180,565 mint
                            # 0x0cb7…4b22. ASSDAQ leftover needs
                            # 200–219w / $125–145k / 1.05–1.10 /
                            # 60m. CRC leftover needs 205–215w /
                            # $190–230k. AMC leftover needs 150–
                            # 199w / $170–200k. apecat leftover
                            # needs 200–215w / $50–65k. ironmike
                            # leftover needs <$80k. RTD leftover
                            # needs <$80k / <1.20. A 200–204w
                            # $170–195k book still in 1.10–1.12
                            # at 15m is leftover tape. Exclusive
                            # of $145k so ASSDAQ leftover / $145k
                            # stay holds. Exclusive of 1.10× so
                            # ASSDAQ 1.10× climb / Redbull 1.10×
                            # stay. Exclusive of 1.12× so HODL
                            # 1.12× stays. Exclusive of 199w so
                            # AMC leftover holds. Exclusive of
                            # 205w so CRC leftover holds.
                            # Exclusive of <$80k so ironmike /
                            # RTD leftover hold. A 1.12× climb
                            # stays. A 1.10× climb stays. $170k
                            # stays. $195k stays. 199w stays.
                            # 205w stays. last=0 stays. Under
                            # 15m stays. Do not leftover 1.12×+
                            # or 1.10×- or $195k+ or $170k- or
                            # under 200w or 205w+. Do not leftover
                            # under 15m / last=0. Do not leftover
                            # ASSDAQ leftover recap / CRC leftover
                            # recap / AMC leftover recap / apecat
                            # leftover recap / ironmike $80k+ /
                            # RTD leftover recap / HODL 1.12× /
                            # Redbull 1.10× / WARDOG 1.23× /
                            # leftover-sort 2×+ / 26–35w. Do not
                            # leftover PONS INU (PONS-named) /
                            # ATLAS 1.39× (RTD 1.39× stay) /
                            # JACKMAN 1.39× (RTD 1.39× stay) /
                            # ROI 3.08× (leftover-sort 2×+) /
                            # WARCAT leftover recap / CHIKAI
                            # leftover recap / MrBeast leftover
                            # recap. Do not add RH 2h / 0.99
                            # (BROTHER / BRAIN).
                            or (
                                holders is not None
                                and 200 <= int(holders) <= 204
                                and 170_000 < mcap < 195_000
                                and 1.10 < multiple < 1.12
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 20:20: BIOS 41w / $34k / 1.248× /
                            # 11.3m sat leftover_size=0 #1 last=$26k
                            # liq=$25k max=$34,337 mint
                            # 0x96fa…1e18. ZINGA leftover needs
                            # 40–45w / $35–40k / 1.58+. PHOENIX
                            # leftover needs 40–49w / $45–55k.
                            # AMC leftover needs 40–44w / $850–
                            # 980k. VOF leftover needs 40–44w /
                            # $95–115k. MEMELESS leftover needs
                            # 40–44w / $145–160k. StonkCat leftover
                            # needs 40–44w / $130–145k. INSIDER
                            # leftover needs 36–39w / $26–34k.
                            # A 40–43w $30–35k book still in
                            # 1.24–1.30 at 15m is leftover tape.
                            # Exclusive of $35k so ZINGA leftover
                            # holds. Exclusive of 1.23× so WARDOG
                            # 1.23× stays. Exclusive of 39w so
                            # INSIDER leftover holds. Exclusive
                            # of $45k so PHOENIX leftover holds.
                            # Exclusive of $850k so AMC leftover
                            # holds. A 1.30× climb stays. A 1.24×
                            # climb stays. $30k stays. $35k stays.
                            # 39w stays. 44w stays. last=0 stays.
                            # Under 15m stays. Do not leftover
                            # 1.30×+ or 1.24×- or $35k+ or $30k-
                            # or under 40w or 44w+. Do not leftover
                            # under 15m / last=0. Do not leftover
                            # ZINGA leftover recap / PHOENIX leftover
                            # recap / AMC leftover recap / VOF
                            # leftover recap / MEMELESS leftover
                            # recap / StonkCat leftover recap /
                            # INSIDER leftover recap / WARDOG
                            # 1.23× / HJ $27k / HARD $41k /
                            # leftover-sort 2×+ / 26–35w. Do not
                            # leftover PONS INU (PONS-named) /
                            # ATLAS 1.39× (RTD 1.39× stay) /
                            # JACKMAN 1.39× / BIRK 29w (26–35w) /
                            # AMD 50w (MDFK below $48k stay) /
                            # SLOP leftover recap / WARCAT leftover
                            # recap / sixseven 1.43× (MSTR 1.40×+) /
                            # POINTLESS 26w (26–35w). Do not add
                            # RH 2h / 0.99 (BROTHER / BRAIN).
                            or (
                                holders is not None
                                and 40 <= int(holders) <= 43
                                and 30_000 < mcap < 35_000
                                and 1.24 < multiple < 1.30
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 21:00: Safemoon 333w / $67k /
                            # 1.414× / 3.3h sat leftover_size=0
                            # #4 last=$67k liq=$24k max=$66,703
                            # mint 0x4f30…a6a4. NVIDIH leftover
                            # needs 340–379w / $35–48k / 1.45–
                            # 1.55. CYBR leftover needs 300–
                            # 319w / $100–120k / 1.20–1.30.
                            # WARCAT leftover needs 345–355w /
                            # $100–125k / 1.75–1.85. SAFEMOON
                            # leftover needs 100–109w / $110–
                            # 130k / <1.40. ironmike leftover
                            # needs 200w+ / <$80k / <1.20. A
                            # 328–336w $60–72k book still in
                            # 1.40–1.45 at 15m with a real last
                            # print is leftover tape. Exclusive
                            # of 339w so NVIDIH leftover / 339w
                            # stay holds. Exclusive of 320w so
                            # CYBR leftover holds. Exclusive of
                            # $110k so SAFEMOON leftover holds.
                            # Exclusive of 1.20× so ironmike /
                            # RTD leftover hold. Exclusive of
                            # 1.39× so RTD 1.39× stays.
                            # Exclusive of 1.40× so MSTR 1.40×
                            # stays. Exclusive of last=0. A
                            # 1.45× climb stays. A 1.40× climb
                            # stays. $60k stays. $72k stays.
                            # 327w stays. 337w stays. last=0
                            # stays. Under 15m stays. Do not
                            # leftover 1.45×+ or 1.40×- or
                            # $72k+ or $60k- or under 328w or
                            # 337w+. Do not leftover under 15m
                            # / last=0. Do not leftover NVIDIH
                            # leftover recap / CYBR leftover
                            # recap / WARCAT leftover recap /
                            # SAFEMOON leftover recap / ironmike
                            # leftover recap / RTD leftover recap
                            # / VIAGRA recap / PlutoCoin leftover
                            # recap / RTD 1.39× / DUMBMONEY
                            # 1.37×+ / MSTR 1.40×+ / THEBIGLONG
                            # 1.55× / LTPT 1.63× / CHROME 1.72×
                            # / WARDOG 1.23× / leftover-sort 2×+
                            # / 26–35w. Do not leftover Coca-Cola
                            # leftover recap / the live Coca-Cola
                            # 1.21× climb / BIOS leftover recap /
                            # SLOP leftover recap / PS 39w (ZB
                            # 1.05×+ / INSIDER 1.15×+ / $34k+) /
                            # AMD 50w (MDFK below $48k) / ATLAS
                            # 1.39× / BOMB 1.74× / Heimdall 27w
                            # (26–35w + leftover-sort 2×+) /
                            # PONS INU / snowball 29w / COCAINE
                            # 2.12× / Alibaba 26w. Do not add
                            # RH 2h / 0.99 (BROTHER / BRAIN).
                            # Do not cut the VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 328 <= int(holders) <= 336
                                and 60_000 < mcap < 72_000
                                and 1.40 < multiple < 1.45
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 21:20: God's Eye 117w / $64k /
                            # 1.270× / 6.7m sat leftover_size=0
                            # #0 last=$32k liq=$15k max=$64,402
                            # mint 0x9ec4…8441. The 100–149w /
                            # <$80k / <1.05 leftover-in-waiting
                            # missed 1.25–1.35 after the print
                            # left 1.00×. STAPLER leftover needs
                            # 115–125w / $55–60k / <1.48 / 8h.
                            # SAFEMOON leftover needs 100–109w /
                            # $110–130k / <1.40. Safemoon leftover
                            # needs 328–336w / $60–72k / 1.40–
                            # 1.45. ACC leftover needs 60–64w /
                            # $65–72k. A 115–124w $60–70k book
                            # still in 1.25–1.35 at 15m with a
                            # real last print is leftover tape.
                            # Exclusive of <1.05 so the 100–
                            # 149w leftover-in-waiting holds.
                            # Exclusive of $60k so STAPLER
                            # leftover holds. Exclusive of 109w
                            # so SAFEMOON leftover holds.
                            # Exclusive of 1.23× so WARDOG
                            # 1.23× stays. Exclusive of 1.55×
                            # so THEBIGLONG 1.55× stays.
                            # Exclusive of last=0. A 1.35×
                            # climb stays. A 1.25× climb stays.
                            # $60k stays. $70k stays. 114w
                            # stays. 125w stays. last=0 stays.
                            # Under 15m stays. Do not leftover
                            # 1.35×+ or 1.25×- or $70k+ or
                            # $60k- or under 115w or 125w+.
                            # Do not leftover under 15m /
                            # last=0. Do not leftover the 100–
                            # 149w / <$80k / <1.05 leftover-
                            # in-waiting recap / STAPLER leftover
                            # recap / Safemoon leftover recap /
                            # SAFEMOON leftover recap / ACC
                            # leftover recap / ACC below $65k /
                            # WARDOG 1.23× / HODL 1.12× /
                            # Redbull 1.10× / THEBIGLONG 1.55× /
                            # LTPT 1.63× / CHROME 1.72× / RTD
                            # 1.39× / MSTR 1.40×+ / leftover-sort
                            # 2×+ / 26–35w. Do not leftover
                            # Coca-Cola leftover recap / the live
                            # Coca-Cola 1.21× climb / BIOS leftover
                            # recap / SLOP leftover recap / WARCAT
                            # leftover recap / PS 39w (ZB 1.05×+ /
                            # INSIDER 1.15×+ / $34k+) / AMD 50w
                            # (MDFK below $48k) / Business 1.550×
                            # (THEBIGLONG 1.55×) / Snow Bunny
                            # (ACC below $65k) / PONS INU / BOMB
                            # 1.74× / OpenAI / Coca Cola 72w.
                            # Do not add RH 2h / 0.99 (BROTHER /
                            # BRAIN). Do not cut the VIAGRA 45m
                            # clock.
                            or (
                                holders is not None
                                and 115 <= int(holders) <= 124
                                and 60_000 < mcap < 70_000
                                and 1.25 < multiple < 1.35
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 21:40: Agent OBS 395w / $443k /
                            # 1.418× / 20.5m sat leftover_size=0
                            # #0 last=$418k liq=$60k max=$442,979
                            # mint 0x4736…257a. PUPS leftover
                            # needs 390–399w / $220–250k / 1.60–
                            # 1.80 / last<$160k. MrBeast leftover
                            # needs 385–399w / $110–140k / 1.12–
                            # 1.20. MEME leftover needs 300w+ /
                            # $200k+ / <1.30 / prepumped. Safemoon
                            # leftover needs 328–336w / $60–72k /
                            # 1.40–1.45. A 390–399w $400–480k
                            # book still in 1.40–1.45 at 15m with
                            # a real last print is leftover tape.
                            # Exclusive of $250k so PUPS leftover
                            # holds. Exclusive of $140k so MrBeast
                            # leftover holds. Exclusive of <1.30 /
                            # prepumped so MEME leftover holds.
                            # Exclusive of 336w so Safemoon leftover
                            # holds. Exclusive of 1.40× so MSTR
                            # 1.40× stays. Exclusive of 1.55× so
                            # THEBIGLONG 1.55× stays. Exclusive
                            # of last=0. A 1.45× climb stays. A
                            # 1.40× climb stays. $400k stays.
                            # $480k stays. 389w stays. 400w
                            # stays. last=0 stays. Under 15m
                            # stays. Do not leftover 1.45×+ or
                            # 1.40×- or $480k+ or $400k- or
                            # under 390w or 400w+. Do not leftover
                            # under 15m / last=0. Do not leftover
                            # PUPS leftover recap / MrBeast leftover
                            # recap / the live MrBeast 1.20× climb /
                            # MEME leftover recap / Safemoon leftover
                            # recap / God's Eye leftover recap /
                            # MSTR 1.40×+ / THEBIGLONG 1.55× /
                            # LTPT 1.63× / CHROME 1.72× / RTD
                            # 1.39× / DUMBMONEY 1.37×+ / WARDOG
                            # 1.23× / leftover-sort 2×+ / 26–35w.
                            # Do not leftover Coca-Cola leftover
                            # recap / the live Coca-Cola 1.21×
                            # climb / BIOS leftover recap / SLOP
                            # leftover recap / WARCAT leftover
                            # recap / Palantir (USDCRACK 1.05×+) /
                            # PONS AI (PONS-named) / Business
                            # 1.550× / Snow Bunny (ACC below $65k)
                            # / employim 1.392× (RTD 1.39×) / AGI
                            # 64w $171k (Shapan leftover recap) /
                            # OpenAI / Coca Cola 72w. Do not add
                            # RH 2h / 0.99 (BROTHER / BRAIN). Do
                            # not cut the VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 390 <= int(holders) <= 399
                                and 400_000 < mcap < 480_000
                                and 1.40 < multiple < 1.45
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 22:00: cats@gmail.com 65w / $40k /
                            # 1.376× / 24.2m sat leftover_size=0
                            # #0 last=$36k liq=$33k max=$40,340
                            # mint 0xb19b…de8f. CAB leftover
                            # needs 65–69w / $70–80k / 1.40–
                            # 1.50. TMB leftover needs 60–69w /
                            # $45–50k / 1.05–1.20. ACC leftover
                            # needs 60–64w / $65–72k. A 65–69w
                            # $35–45k book still in 1.37–1.39 at
                            # 15m with a real last print is
                            # leftover tape. Exclusive of $45k
                            # so TMB leftover holds. Exclusive
                            # of $70k so CAB leftover holds.
                            # Exclusive of 64w so ACC leftover
                            # holds. Exclusive of 1.37× so
                            # DUMBMONEY 1.37× stays. Exclusive
                            # of 1.39× so RTD 1.39× stays.
                            # Exclusive of last=0. A 1.39× climb
                            # stays. A 1.37× climb stays. $35k
                            # stays. $45k stays. 64w stays. 70w
                            # stays. last=0 stays. Under 15m
                            # stays. Do not leftover 1.39×+ or
                            # 1.37×- or $45k+ or $35k- or
                            # under 65w or 70w+. Do not leftover
                            # under 15m / last=0. Do not leftover
                            # CAB leftover recap / TMB leftover
                            # recap / ACC leftover recap / ACC
                            # below $65k / DUMBMONEY 1.37×+ /
                            # RTD 1.39× / MSTR 1.40×+ /
                            # THEBIGLONG 1.55× / WARDOG 1.23× /
                            # HODL 1.12× / Redbull 1.10× /
                            # leftover-sort 2×+ / 26–35w.
                            # Do not leftover Agent OBS leftover
                            # recap / God's Eye leftover recap /
                            # Safemoon leftover recap / Coca-Cola
                            # leftover recap / the live Coca-Cola
                            # 1.21× climb / BIOS leftover recap /
                            # SLOP leftover recap / WARCAT leftover
                            # recap / Palantir (USDCRACK 1.05×+) /
                            # PONS AI (PONS-named) / Business
                            # 2.339× / Snow Bunny (ACC below $65k)
                            # / employim 1.555× (THEBIGLONG 1.55×)
                            # / AGI 64w $171k (Shapan leftover
                            # recap) / OpenAI / Coca Cola 72w. Do
                            # not add RH 2h / 0.99 (BROTHER /
                            # BRAIN). Do not cut the VIAGRA 45m
                            # clock.
                            or (
                                holders is not None
                                and 65 <= int(holders) <= 69
                                and 35_000 < mcap < 45_000
                                and 1.37 < multiple < 1.39
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 22:20: Windows 50w / $622k /
                            # 1.383× / 97.3m sat leftover_size=0
                            # #1 last=$551k liq=$68k max=$622,138
                            # mint 0xe741…d70e. ChatGPT leftover
                            # needs 45–69w / $500k+ / <1.20 /
                            # 45m / prepumped. THEINVESTOR
                            # leftover needs 50–59w / $250–
                            # 290k / <1.15. cats leftover needs
                            # 65–69w / $35–45k / 1.37–1.39.
                            # USDCRACK leftover needs 50–59w /
                            # $70–100k / <1.05. A 50–54w
                            # $600–650k book still in 1.37–1.39
                            # at 15m with a real last print is
                            # leftover tape. Exclusive of <1.20
                            # / prepumped so ChatGPT leftover
                            # holds. Exclusive of $290k so
                            # THEINVESTOR leftover holds.
                            # Exclusive of 65w so cats leftover
                            # holds. Exclusive of $100k so
                            # USDCRACK leftover holds.
                            # Exclusive of 1.37× so DUMBMONEY
                            # 1.37× stays. Exclusive of 1.39×
                            # so RTD 1.39× stays. Exclusive of
                            # last=0. A 1.39× climb stays. A
                            # 1.37× climb stays. $600k stays.
                            # $650k stays. 49w stays. 55w
                            # stays. last=0 stays. Under 15m
                            # stays. Do not leftover 1.39×+ or
                            # 1.37×- or $650k+ or $600k- or
                            # under 50w or 55w+. Do not leftover
                            # under 15m / last=0. Do not leftover
                            # ChatGPT leftover recap / ChatGPT
                            # $500k+ / THEINVESTOR leftover
                            # recap / cats leftover recap /
                            # USDCRACK leftover recap / Palantir
                            # (USDCRACK 1.05×+) / DUMBMONEY
                            # 1.37×+ / RTD 1.39× / MSTR 1.40×+
                            # / THEBIGLONG 1.55× / WARDOG 1.23×
                            # / leftover-sort 2×+ / 26–35w.
                            # Do not leftover Agent OBS leftover
                            # recap / God's Eye leftover recap /
                            # Safemoon leftover recap / Coca-Cola
                            # leftover recap / the live Coca-Cola
                            # 1.21× climb / BIOS leftover recap /
                            # SLOP leftover recap / WARCAT leftover
                            # recap / PONS AI (PONS-named) /
                            # Business 2.339× / Snow Bunny (ACC
                            # below $65k) / employim 1.555×
                            # (THEBIGLONG 1.55×) / AGI 64w $171k
                            # (Shapan leftover recap) / OpenAI /
                            # Coca Cola 72w / BILLGATES under 15m
                            # (MSTR 1.40×+ / ironmike $80k+). Do
                            # not add RH 2h / 0.99 (BROTHER /
                            # BRAIN). Do not cut the VIAGRA 45m
                            # clock.
                            or (
                                holders is not None
                                and 50 <= int(holders) <= 54
                                and 600_000 < mcap < 650_000
                                and 1.37 < multiple < 1.39
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 23:00: DELIVERY 161w / $112k /
                            # 1.327× / 21.1m sat leftover_size=0
                            # #0 last=$112k liq=$31k max=$112,289
                            # mint 0x9a88…a1ff. DANGER leftover
                            # needs 165–179w / $80–100k /
                            # 1.30–1.40. Long Cat leftover
                            # needs 160–169w / $130–150k /
                            # <1.32. USDC leftover needs 185–
                            # 195w / $110–130k / 1.40–1.55.
                            # CAT leftover needs 175–185w /
                            # $160–175k. A 160–164w $100–125k
                            # book still in 1.30–1.35 at 15m
                            # with a real last print is leftover
                            # tape. Exclusive of 165w so DANGER
                            # leftover holds. Exclusive of $130k
                            # so Long Cat leftover holds.
                            # Exclusive of 185w so USDC leftover
                            # holds. Exclusive of 1.23× so
                            # WARDOG 1.23× stays. Exclusive of
                            # 1.37× so DUMBMONEY 1.37× stays.
                            # Exclusive of 1.39× so RTD 1.39×
                            # stays. Exclusive of last=0. A
                            # 1.35× climb stays. A 1.30× climb
                            # stays. $100k stays. $125k stays.
                            # 159w stays. 165w stays. last=0
                            # stays. Under 15m stays. Do not
                            # leftover 1.35×+ or 1.30×- or
                            # $125k+ or $100k- or under 160w
                            # or 165w+. Do not leftover under
                            # 15m / last=0. Do not leftover
                            # DANGER leftover recap / Long Cat
                            # leftover recap / USDC leftover
                            # recap / CAT leftover recap /
                            # DELIVERY leftover recap (46–49w)
                            # / WARDOG 1.23× / HODL 1.12× /
                            # Redbull 1.10× / DUMBMONEY 1.37×+
                            # / RTD 1.39× / MSTR 1.40×+ /
                            # THEBIGLONG 1.55× / leftover-sort
                            # 2×+ / 26–35w. Do not leftover
                            # Tony leftover recap / Windows
                            # leftover recap / the live Windows
                            # 1.95× / $877k climb / Agent OBS
                            # leftover recap / the live Agent
                            # OBS 1.851× / $578k climb / cats
                            # leftover recap / God's Eye leftover
                            # recap / Coca-Cola leftover recap /
                            # Claude / Palantir (USDCRACK 1.05×+)
                            # / PONS AI (PONS-named) / BOND
                            # (CASHBIRD) / POOH leftover recap /
                            # employim 1.555× / AGI 64w $171k /
                            # OpenAI / Coca Cola 72w. Do not add
                            # RH 2h / 0.99 (BROTHER / BRAIN). Do
                            # not cut the VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 160 <= int(holders) <= 164
                                and 100_000 < mcap < 125_000
                                and 1.30 < multiple < 1.35
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 23:20: POOH 905w / $662k /
                            # 1.484× / 75.5m sat leftover_size=0
                            # #2 last=$97k liq=$34k max=$662,356
                            # mint 0x0734…d8cd. BABYAI leftover
                            # needs 700w+ / $180–280k /
                            # prepumped / last-back. Windows
                            # leftover needs 50–54w / $600–
                            # 650k / 1.37–1.39. Agent OBS
                            # leftover needs 390–399w / $400–
                            # 480k / 1.40–1.45. NVIDIH leftover
                            # needs 340–379w / $35–48k /
                            # 1.45–1.55. QUOTA leftover needs
                            # 1200–1399w. A 900–909w $650–680k
                            # book still in 1.45–1.50 at 15m
                            # with a real last print is leftover
                            # tape. Exclusive of $650k so
                            # Windows leftover holds. Exclusive
                            # of 1.40× so MSTR 1.40× stays.
                            # Exclusive of 1.45 so Agent OBS
                            # leftover holds. Exclusive of
                            # 1.55× so THEBIGLONG 1.55× stays.
                            # Exclusive of $180–280k /
                            # prepumped so BABYAI leftover
                            # holds. Exclusive of 2.00× so
                            # leftover-sort 2×+ stays.
                            # Exclusive of last=0. A 1.50×
                            # climb stays. A 1.45× climb
                            # stays. $650k stays. $680k stays.
                            # 899w stays. 910w stays. last=0
                            # stays. Under 15m stays. Do not
                            # leftover 1.50×+ or 1.45×- or
                            # $680k+ or $650k- or under 900w
                            # or 910w+. Do not leftover under
                            # 15m / last=0. Do not leftover
                            # BABYAI leftover recap / Windows
                            # leftover recap / the live Windows
                            # 1.95× / $877k climb / Agent OBS
                            # leftover recap / the live Agent
                            # OBS 1.851× / $578k climb / NVIDIH
                            # leftover recap / QUOTA leftover
                            # recap / MSTR 1.40×+ / THEBIGLONG
                            # 1.55× / CHROME 1.72× / BOMB 1.74×
                            # / ATM 1.71× / leftover-sort 2×+
                            # / 26–35w. Do not leftover
                            # DELIVERY leftover recap / cats
                            # leftover recap / Tony leftover
                            # recap / BOND (CASHBIRD) / Palantir
                            # (USDCRACK 1.05×+) / Claude / PONS
                            # AI (PONS-named) / employim 1.555×
                            # / AGI 64w $171k / OpenAI / Coca
                            # Cola 72w. Do not add RH 2h / 0.99
                            # (BROTHER / BRAIN). Do not cut the
                            # VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 900 <= int(holders) <= 909
                                and 650_000 < mcap < 680_000
                                and 1.45 < multiple < 1.50
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 00:00: VECTIS 344w / $504,889 /
                            # 1.091× / 36.7m sat leftover_size=0
                            # #1 last=$500,680 liq=$60,692
                            # max=$504,889 mint 0x7f12… . NVIDIH
                            # leftover needs 340–379w / $35–48k
                            # / 1.45–1.55. Safemoon leftover
                            # needs 328–336w. WARCAT leftover
                            # needs 345–355w. Agent OBS leftover
                            # needs 390–399w / $400–480k /
                            # 1.40–1.45. ChatGPT leftover needs
                            # 45–69w / $450–600k / <1.08 /
                            # prepumped. MEME leftover needs
                            # 300w+ / $200k+ / <1.30 /
                            # prepumped. Windows leftover needs
                            # 50–54w / $600–650k. A 340–344w
                            # $480–520k book still in 1.08–1.10
                            # at 15m with a real last print is
                            # leftover tape. Exclusive of $48k
                            # so NVIDIH leftover holds.
                            # Exclusive of 328–336w so Safemoon
                            # leftover holds. Exclusive of 345w
                            # so WARCAT leftover holds.
                            # Exclusive of 390–399w / $480k so
                            # Agent OBS leftover holds.
                            # Exclusive of 45–69w / <1.08 /
                            # prepumped so ChatGPT leftover
                            # holds. Exclusive of prepumped so
                            # MEME leftover holds. Exclusive of
                            # 1.10× so Redbull 1.10× stays.
                            # Exclusive of 1.12× so HODL 1.12×
                            # stays. Exclusive of $600–650k so
                            # Windows leftover holds. Exclusive
                            # of 2.00× so leftover-sort 2×+
                            # stays. Exclusive of last=0. A
                            # 1.10× climb stays. A 1.08× climb
                            # stays. $480k stays. $520k stays.
                            # 339w stays. 345w stays. last=0
                            # stays. Under 15m stays. Do not
                            # leftover 1.10×+ or 1.08×- or
                            # $520k+ or $480k- or under 340w
                            # or 345w+. Do not leftover under
                            # 15m / last=0. Do not leftover
                            # NVIDIH leftover recap / Safemoon
                            # leftover recap / WARCAT leftover
                            # recap / Agent OBS leftover recap
                            # / the live Agent OBS 1.851× /
                            # $578k climb / ChatGPT leftover
                            # recap / MEME leftover recap /
                            # Windows leftover recap / the
                            # live Windows 1.95× / $877k climb
                            # / HODL 1.12× / Redbull 1.10× /
                            # leftover-sort 2×+ / 26–35w. Do
                            # not leftover DELIVERY leftover
                            # recap / POOH leftover recap /
                            # BOND (CASHBIRD) / Palantir
                            # (USDCRACK 1.05×+) / Claude /
                            # PONS AI (PONS-named) / employim
                            # 1.555× / AGI 64w $171k / OpenAI
                            # / Coca Cola 72w / TIKTOK. Do
                            # not leftover BONS leftover-in-
                            # waiting (MDFK 1.60×-) / TegBots
                            # leftover-in-waiting (100–149w /
                            # <$80k / <1.05). Do not add RH
                            # 2h / 0.99 (BROTHER / BRAIN). Do
                            # not cut the VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 340 <= int(holders) <= 344
                                and 480_000 < mcap < 520_000
                                and 1.08 < multiple < 1.10
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 00:40: BELL 513w / $899,206 /
                            # 1.124× / 20.4m sat leftover_size=0
                            # #0 last=$833k liq=$84k max=$899,206
                            # mint 0x218d…76b0. Tony leftover
                            # needs 495–504w / $120–135k /
                            # 1.80–1.85. Fruit Fly leftover
                            # needs 610–624w. AMC leftover
                            # needs 40–44w / $850–980k /
                            # <1.10. GTA leftover needs 21–
                            # 24w / $900–980k. VOF leftover
                            # needs $900–980k. Puggle $901k
                            # stays. FOMO $1.01M stays.
                            # ChatGPT leftover needs 45–69w
                            # / $450–600k / prepumped.
                            # Windows leftover needs 50–54w
                            # / $600–650k. VECTIS leftover
                            # needs 340–344w / $480–520k /
                            # 1.08–1.10. A 510–519w $880–
                            # 900k book still in 1.12–1.15
                            # at 15m with a real last print
                            # is leftover tape. Exclusive of
                            # 495–504w so Tony leftover
                            # holds. Exclusive of 610–624w
                            # so Fruit Fly leftover holds.
                            # Exclusive of 40–44w so AMC
                            # leftover holds. Exclusive of
                            # $900k so GTA / VOF leftovers
                            # hold and Puggle $901k stays.
                            # Exclusive of $1.01M so FOMO
                            # $1.01M stays. Exclusive of
                            # 1.12× so HODL 1.12× stays.
                            # Exclusive of 1.10× so Redbull
                            # 1.10× stays. Exclusive of
                            # 1.15× so a 1.15× climb stays.
                            # Exclusive of prepumped so
                            # ChatGPT / MEME leftovers hold.
                            # Exclusive of 2.00× so leftover-
                            # sort 2×+ stays. Exclusive of
                            # last=0. A 1.15× climb stays.
                            # A 1.12× climb stays. $880k
                            # stays. $900k stays. 509w
                            # stays. 520w stays. last=0
                            # stays. Under 15m stays. Do
                            # not leftover 1.15×+ or 1.12×-
                            # or $900k+ or $880k- or under
                            # 510w or 520w+. Do not leftover
                            # under 15m / last=0. Do not
                            # leftover Tony leftover recap /
                            # Fruit Fly leftover recap / AMC
                            # leftover recap / GTA leftover
                            # recap / VOF leftover recap /
                            # VECTIS leftover recap / the
                            # live VECTIS 1.114× climb /
                            # ChatGPT leftover recap /
                            # Windows leftover recap / the
                            # live Windows 1.95× / $877k
                            # climb / Agent OBS leftover
                            # recap / HODL 1.12× / Redbull
                            # 1.10× / Puggle $901k / FOMO
                            # $1.01M / leftover-sort 2×+ /
                            # 26–35w. Do not leftover
                            # TegBots leftover-in-waiting /
                            # BONS leftover-in-waiting /
                            # $TPS leftover-in-waiting /
                            # DELIVERY leftover recap /
                            # BOND (CASHBIRD) / Claude /
                            # PONS AI (PONS-named) /
                            # employim 1.555× / OpenAI /
                            # Coca Cola 72w / TIKTOK. Do
                            # not leftover $900k 1.10×+
                            # on the 70–99w band. Do not
                            # add RH 2h / 0.99 (BROTHER /
                            # BRAIN). Do not cut the
                            # VIAGRA 45m clock.
                            or (
                                holders is not None
                                and 510 <= int(holders) <= 519
                                and 880_000 < mcap < 900_000
                                and 1.12 < multiple < 1.15
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 02:40: AD 1123w / $618,837 /
                            # 1.525× / 70.3m sat leftover_size=0
                            # #0 last=$619k liq=$73k max=
                            # $618,837. Exclusive of 50–54w /
                            # $600–650k so Windows leftover
                            # holds. Exclusive of 900–909w /
                            # $650–680k / 1.45–1.50 so POOH
                            # leftover holds. Exclusive of
                            # 1.55× so THEBIGLONG 1.55×
                            # stays. Exclusive of 1.50× so
                            # POOH 1.50× stay holds.
                            # Exclusive of 510–519w so BELL
                            # leftover holds. Exclusive of
                            # 340–344w so VECTIS leftover
                            # holds. Exclusive of last=0.
                            # A 1.55× climb stays. A 1.50×
                            # climb stays. $600k stays.
                            # $630k stays. 1119w stays.
                            # 1130w stays. last=0 stays.
                            # Under 15m stays. Do not
                            # leftover 1.55×+ or 1.50×- or
                            # $630k+ or $600k- or under
                            # 1120w or 1130w+. Do not
                            # leftover under 15m / last=0.
                            # Do not leftover Windows leftover
                            # recap / the live Windows 1.95× /
                            # $877k climb / POOH leftover recap
                            # / BELL leftover recap / the live
                            # BELL $1.01M / 1.265× climb /
                            # VECTIS leftover recap / the live
                            # VECTIS 1.130× climb / THEBIGLONG
                            # 1.55× / MSTR 1.40×+ / leftover-
                            # sort 2×+ / 26–35w. Do not leftover
                            # QARC leftover-in-waiting (MDFK
                            # 50–59w / $48–55k) / ZCAT leftover-
                            # in-waiting (INSIDER 36–39w) /
                            # TegBots leftover-in-waiting /
                            # BONS leftover-in-waiting /
                            # DELIVERY leftover recap / BOND
                            # (CASHBIRD) / Agent OBS leftover
                            # recap / FOMO $1.01M.
                            or (
                                holders is not None
                                and 1120 <= int(holders) <= 1129
                                and 600_000 < mcap < 630_000
                                and 1.50 < multiple < 1.55
                                and age_s >= 15 * 60
                                and 0 < float(card.get("last_mcap") or 0)
                            )
                            # Live 05:40: ERM $37k / 1.85× / 1w / 8.7h
                            # sat #30 above this-window CORNDOG 3w.
                            # Empty-map fat books rank above thin, so
                            # an 8h 1-wallet stub keeps a crowded-band
                            # chair. Do not leftover-sort 2×+ (HA 2.29
                            # / DOGE-1 stay). GASOLINU 284w stays.
                            or (
                                multiple < 2.0
                                and age_s >= 8 * 3600
                                and empty_map == 1
                            )
                            # Live 06:42: SSD $23k / 1.00× / 1w / 6.3h
                            # sat #22 above this-window TCM 5w.
                            # Empty-map ranks above thin, so a 6h
                            # 1-wallet stub keeps a crowded-band
                            # chair. The 8h / <2.0 empty-map clock
                            # stays for ERM-class 1.85×. Do not
                            # leftover-sort 2×+.
                            or (
                                multiple < 1.2
                                and age_s >= 6 * 3600
                                and empty_map == 1
                            )
                        )
                        else 0
                    )
                t = _card_ts(card)
                # Live 08:20: BUN $7.1M / 1.01× / 1.6h / 1186w sat
                # #160 under 140 three-wallet $20k stubs, and NUDES
                # $12M / 0.4h sat #68 under skinny $2k prints,
                # because leftover-size ranked below every
                # this-window row. Aged leftovers (5.5h+ XPA /
                # PORNHUB / RICHDEBT) stay below this-window thin.
                # This-window size leftovers ($1.5M / fat-LP / 2w)
                # sit after fat hunts and above factory stubs.
                # Live 09:00: oUni $3.9M / 2w / 0.5h sat #124 and
                # nvda $11.6M / 5w sat #125 under RH factory because
                # holders≤25 kept thin=1 after leftover-size. Sol
                # microduck $8.4M / $1.8k and MCX $11.5M sat #84/#85
                # with HOTDOG skinny because skinny ranked before
                # size leftover. Clear factory bits on this-window
                # size leftovers only. Aged leftovers stay below
                # this-window thin. HIERO 1.61× / WAIFU stay.
                aged_leftover = (
                    1 if leftover_size == 1 and age_s >= 5.5 * 3600 else 0
                )
                size_leftover = (
                    1 if leftover_size == 1 and aged_leftover == 0 else 0
                )
                if size_leftover:
                    skinny = 0
                    thin = 0
                    empty_map = 0
                # Live 16:06: CASHBIRD 87w / $23k / 1.00× fell off limit=200
                # under factory recency. Live 16:41: crowded_rh=0 before
                # leftover put RAIN / CASHBIRD / HORNY 10.6h at #1–#3
                # above GROYPER 1.61×. Promote 80–99w / $12k+ / <1.05 /
                # 2h+ / <8h leftovers to this-window size leftover so
                # they sit after fat hunts and above factory. HORNY 8h+
                # stays aged leftover. RAIN 3.9h is not leftover —
                # recency among fat books. Do not leftover-sort CASHBIRD
                # off the hunt. P3NG 100w+ / COPY stay leftover tape.
                if (
                    chain == "robinhood"
                    and leftover_size == 1
                    and holders is not None
                    and 80 <= int(holders) < 100
                    and float(card.get("last_liq") or 0) >= SKINNY_HUNT_LIQ
                    and multiple < 1.05
                    and age_s >= 2 * 3600
                    and age_s < 8 * 3600
                ):
                    aged_leftover = 0
                    size_leftover = 1
                    skinny = 0
                    thin = 0
                    empty_map = 0
                return (
                    artifact,
                    real,
                    already_won,
                    airdrop,
                    stale,
                    aged_leftover,
                    skinny,
                    thin,
                    empty_map,
                    size_leftover,
                    -(t.timestamp()) if t else 0.0,
                )

            cards.sort(key=_desk_key)
            # Live 12:40 Sol: four pippo mints occupied #1/#2/#5/#17.
            # Live 14:32 RH: two MOSH ($5.5k / $20k) sat on /rh.
            # Same-ticker flood is a copycat wave. Keep one chair —
            # the fattest real book — without the both-needle copycat
            # filter (nekomaru same-ticker-only stays scored).
            picked: dict[str, dict] = {}
            unnamed: list[dict] = []
            for card in cards:
                sym = (card.get("symbol") or "").strip().lower()
                if not sym:
                    unnamed.append(card)
                    continue
                prev = picked.get(sym)
                if prev is None:
                    picked[sym] = card
                    continue

                def _ticker_key(c: dict) -> tuple:
                    key = _desk_key(c)
                    return key[:-1] + (-float(c.get("last_liq") or 0),)

                if _ticker_key(card) < _ticker_key(prev):
                    picked[sym] = card
            cards = list(picked.values()) + unnamed
            cards.sort(key=_desk_key)
        if historical is not True and chain == "sol":
            from .scoring.outcomes import cap_sol_live_hunt_fat_leftovers

            cards = cap_sol_live_hunt_fat_leftovers(cards)
        return cards[:limit]


@app.get("/api/tokens/{mint}/tape")
async def get_token_tape(mint: str, hours: float = Query(48.0, ge=1.0, le=168.0)):
    """Our own one-minute bars + stored snapshots for the desk tape strip.

    Dex embeds show "No data here" on fresh pairs; this is what we recorded.
    """
    from .ledger import tape_series

    def _run():
        keys = [mint] + ([mint.lower()] if mint.startswith("0x") and mint.lower() != mint else [])
        with session_scope() as session:
            for key in keys:
                token = session.query(Token).filter(Token.mint == key).one_or_none()
                if token:
                    return tape_series(session, token, hours=hours)
        return None

    out = await asyncio.to_thread(_run)
    if out is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return out


@app.get("/api/tokens/{mint}")
async def get_token(mint: str):
    from .research.dexscreener import is_dex_pair_id, pair_market

    lookups = [mint]
    if mint.startswith("0x"):
        lowered = mint.lower()
        if lowered not in lookups:
            lookups.append(lowered)

    async def _lookup(keys: list[str]):
        with session_scope() as session:
            for key in keys:
                token = session.query(Token).filter(Token.mint == key).one_or_none()
                if token:
                    # Live THEINVESTOR: 7h old, 98 holders, still
                    # "No wallet map yet" because RH never looked up
                    # addresses. Fill on the detail read (Blockscout,
                    # not extra GMGN).
                    if token_chain(token) == "robinhood":
                        from .research.holders import hydrate_rh_wallet_map

                        await hydrate_rh_wallet_map(session, token)
                    card = token_detail(token)
                    try:
                        from .scoring.meme_quality import meme_quality_for_token

                        card["meme_quality"] = meme_quality_for_token(session, token)
                    except Exception:
                        log.exception("meme_quality for %s", token.mint)
                        card["meme_quality"] = None
                    try:
                        from .models import PaperFill
                        from .scoring.hold_conviction import hold_conviction_for_fill
                        from .scoring.paper_v1 import PAPER_V1_LINE

                        v1_open = (
                            session.query(PaperFill)
                            .filter(
                                PaperFill.chain == token_chain(token),
                                PaperFill.mint == token.mint,
                                PaperFill.line == PAPER_V1_LINE,
                                PaperFill.status == "open",
                            )
                            .order_by(PaperFill.id.desc())
                            .first()
                        )
                        if v1_open is not None:
                            card["hold_conviction"] = hold_conviction_for_fill(
                                session, token, v1_open, live_p=None
                            )
                    except Exception:
                        log.exception("hold_conviction for %s", token.mint)
                    if token_chain(token) == "robinhood":
                        n_train = get_or_create_model(session, chain="robinhood").n_train
                        apply_young_rh_desk_floor(card, n_train)
                    apply_stall_honesty(card)
                    apply_ath_dump_honesty(card)
                    try:
                        from .ledger import ledger_context

                        card["ledger"] = ledger_context(session, token)
                    except Exception:
                        log.exception("ledger context failed for %s", token.mint)
                        card["ledger"] = None
                    return card
        return None

    card = await _lookup(lookups)
    if card:
        return card
    if is_dex_pair_id(mint):
        # User pasted a DexScreener / Uniswap V4 pair id (BONER's HIMS book).
        resolved = await pair_market(mint, chain="robinhood")
        base = (resolved.get("mint") or "").lower()
        if base:
            card = await _lookup([base])
            if card:
                return card
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/early-wallets")
async def early_wallets(chain: str = Query("sol"), limit: int = Query(100, ge=1, le=200)):
    """Wallets that bought in the first hour of analytics runners.

    Helius parsed swaps. No extra GMGN HTTP. ZCAT is the first seeded book.
    """
    from .research.early_wallets import list_early_wallets

    chain = normalize_chain(chain)
    with session_scope() as session:
        return list_early_wallets(session, chain, limit=limit)


@app.get("/api/fomo-trending")
async def fomo_trending(refresh: bool = Query(False)):
    """FOMO Tokens→Trending vs the desk. Sol + RH only. No extra GMGN.

    ``refresh=true`` forces an hourly-style audit stamp (Learn sanity check).
    """
    from .research.fomo_coverage import fomo_trending_coverage, run_fomo_trending_audit

    with session_scope() as session:
        if refresh:
            await run_fomo_trending_audit(session, force=True)
        return await fomo_trending_coverage(session)


@app.get("/api/fomo-trending/audit")
async def fomo_trending_audit(force: bool = Query(False)):
    """Run or read the hourly FOMO trending sanity audit. Paper/learn only."""
    from .research.fomo_coverage import load_fomo_trending_audit, run_fomo_trending_audit

    with session_scope() as session:
        out = await run_fomo_trending_audit(session, force=force)
        if out.get("skipped"):
            return {
                "ok": True,
                "skipped": True,
                "last_audit": load_fomo_trending_audit(session),
            }
        return out


@app.get("/api/fomo-alerts/flow")
async def fomo_alerts_flow(limit: int = Query(100, ge=1, le=500)):
    """Today's keyed FOMO /ws/alerts social flow (Sol + RH). Learn only — no fills."""
    from .db import apply_report_guards
    from .research.fomo_alerts_flow import flow_digest, recent_flow_events

    with session_scope() as session:
        apply_report_guards(session)
        digest = flow_digest(session)
        digest["recent"] = recent_flow_events(session, limit=limit)
        return digest


@app.get("/api/fomo-alerts/recent")
async def fomo_alerts_recent(limit: int = Query(100, ge=1, le=500)):
    """Recent persisted FOMO alert events for today (UTC)."""
    from .db import apply_report_guards
    from .research.fomo_alerts_flow import recent_flow_events

    with session_scope() as session:
        apply_report_guards(session)
        return {"items": recent_flow_events(session, limit=limit)}


@app.get("/api/fomo-alerts/traders")
async def fomo_alerts_traders(
    limit: int = Query(50, ge=1, le=200),
    days: int = Query(7, ge=1, le=90),
    min_buys: int = Query(5, ge=1, le=50),
    trader: str = Query(""),
    user_id: str = Query(""),
):
    """Ranked FOMO trader scorecard from /ws/alerts buys. Learn only — no fills."""
    from .db import apply_report_guards
    from .research.fomo_trader_scorecard import build_trader_scorecard, trader_scorecard_detail

    with session_scope() as session:
        apply_report_guards(session)
        if trader.strip() or user_id.strip():
            detail = trader_scorecard_detail(session, trader=trader, user_id=user_id, days=days)
            if detail is None:
                return {"ok": True, "trader": None, "note": "no matching trader in window"}
            return {"ok": True, "trader": detail}
        card = build_trader_scorecard(session, days=days, min_buys=min_buys)
        card["traders"] = card["traders"][:limit]
        return card


@app.get("/api/fomo-wallets")
async def fomo_wallets(chain: str = Query("sol"), limit: int = Query(100, ge=1, le=200)):
    """Repeat holders across the top 100 confirmed 5×+ runners.

    Built from stored Helius/Blockscout wallet maps. No extra GMGN HTTP.
    Creator and pool addresses are excluded.
    """
    from .research.fomo_wallets import list_fomo_wallets

    chain = normalize_chain(chain)
    with session_scope() as session:
        return list_fomo_wallets(session, chain, limit=limit)


@app.get("/api/wallets")
async def scored_wallets(chain: str = Query("sol"), limit: int = Query(100, ge=1, le=200)):
    """Unified Early + FOMO + alpha score. Stored maps only, no extra GMGN."""
    from .research.wallet_score import list_scored_wallets

    chain = normalize_chain(chain)
    with session_scope() as session:
        return list_scored_wallets(session, chain, limit=limit)


@app.get("/api/wallets/{owner}")
async def scored_wallet(owner: str, chain: str = Query("sol")):
    """Score one wallet from stored Early / FOMO / alpha / creator rows."""
    from .research.wallet_score import score_wallet

    chain = normalize_chain(chain)
    with session_scope() as session:
        return score_wallet(session, owner, chain)


@app.get("/api/model")
async def get_model(chain: str = Query("sol")):
    return await asyncio.to_thread(_get_model_sync, chain)


def _get_model_sync(chain: str):
    from .scoring.model import evaluate

    chain = normalize_chain(chain)
    with session_scope() as session:
        card = model_card(session, chain=chain)
        card["evaluation"] = evaluate(session, chain=chain)
        card["chain"] = chain
        return card


@app.get("/api/model/history")
async def get_model_history(chain: str = Query("sol")):
    return await asyncio.to_thread(_get_model_history_sync, chain)


def _get_model_history_sync(chain: str):
    from .scoring.model import evaluation_history

    with session_scope() as session:
        return {"snapshots": evaluation_history(session, chain=normalize_chain(chain))}


@app.get("/api/model/calibration")
async def get_model_calibration(
    chain: str = Query("sol"),
    basis: str = Query("entry", pattern="^(entry|research)$"),
    days: int = Query(21, ge=0, le=365),
    scorer: str = Query("all", pattern="^(all|legacy|first_sight)$"),
):
    """basis=entry (default): frozen at-entry decisions vs forward 24h tape, backfill
    excluded. basis=research: legacy bins on the mutable research.p_good.
    scorer narrows the entry bins to one Entry scale (legacy 0.70/0.90 lines,
    first_sight 0.30/0.50); ``scorers`` in the reply counts resolved rows per scale.
    ``days`` defaults to 21 (Learn-safe). 0 uses the same 21-day default so an
    unbounded stampede cannot rebuild the full-history join."""
    return await asyncio.to_thread(_get_model_calibration_sync, chain, basis, days, scorer)


def _get_model_calibration_sync(chain: str, basis: str = "entry", days: int = 21, scorer: str = "all"):
    from .db import apply_report_guards
    from .ledger import CALIBRATION_DEFAULT_DAYS

    chain = normalize_chain(chain)
    with session_scope() as session:
        apply_report_guards(session)
        if basis == "research":
            from .scoring.model import calibration_bins

            return {"basis": "research", "bins": calibration_bins(session, chain=chain)}
        from .ledger import honest_calibration

        window = int(days) if days else CALIBRATION_DEFAULT_DAYS
        since = datetime.now(timezone.utc) - timedelta(days=window)
        return honest_calibration(
            session,
            chain,
            since=since,
            scorer=None if scorer == "all" else scorer,
            window_days=window,
        )


@app.get("/api/model/artifacts")
async def get_model_artifacts(chain: str = Query("sol"), kind: str = Query("entry", pattern="^(entry|live|first_sight|live_runner|ran_shadow)$")):
    """Batch-fit history: candidate vs incumbent metrics per fit, promoted version, isotonic map.

    ``live_runner`` and ``ran_shadow`` are shadow heads: written every fit,
    never promoted, never read by paper or the Hunt card.
    """
    from .scoring.batch_fit import artifact_card

    def _run():
        with session_scope() as session:
            card = artifact_card(session, chain, kind)
            if kind == "live":
                from .scoring.live_fit import live_sample_counts

                card["samples"] = live_sample_counts(session, chain)
                card["label"] = "2x forward from the t+15m print with a live pool; compared against Entry alone"
            if kind == "live_runner":
                from .scoring.live_fit import RUNNER_HIT, RUNNER_HOLD_MULT

                card["shadow"] = True
                card["label"] = f"runner: sellable peak >= {RUNNER_HIT:g}x the t+15m print, or still >= {RUNNER_HOLD_MULT:g}x at the 6h print; same rows as the Live fit, never promoted"
            if kind == "ran_shadow":
                card["shadow"] = True
                card["label"] = "sellable peak >= 1.5x from the fillable print on the first-sight columns; never promoted"
            if kind == "first_sight":
                from .scoring.first_sight import FIRST_SIGHT_CHAINS, FIRST_SIGHT_FIT_CHAINS, chain_features

                card["label"] = "2x (sellable) from the first-sight print within 24h; frozen first-sight columns on every judged decision, seed and live"
                card["scores_entry"] = normalize_chain(chain) in FIRST_SIGHT_CHAINS
                card["shadow"] = normalize_chain(chain) in FIRST_SIGHT_FIT_CHAINS and normalize_chain(chain) not in FIRST_SIGHT_CHAINS
                card["features"] = list(chain_features(chain))
                card["desk_lines"] = desk_lines(session, chain).as_dict()
            return card

    return await asyncio.to_thread(_run)


@app.post("/api/model/fit")
async def post_model_fit(chain: str = Query("sol"), kind: str = Query("entry", pattern="^(entry|live|first_sight)$"), promote: bool = Query(True)):
    """Run one batch fit now (worker also runs hourly). Promotes only if better."""

    def _run():
        with session_scope() as session:
            if kind == "live":
                from .scoring.live_fit import fit_live_model

                return fit_live_model(session, chain, promote=promote)
            if kind == "first_sight":
                from .scoring.first_sight import fit_first_sight

                return fit_first_sight(session, chain, promote=promote)
            from .scoring.batch_fit import fit_entry_model

            return fit_entry_model(session, chain, promote=promote)

    return await asyncio.to_thread(_run)


@app.get("/api/ledger/weekly")
async def get_ledger_weekly(chain: str = Query("sol"), weeks: int = Query(8, ge=1, le=52)):
    """Forward hit rate of the desk lines (lo / hi, per the scorer that wrote each Entry) per ISO week, from frozen decisions."""
    from .ledger import honest_weekly

    def _run():
        from .db import apply_report_guards

        with session_scope() as session:
            apply_report_guards(session)
            return honest_weekly(session, chain, weeks=weeks)

    return await asyncio.to_thread(_run)


@app.get("/api/ledger/decisions")
async def get_ledger_decisions(
    chain: str = Query("sol"),
    kind: str = Query("entry", pattern="^(entry|line70|line90|gate)$"),
    min_p: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(100, ge=1, le=500),
):
    from .ledger import NO_EVIDENCE, decision_result, post_decision_evidence
    from .models import Decision

    def _run():
        chain_n = normalize_chain(chain)
        with session_scope() as session:
            rows = (
                session.query(Decision, Outcome, Token.symbol)
                .join(Token, Token.id == Decision.token_id)
                .outerjoin(Outcome, Outcome.token_id == Decision.token_id)
                .filter(Decision.chain == chain_n, Decision.kind == kind, Decision.entry_p >= min_p)
                .order_by(Decision.at.desc())
                .limit(limit)
                .all()
            )
            evidence = post_decision_evidence(session, (d for d, _o, _s in rows))
            out = []
            for d, o, symbol in rows:
                out.append(
                    {
                        "mint": d.mint,
                        "symbol": symbol,
                        "at": d.at.isoformat() if d.at else None,
                        "kind": d.kind,
                        "source": d.source,
                        "scorer": d.scorer or "legacy",
                        "entry_p": round(float(d.entry_p or 0.0), 4),
                        "heuristic_p": round(float(d.heuristic_p or 0.0), 4),
                        "model_p": round(float(d.model_p or 0.0), 4),
                        "entry_mcap": round(float(d.entry_mcap or 0.0)),
                        "liq": round(float(d.liq or 0.0)),
                        "holders": d.holders,
                        "veto": d.veto,
                        "flags": json.loads(d.flags_json or "[]"),
                        "image_rev": d.image_rev,
                        "model_version": d.model_version,
                        "result": decision_result(d, o, evidence=evidence.get(d.id, NO_EVIDENCE)),
                    }
                )
            return {"chain": chain_n, "kind": kind, "items": out}

    return await asyncio.to_thread(_run)


@app.get("/api/tickets")
async def get_tickets(chain: str | None = Query(None), limit: int = Query(100, ge=1, le=500)):
    from .ledger import list_tickets

    def _run():
        with session_scope() as session:
            return {"items": list_tickets(session, chain, limit=limit), "note": "Shadow tickets. Nothing executes."}

    return await asyncio.to_thread(_run)


@app.get("/api/tickets/report")
async def get_ticket_report(chain: str = Query("sol"), weeks: int = Query(8, ge=1, le=52)):
    from .ledger import ticket_report

    def _run():
        with session_scope() as session:
            return ticket_report(session, chain, weeks=weeks)

    return await asyncio.to_thread(_run)


@app.post("/api/tickets/{ticket_id}/status")
async def post_ticket_status(ticket_id: int, status: str = Query(..., pattern="^(shadow|confirmed|skipped)$")):
    """Manual desk action. ``confirmed`` records the human said yes — nothing executes."""
    from .ledger import set_ticket_status

    def _run():
        with session_scope() as session:
            return set_ticket_status(session, ticket_id, status)

    out = await asyncio.to_thread(_run)
    if out is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return out


MIN_SELLABLE_LIQ = 5_000.0
# New desk Paper tab. Classic leftover paper stays on 0.70 / 5× / 10×.
DESK_PAPER_MIN_P = 0.90
DESK_PAPER_TARGET = 2.0
DESK_PAPER_RIDE = 10.0


@app.get("/api/paper")
async def paper_ledger(
    min_p: float = Query(0.7, ge=0.0, le=1.0),
    target: float = Query(5.0, ge=1.2, le=10.0),
    ride: float = Query(10.0, ge=2.0, le=20.0),
    strategy: str = Query("moonbag", pattern="^(flat|moonbag)$"),
    chain: str = Query("sol"),
    gated: bool = Query(False),
    ledger: bool = Query(True),
):
    """Simulated desk: buy every live token scored >= min_p at its entry mcap.
    No real orders. Moonbag sells half at `target`x (default 5x) and rides
    the rest to a confirmed `ride`x (default 10x) or the 24h exit. flat sells
    everything at the target. Fills only count when a snapshot confirms the
    level traded with real liquidity. Research tool only.
    gated=1 (desk Paper tab): persisted ledger of this-window 90+ fills the
    worker opened at the first liquid print (hard-stop veto). ledger=0 falls
    back to the legacy re-derived view."""
    if not isinstance(min_p, (int, float)):
        min_p = float(getattr(min_p, "default", 0.7) or 0.7)
    if not isinstance(target, (int, float)):
        target = float(getattr(target, "default", 5.0) or 5.0)
    if not isinstance(ride, (int, float)):
        ride = float(getattr(ride, "default", 10.0) or 10.0)
    if ride < target:
        ride = target
    if not isinstance(gated, bool):
        gated = bool(getattr(gated, "default", False))
    if not isinstance(ledger, bool):
        ledger = bool(getattr(ledger, "default", True))
    if gated and ledger:
        from .ledger import paper_ledger_view

        def _ledger():
            with session_scope() as session:
                return paper_ledger_view(session, chain, target=target, ride=ride)

        return await asyncio.to_thread(_ledger)
    # Live /rh: paper sat 9–12s on the event loop and 499'd the tape.
    return await asyncio.to_thread(_paper_ledger_sync, min_p, target, ride, strategy, chain, gated)


@app.get("/api/paper/scorecard")
async def paper_scorecard_route(
    chain: str = Query("sol"),
    target: float = Query(2.0, ge=1.2, le=10.0),
):
    """This-window vs leftover-clock paper scorecard. No row dump."""
    from .ledger import paper_scorecard_view

    def _view():
        with session_scope() as session:
            return paper_scorecard_view(session, chain, target=target)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/v1")
async def paper_v1_route(chain: str = Query("sol")):
    """Daily short list (paper only). Cap 5/UTC day — the 1–5 buy surface."""
    from .ledger import paper_v1_book

    def _view():
        from .db import apply_report_guards

        with session_scope() as session:
            apply_report_guards(session, timeout_ms=20_000)
            return paper_v1_book(session, chain)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/v1/si-pr-probe")
async def paper_v1_si_pr_probe_route(
    chain: str = Query("sol"),
    mint: str = Query(..., min_length=8),
):
    """Debug si-pr path for one FOMO board mint (PAID / MEME spot checks)."""
    from .ledger import si_pr_fomo_probe

    def _view():
        from .db import apply_report_guards

        with session_scope() as session:
            apply_report_guards(session, timeout_ms=15_000)
            return si_pr_fomo_probe(session, chain, mint)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/v1/review")
async def paper_v1_review_route(chain: str = Query("sol"), day: str | None = Query(None)):
    """Daily short-list review: picked / skipped / why / early outcome."""
    from .ledger import paper_v1_review

    def _view():
        from .db import apply_report_guards

        with session_scope() as session:
            apply_report_guards(session, timeout_ms=20_000)
            return paper_v1_review(session, chain, day=day)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/v1/report")
async def paper_v1_report_route(day: str | None = Query(None)):
    """UTC-day short-list export (both chains). Paper only."""
    from .ledger import paper_v1_day_report

    def _view():
        from .db import apply_report_guards

        with session_scope() as session:
            apply_report_guards(session, timeout_ms=25_000)
            return paper_v1_day_report(session, day=day)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/v1/runners-retro")
async def paper_v1_runners_retro_route(
    chain: str = Query("sol"),
    min_multiple: float = Query(5.0, ge=2.0, le=100.0),
):
    """Confirmed runners vs would-have-passed paperV1. Retrospective only."""
    from .scoring.runners_retro import paper_v1_runners_retro

    def _view():
        with session_scope() as session:
            return paper_v1_runners_retro(session, chain, min_multiple=min_multiple)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/v1/miss-cohort")
async def paper_v1_miss_cohort_route(
    chain: str = Query("sol"),
    days: int = Query(14, ge=1, le=60),
):
    """Same-day winners vs losers + paper-miss 5× autopsies. Offline Learn."""
    from .scoring.miss_cohort import miss_cohort

    def _view():
        with session_scope() as session:
            return miss_cohort(session, chain, days=days)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/veto-retro")
async def paper_veto_retro_route(chain: str = Query("sol")):
    """Historical hard/soft veto health. Keep/refuse feedback — not a buy list."""
    from .scoring.veto_retro import veto_retro

    def _view():
        from .db import apply_report_guards

        with session_scope() as session:
            apply_report_guards(session, timeout_ms=30_000)
            return veto_retro(session, chain)

    return await asyncio.to_thread(_view)


@app.get("/api/sanity-loop")
async def sanity_loop_route(
    chain: str = Query("sol"),
    apply: bool = Query(False),
    actions: str | None = Query(
        None,
        description="Comma-separated executable knobs when apply=1 "
        "(repair_thin_thesis, freeze_live_coverage, enrich_thin_thesis_http). "
        "Default: all suggested.",
    ),
):
    """Continuous improve + reality sanity. Gated next actions — not a buy list.

    ``apply=1`` runs enrich/freeze jobs when hard checks pass (never auto-buy).
    ``actions=`` limits to named knobs for one-knob self-improve cycles.
    """
    from .scoring.sanity_jobs import EXECUTABLE
    from .scoring.sanity_loop import sanity_loop

    only = None
    if actions:
        only = [a.strip() for a in actions.split(",") if a.strip() in EXECUTABLE]

    with session_scope() as session:
        return await sanity_loop(session, chain, apply=apply, only_actions=only)


@app.get("/api/paper/v1/day-delta")
async def paper_v1_day_delta_route(
    chain: str = Query("sol"),
    day: str | None = Query(None),
):
    """Yesterday → today short-list delta for the Learn daily ritual."""
    from .scoring.day_delta import paper_v1_day_delta

    def _view():
        with session_scope() as session:
            return paper_v1_day_delta(session, chain, day=day)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/v1/production-gate")
async def paper_v1_production_gate_route(chain: str = Query("sol")):
    """FORWARD production-gate progress for Learn. Read-only. Paper only."""
    from .scoring.production_gate import production_gate_progress

    def _view():
        from .db import apply_report_guards

        with session_scope() as session:
            apply_report_guards(session, timeout_ms=20_000)
            return production_gate_progress(session, chain)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/v1/thesis-gate-audit")
async def paper_v1_thesis_gate_audit_route(repair: bool = Query(False)):
    """Production-gate thesis denominator: per-fill evidence + optional one-shot repair."""
    from .scoring.thesis_gate import repair_thesis_gate_one_shot, thesis_gate_inventory

    if repair:
        async def _repair():
            from .db import apply_report_guards

            with session_scope() as session:
                apply_report_guards(session, timeout_ms=120_000)
                return await repair_thesis_gate_one_shot(session)

        return await _repair()

    def _read():
        from .db import apply_report_guards

        with session_scope() as session:
            apply_report_guards(session, timeout_ms=60_000)
            return thesis_gate_inventory(session)

    return await asyncio.to_thread(_read)


@app.get("/api/paper/v1/offline-sprint")
async def paper_v1_offline_sprint_route(
    chain: str = Query("sol"),
    repair: bool = Query(False),
    refit: bool = Query(False),
):
    """Offline sprint snapshot: capture gap, miss cohort, optional repair/refit."""
    from .scoring.offline_sprint import offline_sprint_status

    def _view():
        with session_scope() as session:
            return offline_sprint_status(session, chain, repair=repair, refit=refit)

    return await asyncio.to_thread(_view)


@app.get("/api/paper/v1/early-diff")
async def paper_v1_early_diff_route(
    chain: str = Query("sol"),
    hours: int = Query(48, ge=1, le=168),
    top_n: int = Query(12, ge=1, le=40),
):
    """SI-cohort feature-divergence ranker — Learn/shadow closer-look only.

    Paper-safe: no fills, no gate changes, serial deployer is a prior never
    a hard block. Surfaces top of ranked recent launches + offline cohort proof.
    """
    from .scoring.early_diff import early_diff_status

    def _view():
        with session_scope() as session:
            return early_diff_status(session, chain, hours=hours, top_n=top_n)

    return await asyncio.to_thread(_view)


@app.get("/api/year-winners")
async def year_winners_route(
    chain: str = Query("sol"),
    min_multiple: float = Query(5.0, ge=2.0, le=100.0),
):
    """Incredible-return template: curated exemplars + ledger runners + alpha notes."""
    from .scoring.year_winners import year_winners_template

    def _view():
        with session_scope() as session:
            return year_winners_template(session, chain, min_multiple=min_multiple)

    return await asyncio.to_thread(_view)


@app.get("/api/risk")
async def risk_get_route():
    """Risk controls stub — arm default off, kill switch, notional caps."""
    from .risk import risk_status

    def _view():
        with session_scope() as session:
            return risk_status(session)

    return await asyncio.to_thread(_view)


@app.post("/api/risk")
async def risk_post_route(request: Request):
    """Update risk controls. Never arms while kill_switch is on. Paper only."""
    from .risk import kill_paper_v1_queue, save_risk

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    def _update():
        with session_scope() as session:
            # Convenience: {"kill_switch": true, "flush_queue": true}
            ctrl = save_risk(session, body)
            flushed = {"skipped": 0}
            if body.get("flush_queue") and ctrl.get("kill_switch"):
                flushed = kill_paper_v1_queue(session, reason="kill_switch")
            return {**ctrl, "queue_flushed": flushed}

    return await asyncio.to_thread(_update)


def _paper_ledger_sync(min_p, target, ride, strategy, chain, gated=False):
    from .models import Research, Snapshot

    with session_scope() as session:
        from sqlalchemy import or_

        # Live stall honesty rewrites research.p_good (TEAL 0.87→0.48).
        # Keep the row if the t0 snap already bought at >= min_p — a desk
        # cannot un-enter a fill just because the card faded.
        t0_bought = (
            session.query(Snapshot.id)
            .filter(Snapshot.token_id == Token.id, Snapshot.kind == "t0", Snapshot.p_good >= min_p)
            .exists()
        )
        score_ok = or_(Research.p_good >= min_p, t0_bought)
        if gated:
            # Live Root: Hunt froze 0.92, live faded to 50, no t0 p on the
            # snap — the 90+ buy is the hunt row, not research.p_good.
            hunt_bought = (
                session.query(HuntCard.id)
                .filter(HuntCard.token_id == Token.id, HuntCard.entry_p >= min_p)
                .exists()
            )
            score_ok = or_(Research.p_good >= min_p, t0_bought, hunt_bought)
        rows = (
            session.query(Token, Research, Outcome)
            .join(Research, Research.token_id == Token.id)
            .join(Outcome, Outcome.token_id == Token.id)
            # Exclude backfill seeds only. Tokens entered live must stay in the
            # ledger even after the freshness reclassifier marks them
            # historical — a desk cannot un-enter a position.
            .filter(
                Token.source != "backfill",
                Token.chain == normalize_chain(chain),
                score_ok,
            )
            .order_by(Token.first_seen_at.desc())
            .all()
        )
        token_ids = [t.id for t, _, _ in rows]
        from .scoring.outcomes import (
            DEAD_POOL_LIQ,
            MAX_HONEST_MULTIPLE,
            is_ghost_book,
            is_prepumped_entry,
        )

        # Highest liquid snapshot mcap per token, one query for the whole book.
        # Live /rh paper: one t0_snapshot_is_ghost query per row 499'd the desk.
        ghost_t0: dict[int, bool] = {}
        sellable_max: dict[int, float] = {}
        live_t0: dict[int, float] = {}
        entry_p_at_t0: dict[int, float] = {}
        if token_ids:
            from sqlalchemy import func as _f

            sellable_max = dict(
                session.query(Snapshot.token_id, _f.max(Snapshot.mcap_usd))
                .filter(Snapshot.token_id.in_(token_ids), Snapshot.liquidity_usd >= MIN_SELLABLE_LIQ)
                .group_by(Snapshot.token_id)
                .all()
            )
            # FRUG-class: outcome.t0 is the $69k floor but the t0 snap already
            # traded at $264k on a live pool. Paper must buy that book, not
            # invent a 5x from the graduation constant. Do not use
            # is_prepumped_entry here on Solana — that skip collapsed the desk.
            # t0.p_good is the at-entry decision. Later repairs (organic-book
            # lift) rewrite research.p_good and would retroactively stuff
            # 200 rugs onto the Sol paper line. RH uses t0 when that snap
            # already bought; live research.p_good only for later repairs.
            rh_paper = normalize_chain(chain) == "robinhood"
            for tid, mcap, liq, vol, snap_p in (
                session.query(
                    Snapshot.token_id,
                    Snapshot.mcap_usd,
                    Snapshot.liquidity_usd,
                    Snapshot.volume_h1,
                    Snapshot.p_good,
                )
                .filter(Snapshot.token_id.in_(token_ids), Snapshot.kind == "t0")
                .order_by(Snapshot.taken_at.asc())
                .all()
            ):
                if rh_paper and tid not in ghost_t0:
                    ghost_t0[tid] = is_ghost_book(liq, vol)
                if tid not in entry_p_at_t0 and float(snap_p or 0.0) > 0:
                    entry_p_at_t0[tid] = float(snap_p)
                if float(liq or 0.0) < MIN_SELLABLE_LIQ:
                    continue
                live_t0[tid] = max(live_t0.get(tid, 0.0), float(mcap or 0.0))
        hunt_p: dict[int, float] = {}
        if gated and token_ids:
            for tid, ep in (
                session.query(HuntCard.token_id, HuntCard.entry_p)
                .filter(HuntCard.token_id.in_(token_ids))
                .all()
            ):
                if float(ep or 0.0) > 0:
                    hunt_p[int(tid)] = float(ep)
        last_live: dict[int, float] = {}
        last_liq_live: dict[int, float] = {}
        if token_ids:
            for tid, mcap, liq, vol in (
                session.query(Snapshot.token_id, Snapshot.mcap_usd, Snapshot.liquidity_usd, Snapshot.volume_h1)
                .filter(Snapshot.token_id.in_(token_ids), Snapshot.mcap_usd > 0)
                .order_by(Snapshot.taken_at.desc(), Snapshot.id.desc())
                .all()
            ):
                if tid in last_live:
                    continue
                if is_ghost_book(liq, vol):
                    continue
                last_live[tid] = float(mcap)
                last_liq_live[tid] = float(liq or 0.0)
        first_book: dict[int, float] = {}
        first_book_at: dict[int, object] = {}
        if gated and token_ids:
            for tid, taken, mcap, liq in (
                session.query(
                    Snapshot.token_id,
                    Snapshot.taken_at,
                    Snapshot.mcap_usd,
                    Snapshot.liquidity_usd,
                )
                .filter(
                    Snapshot.token_id.in_(token_ids),
                    Snapshot.mcap_usd > 0,
                    Snapshot.liquidity_usd >= MIN_SELLABLE_LIQ,
                )
                .order_by(Snapshot.taken_at.asc(), Snapshot.id.asc())
                .all()
            ):
                if tid in first_book:
                    continue
                first_book[int(tid)] = float(mcap)
                first_book_at[int(tid)] = taken
        closed, open_pos, pending, wins, unfilled = [], [], [], 0, 0
        total_return = 0.0
        for token, research, outcome in rows:
            entry = float(outcome.t0_mcap or 0.0)
            if entry <= 0:
                continue
            snap_t0 = float(live_t0.get(token.id) or 0.0)
            if snap_t0 > entry:
                entry = snap_t0
            # Leftover graduation LP / empty Dex pair is not a fill.
            if ghost_t0.get(token.id):
                continue
            t0_p = float(entry_p_at_t0.get(token.id) or 0.0)
            live_p = float(research.p_good or 0.0)
            if gated:
                from .scoring.paper_gate import (
                    PAPER_FIRST_BOOK_GRACE_MIN,
                    paper_entry_p,
                    paper_window_hours,
                )

                if normalize_chain(chain) == "sol":
                    from .scoring.hunt import sol_launch_at

                    launched = sol_launch_at(token)
                else:
                    launched = token.migrated_at or token.first_seen_at
                if launched is None:
                    continue
                if launched.tzinfo is None:
                    launched = launched.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - launched).total_seconds() > paper_window_hours(chain) * 3600:
                    continue
                entry_p = paper_entry_p(float(hunt_p.get(token.id) or 0.0), t0_p, live_p)
                if entry_p < min_p:
                    continue
                liquid = snap_t0
                if liquid <= 0:
                    # Ghost t0 (SALARY $0 liq) — first sellable print in the
                    # grace window is the fill, not a later ATH.
                    taken = first_book_at.get(token.id)
                    if taken is None:
                        continue
                    if getattr(taken, "tzinfo", None) is None:
                        taken = taken.replace(tzinfo=timezone.utc)
                    if (taken - launched).total_seconds() > PAPER_FIRST_BOOK_GRACE_MIN * 60:
                        continue
                    liquid = float(first_book.get(token.id) or 0.0)
                if liquid <= 0:
                    continue
                entry = liquid
            elif normalize_chain(chain) != "robinhood":
                # Sol: at-entry t0 snap only. Later organic-book lifts
                # must not flood the desk with rugs.
                entry_p = t0_p if t0_p > 0 else live_p
                if entry_p < min_p:
                    continue
            elif t0_p >= min_p:
                # RH: stall fade cannot un-enter a t0 fill (TEAL 0.87→0.48).
                entry_p = t0_p
            elif live_p >= min_p:
                # Young-model / small-book repairs are still intended fills
                # when there was no qualifying t0 snap (SANDIH 0.43→0.55).
                entry_p = live_p
            else:
                continue
            # Live 14:47: t0-keep re-entered July PUFFLING / Ignoring
            # leftovers and blew open 21→87. This-window stall fades
            # (TEAL) stay; leftover launches only stay if live p still
            # clears the buy line (ASS 0.72). Do not filter TEAL to live p.
            if normalize_chain(chain) == "robinhood" and not gated:
                launched = token.created_at_chain or token.first_seen_at
                if launched is not None:
                    if launched.tzinfo is None:
                        launched = launched.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - launched).total_seconds() > 18 * 3600 and live_p < min_p:
                        continue
            # Live CURATOR: t0 $172k (4.3× RH floor) then the book was $24k.
            # Same start-high class runners already exclude. Solana paper
            # still uses outcome.t0 (often the $69k floor) — flipping that
            # desk in the same change rewrites the headline PnL.
            if normalize_chain(chain) == "robinhood" and is_prepumped_entry(session, token, research, outcome):
                continue
            # Live ARROW 4929x at a $69k floor entry is a Dex artifact, not
            # a moonbag 10x. Runners already cap at 80x; paper should too.
            if float(outcome.multiple or 0.0) > MAX_HONEST_MULTIPLE:
                continue
            if gated:
                from .scoring.paper_gate import paper_fill_verdict
                from .serialize import (
                    associated_dev_handle,
                    display_token_x,
                    is_sol_leftover_fdv_doing_well,
                )

                try:
                    flags = json.loads(research.risk_flags_json or "[]")
                except json.JSONDecodeError:
                    flags = []
                if is_sol_leftover_fdv_doing_well(
                    {
                        "chain": normalize_chain(token.chain or chain),
                        "t0_mcap": entry,
                        "last_mcap": float(outcome.last_mcap or outcome.max_mcap or 0.0),
                        "last_liq": float(outcome.last_liq or 0.0),
                        "holder_count": research.holder_count,
                        "top10_pct": research.top10_pct,
                        "risk_flags": flags,
                    }
                ):
                    continue
                last_m = float(last_live.get(token.id) or outcome.last_mcap or 0.0)
                last_l = float(last_liq_live.get(token.id) or outcome.last_liq or 0.0)
                try:
                    feat = json.loads(research.features_json or "{}")
                except json.JSONDecodeError:
                    feat = {}
                if (
                    paper_fill_verdict(
                        entry_mcap=entry,
                        last_mcap=last_m,
                        last_liq=last_l,
                        flags=flags,
                        chain=normalize_chain(token.chain or chain),
                        website=token.website or "",
                        twitter=token.twitter or "",
                        twitter_handle=display_token_x(
                            research.twitter_handle,
                            associated_dev_handle(token, research),
                        ),
                        twitter_followers=float(research.twitter_followers or 0),
                        twitter_age_days=float(research.twitter_age_days or 0),
                        twitter_verified=bool(research.twitter_verified),
                        features=feat if isinstance(feat, dict) else {},
                    )
                    != "pass"
                ):
                    continue
            pos = {
                "symbol": token.symbol,
                "mint": token.mint,
                "p_good": round(entry_p, 4),
                "entry_mcap": round(entry),
                "multiple": round(outcome.multiple or 0.0, 2),
            }
            live_m = float(last_live.get(token.id) or 0.0)
            live_mult = (live_m / entry) if live_m > 0 else float(outcome.multiple or 0.0)
            if outcome.label is None:
                # Live FOMODOG: wick max 3.87x then the book dumped to $3.4k.
                # Open tape is mark-to-market, same as approaching.
                pos["multiple"] = round(live_mult, 2)
                open_pos.append(pos)
                continue
            best_fill = float(sellable_max.get(token.id) or 0.0)
            confirmed_fill = best_fill >= target * entry
            if outcome.label == 1 and not confirmed_fill and outcome.t24h_mcap is None:
                # Labeled a winner but no confirmed fill and no 24h price yet:
                # the position is still being held.
                pos["multiple"] = round(live_mult, 2)
                open_pos.append(pos)
                continue
            if outcome.t24h_mcap:
                exit_ret = (outcome.t24h_mcap / entry) - 1.0
            elif (
                normalize_chain(chain) == "robinhood"
                and float(outcome.last_liq or 0.0) >= DEAD_POOL_LIQ
            ):
                # Dex miss at the 24h label tick used to leave t24h empty and
                # close a live $20k flat book as -85%. Use the last liquid
                # print (or stored peak) so the first RH wave is honest.
                live_exit = float(sellable_max.get(token.id) or outcome.max_mcap or 0.0)
                exit_ret = (live_exit / entry) - 1.0 if live_exit > 0 else -0.85
            else:
                exit_ret = -0.85
            if confirmed_fill:
                wins += 1
                if strategy == "moonbag":
                    # half sold at target; the rest rides to a confirmed
                    # ride-multiple (default 10x) or exits with the 24h price
                    ride_ret = (ride - 1.0) if best_fill >= ride * entry else exit_ret
                    ret = 0.5 * (target - 1.0) + 0.5 * ride_ret
                else:
                    ret = target - 1.0
            else:
                if (outcome.multiple or 0.0) >= target:
                    unfilled += 1  # wick through the target we treat as unfilled
                ret = exit_ret
            pos["return_pct"] = round(ret * 100.0, 1)
            total_return += ret
            closed.append(pos)
        from .desk_lines import desk_lines

        lines = desk_lines(session, chain)
        sell = (
            f"sell half at {target}x, ride half to confirmed {ride:g}x or 24h"
            if strategy == "moonbag"
            else f"sell at {target}x (liquidity-confirmed) or 24h"
        )
        if gated:
            desc = (
                f"buy this-window Entry>={lines.hi:.2f} ({lines.scorer} line) now if liquid and no veto "
                f"(copycat/hijack/brand/dump/start-high); {sell}"
            )
        else:
            desc = f"buy p>={min_p} at entry; {sell}"
        return {
            "strategy": desc,
            "mode": strategy,
            "gated": bool(gated),
            "lines": lines.as_dict(),
            "closed_trades": len(closed),
            "open_positions": len(open_pos),
            "pending": len(pending),
            "wins": wins,
            "unfilled_2x_wicks": unfilled,
            "target": target,
            "ride": ride,
            "avg_return_pct": round(total_return / len(closed) * 100.0, 1) if closed else None,
            "total_return_pct": round(total_return * 100.0, 1),
            "closed": closed[:50],
            "open": open_pos[:50],
            "waiting": pending[:20],
        }


@app.get("/api/reference")
async def reference(chain: str = Query("sol")):
    return await asyncio.to_thread(_reference_sync, chain)


def _reference_sync(chain: str):
    chain = normalize_chain(chain)
    with session_scope() as session:
        winners = (
            session.query(Token)
            .join(Outcome, Outcome.token_id == Token.id)
            .filter(Outcome.label == 1, Token.chain == chain)
            .order_by(Outcome.multiple.desc())
            .limit(12)
            .all()
        )
        losers = (
            session.query(Token)
            .join(Outcome, Outcome.token_id == Token.id)
            .filter(Outcome.label == 0, Token.chain == chain)
            .order_by(Outcome.multiple.asc())
            .limit(12)
            .all()
        )
        return {"winners": [token_card(t) for t in winners], "losers": [token_card(t) for t in losers]}


@app.get("/api/watch")
async def graduating_soon(chain: str = Query("sol")):
    """GMGN near-completion trenches: bonding curves about to graduate."""
    return await asyncio.to_thread(_watch_sync, chain)


def _watch_sync(chain: str):
    chain = normalize_chain(chain)
    if chain == "robinhood":
        from .ingest.rh_poll import watch_preview as rh_watch

        rows = rh_watch()
        with session_scope() as session:
            from .research.prewarm import annotate_watch

            return {"graduating_soon": annotate_watch(session, rows, chain), "chain": chain}
    from .ingest.pump_poll import watch_preview

    rows = watch_preview()
    with session_scope() as session:
        from .research.prewarm import annotate_watch

        return {"graduating_soon": annotate_watch(session, rows, chain), "chain": chain}


@app.get("/api/runners")
async def runners(min_multiple: float = Query(5.0, ge=2.0, le=100.0), chain: str = Query("sol")):
    """The tokens that actually ran, with what the model thought at entry.
    Default floor is the 5x win bar (desk hunts 5-50x). capture_rate =
    fraction flagged at entry (p >= 0.6 Solana / 0.5 Robinhood, matching
    each desk's paper buy line). Start-high-and-rug tokens are
    not runners: the pool must have survived."""
    if not isinstance(min_multiple, (int, float)):
        min_multiple = float(getattr(min_multiple, "default", 5.0) or 5.0)
    return await asyncio.to_thread(_runners_sync, min_multiple, chain)


def _runners_sync(min_multiple, chain):
    from .models import Research

    with session_scope() as session:
        from .scoring.outcomes import (
            DEAD_POOL_LIQ,
            MAX_HONEST_MULTIPLE,
            confirmed_runner_multiple,
            is_bundle_copycat_run,
            is_prepumped_entry,
        )

        chain = normalize_chain(chain)
        # Top-N by raw multiple still fills with unconfirmed 10–80x tape
        # (dead books, missing snaps). Live PVP is an honest 8.2x with a
        # liquid post snap and was missing while 250 higher raw multiples
        # failed confirmation. Drop dead-liq / 411-SOL t0 rows and take
        # more candidates so today's 5–20x can reach confirmed_runner.
        filters = [
            Outcome.multiple >= min_multiple,
            Outcome.multiple <= MAX_HONEST_MULTIPLE,
            Token.source != "backfill",
            Token.chain == chain,
            (Outcome.label.is_(None)) | (Outcome.label == 1),  # rugs are not runners
            Outcome.last_liq >= DEAD_POOL_LIQ,  # dead tape cannot occupy the window
            ~Research.risk_flags_json.like("%start-high rug%"),
            ~Research.risk_flags_json.like("%Pre-pumped%"),
        ]
        if chain == "sol":
            filters.append(Outcome.t0_mcap >= 0.4 * graduation_mcap("sol"))
        # Quiet-retired RH leftovers can later print a real Dex book
        # (AIAIAI: historical leftover, then a post snap at 55.6×). Those
        # are not live desk tape. Solana keeps historical majors —
        # `_reclassify_old_majors` parks CAC/Pappy-class names.
        if chain == "robinhood":
            filters.append(Token.is_historical.is_(False))
        rows = (
            session.query(Token, Research, Outcome)
            .join(Research, Research.token_id == Token.id)
            .join(Outcome, Outcome.token_id == Token.id)
            .filter(*filters)
            .order_by(Outcome.multiple.desc())
            .limit(800)
            .all()
        )

        items = []
        flagged = 0
        # /rh paper buys at 0.50. ROCK/SSB/OOOF all sat at ~0.51 and the
        # Solana 0.60 line reported capture 0.0 on three honest 5x names.
        flag_p = 0.5 if chain == "robinhood" else 0.6
        for token, research, outcome in rows:
            if is_prepumped_entry(session, token, research, outcome):
                continue
            if is_bundle_copycat_run(research):
                continue
            confirmed = confirmed_runner_multiple(session, token, outcome)
            if confirmed < min_multiple:
                continue
            p = research.p_good or 0.0
            if p >= flag_p:
                flagged += 1
            items.append(
                {
                    "symbol": token.symbol,
                    "mint": token.mint,
                    "multiple": round(confirmed, 1),
                    "entry_p": round(p, 3),
                    "flagged_at_entry": p >= flag_p,
                    "label": outcome.label,
                    "source": token.source,
                    "gmgn": gmgn_token_url(token.mint, token.chain),
                    "chain": token.chain,
                }
            )
        items.sort(key=lambda r: r["multiple"], reverse=True)
        items = items[:100]
        n = len(items)
        return {
            "min_multiple": min_multiple,
            "runners": n,
            "capture_rate": round(flagged / n, 3) if n else None,
            "items": items,
        }


@app.get("/api/approaching")
async def approaching(
    min_multiple: float = Query(2.0, ge=1.5, le=5.0),
    hours: float = Query(48.0, ge=6.0, le=168.0),
    chain: str = Query("sol"),
):
    """Names climbing toward the 5x win bar. Thin-holder Dex wicks (4-wallet
    prints like live RH SHORT/PLUMBED) are split out so the desk is not
    empty while we wait for the first honest 5x. Default window is 48h so
    old 70x tape (CUCK/Plumber class) does not bury today's climbs."""
    if not isinstance(min_multiple, (int, float)):
        min_multiple = float(getattr(min_multiple, "default", 2.0) or 2.0)
    if not isinstance(hours, (int, float)):
        hours = float(getattr(hours, "default", 48.0) or 48.0)
    return await asyncio.to_thread(_approaching_sync, min_multiple, hours, chain)


def _approaching_sync(min_multiple, hours, chain):
    from .models import Research
    from .scoring.outcomes import (
        MAX_HONEST_MULTIPLE,
        SKINNY_APPROACH_HOURS,
        SKINNY_HUNT_LIQ,
        STALE_APPROACH_HOURS,
        STALE_APPROACH_MULTIPLE,
        approaching_live_multiple,
        is_bundle_copycat_run,
        is_prepumped_entry,
        is_thin_approaching_print,
    )
    from .scoring.features import is_rh_airdrop_book, is_sol_airdrop_tape

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    with session_scope() as session:
        rows = (
            session.query(Token, Research, Outcome)
            .join(Research, Research.token_id == Token.id)
            .join(Outcome, Outcome.token_id == Token.id)
            .filter(
                Outcome.multiple >= min_multiple,
                Outcome.multiple < 5.0,
                Token.source != "backfill",
                Token.chain == normalize_chain(chain),
                Token.first_seen_at >= cutoff,
                (Outcome.label.is_(None)) | (Outcome.label == 1),
                ~Research.risk_flags_json.like("%start-high rug%"),
                ~Research.risk_flags_json.like("%Pre-pumped%"),
            )
            .order_by(Outcome.multiple.desc())
            .limit(200)
            .all()
        )
        honest: list[dict] = []
        thin: list[dict] = []
        for token, research, outcome in rows:
            if is_prepumped_entry(session, token, research, outcome):
                continue
            if is_bundle_copycat_run(research):
                continue
            # SOL-unit / collapse t0 (~411) inflates a dead $1.8k book to 4.5x.
            if normalize_chain(token.chain) == "sol" and 0 < float(outcome.t0_mcap or 0.0) < 0.4 * graduation_mcap("sol"):
                continue
            mult = float(outcome.multiple or 0.0)
            launched = token.created_at_chain or token.first_seen_at
            if launched is not None:
                if launched.tzinfo is None:
                    launched = launched.replace(tzinfo=timezone.utc)
                age_h = (
                    datetime.now(timezone.utc) - launched
                ).total_seconds() / 3600.0
            else:
                age_h = 0.0
            # Approaching = still climbing toward the 5x bar. A 70x print
            # belongs on /api/runners (if confirmed), not this panel.
            # Live FOMODOG printed 3.87x then dumped to $3.4k — use the
            # last live snap so a wick does not sit on the climb list.
            # Live 21:13: TikZ 2.30× / $18k / 0.3h and ONBOARDING
            # 4.89× / $29k sat hunt while a dumped live snap under
            # 2× hid them from approaching. Fat this-window 2×+
            # stays. FOMODOG $3.4k skinny still drops. 007 15.6h
            # stays off — do not restore 8h+ leftovers because
            # the RH strip is empty.
            live = approaching_live_multiple(session, token, outcome)
            if live < min_multiple:
                if not (
                    float(outcome.last_liq or 0.0) >= SKINNY_HUNT_LIQ
                    and age_h < STALE_APPROACH_HOURS
                    and mult >= min_multiple
                ):
                    continue
            elif live < mult:
                mult = live
            if mult < min_multiple or mult >= 5.0 or mult > MAX_HONEST_MULTIPLE:
                continue
            # Live stonkape 4.15× / Bros 2.16× sat on Sol approaching
            # with last_liq $0. Mid-book $400 climbs (PSYOPED) stay;
            # a missing pool is not a 5x approach.
            if float(outcome.last_liq or 0.0) <= 0:
                continue
            # Live 08:20: ☉ 13.8h / 3.70× / $3.2k sat Sol approaching
            # after hunt buried the skinny dump at #187. Live 11:11:
            # TriplePONS 4.9h / 2.40× / $4.2k sat #7 under the 8h
            # clock. Mid-book $400 this-window (PSYOPED) stays. Do
            # not restore ☉ hunt.
            if (
                0 < float(outcome.last_liq or 0.0) < SKINNY_HUNT_LIQ
                and age_h >= SKINNY_APPROACH_HOURS
            ):
                continue
            holders = int(research.holder_count or 0)
            # Live 10:21: BEAVER 1w / 3.68× / $58k sat RH thin prints.
            # A 1–2 wallet book is not a 5–50x climb. Unknown (0)
            # holders stay. CLOUDING 19w thin stays.
            if 1 <= holders <= 2:
                continue
            # Live Ignoring: 1131 wallets / $3.3k / 3.23× sat approaching
            # and conviction B. Hunt already buries airdrop tape; a dust
            # book with a thousand wallets is not a 5-50x climb.
            if is_rh_airdrop_book(token.chain, holders, outcome.last_liq):
                continue
            # Live 12:38: Dark Arena 291w / $3.0k / 3.43× / 3.5h sat
            # Sol approaching. Hunt already buries airdrop tape. The
            # hunt helper also matches Bunny 607w / $3.3k / 4.98×
            # and SUNLIVE 762w / $30k — those stay. Dust + 200w +
            # sat 3h + still under 4× is leftover tape. PUMPLESS
            # 30w / $46k stays. PSYOPED $400 this-window stays.
            if (
                normalize_chain(token.chain) == "sol"
                and holders >= 200
                and 800.0 <= float(outcome.last_liq or 0.0) < 8_000.0
                and mult < 4.0
                and age_h >= 3.0
            ):
                continue
            # Live 13:08: Birkin 2.04× / 578w / $19k sat #2 under
            # PUMPLESS. Hunt already buries airdrop tape ($33/w).
            # Sat-dust requires book < $8k so $19k missed. Do not
            # use the hunt helper as a blanket drop — Bunny 4.98×
            # and SUNLIVE 3.70× stay. Sub-2.5× tape is leftover.
            if is_sol_airdrop_tape(token.chain, holders, outcome.last_liq) and mult < 2.5:
                continue
            thin_print = is_thin_approaching_print(holders, token.chain)
            # Live 10:40: CLOUDING 19w / 3.49× / $79k sat RH thin
            # prints after 2h. A thin 3×+ that already sat is hunt
            # tape, not a 5–50x approach. Honest fat 3× (007) stays.
            # This-window thin 2× (PSYOPED-class) stays.
            if thin_print and age_h >= 2.0 and mult >= 3.0:
                continue
            # Live 11:21: MONK 17.7h / 2.69× sat Sol approaching after
            # sitting under 3× all day. MEMECITY 12.8h / 2.27× and
            # RICHDEBT 9h / 2.31× sat with it. An 8h+ book still
            # under 3× is leftover tape, not a 5–50x climb. BEN
            # this-window 2.43× stays. 007 3.07× stays. Hunt 2×+
            # (HA / HMSTR) stay on the hunt desk.
            if age_h >= STALE_APPROACH_HOURS and mult < STALE_APPROACH_MULTIPLE:
                continue
            row = {
                "symbol": token.symbol,
                "mint": token.mint,
                "multiple": round(mult, 2),
                "entry_p": round(research.p_good or 0.0, 3),
                "holders": holders,
                "thin": thin_print,
                "last_liq": outcome.last_liq,
                "label": outcome.label,
                "gmgn": gmgn_token_url(token.mint, token.chain),
                "chain": token.chain,
            }
            (thin if thin_print else honest).append(row)
        # Live Pumpooor $76k sat under dumped ANGRYCATS $2.1k when
        # multiple was close. Fat books first, then the climb.
        honest.sort(
            key=lambda r: (
                1 if float(r.get("last_liq") or 0) >= SKINNY_HUNT_LIQ else 0,
                float(r.get("multiple") or 0),
            ),
            reverse=True,
        )
        honest = honest[:20]
        thin = thin[:20]
        return {
            "min_multiple": min_multiple,
            "window_hours": hours,
            "win_bar": 5.0,
            "approaching": len(honest),
            "thin_prints": len(thin),
            "items": honest,
            "thin": thin,
            "chain": normalize_chain(chain),
        }


def _doing_well_held_mints(session, chain: str) -> list[str]:
    """Fat books still many multiples from t0 — even if runners skip them.

    Live MEME: 4 holders / copycat so /api/runners dropped it, but
    last $74M vs t0 $22k on $1.2M liq is a held launch, not a recap.
    Live Laptop GsewXp / TNT: labeled 5× live vs desk t0, $40k+ liq,
    still near ATH — runners unconfirm because the t0 snap already
    printed the live book. Pull those onto Doing well only.
    """
    from .scoring.outcomes import MAX_HONEST_ENTRY_MULTIPLE_RH, MAX_HONEST_MULTIPLE

    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    start_high = MAX_HONEST_ENTRY_MULTIPLE_RH * graduation_mcap(chain)
    # Live LAPTOP: leftover $295M / 4279× vs frozen $69k t0. Dex is
    # $2.3k rugged. Do not pull Sol leftover FDV onto Doing well.
    rows = (
        session.query(Token.mint)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(
            Token.chain == chain,
            Token.is_historical.is_(False),
            Token.source != "backfill",
            Token.first_seen_at >= cutoff,
            Outcome.t0_mcap > 0,
            (Outcome.label.is_(None)) | (Outcome.label == 1),
            or_(
                and_(
                    Outcome.last_liq >= DOING_WELL_HELD_LIQ,
                    or_(
                        Outcome.last_mcap >= DOING_WELL_HELD_MULTIPLE * Outcome.t0_mcap,
                        # Live CRCL: runners/approaching skip Pre-pumped. After
                        # the Dex recovery last is 3.6× on a $95k book — not a
                        # 10× last and not a frozen dump. Start-high t0 only.
                        and_(
                            Outcome.t0_mcap >= start_high,
                            Outcome.max_mcap >= 2.0 * Outcome.t0_mcap,
                            or_(
                                Outcome.last_mcap >= 2.0 * Outcome.t0_mcap,
                                Outcome.last_mcap.is_(None),
                                Outcome.last_mcap <= 0,
                                and_(
                                    Outcome.last_mcap > 0,
                                    Outcome.last_mcap < 0.40 * Outcome.max_mcap,
                                    Outcome.last_mcap < 2.0 * Outcome.t0_mcap,
                                ),
                            ),
                        ),
                    ),
                ),
                # Live Laptop GsewXp 5.2× / $40k liq / label=1. Do not
                # leftover-sort 2×+; live last vs desk t0 must be 5×+.
                and_(
                    Outcome.label == 1,
                    Outcome.last_liq >= DOING_WELL_LABELED_LIQ,
                    Outcome.last_mcap >= DOING_WELL_LABELED_LIVE_MULTIPLE * Outcome.t0_mcap,
                    Outcome.last_mcap <= MAX_HONEST_MULTIPLE * Outcome.t0_mcap,
                ),
            ),
        )
        .order_by(Outcome.last_mcap.desc())
        .limit(20)
        .all()
    )
    mints = [str(mint) for (mint,) in rows if mint]
    if chain != "sol" or not mints:
        return mints
    honest = (
        session.query(Token.mint, Outcome.t0_mcap, Outcome.last_mcap)
        .join(Outcome, Outcome.token_id == Token.id)
        .filter(Token.mint.in_(mints))
        .all()
    )
    keep = {
        str(mint)
        for mint, t0, last in honest
        if float(t0 or 0.0) > 0 and float(last or 0.0) / float(t0) <= MAX_HONEST_MULTIPLE
    }
    return [mint for mint in mints if mint in keep]


@app.get("/api/hunt")
async def hunt_board(
    chain: str = Query("sol"),
    limit: int = Query(80, ge=1, le=200),
    hours: float = Query(0.0, ge=0.0, le=168.0),
):
    """This-window tape. Not the 12.8k leftover-sort."""
    return await asyncio.to_thread(_hunt_board_sync, chain, limit, hours)


def _hunt_board_sync(chain: str, limit: int, hours: float):
    from .chains import normalize_mint

    from .scoring.hunt import (
        HUNT_THIN_WATCH_LIVE_CAP,
        _age_hours,
        bloom_promises,
        conviction_from_tape,
        hunt_board_rank,
        hunt_leftover_window,
        hunt_live_value,
        hunt_thin_watch_book,
        latest_snap_tapes,
        list_hunt_mints,
        pick_live_conviction,
    )

    chain = normalize_chain(chain)
    with session_scope() as session:
        mints = list_hunt_mints(session, chain, hours=hours or None, limit=limit)
        if not mints:
            return {"items": [], "chain": chain, "source": "hunt"}
        hunts = {
            normalize_mint(row.mint, chain): row
            for row in session.query(HuntCard).filter(HuntCard.chain == chain, HuntCard.mint.in_(mints)).all()
        }
        promises = bloom_promises(session, mints)
        tokens = (
            session.query(Token)
            .options(
                selectinload(Token.research),
                selectinload(Token.outcome),
            )
            .filter(Token.mint.in_(mints), Token.chain == chain)
            .all()
        )
        by_mint = {normalize_mint(token.mint, chain): token for token in tokens}
        snap_tapes = latest_snap_tapes(session, [token.id for token in tokens])
        cards = []
        for mint in mints:
            key = normalize_mint(mint, chain)
            token = by_mint.get(key)
            if token is None:
                continue
            card = token_card(token)
            hunt = hunts.get(key)
            card["hunt"] = True
            card["stored_max_mcap"] = float(
                (token.outcome.max_mcap if token.outcome else 0.0) or 0.0
            )
            attach_snap_tape(card, snap_tapes.get(token.id))
            card.pop("stored_max_mcap", None)
            card.pop("snap_max_mcap", None)
            if hunt is not None:
                entry = float(hunt.entry_p or 0.0) or float(card.get("p_good") or 0.0)
                last = float(card.get("last_mcap") or hunt.last_mcap or 0.0)
                t0 = float(card.get("t0_mcap") or hunt.t0_mcap or 0.0)
                vol = card.get("volume_h1")
                from .research.holders import holder_tape

                tape_h = holder_tape(token.research)
                flags = [str(f) for f in (card.get("risk_flags") or [])]
                # The frozen scorer's lines, not the chain's: a legacy 0.6
                # and a first-sight 0.6 card can sit on the same tape.
                lines = lines_for_scorer(hunt.scorer or card.get("scorer"), hunt.chain or card.get("chain"))
                card["scorer"] = lines.scorer
                card["desk_lines"] = lines.as_dict()
                last_liq = float(card.get("last_liq") or hunt.last_liq or 0.0)
                holders = int(tape_h.get("n") or card.get("holder_count") or hunt.holders or 0)
                peak = float(card.get("max_mcap") or 0.0)
                tape = conviction_from_tape(
                    chain=chain,
                    entry_p=entry,
                    multiple=float(card.get("multiple") or hunt.multiple or 0.0),
                    last_mcap=last,
                    t0_mcap=t0,
                    max_mcap=peak,
                    last_liq=last_liq,
                    holders=holders,
                    top10_pct=float(card.get("top10_pct") or 0.0),
                    flags=flags,
                    label=card.get("label"),
                    vol_h1=None if vol is None else float(vol or 0.0),
                    holder_prev=tape_h.get("prev"),
                    holders_age_min=tape_h.get("age_min"),
                    thin_entry=lines.thin,
                )
                from .scoring.live_fit import live_features_for_token, live_model_p

                feats = live_features_for_token(
                    session,
                    token,
                    entry_p=entry,
                    t0_mcap=t0,
                    last_mcap=last,
                    last_liq=last_liq,
                    holders=holders,
                    peak=peak,
                    trough=last,
                    vol_h1=0.0 if vol is None else float(vol or 0.0),
                )
                model_p = live_model_p(session, chain, feats)
                unbacked = any("unbacked" in flag.lower() for flag in flags)
                thin = hunt_thin_watch_book(
                    entry,
                    flags,
                    thin_entry=lines.thin,
                    last_liq=last_liq,
                    holders=holders,
                )
                live = pick_live_conviction(
                    tape,
                    promises.get(mint, 0.0),
                    last_mcap=last,
                    t0_mcap=t0,
                    max_mcap=peak,
                    allow_bloom=not unbacked and not thin,
                    cap=HUNT_THIN_WATCH_LIVE_CAP if thin else None,
                    model_p=model_p,
                )
                card["entry_p"] = desk_entry_cap(entry, card)
                shown = hunt_live_value(last, live)
                card["conviction_p"] = shown
                card["live_model_p"] = model_p
                card["score"] = None if shown is None else round(shown * 100.0, 1)
                # Desk marks a capped Live so a wall of 62s reads as a cap, not a signal.
                card["live_cap"] = HUNT_THIN_WATCH_LIVE_CAP if thin else None
            age_h = _age_hours(token)
            card["hunt_age_hours"] = round(age_h, 2)
            card["leftover_window"] = hunt_leftover_window(chain, age_h)
            for heavy in ("thesis", "reasons", "creator", "github_url", "twitter", "website", "telegram"):
                card.pop(heavy, None)
            cards.append(card)
        cards.sort(key=hunt_board_rank, reverse=True)
        return {"items": cards, "chain": chain, "source": "hunt"}


@app.get("/api/bloom")
async def bloom_board(chain: str = Query("sol"), limit: int = Query(40, ge=1, le=80)):
    """Late bloomers: live tape looks promising after a weak (or stale) entry score.

    Entry p_good is not rewritten. Cards expose promise_p + bloom thesis.
    """
    return await asyncio.to_thread(_bloom_board_sync, chain, limit)


def _bloom_board_sync(chain, limit):
    from .scoring.bloom import bloom_bundle_farm, bloom_scam_book, bloom_tape_alive, bloom_whale_book, list_blooms

    chain = normalize_chain(chain)
    with session_scope() as session:
        blooms = list_blooms(session, chain, limit=limit)
        mints = [str(row.get("mint") or "") for row in blooms if row.get("mint")]
        if not mints:
            return {"items": [], "chain": chain}
        tokens = (
            session.query(Token)
            .options(
                selectinload(Token.research),
                selectinload(Token.outcome),
                selectinload(Token.snapshots),
            )
            .filter(Token.mint.in_(mints), Token.chain == chain)
            .all()
        )
        by_mint = {token.mint: token for token in tokens}
        hunt_entries = {
            mint: float(entry or 0.0)
            for mint, entry in session.query(HuntCard.mint, HuntCard.entry_p)
            .filter(HuntCard.chain == chain, HuntCard.mint.in_(mints))
            .all()
        }
        cards = []
        for row in blooms:
            token = by_mint.get(str(row.get("mint") or ""))
            if token is None:
                continue
            card = token_card(token)
            last = float(card.get("last_mcap") or row.get("last_mcap") or 0.0)
            t0 = float(card.get("t0_mcap") or row.get("t0_mcap") or 0.0)
            liq = float(card.get("last_liq") or row.get("last_liq") or 0.0)
            # Stored promise can sit at 59% after a rug until a refresh chair
            # ticks. Drop on the live tape so Bloom is not a graveyard.
            if not bloom_tape_alive(chain, last, t0, liq):
                continue
            if bloom_scam_book(card.get("risk_flags") or []):
                continue
            if bloom_bundle_farm(card.get("top10_pct"), card.get("holder_count")):
                continue
            if bloom_whale_book(card.get("top10_pct"), card.get("risk_flags") or []):
                continue
            card["promise_p"] = float(row.get("promise_p") or 0.0)
            card["conviction_p"] = float(row.get("promise_p") or 0.0)
            # Same frozen entry Hunt shows — not the repaired live p_good
            # the bloom tick stored (TTNB 85 on Hunt vs 92 on Bloom).
            card["entry_p"] = desk_entry_cap(
                frozen_entry_p(token, hunt_entries.get(token.mint)), card
            )
            card["score"] = round(float(row.get("promise_p") or 0.0) * 100.0, 1)
            card["thesis"] = row.get("thesis") or card.get("thesis") or ""
            card["bloom"] = True
            card["bloom_alerted"] = bool(row.get("alerted"))
            card["dev_tier"] = row.get("dev_tier") or ""
            card["dev_line"] = row.get("dev_line") or ""
            # Dive handle is the personal developer, not the token's main X.
            if row.get("dev_handle") and not card.get("dev_handle"):
                card["dev_handle"] = row["dev_handle"]
                card["has_dev_x"] = True
            if row.get("dev_github") and not card.get("github_url"):
                card["github_url"] = row["dev_github"]
            if row.get("reasons"):
                card["reasons"] = list(row.get("reasons") or []) + list(card.get("reasons") or [])
            cards.append(card)
        return {"items": cards, "chain": chain, "watched": True}


@app.get("/api/doing-well")
async def doing_well(chain: str = Query("sol")):
    """Names that are still doing well now — live 2×+ vs t0.

    Hunt leftover-sorts flats onto Live launches. This desk is the
    other tab: same research cards, no leftover factory tape.
    Reuses /api/runners + /api/approaching so start-high rugs, dead
    books, and 8h+ stale <3× stays stay off — then drops dumped-off-ATH
    highlight reels (BELIEVE 13% of peak, CAC $2k after 70×). A fat
    book still many multiples from t0 (live MEME) stays even when
    runners skip thin/copycat and the chart faded 40%+ from ATH.
    Sorted by live multiple, not the dumped ATH print.
    """
    return await asyncio.to_thread(_doing_well_sync, chain)


def _doing_well_sync(chain: str):
    chain = normalize_chain(chain)
    run = _runners_sync(5.0, chain)
    climb = _approaching_sync(2.0, 48.0, chain)
    live_mult = {
        str(row.get("mint") or ""): float(row.get("multiple") or 0.0)
        for row in (climb.get("items") or [])
        if row.get("mint")
    }
    mints: list[str] = []
    seen: set[str] = set()
    for row in list(run.get("items") or []) + list(climb.get("items") or []):
        mint = str(row.get("mint") or "")
        if not mint or mint in seen:
            continue
        seen.add(mint)
        mints.append(mint)
    with session_scope() as session:
        for mint in _doing_well_held_mints(session, chain):
            if mint in seen:
                continue
            seen.add(mint)
            mints.append(mint)
        if not mints:
            return {
                "items": [],
                "chain": chain,
                "runners": int(run.get("runners") or 0),
                "approaching": int(climb.get("approaching") or 0),
            }
        rows = (
            session.query(Token)
            .options(
                selectinload(Token.research),
                selectinload(Token.outcome),
                selectinload(Token.snapshots),
            )
            .filter(Token.mint.in_(mints), Token.chain == chain)
            .all()
        )
        by_mint = {token.mint: token for token in rows}
        from .scoring.hunt import hunt_live_value

        hunt_rows = {
            hunt_mint: (float(entry or 0.0), conv, float(hunt_last or 0.0))
            for hunt_mint, entry, conv, hunt_last in session.query(
                HuntCard.mint, HuntCard.entry_p, HuntCard.conviction_p, HuntCard.last_mcap
            )
            .filter(HuntCard.chain == chain, HuntCard.mint.in_(mints))
            .all()
        }
        rh_n_train = (
            get_or_create_model(session, chain="robinhood").n_train
            if chain == "robinhood"
            else None
        )
        cards = []
        for mint in mints:
            token = by_mint.get(mint)
            if token is None:
                continue
            card = token_card(token)
            # Entry column matches Hunt / Bloom: frozen, not the honesty-faded p_good.
            hunt_entry, hunt_live, hunt_last = hunt_rows.get(mint, (None, None, 0.0))
            card["entry_p"] = desk_entry_cap(frozen_entry_p(token, hunt_entry), card)
            last = float(card.get("last_mcap") or 0.0)
            if hunt_live is not None and hunt_last > 0:
                card["conviction_p"] = hunt_live_value(last, float(hunt_live))
            if rh_n_train is not None:
                apply_young_rh_desk_floor(card, rh_n_train)
                apply_stall_honesty(card)
            apply_ath_dump_honesty(card)
            if mint in live_mult and float(card.get("multiple") or 0.0) < 5.0:
                card["multiple"] = live_mult[mint]
            card["multiple"] = live_doing_well_multiple(card)
            # Live /rh: P sat 27× on Doing well after label=0 (prepump).
            # Held-mints does not look at label. A judged rug is not doing well.
            if card.get("label") == 0:
                continue
            if not still_doing_well(card):
                continue
            cards.append(card)
        cards.sort(key=lambda c: float(c.get("multiple") or 0.0), reverse=True)
        return {
            "items": cards[:40],
            "chain": chain,
            "runners": int(run.get("runners") or 0),
            "approaching": int(climb.get("approaching") or 0),
        }


@app.get("/api/projects")
async def dev_projects(hours: float = Query(48.0, ge=1.0, le=168.0), chain: str = Query("sol")):
    """Tokens that look like actual dev projects: genuine GitHub history plus
    a credible (non-hijacked) dev X account. The tech-narrative runner pool."""
    return await asyncio.to_thread(_dev_projects_sync, hours, chain)


def _dev_projects_sync(hours, chain):
    from .models import Research

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with session_scope() as session:
        rows = (
            session.query(Token, Research)
            .join(Research, Research.token_id == Token.id)
            .filter(
                Token.source != "backfill",
                Token.chain == normalize_chain(chain),
                Token.first_seen_at >= cutoff,
                Token.github_url != "",
            )
            .order_by(Research.p_good.desc())
            .limit(300)
            .all()
        )
        items = []
        for token, research in rows:
            try:
                features = json.loads(research.features_json or "{}")
            except json.JSONDecodeError:
                continue
            if not features.get("real_project"):
                continue
            items.append(
                {
                    "symbol": token.symbol,
                    "name": token.name,
                    "mint": token.mint,
                    "p_good": round(research.p_good or 0.0, 3),
                    "github": token.github_url,
                    "github_stars": research.github_stars,
                    "x_handle": research.twitter_handle,
                    "x_followers": research.twitter_followers,
                    "gmgn": gmgn_token_url(token.mint, token.chain),
                    "chain": token.chain,
                }
            )
        return {"window_hours": hours, "projects": len(items), "items": items[:40]}


def _historical_rh_mints(session, mints: list[str]) -> set[str]:
    """Quiet-retired RH leftovers (AIAIAI) can keep a runnerp row."""
    if not mints:
        return set()
    return {
        mint
        for (mint,) in session.query(Token.mint).filter(
            Token.chain == "robinhood",
            Token.is_historical.is_(True),
            Token.mint.in_(mints),
        )
    }


def _thin_watch_mints(session, mints: list[str], chain: str) -> set[str]:
    """Thin Dex wicks are not runner-watch candidates.

    RH: 4-wallet CYBERCAB / DESPONSITO. Sol: 5-wallet USMS 65× sitting
    on /api/runnerwatch. Unknown (0) holders stay. Pappy-class 13-wallet
    Sol samples are above the Sol floor of 8.
    """
    if not mints:
        return set()
    from .scoring.outcomes import is_thin_holder_print

    chain = normalize_chain(chain)
    rows = (
        session.query(Token.mint, Research.holder_count)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == chain, Token.mint.in_(mints))
        .all()
    )
    return {mint for mint, n in rows if is_thin_holder_print(n, chain)}


def _dumped_watch_mints(
    session, mints: list[str], chain: str
) -> tuple[set[str], dict[str, float], dict[str, float]]:
    """Stale runnerp rows keep a dumped leftover on the climb list.

    Live LAST: confirmed 6.2× runner, ScanState still 3.25× from 00:23
    with last_liq $5600. Approaching already uses last snap / t0.
    No Outcome yet (tests / brand-new ingest) is left alone.

    Also returns live multiple (last snap / t0) so watch/conviction
    can show the climb, not a stale ScanState peak. Live ASS 3.02
    ScanState vs approaching 2.03.

    liq_map carries Outcome.last_liq so watch matches approaching
    (live ASS Dex-miss 0, live YOLO stale $3.7k vs $45k book).
    """
    if not mints:
        return set(), {}, {}
    from .scoring.features import is_rh_airdrop_book, is_sol_airdrop_tape
    from .scoring.outcomes import (
        PREPUMP_FLAG_NEEDLES,
        SKINNY_APPROACH_HOURS,
        SKINNY_HUNT_LIQ,
        STALE_APPROACH_HOURS,
        STALE_APPROACH_MULTIPLE,
        WATCH_MIN_MULTIPLE,
        approaching_live_multiple,
        is_bundle_copycat_run,
        is_prepumped_entry,
    )

    chain = normalize_chain(chain)
    win = float(settings.win_multiple)
    rows = (
        session.query(Token, Outcome, Research)
        .join(Outcome, Outcome.token_id == Token.id)
        .join(Research, Research.token_id == Token.id)
        .filter(Token.chain == chain, Token.mint.in_(mints))
        .all()
    )
    skip: set[str] = set()
    live_map: dict[str, float] = {}
    liq_map: dict[str, float] = {}
    for token, outcome, research in rows:
        # Live SHARK / JOHNAPPLE / WATER: 24h label=0 still sat on
        # watch as 4.11 / 2.47 / 2.5 because ScanState stayed in the
        # 2×–5× band. Approaching already excludes labeled rows.
        if outcome.label is not None:
            skip.add(token.mint)
            continue
        # Live NTDA sat watch A 4.46× / $100k while approaching
        # already dropped the instant-fill + same-ticker copycat.
        # PIG / Dancedog / SOLANAIFY are start-high rugs; LQX is
        # pre-pumped leftover FDV. Same needles as approaching.
        if is_bundle_copycat_run(research):
            skip.add(token.mint)
            continue
        flags = (research.risk_flags_json or "").lower()
        if any(needle in flags for needle in PREPUMP_FLAG_NEEDLES):
            skip.add(token.mint)
            continue
        # Live Dancedoge: t0 $8.5M / 11 wallets / 2.3× sat watch C
        # $368k. Approaching already drops is_prepumped_entry. Flag
        # needles miss a huge t0 with no "pre-pumped" string.
        if is_prepumped_entry(session, token, research, outcome):
            skip.add(token.mint)
            continue
        if is_rh_airdrop_book(
            chain, research.holder_count, outcome.last_liq
        ):
            skip.add(token.mint)
            continue
        # Live 12:38: Dark Arena 291w / $3.0k / 3.43× / 3.5h sat
        # runnerwatch. Bunny 4.98× / SUNLIVE $30k stay.
        watch_holders = int(research.holder_count or 0)
        if (
            chain == "sol"
            and watch_holders >= 200
            and 800.0 <= float(outcome.last_liq or 0.0) < 8_000.0
            and float(outcome.multiple or 0.0) < 4.0
        ):
            launched = token.created_at_chain or token.first_seen_at
            if launched is not None:
                if launched.tzinfo is None:
                    launched = launched.replace(tzinfo=timezone.utc)
                watch_age_h = (
                    datetime.now(timezone.utc) - launched
                ).total_seconds() / 3600.0
            else:
                watch_age_h = 0.0
            if watch_age_h >= 3.0:
                skip.add(token.mint)
                continue
        last_liq = float(outcome.last_liq or 0.0)
        if last_liq > 0:
            liq_map[token.mint] = last_liq
        live = approaching_live_multiple(session, token, outcome)
        live_map[token.mint] = live
        # Live 13:08: Birkin 2.04× / 578w / $19k sat approaching.
        # Hunt already buries airdrop tape. Sat-dust needs <$8k.
        # Bunny 4.98× / SUNLIVE 3.70× stay (live ≥ 2.5).
        if is_sol_airdrop_tape(chain, watch_holders, last_liq) and live < 2.5:
            skip.add(token.mint)
            continue
        if live < WATCH_MIN_MULTIPLE or live >= win:
            skip.add(token.mint)
            continue
        # Live BELIEVE 28.5× / SANDIH 8.9× / A 5.1× sat on watch
        # because ScanState still said 3.55 / 4.38 / 4.16. Confirmed
        # 5× belong on /api/runners even with a fat book. LAST $5.6k
        # leftover is the dust-book case of the same rule. YOLO 2.81
        # and GUH 2.52 stay.
        stored = float(outcome.multiple or 0.0)
        if stored >= win:
            skip.add(token.mint)
            continue
        # Live Bros 2.16× / stonkape 4.15× sat on Sol watch (Bros as
        # Tier B) after approaching already dropped last_liq $0 ghosts.
        # Mid-book $400 (PSYOPED) stays; a missing pool is not a climb.
        if last_liq <= 0:
            skip.add(token.mint)
            continue
        # Live 08:40: ☉ 13.9h / 3.70× / $3.2k sat conviction B after
        # approaching already dropped skinny dumps. Live 11:11:
        # TriplePONS 4.9h / $4.2k sat runnerwatch #7. PSYOPED $400
        # this-window stays. Do not restore ☉ hunt.
        launched = token.created_at_chain or token.first_seen_at
        if launched is not None:
            if launched.tzinfo is None:
                launched = launched.replace(tzinfo=timezone.utc)
            age_h = (
                datetime.now(timezone.utc) - launched
            ).total_seconds() / 3600.0
        else:
            age_h = 0.0
        if 0 < last_liq < SKINNY_HUNT_LIQ and age_h >= SKINNY_APPROACH_HOURS:
            skip.add(token.mint)
            continue
        # Live 11:21: MONK 17.7h / 2.69× sat runnerwatch + conviction
        # B. An 8h+ book still under 3× is leftover tape. BEN
        # this-window 2× stays. 007 3×+ stays. Hunt 2×+ stay.
        if age_h >= STALE_APPROACH_HOURS and live < STALE_APPROACH_MULTIPLE:
            skip.add(token.mint)
    return skip, live_map, liq_map


def _stamp_live_watch_multiple(
    data: dict, mint: str, live_map: dict[str, float], liq_map: dict[str, float] | None = None
) -> None:
    live = live_map.get(mint)
    if live is not None:
        data["mcap_ratio"] = round(float(live), 2)
    # Live ASS: ScanState Dex-miss wrote liq_now=0. Live YOLO:
    # ScanState still $3.7k while Outcome / approaching was $45k.
    # Always overlay the Outcome book; do not drop a fat climber.
    if liq_map:
        book = liq_map.get(mint)
        if book is not None:
            data["liq_now"] = round(float(book))


def _fresh_runnerp(data: dict, freshness: datetime) -> bool:
    try:
        seen_at = datetime.fromisoformat(data.get("at") or "")
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        return seen_at >= freshness
    except ValueError:
        return False


def _live_or_fresh_runnerp(
    data: dict, freshness: datetime, mint: str, live_map: dict[str, float]
) -> bool:
    # Live 05:40: ☉ / MONK / draw still sat approaching A (3.7 / 2.69 /
    # 2.05) but ScanState `at` was ~10.5h old. A 12h cut on `at` drops
    # them around 07:05 even though Outcome is live. A 2×–5× overlay
    # is the freshness signal; day-old ScanState without a live book
    # still expires.
    if mint in live_map:
        return True
    return _fresh_runnerp(data, freshness)


def _already_ran_watch(data: dict) -> bool:
    """Confirmed 5x belongs on /api/runners, not the climb watch.

    Live Sol watch ranked USMS 65× / HeeHaw 48× / TJR 23× as if they
    were still approaching. RH did the same with ROCK 15× / POV 11× /
    LAST 6×. Those names already cleared the win bar.
    """
    return float(data.get("mcap_ratio") or 0.0) >= float(settings.win_multiple)


def _not_yet_climbing_watch(data: dict) -> bool:
    """Flat 1x leftovers are not a runner watch.

    After dropping 5x names, Sol still listed ~3200 rows and RH's top
    30 was A 1.62 / CHARLES 1.46 / HOTDOG 1.32. Same 2x floor as
    approaching.
    """
    from .scoring.outcomes import WATCH_MIN_MULTIPLE

    return float(data.get("mcap_ratio") or 0.0) < WATCH_MIN_MULTIPLE


@app.get("/api/runnerwatch")
async def runner_watch_board(chain: str = Query("sol")):
    """Live tokens ranked by 1h runner potential (trajectory, not entry)."""
    return await asyncio.to_thread(_runner_watch_board_sync, chain)


def _runner_watch_board_sync(chain: str):
    from .models import ScanState
    from .scoring.outcomes import MAX_HONEST_MULTIPLE

    freshness = datetime.now(timezone.utc) - timedelta(hours=12)
    with session_scope() as session:
        rows = session.query(ScanState).filter(ScanState.key.like("runnerp:%")).all()
        chain = normalize_chain(chain)
        parsed: list[tuple[str, dict]] = []
        for row in rows:
            try:
                data = json.loads(row.value or "{}")
            except json.JSONDecodeError:
                continue
            if normalize_chain(data.get("chain") or "sol") != chain:
                continue
            # Stale ScanState can keep HeeHaw-class 98x artifacts on the board
            # after runners already cap at 80x.
            if float(data.get("mcap_ratio") or 0.0) > MAX_HONEST_MULTIPLE:
                continue
            # Freshness waits for the Outcome overlay — a live 2×–5×
            # book (☉ 10.5h-old `at`) stays. already_ran / sub-2×
            # still drop before the Token query so USMS 65× / flat
            # leftovers do not inflate the mint list.
            if _already_ran_watch(data):
                continue
            if _not_yet_climbing_watch(data):
                continue
            parsed.append((row.key.split(":", 1)[1], data))
        mints = [mint for mint, _ in parsed]
        skip = _thin_watch_mints(session, mints, chain)
        dumped, live_map, liq_map = _dumped_watch_mints(session, mints, chain)
        skip |= dumped
        if chain == "robinhood":
            skip |= _historical_rh_mints(session, mints)
        items = []
        for mint, data in parsed:
            if mint in skip:
                continue
            _stamp_live_watch_multiple(data, mint, live_map, liq_map)
            if not _live_or_fresh_runnerp(data, freshness, mint, live_map):
                continue
            data["mint"] = mint
            data["gmgn"] = gmgn_token_url(mint, chain)
            items.append(data)
        # runner_p saturates at 1.0 (live BOMBA 1.81x and ROCK 5.4x both
        # scored 1.0). Tie-break on the actual multiple so 5x names win.
        # Live ANGRYCATS $2.1k (B) sat above Pumpooor $76k (C). Fat
        # books first — the desk hunts 5-50x, not a dumped mid-book.
        # Live 20:40: BWA 9w / $28k sat #8 above ROBBIE 30w. Hunt and
        # approaching already use the 15-wallet Sol line; watch now
        # matches. Stay on the board — Pappy-class 13w is not skipped
        # (runner floor stays 8).
        from .scoring.outcomes import SKINNY_HUNT_LIQ, is_thin_approaching_print

        watch_mints = [d.get("mint") for d in items if d.get("mint")]
        holder_map: dict[str, int | None] = {}
        if watch_mints:
            holder_map = {
                mint: n
                for mint, n in (
                    session.query(Token.mint, Research.holder_count)
                    .join(Research, Research.token_id == Token.id)
                    .filter(Token.chain == chain, Token.mint.in_(watch_mints))
                    .all()
                )
            }

        items.sort(
            key=lambda d: (
                1 if float(d.get("liq_now") or 0) >= SKINNY_HUNT_LIQ else 0,
                0 if is_thin_approaching_print(holder_map.get(d.get("mint")), chain) else 1,
                d.get("runner_p") or 0,
                d.get("mcap_ratio") or 0,
            ),
            reverse=True,
        )
        return {"watching": len(items), "items": items[:30], "chain": chain}


@app.get("/api/conviction")
async def conviction_board(chain: str = Query("sol")):
    """The short list: tokens where independent systems agree. Tier A =
    every gate passed (entry score, trajectory, wallet structure, real
    liquidity, no hard stops); Tier B = watchlist. C is never shown."""
    from .models import ScanState
    from .scoring.outcomes import CONVICTION_MIN_LIQ, MAX_HONEST_MULTIPLE

    freshness = datetime.now(timezone.utc) - timedelta(hours=12)
    with session_scope() as session:
        rows = session.query(ScanState).filter(ScanState.key.like("runnerp:%")).all()
        chain = normalize_chain(chain)
        parsed: list[tuple[str, dict]] = []
        for row in rows:
            try:
                data = json.loads(row.value or "{}")
            except json.JSONDecodeError:
                continue
            if normalize_chain(data.get("chain") or "sol") != chain:
                continue
            if float(data.get("mcap_ratio") or 0.0) > MAX_HONEST_MULTIPLE:
                continue
            if _already_ran_watch(data):
                continue
            if _not_yet_climbing_watch(data):
                continue
            parsed.append((row.key.split(":", 1)[1], data))
        mints = [mint for mint, _ in parsed]
        skip = _thin_watch_mints(session, mints, chain)
        dumped, live_map, liq_map = _dumped_watch_mints(session, mints, chain)
        skip |= dumped
        if chain == "robinhood":
            skip |= _historical_rh_mints(session, mints)
        tiers: dict[str, list] = {"A": [], "B": []}
        for mint, data in parsed:
            if mint in skip:
                continue
            _stamp_live_watch_multiple(data, mint, live_map, liq_map)
            # Live ANGRYCATS $2.1k sat conviction B after the dump.
            # Watch still lists it; conviction needs a known real pool.
            # Missing liq_now is a legacy ScanState — leave it.
            if data.get("liq_now") is not None and float(data.get("liq_now") or 0.0) < CONVICTION_MIN_LIQ:
                continue
            tier = data.get("tier")
            if tier not in tiers:
                continue
            # Stale `at` still expires leftovers. A live Outcome
            # overlay (☉ / MONK / draw) keeps the short list honest.
            if not _live_or_fresh_runnerp(data, freshness, mint, live_map):
                continue
            data["mint"] = mint
            data["gmgn"] = gmgn_token_url(mint, chain)
            tiers[tier].append(data)
        for bucket in tiers.values():
            bucket.sort(key=lambda d: (d.get("runner_p") or 0, d.get("mcap_ratio") or 0), reverse=True)
        return {
            "tier_a": tiers["A"][:20],
            "tier_b": tiers["B"][:30],
            "chain": chain,
            "note": "Tier A alerts; Tier B is a watchlist; everything else is background.",
        }


@app.get("/api/report")
async def daily_report(chain: str = Query("sol")):
    """One-URL digest of the last 24h: ingest volume, label outcomes, the
    current top-scored live tokens, and the evaluation trend."""
    from .models import Research
    from .scoring.model import evaluation_history

    chain = normalize_chain(chain)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with session_scope() as session:
        ingested = session.query(Token).filter(Token.first_seen_at >= cutoff, Token.chain == chain).count()
        live_now = session.query(Token).filter(Token.is_historical.is_(False), Token.chain == chain).count()
        labeled = (
            session.query(Outcome)
            .join(Token, Token.id == Outcome.token_id)
            .filter(Token.chain == chain, Outcome.labeled_at.is_not(None), Outcome.labeled_at >= cutoff)
        )
        labels_24h = labeled.count()
        wins_24h = labeled.filter(Outcome.label == 1).count()
        top_live = (
            session.query(Token, Research)
            .join(Research, Research.token_id == Token.id)
            .filter(Token.is_historical.is_(False), Token.chain == chain)
            .order_by(Research.p_good.desc())
            .limit(5)
            .all()
        )
        history = evaluation_history(session, chain=chain)
        trend = {}
        if history:
            trend = {"first": history[0], "latest": history[-1], "snapshots": len(history)}
        from .scoring.outcomes import confirmed_runner_multiple, is_bundle_copycat_run, is_prepumped_entry

        runner_filters = [
            Outcome.multiple >= 10.0,
            Token.source != "backfill",
            Token.chain == chain,
            Token.first_seen_at >= cutoff,
            (Outcome.label.is_(None)) | (Outcome.label == 1),
            (Outcome.label == 1) | (Outcome.last_liq >= 800),
            ~Research.risk_flags_json.like("%start-high rug%"),
            ~Research.risk_flags_json.like("%Pre-pumped%"),
        ]
        if chain == "robinhood":
            runner_filters.append(Token.is_historical.is_(False))
        runner_cands = (
            session.query(Token, Research, Outcome)
            .join(Outcome, Outcome.token_id == Token.id)
            .join(Research, Research.token_id == Token.id)
            .filter(*runner_filters)
            .limit(400)
            .all()
        )
        runners_24h = sum(
            1
            for token, research, outcome in runner_cands
            if not is_prepumped_entry(session, token, research, outcome)
            and not is_bundle_copycat_run(research)
            and confirmed_runner_multiple(session, token, outcome) >= 10.0
        )
        return {
            "window_hours": 24,
            "ingested_24h": ingested,
            "live_now": live_now,
            "labels_24h": labels_24h,
            "wins_24h": wins_24h,
            "win_rate_24h": round(wins_24h / labels_24h, 3) if labels_24h else None,
            "runners_10x_24h": runners_24h,
            "win_definition": f"{settings.win_multiple}x without liquidity collapse",
            "top_live": [
                {
                    "symbol": t.symbol,
                    "mint": t.mint,
                    "p_good": r.p_good,
                    "gmgn": gmgn_token_url(t.mint, t.chain),
                    "chain": t.chain,
                }
                for t, r in top_live
            ],
            "evaluation_trend": trend,
            "chain": chain,
            "note": "Research probabilities, not financial advice.",
        }


@app.post("/webhooks/helius")
@app.post("/webhooks/migrate")
async def webhook(request: Request):
    payload = await request.json()
    mints = await handle_webhook(payload, source="webhook")
    return {"ingested": mints}


@app.post("/api/scan-now")
async def scan_now(chain: str = Query("sol")):
    from .db import ingest_lock
    from .scoring.outcomes import refresh_outcomes

    chain = normalize_chain(chain)
    if chain == "robinhood":
        from .ingest.rh_poll import poll_robinhood

        news = await poll_robinhood()
    else:
        from .ingest.pump_poll import poll_new_migrations

        news = await poll_new_migrations()
    async with ingest_lock:
        with session_scope() as session:
            await refresh_outcomes(session)
    return {"new": news, "chain": chain}
