"""Running-image stamp so a cached Docker COPY cannot hide a miss.

Test desk: three-pane terminal (tape | chart | research). FEATURE_NAMES 66.
"""

from pathlib import Path

IMAGE_REV = "stack-v207"

# Written by scripts/stamp_deploy.sh right before `railway up`. CLI uploads
# carry no git metadata (no RAILWAY_GIT_COMMIT_SHA, no .git), so this file is
# the only way /health can say which tree is running.
_HERE = Path(__file__).resolve().parent
GIT_SHA_STAMP = _HERE / ".git_sha"
DEPLOY_STAMP = _HERE / ".deploy_stamp"


def _read_stamp(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except Exception:
        return ""


def running_git_sha() -> str:
    """Short SHA of the running tree. Health-only; IMAGE_REV stays stack-vN.

    Order: platform env → `.git_sha` stamp shipped inside the image → a live
    `git` checkout (dev only). Empty string when none of them can say.
    """
    import os
    import subprocess

    for key in ("RAILWAY_GIT_COMMIT_SHA", "RAILWAY_GIT_COMMIT", "GIT_COMMIT", "SOURCE_COMMIT"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val[:12]
    stamped = _read_stamp(GIT_SHA_STAMP)
    if stamped:
        return stamped[:12]
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return ""


def deploy_stamp() -> str:
    """UTC stamp touched at deploy time (`launchfinder/.deploy_stamp`), or ''."""
    return _read_stamp(DEPLOY_STAMP)
