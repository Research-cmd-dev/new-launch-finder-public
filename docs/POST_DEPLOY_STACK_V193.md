# stack-v193 — miss_reason + capped auto-repair

`secondary_miss_audit` classifies each miss (`miss_reason`) and attempts paper-safe ingest repair (≤5 HC `never_ingested` / `ingest_lag` per coverage). No paperV1 opens; **fomo_mirror** unchanged.

```bash
pytest tests/test_fomo_miss_autofix.py tests/test_fomo_secondary_miss_audit.py -q
```

Tarball: `artifacts/launchfinder-stack-v193.tar.gz`
