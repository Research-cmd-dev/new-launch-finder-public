from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..chains import normalize_chain
from ..models import ModelState, Outcome, Research, Token
from .features import (
    FEATURE_NAMES,
    feature_vector,
    heuristic_probability,
    is_brand_clone_book,
    is_copycat_dump_book,
    is_copycat_flood_book,
    is_funder_rug_book,
    is_staged_social_book,
    is_organic_book,
    rh_lp_open_clears_paper,
    social_ticket_cap,
    stall_honesty_cap,
)
from .preview import PREVIEW_FEATURE, PREVIEW_WEIGHT


def _sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def get_or_create_model(session: Session, chain: str = "sol") -> ModelState:
    chain = normalize_chain(chain)
    row = (
        session.query(ModelState)
        .filter(ModelState.chain == chain)
        .order_by(ModelState.id.desc())
        .first()
    )
    if row:
        return row
    # Pre-migration row has chain defaulted to sol after ALTER; still accept
    # a single legacy row with a blank chain as the Solana book.
    if chain == "sol":
        row = session.query(ModelState).filter((ModelState.chain.is_(None)) | (ModelState.chain == "")).order_by(ModelState.id.desc()).first()
        if row:
            row.chain = "sol"
            return row
    row = ModelState(chain=chain, version=1, weights_json=json.dumps({n: 0.0 for n in FEATURE_NAMES}), bias=-0.4)
    session.add(row)
    session.flush()
    return row


