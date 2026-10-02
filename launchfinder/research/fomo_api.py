"""fomo.family activity via fomoapi.io.

Trending is keyed ``GET /v2/leaderboard/tokens/trending``. The payload can be
``source: captured`` with ``capturedAt`` hours behind the FOMO app UI — see
``parse_trending_upstream_meta`` / ``FOMO_TRENDING_CAPTURE_BUDGET_S``.
Keyless GET /v2/alerts is the RH firehose — do not send the bearer
on that call (keyed REST alerts cost 0.5 credit; keyless is free).
Keyed WSS ``/ws/alerts`` streams social flow; messages are free once
connected (see ``ingest/fomo_alerts_ws.py`` — learn only, no fills).
Keyed ``GET /v2/users/id/{id}`` wallet lookups stay **off** unless
``FOMO_USER_WALLET_LOOKUP=1`` (stack-v200 credit-burn kill). Prefer free X.
Never log the bearer key. No extra GMGN HTTP.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from ..chains import normalize_chain, normalize_mint
from ..config import env_str, settings
from ..httputil import client
from ..models import utcnow

log = logging.getLogger("launchfinder.fomo_api")

FOMO_API = "https://api.fomoapi.io"
# Official trending REST can lag the app by hours while ``stale: false``.
FOMO_TRENDING_CAPTURE_BUDGET_S = 15 * 60.0
RH_NETWORKS = frozenset({"robinhood", "4663", "robinhood chain", "rh", "hood"})
SOL_NETWORKS = frozenset({"sol", "solana", "1399811149"})
_SOL_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class FomoSitOut(Exception):
    """401/402 — key missing, invalid, or credits exhausted."""

    def __init__(self, status: int):
        super().__init__(f"FOMO API {status}")
        self.status = int(status)


def _is_robinhood(network: Any) -> bool:
    return str(network or "").strip().lower() in RH_NETWORKS


def desk_chain_from_fomo(network: Any, address: str = "") -> str | None:
    """Map a FOMO board/alert row onto a desk chain. Drop other EVMs.

    Live board mixes Sol + Robinhood + BSC/Base/ETH. Only Sol and RH
    are this-window doors. A bare 0x address is not RH — that is how
    BSC leaked if we guessed. A bare base58 mint is Sol.
    """
    n = str(network or "").strip().lower()
    addr = str(address or "").strip()
    if n in RH_NETWORKS:
        return "robinhood"
    if n in SOL_NETWORKS:
        return "sol"
    if not n and _SOL_MINT_RE.fullmatch(addr):
        return "sol"
    return None


def _auth_headers() -> dict[str, str] | None:
    key = (settings.fomo_api_key or "").strip()
    if not key:
        return None
    return {"authorization": f"Bearer {key}"}


def parse_board_tokens(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("tokens")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        token = row.get("token") if isinstance(row.get("token"), dict) else {}
        address = str(token.get("address") or row.get("address") or "").strip()
        network = row.get("network") or token.get("network") or token.get("networkId")
        chain = desk_chain_from_fomo(network, address)
        if chain is None:
            continue
        if chain == "robinhood" and not address.lower().startswith("0x"):
            continue
        mint = normalize_mint(address, chain)
        if not mint or mint in seen:
            continue
        seen.add(mint)
        try:
            buyers = int(row.get("fomoBuyers") or 0)
        except (TypeError, ValueError):
            buyers = 0
        try:
            mcap = float(row.get("marketCapUsd") or 0.0)
        except (TypeError, ValueError):
            mcap = 0.0
        out.append(
            {
                "mint": mint,
                "name": str(token.get("name") or ""),
                "symbol": str(token.get("symbol") or ""),
                "network": chain,
                "chain": chain,
                "fomo_buyers": buyers,
                "mcap_usd": mcap,
                "rank": row.get("rank"),
            }
        )
    return out


def parse_alert_tokens(payload: Any) -> list[dict[str, Any]]:
    """Unique RH mints from the live FOMO activity firehose."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("alerts")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        address = str(row.get("tokenAddress") or row.get("address") or "").strip()
        if not address.startswith("0x"):
            continue
        mint = address.lower()
        if mint in seen:
            continue
        chain = row.get("chain") or row.get("chainId")
        if not _is_robinhood(chain):
            continue
        seen.add(mint)
        symbol = str(row.get("token") or row.get("symbol") or "")
        out.append(
            {
                "mint": mint,
                "name": symbol,
                "symbol": symbol,
                "network": "robinhood",
                "fomo_buyers": 0,
            }
        )
    return out


