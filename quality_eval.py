"""원본을 옮기지 않고 같은 평가 문서로 분류 결과를 비교한다."""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from math import comb
from pathlib import Path

from ai_groups import assign_ai_folders
from organizer import scan
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


def evaluate(source: Path, labels_file: Path, output_file: Path) -> dict:
    source = source.resolve()
    started = time.perf_counter()
    documents = scan(source, source.parent / (source.name + "_평가결과"), find_tesseract())
    scan_seconds = time.perf_counter() - started
    by_path = {doc.source: doc for doc in documents}

    # 분류 전에 파일이 평가 목록을 만들 때와 같은지 확인한다.
    with labels_file.open(encoding="utf-8-sig", newline="") as handle:
        labels = list(csv.DictReader(handle))
    if not labels:
        raise ValueError("평가 목록이 비어 있습니다")
    selected = []
    for row in labels:
        path = (source / row["relative_path"]).resolve()
        if not path.is_relative_to(source) or path not in by_path:
            raise ValueError(f"평가 파일을 찾을 수 없습니다: {row['relative_path']}")
        document = by_path[path]
        if document.preview_digest.hex() != row["sha256"]:
            raise ValueError(f"평가 파일이 변경되었습니다: {row['relative_path']}")
        selected.append((row, document))

    assign_ai_folders(documents)
    classify_seconds = time.perf_counter() - started - scan_seconds
    predictions = [doc.folder for _, doc in selected]
    scores = score_labels([row["expected_folder"] for row, _ in selected], predictions)
    scores.update({
        "scanned_files": len(documents),
        "scan_seconds": round(scan_seconds, 1),
        "classify_seconds": round(classify_seconds, 1),
        "all_folder_count": len({doc.folder for doc in documents if doc.folder != "미분류"}),
        "all_unclassified": sum(doc.folder == "미분류" for doc in documents),
        "read_failures": sum(bool(doc.error) for doc in documents),
    })
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "relative_path", "expected_folder", "suggested_folder",
            "suggested_subfolder", "method", "error",
        ))
        writer.writeheader()
        for row, doc in selected:
            writer.writerow({
                "relative_path": row["relative_path"],
                "expected_folder": row["expected_folder"],
                "suggested_folder": doc.folder,
                "suggested_subfolder": doc.subfolder,
                "method": doc.method,
                "error": doc.error,
            })
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description="실제 문서 분류를 읽기 전용으로 평가")
    parser.add_argument("source", type=Path)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.source, args.labels, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
