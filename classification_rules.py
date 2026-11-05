"""분류 설정 파일을 읽어 파일 형식별 폴더 규칙을 제공한다."""

from __future__ import annotations

import json
from pathlib import Path


def load_rules(path: Path) -> tuple[set[str], dict[str, str], set[str], set[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    folders: dict[str, str] = {}
    structured: set[str] = set()
    type_only: set[str] = set()
    for group in data["file_groups"]:
        for extension in group["extensions"]:
            extension = extension.casefold()
            if not extension.startswith(".") or extension in folders:
                raise ValueError(f"중복되거나 잘못된 확장자: {extension}")
            folders[extension] = group["folder"]
            if group["preserve_structure"]:
                structured.add(extension)
            if group.get("always_by_type", False):
                type_only.add(extension)
    return set(data["ignored_words"]), folders, structured, type_only


COMMON, TYPE_FOLDERS, STRUCTURED_TYPES, TYPE_ONLY = load_rules(
    Path(__file__).resolve().parent / "config" / "classification.json"
)


GIT_SETTINGS = {".gitignore", ".gitattributes"}


def _qgis_metadata(path: Path) -> bool:
    if path.suffix.casefold() != ".qmd":
        return False
    try:
        with path.open("rb") as source:
            header = source.read(2048).lower()
    except OSError:
        return False
    return b"<qgis " in header or b"<qgis>" in header


def is_structured_file(path: Path) -> bool:
    suffix = path.suffix.casefold()
    return (suffix in STRUCTURED_TYPES or _qgis_metadata(path)
            or (suffix in {".cst", ".fix"} and path.with_suffix(".shp").is_file()))


def is_type_only(path: Path) -> bool:
    return (path.name.casefold() in GIT_SETTINGS
            or path.suffix.casefold() in TYPE_ONLY
            or is_structured_file(path))

def type_folder(path: Path) -> str:
    if path.name.casefold() in GIT_SETTINGS:
        return "버전 관리 설정"
    if path.suffix.casefold() in {".qmd", ".cst", ".fix"} and is_structured_file(path):
        return "GIS 자료"
    suffix = path.suffix.casefold()
    return TYPE_FOLDERS.get(suffix, f"{suffix[1:].upper()} 파일" if suffix else "확장자 없는 파일")
