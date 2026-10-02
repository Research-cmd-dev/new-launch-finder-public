from __future__ import annotations

from typing import Any

from ..social import is_staged_social_pair

FEATURE_NAMES = [
    "has_twitter",
    "twitter_followers_n",
    "twitter_age_n",
    "twitter_verified",
    "has_website",
    "has_telegram",
    "has_github",
    "github_stars_n",
    "github_age_n",
    "reply_n",
    "holder_n",
    "top10_inv",
    "top1_inv",
    "fresh_wallet_n",
    "creator_hold_inv",
    "creator_win_rate",
    "creator_serial_penalty",
    "migrate_speed",
    "buy_pressure",
    "liquidity_n",
    "volume_n",
    "name_quality",
    "nsfw",
    "banned",
    "gmgn_present",
    "gmgn_honeypot",
    "gmgn_bundled",
    "gmgn_insider_n",
    "gmgn_sniper_n",
    "gmgn_rug_n",
    "gmgn_smart_n",
    "gmgn_kol_n",
    # Research-backed additions (arXiv 2602.14860 + GMGN DD card).
    "gmgn_bot_rate_n",
    "gmgn_wash",
    "gmgn_sniper_count_n",
    "gmgn_bundler_vol_n",
    "gmgn_dev_sold",
    "gmgn_creator_ath_n",
    "gmgn_dexscr_paid",
    "usd_buy_pressure",
    "symbol_flood_n",
    "x_handle_hijack",
    "funder_rug_n",
    "entry_collapse",
    "github_auth_n",
    "real_project",
    "meta_heat",
    "market_heat_n",
    "entry_premium",
    "mcap_per_holder_n",
    # Already extracted for the heuristic; the model now sees them too.
    "gmgn_tax_n",
    "gmgn_og",
    "gmgn_lock_n",
    "gmgn_burned",
    "gmgn_cto",
    "gmgn_image_dup",
    "organic_book",
    "launchpad_pons",
    "gmgn_activity_n",
    "x_mentions_n",
    "gmgn_rat_n",
    "gmgn_renounced",
    "gmgn_open_source",
    "gmgn_callout_n",
    "gmgn_net_buy_n",
    "rh_thin_book",
]


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _first_pos(*values: float) -> float:
    for value in values:
        if value and value > 0:
            return float(value)
    return 0.0


# Live MEME: Blockscout t0 was 4 rows / 98.8% labeled pool. That is
# the LP, not a SHORT/PLUMBED 4-wallet wick. Fat Dex last_liq is the
# same signal after the first refresh. Heuristic-only — do not add
# to FEATURE_NAMES.
RH_LP_OPEN_POOL_PCT = 80.0
RH_FAT_BOOK_LIQ = 50_000.0


def rh_lp_open_book(holders: dict[str, Any] | None) -> bool:
    """True when the holder sample is the LP, not retail wallets.

    Live MEME (`0x385F…1e18`): 4 Blockscout rows, 98.8% `pool`, one
    1.2% wallet, dead + second pool at 0%. rh_thin_book capped p at
    0.48 and the model buried it at 0.03 while t0 was a real $22k
    book that ran to $80M+. SHORT/PLUMBED 4-wallet wicks have
    retail wallets, not 80%+ labeled pool.
    """
    wallets = (holders or {}).get("top_wallets") or []
    if not wallets:
        return False
    pool_pct = 0.0
    for wallet in wallets:
        label = str(wallet.get("label") or "").lower()
        owner = str(wallet.get("owner") or "").lower()
        if label in {"pool", "dead"} or owner.endswith("dead"):
            pool_pct += float(wallet.get("pct") or 0.0)
    return pool_pct >= RH_LP_OPEN_POOL_PCT


def is_rh_late_dex_book(features: dict[str, Any] | None) -> bool:
    """Start-high Dex catch-up that already has a real tape.

    Live CRCL (`0x51D1…ebf9d7`): rh_dex ingest at t0 $272k / 438
    wallets / $316k vol / $52k liq. entry_premium is honest — do
    not paper-buy — but this is not a 4-wallet leftover FDV.
    Dex 24h +2130% implies a ~$59k open we arrived late to.
    Heuristic-only — do not add to FEATURE_NAMES.
    """
    feats = features or {}
    if not feats.get("entry_premium"):
        return False
    if feats.get("entry_collapse") or feats.get("rh_thin_book"):
        return False
    return (
        float(feats.get("holder_n") or 0) >= 0.60
        and float(feats.get("volume_n") or 0) >= 0.74
        and float(feats.get("liquidity_n") or 0) >= 0.45
    )


def is_rh_fair_lp_open(features: dict[str, Any] | None) -> bool:
    """Pool is the book, not a start-high dump. GMGN may still be late.

    Live MEME paper window was ~6 minutes. Heuristic-only — do not
    add to FEATURE_NAMES.
    """
    feats = features or {}
    if not feats.get("rh_lp_open_book"):
        return False
    return not (feats.get("entry_premium") or feats.get("entry_collapse"))


def is_rh_clean_lp_open(features: dict[str, Any] | None) -> bool:
    """Fair LP open plus a clean GMGN snapshot.

    Live MEME: pool is the book, rug/insider/sniper near 0, not a
    start-high dump. Heuristic-only — do not add to FEATURE_NAMES.
    """
    feats = features or {}
    if not is_rh_fair_lp_open(feats):
        return False
    if not feats.get("gmgn_present"):
        return False
    if feats.get("gmgn_honeypot") or feats.get("gmgn_bundled"):
        return False
    return float(feats.get("gmgn_rug_n") or 0) < 0.15


def rh_lp_open_clears_paper(features: dict[str, Any] | None) -> bool:
    """Catch the next MEME-class open even when GMGN is still empty.

    Blockscout 80%+ pool is the t0 signal. Withhold the paper clear
    only after GMGN already says honeypot / bundled / rug. SHORT /
    PLUMBED stay thin-capped — they are retail wicks, not LP opens.
    """
    feats = features or {}
    if not is_rh_fair_lp_open(feats):
        return False
    if not feats.get("gmgn_present"):
        return True
    return is_rh_clean_lp_open(feats)


def is_brand_clone_book(features: dict[str, float] | None) -> bool:
    """Official-brand X / website pasted onto a copycat ticker.

    Live Google Gemini: @GeminiApp 573k verified + copycat flood +
    gemini.google.com. Organic book then printed 98%. RH MEME is a
    fair LP open on a copied ticker with a brand-new X — not this.
    """
    feats = features or {}
    if rh_lp_open_clears_paper(feats):
        return False
    if float(feats.get("x_handle_hijack") or 0) >= 0.5:
        return True
    celebrity = (
        float(feats.get("twitter_verified") or 0) >= 0.5
        and float(feats.get("twitter_followers_n") or 0) >= 0.85
        and float(feats.get("twitter_age_n") or 0) >= 0.25
    )
    flooded = float(feats.get("symbol_flood_n") or 0) >= 0.5
    return celebrity and flooded


