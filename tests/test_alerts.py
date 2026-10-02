from launchfinder.alerts import format_alert, is_hard_stopped


def test_hard_stop_flags_suppress_alerts():
    assert is_hard_stopped(["GMGN flags this as a honeypot"])
    assert is_hard_stopped(["GMGN detects wash trading"])
    assert is_hard_stopped(["Claimed X account never tweets this ticker (likely hijacked)"])
    assert is_hard_stopped(["Same ticker launched repeatedly in 24h (copycat spam)"])
    assert not is_hard_stopped(["Supply looks concentrated in top wallets"])
    assert not is_hard_stopped([])


def test_alert_includes_reasons():
    text = format_alert(
        symbol="RVR",
        name="River",
        mint="Mint111",
        p_good=0.8,
        mcap_usd=100000,
        flags=[],
        reasons=["Has an X account", "Holder count looks organic", "GMGN clean", "extra"],
    )
    assert "why: Has an X account; Holder count looks organic; GMGN clean" in text
    assert "extra" not in text


def test_alert_message_contains_essentials():
    text = format_alert(
        symbol="RVR",
        name="River",
        mint="So11111111111111111111111111111111111111112",
        p_good=0.82,
        mcap_usd=145000.0,
        flags=["top10 concentrated"],
    )
    assert "RVR" in text
    assert "82%" in text
    assert "$145,000" in text
    assert "top10 concentrated" in text
    assert "pump.fun/coin/So1111" in text
    assert "gmgn.ai/sol/token/So1111" in text


def test_rh_alert_uses_robinhood_links():
    text = format_alert(
        symbol="FLAP",
        name="Flap",
        mint="0xabc",
        p_good=0.8,
        mcap_usd=50000,
        flags=[],
        chain="robinhood",
    )
    assert "gmgn.ai/robinhood/token/0xabc" in text
    assert "pump.fun" not in text


def test_alert_message_omits_empty_sections():
    text = format_alert(
        symbol="",
        name="",
        mint="ABCDEF1234567890",
        p_good=0.7,
        mcap_usd=0.0,
        flags=[],
    )
    assert "ABCDEF12" in text
    assert "mcap" not in text
    assert "flags" not in text
