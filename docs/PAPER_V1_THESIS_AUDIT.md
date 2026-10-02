# paperV1 open thesis audit (operator / offline)

Production gate **thesis_coverage** counts **hard tags** (`github` / `dev` / `cto`) on
`paper_fills` where `line = 'paper_v1'` and `status IN ('open','closed')`, via frozen
entry `Decision.features_json` (`_v1_thesis`).

## Postgres: list opens

```sql
SELECT pf.id, pf.chain, pf.mint, pf.token_id, pf.status, pf.opened_at
FROM paper_fills pf
WHERE pf.line = 'paper_v1'
  AND pf.status IN ('open', 'closed')
ORDER BY pf.id DESC
LIMIT 200;
```

## In-process harness (no live numbers in repo)

```bash
python3 -c "
from launchfinder.db import init_db, session_scope
from launchfinder.scoring.thesis_enrich import audit_paper_v1_open_thesis_gaps
init_db()
with session_scope() as s:
    print(audit_paper_v1_open_thesis_gaps(s))
"
```

Bucket meanings:

| key | meaning |
|-----|---------|
| `gate_hard_tag` | already counts toward coverage |
| `raw_would_*` | `raw_json` alone would stamp a hard tag if synced to entry |
| `columns_only_github` | `raw_json.github` empty but `tokens.github_url` / `research.github_*` can hydrate |
| `meme_only` | no hard signal; `name_quality` only |
| `score_only_empty` | no thesis evidence in raw or columns |

Cycle 6 knob (`stack-v136`): `hydrate_thesis_raw_from_research` before
`sync_thesis_from_raw` on paperV1 repair / qualify paths.

Cycle 7 knob (`stack-v137`): `enrich_paper_v1_open_thin_thesis_http` on each
hunt_tape tick (≤6 tokens, ≤4 website fetches) before paper sync repair.

Cycle 8 knob (`stack-v138`): `enrich_paper_v1_open_free_sources` first on each
tick — stored meta (GMGN CTO / socials in `raw_json`, token links), then ≤2
GMGN `token_research` and ≤2 pump.fun `get_coin` when website is missing.

Cycle 9 knob (`stack-v139`): honor `gmgn_deep_available` / process cooldown in
free-source enrich; **≤1** GMGN `token_research` per hunt_tape tick; meta +
pump paths still run while GMGN sits out.

Cycle 10 knob (`stack-v140`): paperV1 **opens** require a hard thesis tag
(github/dev/cto) after stored-meta sync (`v1_hard_tag_ready`); score-only /
Live≥0.70 / 2× peak paths no longer open without evidence. Lock skips with
`v1 no-thesis`. Existing opens unchanged.

Cycle 13 knob (`stack-v181`): stored `url_meta` GitHub refs trigger capped
`lookup_repo` on paperV1 open enrich (free-source + before-entry HTTP);
`thesis_open_has_discoverable_evidence` widens the HTTP queue beyond
`token.website` alone. Meme-only legacy opens stay untagged.

Cycle 12 knob (`stack-v142`): `ensure_entry_thesis_from_stored` hydrates +
syncs **Research** even when only a gate Decision exists (wide-fill path), so
repair / gate ``_v1_thesis`` merge can count column-backed GitHub. HTTP open
enrich also tries twitter/description links before ``no_site`` skip.
