# SI meme-copy printers vs Super* duds — Learn-only cohort study

**Paper-safe.** No deploys, no gate changes, no thesis-tag invention, no Hunt-floor changes, no Origin tip, no opening copycat/start-high paper fills.  
**Serial deployer policy (Dave 2026-10-01):** serial is a **prior**, never a hard block. Super Intelligence (`9aqm…`) is serial (33 launches / 4 wins / 21 rugs on token card) **and** ~80×.

Artifacts: `cohort.csv`, `findings.json`, `raw/`, `tape/` under `/workspace/tmp/si-cohort-study/`.

## Cohort

| Label | N | Notes |
|-------|---|--------|
| **printer** | **2** | Super Inu `DEW9…` ~143×; Super Intelligence `9aqm…` ~80× |
| dud | 22 | Super Gigainu / Ape / Ansem / Coin / Elon inu / Girl Inu / Anonymous Super Inu / Super Inu clones / SI ticker flops / etc. from paper_v1 shadow + FOMO |
| adjacent_printer | 1 | Stupid Inu `3VPo…` SI ticker ~77× — **not** Dave’s Super* meme-copy pair; annotated only |
| unknown | 2 | Super Intelligence Inu ~9.5×; another SI ~8× |

Sources: production HTTP (`/api/tokens/{mint}`, tape, FOMO, paper review shadow). No DB.

## Top findings (ranked by promise at decision time)

### 1) Rising book in first ~15m (or already-organic FOMO mega-book) — strongest

- **Super Intelligence (honest early traj):** t0 snap mcap ~$14k / vol_h1 ~$17k → ~8.6m later mcap ~$119k / liq ~$30k / vol ~$108k → t15m mcap ~$164k / vol ~$189k. **mcap_t0→early ≈ 8.3×**, `rising_book_15m=1`, `dump_on_volume=0`. Ledger entry_mcap **47300** (matches Dave).
- **Super Inu (FOMO late catch):** first sight already ~$1.7M mcap, ~$136k liq, **~$2.1M vol_h1**, 2310 holders, top10 26%. Labeled `t15m` snap is **+47h** (ignored). No true first-15m post-migrate tape in DB. At **gate time** the signal is “already organic hot book,” not a reconstructable pre-migrate tape.
- **Duds:** median honest `mcap_t0_to_early_ratio` ≈ **0.19** (book dumps). Examples: Super Gigainu 50k→2.6k with vol on the way down; Super Coin / slop inu high early vol + collapsing mcap (`dump_on_volume=1`).

Cliff’s δ(printers vs duds) on `rising_book_15m` ≈ **+0.93** (tiny N).

### 2) Holder *profile* (top10), not raw holder count

- Printers: top10 **26% / 35%** (organic-ish).
- Duds: median top10 ≈ **92%**.
- **Counterexample:** Super Inu clone `DW4…` also has **2085 holders** and top10 **23.5%** but **dies** (~1.1×): t0 vol 0, early mcap dumping from ~$367k entry. → **holders alone do not separate.**

### 3) Volume matters *with* direction (Dave hunch: partial confirm)

- Sauce is **volume + rising mcap/liq**, not raw `volume_h1`.
- Dump-on-volume duds print large early vol while mcap collapses (Gigainu, Super Coin, slop inu).
- Super Intelligence: serial wallet’s **rug shape break** = rising book + top10 35% + climbing vol (not a flat/dump tape).

## Serial deployer (explicit)

| Mint | Priors (L/W/R) | Serial? | Early shape | Multiple |
|------|----------------|---------|-------------|----------|
| Super Intelligence `9aqm…` | 33 / 4 / 21 | **yes** | rising book, top10 35%, serial_pattern_break=1 | ~80× |
| Stupid Inu `3VPo…` (adjacent) | serial_penalty on | yes | rising ~2.8×, break=1 | ~77× |
| SI Super Intelligence dud `9TrR…` | serial | yes | top10 ~100%, dump, break=0 | ~1× |

**Do not hard-block serial.** Optional Learn probe: per-creator baseline of early mcap_ratio on prior rugs, then residual/break on this mint — shadow only.

## What does *not* cleanly separate (at first sight / gate)

- Raw `holder_count` (dud Super Inu clone matches printer scale)
- `fresh_wallet_pct` (printers high; many duds also 100%)
- `creator_serial` / prior rugs as a veto (printer is serial)
- `time_to_migrate_min` / migrate_speed (both classes often fast)
- Gate veto string (`copycat spam` / `start-high` hit printers and duds alike)
- Meme quality / social (both printers meme-only, weak or empty X)

## Caveats

1. **N=2** named printers — all separations are descriptive, not shippable thresholds.
2. Super Inu early post-migrate path is **under-observed** in our snaps/tape (FOMO catch already hot; minute bars start days later).
3. Today’s dud list is skewed to **2026-10-01 paper shadow** Super* spam; expand historically before trusting rates.
4. Adjacent Stupid Inu supports the rising-book story but is **out of scope** for the Super* meme-copy claim set.

## What would NOT be safe to ship

- Opening paper_v1 / gated fills on copycat or start-high names
- Inventing SI/Super* thesis tags
- Hard serial-deployer block
- Lowering Hunt floors or any production gate change from this N
- IMAGE_REV / Origin tip / dual-deploy
- Promoting FOMO rank alone into an entry rule

## Optional next Learn probe (still paper-safe)

1. **Shadow feature `early_book_shape`:** log `{mcap_t0_to_15m_ratio, liq_build, dump_on_volume, top10_at_entry, fomo_late_hot}` for SI/Super*/copycat-shadow — **no fills**.
2. **Per-creator rug-shape baseline** for wallets with ≥5 priors; score this-mint residual (serial as prior + break detector).
3. Widen cohort via archived FOMO dumps / DB same-ticker count (read-only) beyond the 2026-10-01 shadow day.

## Volume / holder verdict vs Dave’s hunch

**Partial confirm.** Volume and holder profile are the right neighborhood, but the discriminative bit is **trajectory + concentration**: rising book with moderate top10 (or already-hot organic FOMO book), not raw volume or raw holders. Serial rhymes with rugs until the early tape **breaks** that shape — as on Super Intelligence.
