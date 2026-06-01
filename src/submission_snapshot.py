"""Submission video snapshots for per-run growth tracking."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.config import APP_ROOT


SNAPSHOT_ROOT = APP_ROOT / "data" / "submission_snapshots"


def normalize_video(video: dict) -> dict:
    """Keep the fields needed for display and growth diffing."""
    return {
        "bvid": str(video.get("bvid") or ""),
        "aid": video.get("aid") or 0,
        "title": str(video.get("title") or ""),
        "play": _as_int(video.get("play")),
        "comment": _as_int(video.get("comment")),
        "video_review": _as_int(video.get("video_review")),
        "length": str(video.get("length") or ""),
        "created": _as_int(video.get("created")),
        "pic": str(video.get("pic") or ""),
    }


def save_snapshot(mid: int, videos: list[dict]) -> dict:
    """Save a complete snapshot and return videos annotated with deltas."""
    now = datetime.now()
    user_dir = SNAPSHOT_ROOT / str(mid)
    user_dir.mkdir(parents=True, exist_ok=True)

    normalized = [normalize_video(video) for video in videos if video.get("bvid")]
    previous = load_latest(mid)
    previous_map = {item.get("bvid"): item for item in previous.get("videos", [])}

    annotated = []
    for video in normalized:
        old = previous_map.get(video["bvid"]) or {}
        item = dict(video)
        item["play_delta"] = video["play"] - _as_int(old.get("play"))
        item["comment_delta"] = video["comment"] - _as_int(old.get("comment"))
        item["is_new"] = not bool(old)
        annotated.append(item)

    payload = {
        "mid": mid,
        "created_at": now.isoformat(timespec="seconds"),
        "video_count": len(normalized),
        "videos": normalized,
    }

    snapshot_path = user_dir / f"{now.strftime('%Y%m%d_%H%M%S')}.json"
    latest_path = user_dir / "latest.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    snapshot_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")

    return {
        "snapshot_at": payload["created_at"],
        "previous_snapshot_at": previous.get("created_at") or "",
        "videos": annotated,
    }


def load_in_progress(mid: int) -> dict:
    path = SNAPSHOT_ROOT / str(mid) / "in_progress.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_in_progress(mid: int, videos: list[dict], next_page: int, page_size: int, total_count: int) -> None:
    user_dir = SNAPSHOT_ROOT / str(mid)
    user_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "mid": mid,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "next_page": next_page,
        "page_size": page_size,
        "total_count": total_count,
        "videos": [normalize_video(video) for video in videos if video.get("bvid")],
    }
    (user_dir / "in_progress.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clear_in_progress(mid: int) -> None:
    path = SNAPSHOT_ROOT / str(mid) / "in_progress.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def load_latest(mid: int) -> dict:
    latest_path = SNAPSHOT_ROOT / str(mid) / "latest.json"
    if not latest_path.exists():
        return {}
    try:
        return json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
