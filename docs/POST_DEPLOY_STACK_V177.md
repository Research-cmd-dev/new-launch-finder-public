# Post-deploy — stack-v177 (honest GitHub + paper_sync heartbeat)

- Thesis hydrate no longer invents `commits` / `contributors` for `github` hard tags.
- Worker beats `loop:paper_sync` after successful ledger sync; stability gate requires it.

```bash
curl -sS "$BASE/health" | python3 -c "import sys,json; assert json.load(sys.stdin)['image_rev']=='stack-v177'"
curl -sS "$BASE/health" | python3 -c "import sys,json; print('paper_sync', (json.load(sys.stdin).get('loops') or {}).get('paper_sync'))"
```