async def fetch_alerts(*, limit: int = 100) -> list[dict[str, Any]]:
    """Live RH FOMO alerts. Keyless on purpose — do not attach the bearer."""
    resp = await client().get(
        f"{FOMO_API}/v2/alerts",
        params={"chain": "robinhood", "limit": max(1, min(int(limit), 100))},
    )
    if resp.status_code >= 400:
        return []
    try:
        payload = resp.json()
    except Exception:
        return []
    return parse_alert_tokens(payload)


_FOMO_USER_WALLET_CACHE: dict[str, str] = {}
_FOMO_USER_WALLET_CACHE_MAX = 800
_WALLET_LOOKUP_SKIP_LOGGED = False
WALLET_LOOKUP_SKIPS = 0


def fomo_user_wallet_lookup_enabled() -> bool:
    """Keyed GET /v2/users/id/{id} is off unless explicitly opted in.

    Default off: WS alerts are free; user-profile REST burns FOMO credits.
    """
    return env_str("FOMO_USER_WALLET_LOOKUP", "0").lower() in {"1", "true", "on", "yes"}


def note_wallet_lookup_skipped(*, user_id: str = "", chain: str = "") -> None:
    """Count a skipped profile lookup. Log once (not per alert)."""
    global _WALLET_LOOKUP_SKIP_LOGGED, WALLET_LOOKUP_SKIPS
    WALLET_LOOKUP_SKIPS += 1
    if _WALLET_LOOKUP_SKIP_LOGGED:
        return
    _WALLET_LOOKUP_SKIP_LOGGED = True
    log.info(
        "FOMO user-wallet REST skipped (FOMO_USER_WALLET_LOOKUP off) "
        "uid=%s chain=%s — keyed GET /v2/users/id disabled; WS wallet only",
        (user_id or "")[:12],
        chain or "",
    )


def reset_wallet_lookup_skip_state() -> None:
    """Test helper."""
    global _WALLET_LOOKUP_SKIP_LOGGED, WALLET_LOOKUP_SKIPS
    _WALLET_LOOKUP_SKIP_LOGGED = False
    WALLET_LOOKUP_SKIPS = 0


def _wallet_for_desk_chain(raw: str, chain: str) -> str:
    addr = str(raw or "").strip()
    if not addr:
        return ""
    ch = normalize_chain(chain)
    if ch == "robinhood":
        if not addr.lower().startswith("0x"):
            return ""
        return normalize_mint(addr, ch)
    if ch == "sol":
        if addr.lower().startswith("0x"):
            return ""
        if not _SOL_MINT_RE.fullmatch(addr):
            return ""
        return normalize_mint(addr, ch)
    return ""


def _parse_user_profile_wallets(payload: Any, chain: str) -> str:
    if not isinstance(payload, dict):
        return ""
    wallets = payload.get("wallets")
    if not isinstance(wallets, dict):
        wallets = payload.get("wallet") if isinstance(payload.get("wallet"), dict) else {}
    ch = normalize_chain(chain)
    if ch == "robinhood":
        for key in ("evm", "robinhood", "rh"):
            w = _wallet_for_desk_chain(str(wallets.get(key) or ""), ch)
            if w:
                return w
    else:
        for key in ("solana", "sol"):
            w = _wallet_for_desk_chain(str(wallets.get(key) or ""), ch)
            if w:
                return w
    return ""