# Solana blend ramps in over 80 labels. RH is still a leftover-poisoned
# 24h-loss book. Floor held through 160 / 240 / 360 / 480 / 720 / 840 /
# 960 / 1080 / 1200 / 1320 / 1440 / 1560 / 1680 / 1800 / 1920 / 2040 /
# 2160 / 2280 / 2400 / 2520 / 2640 / 2760 / 2880 / 3000 / 3120 / 3240 /
# 3360 / 3480 / 3600 / 3720 / 3840 / 3960 / 4080 / 4200 / 4320 / 4440 /
# 4560 / 4680 / 4800 / 5040. Live 01:08: n_train 4878 / 0.7-0.8
# actual 0.172 — 4800 never held (n_train was already 4803). Live
# 01:20: n_train 5001 already past 4920. Live 01:47: n_train 5195
# / RECT 0.80→0.48 — 5040 and 5160 already lapsed. Live 02:20:
# n_train 5359 / 5280 already lapsed and 5400 already passed.
# Live 02:47: n_train 5570 / 5520 already lapsed and 5640 already
# passed. Live 03:40: n_train 5763 / 5760 already lapsed. Live
# 04:07: n_train 5890 / 5880 already lapsed. Live 05:00:
# n_train 6011 / 6000 already lapsed. PONSANIME desk 0.11
# vs heur 0.68. Live 05:05: hold 6120. Live 05:51: n_train
# 6138 — 6120 already lapsed. Live 06:22: n_train 6271 —
# 6240 already lapsed. Live 06:39: hold 6360. Live 07:02:
# n_train 6382 — 6360 already lapsed. Live 07:09: hold 6480.
# Live 07:40: n_train 6493 — 6480 already lapsed. HORNY desk
# 0.086 vs heur 0.51. Live 07:44: hold 6600. Live 08:10:
# n_train 6605 — 6600 already lapsed. HORNY desk 8.6 again.
# Hold through 6720. Live 09:26: n_train 6840 — 6840 already
# lapsed. HORNY desk 8.6 vs heur 0.51 + stall. Hold 6960.
# Live 10:00: n_train 6974 — 6960 already lapsed. HORNY desk
# 8.6 again vs heur 0.51. Hold 7080. Do not jump two steps.
# Live 10:40: n_train 7091 — 7080 already lapsed. HORNY desk
# 8.6 again vs heur 0.51. Hold 7200. Do not jump two steps.
# Live 11:40: n_train 7204 — 7200 already lapsed. OWL 12.1 /
# ROBINMUSK 11.2 vs heur after the floor dropped. Hold 7320.
# Live 12:50: n_train 7343 — 7320 already lapsed. OWL 12.1 /
# ROBINMUSK 11.2 again vs heur. Hold 7440. Do not jump two
# steps. Live 13:20: n_train 7452 — 7440 already lapsed. OWL
# 12.1 / ROBINMUSK 11.2 / RAIN 14.4 / TIQS 14.3 vs heur
# again. Hold 7560. Do not jump two steps. Live 14:18:
# n_train 7605 — 7560 already lapsed. TIQS 14.1 /
# NANOCHICKLET 10.0 / OWL 12.1 / ROBINMUSK 11.2 vs heur
# again. Hold 7680. Do not jump two steps. Live 14:50:
# n_train 7704 — 7680 already lapsed. TIQS 14.1 /
# NANOCHICKLET 10.0 / OWL 10.2 / ROBINMUSK 11.2 vs heur
# again. Hold 7800. Do not jump two steps. Live 15:36:
# Hold 7920. Do not jump two steps. Live 16:06: n_train 7924 —
# 7920 already lapsed. TIQS 14.0 / NANOCHICKLET 10.0 / HJ 9.8 /
# GROYPER 58 vs heur again. Hold 8040. Do not jump two steps.
# Live 16:41: n_train 8048 — 8040 already lapsed. Hold 8160.
# Do not jump two steps. Live 17:22: n_train 8206 — 8160
# already lapsed. GROYPER 0.08 / NANO 0.10 vs heur again.
# Hold 8280. Do not jump two steps. Live 17:43: n_train 8294 —
# 8280 already lapsed. GROYPER 0.08 / NANO 0.10 / HJ 0.10 vs
# heur again. Hold 8400. Do not jump two steps. Live 18:04:
# n_train 8410 — 8400 already lapsed. GROYPER 0.08 / NANO 0.10
# / HJ 0.10 / MINOXIDIL 0.19 vs heur again. Hold 8520. Do not
# jump two steps. Live 18:33: n_train 8540 — 8520 already
# lapsed. GROYPER 0.08 / NANO 0.10 / HJ 0.10 vs heur again.
# Hold 8640. Do not jump two steps. Live 18:47: n_train 8659
# — 8640 already lapsed. GROYPER 0.08 / NANO 0.10 / HJ 0.10
# / MINOXIDIL 0.15 vs heur again. Hold 8760. Do not jump
# two steps. Live 19:13: n_train 8776 — 8760 already lapsed.
# GROYPER 0.08 / NANO 0.10 / MINOXIDIL 0.15 vs heur again.
# Hold 8880. Do not jump two steps. Live 19:35: n_train 8896 —
# 8880 already lapsed. GROYPER 0.24 / NANO 0.10 / MINOXIDIL 0.15
# vs heur again. Hold 9000. Do not jump two steps. Live 19:58:
# n_train 9016 — 9000 already lapsed. GROYPER 0.24 / NANO 0.10 /
# MINOXIDIL 0.15 vs heur again. Hold 9120. Do not jump two steps.
# Live 20:33: n_train 9164 — 9120 already lapsed. GROYPER 0.24 /
# NANO 0.10 / MINOXIDIL 0.15 vs heur again. Hold 9240. Do not
# jump two steps. Live 20:57: n_train 9306 — 9240 already lapsed.
# GROYPER 0.24 / NANO 0.10 / MINOXIDIL 0.15 vs heur again. Hold
# 9360. Do not jump two steps. Live 21:13: n_train 9367 — 9360
# already lapsed. GROYPER 0.13 / NANO 0.10 / MINOXIDIL 0.15 vs
# heur again. Hold 9480. Do not jump two steps. Live 21:38:
# n_train 9493 — 9480 already lapsed. GROYPER / NANO /
# MINOXIDIL 0.48 stall vs heur again. Hold 9600. Do not
# jump two steps. Live 21:55: n_train 9647 — 9600 already
# lapsed. MINOXIDIL 0.48 stall vs heur again. Hold 9720.
# Do not jump two steps. Live 22:14: n_train 9823 — 9720
# already lapsed. MINOXIDIL 0.48 stall vs heur again.
# Hold 9840. Do not jump two steps. Live 22:28: n_train
# 9902 — 9840 already lapsed. MINOXIDIL 0.48 stall vs
# heur again. Hold 9960. Do not jump two steps.
# Live 22:48: n_train 10024 — 9960 already lapsed.
# MINOXIDIL 0.48 stall vs heur again. Hold 10080.
# Do not jump two steps. Live 23:09: n_train 10140 —
# 10080 already lapsed. MINOXIDIL 0.48 stall vs heur
# again. Hold 10200. Do not jump two steps.
# Live 23:36: n_train 10269 — 10200 already lapsed.
# MINOXIDIL 0.48 stall vs heur again. Hold 10320.
# Do not jump two steps. Live 23:52: n_train 10341 —
# 10320 already lapsed. MINOXIDIL 0.48 stall vs heur
# again. Hold 10440. Do not jump two steps.
# Live 00:16: n_train 10451 — 10440 already lapsed.
# MINOXIDIL 0.27 vs heur after the floor dropped.
# Hold 10560. Do not jump two steps.
# Live 00:36: n_train 10575 — 10560 already lapsed.
# Ladybonercoin 0.16 / MINOXIDIL 0.27 vs heur after
# the floor dropped. Hold 10680. Do not jump two steps.
# Live 00:53: n_train 10687 — 10680 already lapsed.
# Hold 10800. Do not jump two steps.
# Live 01:30: n_train 10813 — 10800 already lapsed.
# Hold 10920. Do not jump two steps.
# Live 02:00: n_train 10960 — 10920 already lapsed.
# Hold 11040. Do not jump two steps.
# Live 03:15: n_train 11180 — 11040 and 11160 already
# lapsed. Hold 11280 (two steps; already past 11160).
# Live 04:12: n_train 11334 — 11280 already lapsed.
# Hold 11400. Do not jump two steps.
# Live 04:40: n_train 11518 — 11400 already lapsed.
# Hold 11520. Do not jump two steps.
# Live 05:00: n_train 11654 — 11520 already lapsed
# and 11640 already passed. Hold 11760 (two
# steps; already past 11640). Do not jump to
# 11880 unless already past 11760.
# Live 05:20: n_train 11761 — 11760 already lapsed.
# Hold 11880. Do not jump two steps.
# Live 06:00: n_train 11938 — 11880 already lapsed.
# Hold 12000. Do not jump two steps.
# Live 06:20: n_train 12079 — 12000 already lapsed.
# Hold 12120. Do not jump two steps.
# Live 06:40: n_train 12150 — 12120 already lapsed.
# Hold 12240. Do not jump two steps.
# Live 07:00: n_train 12242 — 12240 already lapsed.
# Hold 12360. Do not jump two steps.
# Live 07:20: n_train 12367 — 12360 already lapsed.
# Hold 12480. Do not jump two steps.
# Live 08:00: n_train 12538 — 12480 already lapsed.
# Hold 12600. Do not jump two steps.
# Live 08:20: n_train 12606 — 12600 already lapsed.
# Hold 12720. Do not jump two steps.
# Live 09:00: n_train 12740 — 12720 already lapsed.
# Hold 12840. Do not jump two steps.
# Live 09:40: n_train 12865 — 12840 already lapsed.
# Hold 12960. Do not jump two steps.
# Live 10:20: n_train 12984 — 12960 already lapsed.
# Hold 13080. Do not jump two steps.
# Live 10:40: n_train 13039 — still under 13080.
# Hold 13080. Next hold 13200 only if n_train PASSES 13080.
# Live 11:00: n_train 13081 — 13080 already lapsed.
# Hold 13200. Do not jump two steps.
# Live 11:20: n_train 13128 — still under 13200.
# Hold 13200. Next hold 13320 only if n_train PASSES 13200.
# Live 11:41: n_train 13185 — still under 13200.
# Hold 13200. Next hold 13320 only if n_train PASSES 13200.
# Live 11:48: n_train 13207 — 13200 already lapsed (QUANT
# p=0.13 vs hp 0.61). Hold 13320. Do not jump two steps.
# Live 12:04: n_train 13245 — still under 13320.
# Hold 13320. Next hold 13440 only if n_train PASSES 13320.
# Live 12:21: n_train 13279 — still under 13320.
# Hold 13320. Next hold 13440 only if n_train PASSES 13320.
# Live 12:40: n_train 13344 — 13320 already lapsed (QUANT
# p=0.17 vs hp 0.61). Hold 13440. Do not jump two steps.
# Live 13:00: n_train 13450 — 13440 already lapsed.
# Hold 13560. Next hold 13680 only if n_train PASSES 13560.
# Live 14:09: n_train 13583 — 13560 already lapsed. Hold 13680.
# Live 15:01: n_train 13750 — 13680 already lapsed. Hold 13800.
# Live 15:21: n_train 13806 — 13800 already lapsed. Hold 13920.
# Live 16:22: n_train 13976 — 13920 already lapsed. Hold 14040.
# Live 16:43: n_train 14046 — 14040 already lapsed. Hold 14160.
# Live 17:30: n_train 14258 — 14160 already lapsed. Hold 14280.
# Live 17:53: n_train 14363 — 14280 already lapsed. Hold 14400.
# Live 18:44: n_train 14537 — 14520 already lapsed. Hold 14640.
# Live 19:32: n_train 14677 — 14640 already lapsed. Hold 14760.
# Live 19:54: n_train 14765 — 14760 already lapsed. Hold 14880.
# Live 20:47: n_train 14950 — 14880 already lapsed. Hold 15000.
# Live 21:11: n_train 15041 — 15000 already lapsed. Hold 15120.
# Live 21:33: n_train 15121 — 15120 already lapsed. Hold 15240.
# Live 21:55: n_train 15205 — still under 15240. Hold 15240.
# Live 22:10: n_train 15247 — 15240 already lapsed. Hold 15360.
# Live 22:27: n_train 15333 — still under 15360. Hold 15360.
# Live 22:49: n_train 15370 — 15360 already lapsed. Hold 15480.
# Live 23:12: n_train 15401 — still under 15480. Hold 15480.
# Live 23:33: n_train 15436 — still under 15480. Hold 15480.
# Live 23:55: n_train 15519 — 15480 already lapsed. Hold 15600.
# Live 00:46: n_train 15606 — 15600 already lapsed. Hold 15720.
# Live 01:08: n_train 15639 — still under 15720. Hold 15720.
# Live 01:29: n_train 15702 / 15717 — still under 15720. Hold 15720.
# Live 01:50: n_train 15756 — 15720 already lapsed. Hold 15840.
# Live 02:13: n_train 15803 — still under 15840. Hold 15840.
# Live 02:33: n_train 15862 — 15840 already lapsed. Hold 15960.
# Live 02:54: n_train 15882 / 15900 — still under 15960. Hold 15960.
# Live 03:12: n_train 15913 — still under 15960. Hold 15960.
# Live 03:30: n_train 15958 — still under 15960. Hold 15960.
# Live 03:49: n_train 16036 — 15960 already lapsed. Hold 16080.
# Live 04:07: n_train 16083 — 16080 already lapsed. Hold 16200.
# Live 04:26: n_train 16137 / 16151 — still under 16200. Hold 16200.
# Live 05:20: n_train 16242 — still under 16320. Hold 16320.
# Live 05:40: n_train 16275 — still under 16320. Hold 16320.
# Live 06:00: n_train 16348 — 16320 already lapsed. Hold 16440.
# Live 06:20: n_train 16390 — still under 16440. Hold 16440.
# Live 06:40: n_train 16459 — 16440 already lapsed. Hold 16560.
# Live 07:00: n_train 16520 / 16528 — still under 16560. Hold 16560.
# Live 07:20: n_train 16561 — 16560 already lapsed. Hold 16680.
# Live 07:40: n_train 16642 — still under 16680. Hold 16680.
# Live 08:00: n_train 16709 — 16680 already lapsed. Hold 16800.
# Live 08:20: n_train 16752 / 16757 — still under 16800. Hold 16800.
# Live 08:40: n_train 16809 — 16800 already lapsed. Hold 16920.
# Live 09:00: n_train 16857 — still under 16920. Hold 16920.
# Live 09:20: n_train 16901 — still under 16920. Hold 16920.
# Live 09:40: n_train 16955 / 16961 — 16920 already lapsed. Hold 17040.
# Live 10:00: n_train 17011 / 17015 — still under 17040. Hold 17040.
# Live 10:20: n_train 17059 / 17066 — 17040 already lapsed. Hold 17160.
# Live 10:40: n_train 17086 / 17087 — still under 17160. Hold 17160.
# Live 11:00: n_train 17149 / 17150 — still under 17160. Hold 17160.
# Live 11:20: n_train 17199 / 17200 — 17160 already lapsed. Hold 17280.
# Live 11:40: n_train 17245 / 17246 — still under 17280. Hold 17280.
# Live 12:00: n_train 17281 / 17283 — 17280 already lapsed. Hold 17400.
# Live 12:20: n_train 17318 / 17319 — still under 17400. Hold 17400.
# Live 12:40: n_train 17364 / 17365 — still under 17400. Hold 17400.
# Live 13:00: n_train 17384 / 17385 — still under 17400. Hold 17400.
# Live 13:20: n_train 17399 at fire start / 17406 mid-fire — 17400
# already lapsed. Hold 17520. Do not jump two steps.
# Live 13:40: n_train 17424 / 17425 — still under 17520. Hold 17520.
# Live 14:00: n_train 17462 / 17463 — still under 17520. Hold 17520.
# Live 14:20: n_train 17509 / 17510 — still under 17520. Hold 17520.
# Live 14:40: n_train 17553 / 17554 — 17520 already lapsed. Hold 17640.
# Do not jump two steps.
# Live 15:00: n_train 17600 / 17601 — still under 17640. Hold 17640.
# Live 15:20: n_train 17630 / 17631 — still under 17640. Hold 17640.
# Live 15:40: n_train 17665 / 17666 — 17640 already lapsed. Hold 17760.
# Do not jump two steps.
# Live 16:00: n_train 17700 / 17701 — still under 17760. Hold 17760.
# Live 16:20: n_train 17743 / 17744 — still under 17760. Hold 17760.
# Live 16:40: n_train 17779 / 17780 — 17760 already lapsed. Hold 17880.
# Do not jump two steps.
# Live 17:00: n_train 17808 / 17809 — still under 17880. Hold 17880.
# Live 17:20: n_train 17839 / 17840 — still under 17880. Hold 17880.
# Live 17:40: n_train 17894 / 17895 — 17880 already lapsed. Hold 18000.
# Do not jump two steps.
# Live 18:00: n_train 17962 / 17963 — still under 18000. Hold 18000.
# Live 18:20: n_train 18023 / 18024 — 18000 already lapsed. Hold 18120.
# Do not jump two steps.
# Live 18:40: n_train 18084 / 18085 — still under 18120. Hold 18120.
# Live 19:00: n_train 18165 / 18166 — 18120 already lapsed. Hold 18240.
# Do not jump two steps.
# Live 19:20: n_train 18240 / 18241 — still at 18240, does not pass.
# Hold 18240. Next hold 18360 only if n_train PASSES 18240.
# Live 19:40: n_train 18306 / 18307 — 18240 already lapsed. Hold 18360.
# Do not jump two steps.
# Live 20:00: n_train 18378 / 18379 — 18360 already lapsed. Hold 18480.
# Do not jump two steps.
# Live 20:20: n_train 18459 / 18460 — still under 18480. Hold 18480.
# Live 20:40: n_train 18545 / 18546 — 18480 already lapsed. Hold 18600.
# Do not jump two steps.
# Live 21:00: n_train 18600 / 18601 — equal does not pass 18600.
# Hold 18600. Next hold 18720 only if n_train PASSES 18600.
# Live 21:20: n_train 18646 / 18647 — 18600 already lapsed. Hold 18720.
# Do not jump two steps.
# Live 21:40: n_train 18704 / 18705 — still under 18720. Hold 18720.
# Next hold 18840 only if n_train PASSES 18720.
# Live 22:00: n_train 18750 / 18751 — 18720 already lapsed. Hold 18840.
# Do not jump two steps.
# Live 22:20: n_train 18783 / 18784 — still under 18840. Hold 18840.
# Next hold 18960 only if n_train PASSES 18840.
# Live 22:40: n_train 18829 / 18830 — still under 18840. Hold 18840.
# Next hold 18960 only if n_train PASSES 18840.
# Live 23:00: n_train 18874 / 18875 — 18840 already lapsed. Hold 18960.
# Do not jump two steps.
# Live 23:20: n_train 18932 / 18945 — still under 18960. Hold 18960.
# Next hold 19080 only if n_train PASSES 18960.
# Live 23:40: n_train 18964 / 18964 — 18960 already lapsed. Hold 19080.
# Do not jump two steps.
# Live 00:40: n_train 19090 / 19091 — 19080 already lapsed. Hold 19200.
# Do not jump two steps.
# Live 01:00: n_train 19169 / 19170 — still under 19200. Hold 19200.
# Next hold 19320 only if n_train PASSES 19200.
# Live 01:20: n_train 19202 / 19203 — 19200 already lapsed. Hold 19320.
# Do not jump two steps.
# Live 01:40: n_train 19270 / 19271 — still under 19320. Hold 19320.
# Next hold 19440 only if n_train PASSES 19320.
# Live 02:00: n_train 19339 / 19340 — 19320 already lapsed. Hold 19440.
# Do not jump two steps.
# Live 02:20: n_train 19393 / 19394 — still under 19440. Hold 19440.
# Next hold 19560 only if n_train PASSES 19440.
# Live 02:40: n_train 19430 / 19431 — still under 19440. Hold 19440.
# Live 03:00: n_train 19455 / 19456 — 19440 already lapsed. Hold 19560.
# Do not jump two steps.
# Live 2026-09-14 22:52: n_train 46383 / 0.9-bin actual 0.256 /
# base win 0.013 / top-decile 0.06. Hold 19560 lapsed by ~26k
# leftover labels. New RH ingest is 85% leftover model. Hold 48000.
YOUNG_RH_N_TRAIN = 48000


