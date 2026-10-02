# Post-deploy — stack-v181 (honest thesis coverage)

## Diagnosis (gate 2/15 hard tags)

Production gate counts **github/dev/cto** on frozen paperV1 opens (`_v1_thesis`). Live ~13/15 are **meme-only legacy** (no CTO/GitHub/dev in `raw_json` or columns) — repair cannot tag without inventing evidence (v177 `url_meta` stays fail-closed).

Repairable bucket: opens with a **stored GitHub ref** (`url_meta` / `github_url`) but no API payload yet — auth stays &lt;0.6 until `lookup_repo` runs.

## Change

- HTTP open enrich treats **raw `github.full_name`**, GMGN website, and link blobs as discoverable (not only `token.website`).
- `enrich_thesis_before_entry` + free-source pass call **honest GitHub API lookup** on stored refs (capped).
- Slightly higher per-tick caps; RH qualify-candidate HTTP enrich on worker when enabled.

## Verify

```bash
curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v181'"
python3 -c "
from launchfinder.db import init_db, session_scope
from launchfinder.scoring.thesis_enrich import audit_paper_v1_open_thesis_gaps
init_db()
with session_scope() as s:
    print(audit_paper_v1_open_thesis_gaps(s))
"
```

Expect `gate_hard_tag` to rise only when real API/meta evidence exists; `meme_only` unchanged for true legacy opens.
