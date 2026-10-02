from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import graduation_mcap, token_chain
from ..config import GRADUATION_MCAP_USD, settings
from ..desk_lines import scorer_for
from ..models import Outcome, Research, Snapshot, Token, utcnow
from ..scoring.features import extract_features
from ..scoring.model import predict
from ..social import extract_github, extract_twitter_handle
from . import dexscreener, github, gmgn, holders, pumpfun, twitter

log = logging.getLogger("launchfinder.research")


def _minutes_between(start: datetime | None, end: datetime | None) -> float:
    if not start or not end:
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds() / 60.0)


def _is_doa_junk(token: Token, coin: dict[str, Any], market: dict[str, Any]) -> bool:
    """Dead-on-arrival spam that needs no security research: banned, phishing
    bait names, or a pool that is already empty minutes after migration."""
    if coin.get("banned") or token.banned:
        return True
    name = f"{coin.get('name') or token.name or ''} {coin.get('symbol') or token.symbol or ''}".lower()
    if any(bait in name for bait in ("claim", "airdrop", "http")):
        return True
    liq = float(market.get("liquidity_usd") or 0.0)
    mcap = float(market.get("mcap_usd") or 0.0)
    # Thin pools rarely run and cost the same GMGN budget as real candidates.
    if market and (liq < 3_000 or mcap < 25_000):
        return True
    return False


def _market_heat(session: Session, chain: str = "sol") -> float:
    """Migration velocity: this hour vs the trailing-24h hourly average.
    1.0 = normal; >1.5 = hot tape where runners cluster."""
    from datetime import timedelta

    now = utcnow()
    hour = (
        session.query(Token)
        .filter(Token.first_seen_at >= now - timedelta(hours=1), Token.source != "backfill", Token.chain == chain)
        .count()
    )
    day = (
        session.query(Token)
        .filter(Token.first_seen_at >= now - timedelta(hours=24), Token.source != "backfill", Token.chain == chain)
        .count()
    )
    return hour / max(1.0, day / 24.0)


def _symbol_flood(session: Session, token: Token) -> int:
    """How many *other* tokens with the same ticker we saw in the last 24h.
    Copycat waves (RST x4 in an hour) are sniper bait, not organic launches."""
    from datetime import timedelta

    from sqlalchemy import func

    symbol = (token.symbol or "").strip().lower()
    if not symbol:
        return 0
    cutoff = utcnow() - timedelta(hours=24)
    return (
        session.query(Token.id)
        .filter(
            func.lower(Token.symbol) == symbol,
            Token.first_seen_at >= cutoff,
            Token.mint != token.mint,
            Token.chain == token_chain(token),
        )
        .count()
    )


def _creator_stats(session: Session, creator: str, coins: list[dict[str, Any]]) -> dict[str, Any]:
    launches = len(coins)
    wins = 0
    rugs = 0
    for coin in coins:
        ath = float(coin.get("ath_mcap") or 0.0)
        mcap = float(coin.get("mcap_usd") or 0.0)
        if coin.get("complete") and ath >= GRADUATION_MCAP_USD * 3:
            wins += 1
        if coin.get("complete") and mcap > 0 and mcap < GRADUATION_MCAP_USD * 0.35:
            rugs += 1
    known = (
        session.query(Token)
        .filter(Token.creator == creator)
        .all()
        if creator
        else []
    )
    for token in known:
        outcome = token.outcome
        if outcome and outcome.label == 1:
            wins += 1
        if outcome and outcome.label == 0:
            rugs += 1
        launches = max(launches, len(known))
    return {"launches": launches, "wins": wins, "rugs": rugs}


async def _empty_dict() -> dict[str, Any]:
    return {}


async def _empty_list() -> list:
    return []


def _live_open_fast(token: Token, coin: dict[str, Any] | None) -> bool:
    """True when paper should open on Dex facts and finish enrich later."""
    if token.is_historical or (coin or {}).get("historical"):
        return False
    return True


