"""PC 문서를 분류하고 내용을 검증한 뒤 결과 폴더로 이동한다."""

from __future__ import annotations

import hashlib
import csv
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from auto_groups import propose_folders
from classification_rules import STRUCTURED_TYPES, type_folder
from reader import SUPPORTED, read_document


def default_destination(source: Path) -> Path:
    return source.parent / f"{source.name}_자동정리결과"


@dataclass
class Document:
    source: Path
    folder: str
    folder_reason: str
    method: str
    error: str = ""
    duplicate_of: Path | None = None
    lead_text: str = ""
    preview_digest: bytes | None = None


def valid_folder_name(name: str) -> bool:
    """사용자가 적은 폴더 이름이 결과 폴더 밖을 가리키지 않도록 확인한다."""
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return bool(
        name and len(name) <= 60 and name == name.strip() and not name.endswith(".")
        and name not in {".", ".."} and not re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
        and name.split(".")[0].upper() not in reserved
    )


def rename_folder(documents: list[Document], old_name: str, new_name: str) -> list[int]:
    """제안된 폴더 하나의 이름을 묶음 전체에 적용한다."""
    if not valid_folder_name(new_name):
        raise ValueError(f"사용할 수 없는 폴더 이름: {new_name}")
    changed = []
    for index, document in enumerate(documents):
        if document.folder == old_name:
            document.folder = new_name
            document.folder_reason = "사용자 지정"
            changed.append(index)
    return changed