def is_staged_social_book(features: dict[str, float] | None) -> bool:
    """Pasted tweet + generated website. Live TWIN Entry 92.

    Hijack only runs at 50k followers, so a 162-follower verified
    status URL still collected age/verified bonuses. FEATURE_NAMES
    stays 66 — staged_social is a side key.
    """
    feats = features or {}
    if rh_lp_open_clears_paper(feats):
        return False
    return float(feats.get("staged_social") or 0) >= 0.5


def is_funder_rug_book(features: dict[str, float] | None) -> bool:
    """Creator funded by a rug-factory wallet.

    Live Pumpball: funder_rug_n=1.0 on a 333-wallet organic book.
    Heuristic −0.12 is cancelled by organic bonuses; Sol floor then
    reprints 92% while the learned model is 2%. FEATURE_NAMES stays 66.
    """
    return float((features or {}).get("funder_rug_n") or 0) >= 0.6


def is_copycat_flood_book(features: dict[str, float] | None) -> bool:
    """Same-ticker 24h flood that is not an RH MEME fair LP open.

    Live OpenAI: copycat flag, no official X, 2395 holders, two-tick
    chart. Organic floor printed 90%. FEATURE_NAMES stays 66.
    """
    feats = features or {}
    if rh_lp_open_clears_paper(feats):
        return False
    return float(feats.get("symbol_flood_n") or 0) >= 0.5


def is_copycat_dump_book(features: dict[str, float] | None) -> bool:
    """Same-ticker flood that is already selling in hour one.

    Live JubJub: copycat + dw selling + first-hour dump on a 2200-wallet
    $97k book. Organic bonuses cancelled the −0.10/−0.05 hits and the
    model printed 99%. RH MEME is a fair LP open — not this.
    Do not append to FEATURE_NAMES.
    """
    feats = features or {}
    if rh_lp_open_clears_paper(feats):
        return False
    if float(feats.get("symbol_flood_n") or 0) < 0.5:
        return False
    dump = float(feats.get("price_change_h1_n") or 0.5) <= 0.22 and float(feats.get("volume_n") or 0) > 0.45
    dw = float(feats.get("usd_buy_pressure") or 0.5) < 0.35
    return dump or dw


def rh_thin_book_flag(
    chain: str,
    holder_count: float | int,
    last_liq: float,
    *,
    holders: dict[str, Any] | None = None,
    features: dict[str, Any] | None = None,
) -> float:
    """1.0 only for a skinny RH retail book.

    Second-look / re-anchor used to recap any <20-wallet row under
    $50k liq. Live MEME t0 was $22k liq / 4 LP rows — that recap
    flipped p 0.04↔0.33 in hour one while volume went $768→$85k.
    """
    from ..chains import normalize_chain

    if normalize_chain(chain) != "robinhood":
        return 0.0
    if int(holder_count or 0) >= 20:
        return 0.0
    if (features or {}).get("rh_lp_open_book") or rh_lp_open_book(holders):
        return 0.0
    if float(last_liq or 0) >= RH_FAT_BOOK_LIQ:
        return 0.0
    return 1.0


def _holder_book(holders: dict[str, Any], gmgn: dict[str, Any]) -> tuple[float, float, float, float, float]:
    """Prefer the richer book already on the card.

    RH holder_stats is empty (no Helius). A 2-wallet Dex print next to
    GMGN's 80-wallet trench card is not the market — score the trench card.
    """
    holder_count = float(holders.get("holder_count") or holders.get("holder_sample") or 0)
    top10 = float(holders.get("top10_pct") or 0)
    top1 = float(holders.get("top1_pct") or 0)
    fresh_wallets = float(holders.get("fresh_wallet_pct") or 0)
    creator_hold = float(holders.get("creator_hold_pct") or 0)
    gmgn_holders = float(gmgn.get("holder_count") or 0)
    richer = gmgn_holders >= 20 and gmgn_holders > holder_count * 1.5
    if holder_count <= 0 or richer:
        holder_count = gmgn_holders or holder_count
        if not top10:
            top10 = float(gmgn.get("top10_pct") or 0)
        if not creator_hold:
            creator_hold = float(gmgn.get("dev_hold_pct") or 0)
        if not fresh_wallets:
            fresh_wallets = float(gmgn.get("fresh_wallet_pct") or 0)
    return holder_count, top10, top1, fresh_wallets, creator_hold


def _log_norm(value: float, scale: float) -> float:
    if value <= 0:
        return 0.0
    import math

    return _clip01(math.log1p(value) / math.log1p(scale))


def _brand_website_claim(ctx: dict[str, Any] | None) -> bool:
    from ..social import is_official_brand_website

    return is_official_brand_website(str((ctx or {}).get("website") or ""))


