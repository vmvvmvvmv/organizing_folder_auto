"""로컬 임베딩으로 미분류 문서를 기존 폴더에 제안한다."""

from __future__ import annotations

import http.client
import json
import math
from collections import defaultdict
from collections.abc import Callable

from organizer import Document
from reader import SUPPORTED

MODEL = "qwen3-embedding:0.6b"
MIN_SIMILARITY = 0.82
MIN_MARGIN = 0.08


def ollama_embeddings(texts: list[str]) -> list[list[float]]:
    """문서 내용은 PC의 Ollama에만 보낸다. 모델은 별도로 설치한다."""
    vectors = []
    for start in range(0, len(texts), 16):
        connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=30)
        payload = json.dumps({
            "model": MODEL,
            "input": texts[start:start + 16],
            "truncate": False,
        }, ensure_ascii=False).encode("utf-8")
        try:
            connection.request("POST", "/api/embed", payload, {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status == 404:
                raise RuntimeError(f"AI 모델이 없습니다. ollama pull {MODEL} 을 실행하세요.")
            if response.status != 200:
                raise RuntimeError(f"Ollama 응답 오류: HTTP {response.status}")
            batch = json.loads(response.read())["embeddings"]
            if not isinstance(batch, list) or len(batch) != len(texts[start:start + 16]):
                raise ValueError("Ollama 임베딩 응답의 문서 수가 다릅니다")
            vectors.extend(batch)
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError("Ollama에 연결할 수 없습니다. 설치·실행 상태를 확인하세요.") from exc
        finally:
            connection.close()
    return vectors


def _unit(vector: list[float]) -> list[float]:
    if not isinstance(vector, list) or not vector or any(
        not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector
    ):
        raise ValueError("Ollama 임베딩 값이 올바르지 않습니다")
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        raise ValueError("Ollama 임베딩 값이 비어 있습니다")
    return [value / norm for value in vector]


def assign_ai_folders(
    documents: list[Document],
    embed: Callable[[list[str]], list[list[float]]] = ollama_embeddings,
) -> int:
    """확실히 비슷한 미분류 문서만 기존 폴더에 합친다. 오류 시 아무것도 바꾸지 않는다."""
    originals = [
        doc for doc in documents
        if doc.duplicate_of is None and not doc.error and doc.lead_text.strip()
        and doc.source.suffix.lower() in SUPPORTED
    ]
    seeds = [doc for doc in originals if doc.folder != "미분류"]
    unknown = [doc for doc in originals if doc.folder == "미분류"]
    if not seeds or not unknown:
        return 0

    ordered = seeds + unknown
    vectors = embed([f"{doc.source.name}\n{doc.lead_text}" for doc in ordered])
    if len(vectors) != len(ordered):
        raise ValueError("Ollama 임베딩 응답의 문서 수가 다릅니다")
    vectors = [_unit(vector) for vector in vectors]
    dimensions = len(vectors[0])
    if any(len(vector) != dimensions for vector in vectors):
        raise ValueError("Ollama 임베딩 차원이 서로 다릅니다")

    grouped = defaultdict(list)
    for doc, vector in zip(seeds, vectors):
        grouped[doc.folder].append(vector)
    centroids = {}
    for folder, members in grouped.items():
        centroids[folder] = _unit([sum(member[i] for member in members) for i in range(dimensions)])

    # 전부 계산한 뒤에만 반영해야 연결 실패 시 기존 미리보기가 그대로 남는다.
    suggestions = []
    for doc, vector in zip(unknown, vectors[len(seeds):]):
        scores = sorted(
            ((sum(a * b for a, b in zip(vector, center)), folder) for folder, center in centroids.items()),
            reverse=True,
        )
        best, folder = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else -1.0
        if best >= MIN_SIMILARITY and best - runner_up >= MIN_MARGIN:
            suggestions.append((doc, folder, best))

    for doc, folder, score in suggestions:
        doc.folder = folder
        doc.folder_reason = f"AI 유사 문서: {score:.2f}"
    changed = {doc.source: doc for doc, _, _ in suggestions}
    for doc in documents:
        original = changed.get(doc.duplicate_of)
        if original:
            doc.folder = original.folder
            doc.folder_reason = original.folder_reason
    return len(suggestions)