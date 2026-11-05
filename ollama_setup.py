"""로컬 AI(Ollama) 설치와 실행을 앱 안에서 처리한다.

- 설치돼 있지만 꺼져 있으면 실행한다.
- 설치돼 있지 않으면 Windows 패키지 관리자(winget)로 공식 패키지 Ollama.Ollama를 설치한다.
  winget은 패키지 해시를 확인하고 설치하므로, 앱이 직접 설치 파일을 받아 실행하지 않는다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

WINGET_ID = "Ollama.Ollama"
NO_CONSOLE = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
DETACHED = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0


def find_ollama() -> Path | None:
    """설치된 Ollama 실행 파일(트레이 앱 우선)."""
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
    for candidate in (local / "ollama app.exe", local / "ollama.exe"):
        if candidate.is_file():
            return candidate
    found = shutil.which("ollama")
    return Path(found) if found else None


def winget_available() -> bool:
    return shutil.which("winget") is not None


def wait_until_running(is_running: Callable[[], bool], timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return True
        time.sleep(1)
    return is_running()


def start_ollama(is_running: Callable[[], bool]) -> None:
    """설치된 Ollama를 실행하고 응답할 때까지 기다린다."""
    executable = find_ollama()
    if executable is None:
        raise RuntimeError("설치된 Ollama를 찾지 못했습니다")
    args = [str(executable)] if executable.name == "ollama app.exe" else [str(executable), "serve"]
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=NO_CONSOLE | DETACHED, close_fds=True)
    if not wait_until_running(is_running):
        raise RuntimeError("Ollama를 실행했지만 응답이 없습니다. 잠시 뒤 다시 시도하세요")


def install_ollama(
    report: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """winget으로 Ollama를 설치한다. 진행 메시지를 한 줄씩 report로 알린다."""
    if not winget_available():
        raise RuntimeError("이 PC에는 winget이 없어 자동 설치를 할 수 없습니다")
    process = subprocess.Popen(
        ["winget", "install", "--exact", "--id", WINGET_ID, "--silent",
         "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=NO_CONSOLE,
    )
    tail: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        if should_cancel and should_cancel():
            process.kill()
            raise InterruptedError("Ollama 설치를 중단했습니다")
        # winget은 진행 막대를 같은 줄에 덮어써서 \r로 나뉜 마지막 조각만 의미가 있다.
        text = line.replace("\r", "\n").strip().splitlines()[-1].strip() if line.strip() else ""
        if text and any(ch.isalnum() for ch in text):
            tail = (tail + [text])[-5:]
            if report:
                report(text[:120])
    if process.wait() != 0:
        raise RuntimeError("Ollama 자동 설치에 실패했습니다: " + " / ".join(tail[-2:]))
