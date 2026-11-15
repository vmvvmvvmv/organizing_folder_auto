"""이 PC(%LOCALAPPDATA%\\DocumentOrganizer)에 저장하는 사용자 설정과 지난 미리보기 위치."""

from __future__ import annotations

import json
from pathlib import Path

from ai_hierarchy import parse_user_folders
from local_cache import default_cache_path


def excludes_path() -> Path:
    return default_cache_path().with_name("exclude.json")


def load_excludes() -> list[str]:
    try:
        data = json.loads(excludes_path().read_text(encoding="utf-8"))
        return [str(item) for item in data if str(item).strip()] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_excludes(patterns: list[str]) -> None:
    try:
        excludes_path().parent.mkdir(parents=True, exist_ok=True)
        excludes_path().write_text(json.dumps(patterns, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def last_preview_path() -> Path:
    return default_cache_path().with_name("last_preview.json")


def user_folders_path() -> Path:
    return default_cache_path().with_name("user_folders.json")


def load_user_folders() -> tuple[list[str], bool]:
    try:
        data = json.loads(user_folders_path().read_text(encoding="utf-8"))
        return parse_user_folders("\n".join(data.get("folders", [])))[0], bool(data.get("allow_new", False))
    except (OSError, ValueError, AttributeError):
        return [], False


def save_user_folders(folders: list[str], allow_new: bool) -> None:
    try:
        path = user_folders_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"folders": folders, "allow_new": allow_new}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 저장하지 못해도 이번 실행에서는 쓴다.


FOLDERS_LABEL = "2. 폴더 구조 정하기"


def user_folders_label(folders: list[str]) -> str:
    return f"{FOLDERS_LABEL} ({len(folders)})" if folders else f"{FOLDERS_LABEL} (선택)"
