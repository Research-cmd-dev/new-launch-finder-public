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
TIME_SPLIT = 0.70

# Capture is not an arm. There is no flag that lifts a ticket.
CAPTURES = True
ENTERS_BUY_PATH = False

FROZEN_FEATURE_KEYS = (
    "ticker_birth_n",
    "seconds_since_first",
    "same_creator",
    "same_funder",
    "first_still_above_open",
    "top10_pct",
    "bundler_pct",
    "rat_pct",
    "dev_sold",
    "holders",
    "liq",
)


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


def frozen_row(
    *,
    chain: str,
    symbol: str | None,
    mint: str,
    seen_at: datetime,
    migrated_at: datetime,
    earlier: Iterable[dict[str, Any]],
    book: dict[str, Any] | None = None,
    creator: str | None = None,
    funder: str | None = None,
    mint_authority_live: bool = False,
    freeze_authority_live: bool = False,
    decision_price: float | None = None,
) -> dict[str, Any]:
    """One capture row. Join to paper_fills on chain+mint. Does not book."""
    birth = assign_birth(
        earlier,
        chain=chain,
        symbol=symbol,
        mint=mint,
        seen_at=seen_at,
        creator=creator,
        funder=funder,
    )
    frozen = freeze_clock(birth, book, migrated_at=migrated_at)
    stops = hard_stops(
        liq=frozen.get("liq"),
        mint_authority_live=mint_authority_live,
        freeze_authority_live=freeze_authority_live,
        dev_sold=bool(frozen.get("dev_sold")),
    )
    return {
        "chain": str(chain or "").strip().lower(),
        "symbol": symbol or "",
        "mint": mint,
        "cluster_key": birth.get("cluster_key"),
        "first_mint": birth.get("first_mint"),
        "window_closes_at": birth.get("window_closes_at"),
        "decision_price": decision_price,
        "hard_stops": stops,
        "label": None,
        "paper_only": True,
        "enters_buy_path": ENTERS_BUY_PATH,
        **frozen,
    }


def attach_label(row: dict[str, Any], **label_kwargs: Any) -> dict[str, Any]:
    """Write the path label onto a frozen row. Features are not rewritten."""
    out = dict(row)
    out["label"] = label_path(**label_kwargs)
    return out


def _decision_at(row: dict[str, Any]) -> datetime:
    raw = row.get("decision_at")
    if isinstance(raw, datetime):
        return _as_utc(raw)
    if isinstance(raw, str) and raw:
        return _as_utc(datetime.fromisoformat(raw))
    return datetime.min.replace(tzinfo=timezone.utc)


def _gate(row: dict[str, Any]) -> bool:
    label = row.get("label") if isinstance(row.get("label"), dict) else {}
    return bool(label.get("production_gate"))


def precision_of(rows: Iterable[dict[str, Any]]) -> float | None:
    taken = list(rows)
    if not taken:
        return 0.0
    return sum(1 for row in taken if _gate(row)) / len(taken)


def buy_first(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if int(row.get("ticker_birth_n") or 0) == 1 and not row.get("hard_stops")]


def candidate_score(row: dict[str, Any]) -> float:
    """Forward-only score. Birth order is a feature, not a veto.

    Later births can outrank the first if the book at +60s is cleaner.
    Hard stops are excluded before scoring. This is a paper ranker, not a ticket.
    """
    if row.get("hard_stops"):
        return -1.0
    birth = int(row.get("ticker_birth_n") or 1)
    holders = float(row.get("holders") or 0.0)
    liq = float(row.get("liq") or 0.0)
    top10 = row.get("top10_pct")
    top10_pen = 0.0 if top10 is None else max(0.0, float(top10) - 40.0) / 100.0
    bundler = float(row.get("bundler_pct") or 0.0) / 100.0
    same_funder = 0.35 if row.get("same_funder") else 0.0
    # Ordinal 4 is allowed. Only the book and funder identity move the score.
    return (
        min(holders, 80.0) / 80.0
        + min(liq, 20_000.0) / 20_000.0
        - top10_pen
        - bundler
        - same_funder
        - 0.02 * max(0, birth - 1)
    )


def select_score(rows: Iterable[dict[str, Any]], *, min_score: float) -> list[dict[str, Any]]:
    return [row for row in rows if candidate_score(row) >= min_score]


def time_split(rows: Iterable[dict[str, Any]], *, frac: float = TIME_SPLIT) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(rows, key=_decision_at)
    if not ordered:
        return [], []
    cut = max(1, int(len(ordered) * frac))
    if cut >= len(ordered):
        cut = len(ordered) - 1
    return ordered[:cut], ordered[cut:]


def evaluate_time_split(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Holdout precision vs skip-all and buy-first. Does not arm."""
    train, test = time_split(rows)
    # Threshold is fit on train only: best score cut by train precision, floored at 0.
    scores = sorted({round(candidate_score(row), 4) for row in train if candidate_score(row) >= 0})
    best_cut = 0.0
    best_prec = -1.0
    for cut in scores or [0.0]:
        prec = precision_of(select_score(train, min_score=cut))
        if prec is not None and prec >= best_prec:
            best_prec = prec
            best_cut = cut
    model_rows = select_score(test, min_score=best_cut)
    first_rows = buy_first(test)
    model_p = precision_of(model_rows)
    skip_p = 0.0
    first_p = precision_of(first_rows)
    return {
        "paper_only": True,
        "enters_buy_path": False,
        "n_train": len(train),
        "n_test": len(test),
        "threshold": best_cut,
        "model_precision": model_p,
        "skip_all_precision": skip_p,
        "buy_first_precision": first_p,
        "n_model": len(model_rows),
        "n_buy_first": len(first_rows),
        "beats_dumb_rules": beats_dumb_rules(
            model_precision=model_p,
            skip_all_precision=skip_p,
            buy_first_precision=first_p,
        ),
        "promote": False,
    }
