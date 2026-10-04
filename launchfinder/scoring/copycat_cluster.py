"""Copycat cluster path.

A same-symbol mint is a row, not a skip. The cluster is the dataset.
This module does not book, does not arm, and does not read or set
COPYCAT_LEARN_ARMED. The buy path must not import it.

Cluster: one chain, one normalized symbol, opened by the first mint
we see, closed 24 hours later. ticker_birth_n is assigned when the
mint is first seen and is never rebuilt after one of them runs.

Clock features are only what is known 60 seconds after that mint's
migrate. Later clone count and later FOMO rank are not features.

Label is the path after the clock. The 5x / $5,000 gate is a column,
not the training label. The score has to beat two dumb rules on a
time split before it can enter a short list: skip every copycat, and
buy the first of the name.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

CLUSTER_WINDOW = timedelta(hours=24)
DECISION_LAG = timedelta(seconds=60)
LABEL_MARKS = (timedelta(minutes=15), timedelta(hours=1))
PRODUCTION_MULTIPLE = 5.0
PRODUCTION_LIQ = 5_000.0

# Capture is not an arm. There is no flag that lifts a ticket.
CAPTURES = True
ENTERS_BUY_PATH = False


def normalize_symbol(symbol: str | None) -> str:
    """Cluster key symbol. Case, punctuation, and emoji do not split a name."""
    raw = unicodedata.normalize("NFKC", str(symbol or ""))
    raw = raw.casefold().strip()
    raw = re.sub(r"[^\w]+", "", raw, flags=re.UNICODE)
    return raw


def cluster_key(chain: str, symbol: str | None) -> str | None:
    name = normalize_symbol(symbol)
    if not name:
        return None
    return f"{str(chain or '').strip().lower()}:{name}"


def _as_utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def assign_birth(
    earlier: Iterable[dict[str, Any]],
    *,
    chain: str,
    symbol: str | None,
    mint: str,
    seen_at: datetime,
    creator: str | None = None,
    funder: str | None = None,
) -> dict[str, Any]:
    """Birth order from mints already seen. Does not look at the future.

    ``earlier`` rows need seen_at, mint, and optionally creator, funder,
    open_price. Rows outside the 24h window, or this mint, are ignored.
    """
    key = cluster_key(chain, symbol)
    seen = _as_utc(seen_at)
    members: list[dict[str, Any]] = []
    if key:
        for row in earlier:
            if not isinstance(row, dict):
                continue
            if str(row.get("mint") or "") == mint:
                continue
            if cluster_key(row.get("chain") or chain, row.get("symbol")) != key:
                continue
            row_at = row.get("seen_at")
            if not isinstance(row_at, datetime):
                continue
            row_at = _as_utc(row_at)
            if row_at > seen or seen - row_at > CLUSTER_WINDOW:
                continue
            members.append(row)
    members.sort(key=lambda row: _as_utc(row["seen_at"]))
    first = members[0] if members else None
    first_at = _as_utc(first["seen_at"]) if first else seen
    same_creator = bool(
        creator and first and creator == first.get("creator") and first.get("creator")
    )
    same_funder = bool(
        funder and any(funder == row.get("funder") and row.get("funder") for row in members)
    )
    return {
        "cluster_key": key,
        "ticker_birth_n": len(members) + 1,
        "seconds_since_first": int((seen - first_at).total_seconds()),
        "same_creator": same_creator,
        "same_funder": same_funder,
        "first_mint": None if first is None else first.get("mint"),
        "first_still_above_open": None if first is None else first.get("above_open"),
        "window_closes_at": (first_at + CLUSTER_WINDOW).isoformat(),
    }


def freeze_clock(
    birth: dict[str, Any],
    book: dict[str, Any] | None,
    *,
    migrated_at: datetime,
) -> dict[str, Any]:
    """Features allowed at 60 seconds after migrate. Missing book stays null."""
    book = book if isinstance(book, dict) else {}
    clock = _as_utc(migrated_at) + DECISION_LAG
    frozen = {
        "decision_at": clock.isoformat(),
        "ticker_birth_n": birth.get("ticker_birth_n"),
        "seconds_since_first": birth.get("seconds_since_first"),
        "same_creator": birth.get("same_creator"),
        "same_funder": birth.get("same_funder"),
        "first_still_above_open": birth.get("first_still_above_open"),
        "top10_pct": book.get("top10_pct"),
        "bundler_pct": book.get("bundler_pct"),
        "rat_pct": book.get("rat_pct"),
        "dev_sold": book.get("dev_sold"),
        "holders": book.get("holders"),
        "liq": book.get("liq"),
        "book_missing": book.get("holders") is None,
    }
    return frozen


def hard_stops(
    *,
    liq: float | None,
    mint_authority_live: bool = False,
    freeze_authority_live: bool = False,
    dev_sold: bool = False,
) -> list[str]:
    """Stops shared with every mint. A name match is not one of them."""
    stops: list[str] = []
    if liq is None or float(liq) <= 0:
        stops.append("no_sellable_liq")
    if mint_authority_live:
        stops.append("mint_authority")
    if freeze_authority_live:
        stops.append("freeze_authority")
    if dev_sold:
        stops.append("dev_sold")
    return stops


def label_path(
    *,
    decision_price: float | None,
    price_15m: float | None,
    price_1h: float | None,
    worst_drawdown: float | None,
    liq_1h: float | None,
) -> dict[str, Any]:
    """Path label. Production gate is recorded and is not the class."""
    def _ret(price: float | None) -> float | None:
        if not decision_price or price is None or decision_price <= 0:
            return None
        return float(price) / float(decision_price) - 1.0

    ret_1h = _ret(price_1h)
    multiple = None if ret_1h is None else ret_1h + 1.0
    liq_left = liq_1h is not None and float(liq_1h) >= PRODUCTION_LIQ
    return {
        "ret_15m": _ret(price_15m),
        "ret_1h": ret_1h,
        "worst_drawdown": worst_drawdown,
        "liq_still_there": liq_1h is not None and float(liq_1h) > 0,
        "production_gate": bool(
            multiple is not None and multiple >= PRODUCTION_MULTIPLE and liq_left
        ),
    }


def cluster_winner(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """After the window closes: which birth order had the best multiple.

    This is a label for the cluster. It is not a feature for a live mint.
    """
    best: dict[str, Any] | None = None
    best_mult = -1.0
    for row in rows:
        label = row.get("label") if isinstance(row.get("label"), dict) else {}
        ret = label.get("ret_1h")
        if ret is None:
            continue
        mult = float(ret) + 1.0
        if mult > best_mult:
            best_mult = mult
            best = {
                "mint": row.get("mint"),
                "ticker_birth_n": row.get("ticker_birth_n"),
                "multiple_1h": mult,
            }
    return best


def beats_dumb_rules(
    *,
    model_precision: float | None,
    skip_all_precision: float | None,
    buy_first_precision: float | None,
) -> bool:
    """True only when the score beats skip-all and buy-the-first."""
    if model_precision is None:
        return False
    others = [p for p in (skip_all_precision, buy_first_precision) if p is not None]
    if len(others) < 2:
        return False
    return float(model_precision) > max(others)