def blend_probability(heuristic_p: float, model_p: float, n_train: int) -> float:
    # Until the model has enough labels, trust the rules more.
    w = min(0.85, n_train / 80.0)
    return (1.0 - w) * heuristic_p + w * model_p


def predict(session: Session, features: dict[str, float], *, chain: str = "sol") -> dict[str, Any]:
    reasons: list[str] = []
    flags: list[str] = []
    heuristic = heuristic_probability(features, reasons, flags)
    model = get_or_create_model(session, chain=chain)
    weights = json.loads(model.weights_json or "{}")
    z = float(model.bias)
    for name in FEATURE_NAMES:
        z += float(weights.get(name, 0.0)) * float(features.get(name, 0.0))
    prev = float(features.get(PREVIEW_FEATURE) or 0.0)
    if prev > 0:
        z += float(weights.get(PREVIEW_WEIGHT, 0.0)) * prev
    model_p = _sigmoid(z)
    # Phase 1: a promoted batch artifact carries an isotonic map fitted on
    # a forward validation slice. The raw sigmoid is a rank, not a rate.
    from .batch_fit import apply_calibration, promoted_calibration

    cal = promoted_calibration(session, chain)
    if cal:
        model_p = apply_calibration(cal, model_p)
    p = blend_probability(heuristic, model_p, model.n_train)
    # Disagreement guard: the model can refine the rules but not overrule
    # them wholesale. USWS scored 0.93 from the model against 0.30 from the
    # rules (bundle flags) — a pattern the model had learned to love because
    # bundled pumps print paper multiples. The rules keep veto power.
    # Live 15:10 Sol: JUST heur 0.47 / model 1.00 / blend 0.82 paper-bought
    # a 1.00× bundle. 0.6–0.8 bins win 33–44%. +0.18 keeps a 0.52
    # heuristic on the 0.70 paper line and leaves organic floors intact.
    p = min(p, heuristic + 0.18)
    # Live 13:50: INF 0.89 vs heur 0.62 after the 2040 floor lapsed.
    # RH high-p bins win ~10%. Do not let the model add a full 35 pts.
    if normalize_chain(chain) == "robinhood":
        p = min(p, heuristic + 0.15)
    # Live SANDIH: heuristic 0.51 / model 0.22 / blend 0.43. Honest 5.4x
    # ($39k → $113k / 60 holders) missed paper and capture. Until RH has
    # 80 labels the rules are the floor.
    if normalize_chain(chain) == "robinhood" and model.n_train < YOUNG_RH_N_TRAIN:
        p = max(p, heuristic)
    # Sol n_train is thousands; blend weight is 0.85 model. The model learned
    # "serial / fresh / no-smart-yet" as bearish and buries the organic
    # 10–20x class (rehanfal 0.54, MACRODUCK 0.31) even after the rules
    # lift them. A live book is an independent system — do not let the
    # blend talk it under the 0.60 capture line.
    organic = is_organic_book(features) or bool(features.get("organic_book"))
    if organic and heuristic >= 0.60:
        p = max(p, heuristic)
    # Live MEME: leftover-poisoned model printed 0.006 against a
    # fair LP open (heuristic ~0.63). Young floor holds until 14160,
    # then w=0.85 would bury the next one at ~0.10. Paper reads 0.50.
    # SHORT/PLUMBED stay thin-capped at 0.48 below. FEATURE_NAMES 66.
    if normalize_chain(chain) == "robinhood" and rh_lp_open_clears_paper(features):
        p = max(p, heuristic)
    # Sol paper uses t0 snapshot p (min 0.70) so this only moves capture
    # and the live desk. Live PVP was organic-wide after the vol OR but
    # heuristic 0.29 / blend 0.36 — still under the 0.60 flag line.
    # Do not floor Robinhood: paper reads research.p_good at 0.50.
    if organic and normalize_chain(chain) == "sol" and heuristic >= 0.25:
        p = max(p, 0.60)
    # Live ELEGANS/HTD/STARMAN: 1–13 wallets, heuristic already 0.48, but
    # n_train 165 / w=0.85 blended model_p 0.67 back to 0.64 and paper
    # would buy. The thin/empty caps are paper-line vetoes — apply after
    # the young-model floor, not only inside the heuristic.
    if (
        features.get("rh_thin_book")
        or features.get("rh_empty_book")
        or features.get("rh_airdrop_book")
        or features.get("rh_bot_dump")
    ):
        p = min(p, 0.48)
    # After the Sol organic 0.60 floor. Live Google Gemini was a
    # clean-looking 836-wallet book on @GeminiApp + copycat flood.
    if is_brand_clone_book(features):
        if p >= 0.50:
            flags.append("Claimed celebrity/brand X — not this launch")
        p = min(p, 0.48)
    # After the Sol organic 0.60 floor. Live JubJub: copycat + dump
    # on a fat book, heuristic 92 / model 99. Same 0.48 paper veto.
    if is_copycat_dump_book(features):
        p = min(p, 0.48)
    # Live OpenAI: copycat-only, no dump, fat organic book, Entry 90.
    if is_copycat_flood_book(features):
        p = min(p, 0.48)
    # Live Pumpball: funder-rug + organic book, heuristic 92 / model 2%.
    if is_funder_rug_book(features):
        p = min(p, 0.48)
    if is_staged_social_book(features):
        p = min(p, 0.48)
    # Live USWR: late Dex $5M t0 looked organic; Sol floor lifted
    # a 0.48 start-high card to 0.60 and Hunt showed Entry 62.
    if features.get("entry_premium"):
        p = min(p, 0.48)
    if normalize_chain(chain) == "sol":
        ticket = social_ticket_cap(p, features)
        if ticket + 1e-4 < p:
            flags.append("Social card without a live tape — under paper")
        p = ticket
    stalled = stall_honesty_cap(
        p,
        age_min=float(features.get("age_min") or 0),
        multiple=float(features.get("live_multiple") or 0),
    )
    if stalled + 1e-4 < p:
        flags.append("Score faded — still under 2x after sitting")
        p = stalled
    return {
        "p_good": round(p, 4),
        "heuristic_p": round(heuristic, 4),
        "model_p": round(model_p, 4),
        "reasons": reasons,
        "risk_flags": flags,
        "n_train": model.n_train,
        "model_version": model.version,
    }


