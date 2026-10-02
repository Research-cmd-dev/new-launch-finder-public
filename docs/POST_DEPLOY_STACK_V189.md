# stack-v189 — graduated FOMO secondary + BANDIT-class poll

## 2) Graduated board (when trending capture stale)

When trending `board_stale`, also fetch `GET /v2/leaderboard/tokens/graduated` (live-fomo).

- `/api/fomo-trending`: `graduated_board` block + `sanity_board=graduated_secondary`
- Door **miss/caught/seen** counts use graduated rows; `trending_items` stays the stale trending mirror
- Learn UI labels graduated as secondary (not Trending rank)

## 4) BANDIT-class instant curve

- GMGN trenches every **2nd** poll (~24s), not every 5th
- Pump **created_timestamp** complete page limit **60**
- Each poll: `_ingest_watch_door_completions` probes watch strip ≥0.90 progress via Pump API (`source=watch_door`)
- Migrate WS logsSubscribe commitment **`processed`** (faster than confirmed)

Prewarm band stays **0.80–0.89** only.

## Verify

```bash
pytest tests/test_fomo_trending_mirror.py tests/test_pump_watch_door.py -q
```

Tarball: `artifacts/launchfinder-stack-v189.tar.gz`
