#!/usr/bin/env python3
"""Live scorecard for the Launch Finder research desk.

Proves (or flags) the four claims in .agents/skills/launchfinder-verify/SKILL.md:
first-book Entry, minute tape, Live health, paper gates. API-only. No extra GMGN.

Exit 1 on FAIL. WARN does not fail the process — it is scoring-loop material.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launchfinder.image_rev import IMAGE_REV
from launchfinder.scoring.live_fit import LIVE_PAPER_HI
from launchfinder.scoring.paper_gate import PAPER_NO_RUN_HOURS

DEFAULT_BASE = "https://new-launch-finder-production.up.railway.app"
STATE_PATH = Path("/tmp/paper-loop-state.json")
LOOP_STALE_S = 180
TAPE_STALE_S = 180
HUNT_THIN_CAP = 0.62
FILLABLE_LIQ = 5_000.0
FAT_LIQ = 8_000.0


class FetchError(RuntimeError):
    pass


def _get(base: str, path: str) -> dict:
    url = f"{base.rstrip('/')}{path}"
    try:
        with urllib.request.urlopen(url, timeout=45) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise FetchError(f"{path}: {exc}") from exc


def _f(row: dict, *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if row.get(key) is not None:
            try:
                return float(row.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
    return default


def _pattern(card: dict) -> list[str]:
    t0 = _f(card, "t0_mcap", "entry_mcap")
    last = _f(card, "last_mcap", "mcap")
    mx = _f(card, "max_mcap")
    flags = card.get("risk_flags") or card.get("flags") or []
    if isinstance(flags, str):
        flags = [flags]
    blob = " ".join(str(f).lower() for f in flags)
    hits: list[str] = []
    two = (
        t0 >= 20_000
        and last <= 1.15 * t0
        and mx >= 1.8 * t0
        and last < 0.65 * mx
        and not ("sniper" in blob and mx < 5 * t0)
    )
    if two and (card.get("chain") or "") in {"sol", "solana", ""}:
        hits.append("two-tick")
    if t0 >= 20_000 and 0 < last < 25_000 and last < 0.50 * t0:
        hits.append("collapsed")
    if t0 >= 500_000 and 0 < last < 25_000 and last < 0.05 * t0:
        hits.append("phantom")
    return hits


def _leak_line(card: dict) -> float:
    scorer = card.get("scorer") or "legacy"
    chain = (card.get("chain") or "sol").replace("solana", "sol")
    if scorer == "first_sight":
        return 0.14 if chain.startswith("sol") else 0.30
    return 0.90


class Scorecard:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, claim: str, detail: str) -> None:
        self.rows.append((status, claim, detail))

    def fail(self, claim: str, detail: str) -> None:
        self.add("FAIL", claim, detail)

    def warn(self, claim: str, detail: str) -> None:
        self.add("WARN", claim, detail)

    def ok(self, claim: str, detail: str) -> None:
        self.add("PASS", claim, detail)

    @property
    def failed(self) -> bool:
        return any(s == "FAIL" for s, _, _ in self.rows)

    def dump(self) -> dict:
        return {"rows": [{"status": s, "claim": c, "detail": d} for s, c, d in self.rows]}


def _check_health(base: str, card: Scorecard) -> dict:
    h = _get(base, "/health")
    rev = h.get("image_rev")
    if rev != IMAGE_REV:
        card.fail("deploy", f"/health.image_rev={rev} != {IMAGE_REV}")
    else:
        card.ok("deploy", f"image_rev {rev} role={h.get('role')}")
    if not h.get("ok"):
        card.fail("deploy", "health.ok is false")
    for name in ("helius", "bitquery", "gmgn", "hunt_tape", "paper"):
        if h.get(name) is not True:
            card.warn("tape", f"health.{name}={h.get(name)}")
    loops = h.get("loops") or {}
    tape = loops.get("hunt_tape") or {}
    note = str(tape.get("note") or "")
    age = float(tape.get("age_s") or 10**9)
    if "helius_capped" in note:
        card.warn("tape", f"hunt_tape helius_capped ({note})")
    if age > LOOP_STALE_S:
        card.fail("tape", f"hunt_tape age {age:.0f}s > {LOOP_STALE_S}s ({note})")
    else:
        card.ok("tape", f"hunt_tape {note} age={age:.0f}s")
    ingest = loops.get("ingest") or {}
    if float(ingest.get("age_s") or 10**9) > LOOP_STALE_S:
        card.warn("first-book", f"ingest stale {ingest}")
    return h


def _check_hunt(base: str, chain: str, card: Scorecard) -> list[dict]:
    payload = _get(base, f"/api/hunt?chain={chain}")
    items = payload.get("items") or []
    if len(items) < 10:
        card.fail("tape", f"hunt {chain} only {len(items)} cards")
        return items
    fs = [c for c in items if c.get("scorer") == "first_sight"]
    fillable = [c for c in items if _f(c, "last_liq", "liq") >= FILLABLE_LIQ]
    known = [c for c in fillable if int(c.get("holder_count") or 0) > 0]
    models = [c for c in items if c.get("live_model_p") is not None]
    thin62 = [c for c in items if c.get("live_cap") == HUNT_THIN_CAP]
    fat62 = [
        c
        for c in thin62
        if _f(c, "last_liq", "liq") >= 8_000
        and int(c.get("holder_count") or 0) not in range(1, 26)
    ]
    if chain == "rh" and fat62:
        card.fail("live", f"RH fat books capped Live 62: {[c.get('symbol') for c in fat62[:6]]}")
    elif chain == "rh":
        card.ok("live", f"RH Hunt {len(items)} fat-62={len(fat62)} thin-cap={len(thin62)}")
    if chain == "sol":
        if models and len(models) < 20:
            card.warn("live", f"Sol live_model_p set on {len(models)}/{len(items)}")
        elif models:
            vals = sorted({round(float(c.get("live_model_p") or 0), 4) for c in models})
            if len(vals) <= 3:
                card.warn("live", f"Sol live_model_p clustered {vals} on {len(models)} cards — wired, not separated")
            else:
                card.ok("live", f"Sol live_model_p distinct {len(vals)} values on {len(models)} cards")
        ceil = max((_f(c, "entry_p") for c in fs), default=0.0)
        if ceil >= 0.14:
            card.warn("first-book", f"Sol first-sight ceiling {ceil:.4f} reached buy line")
        else:
            card.ok("first-book", f"Sol first-sight ceiling {ceil:.4f} (buy line 0.14 silent)")
        if fillable and len(known) < max(1, len(fillable) // 4):
            card.warn("tape", f"Sol fillable holders {len(known)}/{len(fillable)}")
        else:
            card.ok("tape", f"Sol fillable holders {len(known)}/{len(fillable)}")
    leaks = []
    deadly = []
    for c in items:
        hits = _pattern(c)
        if hits and _f(c, "entry_p") >= _leak_line(c):
            row = f"{c.get('symbol')} {c.get('entry_p')} {hits}"
            leaks.append(row)
            if any(h in {"collapsed", "phantom"} for h in hits):
                deadly.append(row)
    if deadly:
        card.warn("first-book", f"{chain} buy-line collapsed/phantom (paper hold should skip): {deadly[:6]}")
    if leaks:
        card.warn(
            "first-book",
            f"{chain} hi-line pattern hits (coarse plateau, not a gate bug): {leaks[:8]}",
        )
    else:
        card.ok("first-book", f"{chain} no pattern leak at the buy line")
    dumps = [
        c
        for c in items
        if _f(c, "last_mcap") > 0
        and _f(c, "t0_mcap") > 0
        and _f(c, "last_mcap") / _f(c, "t0_mcap") < 0.50
        and _f(c, "conviction_p", "score") > 0.70
        and (c.get("live_model_p") is None or float(c.get("live_model_p") or 0) > 0.70)
    ]
    if dumps:
        card.fail("live", f"{chain} dumped books still Live-hot: {[c.get('symbol') for c in dumps[:6]]}")
    return items


def _pick_named(items: list[dict]) -> list[dict]:
    fat = [
        c
        for c in items
        if c.get("scorer") == "first_sight"
        and _f(c, "last_liq", "liq") >= FAT_LIQ
        and int(c.get("holder_count") or 0) not in range(1, 26)
        and c.get("mint")
    ]
    fat.sort(key=lambda c: _f(c, "entry_p"), reverse=True)
    dumped = [
        c
        for c in items
        if c.get("mint")
        and _f(c, "t0_mcap") >= 20_000
        and _f(c, "last_mcap") > 0
        and _f(c, "last_mcap") / _f(c, "t0_mcap") < 0.50
    ]
    dumped.sort(key=lambda c: _f(c, "last_mcap") / max(_f(c, "t0_mcap"), 1.0))
    picked: list[dict] = []
    for card in (*fat[:1], *dumped[:1]):
        if card not in picked:
            picked.append(card)
    return picked


def _bar_age_s(tape: dict) -> float | None:
    bars = tape.get("bars") or []
    if not bars:
        return None
    raw = str((bars[-1] or {}).get("t") or "")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds())


def _frozen_entry(detail: dict) -> tuple[float, str]:
    ledger = ((detail.get("ledger") or {}).get("entry") or {})
    frozen = _f(ledger, "entry_p")
    if frozen:
        return frozen, "ledger"
    return _f(detail, "entry_p", "p_good"), "token"


def _empty_tape_reason(card_row: dict) -> str | None:
    """Intentional empty tape — dead leftover, not a missing-minute-bar bug.

    Fat live leftovers (ROB / TYPING class: last + liq) must still have bars.
    """
    last = _f(card_row, "last_mcap")
    liq = _f(card_row, "last_liq", "liq")
    if last <= 0 and liq < 800:
        return "no live last/liq — leftover not taped"
    return None


def _check_named(base: str, card_row: dict, score: Scorecard) -> None:
    mint = str(card_row.get("mint") or "")
    symbol = card_row.get("symbol") or mint[:8]
    chain = card_row.get("chain") or "?"
    detail = _get(base, f"/api/tokens/{mint}")
    hunt_entry = _f(card_row, "entry_p")
    frozen, src = _frozen_entry(detail)
    if hunt_entry and frozen and abs(hunt_entry - frozen) > 0.03:
        score.warn(
            "first-book",
            f"{chain} {symbol} Hunt Entry {hunt_entry:.4f} != {src} {frozen:.4f}",
        )
    else:
        score.ok("first-book", f"{chain} {symbol} Hunt Entry {hunt_entry:.4f} matches {src}")
    hunt_scorer = card_row.get("scorer")
    ledger_scorer = ((detail.get("ledger") or {}).get("entry") or {}).get("scorer")
    if hunt_scorer and ledger_scorer and hunt_scorer != ledger_scorer:
        score.warn("first-book", f"{chain} {symbol} Hunt scorer {hunt_scorer} != ledger {ledger_scorer}")
    tape = _get(base, f"/api/tokens/{mint}/tape")
    bars = tape.get("bars") or []
    age = _bar_age_s(tape)
    if not bars:
        why = _empty_tape_reason(card_row)
        if why:
            score.warn("tape", f"{chain} {symbol} Hunt name has no tape bars ({why})")
            return
        score.fail("tape", f"{chain} {symbol} Hunt name has no tape bars")
        return
    if age is not None and age > TAPE_STALE_S:
        score.warn("tape", f"{chain} {symbol} last bar {age:.0f}s old ({len(bars)} bars)")
    else:
        score.ok("tape", f"{chain} {symbol} {len(bars)} bars age={age:.0f}s" if age is not None else f"{chain} {symbol} {len(bars)} bars")
    live = _f(card_row, "conviction_p", "score")
    if live > 1.0:
        live = live / 100.0
    model = card_row.get("live_model_p")
    dumped = _f(card_row, "t0_mcap") > 0 and _f(card_row, "last_mcap") / _f(card_row, "t0_mcap") < 0.50
    if dumped and live > 0.70:
        score.fail("live", f"{chain} {symbol} dumped but Live {live:.3f} (model={model})")
    elif dumped and model is not None and float(model) > 0.70 and live <= 0.40:
        score.ok("live", f"{chain} {symbol} dump: tape Live {live:.3f} beat model {float(model):.3f}")


def _check_paper(base: str, chain: str, card: Scorecard) -> dict:
    p = _get(base, f"/api/paper?gated=true&chain={chain}")
    lines = p.get("lines") or {}
    hi = float(lines.get("hi") or 0)
    want = 0.14 if chain == "sol" else 0.3
    if abs(hi - want) > 1e-6:
        card.fail("paper", f"{chain} paper hi={hi} want {want}")
    else:
        card.ok("paper", f"{chain} lines hi={hi} scorer={lines.get('scorer')}")
    opens = p.get("open") or []
    closed = p.get("closed") or []
    if chain == "sol":
        sneak = [o for o in opens if _f(o, "p_good", "entry_p") < 0.10]
        mid = [o for o in opens if 0.10 <= _f(o, "p_good", "entry_p") < 0.14]
        if sneak:
            card.fail("paper", f"Sol opens under watch line: {[o.get('symbol') for o in sneak]}")
        if mid:
            card.warn("paper", f"Sol watch-line opens (need Live≥{LIVE_PAPER_HI}): {[o.get('symbol') for o in mid]}")
        if opens:
            card.ok("paper", f"Sol paper open={len(opens)} closed={len(closed)}")
        else:
            card.ok("paper", "Sol paper 0 open (0.14 silent / Live below 0.50)")
        bought_dump = [
            o
            for o in opens
            if _f(o, "entry_mcap") >= 20_000
            and _f(o, "multiple") > 0
            and _f(o, "multiple") < 0.50
        ]
        stale_graves = []
        waiting_no_run = []
        now = datetime.now(timezone.utc)
        for o in bought_dump:
            peak = _f(o, "peak_multiple")
            opened = o.get("opened_at") or ""
            age_h = None
            try:
                age_h = (now - datetime.fromisoformat(opened.replace("Z", "+00:00"))).total_seconds() / 3600.0
            except (TypeError, ValueError):
                age_h = None
            if peak >= 1.5 or (age_h is not None and age_h >= PAPER_NO_RUN_HOURS):
                stale_graves.append(o)
            else:
                waiting_no_run.append(o)
        if stale_graves:
            card.fail("paper", f"Sol open fill already dumped: {[(o.get('symbol'), o.get('multiple'), o.get('peak_multiple')) for o in stale_graves[:6]]}")
        elif waiting_no_run:
            card.warn("paper", f"Sol open fill waiting no-run clock: {[(o.get('symbol'), o.get('multiple')) for o in waiting_no_run[:6]]}")
    else:
        sneak = [o for o in opens if _f(o, "p_good", "entry_p") < 0.25]
        mid = [o for o in opens if 0.25 <= _f(o, "p_good", "entry_p") < 0.30]
        if sneak:
            card.fail("paper", f"RH opens under watch line: {[o.get('symbol') for o in sneak]}")
        if mid:
            card.warn("paper", f"RH watch-line opens (need Live≥{LIVE_PAPER_HI}): {[o.get('symbol') for o in mid]}")
        if opens:
            card.ok("paper", f"RH paper open={len(opens)} closed={len(closed)}")
        else:
            card.ok("paper", "RH paper 0 open (0.30 silent / Live below 0.50)")
    dumps = [o for o in closed if (o.get("exit_reason") or "") == "live dump"]
    if dumps:
        card.ok("paper", f"{chain} live-dump exits: {[(o.get('symbol'), o.get('multiple'), o.get('peak_multiple')) for o in dumps[:6]]}")
        leaked = [o for o in dumps if _f(o, "peak_multiple") < 1.5]
        if leaked:
            card.fail("paper", f"{chain} live-dump without 1.5× peak: {[(o.get('symbol'), o.get('peak_multiple')) for o in leaked[:4]]}")
    no_runs = [o for o in closed if (o.get("exit_reason") or "") == "no run"]
    if no_runs:
        card.ok("paper", f"{chain} no-run exits: {[(o.get('symbol'), o.get('multiple'), o.get('peak_multiple')) for o in no_runs[:6]]}")
        leaked = [o for o in no_runs if _f(o, "peak_multiple") >= 1.5]
        if leaked:
            card.fail("paper", f"{chain} no-run on a 1.5×+ run: {[(o.get('symbol'), o.get('peak_multiple')) for o in leaked[:4]]}")
    stuck = [
        o
        for o in opens
        if _f(o, "peak_multiple") >= 1.5
        and _f(o, "multiple") > 0
        and _f(o, "multiple") < 0.50 * _f(o, "peak_multiple")
    ]
    if stuck:
        card.warn("paper", f"{chain} still-open dumped runs: {[(o.get('symbol'), o.get('multiple'), o.get('peak_multiple')) for o in stuck[:6]]}")
    return p


def _check_models(base: str, card: Scorecard) -> dict:
    out: dict = {}
    for chain in ("sol", "rh"):
        live = _get(base, f"/api/model/artifacts?kind=live&chain={chain}")
        fs = _get(base, f"/api/model/artifacts?kind=first_sight&chain={chain}")
        samples = live.get("samples") or {}
        out[chain] = {"live": live, "first_sight": fs}
        src = samples.get("resolved_by_source") or {}
        card.ok(
            "live",
            f"{chain} Live samples={samples.get('samples')} resolved={samples.get('resolved')} "
            f"pos={samples.get('positives')} src={src} promoted={live.get('promoted_version')}",
        )
        if chain == "sol" and not live.get("promoted_version"):
            card.warn("live", "Sol Live artifact not promoted — Hunt health is tape-only")
        arts = fs.get("artifacts") or []
        promo = next((a for a in arts if a.get("promoted")), None)
        if promo and int(promo.get("label_version") or 0) != 3:
            card.fail("first-book", f"{chain} first-sight label_version={promo.get('label_version')} want 3")
        elif promo:
            card.ok("first-book", f"{chain} first-sight v{fs.get('promoted_version')} label 3 auc={promo.get('auc')}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch Finder live desk scorecard")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--skip-spot", action="store_true", help="Skip named mint /tape checks")
    args = parser.parse_args()
    card = Scorecard()
    hunts: dict[str, list] = {}
    papers: dict[str, dict] = {}
    models: dict = {}
    health: dict = {}
    try:
        health = _check_health(args.base, card)
        hunts = {ch: _check_hunt(args.base, ch, card) for ch in ("sol", "rh")}
        papers = {ch: _check_paper(args.base, ch, card) for ch in ("sol", "rh")}
        models = _check_models(args.base, card)
        if not args.skip_spot:
            for ch, items in hunts.items():
                for named in _pick_named(items):
                    _check_named(args.base, named, card)
    except FetchError as exc:
        card.fail("deploy", f"API fetch failed: {exc}")
    print(f"verify_desk  image_rev_commit={IMAGE_REV}  live={health.get('image_rev')}")
    for status, claim, detail in card.rows:
        print(f"  [{status}] {claim:11} {detail}")
    summary = {
        "image_rev": health.get("image_rev"),
        "failed": card.failed,
        "scorecard": card.dump(),
        "hunt": {ch: len(hunts[ch]) for ch in hunts},
        "paper": {
            ch: {"open": len(papers[ch].get("open") or []), "closed": len(papers[ch].get("closed") or [])}
            for ch in papers
        },
        "live_samples": {
            ch: (models[ch]["live"].get("samples") if isinstance(models[ch]["live"], dict) else None) for ch in models
        },
    }
    print(json.dumps({"failed": card.failed, "n_rows": len(card.rows)}, indent=2))
    if args.write_state:
        path = Path(args.state)
        try:
            state = json.loads(path.read_text()) if path.exists() else {}
        except json.JSONDecodeError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        state["verify_desk"] = summary
        path.write_text(json.dumps(state, indent=2) + "\n")
    return 1 if card.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
