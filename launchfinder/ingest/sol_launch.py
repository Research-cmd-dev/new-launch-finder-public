"""Solana launch-mint helpers. Pump graduates and Raydium LaunchLab deploys.

Do not treat a random Raydium AMM pool (`pool_ray`) as a new launch.
LaunchLab program txs include every buy/sell — only initialize / migrate.
"""

from __future__ import annotations

from typing import Any

from ..config import MIGRATE_PROGRAM, PUMP_PROGRAM, PUMPSWAP_PROGRAM, RAYDIUM_LAUNCHPAD_PROGRAM
WSOL = "So11111111111111111111111111111111111111112"

SKIP_ACCOUNTS = {
    WSOL,
    MIGRATE_PROGRAM,
    PUMP_PROGRAM,
    PUMPSWAP_PROGRAM,
    RAYDIUM_LAUNCHPAD_PROGRAM,
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzLbLf4hYEeiNJF",
    "ComputeBudget111111111111111111111111111111",
}

_LAUNCH_IX = (
    "instruction: initialize",
    "instruction: initializev2",
    "instruction: initialize_v2",
    "instruction: migratetoamm",
    "instruction: migratetocpswap",
    "instruction: migrate_to_amm",
    "instruction: migrate_to_cpswap",
    "instruction: migrate",
    "instruction: createlaunchpad",
)
_TRADE_IX = (
    "instruction: buy",
    "instruction: sell",
    "instruction: swap",
    "instruction: buyexactin",
    "instruction: buyexactout",
    "instruction: sellexactin",
    "instruction: sellexactout",
)
_TRADE_TYPES = {"SWAP", "TRANSFER", "NFT_SALE", "BURN", "STAKE"}


def is_pump_mint(mint: str) -> bool:
    return bool(mint) and mint.endswith("pump")


def is_skip_account(addr: str) -> bool:
    return not addr or addr in SKIP_ACCOUNTS


def _norm_logs(logs: list[str] | None) -> str:
    return "\n".join(logs or []).lower().replace(" ", "")


def mentions_launchpad(blob: str) -> bool:
    return RAYDIUM_LAUNCHPAD_PROGRAM in (blob or "")


def is_raydium_launch_logs(logs: list[str] | None) -> bool:
    """True for LaunchLab initialize / migrate, not a curve buy/sell."""
    compact = _norm_logs(logs)
    if any(n.replace(" ", "") in compact for n in _TRADE_IX):
        return False
    return any(n.replace(" ", "") in compact for n in _LAUNCH_IX)


def is_raydium_launch_event(event: dict[str, Any]) -> bool:
    kind = str(event.get("type") or event.get("transactionType") or "").upper()
    if kind in _TRADE_TYPES:
        return False
    blob = " ".join(
        [
            str(event.get("source") or ""),
            str(event.get("description") or ""),
            RAYDIUM_LAUNCHPAD_PROGRAM if _event_mentions_launchpad(event) else "",
        ]
    )
    logs = event.get("logs") or event.get("logMessages") or []
    if isinstance(logs, list) and is_raydium_launch_logs([str(x) for x in logs]):
        return True
    if not _event_mentions_launchpad(event):
        return False
    # Enhanced "UNKNOWN" / "CREATE" on the LaunchLab program — deploy or migrate.
    return kind in {"", "UNKNOWN", "CREATE", "TOKEN_MINT", "ANY"}


def _event_mentions_launchpad(event: dict[str, Any]) -> bool:
    if event.get("source") and "raydium" in str(event.get("source")).lower():
        # SWAP already excluded by caller type check.
        return True
    for key in event.get("accountData") or []:
        acct = (key or {}).get("account") or ""
        if acct == RAYDIUM_LAUNCHPAD_PROGRAM:
            return True
    accs = event.get("accountKeys") or event.get("accounts") or []
    for key in accs:
        pubkey = key.get("pubkey") if isinstance(key, dict) else str(key)
        if pubkey == RAYDIUM_LAUNCHPAD_PROGRAM:
            return True
    return mentions_launchpad(str(event))


def mints_from_event(event: dict[str, Any]) -> list[str]:
    found: list[str] = []
    mint = event.get("mint") or event.get("token") or ""
    if mint:
        found.append(mint)
    meta = ((event.get("transaction") or {}).get("meta") or event.get("meta") or {})
    for balance in meta.get("postTokenBalances") or []:
        cand = balance.get("mint")
        if cand:
            found.append(cand)
    for key in event.get("accountData") or []:
        acct = (key or {}).get("account") or ""
        if acct:
            found.append(acct)
        for change in (key or {}).get("tokenBalanceChanges") or []:
            cand = (change or {}).get("mint") or ""
            if cand:
                found.append(cand)
    out: list[str] = []
    seen: set[str] = set()
    for cand in found:
        if cand in seen or is_skip_account(cand):
            continue
        seen.add(cand)
        out.append(cand)
    return out


def pick_launch_mints(event: dict[str, Any], logs: list[str] | None = None) -> list[str]:
    """Pump mints always. LaunchLab mints only on initialize / migrate."""
    mints = mints_from_event(event)
    pump = [m for m in mints if is_pump_mint(m)]
    if pump:
        return pump
    if logs and is_raydium_launch_logs(logs):
        return [m for m in mints if not is_pump_mint(m)]
    if is_raydium_launch_event(event):
        return [m for m in mints if not is_pump_mint(m)]
    return []