async def research_token(session: Session, token: Token, *, coin: dict[str, Any] | None = None) -> Research:
    mint = token.mint
    chain = token_chain(token)
    on_sol = chain == "sol"
    if coin is None:
        coin = (await pumpfun.get_coin(mint) or {}) if on_sol else {}
    fast = _live_open_fast(token, coin)
    market_task = asyncio.create_task(dexscreener.token_market(mint, chain=chain))
    creator_task = asyncio.create_task(
        pumpfun.creator_coins(token.creator or coin.get("creator") or "") if on_sol else _empty_list()
    )
    user_task = asyncio.create_task(
        pumpfun.get_user(token.creator or coin.get("creator") or "") if on_sol else _empty_dict()
    )
    holders_task = asyncio.create_task(
        holders.holder_stats(
            mint,
            token.creator or coin.get("creator") or "",
            token.pool_address or coin.get("pool_address") or "",
            chain=chain,
            max_pages=holders.SOL_HOLDER_OPEN_PAGES if fast else holders.SOL_HOLDER_TAPE_PAGES,
            annotate=not fast,
        )
    )
    gmgn_seed = gmgn.summarize_trench_row(coin["gmgn_row"], mint, chain=chain) if coin.get("gmgn_row") else None

    market = await market_task
    creator_coins = await creator_task
    user = await user_task
    holder_info = await holders_task
    # GMGN calls are the scarcest budget. Trench-seeded rows are free; for the
    # rest, skip clear dead-on-arrival junk that scores near zero regardless.
    # Official pons factory ingest is GMGN-independent — never spend quota
    # on a sit-out or extra /token/info just because Dex has a book.
    # Live open (v202): use the seed / skip HTTP so paper can fill on Dex
    # mcap/liq. Background enrich finishes GMGN / X / extra DAS pages.
    if coin.get("skip_gmgn"):
        gmgn_info = {}
    elif gmgn_seed is not None:
        gmgn_info = dict(gmgn_seed)
        if not fast:
            gmgn_info = await gmgn.token_research(mint, seed=gmgn_seed, chain=chain)
    elif fast:
        gmgn_info = {}
    elif not _is_doa_junk(token, coin, market):
        gmgn_info = await gmgn.token_research(mint, seed=gmgn_seed, chain=chain)
    else:
        gmgn_info = {}
    if not (holder_info or {}).get("holder_count"):
        fallback = holders.holders_from_gmgn(gmgn_info)
        if fallback:
            holder_info = {**(holder_info or {}), **fallback}

    twitter_url = token.twitter or coin.get("twitter") or market.get("twitter") or ""
    gmgn_handle = str((gmgn_info or {}).get("twitter_username") or "")
    if not twitter_url and gmgn_handle:
        twitter_url = gmgn_handle if gmgn_handle.startswith("http") else f"https://x.com/{gmgn_handle.lstrip('@')}"
    creator_handle = extract_twitter_handle(user.get("x_username") or "")
    handle = extract_twitter_handle(twitter_url) or extract_twitter_handle(gmgn_handle)
    # Pump user.x_username is a personal developer account, not the
    # token's main X. Do not score its age/followers as project social.
    if handle and creator_handle and handle.lower() == creator_handle.lower():
        handle = ""
        twitter_url = ""
    website = token.website or coin.get("website") or market.get("website") or ""
    telegram = token.telegram or coin.get("telegram") or market.get("telegram") or ""
    blob = " ".join(
        [
            token.description or "",
            coin.get("description") or "",
            website,
            market.get("github") or "",
        ]
    )
    gh_url, gh_ref = extract_github(blob)
    if not gh_ref:
        gh_url, gh_ref = extract_github(market.get("github") or "")
    # Project sites often bury the repo link; scrape once before freeze so
    # thesis keys (github_auth_n / real_project) are not stuck at zero.
    # Live fast-open defers the website scrape so Dex facts can paper-fill.
    if not gh_ref and website and not token.is_historical and not fast:
        from ..social import is_official_brand_website
        from ..scoring.thesis_enrich import discover_github_url

        if not is_official_brand_website(website):
            scraped_url, scraped_ref = await discover_github_url(
                token, allow_website_fetch=True, website=website
            )
            if scraped_ref:
                gh_url, gh_ref = scraped_url, scraped_ref
    if isinstance((coin or {}).get("prewarm_github"), dict) and coin.get("prewarm_github"):
        gh = dict(coin["prewarm_github"])
    elif gh_ref and not fast:
        gh = await github.lookup_repo(gh_ref)
    elif (not fast) and (not token.is_historical) and settings.github_token and (coin.get("name") or token.name):
        gh = await github.search_token(coin.get("name") or token.name, coin.get("symbol") or token.symbol, mint)
    else:
        gh = {}
    if gh.get("url"):
        gh_url = gh["url"]

    if isinstance((coin or {}).get("prewarm_twitter"), dict) and coin.get("prewarm_twitter"):
        tw = dict(coin["prewarm_twitter"])
    elif handle and not fast:
        tw = await twitter.lookup_handle(handle)
    else:
        tw = {}
    if handle and not float(tw.get("followers") or 0) and float((gmgn_info or {}).get("twitter_followers") or 0):
        tw = {**tw, "followers": int(gmgn_info["twitter_followers"]), "source": tw.get("source") or "gmgn"}
    mentions = 0
    followers_n = float(tw.get("followers") or 0)
    # Mention counts cost paid X credits. NASA / Google clones never
    # spend one. Mid-size project accounts only. Live fast-open skips paid X.
    spend_x = (not fast) and twitter.should_spend_x_credits(
        handle,
        followers=followers_n,
        age_days=float(tw.get("age_days") or 0),
        verified=bool(tw.get("verified")),
    )
    if twitter.is_official_brand_handle(handle) or twitter.is_claimed_brand_x(
        handle,
        followers=followers_n,
        age_days=float(tw.get("age_days") or 0),
        verified=bool(tw.get("verified")),
    ):
        tw = {**tw, "hijack": True, "owner_mentions": 0}
    elif handle and spend_x and (followers_n >= twitter.PAID_MENTION_FOLLOWERS or tw.get("verified")):
        mentions = await twitter.mention_count(f"${token.symbol}" if token.symbol else handle)
    # Handle-hijack check: tokens love to claim celebrity X accounts. A real
    # owner launch tweets the ticker; a hijacked claim never does. Only an
    # authoritative zero (API reachable) counts as evidence.
    if (
        handle
        and spend_x
        and followers_n >= 50_000
        and (token.symbol or token.mint or "").strip()
        and not tw.get("hijack")
    ):
        # Aged celebrity / brand accounts tweet their name constantly.
        # Require the mint so @CocaCola / @GeminiApp do not clear a clone.
        brand = twitter.is_brand_x_account(
            followers_n,
            float(tw.get("age_days") or 0),
            bool(tw.get("verified")),
        )
        owner_mentions = await twitter.account_mentions(
            handle,
            token.symbol or "",
            mint=(token.mint or "") if brand else "",
        )
        if owner_mentions is not None:
            tw = {**tw, "owner_mentions": owner_mentions, "hijack": owner_mentions == 0}

    created = token.created_at_chain or coin.get("created_at")
    migrated = token.migrated_at or utcnow()
    time_to_migrate = _minutes_between(created, migrated)
    creator_stats = _creator_stats(session, token.creator or coin.get("creator") or "", creator_coins)
    symbol_flood = _symbol_flood(session, token)
    from ..scoring.outcomes import extract_meta_words, funder_stats as _funder_stats, hot_metas

    funder_history = _funder_stats(session, str((gmgn_info or {}).get("fund_from") or ""))
    meta_match = sorted(
        extract_meta_words(token.name or coin.get("name") or "", token.symbol or coin.get("symbol") or "")
        & hot_metas(session, chain=chain)
    )
    market_heat = _market_heat(session, chain=chain)

    ctx = {
        "chain": chain,
        "source": token.source or coin.get("source") or "",
        "coin": {
            **coin,
            "name": token.name or coin.get("name"),
            "symbol": token.symbol or coin.get("symbol"),
            "source": token.source or coin.get("source") or "",
            "launchpad": coin.get("launchpad") or (gmgn_info or {}).get("launchpad") or "",
        },
        "market": market,
        "twitter": tw,
        "twitter_handle": handle,
        "twitter_url": twitter_url,
        "website": website,
        "telegram": telegram,
        "github": gh,
        "github_url": gh_url,
        "holders": holder_info,
        "gmgn": gmgn_info,
        "creator_stats": creator_stats,
        "time_to_migrate_min": time_to_migrate,
        "symbol_flood": symbol_flood,
        "funder_stats": funder_history,
        "meta_match": meta_match,
        "market_heat": market_heat,
        "x_mentions": mentions,
        "last_liq": float(token.outcome.last_liq) if token.outcome else 0.0,
        "t0_mcap": float(token.outcome.t0_mcap) if token.outcome else 0.0,
        "age_min": _minutes_between(token.first_seen_at, utcnow()),
        "live_multiple": float(token.outcome.multiple or 0.0) if token.outcome else 0.0,
    }
    features = extract_features(ctx)
    if fast:
        from ..scoring.paper_gate import stamp_paper_provisional

        features = stamp_paper_provisional(features, True)
    from ..scoring.preview import merge_preview_feature, preview_score

    prev = 0.0
    preview_reasons: list = []
    preview_flags: list = []
    if not token.is_historical:
        prev, preview_reasons, preview_flags = preview_score(
            symbol=str(token.symbol or coin.get("symbol") or ""),
            name=str(token.name or coin.get("name") or ""),
            twitter=str(handle or token.twitter or coin.get("twitter") or ""),
            website=str(website or ""),
            telegram=str(telegram or ""),
            github=str(gh_url or ""),
            twitter_followers=int(tw.get("followers") or 0),
            twitter_age_days=float(tw.get("age_days") or 0.0),
            twitter_verified=bool(tw.get("verified")),
            github_stars=int(gh.get("stars") or 0),
            progress=float(coin.get("progress") or 0.0) or (0.9 if token.migrated_at else 0.0),
            age_min=float(time_to_migrate or 0.0),
            creator_launches=int(creator_stats.get("launches") or 0),
            creator_wins=int(creator_stats.get("wins") or 0),
            creator_rugs=int(creator_stats.get("rugs") or 0),
            reply_count=int(token.reply_count or coin.get("reply_count") or 0),
            prewarmed=bool(coin.get("prewarm_twitter") or coin.get("preview_p")),
        )
    features = merge_preview_feature(features, prev)
    scored = predict(session, features, chain=chain)
    scored["legacy_p"] = float(scored.get("p_good") or 0.0)
    scored["first_sight_p"] = None
    mcap = float(market.get("mcap_usd") or coin.get("mcap_usd") or 0.0)
    if mcap > 20_000_000.0:  # flash print at research time = manipulated pool
        mcap = 0.0
    price = float(market.get("price_usd") or 0.0)
    liq = float(market.get("liquidity_usd") or 0.0)
    if not token.is_historical:
        # First-sight model (v75): frozen first-sight facts, fitted on the
        # honest board. Replaces the legacy blend as Entry where promoted;
        # the blend stays on the row as heuristic_p / model_p / legacy_p.
        try:
            from ..scoring.first_sight import book_is_fillable, features_from_live, first_sight_p

            fs_feats = features_from_live(
                token,
                market=market,
                holder_info=holder_info,
                creator_stats=creator_stats,
                twitter=tw,
                twitter_url=twitter_url or "",
                website=website or "",
                telegram=telegram or "",
            )
            fs_p = first_sight_p(session, chain, fs_feats)
        except Exception:
            log.exception("first-sight score failed for %s", token.mint)
            fs_p = None
        if fs_p is not None:
            scored["first_sight_p"] = fs_p
            if book_is_fillable(liq):
                scored["p_good"] = fs_p
            else:
                # Pre-book: keep the legacy number for display, do not freeze
                # first-sight 0.01 as Entry. The first sellable print finalizes.
                scored["awaiting_fill"] = True
    features["legacy_p"] = scored["legacy_p"]

    research = token.research
    if research is None:
        research = Research(token=token)
        session.add(research)

    research.researched_at = utcnow()
    research.features_json = json.dumps(features)
    research.reasons_json = json.dumps(scored["reasons"])
    research.risk_flags_json = json.dumps(scored["risk_flags"])
    research.twitter_handle = handle
    research.twitter_followers = int(tw.get("followers") or 0)
    research.twitter_tweets = int(tw.get("tweets") or 0)
    research.twitter_age_days = float(tw.get("age_days") or 0)
    research.twitter_verified = bool(tw.get("verified"))
    research.x_mentions_1h = int(mentions)
    research.github_stars = int(gh.get("stars") or 0)
    research.github_forks = int(gh.get("forks") or 0)
    research.github_age_days = float(gh.get("age_days") or 0)
    research.holder_count = int(holder_info.get("holder_count") or holder_info.get("holder_sample") or 0)
    research.top10_pct = float(holder_info.get("top10_pct") or 0)
    research.creator_hold_pct = float(holder_info.get("creator_hold_pct") or 0)
    research.creator_prior_launches = int(creator_stats["launches"])
    research.creator_prior_wins = int(creator_stats["wins"])
    research.creator_prior_rugs = int(creator_stats["rugs"])
    research.time_to_migrate_min = time_to_migrate
    research.p_good = scored["p_good"]
    research.heuristic_p = scored["heuristic_p"]
    research.model_p = scored["model_p"]
    research.preview_p = float(prev or 0.0)
    research.scorer = scorer_for(scored)
    if scored.get("awaiting_fill"):
        from ..scoring.first_sight import mark_awaiting_fill

        mark_awaiting_fill(research)
    elif scored.get("first_sight_p") is not None and not scored.get("awaiting_fill"):
        from ..scoring.first_sight import mark_fill_finalized

        mark_fill_finalized(research)
    research.thesis = _thesis(token, scored, tw, gh, market, holder_info, gmgn_info)
    research.raw_json = json.dumps(
        {
            "market": market,
            "twitter": tw,
            "github": gh,
            "holders": holder_info,
            "gmgn": gmgn_info,
            "creator_stats": creator_stats,
            "preview": {
                "p": float(prev or 0.0),
                "reasons": preview_reasons,
                "flags": preview_flags,
            },
            "user": {k: user.get(k) for k in ("username", "followers", "x_username", "bio") if user},
        },
        default=str,
    )

    if token.website == "":
        token.website = website or ""
    if token.twitter == "":
        token.twitter = twitter_url or ""
    if token.telegram == "":
        token.telegram = telegram or ""
    if gh_url:
        token.github_url = gh_url
    if coin.get("pool_address") and not token.pool_address:
        token.pool_address = coin["pool_address"]
    if coin.get("reply_count"):
        token.reply_count = int(coin["reply_count"])

    session.add(
        Snapshot(
            token=token,
            kind="t0" if not any(s.kind == "t0" for s in token.snapshots) else "live",
            price_usd=price,
            mcap_usd=mcap,
            volume_h1=float(market.get("volume_h1") or 0),
            volume_m5=float(market.get("volume_m5") or 0),
            liquidity_usd=liq,
            buys_m5=int(market.get("buys_m5") or 0),
            sells_m5=int(market.get("sells_m5") or 0),
            score=scored["p_good"] * 100.0,
            p_good=scored["p_good"],
        )
    )
    if token.outcome is None:
        # GMGN reports the exact market cap at migration — a better entry
        # proxy than the graduation constant when dexscreener has no data yet.
        floor = graduation_mcap(chain)
        mig = float((gmgn_info or {}).get("migration_mcap") or 0.0)
        vol = float(market.get("volume_h1") or 0.0)
        if chain == "robinhood":
            from ..scoring.outcomes import rh_ingest_entry

            chg = market.get("price_change_h1")
            try:
                chg_n = float(chg) if chg is not None else 0.0
            except (TypeError, ValueError):
                chg_n = 0.0
            if chg_n == 0.0:
                chg = market.get("price_change_h24")
            entry, seed_max = rh_ingest_entry(
                mcap, liq, vol, mig, floor, price_change_pct=chg
            )
        else:
            entry = mcap or mig or floor
            if entry < 0.4 * floor:
                # Collapse or GMGN migration_mcap in SOL (~411 = $69k / ~$168).
                # Trusting a tiny mig re-anchors to the same fake $410 t0
                # (live ARROW/RST/WWR 4.5x on a $1.8k book).
                entry = floor
            seed_max = max(entry, mcap)
        session.add(
            Outcome(
                token=token,
                t0_mcap=entry,
                max_mcap=seed_max,
                last_liq=liq,
            )
        )
    session.flush()
    try:
        from ..ledger import record_entry_decision
        from ..scoring.thesis_enrich import enrich_thesis_before_entry

        entry_mcap = mcap or (float(token.outcome.t0_mcap or 0.0) if token.outcome else 0.0)
        if not scored.get("awaiting_fill"):
            if not fast:
                try:
                    await enrich_thesis_before_entry(session, token, research)
                except Exception:
                    log.exception("thesis enrich before entry failed for %s", token.mint)
            record_entry_decision(
                session,
                token,
                research,
                scored,
                market={**market, "mcap_usd": entry_mcap},
                holder_count=int(research.holder_count or 0),
            )
    except Exception:
        log.exception("ledger entry decision failed for %s", token.mint)
    return research


