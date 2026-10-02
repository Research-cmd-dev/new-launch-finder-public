"""Paper-only Learn grain for copycat hard skips (www FALSE-VETO).

Keeps the copycat hard veto on every ticket path. Existing
``paper_v1_shadow`` ``v1 veto|copycat`` labels stay. This module only
adds a would-have side-key when FOMO top-band + Hunt + honest floors
pass, and seeds www as miss-cohort evidence row #1.

Side keys only — does not grow FEATURE_NAMES. Never arms live.
"""

from __future__ import annotations

from typing import Any, Iterable

from .paper_gate import PAPER_CONFIRM_LIQ, PAPER_FRESH_HOLDERS, paper_hard_veto
from .paper_v1 import PAPER_V1_COPYCAT_VETO_STAMP, v1_thesis_ok

# Live arm stays off for this grain forever.
COPYCAT_LEARN_ARMED = False
FOMO_TOP_BAND = 3
GATE_VETO_COPYCAT = PAPER_V1_COPYCAT_VETO_STAMP  # "copycat"
GATE_VETO_KEY = "gate_veto"
WOULD_HAVE_KEY = "would_have"
FOMO_RANK_KEY = "fomo_rank"

WWW_MINT = "GAwhcphCqCv5bKHmCiN4VDdNWfbXJL4npmkc8L3Q9S9H"
WWW_EVIDENCE: dict[str, Any] = {
    "id": "www-false-veto",
    "symbol": "www",
    "name": "world wide web",
    "mint": WWW_MINT,
    "chain": "sol",
    "label": "FALSE-VETO",
    "gate_veto": GATE_VETO_COPYCAT,
    "entry_p": 0.2712,
    "entry_mcap": 204_000,
    "multiple": 22.0,
    "multiple_lo": 20.0,
    "multiple_hi": 24.0,
    "seeded": True,
    "would_have": None,
    "note": (
        "copycat hard skip → later ~20–24× vs paper entry ~$204k / "
        "entry_p 0.2712. Keep hard skip for true clones. Evidence row #1."
    ),
}


def is_copycat_veto(veto: str | None) -> bool:
    raw = str(veto or "").strip().lower()
    return "copycat" in raw


def normalize_gate_veto(veto: str | None) -> str:
    """Autopsy / side-key form. Copycat spam collapses to ``copycat``."""
    raw = str(veto or "").strip()
    if not raw:
        return ""
    if is_copycat_veto(raw):
        return GATE_VETO_COPYCAT
    return raw[:64]


