"""원본을 옮기지 않고 앱의 로컬 AI 분류 경로를 정답 목록과 비교한다.

    python quality_eval.py <원본 폴더> --labels a.csv [--labels b.csv] --output 결과.csv
        [--documents 본문.jsonl] [--cache ollama_cache.json]

--documents: 이전에 읽은 본문(JSONL)을 써서 OCR을 건너뛴다.
--cache: Ollama 응답을 요청 내용별로 저장한다. 코드 수정 전후를 같은 캐시로 돌리면
         모델 응답이 같으므로 결과 차이는 코드 변경에서만 생긴다.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from collections.abc import Callable
from math import comb
from pathlib import Path

import ai_groups
from ai_groups import assign_ai_folders
from organizer import Document, scan
from reader import find_tesseract


def score_labels(expected: list[str], predicted: list[str]) -> dict:
    """폴더 이름이 달라도 같은 문서를 함께 묶었는지 비교한다."""
    if len(expected) != len(predicted):
        raise ValueError("기대값과 분류 결과의 파일 수가 다릅니다")
    expected_sizes = Counter(expected)
    predicted_sizes = Counter(predicted)
    intersections = Counter(zip(expected, predicted))
    true_positive = sum(
        comb(size, 2) for (_, suggested), size in intersections.items()
        if suggested != "미분류"
    )
    expected_pairs = sum(comb(size, 2) for size in expected_sizes.values())
    predicted_pairs = sum(
        comb(size, 2) for name, size in predicted_sizes.items()
        if name != "미분류"
    )
    precision = true_positive / predicted_pairs if predicted_pairs else 0.0
    recall = true_positive / expected_pairs if expected_pairs else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "files": len(expected),
        "folder_count": sum(name != "미분류" for name in predicted_sizes),
        "unclassified": predicted_sizes["미분류"],
        "true_positive_pairs": true_positive,
        "predicted_pairs": predicted_pairs,
        "expected_pairs": expected_pairs,
        "pair_precision": precision,
        "pair_recall": recall,
        "pair_f1": f1,
    }


def cached_ollama(cache_file: Path) -> tuple[dict[str, Callable], Callable[[], None]]:
    """앱과 같은 Ollama 호출을 하되 같은 요청은 저장된 응답(오류 포함)을 다시 쓴다.

    assign_ai_folders(**calls)에 그대로 넘길 함수 묶음과 캐시 저장 함수를 돌려준다.
    """
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}

    def cached(kind: str, payload: object, call: Callable[[], list]) -> list:
        key = kind + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if key not in cache:
            try:
                cache[key] = {"ok": call()}
            except (ai_groups.ContextSizeError, ai_groups.IncompleteResultError) as exc:
                cache[key] = {"error": type(exc).__name__}
        hit = cache[key]
        if "error" in hit:
            raise getattr(ai_groups, hit["error"])("저장된 Ollama 오류")
        return hit["ok"] if kind == "embed" else [tuple(answer) for answer in hit["ok"]]

    def embed(texts: list[str]) -> list[list[float]]:
        return cached("embed", texts, lambda: ai_groups.ollama_embeddings(texts))

    def classify(items: list[dict], known: list[str]) -> list[tuple[str, str]]:
        # 앱의 AI 분류와 같은 지시문을 쓴다.
        return cached("classify", [items, known], lambda: ai_groups.ollama_classify(
            items, known, instructions_override=ai_groups.AI_ONLY_INSTRUCTIONS))

    def subdivide(items: list[dict], parent: str) -> list[tuple[str, str]]:
        return cached("sub", [items, parent], lambda: ai_groups.ollama_subdivide(items, parent))

    def save() -> None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    calls = {"embed": embed, "classify": classify, "subdivide": subdivide}
    return calls, save


def load_documents(source: Path, documents_file: Path) -> list[Document]:
    """이전에 읽은 본문으로 앱의 AI 분류 직전 상태(scan(classify=False))를 만든다."""
    documents = []
    with documents_file.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            path = source / row["relative_path"]
            documents.append(Document(
                path, "미분류", "로컬 AI 분류 대기", row["method"], row["error"],
                source / row["duplicate_of"] if row["duplicate_of"] else None,
                row["lead_text"], bytes.fromhex(row["sha256"]),
                preserve_structure=ai_groups.is_structured_file(path),
            ))
    return documents


def evaluate(
    source: Path, labels_files: list[Path], output_file: Path,
    documents_file: Path | None = None, cache_file: Path | None = None,
) -> dict:
    source = source.resolve()
    started = time.perf_counter()
    documents = (load_documents(source, documents_file) if documents_file
                 else scan(source, source.parent / (source.name + "_평가결과"),
                           find_tesseract(), classify=False))
    scan_seconds = time.perf_counter() - started
    by_relative = {doc.source.relative_to(source).as_posix(): doc for doc in documents}

    # 분류 전에 파일이 평가 목록을 만들 때와 같은지 확인한다.
    labels: dict[str, dict[str, str]] = {}
    for labels_file in labels_files:
        with labels_file.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            raise ValueError(f"평가 목록이 비어 있습니다: {labels_file}")
        labels[labels_file.name] = {}
        for row in rows:
            relative = row["relative_path"].replace("\\", "/")
            document = by_relative.get(relative)
            if document is None:
                raise ValueError(f"평가 파일을 찾을 수 없습니다: {row['relative_path']}")
            if document.preview_digest.hex() != row["sha256"]:
                raise ValueError(f"평가 파일이 변경되었습니다: {row['relative_path']}")
            labels[labels_file.name][relative] = row["expected_folder"]

    calls: dict[str, Callable] = {}
    save = None
    if cache_file:
        calls, save = cached_ollama(cache_file)
    try:
        assign_ai_folders(documents, ai_only=True, **calls)
    finally:
        if save:
            save()
    classify_seconds = time.perf_counter() - started - scan_seconds

    sizes = Counter(doc.folder for doc in documents if doc.folder != "미분류")
    scores: dict = {
        "scanned_files": len(documents),
        "scan_seconds": round(scan_seconds, 1),
        "classify_seconds": round(classify_seconds, 1),
        "all_folder_count": len(sizes),
        "folders_with_1_2_files": sum(size <= 2 for size in sizes.values()),
        "all_unclassified": sum(doc.folder == "미분류" for doc in documents),
        "structured_unclassified": sum(doc.folder == "미분류" and doc.preserve_structure
                                       for doc in documents),
        "retried": sum("재판정" in doc.folder_reason for doc in documents),
        "merged_small_folders": sum("작은 폴더 병합" in doc.folder_reason for doc in documents),
        "consolidated": sum("폴더 통합" in doc.folder_reason for doc in documents),
        "read_failures": sum(bool(doc.error) for doc in documents),
    }
    for name, expected in labels.items():
        paths = list(expected)
        scores[name] = score_labels([expected[path] for path in paths],
                                    [by_relative[path].folder for path in paths])

    expected_any = {path: folder for rows in labels.values() for path, folder in rows.items()}
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("relative_path", "expected_folder", "suggested_folder",
                         "suggested_subfolder", "reason", "method", "error"))
        for relative, doc in sorted(by_relative.items()):
            writer.writerow((relative, expected_any.get(relative, ""), doc.folder,
                             doc.subfolder, doc.folder_reason, doc.method, doc.error))
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description="앱의 로컬 AI 분류를 읽기 전용으로 평가")
    parser.add_argument("source", type=Path)
    parser.add_argument("--labels", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--documents", type=Path, help="이전에 읽은 본문 JSONL (OCR 생략)")
    parser.add_argument("--cache", type=Path, help="Ollama 응답 캐시 JSON")
    args = parser.parse_args()
    print(json.dumps(
        evaluate(args.source, args.labels, args.output, args.documents, args.cache),
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
