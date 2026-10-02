from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_CHAIN = "sol"


@dataclass(frozen=True)
class ChainProfile:
    key: str
    label: str
    gmgn: str
    dex: str
    explorer_url: str
    launch_note: str
    graduation_mcap: float
    launchpads: tuple[str, ...] | None = None


CHAINS: dict[str, ChainProfile] = {
    "sol": ChainProfile(
        key="sol",
        label="Solana / Pump.fun",
        gmgn="sol",
        dex="solana",
        explorer_url="https://solscan.io/token/{mint}",
        launch_note="Pump.fun · Raydium LaunchLab",
        graduation_mcap=69_000.0,
        launchpads=("Pump.fun", "pump_mayhem", "pump_mayhem_agent", "pump_agent", "ray_launchpad"),
    ),
    "robinhood": ChainProfile(
        key="robinhood",
        label="Robinhood Chain",
        gmgn="robinhood",
        dex="robinhood",
        explorer_url="https://robinhoodchain.blockscout.com/token/{mint}",
        launch_note="PONS · Flap / Klik / Noxa",
        # EVM launchpads do not share Pump.fun's $69k floor. Prefer GMGN
        # migration_mcap / first Dex print; this is only the collapse anchor.
        graduation_mcap=40_000.0,
        # Documented for UI / logging. Trenches stay unfiltered (None) so
        # Flap graduates still arrive; PONS is ingested from the official
        # factory (ponsfamily.com/launchpad) plus GMGN new_creation.
        launchpads=("pons",),
    ),
}

_ALIASES = {
    "solana": "sol",
    "rh": "robinhood",
    "robinhoodchain": "robinhood",
    "robinhood-chain": "robinhood",
}


def normalize_chain(value: str | None) -> str:
    # FastAPI Query() objects leak through when tests call endpoints directly.
    if not isinstance(value, str):
        value = getattr(value, "default", None)
        if not isinstance(value, str):
            value = DEFAULT_CHAIN
    raw = (value or DEFAULT_CHAIN).strip().lower()
    raw = _ALIASES.get(raw, raw)
    return raw if raw in CHAINS else DEFAULT_CHAIN


def profile(chain: str | None) -> ChainProfile:
    return CHAINS[normalize_chain(chain)]


def token_chain(token: Any) -> str:
    return normalize_chain(getattr(token, "chain", None))


def graduation_mcap(chain: str | None) -> float:
    return profile(chain).graduation_mcap


def _looks_like_pair_id(value: str | None) -> bool:
    raw = (value or "").strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    return len(raw) == 64 and all(c in "0123456789abcdef" for c in raw)


def normalize_mint(mint: str, chain: str | None = None) -> str:
    mint = (mint or "").strip()
    if normalize_chain(chain) != "sol" and mint.startswith("0x"):
        return mint.lower()
    return mint


def gmgn_token_url(mint: str, chain: str | None = None) -> str:
    return f"https://gmgn.ai/{profile(chain).gmgn}/token/{mint}"


def dex_token_url(mint: str, chain: str | None = None) -> str:
    return f"https://dexscreener.com/{profile(chain).dex}/{mint}"


def dex_pair_url(pair_address: str, chain: str | None = None) -> str:
    return f"https://dexscreener.com/{profile(chain).dex}/{pair_address}"


def explorer_token_url(mint: str, chain: str | None = None) -> str:
    return profile(chain).explorer_url.format(mint=mint)


def chain_links(mint: str, chain: str | None = None, pair_address: str = "") -> dict[str, str]:
    ch = normalize_chain(chain)
    dex = dex_pair_url(pair_address, ch) if _looks_like_pair_id(pair_address) else dex_token_url(mint, ch)
    links = {
        "dex": dex,
        "explorer": explorer_token_url(mint, ch),
        "gmgn": gmgn_token_url(mint, ch),
    }
    if ch == "sol":
        links["pump"] = f"https://pump.fun/coin/{mint}"
        links["solscan"] = links["explorer"]
    if ch == "robinhood":
        links["pons"] = f"https://www.ponsfamily.com/launchpad/{mint}"
        links["pons_launchpad"] = "https://www.ponsfamily.com/launchpad"
    return links
