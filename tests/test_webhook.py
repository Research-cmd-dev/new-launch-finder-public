from launchfinder.ingest.webhooks import _mints_from_payload


def test_extracts_pump_mint():
    payload = {
        "signature": "abc",
        "meta": {"postTokenBalances": [{"mint": "So11111111111111111111111111111111111111112"}, {"mint": "Abcdefghijklmnopqrstuvwxyz123456789pump"}]},
    }
    mints = _mints_from_payload(payload)
    assert mints[0][0].endswith("pump")