POSITIVE_WEIGHT = 2.5  # missing a runner costs more than researching a dud


def online_update(session: Session, features: dict[str, float], label: int, lr: float = 0.08, l2: float = 2e-3, *, chain: str = "sol") -> ModelState:
    if label == 1:
        # Under the 3x definition positives are scarce; weight them so the
        # model optimizes recall on the class we hunt.
        lr = lr * POSITIVE_WEIGHT
    model = get_or_create_model(session, chain=chain)
    weights = json.loads(model.weights_json or "{}")
    x = feature_vector(features)
    z = float(model.bias)
    for name, xi in zip(FEATURE_NAMES, x, strict=True):
        z += float(weights.get(name, 0.0)) * xi
    prev = float(features.get(PREVIEW_FEATURE) or 0.0)
    pw = float(weights.get(PREVIEW_WEIGHT, 0.0))
    if prev > 0:
        z += pw * prev
    p = _sigmoid(z)
    err = p - float(label)
    # L2 decay keeps weights bounded: with ~40 correlated features and an
    # online stream, undecayed logistic weights drift until single features
    # saturate the sigmoid.
    for name, xi in zip(FEATURE_NAMES, x, strict=True):
        w = float(weights.get(name, 0.0))
        weights[name] = w - lr * (err * xi + l2 * w)
    if prev > 0:
        weights[PREVIEW_WEIGHT] = pw - lr * (err * prev + l2 * pw)
    model.bias = float(model.bias) - lr * err
    model.weights_json = json.dumps(weights)
    model.n_train += 1
    pred = 1 if p >= 0.5 else 0
    if pred == label:
        model.n_correct += 1
    model.version += 1
    model.updated_at = datetime.now(timezone.utc)
    session.add(model)
    return model