def extract_features(ctx: dict[str, Any]) -> dict[str, float]:
    tw = ctx.get("twitter") or {}
    gh = ctx.get("github") or {}
    holders = ctx.get("holders") or {}
    market = ctx.get("market") or {}
    coin = ctx.get("coin") or {}
    creator = ctx.get("creator_stats") or {}
    gmgn = ctx.get("gmgn") or {}

    followers = float(tw.get("followers") or gmgn.get("twitter_followers") or 0)
    tw_age = float(tw.get("age_days") or 0)
    stars = float(gh.get("stars") or 0)
    gh_age = float(gh.get("age_days") or 0)
    replies = float(coin.get("reply_count") or 0)
    holder_count, top10, top1, fresh_wallets, creator_hold = _holder_book(holders, gmgn)
    prior = float(creator.get("launches") or 0) or float(gmgn.get("creator_open_count") or 0)
    wins = float(creator.get("wins") or 0)
    rugs = float(creator.get("rugs") or 0)
    minutes = float(ctx.get("time_to_migrate_min") or 0)
    from ..chains import normalize_chain

    chain = normalize_chain(ctx.get("chain"))
    buys = float(market.get("buys_m5") or 0) + float(market.get("buys_h1") or 0)
    sells = float(market.get("sells_m5") or 0) + float(market.get("sells_h1") or 0)
    stored_liq = float(ctx.get("last_liq") or 0) if float(ctx.get("last_liq") or 0) >= 800 else 0.0
    gmgn_liq = float(gmgn.get("liquidity") or 0)
    gmgn_vol = float(gmgn.get("volume_1h") or gmgn.get("volume_h1") or 0)
    # Leftover FDV prints a liquidity number with no tape (STACKS).
    gmgn_live_liq = gmgn_liq if gmgn_liq >= 800 and gmgn_vol >= 100 else 0.0
    liq = _first_pos(
        float(market.get("liquidity_usd") or 0),
        float(gmgn.get("liquidity") or 0),
        # Worker last_liq is a real book when Dex/GMGN snapshot is empty
        # (live CHAD $14k / BB $19k). Ignore leftover dust under $800.
        stored_liq,
    )
    vol = _first_pos(
        float(market.get("volume_h1") or market.get("volume_m5") or 0),
        float(gmgn.get("volume_1h") or 0),
    )
    handle = ctx.get("twitter_handle") or gmgn.get("twitter_username") or ""
    name = f"{coin.get('name') or ''} {coin.get('symbol') or ''}"

    win_rate = wins / prior if prior else 0.0
    # Launch count without rugs is normal on Pump.fun / PONS. The old
    # (prior-2)/10 rule branded every volume deployer a serial scammer
    # and sat POV (9.2x) / JJJACKET (14x) / MACRODUCK (9.8x) under water.
    if rugs:
        serial = _clip01(rugs / 3.0)
    elif prior >= 15:
        serial = 0.25
    else:
        serial = 0.0
    if minutes <= 0:
        migrate_speed = 0.4
    elif minutes < 2:
        # Pump.fun sub-2m fills are often bundles. RH trench timestamps
        # are created≈opened, so every launch looks instant — ignore it.
        migrate_speed = 0.4 if chain == "robinhood" else 0.05
    elif minutes < 20:
        migrate_speed = 0.55
    elif minutes < 360:
        migrate_speed = 0.9
    else:
        migrate_speed = 0.45
    pressure = buys / (buys + sells) if (buys + sells) else 0.5
    quality = 0.7
    if len((coin.get("symbol") or "")) <= 1:
        quality = 0.2
    if any(ch.isdigit() for ch in (coin.get("symbol") or "")) and len(coin.get("symbol") or "") > 8:
        quality = 0.3
    if "http" in name.lower() or "claim" in name.lower() or "airdrop" in name.lower():
        quality = 0.1

    out = {
        "has_twitter": 1.0 if handle else 0.0,
        "twitter_followers_n": _log_norm(followers, 50_000),
        "twitter_age_n": _clip01(tw_age / 730.0),
        "twitter_verified": 1.0 if tw.get("verified") else 0.0,
        "has_website": 1.0 if ctx.get("website") else 0.0,
        "has_telegram": 1.0 if ctx.get("telegram") else 0.0,
        "has_github": 1.0 if ctx.get("github_url") else 0.0,
        "github_stars_n": _log_norm(stars, 200),
        "github_age_n": _clip01(gh_age / 365.0),
        "reply_n": _log_norm(replies, 400),
        "holder_n": _log_norm(holder_count, 2_000),
        "top10_inv": _clip01(1.0 - top10 / 100.0) if top10 else 0.45,
        "top1_inv": _clip01(1.0 - top1 / 50.0) if top1 else 0.5,
        "fresh_wallet_n": _clip01(fresh_wallets / 100.0),
        "creator_hold_inv": _clip01(1.0 - creator_hold / 80.0) if creator_hold else 0.5,
        "creator_win_rate": _clip01(win_rate),
        "creator_serial_penalty": serial,
        "migrate_speed": migrate_speed,
        "buy_pressure": _clip01(pressure),
        "liquidity_n": _log_norm(liq, 80_000),
        "volume_n": _log_norm(vol, 50_000),
        "name_quality": quality,
        "nsfw": 1.0 if coin.get("nsfw") else 0.0,
        "banned": 1.0 if coin.get("banned") else 0.0,
        "gmgn_present": 1.0 if gmgn.get("source") else 0.0,
        "gmgn_honeypot": 1.0 if gmgn.get("honeypot") else 0.0,
        "gmgn_bundled": 1.0 if gmgn.get("bundled") else 0.0,
        "gmgn_insider_n": _clip01(float(gmgn.get("insider_pct") or 0) / 100.0),
        "gmgn_sniper_n": _clip01(float(gmgn.get("sniper_pct") or 0) / 100.0),
        "gmgn_rug_n": _clip01(float(gmgn.get("rug_risk") or 0) / 100.0),
        "gmgn_smart_n": _log_norm(float(gmgn.get("smart_degen") or 0), 40),
        "gmgn_kol_n": _log_norm(float(gmgn.get("renowned") or 0), 20),
        "gmgn_bot_rate_n": _clip01(float(gmgn.get("bot_rate") or 0) / 100.0),
        "gmgn_wash": 1.0 if gmgn.get("wash_trading") else 0.0,
        "gmgn_sniper_count_n": _clip01(float(gmgn.get("sniper_count") or 0) / 40.0),
        "gmgn_bundler_vol_n": _clip01(float(gmgn.get("bundler_vol_pct") or 0) / 100.0),
        "gmgn_dev_sold": 1.0 if gmgn.get("dev_sold") else 0.0,
        "gmgn_creator_ath_n": _log_norm(float(gmgn.get("creator_ath_mc") or 0), 5_000_000),
        "gmgn_dexscr_paid": 1.0 if gmgn.get("dexscr_paid") else 0.0,
        "gmgn_tax_n": _clip01(max(float(gmgn.get("buy_tax_pct") or 0), float(gmgn.get("sell_tax_pct") or 0)) / 100.0),
        "gmgn_og": 1.0 if gmgn.get("og") else 0.0,
        "gmgn_lock_n": _clip01(float(gmgn.get("lock_pct") or 0) / 100.0),
        "gmgn_burned": 1.0 if gmgn.get("burned") else 0.0,
        "gmgn_cto": 1.0 if gmgn.get("cto") else 0.0,
        "gmgn_image_dup": _clip01(float(gmgn.get("image_dup") or 0) / 4.0),
        "usd_buy_pressure": _usd_pressure(gmgn),
        "symbol_flood_n": _clip01(float(ctx.get("symbol_flood") or 0) / 4.0),
        "x_handle_hijack": 1.0 if (tw.get("hijack") or _brand_website_claim(ctx)) else 0.0,
        "funder_rug_n": _funder_rug(ctx.get("funder_stats") or {}),
        "entry_collapse": _entry_collapse(market, coin, ctx.get("chain")),
        "github_auth_n": github_authenticity(gh),
        "real_project": _real_project(ctx, tw, gh),
        "meta_heat": 1.0 if ctx.get("meta_match") else 0.0,
        "market_heat_n": _clip01(float(ctx.get("market_heat") or 1.0) / 2.0),
        "entry_premium": _entry_premium(
            market, coin, minutes, ctx.get("chain"), t0=ctx.get("t0_mcap")
        ),
        "mcap_per_holder_n": _mcap_per_holder(market, coin, holder_count),
        "launchpad_pons": _launchpad_pons(ctx, gmgn, coin),
        "gmgn_activity_n": _gmgn_activity(gmgn),
        "x_mentions_n": _log_norm(float(ctx.get("x_mentions") or 0), 50),
        "gmgn_rat_n": _clip01(float(gmgn.get("rat_vol_pct") or 0) / 100.0),
        "gmgn_renounced": 1.0 if gmgn.get("renounced") else 0.0,
        "gmgn_open_source": 1.0 if gmgn.get("open_source") else 0.0,
        "gmgn_callout_n": _log_norm(float(gmgn.get("callout_count") or 0), 20),
        "gmgn_net_buy_n": _log_norm(max(0.0, float(gmgn.get("net_buy_24h") or 0)), 50_000),
        # RH paper buys at 0.50. 1–6 wallet Long.xyz prints were hitting
        # 55–77% on socials + a clean contract. Same 20-wallet floor as
        # the RH thin-print veto — Solana is untouched.
        # Live MEME: 4 Blockscout rows were the LP (98.8% pool) and
        # later last_liq $1.3M. Do not cap a fair LP open or a fat
        # Dex book. SHORT/PLUMBED $200-liq 4-wallets stay thin.
        "rh_thin_book": rh_thin_book_flag(
            chain,
            holder_count,
            _first_pos(
                float(ctx.get("last_liq") or 0),
                float(market.get("liquidity_usd") or 0),
            ),
            holders=holders if isinstance(holders, dict) else {},
        ),
        # Leftover FDV with 20+ holders (RETAILS 36 / p=0.92 / last_liq=0)
        # cleared the thin-book cap. A 4-wallet Dex miss in the first
        # minutes is still POV-class. A 44-wallet book with $0 liq at
        # 5m (live WOLVERINE/PU) is leftover — cap now. Keep out of
        # FEATURE_NAMES — do not resize the trained vector.
        # Live STACKS: GMGN leftover FDV (~$35k liq, $0 vol) blended into
        # `liq`, so empty-book saw a "pool" while Dex / last_liq / t0
        # snap were $0. A trench card with liq AND hour-one volume is a
        # real book Dex has not indexed yet (POV $6k / $4k).
        "rh_empty_book": 1.0
        if is_rh_empty_book(
            chain,
            _first_pos(
                float(market.get("liquidity_usd") or 0),
                stored_liq,
                gmgn_live_liq,
            ),
            last_liq=ctx.get("last_liq"),
            age_min=float(ctx.get("age_min") or 0),
            holders=holder_count,
        )
        else 0.0,
        # Live COIN/FDC: ~1100 PONS wallets on a $3.5k book cleared
        # empty/thin and paper-bought after re-anchor. Heuristic-only.
        "rh_airdrop_book": 1.0
        if is_rh_airdrop_book(
            chain,
            holder_count,
            ctx.get("last_liq"),
            liq=_first_pos(
                float(market.get("liquidity_usd") or 0),
                stored_liq,
            ),
        )
        else 0.0,
        # Heuristic-only. Already on the research card / Dex pair; do not
        # append to FEATURE_NAMES (trained vector stays 68-wide).
        "twitter_tweets_n": _log_norm(float(tw.get("tweets") or 0), 500),
        "vol_persist_n": _vol_persist(vol, float(market.get("volume_h24") or 0)),
        "price_change_h1_n": _price_change_n(float(market.get("price_change_h1") or 0)),
    }
    # Heuristic-only live context. Do not append to FEATURE_NAMES.
    out["age_min"] = float(ctx.get("age_min") or 0)
    out["live_multiple"] = float(ctx.get("live_multiple") or ctx.get("multiple") or 0)
    out["organic_book"] = 1.0 if is_organic_book(out) else 0.0
    twitter_url = str(ctx.get("twitter_url") or coin.get("twitter") or "")
    out["staged_social"] = 1.0 if is_staged_social_pair(twitter_url, ctx.get("website")) else 0.0
    # Live LIVOCAT: bot-dominated dump scored 0.53 and paper-bought.
    # Combined veto — each flag alone is only −0.10 / −0.05.
    out["rh_bot_dump"] = 1.0 if is_rh_bot_dump_book(chain, out) else 0.0
    # Heuristic-only. Live MEME fair LP open. Do not append to FEATURE_NAMES.
    out["rh_lp_open_book"] = 1.0 if chain == "robinhood" and rh_lp_open_book(holders) else 0.0
    return out