def scan(
    source: Path, destination: Path, executable: str | None,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[Document]:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise ValueError("원본 폴더가 없습니다")
    if source == destination or source.is_relative_to(destination):
        raise ValueError("결과 폴더가 원본 폴더와 같거나 원본 폴더를 포함합니다")
    files = sorted(
        path for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
        and not path.resolve().is_relative_to(destination)
    )
    results = []
    seen: dict[bytes, Document] = {}
    for index, path in enumerate(files, 1):
        if should_cancel and should_cancel():
            raise InterruptedError("검사가 중단되었습니다")
        # 내용을 못 읽는 파일도 이동 전 검증할 수 있도록 미리보기 시점의 해시를 남긴다.
        digest = _digest(path)
        try:
            # 같은 내용이면 분류와 OCR도 다시 하지 않는다.
            shareable = path.suffix.lower() not in STRUCTURED_TYPES
            if shareable and digest in seen:
                original = seen[digest]
                results.append(Document(
                    path, "", f"동일 파일: {original.source.name}",
                    "중복 파일", original.error, original.source,
                    preview_digest=digest,
                ))
            else:
                text, method = (
                    read_document(path, executable) if path.suffix.lower() in SUPPORTED
                    else ("", "파일명·형식")
                )
                document = Document(path, "", "", method, lead_text=text[:500], preview_digest=digest)
                results.append(document)
                if shareable:
                    seen[digest] = document
        except (OSError, ValueError, RuntimeError) as exc:
            document = Document(path, "", "", "", str(exc), preview_digest=digest)
            results.append(document)
            if path.suffix.lower() not in STRUCTURED_TYPES:
                seen[digest] = document
        if progress:
            progress(index, len(files))
    originals = [doc for doc in results if doc.duplicate_of is None]
    names_by_original = {doc.source: [doc.source.stem] for doc in originals}
    for doc in results:
        if doc.duplicate_of is not None:
            names_by_original[doc.duplicate_of].append(doc.source.stem)
    # 같은 내용의 다른 파일명도 단서로 쓰되 문서 수는 한 번만 센다.
    proposals = propose_folders([
        (" ".join(names_by_original[doc.source]), doc.lead_text) for doc in originals
    ])
    for doc, (folder, reason) in zip(originals, proposals):
        if doc.source.suffix.lower() in STRUCTURED_TYPES or (folder == "미분류" and doc.method == "파일명·형식"):
            doc.folder = type_folder(doc.source)
            doc.folder_reason = f"파일 형식: {doc.source.suffix.lower() or '없음'}"
        else:
            doc.folder, doc.folder_reason = folder, reason
    by_source = {doc.source: doc for doc in originals}
    for doc in results:
        if doc.duplicate_of is not None:
            original = by_source[doc.duplicate_of]
            doc.folder, doc.folder_reason = original.folder, original.folder_reason
    return results


def _digest(path: Path) -> bytes:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.digest()


def _available_target(folder: Path, source: Path) -> Path:
    target = folder / source.name
    counter = 2
    while target.exists() or target.is_symlink():
        target = folder / f"{source.stem}_{counter}{source.suffix}"
        counter += 1
    return target


def _structured_group(document: Document) -> tuple[str, Path] | None:
    if document.source.suffix.lower() in STRUCTURED_TYPES:
        return document.folder, document.source.parent
    return None


def _target_folder(document: Document, destination: Path, source_root: Path | None) -> Path:
    folder = destination / document.folder
    if _structured_group(document) and source_root:
        folder /= document.source.parent.relative_to(source_root)
    # 하위 폴더를 만들기 전에 기존 심볼릭 링크가 결과 폴더 밖을 가리키는지 확인한다.
    if not folder.resolve().is_relative_to(destination):
        raise ValueError("결과 폴더 밖으로 이동할 수 없습니다")
    return folder


def move_documents(
    documents: list[Document], destination: Path,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    source_root: Path | None = None,
) -> tuple[list[tuple[Document, Path]], list[tuple[Document, Path]], list[tuple[Document, str]]]:
    """복사본과 원본의 내용이 같을 때만 원본을 제거한다."""
    destination = destination.resolve()
    source_root = source_root.resolve() if source_root else None
    if not destination.is_dir():
        raise ValueError("결과 폴더가 없습니다")
    previewed = [doc for doc in documents if doc.preview_digest is not None]
    if source_root and len(previewed) == len(documents):
        current = {
            path.resolve() for path in source_root.rglob("*")
            if path.is_file() and not path.is_symlink()
            and not path.resolve().is_relative_to(destination)
        }
        if current != {doc.source.resolve() for doc in documents}:
            raise ValueError("미리보기 이후 파일 목록이 변경되었습니다. 미리보기를 다시 실행하세요")
    for document in previewed:
        if not document.source.is_file() or document.source.is_symlink() or _digest(document.source) != document.preview_digest:
            raise ValueError(f"미리보기 이후 파일 내용이 변경되었습니다. 미리보기를 다시 실행하세요: {document.source}")
    moved = []
    deduplicated = []
    errors = []
    existing = {}
    for path in sorted(destination.rglob("*")):
        if should_cancel and should_cancel():
            return moved, deduplicated, errors
        if path.is_file() and not path.is_symlink() and path.suffix.lower() not in STRUCTURED_TYPES:
            existing[_digest(path)] = path

    groups: dict[tuple[str, Path], list[Document]] = {}
    for document in documents:
        if group := _structured_group(document):
            groups.setdefault(group, []).append(document)
    blocked: dict[tuple[str, Path], str] = {}
    for group, members in groups.items():
        for document in members:
            try:
                target = _target_folder(document, destination, source_root) / document.source.name
                if target.exists() or target.is_symlink():
                    if target.is_symlink() or not target.is_file() or _digest(target) != _digest(document.source):
                        blocked[group] = "같은 이름의 다른 구성 파일이 이미 있습니다. 묶음 전체를 원본에 유지했습니다"
                        break
            except (OSError, ValueError) as exc:
                blocked[group] = str(exc)
                break

    ordered = sorted(
        enumerate(documents),
        key=lambda pair: (0, pair[1].folder.casefold(), str(pair[1].source.parent).casefold(), pair[0])
        if _structured_group(pair[1]) else (1, "", "", pair[0]),
    )
    previous_group = None
    for index, (_, document) in enumerate(ordered, 1):
        group = _structured_group(document)
        if should_cancel and should_cancel() and (group is None or group != previous_group):
            break
        try:
            if group in blocked:
                raise ValueError(blocked[group])
            if not valid_folder_name(document.folder):
                raise ValueError(f"사용할 수 없는 폴더 이름: {document.folder}")
            source = document.source
            if not source.is_file() or source.is_symlink() or source.resolve().is_relative_to(destination):
                raise ValueError("원본 파일이 없거나 결과 폴더 안에 있습니다")
            folder = _target_folder(document, destination, source_root)
            folder.mkdir(parents=True, exist_ok=True)
            digest = _digest(source)
            if document.preview_digest is not None and digest != document.preview_digest:
                raise ValueError("미리보기 이후 파일 내용이 변경되었습니다. 원본을 유지했습니다")
            if source.suffix.lower() in STRUCTURED_TYPES:
                candidate = folder / source.name
                matched = candidate if candidate.is_file() and not candidate.is_symlink() and _digest(candidate) == digest else None
                if candidate.exists() and matched is None:
                    raise ValueError("같은 이름의 다른 구성 파일이 이미 있습니다. 원본을 유지했습니다")
            else:
                matched = existing.get(digest)
            if matched is not None:
                # 기존 결과 파일이나 이번 실행에서 옮긴 파일도 다시 읽고 확인한다.
                if _digest(source) != digest or _digest(matched) != digest:
                    raise ValueError("파일 내용이 검사 중 변경되었습니다")
                source.unlink()
                deduplicated.append((document, matched))
                continue

            temporary = None
            try:
                # 미완성 파일이 결과 목록에 보이지 않도록 임시 파일에서 검증한다.
                with tempfile.NamedTemporaryFile(dir=folder, prefix=".정리-", suffix=".partial", delete=False) as output:
                    temporary = Path(output.name)
                    with source.open("rb") as original:
                        shutil.copyfileobj(original, output)
                    output.flush()
                    os.fsync(output.fileno())
                shutil.copystat(source, temporary)
                if _digest(source) != digest or _digest(temporary) != digest:
                    raise ValueError("파일 내용 검증 실패: 원본을 유지했습니다")
                while True:
                    target = _available_target(folder, source)
                    try:
                        temporary.rename(target)
                        break
                    except FileExistsError:
                        continue
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            if _digest(source) != digest or _digest(target) != digest:
                target.unlink(missing_ok=True)
                raise ValueError("이동 후 내용 검증 실패: 원본을 유지했습니다")
            # 원본 삭제에 실패하면 검증된 결과 파일과 원본을 모두 남긴다.
            if source.suffix.lower() not in STRUCTURED_TYPES:
                existing[digest] = target
            source.unlink()
            moved.append((document, target))
        except (OSError, ValueError) as exc:
            errors.append((document, str(exc)))
        finally:
            if progress:
                progress(index, len(documents))
            previous_group = group
    return moved, deduplicated, errors


def write_result_report(
    source: Path, destination: Path, documents: list[Document],
    moved: list[tuple[Document, Path]], deduplicated: list[tuple[Document, Path]],
    errors: list[tuple[Document, str]],
) -> Path:
    """실행마다 별도 CSV를 남겨 이동·중복·실패 내역을 보존한다."""
    moved_paths = {doc.source: path for doc, path in moved}
    deduplicated_paths = {doc.source: path for doc, path in deduplicated}
    error_messages = {doc.source: message for doc, message in errors}
    base = f"정리_결과_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    number = 1
    while True:
        report = destination / f"{base}{f'_{number}' if number > 1 else ''}.csv"
        try:
            stream = report.open("x", encoding="utf-8-sig", newline="")
            break
        except FileExistsError:
            number += 1
    with stream:
        writer = csv.writer(stream)
        writer.writerow(("원본", "폴더", "상태", "결과 파일", "분류 조건", "읽기 방식", "읽기 오류", "이동 오류"))
        for doc in documents:
            status = (
                "이동" if doc.source in moved_paths else
                "중복 정리" if doc.source in deduplicated_paths else
                "이동 실패" if doc.source in error_messages else "미처리"
            )
            writer.writerow((
                str(doc.source.relative_to(source)), doc.folder, status,
                str(moved_paths.get(doc.source, deduplicated_paths.get(doc.source, ""))),
                doc.folder_reason, doc.method, doc.error, error_messages.get(doc.source, ""),
            ))
    return report
