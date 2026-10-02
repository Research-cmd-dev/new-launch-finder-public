from __future__ import annotations

import asyncio
import logging

from .config import settings
from .db import ingest_lock, init_db, session_scope
from .ingest.pump_poll import backfill_history, poll_new_migrations
from .ingest.rh_poll import backfill_robinhood, poll_robinhood
from .ingest.bitquery import listen_rh_pools
from .ingest.solana_ws import listen_migrations
from .scoring.outcomes import (
    apply_label_fixes,
    refresh_outcomes,
    repair_corrupt_mcaps,
    repair_entry_prices,
    repair_rh_graduation_floor,
    repair_rh_holders_from_gmgn,
    repair_rh_instant_fill,
    repair_rh_young_model_floor,
    repair_rh_small_book_collapse,
    repair_organic_book_scores,
    repair_organic_wide_book_scores,
    repair_rh_thin_book_scores,
    repair_rh_empty_book_scores,
    repair_rh_empty_book_from_last_liq,
    repair_hunt_mint_case,
    park_boner_reference,
)

log = logging.getLogger("launchfinder.worker")
_stop = asyncio.Event()
_task: asyncio.Task | None = None

# Boot must not block Hunt tape / ingest. One-shots that already ran return
# immediately; a new repair (v146 hunt mint align) must not hold the loop for
# a full-table ORM scan on Postgres.
BOOT_REPAIR_BUDGET_SECONDS = 75.0


