"""진행 표시 문구: 진행 수, 남은 시간, AI 분류 전체 예상 시간."""

from __future__ import annotations

import queue
import time

from local_cache import LocalCache


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{max(1, round(seconds))}초"
    if seconds < 3600:
        return f"{round(seconds / 60)}분"
    return f"{int(seconds // 3600)}시간 {round(seconds % 3600 / 60)}분"


def progress_text(label: str, started: float, done: int, total: int) -> str:
    """진행 수와 지금까지 속도로 계산한 남은 시간을 보여준다."""
    text = f"{label} {done}/{total}"
    elapsed = time.monotonic() - started
    if 0 < done < total and elapsed >= 3:
        text += f" · 남은 {_duration(elapsed / done * (total - done))}"
    return text


# 문서 한 개의 AI 분류 시간(초). 이 PC에서 잰 값이 저장돼 있으면 그 값을 쓴다.
DEFAULT_AI_SECONDS = 3.0


def whole_text(whole: float, elapsed: float) -> str:
    """AI 분류 전체의 예상 시간과 지금까지 걸린 시간."""
    if elapsed > whole:
        return f" · 예상 {_duration(whole)} 초과 · 경과 {_duration(elapsed)}"
    return f" · 전체 {_duration(whole)} 중 {_duration(elapsed)} 경과"


def ai_seconds_per_document(cache: LocalCache | None) -> float:
    try:
        return float(cache.get("stats", "ai_seconds_per_document")) if cache else DEFAULT_AI_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_AI_SECONDS


def remember_ai_speed(cache: LocalCache | None, seconds: float, count: int) -> None:
    """실제로 AI에게 물은 실행만 기억한다. 저장된 답으로 금방 끝난 실행은 다음 예상을 틀리게 만든다."""
    if cache and count and seconds / count >= 0.3:
        known = cache.get("stats", "ai_seconds_per_document")
        measured = seconds / count
        cache.put("stats", "ai_seconds_per_document",
                  str(round((float(known) + measured) / 2 if known else measured, 2)))


class progress_reporter:
    """진행 수와 남은 시간을 화면에 알린다. stage()로 단계가 바뀌면 이름과 시간 계산을 새로 시작한다."""

    def __init__(self, events: queue.Queue, label: str, whole: float | None = None) -> None:
        """whole: 이 작업 전체의 예상 시간(초). 주면 진행 표시 뒤에 전체 예상과 지난 시간을 붙인다."""
        self.events, self.label, self.started = events, label, time.monotonic()
        self.whole, self.began = whole, self.started

    def overall(self) -> str:
        if not self.whole:
            return ""
        return whole_text(self.whole, time.monotonic() - self.began)

    def stage(self, label: str) -> None:
        self.label, self.started = label, time.monotonic()
        self.events.put(("progress", f"{label}…{self.overall()}"))

    def __call__(self, done: int, total: int) -> None:
        self.events.put(("progress", progress_text(self.label, self.started, done, total) + self.overall()))


def size_text(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
