"""Desk lines: the two Entry thresholds the desk acts on, per scorer.

The legacy blend was over-confident, so its lines sat at 0.70 / 0.90 and
still hit 2x under 20% of the time. The first-sight model is calibrated:
its Entry *is* the forward probability of a sellable 2x within 24h, and on
the honest Sol board that tops out near 0.67. Fixed 0.70 / 0.90 lines
would make Hunt "90+", the paper ledger, tickets and alerts inert there.

So the lines belong to the scorer that wrote the Entry, not to the desk:

* ``legacy``       0.70 / 0.90 — unchanged.
* ``first_sight``  Sol 0.10 / 0.14 — watch / buy, stacked on the honest
  plateau (v86). The v80 0.15 / 0.30 bar never fired: calibrated p tops
  out near 0.15, live Hunt ceiling ~0.1465, top decile ~12% 2x against a
  6% base. v86 buys that plateau on sight (dial back by raising hi) and
  lets watch 0.10–0.14 fill when Live >= LIVE_PAPER_HI (0.50). History:
  v76-v79 sat at 0.30 / 0.50 on graduation / lifetime-ATH numbers; v80-v85
  sat at 0.15 / 0.30 with Sol paper closed until Live carried it.
* ``first_sight``  Robinhood 0.25 / 0.30 — take the v80 >=0.30 bucket
  (live artifact: ~24/day at 42% 2x / 14% 5x) instead of only >=0.40
  (~13/day at 49%). Watch 0.25 marks the next step down. Dial back by
  raising hi.

Every Decision / HuntCard / Research row carries ``scorer`` so a frozen
Entry is always read against the lines that were in force when it was
written; the honest board never compares a 0.48 first-sight print to a
0.90 legacy line. First-sight lines are per chain, so callers pass the
chain whenever they have one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .chains import normalize_chain

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SCORER_LEGACY = "legacy"
SCORER_FIRST_SIGHT = "first_sight"


@dataclass(frozen=True)
class DeskLines:
    scorer: str
    lo: float
    hi: float
    # Below this Entry the card is a thin-watch book: Hunt Live is capped.
    thin: float

    @property
    def lo_label(self) -> str:
        return f"{round(self.lo * 100):d}"

    @property
    def hi_label(self) -> str:
        return f"{round(self.hi * 100):d}"

    def as_dict(self) -> dict[str, float | str]:
        return {"scorer": self.scorer, "lo": self.lo, "hi": self.hi, "thin": self.thin}


LEGACY_LINES = DeskLines(SCORER_LEGACY, 0.70, 0.90, 0.20)
FIRST_SIGHT_LINES = DeskLines(SCORER_FIRST_SIGHT, 0.10, 0.14, 0.05)
FIRST_SIGHT_RH_LINES = DeskLines(SCORER_FIRST_SIGHT, 0.25, 0.30, 0.05)

_BY_SCORER = {SCORER_LEGACY: LEGACY_LINES, SCORER_FIRST_SIGHT: FIRST_SIGHT_LINES}
_BY_SCORER_CHAIN = {(SCORER_FIRST_SIGHT, "robinhood"): FIRST_SIGHT_RH_LINES}


def lines_for_scorer(scorer: str | None, chain: str | None = None) -> DeskLines:
    """Lines for a frozen Entry, from the scorer stamped next to it and the
    chain it was written on (first-sight lines differ per chain)."""
    key = str(scorer or SCORER_LEGACY)
    if chain:
        hit = _BY_SCORER_CHAIN.get((key, normalize_chain(chain)))
        if hit is not None:
            return hit
    return _BY_SCORER.get(key, LEGACY_LINES)


def legacy_equivalent(p: float, lines: DeskLines) -> float:
    """Map an Entry on ``lines``' scale onto the legacy 0.70 / 0.90 scale,
    piecewise-linear through the two lines (0 -> 0, lo -> 0.70, hi -> 0.90,
    1 -> 1). For code that still thinks in legacy thresholds — runner tiers,
    Bloom's "weak entry" bonus and alert delta — so a first-sight 0.45 on RH
    (above its high line) reads as a strong Entry, not a weak one. Identity
    for legacy lines."""
    p = min(1.0, max(0.0, float(p or 0.0)))
    if lines.scorer == SCORER_LEGACY or lines.hi <= lines.lo or lines.lo <= 0:
        return p
    lo_out, hi_out = LEGACY_LINES.lo, LEGACY_LINES.hi
    if p <= lines.lo:
        return round(p / lines.lo * lo_out, 4)
    if p <= lines.hi:
        return round(lo_out + (p - lines.lo) / (lines.hi - lines.lo) * (hi_out - lo_out), 4)
    return round(hi_out + (p - lines.hi) / (1.0 - lines.hi) * (1.0 - hi_out), 4)


def scorer_for(scored: dict | None) -> str:
    """What the research pipeline just wrote as Entry."""
    scored = scored or {}
    if scored.get("awaiting_fill"):
        return SCORER_LEGACY
    return SCORER_FIRST_SIGHT if scored.get("first_sight_p") is not None else SCORER_LEGACY


def current_scorer(session: Session, chain: str) -> str:
    """Scorer the pipeline will use for the next launch on ``chain``."""
    chain = normalize_chain(chain)
    from .scoring.first_sight import FIRST_SIGHT_CHAINS, promoted_artifact

    if chain in FIRST_SIGHT_CHAINS and promoted_artifact(session, chain) is not None:
        return SCORER_FIRST_SIGHT
    return SCORER_LEGACY


def desk_lines(session: Session, chain: str) -> DeskLines:
    """Lines in force for new Entries on ``chain`` right now."""
    return lines_for_scorer(current_scorer(session, chain), chain)