def configure_worker_logging() -> None:
    """Uvicorn already configured logging, so basicConfig is a no-op.

    Live v61 worker printed only the uvicorn banner for 80m — INFO from
    this package never reached Railway. Attach our own stream handler.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    pkg = logging.getLogger("launchfinder")
    pkg.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in pkg.handlers):
        handler = logging.StreamHandler()
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        pkg.addHandler(handler)
    pkg.propagate = False


async def _run_backfill() -> None:
    try:
        await backfill_history(limit=settings.backfill_limit)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("historical backfill failed")


async def _run_zcat_analytics() -> None:
    try:
        from .ingest.zcat_seed import ensure_zcat_reference, refresh_zcat_early_wallets

        await ensure_zcat_reference()
        wrote = await refresh_zcat_early_wallets()
        if not wrote:
            await asyncio.sleep(45)
            await refresh_zcat_early_wallets()
        from .research.early_wallets import harvest_confirmed_early

        await harvest_confirmed_early(limit=6)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("ZCAT analytics seed failed")


async def _run_rh_backfill() -> None:
    try:
        await backfill_robinhood()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Robinhood backfill failed")


def restore_watch_boards() -> None:
    """Reload both graduating strips from persist before the first poll.

    Deploy boots wipe in-memory preview. GET /api/watch already restores,
    but RH _store used empty memory for first_seen and restamped clocks.
    Call after init_db so the first trench write keeps persist ages.
    """
    from .ingest import pump_poll, rh_poll

    rh_poll._restore_watch_preview()
    pump_poll._restore_watch_preview()


def _boot_repairs(*, budget_seconds: float | None = BOOT_REPAIR_BUDGET_SECONDS) -> None:
    import time

    from .ingest.zcat_seed import park_zcat_reference

    deadline = time.monotonic() + budget_seconds if budget_seconds else None
    log.info("boot repairs start")
    repairs = (
        apply_label_fixes,
        repair_entry_prices,
        repair_corrupt_mcaps,
        repair_rh_graduation_floor,
        repair_rh_holders_from_gmgn,
        repair_rh_instant_fill,
        repair_rh_young_model_floor,
        repair_rh_small_book_collapse,
        repair_organic_book_scores,
        repair_organic_wide_book_scores,
        repair_rh_thin_book_scores,
        repair_rh_empty_book_scores,
        repair_rh_empty_book_from_last_liq,
        park_boner_reference,
        park_zcat_reference,
    )
    skipped = 0
    for repair in repairs:
        if deadline is not None and time.monotonic() > deadline:
            skipped += 1
            continue
        t0 = time.monotonic()
        try:
            with session_scope() as session:
                repair(session)
        except Exception:
            log.exception("boot repair %s failed", repair.__name__)
            continue
        log.info("boot repair %s ok in %.1fs", repair.__name__, time.monotonic() - t0)
    if skipped:
        log.warning("boot repairs: time budget — skipped %s one-shots (already ran or defer)", skipped)
    log.info("boot repairs done")


def _deferred_boot_repairs() -> None:
    """Off the ingest-lock critical path — hunt mint align can be heavy once."""
    import time

    t0 = time.monotonic()
    try:
        with session_scope() as session:
            repair_hunt_mint_case(session)
    except Exception:
        log.exception("deferred repair_hunt_mint_case failed")
    else:
        log.info("deferred boot repair hunt_mint_case ok in %.1fs", time.monotonic() - t0)


async def _boot_maintenance() -> None:
    """Repairs + Hunt rebuild without blocking tape / ingest loops."""
    try:
        await asyncio.to_thread(_boot_repairs)
    except Exception:
        log.exception("boot repairs failed")
    try:
        async with ingest_lock:
            await asyncio.to_thread(_boot_hunt_rebuild)
    except Exception:
        log.exception("boot hunt rebuild failed")
    try:
        await asyncio.to_thread(_deferred_boot_repairs)
    except Exception:
        log.exception("deferred boot repairs failed")


def _boot_hunt_rebuild() -> None:
    """Sync Hunt rebuilds must not run on the asyncio loop.

    Live 2026-09-27: worker deploy FAILED healthcheck at ~180s while
    `rebuild_hunt_window(robinhood, limit=800)` held the event loop after
    Sol rebuild — `/health` could not answer. Boot repairs already use
    `asyncio.to_thread`; Hunt rebuild belongs there too.
    """
    from .scoring.hunt import rebuild_hunt_window

    with session_scope() as session:
        rebuild_hunt_window(session, "sol")
        if settings.robinhood_enabled:
            # 24h RH window — 400 newest missed 12–24h cards on boot.
            rebuild_hunt_window(session, "robinhood", limit=800)


def _beat(name: str, note: str = "") -> None:
    try:
        from .ledger import beat

        with session_scope() as session:
            beat(session, name, note=note)
    except Exception:
        log.exception("heartbeat %s failed", name)


def _seed_ledger() -> None:
    from .ledger import RESEED_RETIRED_KEY, seed_decisions_from_t0

    try:
        with session_scope() as session:
            n = seed_decisions_from_t0(session)
        if n:
            log.info("ledger seed wrote %s decisions", n)
        # Second pass: first-sight scores on tokens the desk had already
        # retired when the first seed ran (the losers). Own key, own commit.
        with session_scope() as session:
            n = seed_decisions_from_t0(session, retired=True, key=RESEED_RETIRED_KEY)
        if n:
            log.info("ledger retired seed wrote %s decisions", n)
        # stack-v76: scorer stamps + seed line-row source, once.
        from .ledger import repair_decision_scorers, repair_gate_scorers

        with session_scope() as session:
            fixed = repair_decision_scorers(session)
        if not fixed.get("skipped"):
            log.info("ledger scorer repair: %s", fixed)
        with session_scope() as session:
            gates = repair_gate_scorers(session)
        if not gates.get("skipped"):
            log.info("ledger gate scorer repair: %s", gates)
    except Exception:
        log.exception("ledger seed failed")


def _sample_live() -> int:
    from .scoring.live_fit import repair_live_sample_holders, sample_live

    n = 0
    with session_scope() as session:
        for chain in ("sol", "robinhood") if settings.robinhood_enabled else ("sol",):
            n += sample_live(session, chain)
            n += repair_live_sample_holders(session, chain)
    return n


def _sample_live_history() -> int:
    from .scoring.live_fit import sample_live_history

    n = 0
    with session_scope() as session:
        for chain in ("sol", "robinhood") if settings.robinhood_enabled else ("sol",):
            n += sample_live_history(session, chain)
    return n


def _sync_paper_ledger() -> dict[str, int]:
    from .ledger import (
        lock_paper_v1,
        promote_paper_v1_queue,
        reconsider_copycat_veto_shadow,
        reconsider_high_si_fomo_shadow,
        reconsider_meme_quality_shadow,
        reconsider_paper_v1,
        sync_paper_ledger,
    )
    from .scoring.sanity_jobs import apply_sanity_jobs_sync
    from .scoring.thesis_enrich import repair_desk_thesis_coverage, repair_thin_entry_thesis
    from .scoring.thesis_weights import refit_thesis_weights

    totals = {"opened": 0, "marked": 0}
    ledger_synced = False
    with session_scope() as session:
        try:
            repaired = repair_thin_entry_thesis(session, limit=200)
            totals["thesis_patched"] = int(repaired.get("patched") or 0)
            v1_thesis = repair_desk_thesis_coverage(session, open_limit=150, hunt_limit=80, shadow_limit=40)
            totals["v1_open_thesis_patched"] = int(v1_thesis.get("patched") or 0)
            totals["v1_open_hard_tags"] = int(v1_thesis.get("gained_hard_tag") or 0)
            totals["hunt_thesis_gained"] = int(v1_thesis.get("hunt_gained_hard_tag") or 0)
            totals["shadow_thesis_gained"] = int(v1_thesis.get("shadow_gained_hard_tag") or 0)
        except Exception:
            log.exception("thesis repair on paper sync failed")
            totals["thesis_patched"] = 0
            totals["v1_open_thesis_patched"] = 0
            totals["v1_open_hard_tags"] = 0
            totals["hunt_thesis_gained"] = 0
            totals["shadow_thesis_gained"] = 0
        # Gated improve: runner thesis repair + Live@entry freeze (HTTP enrich
        # is one-knob / Learn apply — not every paper sync).
        try:
            sync_actions = ["repair_thin_thesis", "freeze_live_coverage"]
            jobs = apply_sanity_jobs_sync(session, "sol", actions=sync_actions)
            if settings.robinhood_enabled:
                rh_jobs = apply_sanity_jobs_sync(session, "robinhood", actions=sync_actions)
                totals["live_frozen"] = int((jobs.get("freeze_live_coverage") or {}).get("frozen") or 0) + int(
                    (rh_jobs.get("freeze_live_coverage") or {}).get("frozen") or 0
                )
                totals["runners_thesis_patched"] = int(
                    ((jobs.get("repair_thin_thesis") or {}).get("runners") or {}).get("patched") or 0
                ) + int(((rh_jobs.get("repair_thin_thesis") or {}).get("runners") or {}).get("patched") or 0)
            else:
                totals["live_frozen"] = int((jobs.get("freeze_live_coverage") or {}).get("frozen") or 0)
                totals["runners_thesis_patched"] = int(
                    ((jobs.get("repair_thin_thesis") or {}).get("runners") or {}).get("patched") or 0
                )
        except Exception:
            log.exception("sanity jobs on paper sync failed")
            totals["live_frozen"] = 0
            totals["runners_thesis_patched"] = 0
        totals["v1_reconsidered"] = 0
        totals["v1_reconsider_shadow"] = 0
        totals["v1_copycat_veto_shadow"] = 0
        totals["v1_high_si_shadow"] = 0
        totals["v1_meme_quality_shadow"] = 0
        paper_sync_beat_sent = False
        for chain in ("sol", "robinhood") if settings.robinhood_enabled else ("sol",):
            try:
                with session.begin_nested():
                    out = sync_paper_ledger(session, chain)
                    totals["opened"] += int(out.get("opened") or 0)
                    totals["marked"] += int(out.get("marked") or 0)
                ledger_synced = True
                if not paper_sync_beat_sent:
                    _beat(
                        "paper_sync",
                        note=(
                            f"opened={totals.get('opened', 0)} "
                            f"marked={totals.get('marked', 0)} chain={chain}"
                        ),
                    )
                    paper_sync_beat_sent = True
            except Exception:
                log.exception("sync_paper_ledger failed for %s", chain)
            # Cycle-4 knob: Sol wide fills that opened Live-cold get a second
            # look at the current print for PAPER_V1_RECONSIDER_HOURS.
            try:
                again = reconsider_paper_v1(session, chain)
                totals["v1_reconsidered"] += again["queued"] + again["opened"]
                totals["v1_reconsider_shadow"] += again["shadow"]
            except Exception:
                log.exception("paperV1 reconsider on paper sync failed for %s", chain)
            try:
                with session.begin_nested():
                    copycat = reconsider_copycat_veto_shadow(session, chain)
                    totals["v1_copycat_veto_shadow"] += int(copycat.get("shadow") or 0)
            except Exception:
                log.exception("paperV1 copycat-veto shadow on paper sync failed for %s", chain)
            try:
                with session.begin_nested():
                    si = reconsider_high_si_fomo_shadow(session, chain)
                    totals["v1_high_si_shadow"] += int(si.get("shadow") or 0)
            except Exception:
                log.exception("paperV1 high-SI fomo shadow on paper sync failed for %s", chain)
            try:
                with session.begin_nested():
                    meme_q = reconsider_meme_quality_shadow(session, chain)
                    totals["v1_meme_quality_shadow"] += int(meme_q.get("shadow") or 0)
            except Exception:
                log.exception("paperV1 meme-quality shadow on paper sync failed for %s", chain)
        # Midday: open queued names that cleared Live≥0.70 / strong thesis.
        try:
            promoted = promote_paper_v1_queue(session)
            totals["v1_promoted"] = int(promoted.get("opened") or 0)
        except Exception:
            log.exception("paperV1 promote on paper sync failed")
            totals["v1_promoted"] = 0
        # One lock after both chains have queued, so Solana cannot spend
        # the five slots before Robinhood is in the same day's list.
        try:
            locked = lock_paper_v1(session)
            totals["v1_opened"] = int(locked.get("opened") or 0)
            totals["v1_skipped"] = int(locked.get("skipped") or 0)
        except Exception:
            log.exception("paperV1 lock on paper sync failed")
            totals["v1_opened"] = 0
            totals["v1_skipped"] = 0
        try:
            fitted = refit_thesis_weights(session)
            totals["thesis_refit"] = 1 if fitted.get("fitted") else 0
            totals["thesis_refit_n"] = int(fitted.get("n") or 0)
            totals["policy_flipped"] = 1 if fitted.get("policy_flipped") else 0
        except Exception:
            log.exception("thesis weight refit failed")
            totals["thesis_refit"] = 0
    return totals


def _batch_fit_cycle() -> dict[str, dict]:
    from .scoring.batch_fit import fit_entry_model
    from .scoring.first_sight import FIRST_SIGHT_FIT_CHAINS, fit_first_sight
    from .scoring.live_fit import fit_live_model, sample_live_history

    out: dict[str, dict] = {}
    for chain in ("sol", "robinhood") if settings.robinhood_enabled else ("sol",):
        with session_scope() as session:
            try:
                out[f"entry:{chain}"] = fit_entry_model(session, chain)
            except Exception:
                log.exception("entry batch fit failed for %s", chain)
        with session_scope() as session:
            try:
                n = sample_live_history(session, chain)
                if n:
                    log.info("live history sampled %s %s", n, chain)
            except Exception:
                log.exception("live history sample failed for %s", chain)
        with session_scope() as session:
            try:
                out[f"live:{chain}"] = fit_live_model(session, chain)
            except Exception:
                log.exception("live batch fit failed for %s", chain)
        if chain in FIRST_SIGHT_FIT_CHAINS:
            with session_scope() as session:
                try:
                    out[f"first_sight:{chain}"] = fit_first_sight(session, chain)
                except Exception:
                    log.exception("first-sight fit failed for %s", chain)
    return out


def _demote_unfrozen() -> list[dict]:
    from .scoring.batch_fit import demote_unfrozen_fits

    with session_scope() as session:
        return demote_unfrozen_fits(session)


async def _vendor_live_history() -> int:
    """GMGN kline + Bitquery trades for Live samples the Hunt tape missed."""
    from .scoring.live_fit import fill_vendor_live_history

    n = 0
    for chain in ("sol", "robinhood") if settings.robinhood_enabled else ("sol",):
        with session_scope() as session:
            try:
                n += await fill_vendor_live_history(session, chain)
            except Exception:
                log.exception("vendor live history failed for %s", chain)
    return n


async def _batch_fit_loop() -> None:
    """Hourly refit on the decision ledger. Promotes only if better."""
    try:
        demoted = await asyncio.to_thread(_demote_unfrozen)
        if demoted:
            log.warning("batch fit: demoted unfrozen fits at boot: %s", demoted)
    except Exception:
        log.exception("demoting unfrozen fits failed")
    try:
        await asyncio.wait_for(_stop.wait(), timeout=settings.batch_fit_delay_seconds)
        return
    except TimeoutError:
        pass
    cycle = 0
    while not _stop.is_set():
        cycle += 1
        try:
            try:
                filled = await _vendor_live_history()
                if filled:
                    log.info("vendor live history filled %s", filled)
            except Exception:
                log.exception("vendor live history failed")
            out = await asyncio.to_thread(_batch_fit_cycle)
            for key, res in out.items():
                log.info("batch fit %s: %s", key, {k: res.get(k) for k in ("fitted", "promoted", "reason", "n_train", "n_valid")})
            await asyncio.to_thread(_beat, "batch_fit", f"cycle {cycle}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("batch fit loop failed")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=settings.batch_fit_seconds)
        except TimeoutError:
            pass


async def _hunt_tape_loop() -> None:
    """60s last+Live for this-window Hunt. Does not hold ingest_lock."""
    cycle = 0
    while not _stop.is_set():
        cycle += 1
        started = asyncio.get_running_loop().time()
        wrote = 0
        try:
            from .scoring.hunt_tape import refresh_hunt_tape

            with session_scope() as session:
                wrote = await refresh_hunt_tape(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hunt tape refresh failed")
        from .research.holders import helius_capped

        note = f"cycle {cycle} wrote {wrote}" + (" helius_capped" if helius_capped() else "")
        await asyncio.to_thread(_beat, "hunt_tape", note)
        log.info(
            "hunt tape cycle %s wrote %s in %.0fs",
            cycle,
            wrote,
            asyncio.get_running_loop().time() - started,
        )
        try:
            await asyncio.to_thread(_sample_live)
        except Exception:
            log.exception("live sampling failed")
        try:
            from .scoring.thesis_enrich import (
                enrich_paper_v1_open_free_sources,
                enrich_paper_v1_open_thin_thesis_http,
                enrich_paper_v1_qualify_candidates_http,
            )

            with session_scope() as session:
                await enrich_paper_v1_open_free_sources(session)
                await enrich_paper_v1_open_thin_thesis_http(session)
                await enrich_paper_v1_qualify_candidates_http(session, "sol")
                if settings.robinhood_enabled:
                    await enrich_paper_v1_qualify_candidates_http(session, "robinhood")
        except Exception:
            log.exception("paperV1 open thesis http enrich failed")
        try:
            ledger = await asyncio.to_thread(_sync_paper_ledger)
            if ledger["opened"]:
                log.info("paper ledger opened %s fills", ledger["opened"])
                from .ledger import alert_new_tickets

                with session_scope() as session:
                    await alert_new_tickets(session)
        except Exception:
            log.exception("paper ledger sync failed")
        try:
            from .scoring.paper_enrich import enrich_provisional_opens

            with session_scope() as session:
                await enrich_provisional_opens(session)
        except Exception:
            log.exception("provisional paper enrich failed")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=settings.hunt_last_seconds)
        except TimeoutError:
            pass


async def _tape_refresh_loop() -> None:
    """Dex/X tape ticks. Must not sit in the live poll `while`.

    Live 00:20 after v61: first refresh_outcomes held ingest_lock across
    ~70 sequential Dex/Pump calls. Sol and RH hunt froze at the boot
    snapshot (SharkCAT / BAWSAQ ~23:05) while Pump kept graduating.
    """
    cycle = 0
    while not _stop.is_set():
        cycle += 1
        started = asyncio.get_running_loop().time()
        try:
            log.info("tape refresh cycle %s start", cycle)
            # Beat before the long Dex chair so stability does not red-fail
            # while refresh_outcomes runs (hunt_tape may hold hunt_cards rows).
            await asyncio.to_thread(_beat, "tape_refresh", f"cycle {cycle} start")
            # Do not hold ingest_lock across HTTP. New graduates need
            # the poll / Helius WS path while this ticks last/liq.
            lock_deferred = False
            try:
                with session_scope() as session:
                    await refresh_outcomes(session)
            except Exception as exc:
                from .scoring.hunt import _hunt_session_defer

                if _hunt_session_defer(exc):
                    log.warning(
                        "tape refresh lock defer cycle %s (%s)",
                        cycle,
                        type(exc).__name__,
                    )
                    await asyncio.to_thread(_beat, "tape_refresh", f"cycle {cycle} lock-defer")
                    lock_deferred = True
                else:
                    raise
            if lock_deferred:
                pass
            elif cycle == 1 or cycle % 15 == 0:
                async with ingest_lock:
                    with session_scope() as session:
                        from .research.fomo_wallets import rebuild_fomo_wallets

                        rebuild_fomo_wallets(session, "sol")
                        if settings.robinhood_enabled:
                            rebuild_fomo_wallets(session, "robinhood")
                try:
                    from .research.early_wallets import harvest_confirmed_early, refresh_tracked_still_holding

                    harvested = await harvest_confirmed_early(limit=6)
                    still = await refresh_tracked_still_holding(limit=3)
                    if harvested or still:
                        log.info("early-wallet harvest %s still-in %s", harvested, still)
                except Exception:
                    log.exception("early-wallet refresh failed")
            if not lock_deferred:
                log.info(
                    "tape refresh cycle %s done in %.0fs",
                    cycle,
                    asyncio.get_running_loop().time() - started,
                )
                await asyncio.to_thread(_beat, "tape_refresh", f"cycle {cycle} done")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tape refresh failed")
            await asyncio.to_thread(_beat, "tape_refresh", f"cycle {cycle} error")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=settings.poll_seconds)
        except TimeoutError:
            pass


async def _fomo_trending_audit_loop() -> None:
    """FOMO Tokens→Trending chair on poll cadence; full sanity audit hourly. Learn only."""
    from .research.fomo_coverage import (
        FOMO_AUDIT_TIMEOUT_S,
        fomo_trending_chair_seconds,
    )

    chair_s = fomo_trending_chair_seconds()
    # First pass soon after boot so Learn is not empty for an hour.
    first = True
    while not _stop.is_set():
        from .research.fomo_coverage import fomo_heartbeat_note

        note = "ok"
        try:
            try:
                from .research.fomo_coverage import run_fomo_trending_audit

                out = await asyncio.wait_for(
                    run_fomo_trending_audit(force=first),
                    timeout=FOMO_AUDIT_TIMEOUT_S,
                )
                first = False
                audit = out.get("audit") or out.get("last_audit") or {}
                sanity = (audit.get("sanity") if isinstance(audit, dict) else None) or {}
                sit_out = out.get("sit_out")
                if sit_out is None and isinstance(audit, dict):
                    sit_out = audit.get("sit_out")
                note = out.get("note") or fomo_heartbeat_note(
                    sit_out=sit_out if isinstance(sit_out, int) else None,
                    skipped=bool(out.get("skipped")),
                    sanity=sanity,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                log.warning("fomo trending audit timed out after %ss", FOMO_AUDIT_TIMEOUT_S)
                first = False
                note = fomo_heartbeat_note(error=f"timeout {int(FOMO_AUDIT_TIMEOUT_S)}s")
            except Exception as exc:
                log.exception("fomo trending audit failed")
                first = False
                from .research.fomo_coverage import fomo_error_note

                note = fomo_heartbeat_note(error=fomo_error_note(exc))
        finally:
            try:
                await asyncio.to_thread(_beat, "fomo_trending", note)
            except Exception:
                log.exception("fomo trending heartbeat failed")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=chair_s)
        except TimeoutError:
            pass


async def _early_diff_alert_loop() -> None:
    """SI early-diff threshold alerts + survivor h2/h3/h4. Learn/shadow only."""
    first_wait = 90.0
    while not _stop.is_set():
        try:
            await asyncio.wait_for(_stop.wait(), timeout=first_wait)
            break
        except TimeoutError:
            pass
        first_wait = 300.0
        try:
            from .scoring.early_diff import run_early_diff_tick

            def _tick() -> dict:
                with session_scope() as session:
                    return run_early_diff_tick(session, "sol", hours=6, limit=60)

            out = await asyncio.to_thread(_tick)
            await asyncio.to_thread(
                _beat,
                "early_diff",
                f"scan={out.get('n_scanned')} alerts_new={out.get('n_alerts_new')} "
                f"survivors={(out.get('survivors') or {}).get('n_survivors')}",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("early-diff alert tick failed")


async def loop() -> None:
    configure_worker_logging()
    log.info("worker ingest loop starting")
    init_db()
    restore_watch_boards()
    seed_task = asyncio.create_task(asyncio.to_thread(_seed_ledger))
    backfill_task = asyncio.create_task(_run_backfill())
    rh_backfill_task = asyncio.create_task(_run_rh_backfill())
    zcat_task = asyncio.create_task(_run_zcat_analytics())
    ws_task = asyncio.create_task(listen_migrations(_stop))
    bitquery_task = asyncio.create_task(listen_rh_pools(_stop))
    refresh_task = asyncio.create_task(_tape_refresh_loop())
    hunt_tape_task = asyncio.create_task(_hunt_tape_loop())
    batch_fit_task = asyncio.create_task(_batch_fit_loop())
    fomo_audit_task = asyncio.create_task(_fomo_trending_audit_loop())
    early_diff_task = asyncio.create_task(_early_diff_alert_loop())
    from .ingest.fomo_alerts_ws import listen_fomo_alerts

    fomo_alerts_task = asyncio.create_task(listen_fomo_alerts(_stop, _beat))
    boot_task = asyncio.create_task(_boot_maintenance())
    log.info("worker ingest loop up")
    cycle = 0
    try:
        while not _stop.is_set():
            try:
                news = await poll_new_migrations()
                if news:
                    log.info("poll ingested %s new migrations", len(news))
                from .ingest.dex_poll import poll_sol_dex

                sol_dex_news = await poll_sol_dex()
                if sol_dex_news:
                    log.info("sol dex ingested %s launches", len(sol_dex_news))
                # Official pons factory every cycle (public RPC, no GMGN).
                # GMGN trenches every 2nd cycle (~24s) so a sit-out does
                # not hide PONS launches.
                cycle += 1
                if settings.robinhood_enabled:
                    if cycle % 2 == 0:
                        rh_news = await poll_robinhood()
                        if rh_news:
                            log.info("robinhood poll ingested %s launches", len(rh_news))
                    else:
                        from .ingest.dex_poll import poll_rh_dex
                        from .ingest.pons_poll import poll_pons_launches

                        pons_news = await poll_pons_launches()
                        dex_news = await poll_rh_dex()
                        from .ingest.fomo_poll import poll_fomo_launches

                        fomo_news = await poll_fomo_launches()
                        extra = len(pons_news) + len(dex_news) + len(fomo_news)
                        if extra:
                            log.info("pons/dex/fomo ingested %s launches", extra)
                try:
                    from .research.prewarm import prewarm_watch_cycle

                    warmed = await prewarm_watch_cycle("sol")
                    if settings.robinhood_enabled:
                        warmed += await prewarm_watch_cycle("robinhood")
                    if warmed:
                        log.info("prewarmed %s near-grad books", warmed)
                except Exception:
                    log.exception("prewarm cycle failed")
                await asyncio.to_thread(_beat, "ingest", f"cycle {cycle}")
            except Exception:
                log.exception("scan cycle failed")
            try:
                await asyncio.wait_for(_stop.wait(), timeout=settings.poll_seconds)
            except TimeoutError:
                pass
    finally:
        _stop.set()
        ws_task.cancel()
        bitquery_task.cancel()
        seed_task.cancel()
        backfill_task.cancel()
        rh_backfill_task.cancel()
        zcat_task.cancel()
        refresh_task.cancel()
        hunt_tape_task.cancel()
        batch_fit_task.cancel()
        fomo_audit_task.cancel()
        early_diff_task.cancel()
        fomo_alerts_task.cancel()
        boot_task.cancel()


def start_background() -> None:
    global _task
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(loop())


async def stop_background() -> None:
    _stop.set()
    if _task:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except Exception:
            _task.cancel()