def stall_honesty_cap(
    p: float,
    *,
    age_min: float = 0.0,
    multiple: float = 0.0,
    labeled: bool = False,
) -> float:
    """Pull p down when a name sits under 2x.

    Live 13:50 RH: TEAL 0.87 / 75m / 1.0x, XAI 0.92 / 4.5h / 1.0x,
    SHELLY 0.77 / 75m / 1.0x. Top-decile win rate is 0.096; the
    0.7–0.8 bin is 0.74 predicted / 0.125 actual. Do not touch 2x+
    (YOLO / GUH / CASHBIRD) or labeled rows. FEATURE_NAMES stays 66.
    """
    if labeled:
        return p
    m = float(multiple or 0.0)
    if m >= 2.0:
        return p
    age = float(age_min or 0.0)
    if age < 30.0:
        return p
    flat = m < 1.3
    if age >= 240.0:
        cap = 0.25 if flat else 0.48
    elif age >= 120.0:
        cap = 0.35 if flat else 0.55
    elif age >= 60.0 and flat:
        cap = 0.48
    elif age >= 30.0 and flat:
        cap = max(0.48, p - 0.12)
    else:
        return p
    return min(float(p), cap)


def ath_dump_honesty_cap(
    p: float,
    *,
    max_mcap: float = 0.0,
    last_mcap: float = 0.0,
    multiple: float = 0.0,
) -> float:
    """Pull p down when a confirmed run dumped off ATH.

    Live BELIEVE: honest 28× from $25k t0 to $703k, then last print
    $89k (13% of ATH) still showed 78. Keep it on confirmed 5×; this
    is not a start-high wick. FEATURE_NAMES stays 66.
    """
    mx = float(max_mcap or 0.0)
    last = float(last_mcap or 0.0)
    if mx <= 0 or last <= 0:
        return p
    if float(multiple or 0.0) < 5.0:
        return p
    retain = last / mx
    if retain >= 0.40:
        return p
    if retain < 0.15:
        cap = 0.35
    elif retain < 0.25:
        cap = 0.48
    else:
        cap = 0.55
    return min(float(p), cap)


def is_rh_empty_book(
    chain: str | None,
    liq: float,
    *,
    last_liq: float | None,
    age_min: float,
    holders: float,
) -> bool:
    """No live RH pool. 20+ holder leftovers cap immediately; Dex-miss
    infants under 20 wallets still wait 30 minutes (POV).

    `liq` is the Dex / stored book, or a GMGN trench card that also
    has hour-one volume. GMGN leftover FDV (liq, no tape) does not count.
    """
    from ..chains import normalize_chain

    if normalize_chain(chain) != "robinhood":
        return False
    if float(liq or 0) >= 800:
        return False
    if last_liq is None:
        return False
    stored = float(last_liq or 0)
    if stored >= 800:
        return False
    # 20+ wallets and a known-empty last_liq: leftover FDV
    # (WOLVERINE 44 / $0 at 5m). A missing last_liq is still a Dex miss.
    if float(holders or 0) >= 20:
        return True
    return stored < 800 and float(age_min or 0) >= 30


