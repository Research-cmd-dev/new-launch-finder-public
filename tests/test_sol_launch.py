from launchfinder.config import RAYDIUM_LAUNCHPAD_PROGRAM
from launchfinder.ingest.sol_launch import (
    is_raydium_launch_event,
    is_raydium_launch_logs,
    pick_launch_mints,
)
from launchfinder.ingest.webhooks import _mints_from_payload


def test_pump_mint_still_extracted():
    payload = {
        "signature": "abc",
        "meta": {
            "postTokenBalances": [
                {"mint": "So11111111111111111111111111111111111111112"},
                {"mint": "Abcdefghijklmnopqrstuvwxyz123456789pump"},
            ]
        },
    }
    mints = _mints_from_payload(payload)
    assert mints[0][0].endswith("pump")


def test_launchlab_initialize_extracts_non_pump_mint():
    mint = "RayLaunchMint11111111111111111111111111111"
    logs = [
        f"Program {RAYDIUM_LAUNCHPAD_PROGRAM} invoke [1]",
        "Program log: Instruction: InitializeV2",
    ]
    event = {
        "signature": "ray1",
        "type": "UNKNOWN",
        "logs": logs,
        "accountData": [{"account": RAYDIUM_LAUNCHPAD_PROGRAM}, {"account": mint}],
        "meta": {"postTokenBalances": [{"mint": mint}]},
    }
    assert is_raydium_launch_logs(logs)
    assert pick_launch_mints(event, logs) == [mint]
    assert _mints_from_payload(event)[0][0] == mint


def test_launchlab_buy_is_not_a_deploy():
    mint = "RayLaunchMint11111111111111111111111111111"
    logs = [
        f"Program {RAYDIUM_LAUNCHPAD_PROGRAM} invoke [1]",
        "Program log: Instruction: BuyExactIn",
    ]
    event = {
        "signature": "buy1",
        "type": "SWAP",
        "logs": logs,
        "accountData": [{"account": RAYDIUM_LAUNCHPAD_PROGRAM}, {"account": mint}],
        "meta": {"postTokenBalances": [{"mint": mint}]},
    }
    assert not is_raydium_launch_logs(logs)
    assert not is_raydium_launch_event(event)
    assert pick_launch_mints(event, logs) == []
    assert _mints_from_payload(event) == []
