"""정리 결과 폴더 구조를 기록관리 지침에서 공통으로 권하는 원칙에 비춰 채점한다(100점).

정답(맞는 주제)이 있는지와는 별개로, '사람이 찾기 좋은 구조인가'를 본다. 근거:
- 기능·업무 기준으로 나눈다(형식·사람·날짜 기준 아님): Manitoba Archives, Tasmania OSA Advice 66,
  University of Washington Records Management, 정부기능분류체계(BRM).
- 얕게 둔다(2~3단계, 많아도 4~5단계): Johnny.Decimal, London 지침, University of Wisconsin.
- 한 단계의 폴더 수를 제한한다(최상위·각 단계 10개 안팎): Johnny.Decimal.
- 하위 폴더를 지나치게 늘리지 않는다: University of Washington.
- 폴더 이름은 짧게: University of Wisconsin.
- '미분류'를 줄인다: 이 앱의 기준(위 지침에서 직접 확인한 근거는 없다).
- 이름만 보고 안에 무엇이 있는지 알 수 있어야 한다: 이 앱의 기준. '관리'·'운영'·'업무' 같은 넓은 말만으로
  지은 이름(구체적 이름)과, 폴더 이름의 단어가 안의 파일 이름·본문에 실제로 나오는지(이름 대표성)를 본다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from auto_groups import VAGUE_WORDS, _tokens, is_vague_name
from classification_rules import TYPE_FOLDERS
from organizer import Document

FORMAT_FOLDERS = {*TYPE_FOLDERS.values(), "GIS 자료", "버전 관리 설정", "사진·이미지"}
WEIGHTS = {"기능 기준": 15, "최상위 폴더 수": 15, "하위 폴더 수": 10, "작은 폴더 적음": 15,
           "이름 겹침 없음": 10, "짧은 이름": 5, "구체적 이름": 10, "이름 대표성": 10, "미분류 적음": 10}


@dataclass
class StructureScore:
    score: int
    parts: dict[str, float]  # 항목별 0~1
    facts: dict[str, float]

    def summary(self) -> str:
        worst = sorted(self.parts.items(), key=lambda item: item[1])[:2]
        return f"구조 점수 {self.score}점 · 약한 항목: " + ", ".join(f"{name} {value:.0%}" for name, value in worst)


def _count_score(count: int, good: int, bad: int) -> float:
    """good 개 이하면 1, bad 개 이상이면 0, 사이는 직선으로 줄인다."""
    if count <= good:
        return 1.0
    if count >= bad:
        return 0.0
    return 1 - (count - good) / (bad - good)


def structure_score(documents: list[Document]) -> StructureScore:
    originals = [doc for doc in documents if doc.duplicate_of is None]
    total = len(originals) or 1
    placed = [doc for doc in originals if doc.folder and doc.folder != "미분류"]
    tops = Counter(doc.folder for doc in placed)
    leaves = Counter((doc.folder, doc.subfolder) for doc in placed)
    subs = Counter(folder for folder, sub in leaves if sub)  # 최상위 폴더마다 하위 폴더 수
    format_share = sum(doc.folder in FORMAT_FOLDERS for doc in placed) / (len(placed) or 1)
    # 최종 위치(하위 폴더가 있으면 하위 폴더) 기준으로 파일 1~2개짜리 폴더 비율
    small_share = sum(size <= 2 for size in leaves.values()) / (len(leaves) or 1)
    topic_tops = [name for name in tops if name not in FORMAT_FOLDERS]
    words = {name: _tokens(name) for name in topic_tops}
    overlapping = sum(any(words[name] & words[other] for other in topic_tops if other != name)
                      for name in topic_tops) / (len(topic_tops) or 1)
    names = list(tops) + [sub for _, sub in leaves if sub]
    short = sum(len(name) <= 15 for name in names) / (len(names) or 1)
    biggest_sub = max(subs.values(), default=0)
    topic_names = [name for name in names if name not in FORMAT_FOLDERS]
    concrete = sum(not is_vague_name(name) for name in topic_names) / (len(topic_names) or 1)
    topical_docs = [doc for doc in placed if doc.folder not in FORMAT_FOLDERS]

    def represents(doc: Document) -> bool:
        text = f"{doc.source.stem} {doc.lead_text}".casefold().replace(" ", "")
        terms = [term for name in (doc.folder, doc.subfolder) if name
                 for term in _tokens(name) if term not in VAGUE_WORDS]
        return any(term in text for term in terms)

    representative = sum(map(represents, topical_docs)) / (len(topical_docs) or 1)
    parts = {
        "기능 기준": 1 - format_share,
        "최상위 폴더 수": _count_score(len(tops), 10, 40),
        "하위 폴더 수": _count_score(biggest_sub, 10, 30),
        "작은 폴더 적음": 1 - small_share,
        "이름 겹침 없음": 1 - overlapping,
        "짧은 이름": short,
        "구체적 이름": concrete,
        "이름 대표성": representative,
        "미분류 적음": 1 - (total - len(placed)) / total,
    }
    score = round(sum(WEIGHTS[name] * value for name, value in parts.items()))
    facts = {"최상위 폴더": len(tops), "최종 폴더": len(leaves), "형식 폴더 비율": format_share,
             "작은 폴더 비율": small_share, "이름 겹치는 최상위 비율": overlapping,
             "가장 많은 하위 폴더": biggest_sub}
    return StructureScore(score, parts, facts)
