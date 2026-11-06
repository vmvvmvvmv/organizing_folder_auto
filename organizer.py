"""PC 문서를 분류하고 내용을 검증한 뒤 결과 폴더로 이동한다."""

from __future__ import annotations

import hashlib
import csv
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from auto_groups import propose_folders
from classification_rules import is_structured_file, is_type_only, type_folder
from reader import SUPPORTED, read_document


# 미리보기 때 내용을 읽지 못한 파일의 preview_digest. 이런 파일은 옮기지 않는다.
UNREADABLE = b""


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
    subfolder: str = ""
    preserve_structure: bool = False
    output_name: str = ""
    detail: str = ""  # 소분류(대분류 / 중분류 / 소분류의 세 번째 단계)


def route(doc: Document) -> tuple[str, ...]:
    """파일이 들어갈 폴더 경로: (대분류,), (대분류, 중분류), (대분류, 중분류, 소분류)."""
    if not doc.subfolder:
        return (doc.folder,)
    return (doc.folder, doc.subfolder, doc.detail) if doc.detail else (doc.folder, doc.subfolder)


def valid_folder_name(name: str) -> bool:
    """사용자가 적은 폴더 이름이 결과 폴더 밖을 가리키지 않도록 확인한다."""
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return bool(
        name and len(name) <= 60 and name == name.strip() and not name.endswith(".")
        and name not in {".", ".."} and not re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
        and name.split(".")[0].upper() not in reserved
    )


def rename_document(document: Document, new_name: str) -> None:
    """원본은 그대로 두고 정리 결과에 사용할 파일명만 지정한다."""
    if document.duplicate_of is not None:
        raise ValueError("중복 파일은 남길 원본 파일의 이름을 바꾸세요")
    if document.preserve_structure or is_structured_file(document.source):
        raise ValueError("구성 파일은 연결이 끊길 수 있어 이름을 바꿀 수 없습니다")
    if (not valid_folder_name(new_name)
            or Path(new_name).suffix.casefold() != document.source.suffix.casefold()):
        raise ValueError("파일 이름을 확인하세요. 확장자는 바꿀 수 없습니다")
    document.output_name = "" if new_name == document.source.name else new_name


def reassign_documents(
    documents: list[Document], indices: list[int], folder: str, subfolder: str, detail: str = "",
) -> list[int]:
    """미리보기 분류만 바꾼다. 중복본과 함께 보관할 구성 파일도 같이 바꾼다."""
    if (not valid_folder_name(folder) or (subfolder and not valid_folder_name(subfolder))
            or (detail and (not subfolder or not valid_folder_name(detail)))):
        raise ValueError("사용할 수 없는 폴더 이름입니다")
    selected = [documents[index] for index in indices]
    originals = {doc.duplicate_of or doc.source for doc in selected}
    related = {
        (doc.source.parent, type_folder(doc.source)) for doc in selected
        if doc.preserve_structure or is_structured_file(doc.source)
    }
    changed = []
    for index, doc in enumerate(documents):
        key = (doc.source.parent, type_folder(doc.source))
        if ((doc.duplicate_of or doc.source) in originals
                or ((doc.preserve_structure or is_structured_file(doc.source)) and key in related)):
            if (doc.folder, doc.subfolder, doc.detail) != (folder, subfolder, detail):
                doc.folder = folder
                doc.subfolder = subfolder
                doc.detail = detail
                doc.folder_reason = "사용자 지정"
                changed.append(index)
    return changed

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


def rename_subfolder(documents: list[Document], folder: str, old_name: str, new_name: str) -> list[int]:
    """선택한 주제 안의 하위 폴더만 이름을 바꾼다. 빈 이름은 상위로 합친다."""
    if new_name and not valid_folder_name(new_name):
        raise ValueError(f"사용할 수 없는 하위 폴더 이름: {new_name}")
    changed = []
    for index, document in enumerate(documents):
        if document.folder == folder and document.subfolder == old_name:
            document.subfolder = new_name
            if not new_name:
                document.detail = ""  # 상위로 합치면 그 아래 소분류도 함께 없앤다.
            document.folder_reason = "사용자 지정"
            changed.append(index)
    return changed


