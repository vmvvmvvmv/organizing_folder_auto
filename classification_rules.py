"""분류 설정 파일을 읽어 파일 형식별 폴더 규칙을 제공한다."""

from __future__ import annotations

import json
from pathlib import Path


def load_rules(path: Path) -> tuple[set[str], dict[str, str], set[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    folders: dict[str, str] = {}
    structured: set[str] = set()
    for group in data["file_groups"]:
        for extension in group["extensions"]:
            extension = extension.casefold()
            if not extension.startswith(".") or extension in folders:
                raise ValueError(f"중복되거나 잘못된 확장자: {extension}")
            folders[extension] = group["folder"]
            if group["preserve_structure"]:
                structured.add(extension)
    return set(data["ignored_words"]), folders, structured


COMMON, TYPE_FOLDERS, STRUCTURED_TYPES = load_rules(
    Path(__file__).resolve().parent / "config" / "classification.json"
)


def type_folder(path: Path) -> str:
    suffix = path.suffix.casefold()
    return TYPE_FOLDERS.get(suffix, f"{suffix[1:].upper()} 파일" if suffix else "확장자 없는 파일")
