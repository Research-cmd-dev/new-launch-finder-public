# Copycat cluster path

Replaces the copycat hard-skip Learn grain (`COPYCAT_LEARN_ARMED`,
`would_have`, FOMO top-band proxy, symbol-flood penalty) for new captures.

A same-symbol mint is a row. `ticker_birth_n` is assigned at first sight
from earlier mints only (chain + normalized symbol, 24h window) and stored
as a feature. The 4th mint of a name is eligible. A name match is not a veto.

Decision clock is migrate + 60s. Frozen features: birth order, seconds since
first, same creator/funder, first still above open, top10 / bundler / rat,
dev sold, holders, liquidity. Later clone count and later FOMO rank are not
features.

Label is the path: 15m return, 1h return, worst drawdown, liquidity still
there. `production_gate` (5x and liq still >= $5k at 1h) is a column, not the
training class.

Join key for paper is `chain` + `mint` onto `paper_fills`. This module does
not open a fill and does not import into the buy path.

`CAPTURES = True`. `ENTERS_BUY_PATH = False`. `evaluate_time_split` must beat
skip-all and buy-first on a forward time split before any short list. Even
then `promote` stays false until a human arms a separate gate. Live stack is
unchanged by this public snapshot.