def is_rh_airdrop_book(
    chain: str | None,
    holders: float,
    last_liq: float | None,
    *,
    liq: float = 0.0,
) -> bool:
    """PONS factory tape: hundreds of wallets on a dust book.

    Live COIN/FDC: 1093/1127 holders and $3.5k last_liq scored 0.60
    after re-anchor. Real books stay clear — CASHBIRD 24/$76k, GUH
    105/$47k, POV. Do not append to FEATURE_NAMES.
    """
    from ..chains import normalize_chain

    if normalize_chain(chain) != "robinhood":
        return False
    if float(holders or 0) < 200:
        return False
    stored = float(last_liq or 0)
    live = float(liq or 0)

    def _dust(book: float) -> bool:
        return 800.0 <= book < 8_000.0

    # Live Goldinu: last_liq parked $4k / 1148 wallets but Dex still
    # printed the $30k leftover book, so max() hid the airdrop and
    # p stayed 0.92. Prefer the dust print when either side is one.
    if _dust(stored):
        book = stored
    elif _dust(live):
        book = live
    else:
        book = max(stored, live)
    if book < 800 or book >= 8_000:
        return False
    return (book / float(holders)) < 15.0


def is_sol_airdrop_tape(
    chain: str | None,
    holders: float,
    last_liq: float | None,
) -> bool:
    """Sol hunt sort: hundreds of wallets on a thin-per-wallet book.

    Live 07:40: Loria 2105 / $18k and Cheyenne 4998 / $47k sat above
    OnlyUpBOT 55 / $219k because the RH airdrop helper is RH-only and
    requires book < $8k. Live 11:40: Syoma 356 / $10k / $30.7 sat #11
    above USWR $223k. Live 12:21: the book grew to $14.8k / $41.6 and
    sat #12 again. Approaching uses a tighter sat-dust rule so Bunny
    607w / $3.3k / 4.98× and SUNLIVE 762w / $30k stay. Do not use
    in predict; do not append to FEATURE_NAMES.
    """
    from ..chains import normalize_chain

    if normalize_chain(chain) != "sol":
        return False
    n = float(holders or 0.0)
    book = float(last_liq or 0.0)
    if n < 200 or book < 800.0:
        return False
    return (book / n) < 45.0


def is_rh_bot_dump_book(chain: str | None, features: dict[str, float]) -> bool:
    """Bot-dominated first-hour dump. Paper line veto, heuristic-only.

    Live LIVOCAT: 69 wallets / $6.5k / p=0.53 after −0.10 bot and −0.05
    dump, then paper-bought and marked 0.36x. Do not append to FEATURE_NAMES.
    """
    from ..chains import normalize_chain

    if normalize_chain(chain) != "robinhood":
        return False
    organic = bool(features.get("organic_book"))
    bot_cut = 0.75 if organic else 0.5
    if float(features.get("gmgn_bot_rate_n") or 0) <= bot_cut:
        return False
    return float(features.get("price_change_h1_n") or 0) <= 0.22 and float(features.get("volume_n") or 0) > 0.45


def prefer_rh_dust_liq(stored: float | None, live: float = 0.0) -> float:
    """Keep a parked dust book when Dex still prints leftover FDV.

    Live Goldinu: last_liq $4k, Dex leftover $30k. Writing the leftover
    onto last_liq hid the airdrop and skipped the 0.48 second-look.
    """
    parked = float(stored or 0.0)
    tick = float(live or 0.0)
    if 800.0 <= parked < 8_000.0 and tick >= 8_000.0:
        return parked
    return tick if tick > 0 else parked


def _vol_persist(vol_h1: float, vol_h24: float) -> float:
    """Hour-one volume that is still printing later is a real book, not a wick."""
    if vol_h1 <= 0 or vol_h24 <= vol_h1:
        return 0.0
    return _clip01((vol_h24 / vol_h1) / 8.0)


def _price_change_n(pct: float) -> float:
    """Map Dex h1 % change onto [0, 1]. 0% ≈ 0.40, −60% = 0, +90% = 1."""
    return _clip01((pct + 60.0) / 150.0)


def is_organic_book(features: dict[str, float]) -> bool:
    """Live deepening book at entry — the actual 5x signature.

    Classic path: rehanfal 19x / MACRODUCK 9.8x (liq + hour-one volume +
    70+ wallets). Wide path: PVP 10x had the holders and the pool
    (138 wallets / $15k liq) but only $1.1k hour-one volume — vol_n 0.65
    missed 0.74. Lottery tickets (Pappy 13, SOLLY 12) stay out.

    Thresholds are in log-space (see extract_features scales). Equivalents:
    liquidity_n 0.75 ≈ $5k, volume_n 0.74 ≈ $3.5k / 0.64 ≈ $1k,
    holder_n 0.55 ≈ 70 wallets / 0.64 ≈ 130 wallets.
    Looser cuts (~0.40) treated a $90 / 30-wallet print as organic.
    """
    if features.get("entry_premium") or features.get("entry_collapse"):
        return False
    liq = float(features.get("liquidity_n") or 0)
    vol = float(features.get("volume_n") or 0)
    holders = float(features.get("holder_n") or 0)
    classic = liq >= 0.75 and vol >= 0.74 and holders >= 0.55
    wide = liq >= 0.75 and vol >= 0.64 and holders >= 0.64
    # Live 15:10: fih / BONZI / MACRODUCK sat 0.53–0.54 — 70–130
    # wallets and a $1k tape, shy of wide's 130-wallet cut. Capture
    # is 0.60; this is not the $90 / 30-wallet lottery.
    mid = liq >= 0.75 and vol >= 0.64 and holders >= 0.55
    return classic or wide or mid


def has_live_tape(features: dict[str, float]) -> bool:
    """A pool that is printing, not a social card on leftover FDV."""
    if is_organic_book(features) or bool(features.get("organic_book")):
        return True
    return float(features.get("liquidity_n") or 0) >= 0.75 and float(features.get("volume_n") or 0) >= 0.64


def social_ticket_cap(p: float, features: dict[str, float]) -> float:
    """Sol paper 0.70 bought 77/949 at −45%. 0.6–0.8 bins win 33–44%.

    Twitter + website + clean GMGN without a live tape (HOLMES /
    GINGER / PSYOP at $69k) must not clear the paper line.
    FEATURE_NAMES stays 66.
    """
    if has_live_tape(features):
        return p
    return min(float(p), 0.68)


def is_broad_book(features: dict[str, float]) -> bool:
    """Holder base is wide enough that top wallets are not the whole market."""
    return float(features.get("top10_inv") or 0) >= 0.75 and float(features.get("holder_n") or 0) >= 0.45


def _graduation_floor(chain: str | None) -> float:
    from ..chains import graduation_mcap

    return graduation_mcap(chain)


