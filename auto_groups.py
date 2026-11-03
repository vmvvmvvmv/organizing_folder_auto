"""문서에서 반복되는 단어로 폴더 이름을 제안한다."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from classification_rules import COMMON

WORDS = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}")


def _tokens(text: str) -> set[str]:
    return {
        word.casefold() for word in WORDS.findall(text)
        if word.casefold() not in COMMON and len(word) <= 24
    }


def _name_tokens(text: str) -> set[str]:
    terms = _tokens(text)
    # 띄어쓰지 않은 파일명도 앞부분이 같으면 묶을 수 있게 한다.
    for word in WORDS.findall(text):
        if re.fullmatch(r"[가-힣]{4,}", word):
            terms.update(
                prefix for length in range(2, min(4, len(word) - 1) + 1)
                if (prefix := word[:length]) not in COMMON
            )
    return terms


def propose_folders(files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """여러 문서에 반복되는 파일명·첫 제목 단어로 폴더를 제안한다."""
    if not files:
        return []

    full_names = Counter()
    prefixes = Counter()
    locations = defaultdict(set)
    for index, (filename, lead_text) in enumerate(files):
        name = Path(filename).stem
        full_terms = _tokens(name)
        name_terms = _name_tokens(name)
        title_terms = _tokens(lead_text[:240])
        full_names.update(full_terms)
        prefixes.update(name_terms - full_terms)
        for term in name_terms | title_terms:
            locations[term].add(index)

    minimum = 4 if len(files) >= 100 else 3 if len(files) >= 30 else 2
    candidates = [
        term for term, places in locations.items()
        if len(places) >= minimum
        and (len(files) < 10 or len(places) / len(files) <= 0.7)
    ]
    candidates.sort(key=lambda term: (-(full_names[term] * 4 + prefixes[term] + len(locations[term])), -len(locations[term]), -len(term), term))

    folders = [("미분류", "공통 단어 없음") for _ in files]
    available = set(range(len(files)))
    group_count = 0
    for term in candidates:
        if len(available) < minimum or group_count >= 20:
            break
        members = available & locations[term]
        if len(members) < minimum:
            continue
        for index in members:
            folders[index] = term, f"공통 단어: {term} ({len(members)}개 파일)"
        available -= members
        group_count += 1
    return folders
