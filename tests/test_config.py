from launchfinder.config import env_float, env_int, env_str


def test_empty_railway_refs_use_defaults(monkeypatch):
    monkeypatch.setenv("ALERT_MIN_P", "")
    monkeypatch.setenv("BLOOM_WATCH_HOURS", "   ")
    monkeypatch.setenv("BACKFILL_LIMIT", "")
    monkeypatch.setenv("LAUNCHFINDER_ROLE", "")
    monkeypatch.delenv("BITQUERY_API_TOKEN", raising=False)
    monkeypatch.setenv("BITQUERY_TOKEN", "")

    assert env_float("ALERT_MIN_P", "0.7") == 0.7
    assert env_float("BLOOM_WATCH_HOURS", "168") == 168.0
    assert env_int("BACKFILL_LIMIT", "20") == 20
    assert env_str("LAUNCHFINDER_ROLE", "all") == "all"
    assert env_str("BITQUERY_API_TOKEN") == ""
    assert (env_str("BITQUERY_API_TOKEN") or env_str("BITQUERY_TOKEN")) == ""


def test_set_values_win(monkeypatch):
    monkeypatch.setenv("ALERT_MIN_P", "0.82")
    monkeypatch.setenv("BACKFILL_LIMIT", "8")
    monkeypatch.setenv("LAUNCHFINDER_ROLE", " worker ")

    assert env_float("ALERT_MIN_P", "0.7") == 0.82
    assert env_int("BACKFILL_LIMIT", "20") == 8
    assert env_str("LAUNCHFINDER_ROLE", "all") == "worker"