def rename_detail(
    documents: list[Document], folder: str, subfolder: str, old_name: str, new_name: str,
) -> list[int]:
    """중분류 안의 소분류 이름만 바꾼다. 빈 이름은 중분류로 합친다."""
    if new_name and not valid_folder_name(new_name):
        raise ValueError(f"사용할 수 없는 소분류 이름: {new_name}")
    changed = []
    for index, document in enumerate(documents):
        if (document.folder, document.subfolder, document.detail) == (folder, subfolder, old_name):
            document.detail = new_name
            document.folder_reason = "사용자 지정"
            changed.append(index)
    return changed

# 개발 도구가 만드는 폴더. 문서가 아니고 수만 개 파일이 들어 있을 수 있다.
TOOL_FOLDERS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", ".gradle",
                ".idea", ".vs", ".mypy_cache", ".pytest_cache", ".tox"}
# 이 파일이 있는 폴더는 코드 프로젝트나 압축을 푼 Office 문서([Content_Types].xml)로 본다.
# 파일을 낱개로 옮기면 망가지므로 폴더째 그대로 둔다.
PROJECT_MARKERS = {".git", "package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod",
                   "pom.xml", "build.gradle", "build.gradle.kts", "[Content_Types].xml"}


def _is_code_project(names: list[str]) -> bool:
    return any(name in PROJECT_MARKERS or name.casefold().endswith(".sln") for name in names)


def source_files(source: Path, destination: Path) -> tuple[list[Path], list[Path]]:
    """정리할 파일과 건드리지 않을 코드 프로젝트 폴더를 돌려준다. 미리보기와 이동 검사가 함께 쓴다."""
    source = source.resolve()
    destination = destination.resolve()
    if _is_code_project(os.listdir(source)):
        raise ValueError("선택한 폴더가 코드 프로젝트(또는 압축을 푼 문서)입니다. 그 상위 폴더를 선택하세요")
    files: list[Path] = []
    projects: list[Path] = []
    for current, folders, names in os.walk(source):
        here = Path(current)
        if here != source and _is_code_project(folders + names):
            projects.append(here)
            folders[:] = []
            continue
        folders[:] = sorted(
            name for name in folders
            if name not in TOOL_FOLDERS and not (here / name).resolve().is_relative_to(destination)
        )
        files.extend(
            path for name in names
            if (path := here / name).is_file() and not path.is_symlink()
            and not path.resolve().is_relative_to(destination)
        )
    return sorted(files), projects


def scan(
    source: Path, destination: Path, executable: str | None,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    classify: bool = True,
    cache=None,
) -> list[Document]:
    """cache(local_cache.LocalCache)를 주면 해시와 본문을 저장해 두고 다음 미리보기에서 다시 쓴다."""
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise ValueError("원본 폴더가 없습니다")
    if source == destination or source.is_relative_to(destination):
        raise ValueError("결과 폴더가 원본 폴더와 같거나 원본 폴더를 포함합니다")
    files, _ = source_files(source, destination)
    results = []
    seen: dict[bytes, Document] = {}
    for index, path in enumerate(files, 1):
        if should_cancel and should_cancel():
            raise InterruptedError("검사가 중단되었습니다")
        # 내용을 못 읽는 파일도 이동 전 검증할 수 있도록 미리보기 시점의 해시를 남긴다.
        structured = is_structured_file(path)
        try:
            digest = cache.digest(path, _digest) if cache else _digest(path)
        except OSError as exc:
            # 다른 프로그램이 잠근 파일 하나 때문에 미리보기 전체를 멈추지 않는다.
            # 내용을 확인하지 못했으므로 이동 단계에서는 원본에 남긴다.
            results.append(Document(path, "", "", "", f"파일을 읽을 수 없습니다: {exc}",
                                    preview_digest=UNREADABLE, preserve_structure=structured))
            if progress:
                progress(index, len(files))
            continue
        try:
            # 같은 내용이면 분류와 OCR도 다시 하지 않는다.
            shareable = not structured
            if shareable and digest in seen:
                original = seen[digest]
                results.append(Document(
                    path, "", f"동일 파일: {original.source.name}",
                    "중복 파일", original.error, original.source,
                    preview_digest=digest,
                ))
            else:
                if path.suffix.lower() not in SUPPORTED:
                    text, method = "", "파일명·형식"
                elif cache:
                    text, method = cache.read(digest, bool(executable),
                                              lambda: read_document(path, executable))
                else:
                    text, method = read_document(path, executable)
                document = Document(path, "", "", method, lead_text=text[:1200], preview_digest=digest,
                                    preserve_structure=structured)
                results.append(document)
                if shareable:
                    seen[digest] = document
        except (OSError, ValueError, RuntimeError) as exc:
            document = Document(path, "", "", "", str(exc), preview_digest=digest,
                                preserve_structure=structured)
            results.append(document)
            if not structured:
                seen[digest] = document
        if progress:
            progress(index, len(files))
    if not classify:
        for doc in results:
            doc.folder = "미분류"
            doc.folder_reason = "로컬 AI 분류 대기"
        return results
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
        if is_type_only(doc.source) or (folder == "미분류" and doc.method == "파일명·형식"):
            doc.folder = type_folder(doc.source)
            doc.folder_reason = f"파일 형식: {doc.source.suffix.lower() or '없음'}"
        else:
            doc.folder, doc.folder_reason = folder, reason
    by_source = {doc.source: doc for doc in originals}
    for doc in results:
        if doc.duplicate_of is not None:
            original = by_source[doc.duplicate_of]
            doc.folder, doc.folder_reason = original.folder, original.folder_reason
            doc.subfolder = original.subfolder
            doc.detail = original.detail
    return results