def _entry_premium(
    market: dict[str, Any],
    coin: dict[str, Any],
    minutes_to_migrate: float,
    chain: str | None = None,
    t0: float | None = None,
) -> float:
    """Pre-pumped through migration: arriving minutes after graduation at
    many multiples of the graduation baseline means a bundle bought the whole
    curve and the 'price' is theirs, not a market's (USWS: $2.1M in 40s).

    RH honest entry is 2.5× the $40k floor (same band as is_prepumped_entry).
    Live KFC: stored t0 $146k / Dex book $40k — market-only mcap missed it.
    """
    from ..chains import normalize_chain

    mcap = max(
        float(market.get("mcap_usd") or 0.0),
        float(coin.get("mcap_usd") or 0.0),
        float(t0 or 0.0),
    )
    if mcap <= 0:
        return 0.0
    floor = _graduation_floor(chain)
    if floor <= 0:
        return 0.0
    premium = mcap / floor
    # Keep in sync with outcomes.MAX_HONEST_ENTRY_MULTIPLE_RH (2.5).
    if normalize_chain(chain) == "robinhood" and premium >= 2.5:
        return 1.0
    if premium >= 10.0 and 0 < minutes_to_migrate < 30:
        return 1.0
    if premium >= 5.0 and 0 < minutes_to_migrate < 5:
        return 1.0
    return 0.0


def _mcap_per_holder(market: dict[str, Any], coin: dict[str, Any], holder_count: float) -> float:
    """Value per holder: $2M across 66 wallets is a stage set, not a market."""
    mcap = float(market.get("mcap_usd") or coin.get("mcap_usd") or 0.0)
    if mcap <= 0 or holder_count <= 0:
        return 0.0
    return _clip01((mcap / holder_count) / 40_000.0)


def _real_project(ctx: dict[str, Any], tw: dict[str, Any], gh: dict[str, Any]) -> float:
    """A genuine GitHub with pre-launch history AND a credible dev X account.
    The rare combination that marks an actual project rather than a meme —
    and the profile behind most tech-narrative runners."""
    if github_authenticity(gh) < 0.6:
        return 0.0
    if not ctx.get("twitter_handle") or tw.get("hijack"):
        return 0.0
    followers = float(tw.get("followers") or 0)
    age = float(tw.get("age_days") or 0)
    credible_x = bool(tw.get("verified")) or age >= 90 or followers >= 1_000
    return 1.0 if credible_x else 0.0


def github_authenticity(gh: dict[str, Any]) -> float:
    """Does the linked repo have real history, or was it staged for launch?
    Real projects: history predating the token, real commit volume, more
    than one contributor. Staged repos: same-week creation, a couple of
    commits, or a straight fork of someone else's code."""
    if not gh or not gh.get("full_name"):
        return 0.0
    s = 0.1
    age = float(gh.get("age_days") or 0.0)
    if age >= 30:
        s += 0.3
    elif age >= 7:
        s += 0.1
    commits = gh.get("commits")
    if commits is not None:
        if commits >= 20:
            s += 0.25
        elif commits <= 2:
            s -= 0.25
    contributors = gh.get("contributors")
    if contributors is not None and contributors >= 2:
        s += 0.15
    if float(gh.get("stars") or 0) >= 5:
        s += 0.15
    if gh.get("is_fork"):
        s -= 0.4
    return _clip01(s)


def _entry_collapse(market: dict[str, Any], coin: dict[str, Any], chain: str | None = None) -> float:
    """Already trading far below graduation mcap minutes after migrating:
    the start-high-and-rug pattern — the dump happened before we arrived."""
    from ..chains import normalize_chain

    mcap = float(market.get("mcap_usd") or coin.get("mcap_usd") or 0.0)
    if mcap <= 0:
        return 0.0
    # RH honest books print $3–13k (CATTIES / POV / JJJACKET). 0.4× the
    # $40k floor is $16k and brands those as start-high rugs, then the
    # runners SQL drops them. A live pool is not a dump.
    if normalize_chain(chain) == "robinhood":
        liq = float(market.get("liquidity_usd") or 0.0)
        if liq >= 800.0:
            return 0.0
    return 1.0 if mcap < 0.4 * _graduation_floor(chain) else 0.0


def _funder_rug(stats: dict[str, Any]) -> float:
    """How rug-heavy is the wallet that funded this creator, per our own
    resolved outcomes. Wins forgive: a funder with wins and rugs is a volume
    player, not a rug factory. CEX hop labels never reach here — funder_stats
    returns {} unless fund_from is a chain address (Stamp / Binance)."""
    rugs = float(stats.get("rugs") or 0)
    wins = float(stats.get("wins") or 0)
    if rugs <= 0:
        return 0.0
    return _clip01((rugs - wins) / 3.0)


def _launchpad_pons(ctx: dict[str, Any], gmgn: dict[str, Any], coin: dict[str, Any]) -> float:
    """PONS is the official RH pad. Factory leftovers and random Dex prints
    are a different, more P&D-heavy population."""
    pad = str(gmgn.get("launchpad") or coin.get("launchpad") or "").strip().lower()
    source = str(ctx.get("source") or coin.get("source") or "").strip().lower()
    if pad == "pons" or source in ("rh_pons", "pons"):
        return 1.0
    return 0.0


def _gmgn_activity(gmgn: dict[str, Any]) -> float:
    """First-hour tape that is already on the research card (swaps / hot)."""
    if not gmgn.get("source"):
        return 0.0
    swaps = _log_norm(float(gmgn.get("swaps_1h") or 0), 400)
    hot = _clip01(float(gmgn.get("hot_level") or 0) / 5.0)
    vol = _log_norm(float(gmgn.get("volume_1h") or 0), 50_000)
    return max(swaps, hot, vol)


def _usd_pressure(gmgn: dict[str, Any]) -> float:
    """USD-volume buy share in the first hour — count-based pressure is easy
    to fake with dust trades; dollar-weighted pressure is harder."""
    buy = float(gmgn.get("buy_vol_1h") or 0)
    sell = float(gmgn.get("sell_vol_1h") or 0)
    if buy + sell <= 0:
        return 0.5
    return _clip01(buy / (buy + sell))


def feature_vector(features: dict[str, float]) -> list[float]:
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]


def heuristic_probability(features: dict[str, float], reasons: list[str], flags: list[str]) -> float:
    try:
        return _heuristic_probability(features, reasons, flags)
    except (TypeError, KeyError, ValueError):
        # Incomplete stored snapshots must not crash predict() / repairs.
        return 0.50


