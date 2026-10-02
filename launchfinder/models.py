from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    symbol: Mapped[str] = mapped_column(String(32), default="")
    creator: Mapped[str] = mapped_column(String(64), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(Text, default="")
    twitter: Mapped[str] = mapped_column(Text, default="")
    website: Mapped[str] = mapped_column(Text, default="")
    telegram: Mapped[str] = mapped_column(Text, default="")
    github_url: Mapped[str] = mapped_column(Text, default="")
    created_at_chain: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    migrated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(32), default="poll")
    signature: Mapped[str] = mapped_column(String(128), default="")
    pool_address: Mapped[str] = mapped_column(String(128), default="")
    is_historical: Mapped[bool] = mapped_column(Boolean, default=False)
    nsfw: Mapped[bool] = mapped_column(Boolean, default=False)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)

    snapshots: Mapped[list[Snapshot]] = relationship(back_populates="token", cascade="all, delete-orphan")
    research: Mapped[Research | None] = relationship(back_populates="token", uselist=False, cascade="all, delete-orphan")
    outcome: Mapped[Outcome | None] = relationship(back_populates="token", uselist=False, cascade="all, delete-orphan")


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="live")  # t0, live, t15m, t1h, t6h, t24h
    price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    mcap_usd: Mapped[float] = mapped_column(Float, default=0.0)
    volume_h1: Mapped[float] = mapped_column(Float, default=0.0)
    volume_m5: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity_usd: Mapped[float] = mapped_column(Float, default=0.0)
    buys_m5: Mapped[int] = mapped_column(Integer, default=0)
    sells_m5: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    p_good: Mapped[float] = mapped_column(Float, default=0.0)

    token: Mapped[Token] = relationship(back_populates="snapshots")


class Research(Base):
    __tablename__ = "research"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), unique=True)
    researched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    risk_flags_json: Mapped[str] = mapped_column(Text, default="[]")
    twitter_handle: Mapped[str] = mapped_column(String(64), default="")
    twitter_followers: Mapped[int] = mapped_column(Integer, default=0)
    twitter_tweets: Mapped[int] = mapped_column(Integer, default=0)
    twitter_age_days: Mapped[float] = mapped_column(Float, default=0.0)
    twitter_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    x_mentions_1h: Mapped[int] = mapped_column(Integer, default=0)
    github_stars: Mapped[int] = mapped_column(Integer, default=0)
    github_forks: Mapped[int] = mapped_column(Integer, default=0)
    github_age_days: Mapped[float] = mapped_column(Float, default=0.0)
    holder_count: Mapped[int] = mapped_column(Integer, default=0)
    top10_pct: Mapped[float] = mapped_column(Float, default=0.0)
    creator_hold_pct: Mapped[float] = mapped_column(Float, default=0.0)
    creator_prior_launches: Mapped[int] = mapped_column(Integer, default=0)
    creator_prior_wins: Mapped[int] = mapped_column(Integer, default=0)
    creator_prior_rugs: Mapped[int] = mapped_column(Integer, default=0)
    time_to_migrate_min: Mapped[float] = mapped_column(Float, default=0.0)
    thesis: Mapped[str] = mapped_column(Text, default="")
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    p_good: Mapped[float] = mapped_column(Float, default=0.0)
    heuristic_p: Mapped[float] = mapped_column(Float, default=0.0)
    model_p: Mapped[float] = mapped_column(Float, default=0.0)
    # Watch-time prior. Not in FEATURE_NAMES. 0 when the name skipped Watch.
    preview_p: Mapped[float] = mapped_column(Float, default=0.0)
    # Which model wrote p_good at first sight: legacy | first_sight. The
    # desk lines (0.70/0.90 vs 0.30/0.50) follow this, never the chain.
    scorer: Mapped[str] = mapped_column(String(16), default="legacy")

    token: Mapped[Token] = relationship(back_populates="research")


