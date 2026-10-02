# stack-v191 — secondary board parse + loud empty reasons

Fixes silent empty `gmgn_trending_board` / `dexscreener_trending_board` on live:

- GMGN: treat HTTP-200 body `code=429` as rate-limit; unwrap nested `data.rank`; log payload key trace when empty; surface `skip_reason` / `empty_reason`.
- DexScreener: per-path `fetch_paths` meta; Sol `chainId`/`tokenAddress` parsing; `empty_reason` when filtered empty.

Dockerfile: `sol_trending_boost_rows` from `launchfinder.research.dexscreener`.

```bash
pytest tests/test_fomo_secondary_parse.py tests/test_fomo_trending_mirror.py -q
```

Tarball: `artifacts/launchfinder-stack-v191.tar.gz`
