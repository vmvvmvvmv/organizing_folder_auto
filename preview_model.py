"""미리보기 목록의 모양: 폴더 경로별 묶기, 검색, 선택한 행 해석, 폴더 단계별 색 태그."""

from __future__ import annotations

from pathlib import Path
from tkinter import ttk

from organizer import Document, route
from reader import SUPPORTED

FILTERS = ("전체", "미분류", "읽기 실패", "중복", "내용 못 읽은 파일")


def group_preview_documents(
    documents: list[Document], mode: str, query: str = "",
) -> dict[tuple[str, ...], list[int]]:
    """현재 필터의 파일을 폴더 경로((대분류,), (대분류, 중분류), (대분류, 중분류, 소분류))별로 묶는다.

    query가 있으면 파일 이름이나 폴더 경로에 그 글자가 들어간 파일만 남긴다(대소문자·띄어쓰기 무시).
    """
    query = "".join(query.split()).casefold()
    groups: dict[tuple[str, ...], list[int]] = {}
    for index, doc in enumerate(documents):
        if query:
            path = route(doc) if doc.folder else ("미분류",)
            text = "".join(f"{doc.output_name or doc.source.name}{''.join(path)}".split()).casefold()
            if query not in text:
                continue
        visible = (
            mode == "전체"
            or (mode == "미분류" and doc.folder == "미분류")
            or (mode == "읽기 실패" and bool(doc.error))
            or (mode == "중복" and doc.duplicate_of is not None)
            or (mode == "내용 못 읽은 파일" and doc.source.suffix.lower() not in SUPPORTED)
        )
        if visible:
            path = route(doc) if doc.folder else ("미분류",)
            groups.setdefault(path, []).append(index)
    return groups


def preview_location(source_root: Path, source: Path) -> str:
    """반복되는 전체 경로 대신 원본 폴더 아래 위치만 표시한다."""
    try:
        parent = source.parent.relative_to(source_root)
    except ValueError:
        return str(source.parent)
    return "원본 폴더" if parent == Path(".") else str(parent)


def selected_file_indices(items: tuple[str, ...]) -> list[int]:
    """폴더 행을 파일 편집 대상으로 착각하지 않도록 구분한다."""
    return [int(item[5:]) for item in items if item.startswith("file:") and item[5:].isdigit()]

def drop_folder_row(
    table: ttk.Treeview, folder_rows: dict[str, tuple[str, ...]], item: str,
) -> str | None:
    """폴더 안의 파일 행에 놓아도 가장 가까운 부모 폴더를 대상으로 삼는다."""
    while item:
        if item in folder_rows:
            return item
        item = table.parent(item)
    return None


FOLDER_LEVEL_STYLES = (
    ("#d6e4f5", "AppBold"),   # 대분류
    ("#e3f2e1", None),                            # 중분류
    ("#fbf3d9", None),                            # 소분류
)


def folder_tag(path: tuple[str, ...]) -> str:
    """폴더 행의 색 태그. 단계(깊이)에 따라 다르다."""
    return f"folder{min(len(path), len(FOLDER_LEVEL_STYLES))}"


def row_tags(folder_rows: dict[str, tuple[str, ...]], row: str) -> tuple[str, ...]:
    path = folder_rows.get(row)
    return (folder_tag(path),) if path else ()


def in_folder(doc: Document, path: tuple[str, ...]) -> bool:
    """파일이 그 폴더 경로 안(아래 단계 포함)에 들어가는지."""
    return route(doc)[:len(path)] == path


def preview_selection(
    documents: list[Document], folder_rows: dict[str, tuple[str, ...]], item: str,
) -> list[Document]:
    """선택한 파일 또는 제안 폴더에 들어갈 파일을 돌려준다."""
    if item in folder_rows:
        return [doc for doc in documents if in_folder(doc, folder_rows[item])]
    indices = selected_file_indices((item,))
    return [documents[indices[0]]] if indices and indices[0] < len(documents) else []