class Outcome(Base):
    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), unique=True)
    t0_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    t15m_mcap: Mapped[float | None] = mapped_column(Float)
    t1h_mcap: Mapped[float | None] = mapped_column(Float)
    t6h_mcap: Mapped[float | None] = mapped_column(Float)
    t24h_mcap: Mapped[float | None] = mapped_column(Float)
    max_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    last_liq: Mapped[float] = mapped_column(Float, default=0.0)
    last_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    multiple: Mapped[float] = mapped_column(Float, default=0.0)
    label: Mapped[int | None] = mapped_column(Integer)  # 1 good, 0 bad, null pending
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_for_train: Mapped[bool] = mapped_column(Boolean, default=False)

    token: Mapped[Token] = relationship(back_populates="outcome")


class ModelState(Base):
    __tablename__ = "model_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    weights_json: Mapped[str] = mapped_column(Text, default="{}")
    bias: Mapped[float] = mapped_column(Float, default=0.0)
    n_train: Mapped[int] = mapped_column(Integer, default=0)
    n_correct: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScanState(Base):
    __tablename__ = "scan_state"
    __table_args__ = (UniqueConstraint("key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FomoWallet(Base):
    """Repeat holder across the desk's top 100 confirmed 5×+ runners.

    Built from stored research.raw_json holder maps (Helius / Blockscout).
    No extra GMGN HTTP. Creator and pool addresses are excluded.
    """

    __tablename__ = "fomo_wallets"
    __table_args__ = (UniqueConstraint("chain", "owner", name="uq_fomo_wallets_chain_owner"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)
    n_tokens: Mapped[int] = mapped_column(Integer, default=0)
    n_wins: Mapped[int] = mapped_column(Integer, default=0)
    n_rugs: Mapped[int] = mapped_column(Integer, default=0)
    best_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    sum_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    best_symbol: Mapped[str] = mapped_column(String(32), default="")
    best_mint: Mapped[str] = mapped_column(String(64), default="")
    last_pct: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    hits: Mapped[list[FomoWalletHit]] = relationship(back_populates="wallet", cascade="all, delete-orphan")


class FomoAlertEvent(Base):
    """Keyed FOMO /ws/alerts social flow. Learn only — never opens paper fills."""

    __tablename__ = "fomo_alert_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_fomo_alert_events_event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="")
    trader: Mapped[str] = mapped_column(String(128), default="", index=True)
    trader_wallet: Mapped[str] = mapped_column(String(64), default="")
    token_symbol: Mapped[str] = mapped_column(String(64), default="")
    mint: Mapped[str] = mapped_column(String(64), default="", index=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    alert_type: Mapped[str] = mapped_column(String(24), default="")
    usd_value: Mapped[float] = mapped_column(Float, default=0.0)
    trade_id: Mapped[str] = mapped_column(String(128), default="")
    text_snippet: Mapped[str] = mapped_column(String(512), default="")
    event_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    on_hunt: Mapped[bool] = mapped_column(Boolean, default=False)
    on_paper_v1: Mapped[bool] = mapped_column(Boolean, default=False)
    known_token: Mapped[bool] = mapped_column(Boolean, default=False)
    paper_v1_skipped: Mapped[bool] = mapped_column(Boolean, default=False)


class FomoWalletHit(Base):
    __tablename__ = "fomo_wallet_hits"
    __table_args__ = (UniqueConstraint("wallet_id", "token_id", name="uq_fomo_hits_wallet_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("fomo_wallets.id"), index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    mint: Mapped[str] = mapped_column(String(64), default="")
    symbol: Mapped[str] = mapped_column(String(32), default="")
    multiple: Mapped[float] = mapped_column(Float, default=0.0)
    pct: Mapped[float] = mapped_column(Float, default=0.0)
    is_win: Mapped[bool] = mapped_column(Boolean, default=True)

    wallet: Mapped[FomoWallet] = relationship(back_populates="hits")


class EarlyWallet(Base):
    """Wallets that bought in the first window of an analytics runner.

    Helius parsed swaps only. No extra GMGN HTTP.
    """

    __tablename__ = "early_wallets"
    __table_args__ = (UniqueConstraint("chain", "owner", name="uq_early_wallets_chain_owner"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)
    n_runners: Mapped[int] = mapped_column(Integer, default=0)
    n_still_in: Mapped[int] = mapped_column(Integer, default=0)
    n_profitable: Mapped[int] = mapped_column(Integer, default=0)
    n_sized: Mapped[int] = mapped_column(Integer, default=0)
    sum_sol_spent: Mapped[float] = mapped_column(Float, default=0.0)
    fastest_buy_s: Mapped[float] = mapped_column(Float, default=0.0)
    fomo_hits: Mapped[int] = mapped_column(Integer, default=0)
    best_symbol: Mapped[str] = mapped_column(String(32), default="")
    best_mint: Mapped[str] = mapped_column(String(64), default="")
    best_mark_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    hits: Mapped[list[EarlyWalletHit]] = relationship(back_populates="wallet", cascade="all, delete-orphan")


class EarlyWalletHit(Base):
    __tablename__ = "early_wallet_hits"
    __table_args__ = (UniqueConstraint("wallet_id", "token_id", name="uq_early_hits_wallet_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("early_wallets.id"), index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    mint: Mapped[str] = mapped_column(String(64), default="")
    symbol: Mapped[str] = mapped_column(String(32), default="")
    first_buy_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    age_at_buy_s: Mapped[float] = mapped_column(Float, default=0.0)
    sol_spent: Mapped[float] = mapped_column(Float, default=0.0)
    token_amount: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    mark_multiple: Mapped[float] = mapped_column(Float, default=0.0)
    still_holding: Mapped[bool] = mapped_column(Boolean, default=False)

    wallet: Mapped[EarlyWallet] = relationship(back_populates="hits")


class Decision(Base):
    """Append-only record of what the desk said at the moment it said it.

    One row per (chain, mint, kind). ``entry`` is the first score; ``line70``
    / ``line90`` are desk-line crossings; ``gate`` is the gated-90 verdict.
    Rows are never updated — the honest scoreboard, training set and paper
    ledger read these instead of the mutable ``research`` row.
    """

    __tablename__ = "decisions"
    __table_args__ = (UniqueConstraint("chain", "mint", "kind", name="uq_decisions_chain_mint_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    mint: Mapped[str] = mapped_column(String(64), default="", index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="entry", index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(16), default="live")  # live | seed_t0
    entry_p: Mapped[float] = mapped_column(Float, default=0.0)
    heuristic_p: Mapped[float] = mapped_column(Float, default=0.0)
    model_p: Mapped[float] = mapped_column(Float, default=0.0)
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    features_hash: Mapped[str] = mapped_column(String(40), default="")
    entry_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    liq: Mapped[float] = mapped_column(Float, default=0.0)
    vol_h1: Mapped[float] = mapped_column(Float, default=0.0)
    holders: Mapped[int] = mapped_column(Integer, default=0)
    flags_json: Mapped[str] = mapped_column(Text, default="[]")
    veto: Mapped[str] = mapped_column(String(64), default="")
    image_rev: Mapped[str] = mapped_column(String(32), default="")
    model_version: Mapped[int] = mapped_column(Integer, default=0)
    scorer: Mapped[str] = mapped_column(String(16), default="legacy")


class PaperFill(Base):
    """Persisted gated-90 paper fill. Opened once, closed once, never re-derived."""

    __tablename__ = "paper_fills"
    __table_args__ = (UniqueConstraint("chain", "mint", "line", name="uq_paper_fills_chain_mint_line"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    mint: Mapped[str] = mapped_column(String(64), default="", index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    line: Mapped[str] = mapped_column(String(16), default="gated90")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    entry_p: Mapped[float] = mapped_column(Float, default=0.0)
    entry_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    entry_liq: Mapped[float] = mapped_column(Float, default=0.0)
    target: Mapped[float] = mapped_column(Float, default=2.0)
    ride: Mapped[float] = mapped_column(Float, default=10.0)
    max_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    min_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    last_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    last_liq: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open | closed | queued | skipped
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str] = mapped_column(String(32), default="")
    open_via: Mapped[str | None] = mapped_column(String(16), default=None)
    return_pct: Mapped[float | None] = mapped_column(Float)
    image_rev: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Ticket(Base):
    """Shadow buy ticket. The interface a future executor calls. No orders here."""

    __tablename__ = "tickets"
    __table_args__ = (UniqueConstraint("chain", "mint", name="uq_tickets_chain_mint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    mint: Mapped[str] = mapped_column(String(64), default="", index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    fill_id: Mapped[int | None] = mapped_column(ForeignKey("paper_fills.id"))
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    symbol: Mapped[str] = mapped_column(String(32), default="")
    entry_p: Mapped[float] = mapped_column(Float, default=0.0)
    entry_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    liq: Mapped[float] = mapped_column(Float, default=0.0)
    size_usd: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_pct: Mapped[float] = mapped_column(Float, default=0.0)
    stop_mult: Mapped[float] = mapped_column(Float, default=0.5)
    take_mult: Mapped[float] = mapped_column(Float, default=2.0)
    ride_mult: Mapped[float] = mapped_column(Float, default=10.0)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    flags_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default="shadow", index=True)  # shadow | confirmed | skipped
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    image_rev: Mapped[str] = mapped_column(String(32), default="")


class TapeBar(Base):
    """One-minute Hunt tape print. Kept ~48h; feeds drawdown, fills and the Live model."""

    __tablename__ = "tape_bars"
    __table_args__ = (UniqueConstraint("chain", "mint", "minute", name="uq_tape_bars_chain_mint_minute"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    mint: Mapped[str] = mapped_column(String(64), default="", index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mcap_usd: Mapped[float] = mapped_column(Float, default=0.0)
    price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity_usd: Mapped[float] = mapped_column(Float, default=0.0)
    volume_h1: Mapped[float] = mapped_column(Float, default=0.0)
    holders: Mapped[int] = mapped_column(Integer, default=0)


class LiveSample(Base):
    """Tape state ~15 minutes after an entry decision. Training row for the Live model."""

    __tablename__ = "live_samples"
    __table_args__ = (UniqueConstraint("chain", "mint", name="uq_live_samples_chain_mint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    mint: Mapped[str] = mapped_column(String(64), default="", index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    minutes_after: Mapped[float] = mapped_column(Float, default=0.0)
    entry_p: Mapped[float] = mapped_column(Float, default=0.0)
    entry_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    mcap_usd: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity_usd: Mapped[float] = mapped_column(Float, default=0.0)
    volume_h1: Mapped[float] = mapped_column(Float, default=0.0)
    holders: Mapped[int] = mapped_column(Integer, default=0)
    peak_before: Mapped[float] = mapped_column(Float, default=0.0)
    trough_before: Mapped[float] = mapped_column(Float, default=0.0)
    features_json: Mapped[str] = mapped_column(Text, default="{}")


class ModelArtifact(Base):
    """Versioned batch fit with its forward metrics. Promotion copies weights to ModelState."""

    __tablename__ = "model_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    kind: Mapped[str] = mapped_column(String(16), default="entry", index=True)  # entry | live
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    weights_json: Mapped[str] = mapped_column(Text, default="{}")
    bias: Mapped[float] = mapped_column(Float, default=0.0)
    calibration_json: Mapped[str] = mapped_column(Text, default="[]")
    n_train: Mapped[int] = mapped_column(Integer, default=0)
    n_valid: Mapped[int] = mapped_column(Integer, default=0)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    incumbent_json: Mapped[str] = mapped_column(Text, default="{}")
    promoted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class HuntCard(Base):
    """This-window hunt index. Desk reads these rows, not a 12.8k leftover-sort."""

    __tablename__ = "hunt_cards"
    __table_args__ = (UniqueConstraint("chain", "mint", name="uq_hunt_cards_chain_mint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), default="sol", index=True)
    mint: Mapped[str] = mapped_column(String(64), default="", index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_p: Mapped[float] = mapped_column(Float, default=0.0)
    conviction_p: Mapped[float] = mapped_column(Float, default=0.0)
    t0_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    last_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    multiple: Mapped[float] = mapped_column(Float, default=0.0)
    last_liq: Mapped[float] = mapped_column(Float, default=0.0)
    holders: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Frozen with entry_p at first insert; the desk lines read against it.
    scorer: Mapped[str] = mapped_column(String(16), default="legacy")
