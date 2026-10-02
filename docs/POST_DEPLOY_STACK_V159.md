# Post-deploy — stack-v159 (meme quality score, Learn-first)

Paper-only meme pile quality (tape / holders / wallets / discipline). Does **not** open `paper_v1` from meme tag alone.

```bash
BASE=https://new-launch-finder-production.up.railway.app
DAY=$(date -u +%F)

curl -sS "$BASE/health" | python3 -c "
import sys,json
h=json.load(sys.stdin)
assert h.get('image_rev')=='stack-v159'
assert h.get('armed') in (False, None, 0, 'false')
print('paper_only ok', h.get('paper_only'), 'armed', h.get('armed'))
"

# Learn review: meme_quality samples + shadow rows may include v1 meme-q|d:…
curl -sS "$BASE/api/paper/v1/review?chain=sol&day=$DAY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
mq=d.get('meme_quality') or []
print('meme_quality_samples', len(mq))
if mq:
    assert mq[0].get('score') is not None
shadow=[r for r in (d.get('shadow') or []) if str(r.get('skip_reason','')).startswith('v1 meme-q')]
print('meme_q_shadow_n', len(shadow))
"

# Token detail: meme-only name should expose meme_quality.score (pick a Hunt meme ticker).
MINT="${MINT:-DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP}"
curl -sS "$BASE/api/tokens/$MINT" | python3 -c "
import sys,json,os
d=json.load(sys.stdin)
mq=d.get('meme_quality') or {}
if mq.get('hard_veto'):
    assert mq.get('hard_veto')=='copycat spam' or mq.get('score')==0
print('meme_quality', mq.get('score'), 'veto', mq.get('hard_veto'))
"
```

`copycat spam` stays hard (`PAPER_SHADOW_VETOES` unchanged). Top-decile meme-only shadows use `v1 meme-q|d:YYYY-MM-DD` (≤32 chars).