def train_pending(session: Session) -> int:
    # Lazy: outcomes imports train_pending at module load.
    from .outcomes import is_rh_leftover_fdv

    rows = (
        session.query(Outcome, Research, Token)
        .join(Research, Research.token_id == Outcome.token_id)
        .join(Token, Token.id == Outcome.token_id)
        .filter(Outcome.label.is_not(None), Outcome.used_for_train.is_(False))
        .all()
    )
    from .batch_fit import has_promoted

    trained = 0
    frozen: dict[str, bool] = {}
    for outcome, research, token in rows:
        try:
            features = json.loads(research.features_json or "{}")
        except json.JSONDecodeError:
            continue
        if not features:
            continue
        # Leftover-FDV 24h losses (MORDOR $161k / 5 holders) are not a market.
        # Mark consumed so we do not retry, but do not update weights.
        holders = int(getattr(research, "holder_count", 0) or 0)
        if is_rh_leftover_fdv(outcome, holders, chain=token.chain or "sol"):
            outcome.used_for_train = True
            continue
        chain = normalize_chain(token.chain or "sol")
        # Once a batch artifact is promoted the weights belong to it. Online
        # SGD on repaired features would drift them; the hourly refit owns
        # learning from here.
        if chain not in frozen:
            frozen[chain] = has_promoted(session, chain)
        if frozen[chain]:
            outcome.used_for_train = True
            continue
        online_update(session, features, int(outcome.label), chain=chain)
        outcome.used_for_train = True
        trained += 1
    return trained


