from launchfinder.db import init_db, session_scope
from launchfinder.scoring.features import FEATURE_NAMES, heuristic_probability
from launchfinder.scoring.model import get_or_create_model, online_update, predict
from launchfinder.scoring.preview import PREVIEW_FEATURE, PREVIEW_WEIGHT, attach_watch_scores, preview_score


def test_feature_names_untouched():
    assert len(FEATURE_NAMES) == 66
    assert PREVIEW_FEATURE not in FEATURE_NAMES
    assert PREVIEW_WEIGHT not in FEATURE_NAMES


def test_preview_caps_below_hunt_and_rewards_real_socials():
    strong, reasons, _ = preview_score(
        symbol="NICE",
        name="Nice Coin",
        twitter="https://x.com/nice",
        website="https://nice.example",
        github="https://github.com/nice/nice",
        twitter_followers=800,
        twitter_age_days=400,
        twitter_verified=True,
        github_stars=12,
        progress=0.84,
        age_min=45,
        creator_wins=1,
        creator_launches=2,
        reply_count=30,
        prewarmed=True,
    )
    weak, _, flags = preview_score(
        symbol="X9",
        twitter="https://x.com/new",
        twitter_followers=2,
        twitter_age_days=0.4,
        progress=0.86,
        age_min=1.0,
        creator_rugs=2,
        creator_launches=9,
    )
    assert 0.05 <= strong <= 0.60
    assert strong > weak
    assert any("followership" in row.lower() or "github" in row.lower() for row in reasons)
    assert any("instantly" in row.lower() or "rug" in row.lower() for row in flags)


def test_preview_is_a_side_weight_the_model_can_learn():
    init_db()
    base = {name: 0.4 for name in FEATURE_NAMES}
    hot = dict(base)
    hot[PREVIEW_FEATURE] = 0.55
    with session_scope() as session:
        before = predict(session, base)["model_p"]
        same = predict(session, hot)["model_p"]
        assert abs(same - before) < 1e-9
        for _ in range(8):
            online_update(session, hot, 1, lr=0.3)
        model = get_or_create_model(session)
        weights = __import__("json").loads(model.weights_json)
        assert PREVIEW_WEIGHT in weights
        assert weights[PREVIEW_WEIGHT] > 0
        after_hot = predict(session, hot)["model_p"]
        after_base = predict(session, base)["model_p"]
        assert after_hot > after_base


def test_heuristic_mentions_constructive_preview():
    feats = {name: 0.4 for name in FEATURE_NAMES}
    feats[PREVIEW_FEATURE] = 0.50
    reasons: list[str] = []
    flags: list[str] = []
    heuristic_probability(feats, reasons, flags)
    assert any("preview" in row.lower() for row in reasons)


def test_attach_watch_scores_writes_preview():
    init_db()
    with session_scope() as session:
        rows = attach_watch_scores(
            session,
            [
                {
                    "mint": "WatchMint11111111111111111111111111111111",
                    "symbol": "PRE",
                    "name": "Preview",
                    "progress": 0.82,
                    "twitter": "https://x.com/preview",
                    "website": "https://preview.example",
                    "first_seen": "2026-09-13T16:00:00+00:00",
                }
            ],
            "sol",
            {},
        )
    assert len(rows) == 1
    assert 0.05 <= rows[0]["preview_p"] <= 0.60
    assert rows[0]["score"] == round(rows[0]["preview_p"] * 100.0, 1)
