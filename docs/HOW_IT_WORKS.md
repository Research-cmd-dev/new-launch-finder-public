# How Launch Finder works (plain English)


> **Public snapshot note:** `.agents/` skills and private Origin deploy credentials are not shipped here. Prefer [README.md](../README.md) hard locks and [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

This is a **research desk**, not a trading bot. It finds new tokens, writes a score, and keeps watching the live book. It never buys or sells.

Agents: read `.agents/skills/launchfinder-feature-map/SKILL.md` before changing the desk, and `.agents/skills/launchfinder-verify/SKILL.md` before calling a change done. The test desk is Pair-only (Tape/Market tabs are gone).

There are two boards:

- **Solana** (`/`) — mostly Pump.fun coins that just **graduated** (bonding curve done → PumpSwap)
- **Robinhood** (`/rh`) — new RH launches (PONS factory, GMGN trenches, Dex pairs)

A dark **test desk** lives at `/ui` (Solana) and `/ui/rh` (Robinhood). Three panes: tape | Dex Pair | research. Pair is a DexScreener embed. Classic green `/` and `/rh` stay as they are.

Same idea on both: find it early, score it, update the tape as the book moves.

---

## The one-minute version

1. A new token shows up on a **door** we watch.
2. We save a card: name, socials, holders, liquidity, first market cap (**t0**).
3. We give it a **score** — “does this look like a real 5×+ runner, or factory junk?”
4. Every ~12 seconds the **screen** refreshes from the database.
5. In the background we pull Dex again for a **sample** of books and update last market cap. Young unlabeled books can get a **second look** on the score. After a book is judged win/rug, the **entry score stays frozen**; the tape (price / multiple) can still move.

If a name is missing, it usually never hit a door we watch — not “the scanner is off.”

---

## What the numbers on a card mean

| You see | What it is |
|---|---|
| **Score / p(good)** | Chance this launch is worth a closer look. Paper and the model treat this as: did it do a real multiple from **t0** without the pool dying. |
| **t0 / entry mcap** | First honest post-launch print we stored. On Sol graduates this is often near the **$69k** Pump.fun floor. |
| **Last mcap** | Latest print we stored from Dex (or Pump). This can be wrong if we picked a leftover pair — see “Fake $200M” below. |
| **Multiple** | Last (or live) mcap ÷ t0. 2× means it doubled from entry. |
| **Holders / wallets** | Unique addresses that hold the token **when we last counted**. Not “people in Discord.” |
| **Top10** | Share of supply in the ten biggest wallets. 90%+ is usually concentrated / bundled. |
| **Liq** | Dollars in the pool. Under ~$800 we treat the book as dead or a ghost pair. |
| **Age** | On the desk this is usually **created / migrated time**, not “when we first saw it.” |

---

## How we find tokens

### Solana

We ingest **Pump.fun-family** graduates, plus a small Sol Dex door for PEPE-quoted AMM books:

1. Pump.fun’s public list of `complete=true` coins (every ~12s)
2. Helius websocket on the Pump **migrate** program (seconds, if Helius is up)
3. GMGN trenches for Pump.fun / pump_mayhem / **ray_launchpad** (every 5th cycle, one POST). LaunchLab **deploys** come from that POST’s `new_creation`; Pump creates stay on Watch until `complete=true`.
4. Raydium LaunchLab initialize / migrate (`LanMV9s…3uj`) via WS when Helius is up — not required if GMGN is healthy
5. **Dex PEPE quote page + token-profiles/boosts** (`sol_dex`) — Raydium CPMM vs PEPE (`PEPEqnuu…55m`) and paid Dex visibility. Age ≤ Hunt Sol 18h, fillable liq ≥ $5k. ≥$1.5M is leftover at any age (GL1TTR $10M). After 2h a ≥$150k print is leftover (WOJAK $1.5M / 13h stays out). Pump mints stay on doors 1–4.

Classic Pump.fun mints end in `pump`. A random PumpSwap or Raydium AMM that is **not** Pump / LaunchLab / PEPE-quoted / profiled **will not ingest**. We do not scrape “every new pair.” Do not add the LaunchLab program to a Helius **Any** webhook — every curve buy would bill a credit. The PEPE page is a 30-pair popularity list (ROUTE-class); a brand-new $20k book can sit off it until it has volume. Profiles/boosts are the closer-to-migrate half of this door. FOMO trending is the late catch.

Watch (graduating soon) is the **bonding curve** at ~80–89%. That is not ingested as a live launch yet. Stuck at 90%+ for hours is leftover, not “about to graduate.”

### Robinhood

Several doors, none of them is “the whole chain”:

| Door | What it catches | How fast |
|---|---|---|
| **PONS factory** | Official PONS mints | Every cycle (~12s) |
| **GMGN trenches** | Completed + new_creation (PONS, Flap, etc.) | About every other cycle (~24s) |
| **Dex quote pages** | Fresh pairs vs HIMS, WETH, MSTR, GME, … | Every cycle, but only if Dex’s **popularity list** shows the pair |
| **FOMO alerts** | Names already trending in the FOMO app | About every 10 minutes |
| **Bitquery Uni V4 stream** | Pool `Initialize` the second it happens | Instant — **currently off** (no `BITQUERY_API_TOKEN` on Railway) |

**BOOTS** (`0x6360…71b0`) was a Uni V4 book vs **MSTR**. Bitquery would have seen it at 02:16:59. Dex’s MSTR page is a 30-pair popularity list; a $20k book is often missing. We ingested it ~6 minutes later via Dex once it showed up. That is the ROUTE-class miss.

---

## Sol “graduate” vs RH “new launch”

This is the bit that feels backwards.

**Pump.fun graduate:** the coin already ran a curve. By migrate there are usually **dozens or hundreds of holders**. Six wallets at graduation would be weird.

**Robinhood trench print:** often a **brand-new factory mint**. Someone posts an LP. At that second holders are often:

- the pool
- the creator
- one or two snipes

So **2–6 wallets** on a new `/rh` name is normal. It is not “it graduated with no one in it.” Hunt treats Robinhood as **thin at 25 holders or fewer**. Thin names **stay on the desk**, they just sort **under** crowded books (BOOTS at 148 wallets stays #1; NIKOLA at 6 wallets lands around #116).

The age you see on #1 is the **top card**, not the last thing we ingested.

Blockscout (RH holder list) can also be **late**. First research may count only LP rows. The live book can be bigger than the number on the card.

---

## How scoring works

At first sight we pull socials, GitHub, creator history, holders, Dex tape, and GMGN security when we have quota. That becomes a **feature vector** (66 numbers) plus written rules (organic book, empty book, prepumped, leftover FDV, …).

Two scores live on the card:

- **Entry score (`p_good`)** — what we thought at ingest. Paper and the model train on this. After the book is labeled win/rug, this **does not keep rewriting**.
- **Live honesty** — if the book dumped off its high, the **displayed** % can fade even though entry `p` stayed put.

While a name is still **unlabeled and under 2×**, a **second look** can move `p_good` (first hour ~every 5 minutes if that mint got a Dex tick, plus at 15m and 6h). **At 2×+ we stop lifting** entry `p` on purpose (so a run does not inflate its own paper grade).

“Good” for learning is a **real multiple from t0** with a live pool — the desk hunts **5–50×**, not a 2× bounce. We cap honest multiples at **80×**. A 4000× print is treated as leftover / fake tape, not a win.

---

## How tracking / “is the list live?” works

The **webpage** refetches everything every **12 seconds**. That rebuilds hunt, Doing well, etc. from the database. It does **not** call Dex itself.

The **worker** also runs about every 12 seconds and:

- polls for new launches
- refreshes market cap for a **batch**, not every token
  - young books (first ~35 minutes) first
  - then a rotating sample of unlabeled names
  - labeled 5×+ books: a small sample each cycle, for **72 hours**
- first hour: extra snapshots ~every **5 minutes** when that mint is in the batch
- after hour one: last print on each Dex hit; late snapshots ~every **15 minutes**

So the list **paints** every 12s. Market cap on a given card updates when **that mint’s next Dex tick** lands — often minutes, not every refresh.

---

## The tabs

**Live migrations (hunt)**  
New names this window. Real / crowded books first, thin factory prints lower. Fat leftover majors (huge mcap, still ~1×) sort down so they do not look like new runners, and the live Sol page keeps at most eight of them so a last_trade flood (NTDA / NIKE $5–15M) cannot fill the 80. Confirmed 5× wins belong on runners, not as the whole hunt page. Unwatched $1.5M+ Sol books are not ingested as new graduates.

**Doing well**  
Names that are **still up now** — live about **2×+ vs t0**, not a dumped-off-high recap. A held 10×+ book can stay even after a fade from ATH (RH MEME-class). A labeled this-window **5× live** book with a real pool ($20k+ liq) can sit even when runners cannot confirm the multiple (Laptop `GsewXp…` / TNT — t0 snap already the live book). **Sol leftover FDV** does not: if last/t0 is over 80×, or a frozen $69k t0 plus a concentrated leftover tape, it is dropped. That is why leftover **LAPTOP** `76cJ…` left Doing well after it rugged while the card still said $295M.

**Runners / approaching**  
Confirmed or climbing multiples, with extra filters (not a 4-wallet wick, not a dead pool, not an 80× artifact). Approaching uses the Dex-written last mcap when we have one, not a stale climb snapshot (discat sat 2.01× after last rewrote to $3.5k).

**Watch**  
Almost graduated (Sol curve ~80–89%, or RH near-completion). Not a live hunt card yet.

**Wallets / FOMO / Early**  
Wallets that show up early on books that later worked. Built from **stored** Helius / Blockscout maps — no extra GMGN HTTP.

---

## Fake $200M (leftover FDV)

Dex often returns **several pairs**. We take the **deepest liquidity** and may store `marketCap` **or FDV**.

A rugged Pump leftover pair can still print **hundreds of millions FDV** and fat “liq” while the **live PumpSwap book** is $2k. That is LAPTOP: desk 4279× / $295M, Dex live ~$2.3k.

Doing well now refuses that Sol leftover. Hunt/detail can still show the stored leftover print until we always pick the live book.

**80× is a filter, not a target.** We do not raise that cap to “believe” a $200M leftover.

---

## What is on vs off right now

Live health (`image_rev=live-book` as of this writing):

| Integration | Status | What you lose if it’s off |
|---|---|---|
| Helius | On | Sol migrate WS + holder maps get weak |
| GMGN | On | Trenches + security card |
| X / GitHub | On | Weaker social score |
| FOMO | On | Late “already trending” RH names |
| PONS | On | Official RH factory still lands |
| Sol websocket | On | Pump migrate in seconds |
| **Bitquery** | **Off** | RH Uni V4 opens at the block (BOOTS / ROUTE class wait on Dex popularity) |

When Bitquery is set on Railway only (never in git), `/health` should show `bitquery: true` and logs should say we subscribed to Robinhood Uni V4 `Initialize`. Implement checklist: **[docs/BITQUERY.md](BITQUERY.md)**.

---

## Why a name you saw on Dex is missing

Work down the list:

1. **Wrong chain / wrong door** — Sol desk does not ingest a random WSOL PumpSwap. PEPE-quoted / profiled non-pump AMM goes through `sol_dex`. RH desk does not see a V4 pair until Dex’s quote page, a boost, GMGN, PONS, FOMO, or Bitquery.
2. **Too new for the quote page** — pair exists on `dex/tokens/<mint>` but not on the HIMS/MSTR list yet.
3. **It ingested but sorted down** — 2–25 holders on `/rh` sit under fat books. Search the mint.
4. **Leftover / start-high** — huge t0, dumped ATH, or 80×+ tape may be off Doing well / runners on purpose.
5. **Same ticker, different mint** — watch **puter** `59tF9…pump` is not Dex **puter** `BHB7J…`.

---

## What this is not

- Not auto-buy. No private swap key. GMGN is read-only.
- Not a complete chain indexer. We watch specific doors.
- Not a promise the last mcap is the pair you have open. Check Dex if the multiple looks insane.

---

## If you want the desk to get earlier and more honest

Highest leverage, in order:

1. **Bitquery token** on Railway — RH V4 at open.
2. **Always use the live book** — ignore leftover Pump FDV so hunt matches Doing well.
3. **Priority Dex ticks** for 2×+ / Doing well / high-score names (not only a random sample).
4. A separate **live score** that can move after 2×, while **entry score** stays frozen for paper.

That is the whole loop: **doors → card → score → sampled tape → tabs that hide junk.** The screen is live. The tape is only as live as the last Dex tick and the pair we believed.