def _labeled_rows(session: Session, chain: str | None = None):
    q = (
        session.query(Research.p_good, Outcome.label)
        .join(Outcome, Outcome.token_id == Research.token_id)
        .join(Token, Token.id == Research.token_id)
        .filter(Outcome.label.is_not(None))
    )
    if chain:
        q = q.filter(Token.chain == normalize_chain(chain))
    return q.order_by(Research.id.desc()).limit(8_000).all()


def evaluate(session: Session, chain: str | None = "sol") -> dict[str, Any]:
    """Brier + decile precision on resolved outcomes. Accuracy alone is
    misleading at a ~50% base rate."""
    rows = _labeled_rows(session, chain)
    if not rows:
        return {"n": 0}
    n = len(rows)
    base = sum(label for _, label in rows) / n
    brier = sum((p - label) ** 2 for p, label in rows) / n
    ranked = sorted(rows, key=lambda r: r[0], reverse=True)
    k = max(1, n // 10)
    return {
        "n": n,
        "base_win_rate": round(base, 3),
        "brier": round(brier, 4),
        "precision_top_decile": round(sum(label for _, label in ranked[:k]) / k, 3),
        "win_rate_bottom_decile": round(sum(label for _, label in ranked[-k:]) / k, 3),
    }


def calibration_bins(session: Session, width: float = 0.1, chain: str | None = "sol") -> list[dict[str, Any]]:
    """Reliability diagram data: does p=0.8 actually win ~80% of the time?"""
    rows = _labeled_rows(session, chain)
    bins: dict[int, list[tuple[float, int]]] = {}
    for p, label in rows:
        idx = min(int(p / width), int(1.0 / width) - 1)
        bins.setdefault(idx, []).append((p, label))
    out = []
    for idx in sorted(bins):
        members = bins[idx]
        n = len(members)
        out.append(
            {
                "bin": f"{idx * width:.1f}-{(idx + 1) * width:.1f}",
                "n": n,
                "predicted": round(sum(p for p, _ in members) / n, 3),
                "actual": round(sum(label for _, label in members) / n, 3),
            }
        )
    return out


def snapshot_evaluation(session: Session, chain: str = "sol") -> bool:
    """Persist at most one evaluation snapshot per hour for trend tracking."""
    from ..models import ScanState, utcnow

    chain = normalize_chain(chain)
    stamp = f"{datetime.now(timezone.utc):%Y%m%d%H}"
    key = f"eval:{stamp}" if chain == "sol" else f"eval:{chain}:{stamp}"
    if session.query(ScanState.id).filter(ScanState.key == key).first():
        return False
    ev = evaluate(session, chain=chain)
    if not ev.get("n"):
        return False
    model = get_or_create_model(session, chain=chain)
    ev["n_train"] = model.n_train
    ev["at"] = datetime.now(timezone.utc).isoformat()
    session.add(ScanState(key=key, value=json.dumps(ev), updated_at=utcnow()))
    session.flush()  # autoflush is off; make the row visible to the dedup query
    return True


def evaluation_history(session: Session, chain: str = "sol") -> list[dict[str, Any]]:
    from ..models import ScanState

    chain = normalize_chain(chain)
    rows = (
        session.query(ScanState)
        .filter(ScanState.key.like("eval:%"))
        .order_by(ScanState.key.asc())
        .all()
    )
    if chain == "sol":
        rows = [row for row in rows if row.key.startswith("eval:2") or row.key.startswith("eval:1")]
    else:
        rows = [row for row in rows if row.key.startswith(f"eval:{chain}:")]
    out = []
    for row in rows:
        try:
            out.append(json.loads(row.value))
        except json.JSONDecodeError:
            continue
    return out


def evaluate_preview(session: Session, chain: str | None = "sol") -> dict[str, Any]:
    """Win rate of stored watch preview vs labels. Empty until Watch names resolve."""
    q = (
        session.query(Research.preview_p, Outcome.label)
        .join(Outcome, Outcome.token_id == Research.token_id)
        .join(Token, Token.id == Research.token_id)
        .filter(Outcome.label.is_not(None), Research.preview_p > 0)
    )
    if chain:
        q = q.filter(Token.chain == normalize_chain(chain))
    rows = q.all()
    if not rows:
        return {"n": 0}
    high = [(p, lab) for p, lab in rows if p >= 0.40]
    low = [(p, lab) for p, lab in rows if p <= 0.22]
    return {
        "n": len(rows),
        "win_rate": round(sum(lab for _, lab in rows) / len(rows), 3),
        "n_high": len(high),
        "win_rate_high": round(sum(lab for _, lab in high) / len(high), 3) if high else None,
        "n_low": len(low),
        "win_rate_low": round(sum(lab for _, lab in low) / len(low), 3) if low else None,
    }


def model_card(session: Session, chain: str = "sol") -> dict[str, Any]:
    model = get_or_create_model(session, chain=chain)
    acc = (model.n_correct / model.n_train) if model.n_train else None
    weights = json.loads(model.weights_json or "{}")
    return {
        "version": model.version,
        "n_train": model.n_train,
        "accuracy": round(acc, 3) if acc is not None else None,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
        "weights": weights,
        "bias": model.bias,
        "preview_weight": weights.get(PREVIEW_WEIGHT, 0.0),
        "preview": evaluate_preview(session, chain=chain),
    }
