"""정답 없이 미리보기 결과에서 '다시 확인해야 할 신호'를 찾는다.

하나의 점수로 정확도를 대신하지 않는다. 정답이 있는 평가 폴더 7곳에서 분류율·집중도·근거율을 합친
점수를 만들어 보았지만 실제 정확도(쌍 F1)와 순위가 맞지 않았다(Spearman 0.00). 대신 실제로
정확도가 낮았던 경우에 나타난 신호만 경고로 보여준다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from auto_groups import _tokens
from classification_rules import TYPE_FOLDERS
from organizer import Document

# 형식으로 모은 폴더는 주제 근거를 따지지 않는다.
FORMAT_FOLDERS = {*TYPE_FOLDERS.values(), "GIS 자료", "버전 관리 설정"}
IMAGE_FOLDER = "사진·이미지"


@dataclass
class QualityCheck:
    unclassified: float  # 미분류 비율
    images_unsorted: float  # 내용으로 나누지 못하고 '사진·이미지'에 모인 비율
    small_folders: float  # 파일 1~2개짜리 주제 폴더 비율
    no_evidence: float  # 폴더 이름의 단어가 파일명·본문에 없는 주제 폴더 파일 비율
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = (f"점검 · 미분류 {self.unclassified:.0%} · 사진 묶음 {self.images_unsorted:.0%} · "
                f"작은 폴더 {self.small_folders:.0%} · 근거 약함 {self.no_evidence:.0%}")
        return head + (" — 확인: " + "; ".join(self.warnings) if self.warnings else " — 이상 없음")


def _has_evidence(doc: Document) -> bool:
    text = re.sub(r"[^가-힣A-Za-z]", "", f"{doc.source.stem} {doc.lead_text}").casefold()
    names = [name for name in (doc.folder, doc.subfolder, doc.detail) if name]
    return any(term in text for name in names for term in _tokens(name))


def quality_check(documents: list[Document]) -> QualityCheck:
    originals = [doc for doc in documents if doc.duplicate_of is None]
    if not originals:
        return QualityCheck(0.0, 0.0, 0.0, 0.0)
    total = len(originals)
    unclassified = sum(doc.folder in {"", "미분류"} for doc in originals) / total
    images = sum(doc.folder == IMAGE_FOLDER for doc in originals) / total
    topical = [doc for doc in originals
               if doc.folder not in {"", "미분류", IMAGE_FOLDER} and doc.folder not in FORMAT_FOLDERS]
    sizes = Counter(doc.folder for doc in topical)
    small = sum(size <= 2 for size in sizes.values()) / len(sizes) if sizes else 0.0
    no_evidence = sum(not _has_evidence(doc) for doc in topical) / len(topical) if topical else 0.0
    warnings = []
    if unclassified > 0.2:
        warnings.append("미분류 많음 (영어·스캔·글자 없는 파일?)")
    if images > 0.3:
        warnings.append("사진 많음 · 한 폴더에 모음")
    if small > 0.4:
        warnings.append("작은 폴더 많음 · 끌어서 합치기")
    if no_evidence > 0.3:
        warnings.append("이름·내용 불일치 많음")
    return QualityCheck(unclassified, images, small, no_evidence, warnings)