def _thesis(
    token: Token,
    scored: dict[str, Any],
    tw: dict,
    gh: dict,
    market: dict,
    holder_info: dict | None = None,
    gmgn_info: dict | None = None,
) -> str:
    bits = [
        (
            f"{token.symbol or token.name} launched on pons (Robinhood Chain)."
            if (token.source == "rh_pons" or (token.website or "").find("ponsfamily.com") >= 0)
            else f"{token.symbol or token.name} migrated on {token_chain(token)}."
        ),
        (
            f"Awaiting a fillable book — first-sight not frozen (legacy blend {scored.get('legacy_p', 0.0):.0%})."
            if scored.get("awaiting_fill")
            else (
                f"First-sight p(2×)={scored['p_good']:.0%} from the frozen first-sight facts (legacy blend {scored.get('legacy_p', 0.0):.0%})."
                if scored.get("first_sight_p") is not None
                else f"Model p(good)={scored['p_good']:.0%} (heuristic {scored['heuristic_p']:.0%}, learned {scored['model_p']:.0%})."
            )
        ),
    ]
    if tw:
        bits.append(f"X @{tw.get('handle')} has {tw.get('followers', 0):,} followers, account age {tw.get('age_days', 0):.0f}d.")
    if gh.get("full_name"):
        bits.append(f"GitHub {gh['full_name']} ({gh.get('stars', 0)} stars).")
    if market.get("liquidity_usd"):
        bits.append(f"Dex liquidity ${market['liquidity_usd']:,.0f}, 1h vol ${float(market.get('volume_h1') or 0):,.0f}.")
    holders = holder_info or {}
    if holders.get("holder_count"):
        bits.append(
            f"{int(holders['holder_count'])} holders, top10 {float(holders.get('top10_pct') or 0):.0f}%, "
            f"creator {float(holders.get('creator_hold_pct') or 0):.0f}%."
        )
    elif scored.get("first_sight_p") is not None and not holders.get("holder_sample"):
        # The model imputes the holder columns; say so instead of showing 0.
        bits.append("Holder count unavailable at first sight (holder columns imputed, not read as zero).")
    gmgn_bits = gmgn_info or {}
    if gmgn_bits.get("source"):
        extra = []
        if gmgn_bits.get("og"):
            extra.append("OG")
        tax = max(float(gmgn_bits.get("buy_tax_pct") or 0), float(gmgn_bits.get("sell_tax_pct") or 0))
        if tax:
            extra.append(f"tax {tax:.0f}%")
        if gmgn_bits.get("dev_sold"):
            extra.append("dev sold")
        elif gmgn_bits.get("creator_status") == "creator_hold":
            extra.append("dev holding")
        bits.append(
            f"GMGN rug {float(gmgn_bits.get('rug_risk') or 0):.0f}, "
            f"insider {float(gmgn_bits.get('insider_pct') or 0):.0f}%, "
            f"sniper {float(gmgn_bits.get('sniper_pct') or 0):.0f}%"
            + (f", {', '.join(extra)}" if extra else "")
            + "."
        )
    if scored["risk_flags"]:
        bits.append("Risks: " + "; ".join(scored["risk_flags"][:3]) + ".")
    elif scored["reasons"]:
        bits.append("Positives: " + "; ".join(scored["reasons"][:3]) + ".")
    return " ".join(bits)