def _heuristic_probability(features: dict[str, float], reasons: list[str], flags: list[str]) -> float:
    p = 0.28
    organic = is_organic_book(features) or bool(features.get("organic_book"))
    broad = is_broad_book(features)
    if features["has_twitter"]:
        p += 0.07
        reasons.append("Has an X account")
    if features["twitter_followers_n"] > 0.35:
        p += 0.07
        reasons.append("X account already has real followership")
    if features["twitter_age_n"] > 0.25:
        p += 0.06
        reasons.append("X account is not brand-new")
    elif features["has_twitter"] and features["twitter_age_n"] < 0.02:
        # Live MEME: @amemecoinrh created 26m before migrate, verified,
        # 0 followers. That is the official launch account, not a
        # stolen handle. Keep the hit on unverified new X / non-LP.
        if not (rh_lp_open_clears_paper(features) and features.get("twitter_verified")):
            p -= 0.08
            flags.append("X account created very recently")
    if features["twitter_verified"]:
        p += 0.04
        reasons.append("X account is verified")
    if features["has_website"]:
        p += 0.04
        reasons.append("Has a website")
    if features["has_telegram"]:
        p += 0.03
        reasons.append("Has Telegram")
    if features["has_github"]:
        p += 0.05
        reasons.append("Public GitHub linked")
    if features["github_stars_n"] > 0.2:
        p += 0.04
        reasons.append("GitHub repo has stars")
    if features["reply_n"] > 0.35:
        p += 0.05
        reasons.append("Active Pump.fun thread")
    if features["holder_n"] > 0.4:
        p += 0.06
        reasons.append("Holder count looks organic")
    if features["top10_inv"] < 0.35:
        p -= 0.12
        flags.append("Supply looks concentrated in top wallets")
    elif features["top10_inv"] > 0.65:
        p += 0.05
        reasons.append("Top wallets do not dominate supply")
    if features.get("top1_inv", 0.5) < 0.25:
        p -= 0.08
        flags.append("One wallet holds a huge share")
    if features.get("fresh_wallet_n", 0) > 0.55 and not organic:
        p -= 0.08
        flags.append("Many top wallets look brand-new (bundle/sybil risk)")
    if features.get("creator_hold_inv", 0.5) < 0.35:
        p -= 0.1
        flags.append("Creator still holds a large bag")
    if features.get("creator_win_rate", 0) > 0.4:
        p += 0.08
        reasons.append("Creator has prior winners")
        # CAC/BEAST-class: proven creators often print tight early books
        # that later distribute. Do not let concentration alone floor them.
        if features.get("top10_inv", 0.45) < 0.35:
            p += 0.08
            reasons.append("Proven creator — tight early book is not a disqualifier")
    if features.get("creator_serial_penalty", 0) > 0.4 and not organic and not broad:
        p -= 0.12
        flags.append("Creator looks like a serial deployer")
    if features["migrate_speed"] < 0.15:
        p -= 0.1
        flags.append("Bonding curve filled almost instantly (bundle risk)")
    elif features["migrate_speed"] > 0.75:
        p += 0.05
        reasons.append("Time-to-migrate looks human, not instant")
    if features["buy_pressure"] > 0.62:
        p += 0.05
        reasons.append("More buys than sells right after migrate")
    elif features["buy_pressure"] < 0.38:
        p -= 0.06
        flags.append("Sellers already dominate")
    if features["liquidity_n"] > 0.45:
        p += 0.03
    if features["nsfw"]:
        p -= 0.04
        flags.append("Marked NSFW")
    if features["banned"]:
        p -= 0.2
        flags.append("Banned on Pump.fun")
    if features["name_quality"] < 0.2:
        p -= 0.08
        flags.append("Name/ticker looks like a claim/phishing bait")
    if features.get("gmgn_honeypot"):
        p -= 0.22
        flags.append("GMGN flags this as a honeypot")
    if features.get("gmgn_bundled"):
        p -= 0.1
        flags.append("GMGN flags bundled launch")
    if features.get("gmgn_present"):
        if features.get("gmgn_insider_n", 0) > 0.25:
            p -= 0.1
            flags.append("GMGN insider share is high")
        if features.get("gmgn_sniper_n", 0) > 0.35:
            p -= 0.08
            flags.append("GMGN sniper share is high")
        if features.get("gmgn_rug_n", 0) > 0.4:
            p -= 0.12
            flags.append("GMGN rug risk is elevated")
        elif (
            not features.get("gmgn_honeypot")
            and not features.get("gmgn_bundled")
            and features.get("gmgn_rug_n", 0) < 0.15
            and features.get("gmgn_insider_n", 0) < 0.12
        ):
            p += 0.03
            reasons.append("GMGN security snapshot looks clean")
        if features.get("gmgn_smart_n", 0) > 0.35:
            p += 0.06
            reasons.append("GMGN smart-money wallets are already in")
        # Do not penalize "no smart money yet" — they arrive after we score.
        if features.get("gmgn_kol_n", 0) > 0.3:
            p += 0.03
            reasons.append("GMGN KOL wallets hold this token")
        # arXiv 2602.14860: bot-dominated markets have systematically lower
        # success; GMGN DD card: wash trading and >20 snipers are hard stops.
        if features.get("gmgn_wash"):
            p -= 0.12
            flags.append("GMGN detects wash trading")
        bot_cut = 0.75 if organic else 0.5
        if features.get("gmgn_bot_rate_n", 0) > bot_cut:
            p -= 0.1
            flags.append("Bot wallets dominate holders")
        if features.get("gmgn_sniper_count_n", 0) > 0.5:
            p -= 0.08
            flags.append("Heavy sniper presence at launch (20+)")
        bundler_cut = 0.5 if organic else 0.3
        if features.get("gmgn_bundler_vol_n", 0) > bundler_cut:
            p -= 0.08
            flags.append("Large share of volume is bundler bots")
        if features.get("gmgn_dev_sold"):
            p += 0.04
            reasons.append("Dev has exited its bag (no dev-dump overhang)")
        if features.get("gmgn_creator_ath_n", 0) > 0.5:
            p += 0.06
            reasons.append("Creator previously launched a multi-million mcap token")
        if features.get("usd_buy_pressure", 0.5) > 0.62:
            p += 0.04
            reasons.append("Dollar-weighted buys outweigh sells in hour one")
        elif features.get("usd_buy_pressure", 0.5) < 0.35:
            p -= 0.06
            flags.append("Dollar-weighted selling dominates hour one")
        tax = features.get("gmgn_tax_n", 0)
        if tax >= 0.10:
            p -= 0.12
            flags.append("Buy/sell tax is high")
        elif tax >= 0.05:
            p -= 0.06
            flags.append("Buy/sell tax is elevated")
        if features.get("gmgn_og"):
            p += 0.03
            reasons.append("GMGN OG badge")
        if features.get("gmgn_lock_n", 0) >= 0.5 or features.get("gmgn_burned"):
            p += 0.03
            reasons.append("Liquidity locked or burned")
        if features.get("gmgn_cto"):
            flags.append("Community takeover (original dev left)")
        if features.get("gmgn_image_dup", 0) >= 0.5:
            p -= 0.06
            flags.append("Logo reused on other tokens")
    if features.get("symbol_flood_n", 0) >= 0.5:
        if rh_lp_open_clears_paper(features):
            # Live MEME: $MEME is the most-copied ticker. A fair LP
            # open is the real launch, not the spam wave. Runner
            # copycat filters stay — this is heuristic ingest only.
            pass
        else:
            p -= 0.1
            flags.append("Same ticker launched repeatedly in 24h (copycat spam)")
    if features.get("x_handle_hijack"):
        # Cancel the social credit the fake claim earned, then penalize.
        p -= 0.18
        flags.append("Claimed X account never tweets this ticker (likely hijacked)")
    if features.get("funder_rug_n", 0) >= 0.6:
        p -= 0.12
        flags.append("Creator was funded by a wallet behind prior rugs")
    if features.get("entry_collapse"):
        p -= 0.3
        flags.append("Already dumped below graduation mcap (start-high rug pattern)")
    if features.get("has_github"):
        if features.get("github_auth_n", 0) >= 0.6:
            p += 0.08
            reasons.append("GitHub repo has genuine pre-launch history")
        elif features.get("github_auth_n", 0) <= 0.15:
            p -= 0.06
            flags.append("Linked GitHub repo looks staged for launch")
    if features.get("real_project"):
        p += 0.1
        reasons.append("Real dev project: genuine GitHub history + credible dev X account")
    if features.get("meta_heat") and features.get("symbol_flood_n", 0) < 0.5:
        # Fresh entry in a meta that just produced winners — the wave trade.
        p += 0.06
        reasons.append("Name matches a meta with recent confirmed winners")
    if features.get("entry_premium"):
        if is_rh_late_dex_book(features):
            # Live CRCL: −0.25 left p at 0.18 and hid a 438-wallet
            # $316k-vol Dex book. Stay under paper 0.50.
            p -= 0.10
            flags.append("Late Dex catch-up on a real book (start-high t0)")
        else:
            p -= 0.25
            flags.append("Pre-pumped through migration at a huge premium (bundle-owned price)")
    if features.get("mcap_per_holder_n", 0) >= 0.5:
        p -= 0.12
        flags.append("Tiny holder base for its market cap")
    if organic:
        p += 0.10
        reasons.append("Live book looks organic (liq + volume + holders)")
    if broad:
        p += 0.04
        reasons.append("Holder base is broad, not a top-wallet stage")
    if features.get("launchpad_pons"):
        p += 0.04
        reasons.append("Official PONS launchpad")
    if features.get("gmgn_present") and features.get("gmgn_activity_n", 0) > 0.4:
        p += 0.03
        reasons.append("Active first-hour tape")
    if features.get("x_mentions_n", 0) > 0.4:
        p += 0.03
        reasons.append("Ticker is being mentioned on X")
    if features.get("volume_n", 0) > 0.74 and not organic:
        p += 0.03
        reasons.append("First-hour volume is real")
    if features.get("gmgn_present") and features.get("gmgn_rat_n", 0) > 0.3:
        p -= 0.08
        flags.append("GMGN rat-trader volume is high")
    if features.get("gmgn_present") and features.get("gmgn_renounced"):
        p += 0.03
        reasons.append("Contract ownership is renounced")
    if features.get("gmgn_present") and features.get("gmgn_open_source"):
        p += 0.02
        reasons.append("Contract source is verified")
    if features.get("gmgn_present") and features.get("gmgn_callout_n", 0) > 0.35:
        p += 0.02
        reasons.append("Token has public callouts")
    if features.get("gmgn_present") and features.get("gmgn_net_buy_n", 0) > 0.45:
        p += 0.02
        reasons.append("Net buy tape is positive")
    if features.get("gmgn_present") and features.get("gmgn_dexscr_paid") and organic:
        p += 0.02
        reasons.append("Dexscreener profile is paid")
    if features.get("market_heat_n", 0) >= 0.75:
        p += 0.03
        reasons.append("Launch tape is hotter than usual")
    if features.get("has_twitter") and features.get("twitter_tweets_n", 0) < 0.08 and not features.get("twitter_verified"):
        p -= 0.04
        flags.append("Linked X account has almost no posts")
    if features.get("vol_persist_n", 0) >= 0.5:
        p += 0.04
        reasons.append("Volume is persisting past the first hour")
    if features.get("price_change_h1_n", 0) <= 0.22 and features.get("volume_n", 0) > 0.45:
        p -= 0.05
        flags.append("First-hour tape is dumping on real volume")
    elif features.get("price_change_h1_n", 0) >= 0.62 and features.get("volume_n", 0) > 0.5:
        p += 0.03
        reasons.append("First-hour tape is up on real volume")
    if rh_lp_open_clears_paper(features):
        # Live MEME: copycat ticker (−0.10) + brand-new X (−0.08)
        # left heuristic at 0.24, then rh_thin capped and the
        # model buried it at 0.03. The paper window was ~6 min —
        # do not wait on GMGN to clear a Blockscout LP open.
        # Honeypot / bundled / rug still withhold this bump.
        p += 0.16
        reasons.append("Fair LP open — pool is the book, not a 4-wallet wick")
    if not has_live_tape(features):
        capped = social_ticket_cap(p, features)
        if capped + 1e-4 < p:
            flags.append("Social card without a live tape")
        p = capped
    if features.get("rh_thin_book"):
        if p >= 0.50:
            flags.append("Robinhood book is too thin to be a market")
        p = min(p, 0.48)
    if features.get("rh_empty_book"):
        if p >= 0.50:
            flags.append("Robinhood book has no live liquidity")
        p = min(p, 0.48)
    if features.get("rh_bot_dump"):
        if p >= 0.50:
            flags.append("Robinhood first-hour dump is bot-dominated")
        p = min(p, 0.48)
    # Watch prior is a side key, not FEATURE_NAMES. Small nudge only —
    # the model learns `_preview_p` separately when labels land.
    prev = float(features.get("preview_p") or 0.0)
    if prev >= 0.45:
        p += 0.04
        reasons.append("Watch preview was already constructive")
    elif 0 < prev <= 0.20:
        p -= 0.04
        flags.append("Watch preview was already thin")
    if is_brand_clone_book(features):
        flags.append("Claimed celebrity/brand X — not this launch")
        p = min(p, 0.48)
    if is_copycat_dump_book(features):
        p = min(p, 0.48)
    if is_copycat_flood_book(features):
        # After every organic / LP / preview bonus. Flood −0.10 is
        # cancelled by those bonuses otherwise (OpenAI C3Wme 75).
        p = min(p, 0.48)
    if is_funder_rug_book(features):
        # After those bonuses. Pumpball −0.12 + organic +0.10/+0.04
        # left heuristic at 92.
        p = min(p, 0.48)
    if is_staged_social_book(features):
        # After those bonuses. Live TWIN: status URL + twine.auction
        # collected verified/age credit and printed 92.
        flags.append("Project X is a pasted tweet on a generated website")
        p = min(p, 0.48)
    return max(0.02, min(0.92, p))
