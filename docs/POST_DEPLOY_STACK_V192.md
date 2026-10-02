# stack-v192 — secondary union miss audit (always on)

Union FOMO graduated + GMGN trending + DexScreener for door-miss sanity on every `/api/fomo-trending` coverage call. `secondary_miss_audit` on API + audit stamp. `high_confidence_miss` when miss on ≥2 secondaries or one secondary + FOMO trending miss.

**fomo_mirror** unchanged (FOMO trending capture only).

```bash
pytest tests/test_fomo_secondary_miss_audit.py tests/test_fomo_trending_mirror.py -q
```

Tarball: `artifacts/launchfinder-stack-v192.tar.gz`
