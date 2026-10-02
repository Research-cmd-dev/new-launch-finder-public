# stack-v190 — GMGN + DexScreener FOMO secondary boards

When FOMO trending capture is stale, door sanity merges secondary boards (graduated, GMGN swap rank, DexScreener profiles/boosts). **fomo_mirror** production gate unchanged — still FOMO trending REST capture only.

## API

- `/api/fomo-trending`: `gmgn_trending_board`, `dexscreener_trending_board`, `secondary_overlap`, `secondary_sanity_sources`
- Secondary boards always fetched on coverage/audit (GMGN respects cooldown / no key)

## Verify

```bash
pytest tests/test_fomo_trending_mirror.py -q
```

Tarball: `artifacts/launchfinder-stack-v190.tar.gz`

Hotfix: Dockerfile assert imports `sol_trending_boost_rows` from `launchfinder.research.dexscreener` (not `fomo_api`).