def _coerce_rank(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        rank = int(raw)
    except (TypeError, ValueError):
        return None
    if rank <= 0:
        return None
    return rank


def in_fomo_top_band(rank: int | None, *, band: int = FOMO_TOP_BAND) -> bool:
    return rank is not None and 1 <= int(rank) <= int(band)


def fomo_board_rank(items: Iterable[dict[str, Any]] | None, mint: str, *, chain: str = "sol") -> int | None:
    """Rank from the official FOMO trending board, else 1-indexed position."""
    from ..chains import normalize_chain, normalize_mint

    want_chain = normalize_chain(chain)
    want = normalize_mint(mint, want_chain)
    if not want:
        return None
    for i, row in enumerate(items or [], start=1):
        if not isinstance(row, dict):
            continue
        row_chain = normalize_chain(row.get("chain") or row.get("network") or want_chain)
        row_mint = normalize_mint(str(row.get("mint") or ""), row_chain)
        if row_mint != want:
            continue
        rank = _coerce_rank(row.get("rank"))
        if rank is not None:
            return rank
        return i
    return None


def fomo_rank_from_snapshot(session: Any, mint: str, *, chain: str = "sol") -> int | None:
    """FOMO top-band rank from the persisted official trending snapshot."""
    from ..ingest.fomo_poll import load_trending_snapshot

    snap = load_trending_snapshot(session) or {}
    return fomo_board_rank(snap.get("items") or [], mint, chain=chain)


def honest_floors_pass(
    *,
    holders: int | float | None,
    liq: float | None,
    thesis: dict[str, Any] | None,
) -> bool:
    """Existing paper floors only — holders, sellable liq, thesis hard-tag path."""
    if int(holders or 0) < int(PAPER_FRESH_HOLDERS):
        return False
    if float(liq or 0.0) < float(PAPER_CONFIRM_LIQ):
        return False
    return v1_thesis_ok(thesis)


def would_have_copycat_shadow(
    *,
    veto: str | None,
    on_hunt: bool,
    fomo_rank: int | None,
    holders: int | float | None,
    liq: float | None,
    thesis: dict[str, Any] | None,
) -> bool:
    """True when a copycat hard skip was FOMO top-band + Hunt + honest floors.

    Does not book. Does not lift the hard skip. Side-key Learn grain only.
    """
    if not is_copycat_veto(veto):
        return False
    if not on_hunt:
        return False
    if not in_fomo_top_band(fomo_rank):
        return False
    return honest_floors_pass(holders=holders, liq=liq, thesis=thesis)


def stamp_copycat_learn_sides(
    features: dict[str, Any] | None,
    *,
    veto: str | None,
    would_have: bool,
    fomo_rank: int | None,
) -> dict[str, Any] | None:
    """Merge Learn side keys. FEATURE_NAMES untouched."""
    feat = dict(features) if isinstance(features, dict) else {}
    labeled = normalize_gate_veto(veto) or str(feat.get(GATE_VETO_KEY) or "")
    if not labeled:
        return None
    feat[GATE_VETO_KEY] = labeled
    feat[WOULD_HAVE_KEY] = bool(would_have)
    if fomo_rank is not None:
        feat[FOMO_RANK_KEY] = int(fomo_rank)
    return feat


def copycat_false_veto_evidence() -> list[dict[str, Any]]:
    """Seeded autopsy rows. www is always evidence row #1."""
    return [dict(WWW_EVIDENCE)]


def copycat_hard_skip_holds(flags: Iterable[str] | None, *, chain: str = "sol") -> bool:
    return bool(paper_hard_veto(flags, chain=chain))


def feat_gate_veto(features: dict[str, Any] | None) -> str:
    feat = features if isinstance(features, dict) else {}
    return normalize_gate_veto(feat.get(GATE_VETO_KEY))


def resolve_floor_inputs(
    *,
    holders: int | float | None,
    liq: float | None,
    features: dict[str, Any] | None,
) -> tuple[int, float, dict[str, Any]]:
    """Honest floors from existing columns / frozen thesis keys. No new tags."""
    from .paper_v1 import v1_thesis_from_features

    return int(holders or 0), float(liq or 0.0), v1_thesis_from_features(features)


def merge_www_evidence(live: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """www is always evidence row #1. Live www overlays measured fields."""
    rows = [dict(r) for r in (live or []) if isinstance(r, dict) and r.get("mint")]
    www = next((r for r in rows if str(r.get("mint") or "") == WWW_MINT), None)
    others = [r for r in rows if str(r.get("mint") or "") != WWW_MINT]
    first = dict(WWW_EVIDENCE)
    if www:
        for key in ("would_have", "fomo_rank", "multiple", "entry_p", "entry_mcap", "holders", "liq"):
            if www.get(key) is not None:
                first[key] = www[key]
        first["live"] = True
        first["skip_reason"] = www.get("skip_reason") or first.get("skip_reason")
    return [first, *others]


def copycat_winner_card(
    *,
    mint: str,
    symbol: str = "",
    entry_p: float | None = None,
    entry_mcap: float | None = None,
    multiple: float | None = None,
    would_have: bool | None = None,
    fomo_rank: int | None = None,
    skip_reason: str = "",
    holders: int | None = None,
    liq: float | None = None,
    seeded: bool = False,
) -> dict[str, Any]:
    mult = float(multiple or 0.0)
    return {
        "symbol": symbol or "",
        "name": "",
        "mint": mint,
        "chain": "sol",
        "label": "FALSE-VETO" if mint == WWW_MINT or mult >= 5.0 else "WINNER",
        "gate_veto": GATE_VETO_COPYCAT,
        "entry_p": round(float(entry_p or 0.0), 4) if entry_p is not None else None,
        "entry_mcap": round(float(entry_mcap)) if entry_mcap else None,
        "multiple": round(mult, 2) if mult else None,
        "would_have": bool(would_have) if would_have is not None else None,
        "fomo_rank": int(fomo_rank) if fomo_rank is not None else None,
        "holders": int(holders) if holders is not None else None,
        "liq": round(float(liq)) if liq is not None else None,
        "skip_reason": skip_reason or "",
        "seeded": bool(seeded),
        "open": False,
    }


def copycat_veto_mints(session: Any, chain: str = "sol", *, since: Any = None) -> set[str]:
    """Mints that took a copycat hard skip (gate veto or v1 shadow)."""
    from ..chains import normalize_chain
    from ..models import Decision, PaperFill
    from .paper_v1 import PAPER_V1_SHADOW_LINE, is_copycat_skip

    chain = normalize_chain(chain)
    mints: set[str] = set()
    gates = session.query(Decision.mint, Decision.veto).filter(
        Decision.chain == chain,
        Decision.kind == "gate",
    )
    if since is not None:
        gates = gates.filter(Decision.at >= since)
    for mint, veto in gates.all():
        if mint and is_copycat_veto(veto):
            mints.add(str(mint))
    fills = session.query(PaperFill.mint, PaperFill.exit_reason).filter(
        PaperFill.chain == chain,
        PaperFill.line == PAPER_V1_SHADOW_LINE,
    )
    if since is not None:
        fills = fills.filter(PaperFill.opened_at >= since)
    for mint, reason in fills.all():
        if mint and is_copycat_skip(reason):
            mints.add(str(mint))
    return mints


def copycat_veto_cohort(
    session: Any,
    chain: str = "sol",
    *,
    days: int = 14,
    now: Any = None,
    win_mult: float = 2.0,
) -> dict[str, Any]:
    """Copycat-vetoed winners for Learn. Does not book. www is evidence #1.

    Separate from paper-miss runner/dud denominators. Side keys only.
    """
    from datetime import datetime, timedelta, timezone

    from ..chains import normalize_chain
    from ..models import Decision, Outcome, PaperFill, Research, Token
    from .paper_v1 import PAPER_V1_SHADOW_LINE, close_reason_label, is_copycat_skip

    chain = normalize_chain(chain)
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=max(1, int(days)))
    live: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _feat(raw: str | None) -> dict[str, Any]:
        import json

        try:
            data = json.loads(raw or "{}")
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _add(card: dict[str, Any]) -> None:
        mint = str(card.get("mint") or "")
        if not mint or mint in seen:
            return
        seen.add(mint)
        live.append(card)

    fills = (
        session.query(PaperFill, Token, Outcome, Research)
        .join(Token, Token.id == PaperFill.token_id)
        .outerjoin(Outcome, Outcome.token_id == Token.id)
        .outerjoin(Research, Research.token_id == Token.id)
        .filter(
            PaperFill.chain == chain,
            PaperFill.line == PAPER_V1_SHADOW_LINE,
            PaperFill.opened_at >= since,
        )
        .order_by(PaperFill.id.desc())
        .limit(800)
        .all()
    )
    for fill, token, outcome, research in fills:
        if not is_copycat_skip(fill.exit_reason):
            continue
        decision = None
        if fill.decision_id:
            decision = session.get(Decision, fill.decision_id)
        feat = _feat(decision.features_json if decision is not None else None)
        if research is not None and GATE_VETO_KEY not in feat:
            feat = {**_feat(research.features_json), **feat}
        entry_m = float(
            (fill.entry_mcap or 0.0)
            or (decision.entry_mcap if decision is not None else 0.0)
            or (outcome.t0_mcap if outcome else 0.0)
            or 0.0
        )
        peak = float((outcome.max_mcap if outcome else 0.0) or fill.max_mcap or 0.0)
        if entry_m <= 0 or peak <= 0:
            continue
        mult = peak / entry_m
        if mult < float(win_mult):
            continue
        rank = feat.get(FOMO_RANK_KEY)
        try:
            rank = int(rank) if rank is not None else None
        except (TypeError, ValueError):
            rank = None
        _add(
            copycat_winner_card(
                mint=token.mint,
                symbol=token.symbol or "",
                entry_p=fill.entry_p,
                entry_mcap=entry_m,
                multiple=mult,
                would_have=feat.get(WOULD_HAVE_KEY),
                fomo_rank=rank,
                skip_reason=close_reason_label(fill.exit_reason),
                holders=int(
                    (decision.holders if decision is not None else 0)
                    or (research.holder_count if research is not None else 0)
                    or 0
                )
                or None,
                liq=float(
                    (fill.entry_liq or 0.0)
                    or (decision.liq if decision is not None else 0.0)
                    or 0.0
                )
                or None,
            )
        )

    gates = (
        session.query(Decision, Token, Outcome, Research)
        .join(Token, Token.id == Decision.token_id)
        .outerjoin(Outcome, Outcome.token_id == Token.id)
        .outerjoin(Research, Research.token_id == Token.id)
        .filter(
            Decision.chain == chain,
            Decision.kind == "gate",
            Decision.at >= since,
        )
        .order_by(Decision.id.desc())
        .limit(800)
        .all()
    )
    for gate, token, outcome, research in gates:
        if not is_copycat_veto(gate.veto):
            continue
        feat = _feat(gate.features_json)
        if research is not None and GATE_VETO_KEY not in feat:
            feat = {**_feat(research.features_json), **feat}
        entry_m = float(
            (gate.entry_mcap or 0.0) or (outcome.t0_mcap if outcome else 0.0) or 0.0
        )
        peak = float((outcome.max_mcap if outcome else 0.0) or 0.0)
        if entry_m <= 0 or peak <= 0:
            continue
        mult = peak / entry_m
        if mult < float(win_mult):
            continue
        rank = feat.get(FOMO_RANK_KEY)
        try:
            rank = int(rank) if rank is not None else None
        except (TypeError, ValueError):
            rank = None
        _add(
            copycat_winner_card(
                mint=token.mint,
                symbol=token.symbol or "",
                entry_p=gate.entry_p,
                entry_mcap=entry_m,
                multiple=mult,
                would_have=feat.get(WOULD_HAVE_KEY),
                fomo_rank=rank,
                skip_reason=normalize_gate_veto(gate.veto),
                holders=int(gate.holders or (research.holder_count if research is not None else 0) or 0)
                or None,
                liq=float(gate.liq or 0.0) or None,
            )
        )

    evidence_rows = merge_www_evidence(live)
    n_would = sum(1 for r in evidence_rows if r.get(WOULD_HAVE_KEY) is True)
    return {
        "paper_only": True,
        "armed": bool(COPYCAT_LEARN_ARMED),
        "open": False,
        "side_key": GATE_VETO_KEY,
        "gate_veto": GATE_VETO_COPYCAT,
        "would_have_key": WOULD_HAVE_KEY,
        "fomo_top_band": FOMO_TOP_BAND,
        "win_multiple": float(win_mult),
        "n_winners": len(evidence_rows),
        "n_live": len(live),
        "n_would_have": n_would,
        "evidence_rows": evidence_rows[:24],
        "note": (
            "Copycat hard skip stays on every ticket path. "
            "gate_veto=copycat tags copycat-vetoed winners so recall cost "
            "is measurable. www (world wide web) is evidence row #1 "
            "(FALSE-VETO). would_have is a shadow when FOMO top-band + Hunt "
            "+ honest floors pass — never a book. armed=false."
        ),
    }
