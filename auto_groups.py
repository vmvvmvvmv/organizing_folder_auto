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


# 폴더 이름에 이 단어들만 있으면 안에 어떤 파일이 있는지 알 수 없다('업무 운영', '기술', '실습 자료').
# '사진'·'이미지'는 파일 종류를 알려 주므로 모호하지 않다(가족 사진 폴더를 '사진'으로 둔 경우 등).
VAGUE_WORDS = {"관리", "운영", "업무", "자료", "기술", "실습", "지원", "기타", "일반", "문서", "정보",
               "기록", "내용", "파일", "관련", "사항", "활동", "서비스", "전략", "기획", "분석", "모음",
               "데이터", "프로젝트", "참고", "보관", "작업", "개발", "제작", "콘텐츠"}


def is_vague_name(name: str) -> bool:
    # 주제 폴더 안의 연도 하위 폴더('2024')는 기록관리 지침에서도 쓰는 정리 방식이라 모호하지 않다.
    if re.fullmatch(r"(19|20)\d{2}(년)?", name.strip()):
        return False
    words = [word.casefold() for word in WORDS.findall(name)]
    return not words or all(word in VAGUE_WORDS or word in COMMON for word in words)


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
