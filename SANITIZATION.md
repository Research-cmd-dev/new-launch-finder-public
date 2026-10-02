# Sanitization record (public export)

Source: `/workspace/tmp/v207-upload-run` (stack-v207, git `004fed0bd54d`).
Also available as artifact tarball under cloud-agent-artifacts `v207-upload/`.

## Removed / never present

- No `.env` / `.env.*` (only `.env.example` with **names** and paper-safe defaults)
- No `__pycache__` / `.pyc` / `.venv` / local `data/` / `*.db`
- No `.agents/` skills, `.cursor/`, `CONTRIBUTING.md` (not in source extract)
- No Railway project tokens, service account tokens, or GodWilling-account auth
- No `GMGN_PRIVATE_KEY` or wallet signers
- Scripts kept are secret-free (`stamp_deploy.sh`, `cloud-agent-*.sh`, scanners)

## Kept (paper-safe)

- `Dockerfile`, `railway.toml` (generic; no project IDs), `requirements.txt`
- `launchfinder/` (incl. `.git_sha`, `.deploy_stamp`, `IMAGE_REV=stack-v207`)
- `tests/`, `docs/`, safe `scripts/`
- Defaults: `armed=False`, `COPYCAT_LEARN_ARMED=False`, `FOMO_USER_WALLET_LOOKUP=0`,
  `FOMO_NO_HUNT_OPEN=False`, `EARLY_BOOK_OPEN=False`

## Docs softened

- Public banner on `AGENTS.md`; broken `.agents/` / `CONTRIBUTING.md` pointers retargeted
- Snapshot notes on `docs/FORWARD.md` and `docs/HOW_IT_WORKS.md`
- New public `README.md` is the agent + human contract

## Push status

GitHub `Research-cmd-dev/new-launch-finder-public` was **not** accessible to the
Cursor GitHub connector at export time. Push deferred until Dave grants access /
creates the empty repo. Export path + tarball ready for CloudAgent attach.
