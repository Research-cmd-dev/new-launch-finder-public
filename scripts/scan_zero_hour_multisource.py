#!/usr/bin/env python3
"""Zero-hour / first-day reconstruction via GMGN + Gecko + Helius.

Bitquery note: our plan is realtime-only (RH Initialize stream). Archive
DEXTradeByTokens returns 403 — cannot backfill 0–24h candles from Bitquery.
Helius getTransactionsForAddress + enhanced txs recovers Sol first-hour buyers.
GMGN / GeckoTerminal recover day/hour OHLCV for both chains.

Research only. Does not rewrite Decisions. FEATURE_NAMES stays 66.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from launchfinder.research.early_wallets import fetch_early_swaps, parse_early_buys

OUT = Path("/opt/cursor/artifacts/zero_hour_alpha.json")
MD = Path("/opt/cursor/artifacts/zero_hour_alpha.md")
HELIUS_OUT = Path("/opt/cursor/artifacts/zero_hour_helius.json")

GECKO_NET = {"sol": "solana", "robinhood": "robinhood"}
SUPPLY = 1_000_000_000.0

BITQUERY_LIMITATION = (
    "Bitquery product has archive (Sol DEXTradeByTokens from mid-2024, "
    "EVM/OHLCV with archive add-on). Our BITQUERY_API_TOKEN is realtime-only: "
    "streaming.bitquery.io serves ~last hours; fixed past windows >~12h empty; "
    "graphql.bitquery.io archive cubes 403. Upgrade archive to backfill "
    "year-old 0–24h; until then GMGN/Gecko + Helius."
)


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url, headers={"User-Agent": "lf-zh/2", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def dex_pair(mint: str, prefer: str) -> dict[str, Any] | None:
    d = http_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    pairs = [
        p
        for p in (d.get("pairs") or [])
        if ((p.get("baseToken") or {}).get("address") or "").lower() == mint.lower()
    ]
    prefer_set = {"solana"} if prefer == "sol" else {"robinhood"}
    pref = [p for p in pairs if (p.get("chainId") or "").lower() in prefer_set]
    use = pref or pairs
    use = sorted(
        use,
        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
        reverse=True,
    )
    return use[0] if use else None


def gecko_ohlcv(net: str, pair: str, created_ts: float, resolution: str) -> tuple[list, str | None]:
    if resolution == "day":
        before = int(created_ts) + 86400 * 10
        agg = "day"
        limit = 1000
    else:
        before = int(created_ts) + 86400 + 7200
        agg = "hour"
        limit = 1000
    url = (
        f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pair}"
        f"/ohlcv/{agg}?aggregate=1&limit={limit}&currency=usd&before_timestamp={before}"
    )
    try:
        d = http_get(url)
    except Exception as e:
        return [], str(e)
    data = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    rows = sorted(data, key=lambda x: x[0])
    if resolution == "hour":
        rows = [r for r in rows if created_ts - 1800 <= r[0] <= created_ts + 36 * 3600]
    return rows, None


def first_day_from_gecko(rows: list, created_ts: float) -> dict[str, Any] | None:
    if not rows:
        return None
    day = None
    for r in rows:
        if r[0] <= created_ts < r[0] + 86400:
            day = r
            break
    if day is None:
        after = [r for r in rows if r[0] + 86400 >= created_ts]
        day = after[0] if after else rows[0]
    o, h, l, c, v = day[1], day[2], day[3], day[4], day[5] or 0
    if not o or o <= 0:
        return None
    fdv_open = o * SUPPLY
    if fdv_open > 5e10:
        return None
    idx = rows.index(day)
    day2 = rows[idx + 1] if idx + 1 < len(rows) else None
    out: dict[str, Any] = {
        "date": datetime.fromtimestamp(day[0], tz=timezone.utc).date().isoformat(),
        "price_open": o,
        "price_high": h,
        "price_close": c,
        "fdv_open": round(fdv_open, 2),
        "fdv_high": round(h * SUPPLY, 2),
        "fdv_close": round(c * SUPPLY, 2),
        "multiple_high": round(h / o, 2),
        "multiple_close": round(c / o, 2),
        "volume_usd": round(v, 2),
        "vol_over_open_fdv": round(v / fdv_open, 2) if fdv_open > 0 else None,
        "source": "geckoterminal_day",
    }
    if day2 and day2[1] > 0:
        out["day2_high_mult_from_d0_open"] = round(day2[2] / o, 2)
    if fdv_open < 50:
        out["flag"] = "dust-open"
    return out


def ttm(hours: list, mult: float) -> float | None:
    if not hours:
        return None
    o0 = hours[0][1]
    if not o0:
        return None
    t0 = hours[0][0]
    target = o0 * mult
    for r in hours:
        if r[2] >= target:
            return round((r[0] - t0) / 3600.0, 2)
    return None


def fill_gecko_missing(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    need = [r for r in items if not r.get("ok")]
    log(f"gecko fill need={len(need)}")
    upd: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(need):
        mint, sym, chain = r["mint"], r["symbol"], r["chain"]
        net = GECKO_NET.get(chain)
        try:
            pair = dex_pair(mint, chain)
        except Exception as e:
            log(f"{i+1}/{len(need)} {sym} dex-fail {e}")
            upd[mint.lower()] = {**r, "error": f"dex:{e}"}
            time.sleep(0.25)
            continue
        if not pair:
            log(f"{i+1}/{len(need)} {sym} no-pair")
            upd[mint.lower()] = {**r, "error": "no-pair"}
            time.sleep(0.25)
            continue
        created = pair.get("pairCreatedAt")
        if not created:
            log(f"{i+1}/{len(need)} {sym} no-created")
            upd[mint.lower()] = {**r, "error": "no-created"}
            continue
        created_ts = int(created) / 1000.0
        rows, err = gecko_ohlcv(net or "", pair["pairAddress"], created_ts, "day")
        time.sleep(0.4)
        day = first_day_from_gecko(rows, created_ts) if rows else None
        hours, herr = gecko_ohlcv(net or "", pair["pairAddress"], created_ts, "hour")
        time.sleep(0.4)
        t: dict[str, Any] = {}
        if hours:
            t = {
                "to_2x_h": ttm(hours, 2),
                "to_5x_h": ttm(hours, 5),
                "to_10x_h": ttm(hours, 10),
                "to_50x_h": ttm(hours, 50),
                "n_hourly": len(hours),
            }
        ok = bool(day)
        row = {
            **r,
            "ok": ok,
            "first_day": day,
            "time_to": t,
            "pair": pair.get("pairAddress"),
            "pair_created_at": datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat(),
            "error": None if ok else (err or herr or "no-day"),
            "source": "gecko" if ok else r.get("source"),
        }
        upd[mint.lower()] = row
        d = day or {}
        log(
            f"{i+1}/{len(need)} {sym:12} ok={ok} open={d.get('fdv_open')} "
            f"high×={d.get('multiple_high')} vol/open={d.get('vol_over_open_fdv')} "
            f"t5={t.get('to_5x_h')}"
        )
    out = []
    for r in items:
        if r.get("ok"):
            rr = dict(r)
            rr.setdefault("source", "gmgn_kline_1d")
            fd = rr.get("first_day") or {}
            if float(fd.get("fdv_open") or 0) < 50:
                fd = dict(fd)
                fd["flag"] = "dust-open"
                rr["first_day"] = fd
            out.append(rr)
        else:
            out.append(upd.get(r["mint"].lower(), r))
    return out


def helius_first_hour_summary(
    buys: list[dict[str, Any]], n_txs: int
) -> dict[str, Any]:
    if not buys:
        return {"n_txs": n_txs, "n_unique_buyers": 0}
    ages = [float(b.get("age_at_buy_s") or 0) for b in buys]
    sols = [float(b.get("sol_spent") or 0) for b in buys if float(b.get("sol_spent") or 0) > 0]
    ages_sorted = sorted(ages)
    buyers_1m = sum(1 for a in ages if a <= 60)
    buyers_5m = sum(1 for a in ages if a <= 300)
    buyers_15m = sum(1 for a in ages if a <= 900)
    # concentration: top-10 share of token_amount
    amounts = sorted((float(b.get("token_amount") or 0) for b in buys), reverse=True)
    total_amt = sum(amounts) or 1.0
    top10 = sum(amounts[:10]) / total_amt
    return {
        "n_txs": n_txs,
        "n_unique_buyers": len(buys),
        "buyers_1m": buyers_1m,
        "buyers_5m": buyers_5m,
        "buyers_15m": buyers_15m,
        "median_age_s": round(statistics.median(ages_sorted), 1) if ages_sorted else None,
        "p90_age_s": round(ages_sorted[int(0.9 * (len(ages_sorted) - 1))], 1) if ages_sorted else None,
        "buyers_with_sol": len(sols),
        "median_sol": round(statistics.median(sols), 4) if sols else None,
        "sum_sol": round(sum(sols), 3) if sols else None,
        "top10_token_share": round(top10, 3),
        "source": "helius_enhanced",
    }


async def enrich_helius(items: list[dict[str, Any]], *, limit: int | None = None) -> list[dict[str, Any]]:
    sol = [r for r in items if r.get("chain") == "sol"]
    if limit:
        sol = sol[:limit]
    log(f"helius enrich sol={len(sol)}")
    out_by: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(sol):
        mint, sym = r["mint"], r["symbol"]
        pair = r.get("pair")
        created = r.get("pair_created_at")
        if not pair or not created:
            try:
                p = dex_pair(mint, "sol")
            except Exception as e:
                log(f"{i+1}/{len(sol)} {sym} dex-fail {e}")
                out_by[mint.lower()] = {**r, "helius": {"error": str(e)}}
                continue
            if not p:
                log(f"{i+1}/{len(sol)} {sym} no-pair")
                out_by[mint.lower()] = {**r, "helius": {"error": "no-pair"}}
                continue
            pair = p.get("pairAddress")
            created_ts = int(p.get("pairCreatedAt") or 0) / 1000.0
            created = datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat()
            r = {**r, "pair": pair, "pair_created_at": created}
        else:
            created_ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        start = datetime.fromtimestamp(created_ts, tz=timezone.utc)
        end = start + timedelta(hours=1)
        try:
            txs = await fetch_early_swaps(pair or "", start=start, end=end)
            buys = parse_early_buys(txs, mint=mint, pool=pair or "", launched=start)
            summary = helius_first_hour_summary(buys, len(txs))
        except Exception as e:
            log(f"{i+1}/{len(sol)} {sym} helius-fail {e}")
            out_by[mint.lower()] = {**r, "helius": {"error": str(e)}}
            await asyncio.sleep(0.3)
            continue
        row = {**r, "helius": summary}
        out_by[mint.lower()] = row
        log(
            f"{i+1}/{len(sol)} {sym:12} txs={summary.get('n_txs')} buyers={summary.get('n_unique_buyers')} "
            f"1m={summary.get('buyers_1m')} 5m={summary.get('buyers_5m')} "
            f"top10={summary.get('top10_token_share')} sum_sol={summary.get('sum_sol')}"
        )
        await asyncio.sleep(0.35)
    merged = []
    for r in items:
        if r.get("chain") == "sol" and r["mint"].lower() in out_by:
            merged.append(out_by[r["mint"].lower()])
        else:
            merged.append(r)
    return merged


def med(xs: list[Any]) -> float | None:
    vals = [float(x) for x in xs if x is not None]
    return round(statistics.median(vals), 3) if vals else None


def pct(xs: list[float], pred: Any) -> float | None:
    return round(sum(1 for x in xs if pred(x)) / len(xs), 3) if xs else None


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [
        r
        for r in items
        if r.get("ok")
        and r.get("first_day")
        and (r.get("first_day") or {}).get("flag") != "dust-open"
    ]
    dust = [
        r
        for r in items
        if r.get("ok") and (r.get("first_day") or {}).get("flag") == "dust-open"
    ]
    opens = [float(r["first_day"]["fdv_open"]) for r in ok]
    highs = [float(r["first_day"]["multiple_high"]) for r in ok]
    closes = [float(r["first_day"]["multiple_close"]) for r in ok]
    vols = [
        float(r["first_day"]["vol_over_open_fdv"])
        for r in ok
        if r["first_day"].get("vol_over_open_fdv") is not None
    ]
    ttm5 = [
        float(r["time_to"]["to_5x_h"])
        for r in ok
        if (r.get("time_to") or {}).get("to_5x_h") is not None
    ]
    ttm2 = [
        float(r["time_to"]["to_2x_h"])
        for r in ok
        if (r.get("time_to") or {}).get("to_2x_h") is not None
    ]
    ttm10 = [
        float(r["time_to"]["to_10x_h"])
        for r in ok
        if (r.get("time_to") or {}).get("to_10x_h") is not None
    ]
    pons_shape = [
        r["symbol"]
        for r in ok
        if float(r["first_day"]["fdv_open"]) < 100000
        and float(r["first_day"].get("vol_over_open_fdv") or 0) >= 10
        and float(r["first_day"]["multiple_high"]) >= 10
    ]

    helius_ok = [r for r in items if isinstance(r.get("helius"), dict) and r["helius"].get("n_unique_buyers")]
    # Split runners (day1 high >=10x) vs others among helius-enriched with first_day
    runners_h = [
        r
        for r in helius_ok
        if r.get("ok")
        and float((r.get("first_day") or {}).get("multiple_high") or 0) >= 10
    ]
    others_h = [
        r
        for r in helius_ok
        if r.get("ok")
        and float((r.get("first_day") or {}).get("multiple_high") or 0) < 10
    ]

    def hmed(rs: list[dict], key: str) -> float | None:
        return med([(r.get("helius") or {}).get(key) for r in rs])

    helius_alpha = {
        "n_enriched": len(helius_ok),
        "runners_ge_10x": {
            "n": len(runners_h),
            "median_buyers": hmed(runners_h, "n_unique_buyers"),
            "median_buyers_1m": hmed(runners_h, "buyers_1m"),
            "median_buyers_5m": hmed(runners_h, "buyers_5m"),
            "median_top10_share": hmed(runners_h, "top10_token_share"),
            "median_sum_sol": hmed(runners_h, "sum_sol"),
            "symbols": [r["symbol"] for r in runners_h],
        },
        "day1_lt_10x": {
            "n": len(others_h),
            "median_buyers": hmed(others_h, "n_unique_buyers"),
            "median_buyers_1m": hmed(others_h, "buyers_1m"),
            "median_buyers_5m": hmed(others_h, "buyers_5m"),
            "median_top10_share": hmed(others_h, "top10_token_share"),
            "median_sum_sol": hmed(others_h, "sum_sol"),
            "symbols": [r["symbol"] for r in others_h],
        },
    }

    def cohort_sum(name: str) -> dict[str, Any]:
        rs = [r for r in ok if r.get("cohort") == name]
        if not rs:
            return {"n": 0}
        return {
            "n": len(rs),
            "median_fdv_open": med([r["first_day"]["fdv_open"] for r in rs]),
            "median_high": med([r["first_day"]["multiple_high"] for r in rs]),
            "median_vol_over_open": med([r["first_day"].get("vol_over_open_fdv") for r in rs]),
            "pons_shape": [r["symbol"] for r in rs if r["symbol"] in pons_shape],
        }

    alpha_hypotheses = [
        "PONS-shape: open FDV < $100k + day-1 vol/open ≥ 10× + day-1 high ≥ 10× — catchable at first print, not leftover.",
        "Time-to-5× often < 2h on honest opens — midday promote at 2× is late confirmation, not discovery.",
        "Sol Helius: unique first-hour buyers and buyers_in_1m separate dense sniper books from thin opens (compare runners_ge_10x vs day1_lt_10x).",
        "top10_token_share high + low unique buyers = concentrated sniper dump risk — reject even if tape looks hot.",
        "Bitquery cannot rebuild archive candles on current plan — keep for live RH Initialize only.",
    ]
    strategy_candidates = [
        {
            "name": "pons_shape_gate",
            "rule": "At entry: mcap < 100k AND (volume_15m / mcap) proxy ≥ ~1× with rising unique buyers; promote when peak≥2×.",
            "extractable_live": True,
            "needs": ["entry Decision mcap", "tape volume", "optional Helius early buyers"],
        },
        {
            "name": "helius_density_gate",
            "rule": "Sol only: first 5m unique buyers ≥ cohort median of runners; reject if top10_token_share ≥ 0.6 with buyers < 30.",
            "extractable_live": True,
            "needs": ["Helius fetch_early_swaps within minutes of pair open"],
        },
        {
            "name": "leftover_reject",
            "rule": "If first-seen mcap ≥ $1M and age_hours large, never treat as zero-hour exemplar; score as continuation only.",
            "extractable_live": True,
            "needs": ["pairCreatedAt", "entry mcap"],
        },
    ]

    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "sources": ["gmgn_kline_1d", "geckoterminal", "helius_enhanced"],
        "bitquery": {"usable_for_archive": False, "note": BITQUERY_LIMITATION},
        "n": len(items),
        "n_ok": len(ok),
        "n_dust_open_excluded": len(dust),
        "n_missing": sum(1 for r in items if not r.get("ok")),
        "all": {
            "median_fdv_open": med(opens),
            "median_multiple_high": med(highs),
            "median_multiple_close": med(closes),
            "median_vol_over_open": med(vols),
            "median_hours_to_2x": med(ttm2),
            "median_hours_to_5x": med(ttm5),
            "median_hours_to_10x": med(ttm10),
            "buckets": {
                "open_lt_10k": pct(opens, lambda x: x < 10000),
                "open_10k_100k": pct(opens, lambda x: 10000 <= x < 100000),
                "open_100k_1m": pct(opens, lambda x: 100000 <= x < 1000000),
                "open_ge_1m": pct(opens, lambda x: x >= 1000000),
                "day1_ge_5x": pct(highs, lambda x: x >= 5),
                "day1_ge_10x": pct(highs, lambda x: x >= 10),
                "day1_ge_50x": pct(highs, lambda x: x >= 50),
                "vol_ge_10x_open": pct(vols, lambda x: x >= 10),
                "close_held_ge_5x": pct(closes, lambda x: x >= 5),
            },
            "pons_shape_hits": pons_shape,
        },
        "helius_alpha": helius_alpha,
        "by_cohort": {c: cohort_sum(c) for c in ("user_rh", "user_sol", "desk")},
        "items": items,
        "dust_excluded": [
            {
                "symbol": r["symbol"],
                "fdv_open": (r.get("first_day") or {}).get("fdv_open"),
                "multiple_high": (r.get("first_day") or {}).get("multiple_high"),
            }
            for r in dust
        ],
        "alpha_hypotheses": alpha_hypotheses,
        "strategy_candidates": strategy_candidates,
    }


def write_md(report: dict[str, Any]) -> None:
    a = report["all"]
    lines = [
        "# Zero-hour / first-day alpha (GMGN + Gecko + Helius)\n\n",
        f"Coverage: **{report['n_ok']}/{report['n']}** usable "
        f"(excluded {report['n_dust_open_excluded']} dust-open)\n\n",
        f"**Bitquery:** {report['bitquery']['note']}\n\n",
        f"- Median open FDV: **${a['median_fdv_open']}**\n",
        f"- Median day-1 high×: **{a['median_multiple_high']}** · close× **{a['median_multiple_close']}**\n",
        f"- Median vol/open FDV: **{a['median_vol_over_open']}×**\n",
        f"- Median hours → 2×/5×/10×: **{a['median_hours_to_2x']}** / "
        f"**{a['median_hours_to_5x']}** / **{a['median_hours_to_10x']}**\n",
        f"- PONS-shape hits: {', '.join(a['pons_shape_hits']) or '—'}\n\n",
        "## Helius first-hour (Sol)\n\n",
    ]
    ha = report.get("helius_alpha") or {}
    lines.append(f"Enriched: **{ha.get('n_enriched', 0)}** Sol tokens\n\n")
    for label in ("runners_ge_10x", "day1_lt_10x"):
        block = ha.get(label) or {}
        lines.append(
            f"- `{label}` n={block.get('n')} median_buyers={block.get('median_buyers')} "
            f"1m={block.get('median_buyers_1m')} 5m={block.get('median_buyers_5m')} "
            f"top10={block.get('median_top10_share')} sum_sol={block.get('median_sum_sol')} "
            f"syms={', '.join(block.get('symbols') or [])}\n"
        )
    lines.append("\n## Buckets\n\n")
    for k, v in (a.get("buckets") or {}).items():
        lines.append(f"- `{k}`: {v}\n")
    lines.append(
        "\n## Ranked by day-1 high×\n\n"
        "| Sym | Open FDV | High× | Close× | Vol/Open | t→5×h | Buyers1h | Src | Cohort |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    ok = [
        r
        for r in report["items"]
        if r.get("ok")
        and r.get("first_day")
        and (r.get("first_day") or {}).get("flag") != "dust-open"
    ]
    items = sorted(ok, key=lambda r: -float(r["first_day"]["multiple_high"]))
    for r in items:
        d = r["first_day"]
        t = r.get("time_to") or {}
        h = r.get("helius") or {}
        lines.append(
            f"| {r['symbol']} | ${d['fdv_open']} | {d['multiple_high']}× | "
            f"{d['multiple_close']}× | {d.get('vol_over_open_fdv')}× | "
            f"{t.get('to_5x_h')} | {h.get('n_unique_buyers', '—')} | "
            f"{r.get('source')} | {r.get('cohort')} |\n"
        )
    lines.append("\n## Strategy candidates\n\n")
    for s in report.get("strategy_candidates") or []:
        lines.append(f"- **{s['name']}**: {s['rule']} _(live={s['extractable_live']})_\n")
    MD.write_text("".join(lines))


async def main() -> int:
    if not OUT.exists():
        log(f"missing {OUT} — run scan_zero_hour_alpha.py first")
        return 1
    existing = json.loads(OUT.read_text())
    items = existing.get("items") or []
    log(f"loaded items={len(items)} ok={sum(1 for r in items if r.get('ok'))}")

    items = fill_gecko_missing(items)
    # Full Sol Helius pass (can take a few minutes)
    items = await enrich_helius(items)
    report = summarize(items)
    OUT.write_text(json.dumps(report, indent=2, default=str))
    HELIUS_OUT.write_text(
        json.dumps(
            {
                "bitquery": report["bitquery"],
                "helius_alpha": report["helius_alpha"],
                "alpha_hypotheses": report["alpha_hypotheses"],
                "strategy_candidates": report["strategy_candidates"],
                "sol": [
                    {
                        "symbol": r["symbol"],
                        "helius": r.get("helius"),
                        "first_day": r.get("first_day"),
                        "time_to": r.get("time_to"),
                    }
                    for r in items
                    if r.get("chain") == "sol"
                ],
            },
            indent=2,
            default=str,
        )
    )
    write_md(report)
    log(f"SUMMARY {json.dumps(report['all'], indent=2)}")
    log(f"HELIUS {json.dumps(report['helius_alpha'], indent=2)}")
    log(f"missing {[r['symbol'] for r in items if not r.get('ok')]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
