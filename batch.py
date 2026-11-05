"""GUI와 같은 분류 함수를 이용해 대량 폴더를 미리보고 적용한다."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from organizer import Document, default_destination, move_documents, scan, ResultReport
from reader import SUPPORTED, find_tesseract


def default_manifest(source: Path) -> Path:
    key = hashlib.sha256(str(source.resolve()).casefold().encode("utf-8")).hexdigest()[:12]
    return Path(__file__).resolve().parent / "runs" / f"preview_{key}.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preview", "apply"))
    parser.add_argument("source", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    manifest = args.manifest.resolve() if args.manifest else default_manifest(source)
    destination = default_destination(source)

    if args.action == "preview":
        documents = scan(
            source, destination, find_tesseract(),
            lambda done, total: print(f"읽기: {done}/{total}", flush=True) if done % 25 == 0 or done == total else None,
        )
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text(json.dumps({
            "source": str(source),
            "destination": str(destination),
            "format_version": 5,
            "documents": [{
                "source": str(doc.source),
                "folder": doc.folder,
                "subfolder": doc.subfolder,
                "folder_reason": doc.folder_reason,
                "method": doc.method,
                "error": doc.error,
                "duplicate_of": str(doc.duplicate_of) if doc.duplicate_of else None,
                "size": doc.source.stat().st_size,
                "mtime_ns": doc.source.stat().st_mtime_ns,
                "sha256": doc.preview_digest.hex(),
            } for doc in documents],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "total": len(documents),
            "folders": dict(Counter(doc.folder for doc in documents)),
            "duplicates": sum(doc.duplicate_of is not None for doc in documents),
            "read_failures": sum(bool(doc.error) for doc in documents),
            "filename_only": sum(doc.source.suffix.lower() not in SUPPORTED for doc in documents),
            "manifest": str(manifest),
        }, ensure_ascii=True))
        return

    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("format_version") != 5:
        raise ValueError("이전 형식의 미리보기입니다. preview를 다시 실행하세요")
    if Path(data["source"]).resolve() != source or Path(data["destination"]).resolve() != destination.resolve():
        raise ValueError("미리보기의 원본/결과 폴더가 다릅니다")
    documents = []
    for item in data["documents"]:
        path = Path(item["source"])
        stat = path.stat()
        # 미리보기 이후 원본이 바뀌었으면 예전 분류로 이동하지 않는다.
        if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
            raise ValueError(f"미리보기 이후 파일이 변경되었습니다: {path}")
        documents.append(Document(
            path, item["folder"], item["folder_reason"], item["method"],
            item["error"], Path(item["duplicate_of"]) if item["duplicate_of"] else None,
            preview_digest=bytes.fromhex(item["sha256"]),
            subfolder=item.get("subfolder", ""),
        ))
    destination.mkdir(exist_ok=True)
    with ResultReport(source, destination, documents) as report:
        moved, deduplicated, errors = move_documents(
            documents, destination, source_root=source, record=report.record,
        )
    print(json.dumps({
        "moved": len(moved),
        "duplicates_removed": len(deduplicated),
        "errors": [(str(doc.source), message) for doc, message in errors],
        "destination": str(destination),
        "report": str(report.path),
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
