#!/usr/bin/env python3
"""Reconstruct first-day OHLCV for culture runners + desk adds. Research only.

Does not rewrite Decisions. FEATURE_NAMES stays 66. Uses GMGN 1d (+ 1h when
available) to mine open FDV / vol / time-to-multiple patterns.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from launchfinder.research.gmgn import gmgn_available, token_kline

OUT = Path("/opt/cursor/artifacts/zero_hour_alpha.json")
MD = Path("/opt/cursor/artifacts/zero_hour_alpha.md")

# User culture list + desk-relevant adds (mint, symbol, chain, cohort)
TOKENS: list[tuple[str, str, str, str]] = [
    # RH culture
    ("0x39dBED3a2bd333467115dE45665cC57F813C4571", "PONS", "robinhood", "user_rh"),
    ("0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18", "AI", "robinhood", "user_rh"),
    ("0x020bfC650A365f8BB26819deAAbF3E21291018b4", "CASHCAT", "robinhood", "user_rh"),
    ("0xAa07A0e9209e16aC99708C3EC70159c6eF3128A3", "ORBIO", "robinhood", "user_rh"),
    ("0x57c0e45cb534413d1c20a4240955d6bb250bb4f1", "UP", "robinhood", "user_rh"),
    ("0x98096d17e191b3da1d5f99a6d7b3584351b11e18", "BONER", "robinhood", "user_rh"),
    ("0x0e0d2c89a5a019fe1cf762e5e33187631dacc21b", "CHUMP", "robinhood", "user_rh"),
    ("0x385F4f8ae47651ce5F58F5265395a669f8281e18", "MEME", "robinhood", "user_rh"),
    ("0x5cb6f181081301b44905f3ae15419112ecabd8a6", "PIPEDOG", "robinhood", "user_rh"),
    ("0xe170dc96ca10103e0d4c5d9293c5a3a72ee365a5", "JOHN", "robinhood", "user_rh"),
    ("0x56910d4409f3a0c78c64dd8d0545ff0705389870", "INDEX", "robinhood", "user_rh"),
    ("0xe934e36a439c94017b64a3fece66af12099abf50", "STONKBROKER", "robinhood", "user_rh"),
    ("0x7fe995a80075df3dc8ae11a9b82c7fe4202cd87f", "HMM", "robinhood", "user_rh"),
    ("0xd9db30bb0d2b8d2eae3826a1372117e058791e18", "MOO", "robinhood", "user_rh"),
    ("0x45242320dbb855eea8fd36804c6487e10e97fcf9", "TENDIES", "robinhood", "user_rh"),
    ("0x812486eaea648819853f8e372dc9f1516c7868bd", "UBIK", "robinhood", "user_rh"),
    ("0x07ebb29a38fbcb41563817e5e19f2cec619c90d2", "BUN", "robinhood", "user_rh"),
    ("0x451b42a15100c340ca12f7c66de06fac5ea2d751", "BOW", "robinhood", "user_rh"),
    ("0x62c71cd34a52c30d894419cbcc55db2afa8032ea", "YOLO", "robinhood", "user_rh"),
    ("0x51250b135174ca09450ec01c4aff73cf69dbb590", "NOVAAI", "robinhood", "user_rh"),
    # Sol culture (skip PUMP platform)
    ("Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk", "USELESS", "sol", "user_sol"),
    ("6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx", "STONK", "sol", "user_sol"),
    ("9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "ANSEM", "sol", "user_sol"),
    ("Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump", "CATE", "sol", "user_sol"),
    ("HcRLc9VDgjLeK154xDawfb1dmVJ98DoSqcwTHGqiDeJR", "ZCAT", "sol", "user_sol"),
    ("Ce2gx9KGXJ6C9Mp5b5x1sn9Mg87JwEbrQby4Zqo3pump", "NEET", "sol", "user_sol"),
    ("a3W4qutoEJA4232T2gwZUfgYJTetr96pU4SJMwppump", "WHITEWHALE", "sol", "user_sol"),
    ("98kfF7rmsg1QDUEoCqNE7g7M1FdrTt92TEp2CLzypump", "PAID", "sol", "user_sol"),
    ("udPEzBjbGMzYZbuLtByqLYhc4TEs6EebG8R7fwdpump", "TEXTIT", "sol", "user_sol"),
    ("CTPoyCwkjMvoJwU4xvZZqoD8tiYk6yDchySiN5gGpump", "FONE", "sol", "user_sol"),
    ("y1AZt42vceCmStjW4zetK3VoNarC1VxJ5iDjpiupump", "FARTBOY", "sol", "user_sol"),
    ("HuAXPyDWDaMYFKuwQHpqL1oPnj93zdzWmtvFGzCeCUa7", "MASK", "sol", "user_sol"),
    ("DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP", "SI", "sol", "user_sol"),
    ("6UtY9iTZMQQ5QZVrbzFnNaJntV7oySm9k97mvwnuZcxr", "NEARKAT", "sol", "user_sol"),
    ("8RVBk8vxLiUHueLUW1f4izFVqN3nWippLhkohKg6EGkS", "KNOTS", "sol", "user_sol"),
    ("8RNUw4N655VSrZKuhGdywhbSMDTrheguFPfxbpE2NZHQ", "PURR", "sol", "user_sol"),
    ("4MMQY9bwkxxTtsK3W227Q5ABT6yFY8Pmn9Ze7wmAXKY8", "ALLINU", "sol", "user_sol"),
    ("HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ", "GP", "sol", "user_sol"),
    ("GTBxUiw6wJdmmkCGZgRHLyYxqu1vG4KtRpeox6yDpump", "JEANPHIL", "sol", "user_sol"),
    # Desk catches / early runners
    ("FzEh8uAxjUWRgGhzb4TGGBBsKUzsZn8zoSoZGmEppump", "DEX", "sol", "desk"),
    ("CWaGFU2xboUfSx3TN7WytRcHKvccFjvqPPK9LWWwpump", "MeiMei", "sol", "desk"),
    ("0x1615c7761f08be4b52db72ca461a24ac268a2415", "POOF", "robinhood", "desk"),
    ("0xe68c39030152f6a826eec03022e535d869fcbe77", "FRIES", "robinhood", "desk"),
    ("0x1ff5bf4854d900a8f00d888e4a7f9d941f5004d8", "VECTOR", "robinhood", "desk"),
    ("0xa176c6e4f24905d0988f9d98931be2c858d386e0", "DNA", "robinhood", "desk"),
    ("BnHqmki4tJf51kLMG4Pxvyrg4ZDcK9MghmRRNXWpump", "GTF", "sol", "desk"),
    ("4fYxyMNHxzxf4jvB5jCQiJxZGFrPymhnUTY6pEm1pump", "RISK", "sol", "desk"),
]

# Default meme supplies for FDV when price is raw token USD.
DEFAULT_SUPPLY = {
    "robinhood": 1_000_000_000.0,
    "sol": 1_000_000_000.0,
}


def _aware(ts: Any) -> datetime | None:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None


def first_day_from_daily(rows: list[dict[str, Any]], supply: float) -> dict[str, Any] | None:
    if not rows:
        return None
    d0 = rows[0]
    o = float(d0["open"] or 0)
    h = float(d0["high"] or 0)
    c = float(d0["close"] or 0)
    v = float(d0.get("volume") or 0)
    if o <= 0:
        return None
    fdv_open = o * supply
    fdv_high = h * supply
    fdv_close = c * supply
    day2 = rows[1] if len(rows) > 1 else None
    out = {
        "date": _aware(d0["time"]).date().isoformat() if _aware(d0["time"]) else None,
        "price_open": o,
        "price_high": h,
        "price_close": c,
        "fdv_open": round(fdv_open, 2),
        "fdv_high": round(fdv_high, 2),
        "fdv_close": round(fdv_close, 2),
        "multiple_high": round(h / o, 2),
        "multiple_close": round(c / o, 2),
        "volume_usd": round(v, 2),
        "vol_over_open_fdv": round(v / fdv_open, 2) if fdv_open > 0 else None,
        "n_days": len(rows),
    }
    if day2 and float(day2["open"] or 0) > 0:
        out["day2_high_mult_from_d0_open"] = round(float(day2["high"]) / o, 2)
        out["day2_close_mult_from_d0_open"] = round(float(day2["close"]) / o, 2)
    # Week-1 high from open
    week = rows[:7]
    if week:
        wh = max(float(r["high"] or 0) for r in week)
        out["week1_high_mult"] = round(wh / o, 2) if o else None
    return out


def time_to_mult(hourly: list[dict[str, Any]], *, mult: float) -> float | None:
    """Hours from first hour bar open until high clears mult× open."""
    if not hourly:
        return None
    o0 = float(hourly[0]["open"] or 0)
    t0 = _aware(hourly[0]["time"])
    if o0 <= 0 or t0 is None:
        return None
    target = o0 * mult
    for row in hourly:
        if float(row.get("high") or 0) >= target:
            ts = _aware(row["time"])
            if ts is None:
                continue
            return round((ts - t0).total_seconds() / 3600.0, 2)
    return None


async def enrich_one(mint: str, symbol: str, chain: str, cohort: str) -> dict[str, Any]:
    supply = DEFAULT_SUPPLY.get(chain, 1_000_000_000.0)
    daily = await token_kline(mint, chain=chain, resolution="1d")
    day = first_day_from_daily(daily, supply)
    hourly: list[dict[str, Any]] = []
    ttm: dict[str, Any] = {}
    if day and day.get("date"):
        start = datetime.fromisoformat(day["date"]).replace(tzinfo=timezone.utc)
        end = start + timedelta(hours=36)
        hourly = await token_kline(mint, chain=chain, resolution="1h", from_ts=start, to_ts=end)
        if not hourly:
            # Fallback: take first 36 bars of unbounded 1h if API ignores range.
            all_h = await token_kline(mint, chain=chain, resolution="1h")
            if all_h:
                hourly = all_h[:36]
        if hourly:
            ttm = {
                "to_2x_h": time_to_mult(hourly, mult=2.0),
                "to_5x_h": time_to_mult(hourly, mult=5.0),
                "to_10x_h": time_to_mult(hourly, mult=10.0),
                "to_50x_h": time_to_mult(hourly, mult=50.0),
                "n_hourly": len(hourly),
            }
    return {
        "symbol": symbol,
        "mint": mint,
        "chain": chain,
        "cohort": cohort,
        "supply_assumed": supply,
        "first_day": day,
        "time_to": ttm,
        "ok": bool(day),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("ok") and r.get("first_day")]
    if not ok:
        return {"n_ok": 0}

    def vals(key: str) -> list[float]:
        out = []
        for r in ok:
            v = (r.get("first_day") or {}).get(key)
            if v is not None:
                out.append(float(v))
        return out

    def med(xs: list[float]) -> float | None:
        return round(statistics.median(xs), 3) if xs else None

    def pct(xs: list[float], pred) -> float | None:
        return round(sum(1 for x in xs if pred(x)) / len(xs), 3) if xs else None

    opens = vals("fdv_open")
    highs = vals("multiple_high")
    vols = vals("vol_over_open_fdv")
    closes = vals("multiple_close")

    buckets = {
        "open_lt_10k": pct(opens, lambda x: x < 10_000),
        "open_10k_100k": pct(opens, lambda x: 10_000 <= x < 100_000),
        "open_100k_1m": pct(opens, lambda x: 100_000 <= x < 1_000_000),
        "open_ge_1m": pct(opens, lambda x: x >= 1_000_000),
        "day1_ge_5x": pct(highs, lambda x: x >= 5),
        "day1_ge_10x": pct(highs, lambda x: x >= 10),
        "day1_ge_50x": pct(highs, lambda x: x >= 50),
        "vol_ge_10x_open": pct(vols, lambda x: x >= 10),
        "vol_ge_50x_open": pct(vols, lambda x: x >= 50),
        "close_held_ge_5x": pct(closes, lambda x: x >= 5),
    }

    ttm2 = [float(r["time_to"]["to_2x_h"]) for r in ok if (r.get("time_to") or {}).get("to_2x_h") is not None]
    ttm5 = [float(r["time_to"]["to_5x_h"]) for r in ok if (r.get("time_to") or {}).get("to_5x_h") is not None]
    ttm10 = [float(r["time_to"]["to_10x_h"]) for r in ok if (r.get("time_to") or {}).get("to_10x_h") is not None]

    # Alpha shape: low open FDV + high vol/open + fast 5×
    shaped = []
    for r in ok:
        d = r["first_day"]
        if (
            float(d.get("fdv_open") or 0) < 100_000
            and float(d.get("vol_over_open_fdv") or 0) >= 10
            and float(d.get("multiple_high") or 0) >= 10
        ):
            shaped.append(r["symbol"])

    return {
        "n_ok": len(ok),
        "median_fdv_open": med(opens),
        "median_multiple_high": med(highs),
        "median_multiple_close": med(closes),
        "median_vol_over_open": med(vols),
        "median_hours_to_2x": med(ttm2),
        "median_hours_to_5x": med(ttm5),
        "median_hours_to_10x": med(ttm10),
        "buckets": buckets,
        "pons_shape_hits": shaped,
    }


async def main() -> None:
    if not gmgn_available():
        raise SystemExit("GMGN unavailable")
    rows: list[dict[str, Any]] = []
    for i, (mint, sym, chain, cohort) in enumerate(TOKENS):
        t0 = time.monotonic()
        try:
            row = await enrich_one(mint, sym, chain, cohort)
        except Exception as exc:  # noqa: BLE001
            row = {"symbol": sym, "mint": mint, "chain": chain, "cohort": cohort, "ok": False, "error": str(exc)}
        rows.append(row)
        d = row.get("first_day") or {}
        print(
            f"{i+1}/{len(TOKENS)} {sym:12} ok={row.get('ok')} "
            f"open={d.get('fdv_open')} high×={d.get('multiple_high')} "
            f"vol/open={d.get('vol_over_open_fdv')} t5={ (row.get('time_to') or {}).get('to_5x_h') } "
            f"in {time.monotonic()-t0:.1f}s"
        )

    by_cohort: dict[str, list] = {}
    for r in rows:
        by_cohort.setdefault(r.get("cohort") or "?", []).append(r)

    report = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "source": "gmgn_kline_1d(+1h)",
        "n": len(rows),
        "n_ok": sum(1 for r in rows if r.get("ok")),
        "all": summarize(rows),
        "by_cohort": {k: summarize(v) for k, v in by_cohort.items()},
        "items": rows,
        "alpha_hypotheses": [
            "Open FDV mostly <$100k (often <$10k) — chase leftovers >$1M is anti-alpha.",
            "Day-1 vol/open_fdv often >>10× (PONS ~220×) — require real volume, not wick MC.",
            "Time-to-5× often inside a few hours — Live/midday promote matters more than 23:00 lock alone.",
            "Close holding ≥5× separates real first days from two-tick wicks.",
            "Candle alpha is tape shape only; pair with frozen first-sight features when we saw the book.",
        ],
        "strategy_candidates": [
            {
                "name": "First-day tape gate (paper shadow)",
                "idea": "Shadow-score names whose first 1–3h print vol/open and multiple path match pons_shape; never rewrite Entry.",
                "uses": ["gmgn_kline", "hunt tape bars"],
                "risk": "Survivorship if only winners labeled; need same-day misses.",
            },
            {
                "name": "Early-wallet / FOMO overlap at entry",
                "idea": "Already partially built (early_wallets, fomo_wallets, wallet_score). Promote paperV1 when ≥N tracked early wallets buy in first hour.",
                "uses": ["early_wallets", "fomo_wallets", "wallet_score"],
                "risk": "Router/venues pollute maps; keep sized-sol + skip lists.",
            },
            {
                "name": "Meme/name_quality soft path without github",
                "idea": "Culture survivors are meme-tagged, github≈0. Soft thesis should fire on meme+organic_book, not only github/cto.",
                "uses": ["paper_v1 thesis weights", "name_quality", "organic_book"],
                "risk": "More noise; use shadow book + hit2× gate before promoting weight.",
            },
            {
                "name": "Time-to-2× Live promote",
                "idea": "Extend peak-2× promote with hour-clock: if queued and tape hits 2× within N hours with sellable liq, take slot.",
                "uses": ["promote_paper_v1_queue"],
                "risk": "Already shipping peak-2×; tune N and liq floor only.",
            },
            {
                "name": "Pre-grad Sol bonding watch",
                "idea": "Many Sol majors graduate into our $69k floor late. Watch Pump near-complete trenches earlier (already /api bloom/watch).",
                "uses": ["gmgn trenches", "preview_score"],
                "risk": "Firehose; keep preview side-weight, not FEATURE_NAMES growth.",
            },
            {
                "name": "Loser cohort / same-day baseline",
                "idea": "For every reconstructed winner day, sample desk misses that UTC day — required for any claim of alpha.",
                "uses": ["ledger decisions", "weekly scorecard"],
                "risk": "Historical days before desk existence have no loser cohort.",
            },
        ],
    }

    OUT.write_text(json.dumps(report, indent=2, default=str))
    _write_md(report)
    print("wrote", OUT, MD)


def _write_md(report: dict[str, Any]) -> None:
    lines = [
        "# Zero-hour / first-day alpha scan\n\n",
        f"Source: `{report['source']}` · asof {report['asof']}\n\n",
        f"Coverage: **{report['n_ok']}/{report['n']}** with GMGN daily kline\n\n",
    ]
    all_s = report.get("all") or {}
    lines.append("## Aggregate (reconstructed first day)\n\n")
    lines.append(f"- Median open FDV: **${all_s.get('median_fdv_open')}**\n")
    lines.append(f"- Median day-1 high multiple: **{all_s.get('median_multiple_high')}×**\n")
    lines.append(f"- Median day-1 close multiple: **{all_s.get('median_multiple_close')}×**\n")
    lines.append(f"- Median vol / open FDV: **{all_s.get('median_vol_over_open')}×**\n")
    lines.append(f"- Median hours to 2× / 5× / 10×: **{all_s.get('median_hours_to_2x')}** / **{all_s.get('median_hours_to_5x')}** / **{all_s.get('median_hours_to_10x')}**\n")
    lines.append(f"- PONS-shape hits (open<$100k · vol/open≥10 · high≥10×): {', '.join(all_s.get('pons_shape_hits') or []) or '—'}\n\n")
    buckets = all_s.get("buckets") or {}
    lines.append("### Buckets\n\n")
    for k, v in buckets.items():
        lines.append(f"- `{k}`: {v}\n")
    lines.append("\n## Per-token first day\n\n")
    lines.append("| Sym | Chain | Open FDV | High× | Close× | Vol/Open | t→5× h | Cohort |\n|---|---|---|---|---|---|---|---|\n")
    items = sorted(
        [r for r in report.get("items") or [] if r.get("ok")],
        key=lambda r: -float((r.get("first_day") or {}).get("multiple_high") or 0),
    )
    for r in items:
        d = r["first_day"]
        t = r.get("time_to") or {}
        lines.append(
            f"| {r['symbol']} | {r['chain']} | ${d.get('fdv_open')} | {d.get('multiple_high')}× | "
            f"{d.get('multiple_close')}× | {d.get('vol_over_open_fdv')}× | {t.get('to_5x_h')} | {r.get('cohort')} |\n"
        )
    miss = [r for r in report.get("items") or [] if not r.get("ok")]
    if miss:
        lines.append("\n## Missing kline\n\n")
        for r in miss:
            lines.append(f"- {r.get('symbol')} ({r.get('chain')}) {r.get('error') or 'empty'}\n")
    lines.append("\n## Alpha hypotheses\n\n")
    for h in report.get("alpha_hypotheses") or []:
        lines.append(f"- {h}\n")
    lines.append("\n## Strategy candidates (for 1–5/day goal)\n\n")
    for s in report.get("strategy_candidates") or []:
        lines.append(f"### {s['name']}\n\n{s['idea']}\n\n- Uses: {', '.join(s.get('uses') or [])}\n- Risk: {s.get('risk')}\n\n")
    MD.write_text("".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