def fetch_fomo_user_wallet_sync(user_id: str, chain: str) -> str:
    """Keyed REST profile lookup when WS alert lacks wallet (credits apply).

    Hard-disabled unless ``FOMO_USER_WALLET_LOOKUP`` is on. Default off so
    persist cannot spend credits on ``GET /v2/users/id/{id}``.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    if not fomo_user_wallet_lookup_enabled():
        note_wallet_lookup_skipped(user_id=uid, chain=chain)
        return ""
    cache_key = f"{uid}:{normalize_chain(chain)}"
    if cache_key in _FOMO_USER_WALLET_CACHE:
        return _FOMO_USER_WALLET_CACHE[cache_key]
    headers = _auth_headers()
    if headers is None:
        return ""
    url = f"{FOMO_API}/v2/users/id/{quote(uid, safe='')}"
    try:
        with httpx.Client(timeout=httpx.Timeout(12.0, connect=6.0), follow_redirects=True) as http:
            resp = http.get(url, headers=headers)
    except Exception:
        return ""
    if resp.status_code in (401, 402) or resp.status_code >= 400:
        return ""
    try:
        payload = resp.json()
    except Exception:
        return ""
    wallet = _parse_user_profile_wallets(payload, chain)
    if len(_FOMO_USER_WALLET_CACHE) >= _FOMO_USER_WALLET_CACHE_MAX:
        _FOMO_USER_WALLET_CACHE.clear()
    _FOMO_USER_WALLET_CACHE[cache_key] = wallet
    return wallet


def _parse_iso_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        sec = float(raw)
        if sec > 1e12:
            sec /= 1000.0
        try:
            return datetime.fromtimestamp(sec, tz=timezone.utc)
        except (OSError, ValueError):
            return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def parse_trending_upstream_meta(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Upstream board mirror metadata (not desk coverage)."""
    now = now or utcnow()
    if not isinstance(payload, dict) or not payload:
        return {
            "api_source": None,
            "captured_at": None,
            "capture_age_s": None,
            "capture_age_hours": None,
            "api_stale_flag": None,
            "board_stale": False,
            "capture_budget_s": FOMO_TRENDING_CAPTURE_BUDGET_S,
            "fresher_api_known": False,
            "mirror_note": "",
        }
    source = str(payload.get("source") or "").strip() or None
    captured_at = _parse_iso_ts(payload.get("capturedAt") or payload.get("captured_at"))
    age_hours_raw = payload.get("ageHours") if payload.get("ageHours") is not None else payload.get("age_hours")
    age_hours: float | None
    try:
        age_hours = float(age_hours_raw) if age_hours_raw is not None else None
    except (TypeError, ValueError):
        age_hours = None
    capture_age_s: float | None = None
    if captured_at is not None:
        capture_age_s = max(0.0, (now - captured_at).total_seconds())
        if age_hours is None:
            age_hours = capture_age_s / 3600.0
    budget_s = float(FOMO_TRENDING_CAPTURE_BUDGET_S)
    api_stale_flag = payload.get("stale")
    over_budget = False
    if capture_age_s is not None and capture_age_s > budget_s:
        over_budget = True
    if age_hours is not None and age_hours > budget_s / 3600.0:
        over_budget = True
    board_stale = bool(api_stale_flag) or over_budget
    note = ""
    if board_stale:
        age_bit = f"{age_hours:.1f}h" if age_hours is not None else "?"
        note = (
            f"FOMO API trending mirror is stale (source={source or '?'}, capture_age≈{age_bit}). "
            "App Tokens→Trending may differ. No fresher official leaderboard path in public API."
        )
    return {
        "api_source": source,
        "captured_at": captured_at.isoformat() if captured_at else None,
        "capture_age_s": round(capture_age_s, 1) if capture_age_s is not None else None,
        "capture_age_hours": round(age_hours, 3) if age_hours is not None else None,
        "api_stale_flag": api_stale_flag,
        "board_stale": board_stale,
        "capture_budget_s": budget_s,
        "fresher_api_known": False,
        "mirror_note": note,
    }


@dataclass(frozen=True)
class TrendingFetchResult:
    rows: list[dict[str, Any]]
    upstream: dict[str, Any]


async def _fetch_leaderboard_tokens(path: str, *, limit: int = 50) -> TrendingFetchResult:
    """Keyed FOMO token leaderboard. Raises FomoSitOut on 401/402."""
    headers = _auth_headers()
    if headers is None:
        return TrendingFetchResult(
            rows=[],
            upstream={
                **parse_trending_upstream_meta({}),
                "mirror_note": "no FOMO_API_KEY — FOMO leaderboard not fetched",
            },
        )
    resp = await client().get(
        f"{FOMO_API}{path}",
        headers=headers,
        params={"limit": max(1, min(int(limit), 50))},
    )
    if resp.status_code in (401, 402):
        raise FomoSitOut(resp.status_code)
    if resp.status_code >= 400:
        return TrendingFetchResult(rows=[], upstream=parse_trending_upstream_meta({}))
    try:
        payload = resp.json()
    except Exception:
        return TrendingFetchResult(rows=[], upstream=parse_trending_upstream_meta({}))
    upstream = parse_trending_upstream_meta(payload)
    return TrendingFetchResult(rows=parse_board_tokens(payload), upstream=upstream)


async def fetch_trending(*, limit: int = 25) -> TrendingFetchResult:
    """Sol + RH rows on FOMO Tokens→Trending. Raises FomoSitOut on 401/402."""
    return await _fetch_leaderboard_tokens("/v2/leaderboard/tokens/trending", limit=limit)


async def fetch_graduated(*, limit: int = 50) -> TrendingFetchResult:
    """Tokens→Graduated (often live-fomo when trending capture is stale). Learn only."""
    out = await _fetch_leaderboard_tokens("/v2/leaderboard/tokens/graduated", limit=limit)
    upstream = dict(out.upstream or {})
    upstream["board_kind"] = "graduated"
    upstream["board_note"] = (
        "FOMO Tokens→Graduated (live-fomo when fresh). Secondary cross-check — not Trending rank."
    )
    return TrendingFetchResult(rows=list(out.rows or []), upstream=upstream)
