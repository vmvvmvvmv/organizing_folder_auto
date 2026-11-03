"""문서 본문과 OCR 텍스트를 추출한다."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".docx", ".hwpx", ".xlsx", ".pptx"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
MAX_FILE_BYTES = 100 * 1024 * 1024
LOCAL_TESSDATA = Path(__file__).resolve().parent / "models"
NO_CONSOLE = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _xml_text(data: bytes) -> str:
    root = ET.fromstring(data)
    return " ".join(
        node.text or ""
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] in {"t", "text"} and node.text
    )


def _office_text(path: Path) -> str:
    # Office 계열 파일은 ZIP 안에 XML이 들어 있다. 본문 위치만 형식마다 다르다.
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        suffix = path.suffix.lower()
        if suffix == ".docx":
            members = [name for name in names if name == "word/document.xml"]
        elif suffix == ".hwpx":
            members = sorted(
                (name for name in names if re.fullmatch(r"Contents/section\d+\.xml", name, re.I)),
                key=lambda name: int(re.search(r"section(\d+)", name, re.I).group(1)),
            )
        elif suffix == ".xlsx":
            members = [name for name in names if name == "xl/sharedStrings.xml"]
            members += sorted(
                (name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name, re.I)),
                key=lambda name: int(re.search(r"sheet(\d+)", name, re.I).group(1)),
            )
        else:
            members = sorted(
                (name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name, re.I)),
                key=lambda name: int(re.search(r"slide(\d+)", name, re.I).group(1)),
            )
        if not members:
            raise ValueError("문서 본문을 찾을 수 없습니다")
        chunks = []
        for name in members:
            member = archive.getinfo(name)
            # XML의 압축 해제 후 크기가 지나치게 크면 읽지 않고 넘긴다.
            if member.file_size > 20 * 1024 * 1024:
                raise ValueError("문서 본문이 너무 큽니다")
            chunks.append(_xml_text(archive.read(name)))
        return " ".join(chunks)


def find_tesseract() -> str | None:
    bundled = Path(sys.executable).resolve().parent / "ocr" / "tesseract.exe"
    if bundled.is_file():
        return str(bundled)
    candidate = shutil.which("tesseract")
    if candidate:
        return candidate
    roots = [
        Path(os.environ["ProgramFiles"]) if os.environ.get("ProgramFiles") else None,
        Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else None,
        Path(os.environ["LOCALAPPDATA"]) / "Programs" if os.environ.get("LOCALAPPDATA") else None,
    ]
    for root in roots:
        if root and (root / "Tesseract-OCR" / "tesseract.exe").is_file():
            return str(root / "Tesseract-OCR" / "tesseract.exe")
    return None


def ocr_status(executable: str | None) -> str:
    if not executable:
        return "Tesseract OCR 미설치: 사진·스캔 PDF는 아직 읽을 수 없습니다"
    try:
        args = [executable, "--list-langs"]
        if (LOCAL_TESSDATA / "kor.traineddata").is_file():
            args += ["--tessdata-dir", str(LOCAL_TESSDATA)]
        result = subprocess.run(
            args, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=10, check=True, encoding="utf-8", errors="replace",
            creationflags=NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"OCR 확인 실패: {exc}"
    langs = set((result.stdout + result.stderr).split())
    if "kor" not in langs:
        return "Tesseract에 한국어(kor) 언어 데이터가 없습니다"
    return "한국어 OCR 사용 가능"


def _ocr_image(path: Path, executable: str | None) -> str:
    if ocr_status(executable) != "한국어 OCR 사용 가능":
        raise RuntimeError("한국어 OCR을 사용할 수 없습니다. Tesseract와 kor 언어 데이터를 설치하세요")
    args = [executable, str(path), "stdout", "-l", "kor"]
    if (LOCAL_TESSDATA / "kor.traineddata").is_file():
        args += ["--tessdata-dir", str(LOCAL_TESSDATA)]
    result = subprocess.run(
        args,
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=90,
        encoding="utf-8", errors="replace",
        creationflags=NO_CONSOLE,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "OCR 실행 실패")
    return result.stdout


def _pdf_text(path: Path, executable: str | None) -> tuple[str, str]:
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PDF 읽기용 PyMuPDF가 설치되지 않았습니다") from exc

    chunks = []
    used_ocr = False
    skipped_ocr = False
    with pymupdf.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError("암호가 걸린 PDF입니다")
        for page_number in range(min(len(pdf), 5)):
            page = pdf[page_number]
            text = page.get_text()
            # 글자가 거의 없는 페이지는 스캔본으로 보고 OCR을 시도한다.
            if len(_compact(text)) >= 30:
                chunks.append(text)
                continue
            if ocr_status(executable) != "한국어 OCR 사용 가능":
                if text.strip():
                    chunks.append(text)
                skipped_ocr = True
                continue
            used_ocr = True
            with tempfile.TemporaryDirectory() as temporary:
                image_path = Path(temporary) / "page.png"
                page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).save(image_path)
                chunks.append((text + " " + _ocr_image(image_path, executable)).strip())
    if skipped_ocr and not any(chunk.strip() for chunk in chunks):
        raise RuntimeError("스캔 PDF를 읽으려면 한국어 OCR(Tesseract)이 필요합니다")
    method = "PDF 텍스트 + OCR" if used_ocr else "PDF 텍스트"
    if skipped_ocr:
        method += " (일부 스캔 페이지 미인식)"
    return " ".join(chunks), method


def read_document(path: Path, executable: str | None) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("파일이 100MB를 넘습니다")
    try:
        if suffix in {".docx", ".hwpx", ".xlsx", ".pptx"}:
            return _office_text(path), suffix[1:].upper() + " 텍스트"
        if suffix == ".pdf":
            return _pdf_text(path, executable)
        if suffix in IMAGE_TYPES:
            return _ocr_image(path, executable), "이미지 OCR"
    except (zipfile.BadZipFile, ET.ParseError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc)) from exc
    raise ValueError("지원하지 않는 형식입니다")