def _digest(path: Path) -> bytes:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.digest()


def _remove(path: Path, missing_ok: bool = False) -> None:
    """파일을 지운다. Windows 읽기 전용 속성 때문에 거부되면 속성을 풀고 다시 지운다.

    옮기는 파일의 결과 사본은 copystat으로 원래 속성을 그대로 갖고 있다.
    """
    try:
        path.unlink(missing_ok=missing_ok)
    except PermissionError:
        if not path.exists():
            return
        mode = path.stat().st_mode
        if mode & stat.S_IWRITE:
            raise  # 읽기 전용 때문이 아니다(다른 프로그램이 사용 중 등).
        os.chmod(path, mode | stat.S_IWRITE)
        path.unlink()


def _available_target(folder: Path, source: Path, name: str = "") -> Path:
    proposed = Path(name or source.name)
    target = folder / proposed.name
    counter = 2
    while target.exists() or target.is_symlink():
        target = folder / f"{proposed.stem}_{counter}{proposed.suffix}"
        counter += 1
    return target


def _structured_group(document: Document) -> tuple[str, str, str, Path] | None:
    if document.preserve_structure or is_structured_file(document.source):
        return document.folder, document.subfolder, document.detail, document.source.parent
    return None


def _target_folder(document: Document, destination: Path, source_root: Path | None) -> Path:
    folder = destination / document.folder
    if document.subfolder:
        folder /= document.subfolder
        if document.detail:
            folder /= document.detail
    if _structured_group(document) and source_root:
        folder /= document.source.parent.relative_to(source_root)
    # 하위 폴더를 만들기 전에 기존 심볼릭 링크가 결과 폴더 밖을 가리키는지 확인한다.
    if not folder.resolve().is_relative_to(destination):
        raise ValueError("결과 폴더 밖으로 이동할 수 없습니다")
    return folder




def _prepare(document: Document, destination: Path, source_root: Path | None) -> tuple[Path, bytes]:
    """이동 전에 이름·경로·내용을 확인하고 결과 폴더와 현재 해시를 돌려준다."""
    if not valid_folder_name(document.folder):
        raise ValueError(f"사용할 수 없는 폴더 이름: {document.folder}")
    if document.subfolder and not valid_folder_name(document.subfolder):
        raise ValueError(f"사용할 수 없는 하위 폴더 이름: {document.subfolder}")
    if document.detail and (not document.subfolder or not valid_folder_name(document.detail)):
        raise ValueError(f"사용할 수 없는 소분류 이름: {document.detail}")
    source = document.source
    if document.output_name and (
        not valid_folder_name(document.output_name)
        or Path(document.output_name).suffix.casefold() != source.suffix.casefold()
        or document.preserve_structure or is_structured_file(source)
    ):
        raise ValueError("사용할 수 없는 결과 파일 이름입니다")
    if not source.is_file() or source.is_symlink() or source.resolve().is_relative_to(destination):
        raise ValueError("원본 파일이 없거나 결과 폴더 안에 있습니다")
    if document.preview_digest == UNREADABLE:
        raise ValueError("미리보기 때 읽을 수 없던 파일입니다. 원본을 유지했습니다")
    folder = _target_folder(document, destination, source_root)
    folder.mkdir(parents=True, exist_ok=True)
    digest = _digest(source)
    if document.preview_digest is not None and digest != document.preview_digest:
        raise ValueError("미리보기 이후 파일 내용이 변경되었습니다. 원본을 유지했습니다")
    return folder, digest


