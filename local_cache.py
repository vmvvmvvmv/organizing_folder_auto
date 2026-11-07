"""미리보기를 다시 할 때 빠르도록 해시·본문·AI 응답을 이 PC에만 저장한다.

- 파일 해시: 경로·크기·수정 시각이 같으면 다시 읽지 않는다. 이동할 때는 실제 내용을 다시 해시해
  미리보기 해시와 비교하므로, 저장된 해시가 틀려도 파일이 잘못 지워지지 않는다(이동이 거부될 뿐이다).
- 본문·OCR: 내용 해시가 같으면 다시 읽지 않는다.
- Ollama 응답: 같은 요청이면 다시 묻지 않는다.

문서 본문 일부가 저장되므로 앱에서 끌 수 있고 지울 수 있다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path

import ai_groups

# 읽기 방식(reader.py)이 바뀌면 올려서 예전에 저장한 본문을 쓰지 않게 한다.
READER_VERSION = "2026-11-06"


def default_cache_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "DocumentOrganizer" / "cache.sqlite3"


class LocalCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # 작업 스레드에서 만들고 그 스레드에서만 쓴다.
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (space TEXT, key TEXT, value TEXT, "
                        "PRIMARY KEY (space, key))")
        self.pending = 0

    def get(self, space: str, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE space = ? AND key = ?", (space, key)).fetchone()
        return row[0] if row else None

    def put(self, space: str, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (space, key, value))
        self.pending += 1
        if self.pending >= 50:
            self.commit()

    def commit(self) -> None:
        self.db.commit()
        self.pending = 0

    def close(self) -> None:
        self.commit()
        self.db.close()

    def __enter__(self) -> LocalCache:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def digest(self, path: Path, compute: Callable[[Path], bytes]) -> bytes:
        stat = path.stat()
        key = f"{path.absolute()}|{stat.st_size}|{stat.st_mtime_ns}"
        if (value := self.get("digest", key)) is not None:
            return bytes.fromhex(value)
        digest = compute(path)
        self.put("digest", key, digest.hex())
        return digest

    def read(self, digest: bytes, has_ocr: bool, read: Callable[[], tuple[str, str]]) -> tuple[str, str]:
        """같은 내용의 파일은 본문을 다시 읽지 않는다. 읽기 실패는 저장하지 않는다."""
        key = f"{digest.hex()}|{READER_VERSION}|ocr={has_ocr}"
        if (value := self.get("text", key)) is not None:
            text, method = json.loads(value)
            return text, method
        text, method = read()
        self.put("text", key, json.dumps([text[:1200], method], ensure_ascii=False))
        return text, method

    def responses(self, fresh: bool = False) -> MutableMapping[str, dict]:
        """fresh=True면 저장된 AI 답을 쓰지 않고 새로 묻는다. 새로 받은 답은 저장해 다음에 쓴다."""
        return _Responses(self, fresh)


class _Responses(MutableMapping):
    """cached_ollama가 쓰는 dict 모양의 저장소. 모델이 바뀌면 예전 응답을 쓰지 않도록 모델별로 나눈다."""

    def __init__(self, cache: LocalCache, fresh: bool = False) -> None:
        self.cache = cache
        self.space = f"ollama|{ai_groups.MODEL}|{ai_groups.EMBED_MODEL}"
        self.fresh = fresh
        self.renewed: set[str] = set()  # 이번에 새로 받은 답

    def __getitem__(self, key: str) -> dict:
        if self.fresh and key not in self.renewed:
            raise KeyError(key)  # 예전 답은 없는 것처럼 다룬다.
        value = self.cache.get(self.space, key)
        if value is None:
            raise KeyError(key)
        return json.loads(value)

    def __setitem__(self, key: str, value: dict) -> None:
        self.renewed.add(key)
        self.cache.put(self.space, key, json.dumps(value, ensure_ascii=False))

    def __delitem__(self, key: str) -> None:
        self.cache.db.execute("DELETE FROM kv WHERE space = ? AND key = ?", (self.space, key))

    def __iter__(self) -> Iterator[str]:
        return (row[0] for row in self.cache.db.execute("SELECT key FROM kv WHERE space = ?", (self.space,)))

    def __len__(self) -> int:
        return self.cache.db.execute("SELECT COUNT(*) FROM kv WHERE space = ?", (self.space,)).fetchone()[0]


def clear_cache(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def cached_hierarchy(store: MutableMapping) -> dict[str, Callable]:
    """대분류 묶기(ai_hierarchy.add_top_level)에 넘길 Ollama 함수 묶음. 지시문이 바뀌면 다시 묻는다."""
    import ai_hierarchy

    def cached(kind: str, call: Callable, *args: object) -> object:
        key = "hierarchy-" + kind + json.dumps(
            [ai_hierarchy.TAXONOMY_INSTRUCTIONS, ai_hierarchy.MIDDLE_INSTRUCTIONS,
             ai_hierarchy.PICK_INSTRUCTIONS, args],
            ensure_ascii=False, sort_keys=True)
        if key not in store:
            store[key] = {"ok": call(*args)}
        return store[key]["ok"]

    return {
        "taxonomy": lambda folders, low, high, parent="": cached(
            "taxonomy", ai_hierarchy.ollama_taxonomy, folders, low, high, parent),
        "pick": lambda folders, categories: cached("pick", ai_hierarchy.ollama_pick, folders, categories),
        "embed": lambda texts: cached("embed", ai_groups.ollama_embeddings, texts),
    }


def cached_ollama(store: MutableMapping) -> dict[str, Callable]:
    """앱과 같은 Ollama 호출을 하되 같은 요청은 저장된 응답(오류 포함)을 다시 쓴다.

    assign_ai_folders(**calls)에 그대로 넘길 함수 묶음을 돌려준다.
    """

    def cached(kind: str, payload: object, call: Callable[[], list]) -> list:
        key = kind + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if key not in store:
            try:
                store[key] = {"ok": call()}
            except (ai_groups.ContextSizeError, ai_groups.IncompleteResultError) as exc:
                store[key] = {"error": type(exc).__name__}
        hit = store[key]
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

    return {"embed": embed, "classify": classify, "subdivide": subdivide}