def _stage(source: Path, folder: Path, digest: bytes) -> Path:
    """미완성 파일이 결과 목록에 보이지 않도록 임시 파일에 복사하고 검증한다."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".정리-", suffix=".partial", delete=False) as output:
            temporary = Path(output.name)
            with source.open("rb") as original:
                shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, temporary)
        if _digest(source) != digest or _digest(temporary) != digest:
            raise ValueError("파일 내용 검증 실패: 원본을 유지했습니다")
        return temporary
    except BaseException:
        if temporary is not None:
            _remove(temporary, missing_ok=True)
        raise


def _move_group(
    members: list[Document], destination: Path, source_root: Path | None,
) -> list[tuple[Document, str, Path | str]]:
    """구성 파일 묶음은 모두 복사·검증된 뒤에만 원본을 지워 세트가 나뉘지 않게 한다."""
    staged: list[tuple[Document, bytes, Path, Path | None]] = []
    published: list[Path] = []
    current = members[0]
    try:
        for current in members:
            folder, digest = _prepare(current, destination, source_root)
            target = folder / current.source.name
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file() or _digest(target) != digest:
                    raise ValueError("같은 이름의 다른 구성 파일이 이미 있습니다")
                staged.append((current, digest, target, None))
            else:
                staged.append((current, digest, target, _stage(current.source, folder, digest)))
        for current, _, target, temporary in staged:
            if temporary is not None:
                # Windows에서는 대상이 이미 있으면 FileExistsError로 실패한다.
                temporary.rename(target)
                published.append(target)
        for current, digest, target, _ in staged:
            if _digest(current.source) != digest or _digest(target) != digest:
                raise ValueError("이동 후 내용 검증 실패")
    except (OSError, ValueError) as exc:
        for _, _, _, temporary in staged:
            if temporary is not None:
                _remove(temporary, missing_ok=True)
        for target in published:
            _remove(target, missing_ok=True)
        message = f"{current.source.name}: {exc}. 관련 파일 묶음 전체를 원본에 유지했습니다"
        return [(document, "이동 실패", message) for document in members]
    results: list[tuple[Document, str, Path | str]] = []
    for document, _, target, temporary in staged:
        try:
            _remove(document.source)
            results.append((document, "이동" if temporary else "중복 정리", target))
        except OSError as exc:
            results.append((document, "이동 실패", f"원본 삭제 실패, 결과 폴더에 검증된 사본이 있습니다: {exc}"))
    return results


def move_documents(
    documents: list[Document], destination: Path,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    source_root: Path | None = None,
    record: Callable[[Document, str, Path | str], None] | None = None,
) -> tuple[list[tuple[Document, Path]], list[tuple[Document, Path]], list[tuple[Document, str]]]:
    """복사본과 원본의 내용이 같을 때만 원본을 제거한다. record는 파일마다 바로 호출된다."""
    destination = destination.resolve()
    source_root = source_root.resolve() if source_root else None
    if not destination.is_dir():
        raise ValueError("결과 폴더가 없습니다")
    previewed = [doc for doc in documents if doc.preview_digest is not None]
    if source_root and len(previewed) == len(documents):
        current = {path.resolve() for path in source_files(source_root, destination)[0]}
        if current != {doc.source.resolve() for doc in documents}:
            raise ValueError("미리보기 이후 파일 목록이 변경되었습니다. 미리보기를 다시 실행하세요")
    for document in previewed:
        if document.preview_digest == UNREADABLE:
            continue
        try:
            changed = (not document.source.is_file() or document.source.is_symlink()
                       or _digest(document.source) != document.preview_digest)
        except OSError:
            # 다른 프로그램이 잠근 파일은 이동 단계에서 그 파일(또는 묶음)만 실패로 남긴다.
            continue
        if changed:
            raise ValueError(f"미리보기 이후 파일 내용이 변경되었습니다. 미리보기를 다시 실행하세요: {document.source}")
    moved: list[tuple[Document, Path]] = []
    deduplicated: list[tuple[Document, Path]] = []
    errors: list[tuple[Document, str]] = []
    # 이번 실행에서 옮긴 파일은 폴더와 관계없이 같은 내용을 한 번만 남긴다(미리보기의 '중복' 표시와 같다).
    # 실행 전부터 결과 폴더에 있던 파일은 미리보기에 보이지 않았으므로, 지정한 폴더 안에 있을 때만 중복으로 본다.
    moved_by_digest: dict[bytes, Path] = {}
    folder_files: dict[Path, dict[bytes, Path]] = {}

    def existing_in(folder: Path) -> dict[bytes, Path]:
        if folder not in folder_files:
            index: dict[bytes, Path] = {}
            for path in sorted(folder.iterdir()):
                if (path.is_file() and not path.is_symlink() and path.suffix != ".partial"
                        and not is_structured_file(path)):
                    try:
                        index.setdefault(_digest(path), path)
                    except OSError:
                        continue
            folder_files[folder] = index
        return folder_files[folder]

    # GIS/프로그램 구성 파일은 원래 같은 폴더에 있던 묶음을 다른 결과 경로로 나누지 않는다.
    structured_routes: dict[tuple[Path, str], set[tuple[str, str, str]]] = {}
    for document in documents:
        if _structured_group(document):
            key = (document.source.parent, type_folder(document.source))
            structured_routes.setdefault(key, set()).add(
                (document.folder, document.subfolder, document.detail)
            )
    if any(len(routes) > 1 for routes in structured_routes.values()):
        raise ValueError("관련 구성 파일의 폴더가 서로 다릅니다. 같은 결과 폴더로 지정하세요")

    groups: dict[tuple[str, str, str, Path], list[Document]] = {}
    singles: list[Document] = []
    for document in documents:
        if group := _structured_group(document):
            groups.setdefault(group, []).append(document)
        else:
            singles.append(document)
    done = 0

    def finish(document: Document, status: str, value: Path | str) -> None:
        nonlocal done
        target = moved if status == "이동" else deduplicated if status == "중복 정리" else errors
        target.append((document, value))
        if record:
            record(document, status, value)
        done += 1
        if progress:
            progress(done, len(documents))

    for members in groups.values():
        if should_cancel and should_cancel():
            return moved, deduplicated, errors
        for document, status, value in _move_group(members, destination, source_root):
            finish(document, status, value)

    for document in singles:
        if should_cancel and should_cancel():
            break
        try:
            source = document.source
            folder, digest = _prepare(document, destination, source_root)
            matched = moved_by_digest.get(digest) or existing_in(folder).get(digest)
            if matched is not None:
                # 기존 결과 파일이나 이번 실행에서 옮긴 파일도 다시 읽고 확인한다.
                if _digest(source) != digest or _digest(matched) != digest:
                    raise ValueError("파일 내용이 검사 중 변경되었습니다")
                _remove(source)
                finish(document, "중복 정리", matched)
                continue
            temporary = _stage(source, folder, digest)
            try:
                while True:
                    target = _available_target(folder, source, document.output_name)
                    try:
                        temporary.rename(target)
                        break
                    except FileExistsError:
                        continue
            finally:
                _remove(temporary, missing_ok=True)
            if _digest(source) != digest or _digest(target) != digest:
                _remove(target, missing_ok=True)
                raise ValueError("이동 후 내용 검증 실패: 원본을 유지했습니다")
            try:
                _remove(source)
            except OSError as exc:
                # 원본을 못 지우면 방금 만든 사본을 지워, 기록(이동 실패)과 실제 상태를 맞춘다.
                try:
                    _remove(target)
                except OSError:
                    raise OSError(f"원본을 지울 수 없고 결과 사본도 남았습니다({target}): {exc}") from exc
                raise OSError(f"원본을 지울 수 없어 원본을 유지했습니다(다른 프로그램이 사용 중일 수 있음): {exc}") from exc
            moved_by_digest[digest] = target
            finish(document, "이동", target)
        except (OSError, ValueError) as exc:
            finish(document, "이동 실패", str(exc))
    return moved, deduplicated, errors


class ResultReport:
    """이동 도중 앱이 멈춰도 처리한 파일이 남도록 결과를 한 줄씩 바로 디스크에 쓴다."""

    def __init__(self, source: Path, destination: Path, documents: list[Document]) -> None:
        self.source = source
        self.documents = documents
        self.recorded: set[Path] = set()
        base = f"정리_결과_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        number = 1
        while True:
            self.path = destination / f"{base}{f'_{number}' if number > 1 else ''}.csv"
            try:
                self.stream = self.path.open("x", encoding="utf-8-sig", newline="")
                break
            except FileExistsError:
                number += 1
        self.writer = csv.writer(self.stream)
        # '원본 경로'는 되돌리기(undo_report)가 원래 위치를 찾는 데 쓴다.
        self._write(("원본", "폴더", "상태", "결과 파일", "분류 조건", "읽기 방식", "읽기 오류", "이동 오류",
                     "원본 경로"))

    def _write(self, row: tuple[str, ...]) -> None:
        self.writer.writerow(row)
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def record(self, doc: Document, status: str, value: Path | str = "") -> None:
        self.recorded.add(doc.source)
        result, error = (str(value), "") if isinstance(value, Path) else ("", value)
        self._write((
            str(doc.source.relative_to(self.source)),
            "/".join(route(doc)), status,
            result, doc.folder_reason, doc.method, doc.error, error, str(doc.source.absolute()),
        ))

    def close(self) -> None:
        """처리하지 못한 파일은 미처리로 남기고 기록을 닫는다."""
        if self.stream.closed:
            return
        try:
            for doc in self.documents:
                if doc.source not in self.recorded:
                    self.record(doc, "미처리")
        finally:
            self.stream.close()

    def __enter__(self) -> ResultReport:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def undo_jobs(report: Path) -> list[tuple[str, Path, Path]]:
    """정리 결과 기록에서 되돌릴 (상태, 결과 파일, 원래 위치) 목록을 읽는다."""
    with report.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or "원본 경로" not in rows[0]:
        raise ValueError("이 기록에는 원래 위치 정보가 없어 되돌릴 수 없습니다")
    jobs = [(row["상태"], Path(row["결과 파일"]), Path(row["원본 경로"]))
            for row in rows if row["상태"] in {"이동", "중복 정리"}]
    # 중복 정리된 원본은 남겨 둔 사본을 복사해 되살리므로, 그 사본이 되돌아가기 전에 먼저 처리한다.
    return sorted(jobs, key=lambda job: job[0] != "중복 정리")


def undo_report(
    report: Path,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, str]]]:
    """정리 결과 기록대로 파일을 원래 위치에 되돌린다. 이동할 때와 같이 복사·검증한 뒤에만 지운다."""
    destination = report.resolve().parent
    jobs = undo_jobs(report)
    restored: list[tuple[Path, Path]] = []
    errors: list[tuple[Path, str]] = []
    for index, (status, result, original) in enumerate(jobs, 1):
        if should_cancel and should_cancel():
            break
        try:
            # 기록이 고쳐졌어도 결과 폴더 밖의 파일을 옮기거나 결과 폴더 안으로 되돌리지 않는다.
            if (not result.is_absolute() or not original.is_absolute()
                    or not result.resolve().is_relative_to(destination)
                    or original.resolve().is_relative_to(destination)):
                raise ValueError("기록의 경로가 결과 폴더와 맞지 않습니다")
            if not result.is_file() or result.is_symlink():
                raise ValueError("결과 폴더에 파일이 없습니다")
            if original.exists() or original.is_symlink():
                raise ValueError("원래 위치에 같은 이름의 파일이 이미 있어 건너뛰었습니다")
            original.parent.mkdir(parents=True, exist_ok=True)
            digest = _digest(result)
            temporary = _stage(result, original.parent, digest)
            try:
                temporary.rename(original)
            finally:
                _remove(temporary, missing_ok=True)
            if _digest(original) != digest:
                _remove(original, missing_ok=True)
                raise ValueError("되돌린 파일 검증 실패: 결과 폴더의 파일을 유지했습니다")
            if status == "이동":
                try:
                    _remove(result)
                except OSError as exc:
                    raise OSError(f"원래 위치에 되돌렸지만 결과 폴더의 파일을 지우지 못해 두 곳에 있습니다: {exc}") from exc
            restored.append((result, original))
        except (OSError, ValueError) as exc:
            errors.append((original, str(exc)))
        finally:
            if progress:
                progress(index, len(jobs))
    # 비워진 결과 하위 폴더만 지운다. 파일이 남은 폴더와 결과 폴더 자체는 그대로 둔다.
    for current, _, _ in sorted(os.walk(destination), key=lambda item: -len(item[0])):
        if Path(current) != destination:
            try:
                os.rmdir(current)
            except OSError:
                pass
    return restored, errors
