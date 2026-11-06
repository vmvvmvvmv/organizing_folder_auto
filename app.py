"""로컬 문서 분류기 GUI. 실행: python app.py"""

from __future__ import annotations

import queue
import os
from collections.abc import Callable
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import contextlib
import copy
import webbrowser

from quality_score import quality_check
from structure_score import structure_score
from ai_hierarchy import FORMAT_FOLDERS as HIERARCHY_FORMAT_FOLDERS, MIN_TOPICS, add_top_level
from ollama_setup import find_ollama, install_ollama, start_ollama, wait_until_running, winget_available
from local_cache import LocalCache, cached_hierarchy, cached_ollama, clear_cache, default_cache_path
from ai_groups import OLLAMA_DOWNLOAD, assign_ai_folders, ollama_missing_models, ollama_pull
from organizer import Document, default_destination, move_documents, reassign_documents, rename_document, rename_detail, rename_folder, rename_subfolder, route, scan, source_files, undo_jobs, undo_report, valid_folder_name, ResultReport
from reader import IMAGE_TYPES, MAX_FILE_BYTES, SUPPORTED, find_tesseract, ocr_status

CONFIRM_BEFORE_OPEN = {
    ".exe", ".com", ".scr", ".msi", ".msp", ".bat", ".cmd", ".ps1",
    ".vbs", ".js", ".wsf", ".hta", ".jar", ".py", ".lnk", ".url",
}


def move_confirmation_text(documents: list[Document]) -> str:
    return (
        f"{len(documents)}개 파일을 결과 폴더로 이동할까요?\n"
        "이동한 원본과 확인된 중복 원본은 삭제됩니다."
    )



def ai_preview_note(
    documents: list[Document], enabled: bool,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    calls: dict[str, Callable] | None = None,
    source_root: Path | None = None,
) -> str:
    """calls에 저장소를 쓰는 Ollama 함수 묶음(local_cache.cached_ollama)을 주면 같은 질문을 다시 하지 않는다."""
    if not enabled:
        return ""
    count = assign_ai_folders(documents, progress=progress, should_cancel=should_cancel, ai_only=True,
                              source_root=source_root, **(calls or {}))
    return f"AI 분류 {count}개."


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{max(1, round(seconds))}초"
    if seconds < 3600:
        return f"{round(seconds / 60)}분"
    return f"{int(seconds // 3600)}시간 {round(seconds % 3600 / 60)}분"


def progress_text(label: str, started: float, done: int, total: int) -> str:
    """진행 수와 지금까지 속도로 계산한 남은 시간을 보여준다."""
    text = f"{label}: {done}/{total}"
    elapsed = time.monotonic() - started
    if 0 < done < total and elapsed >= 3:
        text += f" · 남은 시간 약 {_duration(elapsed / done * (total - done))}"
    return text


def progress_reporter(events: queue.Queue, label: str) -> Callable[[int, int], None]:
    started = time.monotonic()
    return lambda done, total: events.put(("progress", progress_text(label, started, done, total)))

def group_preview_documents(documents: list[Document], mode: str) -> dict[tuple[str, ...], list[int]]:
    """현재 필터의 파일을 폴더 경로((대분류,), (대분류, 중분류), (대분류, 중분류, 소분류))별로 묶는다."""
    groups: dict[tuple[str, ...], list[int]] = {}
    for index, doc in enumerate(documents):
        visible = (
            mode == "전체"
            or (mode == "미분류" and doc.folder == "미분류")
            or (mode == "읽기 실패" and bool(doc.error))
            or (mode == "중복" and doc.duplicate_of is not None)
            or (mode == "파일명 기준" and doc.source.suffix.lower() not in SUPPORTED)
        )
        if visible:
            path = route(doc) if doc.folder else ("미분류",)
            groups.setdefault(path, []).append(index)
    return groups


def preview_location(source_root: Path, source: Path) -> str:
    """반복되는 전체 경로 대신 원본 폴더 아래 위치만 표시한다."""
    try:
        parent = source.parent.relative_to(source_root)
    except ValueError:
        return str(source.parent)
    return "원본 폴더" if parent == Path(".") else str(parent)


def selected_file_indices(items: tuple[str, ...]) -> list[int]:
    """폴더 행을 파일 편집 대상으로 착각하지 않도록 구분한다."""
    return [int(item[5:]) for item in items if item.startswith("file:") and item[5:].isdigit()]

def drop_folder_row(
    table: ttk.Treeview, folder_rows: dict[str, tuple[str, ...]], item: str,
) -> str | None:
    """폴더 안의 파일 행에 놓아도 가장 가까운 부모 폴더를 대상으로 삼는다."""
    while item:
        if item in folder_rows:
            return item
        item = table.parent(item)
    return None


def in_folder(doc: Document, path: tuple[str, ...]) -> bool:
    """파일이 그 폴더 경로 안(아래 단계 포함)에 들어가는지."""
    return route(doc)[:len(path)] == path


def preview_selection(
    documents: list[Document], folder_rows: dict[str, tuple[str, ...]], item: str,
) -> list[Document]:
    """선택한 파일 또는 제안 폴더에 들어갈 파일을 돌려준다."""
    if item in folder_rows:
        return [doc for doc in documents if in_folder(doc, folder_rows[item])]
    indices = selected_file_indices((item,))
    return [documents[indices[0]]] if indices and indices[0] < len(documents) else []


def render_preview_image(path: Path) -> bytes | None:
    """PDF 첫 페이지나 사진을 작은 PNG로 바꿔 원본 실행 없이 보여준다."""
    if path.suffix.casefold() not in IMAGE_TYPES | {".pdf"} or path.stat().st_size > MAX_FILE_BYTES:
        return None
    import pymupdf

    with pymupdf.open(path) as document:
        if document.needs_pass or not len(document):
            return None
        page = document[0]
        scale = min(760 / page.rect.width, 480 / page.rect.height, 1.0)
        return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")

class OrganizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("문서 OCR 정리")
        self.geometry("1060x650")
        self.minsize(780, 480)
        self.documents: list[Document] = []
        self.folder_rows: dict[str, tuple[str, ...]] = {}
        self.drag_origin: tuple[int, int] | None = None
        self.drag_indices: list[int] = []
        self.drag_active = False
        self.drag_target: str | None = None
        self.drag_hover: str | None = None
        self.drag_preview: tk.Toplevel | None = None
        self.drag_preview_label: tk.Label | None = None
        self.drag_click_item: str | None = None
        self.drag_last: tuple[int, int, int, int] | None = None
        self.drag_scroll_job: str | None = None
        self.preview_paths: tuple[Path, Path] | None = None
        self.last_result_dir: Path | None = None
        self.sort_key = "source"
        self.sort_reverse = False
        self.events: queue.Queue[tuple] = queue.Queue()
        self.busy = False
        self.cancel_event = threading.Event()
        self.close_requested = False
        self.protocol("WM_DELETE_WINDOW", self.request_close)

        self.source_var = tk.StringVar()
        self.destination_var = tk.StringVar()
        self.status_var = tk.StringVar(value=ocr_status(find_tesseract()))
        self.filter_var = tk.StringVar(value="전체")
        self.count_var = tk.StringVar(value="표시 0개")
        self.detail_var = tk.StringVar(value="")
        self.rename_entry: ttk.Entry | None = None
        self.rename_item: str | None = None
        self.context_item: str | None = None
        self.ai_var = tk.BooleanVar(value=False)
        # 대분류로 묶기 전의 (폴더, 하위 폴더, 분류 이유). 묶기를 풀 때 되돌린다.
        self.grouped_snapshot: list[tuple[str, str, str, str]] | None = None
        self.cache_var = tk.BooleanVar(value=True)

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)

        ttk.Label(frame, text="원본 폴더").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.source_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(frame, text="선택", command=self.choose_source).grid(row=0, column=2)

        ttk.Label(frame, text="결과 폴더").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.destination_var).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(frame, text="선택", command=self.choose_destination).grid(row=1, column=2)

        self.ai_checkbox = ttk.Checkbutton(frame, text="로컬 AI 분류 (규칙 검증 포함)", variable=self.ai_var,
                                           command=self.on_ai_toggle)
        self.ai_checkbox.grid(row=2, column=2, sticky="e")
        cache_options = ttk.Frame(frame)
        cache_options.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(cache_options, text="읽은 내용을 이 PC에 저장 (다시 볼 때 빠르게)",
                        variable=self.cache_var).pack(side="left")
        ttk.Button(cache_options, text="저장 내용 지우기", command=self.clear_saved_cache).pack(
            side="left", padx=8)
        self.scan_button = ttk.Button(frame, text="분류 미리보기", command=self.start_scan)
        self.scan_button.grid(row=3, column=0, sticky="w", pady=8)
        self.cancel_button = ttk.Button(frame, text="중단", command=self.cancel_current, state="disabled")
        self.cancel_button.grid(row=3, column=1, sticky="w", pady=8, padx=8)
        result_buttons = ttk.Frame(frame)
        result_buttons.grid(row=3, column=1, sticky="e", pady=8, padx=8)
        self.undo_button = ttk.Button(result_buttons, text="정리 되돌리기…", command=self.start_undo)
        self.undo_button.pack(side="left", padx=(0, 6))
        self.open_button = ttk.Button(result_buttons, text="결과 폴더 열기", command=self.open_result, state="disabled")
        self.open_button.pack(side="left")
        self.move_button = ttk.Button(frame, text="정리 시작", command=self.start_move, state="disabled")
        self.move_button.grid(row=3, column=2, sticky="e", pady=8)

        table_frame = ttk.Frame(frame)
        table_frame.grid(row=4, column=0, columnspan=3, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(1, weight=1)
        filters = ttk.Frame(table_frame)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        ttk.Label(filters, text="표시").pack(side="left")
        filter_box = ttk.Combobox(
            filters, textvariable=self.filter_var, state="readonly", width=12,
            values=("전체", "미분류", "읽기 실패", "중복", "파일명 기준"),
        )
        filter_box.pack(side="left", padx=6)
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_table())
        ttk.Label(filters, textvariable=self.count_var).pack(side="left")
        ttk.Button(filters, text="모두 접기", command=lambda: self.set_all_folders_open(False)).pack(side="right")
        ttk.Button(filters, text="보기", command=self.show_selected_preview).pack(side="right", padx=6)
        self.group_button = ttk.Button(filters, text="대분류로 묶기", command=self.toggle_top_level,
                                       state="disabled")
        self.group_button.pack(side="right", padx=6)
        ttk.Button(filters, text="모두 펼치기", command=lambda: self.set_all_folders_open(True)).pack(
            side="right", padx=6
        )
        self.table = ttk.Treeview(
            table_frame, columns=("location", "method"), show="tree headings", selectmode="extended"
        )
        self.table.heading("#0", text="폴더 / 파일", command=lambda: self.sort_table("category"))
        self.table.heading("location", text="원본 하위 폴더", command=lambda: self.sort_table("source"))
        self.table.heading("method", text="읽기 방식", command=lambda: self.sort_table("method"))
        self.table.column("#0", width=340, minwidth=190, stretch=True)
        self.table.column("location", width=410, minwidth=160, stretch=True)
        self.table.column("method", width=180, minwidth=130, stretch=False)
        self.table.tag_configure("folder", background="#eaf1f8")
        self.table.tag_configure("drop_target", background="#cdebcf")
        self.table.grid(row=1, column=0, sticky="nsew")
        self.table.bind("<<TreeviewSelect>>", self.on_table_selection)
        self.table.bind("<ButtonPress-1>", self.on_drag_press)
        self.table.bind("<B1-Motion>", self.on_drag_motion)
        self.table.bind("<ButtonRelease-1>", self.on_drag_release)
        self.table.bind("<Double-1>", self.on_table_double_click)
        self.table.bind("<Button-3>", self.on_table_right_click)
        self.table.bind("<F2>", lambda _event: self.begin_rename_selected())
        self.table.bind("<Return>", lambda _event: self.show_selected_preview())
        self.table_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table_scrollbar.grid(row=1, column=1, sticky="ns")
        self.table.configure(yscrollcommand=self._on_table_yview)
        ttk.Label(
            table_frame, textvariable=self.detail_var, wraplength=950, justify="left", anchor="w"
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="이름 바꾸기", command=self.rename_context_item)
        self.context_menu.add_command(label="보기 / 열기", command=self.preview_context_item)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="새 주제 폴더로 분류", command=self.create_folder_for_selection)
        self.context_menu.add_command(label="새 하위 폴더로 분류", command=self.create_subfolder_for_selection)
        ttk.Label(frame, textvariable=self.status_var, wraplength=980).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(5, 0)
        )
        # 작업 스레드의 결과는 큐로 받아 화면 스레드에서 처리한다.
        self.after(100, self.poll_events)

    def choose_source(self) -> None:
        folder = filedialog.askdirectory(title="정리할 원본 폴더 선택")
        if folder:
            previous = self.source_var.get().strip()
            current_destination = self.destination_var.get().strip()
            self.source_var.set(folder)
            if not current_destination or (previous and Path(current_destination) == default_destination(Path(previous))):
                self.destination_var.set(str(default_destination(Path(folder))))

    def choose_destination(self) -> None:
        folder = filedialog.askdirectory(title="결과 폴더 선택")
        if folder:
            self.destination_var.set(folder)

    def open_result(self) -> None:
        if self.last_result_dir and self.last_result_dir.is_dir():
            try:
                os.startfile(self.last_result_dir)
            except OSError as exc:
                messagebox.showerror("결과 폴더", str(exc))

    def sort_table(self, key: str) -> None:
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = key
            self.sort_reverse = False
        self.refresh_table()

    def set_all_folders_open(self, opened: bool) -> None:
        for item in self.folder_rows:
            self.table.item(item, open=opened)

    def refresh_table(self) -> None:
        selected = set(self.table.selection())
        selected_paths = {self.folder_rows[item] for item in selected if item in self.folder_rows}
        open_paths = {path for item, path in self.folder_rows.items() if self.table.item(item, "open")}
        self.table.delete(*self.table.get_children())
        self.folder_rows.clear()
        routes = group_preview_documents(self.documents, self.filter_var.get())
        source_root = self.preview_paths[0] if self.preview_paths else Path(self.source_var.get() or ".")
        keys = {
            "category": lambda doc: (doc.folder.casefold(), (doc.output_name or doc.source.name).casefold()),
            "source": lambda doc: ((doc.output_name or doc.source.name).casefold(), str(doc.source).casefold()),
            "method": lambda doc: ((doc.error or doc.method).casefold(), doc.source.name.casefold()),
        }
        # 폴더 경로마다 바로 들어 있는 파일, 바로 아래 폴더, 아래 단계까지 합한 파일 수를 센다.
        children: dict[tuple[str, ...], set[str]] = {}
        totals: dict[tuple[str, ...], int] = {}
        selected_under: set[tuple[str, ...]] = set()
        for path, indices in routes.items():
            has_selected = any(f"file:{index}" in selected for index in indices)
            for depth in range(1, len(path) + 1):
                children.setdefault(path[:depth - 1], set()).add(path[depth - 1])
                totals[path[:depth]] = totals.get(path[:depth], 0) + len(indices)
                if has_selected:
                    selected_under.add(path[:depth])
        restore = []

        def insert_files(parent: str, indices: list[int]) -> None:
            ordered = sorted(
                indices, key=lambda index: keys[self.sort_key](self.documents[index]),
                reverse=self.sort_reverse if self.sort_key != "category" else False,
            )
            for index in ordered:
                doc = self.documents[index]
                file_item = f"file:{index}"
                self.table.insert(
                    parent, "end", iid=file_item, text=doc.output_name or doc.source.name,
                    values=(preview_location(source_root, doc.source),
                            "읽기 실패" if doc.error else doc.method),
                )
                if file_item in selected:
                    restore.append(file_item)

        top_names = children.get((), set())

        def insert_folder(parent: str, path: tuple[str, ...]) -> None:
            item = f"folder:{len(self.folder_rows)}"
            self.folder_rows[item] = path
            opened = (path in open_paths or path in selected_under
                      or (len(path) == 1 and len(top_names) == 1))
            self.table.insert(parent, "end", iid=item, text=f"{path[-1]}  ({totals[path]}개)",
                              values=("", ""), open=opened, tags=("folder",))
            if path in selected_paths:
                restore.append(item)
            insert_files(item, routes.get(path, []))
            for name in sorted(children.get(path, set()), key=str.casefold):
                insert_folder(item, path + (name,))

        for name in sorted(top_names, key=lambda name: (name != "미분류", name.casefold()),
                           reverse=self.sort_reverse if self.sort_key == "category" else False):
            insert_folder("", (name,))
        if restore:
            self.table.selection_set(restore)
        count = sum(len(indices) for indices in routes.values())
        self.count_var.set(f"{len(self.folder_rows)}개 폴더 · {count}/{len(self.documents)}개 파일")
        if not restore:
            self.detail_var.set("")

    def _paths(self) -> tuple[Path, Path] | None:
        source = Path(self.source_var.get().strip())
        destination = Path(self.destination_var.get().strip())
        if not self.source_var.get().strip() or not source.is_dir():
            messagebox.showerror("폴더 확인", "존재하는 원본 폴더를 선택하세요")
            return None
        if not self.destination_var.get().strip():
            messagebox.showerror("폴더 확인", "결과 폴더를 선택하세요")
            return None
        source = source.resolve()
        destination = destination.resolve()
        if source == destination or source.is_relative_to(destination):
            messagebox.showerror("폴더 확인", "결과 폴더는 원본 폴더와 별도로 지정하세요")
            return None
        return source, destination

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.scan_button.configure(state="disabled" if busy else "normal")
        self.ai_checkbox.configure(state="disabled" if busy else "normal")
        self.undo_button.configure(state="disabled" if busy else "normal")
        self.group_button.configure(state="disabled" if busy or not self.documents else "normal")
        self.move_button.configure(state="disabled" if busy or not self.documents or self.preview_paths is None else "normal")
        self.cancel_button.configure(state="normal" if busy and not self.cancel_event.is_set() else "disabled")

    def cancel_current(self) -> None:
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("현재 파일 또는 관련 파일 묶음 처리가 끝나면 중단합니다…")

    def request_close(self) -> None:
        if not self.busy:
            self.destroy()
            return
        if not messagebox.askyesno(
            "작업 중 창 닫기",
            "현재 파일 또는 관련 파일 묶음 처리가 끝나면 작업을 중단하고 창을 닫습니다. 이미 이동한 파일은 결과 폴더에 남고, 결과 기록을 저장합니다. 계속할까요?",
        ):
            return
        self.close_requested = True
        self.cancel_current()

    def on_ai_toggle(self) -> None:
        """AI 분류를 켜면 Ollama와 모델이 준비됐는지 먼저 확인한다."""
        if not self.ai_var.get() or self.busy:
            return
        self.status_var.set("로컬 AI 준비 상태를 확인하는 중입니다…")

        def work() -> None:
            try:
                self.events.put(("ai_setup", ollama_missing_models(), ""))
            except RuntimeError as exc:
                self.events.put(("ai_setup", [], str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _handle_ai_setup(self, missing: list[str], error: str) -> None:
        if error:
            if find_ollama() is not None:
                if messagebox.askyesno(
                    "로컬 AI 준비", "Ollama가 설치돼 있지만 실행 중이 아닙니다. 지금 실행할까요?",
                ):
                    self.start_ollama_setup(install=False)
                    return
            elif winget_available():
                if messagebox.askyesno(
                    "로컬 AI 준비",
                    "로컬 AI 분류에는 무료 프로그램 Ollama가 필요합니다.\n\n"
                    "Windows 패키지 관리자(winget)로 공식 Ollama를 자동 설치할까요? "
                    "(약 1GB, 인터넷 필요. Ollama 사용권에 동의하는 것으로 처리됩니다)",
                ):
                    self.start_ollama_setup(install=True)
                    return
            elif messagebox.askyesno(
                "로컬 AI 준비",
                f"{error}.\n\n로컬 AI 분류에는 무료 프로그램 Ollama가 필요합니다. "
                "설치하고 실행한 뒤 다시 켜세요.\n\n다운로드 페이지를 열까요?",
            ):
                webbrowser.open(OLLAMA_DOWNLOAD)
            self.ai_var.set(False)
            self.status_var.set(f"{error}. Ollama를 설치·실행한 뒤 '로컬 AI 분류'를 다시 켜세요.")
            return
        if not missing:
            self.status_var.set("로컬 AI 사용 가능")
            return
        if messagebox.askyesno(
            "로컬 AI 모델 받기",
            f"필요한 AI 모델이 아직 없습니다: {', '.join(missing)}\n\n"
            "처음 한 번만 받으면 됩니다(모두 약 3GB, 인터넷 필요). 지금 받을까요?",
        ):
            self.start_model_pull(missing)
        else:
            self.ai_var.set(False)
            self.status_var.set("AI 모델이 없어 로컬 AI 분류를 껐습니다")

    def start_ollama_setup(self, install: bool) -> None:
        """Ollama를 설치(필요하면)하고 실행한 뒤, 이어서 모델 확인 단계로 넘어간다."""
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("Ollama를 설치하는 중입니다…" if install else "Ollama를 실행하는 중입니다…")

        def running() -> bool:
            try:
                ollama_missing_models(timeout=2)
                return True
            except RuntimeError:
                return False

        def work() -> None:
            try:
                if install:
                    install_ollama(lambda line: self.events.put(("progress", f"Ollama 설치 중: {line}")),
                                   should_cancel=self.cancel_event.is_set)
                    # 설치 프로그램이 Ollama를 바로 띄우는 경우가 많아 잠시 기다려 본다.
                    if not wait_until_running(running, timeout=15):
                        start_ollama(running)
                else:
                    start_ollama(running)
                self.events.put(("ollama_ready", ""))
            except InterruptedError as exc:
                self.events.put(("ollama_ready", str(exc)))
            except (OSError, RuntimeError) as exc:
                self.events.put(("ollama_ready", str(exc)))

        threading.Thread(target=work, daemon=False).start()

    def start_model_pull(self, models: list[str]) -> None:
        self.cancel_event.clear()
        self._set_busy(True)

        def work() -> None:
            try:
                for position, model in enumerate(models, 1):
                    ollama_pull(
                        model,
                        lambda done, total, position=position, model=model: self.events.put((
                            "progress",
                            f"AI 모델 받는 중 ({position}/{len(models)}) {model}: "
                            f"{done / 1e9:.1f} / {total / 1e9:.1f} GB",
                        )),
                        should_cancel=self.cancel_event.is_set,
                    )
                self.events.put(("pulled", ""))
            except InterruptedError:
                self.events.put(("pulled", "모델 받기를 중단했습니다"))
            except RuntimeError as exc:
                self.events.put(("pulled", str(exc)))

        threading.Thread(target=work, daemon=False).start()

    def clear_saved_cache(self) -> None:
        if self.busy:
            return
        path = default_cache_path()
        if not messagebox.askyesno(
            "저장 내용 지우기",
            f"이 PC에 저장한 파일 해시·읽은 본문·AI 응답을 지울까요?\n{path}\n"
            "원본 파일은 그대로이고, 다음 미리보기만 처음처럼 오래 걸립니다.",
        ):
            return
        try:
            clear_cache(path)
        except OSError as exc:
            messagebox.showerror("저장 내용 지우기", str(exc))
            return
        self.status_var.set("저장한 내용을 지웠습니다")

    def preview_summary(self, counts: bool = True) -> str:
        """미리보기 요약: 개수, 확인 권장 신호, 구조 점수."""
        lines = []
        if counts:
            unknown = sum(doc.folder == "미분류" for doc in self.documents)
            failed = sum(bool(document.error) for document in self.documents)
            duplicates = sum(document.duplicate_of is not None for document in self.documents)
            filename_only = sum(document.source.suffix.lower() not in SUPPORTED for document in self.documents)
            folder_count = len({doc.folder for doc in self.documents if doc.folder and doc.folder != "미분류"})
            lines.append(
                f"{len(self.documents)}개 확인 · 제안 폴더 {folder_count}개 · "
                f"미분류 {unknown}개 · 읽기 실패 {failed}개 · "
                f"동일 파일 {duplicates}개 · 파일명 기준 {filename_only}개."
                + (f" {self.scan_note}" if getattr(self, "scan_note", "") else ""))
        lines.append(quality_check(self.documents).summary())
        lines.append(structure_score(self.documents).summary() + " (기록관리 지침 기준, 찾기 쉬운 구조인지)")
        return "\n".join(lines)

    def toggle_top_level(self) -> None:
        """세부 주제 폴더를 AI가 만든 대분류 아래로 묶거나, 묶기 전으로 되돌린다."""
        if self.busy or not self.documents:
            return
        if self.grouped_snapshot is not None:
            if not messagebox.askyesno(
                "대분류 풀기", "대분류를 풀면 묶은 뒤 바꾼 분류도 묶기 전 상태로 돌아갑니다. 풀까요?",
            ):
                return
            for doc, (folder, subfolder, detail, reason) in zip(self.documents, self.grouped_snapshot):
                doc.folder, doc.subfolder, doc.detail, doc.folder_reason = folder, subfolder, detail, reason
            self.grouped_snapshot = None
            self.group_button.configure(text="대분류로 묶기")
            self.refresh_table()
            self.status_var.set("대분류를 풀었습니다.\n" + self.preview_summary(counts=False))
            return
        topics = {doc.folder for doc in self.documents if doc.duplicate_of is None
                  and doc.folder not in {"", "미분류"} and doc.folder not in HIERARCHY_FORMAT_FOLDERS}
        if len(topics) < MIN_TOPICS:
            messagebox.showinfo(
                "대분류로 묶기",
                f"주제 폴더가 {len(topics)}개라 이미 한눈에 보입니다. {MIN_TOPICS}개 이상일 때 묶습니다.")
            return
        snapshot = [(doc.folder, doc.subfolder, doc.detail, doc.folder_reason) for doc in self.documents]
        documents = copy.deepcopy(self.documents)  # 작업 스레드는 사본만 고친다.
        use_cache = self.cache_var.get()
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("AI로 대분류를 만드는 중입니다…")

        def work() -> None:
            try:
                if ollama_missing_models():
                    raise RuntimeError("AI 모델이 없습니다. '로컬 AI 분류'를 켜서 먼저 준비하세요")
                with contextlib.ExitStack() as stack:
                    cache = stack.enter_context(LocalCache(default_cache_path())) if use_cache else None
                    calls = cached_hierarchy(cache.responses()) if cache else {}
                    plan = add_top_level(documents, **calls)
                self.events.put(("grouped", documents, snapshot, plan, ""))
            except (OSError, RuntimeError, ValueError) as exc:
                self.events.put(("grouped", None, snapshot, {}, f"대분류를 만들지 못했습니다: {exc}"))

        threading.Thread(target=work, daemon=False).start()

    def start_scan(self) -> None:
        if self.busy:
            return
        paths = self._paths()
        if not paths:
            return
        source, destination = paths
        use_ai = self.ai_var.get()
        use_cache = self.cache_var.get()
        self.documents = []
        self.preview_paths = None
        self.refresh_table()
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("문서를 읽는 중입니다…")

        def work() -> None:
            try:
                if use_ai:
                    # 긴 읽기를 마친 뒤에야 모델이 없다고 알리지 않도록 먼저 확인한다.
                    missing = ollama_missing_models()
                    if missing:
                        raise RuntimeError(
                            f"AI 모델이 없습니다: {', '.join(missing)}. "
                            "'로컬 AI 분류'를 껐다 다시 켜면 받을 수 있습니다")
                # 저장소는 이 작업 스레드에서만 열고 닫는다.
                with contextlib.ExitStack() as stack:
                    cache = stack.enter_context(LocalCache(default_cache_path())) if use_cache else None
                    documents = scan(
                        source, destination, find_tesseract(),
                        progress_reporter(self.events, "문서 읽는 중"),
                        should_cancel=self.cancel_event.is_set,
                        classify=not use_ai, cache=cache,
                    )
                    if self.cancel_event.is_set():
                        raise InterruptedError("검사가 중단되었습니다")
                    if use_ai:
                        self.events.put(("progress", "로컬 AI로 문서 임베딩과 폴더명을 계산하는 중입니다…"))
                    ai_note = ai_preview_note(
                        documents, use_ai,
                        progress=progress_reporter(self.events, "로컬 AI 분류 중"),
                        should_cancel=self.cancel_event.is_set,
                        calls=cached_ollama(cache.responses()) if cache else None,
                        source_root=source,
                    )
                if self.cancel_event.is_set():
                    raise InterruptedError("검사가 중단되었습니다")
                projects = source_files(source, destination)[1]
                if projects:
                    names = ", ".join(project.name for project in projects[:3])
                    more = f" 외 {len(projects) - 3}개" if len(projects) > 3 else ""
                    ai_note = (f"코드 프로젝트·압축 푼 문서 폴더 {len(projects)}개({names}{more})는 그대로 둡니다. "
                               + ai_note)
                self.events.put(("scanned", documents, paths, ai_note))
            except InterruptedError:
                self.events.put(("cancelled_scan",))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=False).start()

    def on_drag_press(self, event: tk.Event) -> str | None:
        """마우스를 누른 파일과 기존 다중 선택 파일을 기억한다."""
        self.drag_origin = None
        self.drag_indices = []
        self.drag_active = False
        self.drag_click_item = None
        if self.busy:
            return None
        item = self.table.identify_row(event.y)
        pressed = selected_file_indices((item,))
        if not pressed:
            return None
        selected = self.table.selection()
        self.drag_indices = (
            selected_file_indices(selected) if item in selected else pressed
        )
        self.drag_origin = (event.x, event.y)
        # 여러 파일이 선택된 행을 누르면 Treeview 기본 동작이 선택을 한 행으로 줄인다.
        # 끌기 중에는 선택을 유지하고, 끌지 않고 놓으면 그때 그 행만 선택한다.
        if item in selected and len(self.drag_indices) > 1 and not event.state & 0x0005:
            self.drag_click_item = item
            self.table.focus_set()
            self.table.focus(item)
            return "break"
        return None

    def _show_drag_target(self, item: str | None, hover: str | None = None) -> None:
        hover = hover if hover != item else None
        if (self.drag_target, self.drag_hover) == (item, hover):
            return
        for row in (self.drag_target, self.drag_hover):
            if row and self.table.exists(row):
                self.table.item(row, tags=("folder",) if row in self.folder_rows else ())
        self.drag_target, self.drag_hover = item, hover
        for row in (item, hover):
            if row and self.table.exists(row):
                base = ("folder",) if row in self.folder_rows else ()
                self.table.item(row, tags=base + ("drop_target",))
        self.table.configure(cursor="hand2" if item else "")

    def _move_drag_preview(self, x_root: int, y_root: int, target: str | None) -> None:
        if self.drag_preview is None:
            preview = tk.Toplevel(self)
            preview.withdraw()
            preview.overrideredirect(True)
            preview.attributes("-topmost", True)
            self.drag_preview = preview
            self.drag_preview_label = tk.Label(
                preview, padx=10, pady=6, relief="solid", borderwidth=1,
                font=("Malgun Gothic", 10),
            )
            self.drag_preview_label.pack()
        if len(self.drag_indices) == 1:
            doc = self.documents[self.drag_indices[0]]
            name = doc.output_name or doc.source.name
            label = name if len(name) <= 36 else name[:33] + "..."
        else:
            label = f"{len(self.drag_indices)}개 파일"
        if target:
            label += f"  →  {' / '.join(self.folder_rows[target])}"
        self.drag_preview_label.configure(
            text=label, bg="#d8f2dc" if target else "#e7f1fb",
        )
        self.drag_preview.geometry(f"+{x_root + 18}+{y_root + 18}")
        self.drag_preview.deiconify()

    def _hide_drag_preview(self) -> None:
        if self.drag_preview is not None:
            self.drag_preview.destroy()
            self.drag_preview = None
            self.drag_preview_label = None

    def on_drag_motion(self, event: tk.Event) -> None:
        if not self.drag_origin or not self.drag_indices:
            return
        if not self.drag_active:
            x, y = self.drag_origin
            if abs(event.x - x) + abs(event.y - y) < 6:
                return
            self.drag_active = True
        self.drag_last = (event.x, event.y, event.x_root, event.y_root)
        self._update_drag_target()
        if self.drag_scroll_job is None:
            self._drag_autoscroll()

    def _update_drag_target(self) -> None:
        x, y, x_root, y_root = self.drag_last
        row = self.table.identify_row(y)
        target = drop_folder_row(self.table, self.folder_rows, row)
        self._show_drag_target(target, row if target else None)
        self._move_drag_preview(x_root, y_root, target)

    def _drag_autoscroll(self) -> None:
        """마우스를 목록 위·아래 가장자리에 멈춰 두어도 계속 스크롤한다."""
        self.drag_scroll_job = None
        if not self.drag_active or self.drag_last is None:
            return
        y = self.drag_last[1]
        step = -1 if y < 24 else 1 if y > self.table.winfo_height() - 24 else 0
        if not step:
            return
        self.table.yview_scroll(step, "units")
        self._update_drag_target()
        self.drag_scroll_job = self.after(60, self._drag_autoscroll)

    def _stop_drag_autoscroll(self) -> None:
        if self.drag_scroll_job is not None:
            self.after_cancel(self.drag_scroll_job)
            self.drag_scroll_job = None
        self.drag_last = None

    def on_drag_release(self, event: tk.Event) -> str | None:
        self._stop_drag_autoscroll()
        if not self.drag_active:
            click_item = self.drag_click_item
            self.drag_origin = None
            self.drag_indices = []
            self.drag_click_item = None
            if click_item and self.table.exists(click_item):
                # 끌지 않고 클릭만 했으면 기본 동작처럼 누른 행만 선택한다.
                self.table.selection_set(click_item)
                return "break"
            return None
        row = self.table.identify_row(event.y)
        target_row = drop_folder_row(self.table, self.folder_rows, row)
        target = self.folder_rows.get(target_row)
        indices = self.drag_indices
        self._show_drag_target(None)
        self._hide_drag_preview()
        self.drag_origin = None
        self.drag_indices = []
        self.drag_active = False
        if not target or self.busy:
            self.status_var.set("폴더 안에 놓으세요")
            return "break"
        folder, subfolder, detail = (*target, "", "")[:3]
        changed = reassign_documents(self.documents, indices, folder, subfolder, detail)
        self.refresh_table()
        self.status_var.set(f"{len(changed)}개 파일 → {' / '.join(target)}")
        return "break"

    def on_table_double_click(self, event: tk.Event) -> str | None:
        """내용을 보여줄 수 없는 파일은 Windows 기본 프로그램으로 연다."""
        if self.busy:
            return None
        item = self.table.identify_row(event.y)
        if not item:
            return None
        self.table.selection_set(item)
        if self.table.identify_column(event.x) == "#0":
            if self.table.identify_element(event.x, event.y) == "Treeitem.indicator":
                return None
            self.begin_rename(item)
        else:
            self.show_selected_preview()
        return "break"

    def on_table_right_click(self, event: tk.Event) -> str | None:
        if self.busy:
            return None
        item = self.table.identify_row(event.y)
        if not item:
            return None
        self.context_item = item
        if item not in self.table.selection():
            self.table.selection_set(item)
        files = selected_file_indices(self.table.selection())
        self.context_menu.entryconfigure(3, state="normal" if files else "disabled")
        self.context_menu.entryconfigure(4, state="normal" if files else "disabled")
        self.context_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def rename_context_item(self) -> None:
        if self.context_item:
            self.begin_rename(self.context_item)

    def preview_context_item(self) -> None:
        if self.context_item:
            self.table.selection_set(self.context_item)
            self.show_selected_preview()

    def begin_rename_selected(self) -> str:
        selected = self.table.selection()
        if selected:
            self.begin_rename(selected[0])
        return "break"

    def begin_rename(self, item: str) -> None:
        if self.busy or not self.table.exists(item):
            return
        self.cancel_rename()
        path = self.folder_rows.get(item)
        if path:
            current = path[-1]
        else:
            indices = selected_file_indices((item,))
            if not indices:
                return
            doc = self.documents[indices[0]]
            if doc.duplicate_of is not None or doc.preserve_structure:
                messagebox.showinfo("이름 바꾸기", "이 파일은 이름을 바꿀 수 없습니다")
                return
            current = Path(doc.output_name or doc.source.name).stem
        self.table.see(item)
        self.table.update_idletasks()
        if not self.table.bbox(item, "#0"):
            return
        entry = ttk.Entry(self.table)
        entry.insert(0, current)
        self.rename_entry = entry
        self.rename_item = item
        self._place_rename_entry()
        entry.bind("<Return>", self.finish_rename)
        entry.bind("<Escape>", self.cancel_rename)
        entry.bind("<FocusOut>", self.finish_rename)
        entry.focus_set()
        entry.selection_range(0, "end")

    def _on_table_yview(self, first: str, last: str) -> None:
        self.table_scrollbar.set(first, last)
        self._place_rename_entry()

    def _place_rename_entry(self) -> None:
        """이름 입력칸이 스크롤과 함께 수정 중인 행을 따라가게 한다."""
        entry, item = self.rename_entry, self.rename_item
        if entry is None or not item or not self.table.exists(item):
            return
        box = self.table.bbox(item, "#0")
        if not box:
            # 행이 화면 밖으로 나가면 입력칸도 숨긴다. 다시 보이면 제자리에 나타난다.
            entry.place_forget()
            return
        x, y, width, height = box
        depth = 0
        parent = self.table.parent(item)
        while parent:
            depth += 1
            parent = self.table.parent(parent)
        offset = 20 * (depth + 1)
        entry.place(x=x + offset, y=y, width=max(100, width - offset), height=height)

    def cancel_rename(self, _event: tk.Event | None = None) -> str:
        entry = self.rename_entry
        self.rename_entry = None
        self.rename_item = None
        if entry is not None:
            entry.destroy()
        return "break"

    def finish_rename(self, _event: tk.Event | None = None) -> str:
        entry = self.rename_entry
        if entry is None:
            return "break"
        name = entry.get().strip()
        item = self.rename_item
        self.cancel_rename()
        if not name or not item:
            return "break"
        try:
            path = self.folder_rows.get(item)
            if path:
                # 같은 위치에 이미 있는 폴더 이름으로 바꾸면 두 폴더가 합쳐지므로 먼저 묻는다.
                key, depth = name.casefold(), len(path)
                existing = any(
                    len(route(doc)) >= depth and route(doc)[:depth - 1] == path[:-1]
                    and route(doc)[depth - 1] != path[-1] and route(doc)[depth - 1].casefold() == key
                    for doc in self.documents)
                if existing and not messagebox.askyesno(
                    "폴더 합치기", f"'{name}' 폴더가 이미 있습니다. 두 폴더를 합칠까요?",
                ):
                    return "break"
                if name != path[-1]:
                    if depth == 1:
                        rename_folder(self.documents, path[0], name)
                    elif depth == 2:
                        rename_subfolder(self.documents, path[0], path[1], name)
                    else:
                        rename_detail(self.documents, path[0], path[1], path[2], name)
                new_route = path[:-1] + (name,)
            else:
                indices = selected_file_indices((item,))
                if not indices:
                    return "break"
                doc = self.documents[indices[0]]
                rename_document(doc, name + doc.source.suffix)
                new_route = None
        except ValueError as exc:
            messagebox.showerror("이름 바꾸기", str(exc))
            return "break"
        self.refresh_table()
        if new_route:
            target = next((row for row, route in self.folder_rows.items() if route == new_route), None)
            if target:
                self.table.selection_set(target)
                self.table.see(target)
        self.status_var.set(f"이름 변경: {name}")
        return "break"

    def create_folder_for_selection(self) -> None:
        indices = selected_file_indices(self.table.selection())
        if not indices:
            return
        name = simpledialog.askstring("새 주제 폴더", "폴더 이름", parent=self)
        if name is None:
            return
        try:
            changed = reassign_documents(self.documents, indices, name.strip(), "")
        except ValueError as exc:
            messagebox.showerror("폴더 이름", str(exc))
            return
        self.refresh_table()
        self.status_var.set(f"{len(changed)}개 파일 → {name.strip()}")

    def create_subfolder_for_selection(self) -> None:
        indices = selected_file_indices(self.table.selection())
        if not indices:
            return
        folder = self.documents[indices[0]].folder
        name = simpledialog.askstring("새 하위 폴더", "하위 폴더 이름", parent=self)
        if name is None:
            return
        try:
            changed = reassign_documents(self.documents, indices, folder, name.strip())
        except ValueError as exc:
            messagebox.showerror("폴더 이름", str(exc))
            return
        self.refresh_table()
        self.status_var.set(f"{len(changed)}개 파일 → {folder} / {name.strip()}")

    def on_table_selection(self, _event: tk.Event | None) -> None:
        selected = self.table.selection()
        if not selected:
            return
        item = selected[0]
        if item in self.folder_rows:
            path = self.folder_rows[item]
            count = sum(in_folder(doc, path) for doc in self.documents)
            self.detail_var.set(f"폴더: {' / '.join(path)} · {count}개 파일")
            return
        indices = selected_file_indices((item,))
        if indices:
            doc = self.documents[indices[0]]
            detail = f"파일: {doc.source} · 분류: {doc.folder_reason}"
            if doc.error:
                detail += f" · 읽기 오류: {doc.error}"
            self.detail_var.set(detail)

    def show_selected_preview(self) -> None:
        if self.busy or not self.table.selection():
            messagebox.showinfo("미리보기", "목록에서 파일이나 폴더를 먼저 선택하세요")
            return
        item = self.table.selection()[0]
        selected = preview_selection(self.documents, self.folder_rows, item)
        if not selected:
            messagebox.showinfo("미리보기", "선택한 항목에 파일이 없습니다")
            return
        folder_row = self.folder_rows.get(item)
        image_bytes = None
        preview_error = ""
        if not folder_row:
            document = selected[0]
            try:
                image_bytes = render_preview_image(document.source)
            except (OSError, RuntimeError, ValueError, tk.TclError) as exc:
                preview_error = str(exc)
            if not image_bytes and not document.lead_text.strip():
                if document.source.suffix.casefold() in CONFIRM_BEFORE_OPEN:
                    if not messagebox.askyesno("파일 열기", f"{document.source.name} 파일을 열까요?"):
                        return
                try:
                    os.startfile(document.source)
                except OSError as exc:
                    messagebox.showerror("파일 열기", f"파일을 열지 못했습니다: {exc}")
                return
        window = tk.Toplevel(self)
        window.title("파일·폴더 미리보기")
        window.geometry("880x660")
        window.minsize(560, 380)
        window.transient(self)
        window.bind("<Escape>", lambda _event: window.destroy())
        outer = ttk.Frame(window, padding=12)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        title = (" / ".join(part for part in folder_row if part)
                 if folder_row else selected[0].source.name)
        ttk.Label(header, text=title, font=("Malgun Gothic", 12, "bold")).pack(side="left")
        if not folder_row:
            def open_parent() -> None:
                try:
                    os.startfile(selected[0].source.parent)
                except OSError as exc:
                    messagebox.showerror("원본 폴더", str(exc), parent=window)
            ttk.Button(header, text="원본 위치 열기", command=open_parent).pack(side="right")
        content = ttk.Frame(outer)
        content.pack(fill="both", expand=True)
        body = tk.Text(content, wrap="word", padx=10, pady=10)
        body.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(content, orient="vertical", command=body.yview)
        scrollbar.pack(side="right", fill="y")
        body.configure(yscrollcommand=scrollbar.set)
        if folder_row:
            source_root = self.preview_paths[0] if self.preview_paths else Path(self.source_var.get())
            body.insert("end", f"정리 후 이 폴더에 들어갈 파일 {len(selected)}개\n\n")
            for doc in selected:
                body.insert("end", f"{doc.output_name or doc.source.name}  ·  {preview_location(source_root, doc.source)}\n")
        else:
            doc = selected[0]
            body.insert("end", f"원본: {doc.source}\n정리할 폴더: {doc.folder}")
            if doc.subfolder:
                body.insert("end", f" / {doc.subfolder}" + (f" / {doc.detail}" if doc.detail else ""))
            if doc.output_name:
                body.insert("end", f"\n정리할 이름: {doc.output_name}")
            body.insert("end", f"\n분류 조건: {doc.folder_reason}\n읽기 방식: {doc.method}\n")
            if doc.duplicate_of:
                body.insert("end", f"중복 원본: {doc.duplicate_of}\n")
            if doc.error:
                body.insert("end", f"읽기 오류: {doc.error}\n")
            if preview_error:
                body.insert("end", f"\n화면 미리보기 실패: {preview_error}\n")
            if image_bytes:
                try:
                    window.preview_photo = tk.PhotoImage(data=image_bytes)
                    body.insert("end", "\n")
                    body.image_create("end", image=window.preview_photo)
                    body.insert("end", "\n")
                except tk.TclError as exc:
                    body.insert("end", f"\n화면 미리보기 실패: {exc}\n")
            if doc.lead_text.strip():
                body.insert("end", f"\n추출한 글 일부\n{doc.lead_text.strip()}\n")
            elif not doc.error:
                body.insert("end", "\n내용 미리보기가 없는 파일입니다. 원본 위치에서 확인하세요.\n")
        body.configure(state="disabled")

    def start_undo(self) -> None:
        """정리 결과 기록(CSV)을 골라 그 정리를 되돌린다."""
        if self.busy:
            return
        initial = self.last_result_dir or (
            Path(self.destination_var.get().strip()) if self.destination_var.get().strip() else None)
        path = filedialog.askopenfilename(
            title="되돌릴 정리 결과 기록(CSV) 선택",
            initialdir=str(initial) if initial and initial.is_dir() else None,
            filetypes=[("정리 결과 기록", "정리_결과_*.csv"), ("CSV 파일", "*.csv")],
        )
        if not path:
            return
        report = Path(path)
        try:
            jobs = undo_jobs(report)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("정리 되돌리기", f"기록을 읽을 수 없습니다: {exc}")
            return
        if not jobs:
            messagebox.showinfo("정리 되돌리기", "이 기록에는 되돌릴 파일이 없습니다")
            return
        if not messagebox.askyesno(
            "정리 되돌리기",
            f"{len(jobs)}개 파일을 정리 전 위치로 되돌릴까요?\n"
            "원래 위치에 같은 이름의 파일이 새로 생겼으면 그 파일은 건너뜁니다.",
        ):
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("파일을 원래 위치로 되돌리는 중입니다…")

        def work() -> None:
            try:
                restored, errors = undo_report(
                    report,
                    progress_reporter(self.events, "되돌리는 중"),
                    should_cancel=self.cancel_event.is_set,
                )
                self.events.put(("undone", restored, errors, len(jobs)))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=False).start()

    def start_move(self) -> None:
        if self.busy or not self.documents:
            return
        paths = self._paths()
        if not paths:
            return
        source, destination = paths

        # 미리보기와 다른 폴더를 정리하면 화면에 보인 분류가 틀어질 수 있다.
        if self.preview_paths != paths:
            messagebox.showerror("미리보기 확인", "원본 또는 결과 폴더가 바뀌었습니다. 미리보기를 다시 실행하세요")
            return
        if not messagebox.askyesno("이동 확인", move_confirmation_text(self.documents)):
            return
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("폴더 오류", str(exc))
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("파일을 검증하며 이동 중입니다…")

        def work() -> None:
            try:
                # 기록 파일을 만들 수 없으면 아무것도 옮기지 않는다.
                with ResultReport(source, destination, self.documents) as report:
                    moved, deduplicated, errors = move_documents(
                        self.documents, destination,
                        progress_reporter(self.events, "이동 중"),
                        should_cancel=self.cancel_event.is_set,
                        source_root=source,
                        record=report.record,
                    )
                self.events.put(("moved", moved, deduplicated, errors, destination, report.path))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=False).start()

    def poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    if not self.cancel_event.is_set():
                        self.status_var.set(event[1])
                elif kind == "scanned":
                    self.documents = event[1]
                    self.preview_paths = event[2]
                    self.grouped_snapshot = None
                    self.group_button.configure(text="대분류로 묶기")
                    self.refresh_table()
                    self.scan_note = event[3]
                    self.status_var.set(self.preview_summary())
                    self._set_busy(False)
                elif kind == "grouped":
                    grouped, snapshot, plan, error = event[1:]
                    self._set_busy(False)
                    if error:
                        self.status_var.set(error)
                        if not self.close_requested:
                            messagebox.showerror("대분류로 묶기", error)
                    elif not plan:
                        self.status_var.set("묶을 만한 대분류를 찾지 못해 그대로 두었습니다")
                        if not self.close_requested:
                            messagebox.showinfo("대분류로 묶기", "묶을 만한 대분류를 찾지 못해 그대로 두었습니다.")
                    else:
                        self.documents = grouped
                        self.grouped_snapshot = snapshot
                        self.group_button.configure(text="대분류 풀기")
                        self.refresh_table()
                        self.status_var.set(
                            f"세부 주제 {len(plan)}개를 대분류 {len(set(plan.values()))}개로 묶었습니다. "
                            "마음에 들지 않으면 끌어서 옮기거나 '대분류 풀기'를 누르세요.\n"
                            + self.preview_summary(counts=False))
                elif kind == "cancelled_scan":
                    self.preview_paths = None
                    self.status_var.set("검사가 중단되었습니다. 원본 파일은 그대로 있습니다.")
                    self._set_busy(False)
                elif kind == "moved":
                    moved, deduplicated, errors, destination, report = event[1:]
                    unprocessed = len(self.documents) - len(moved) - len(deduplicated) - len(errors)
                    self.preview_paths = None
                    self.last_result_dir = destination
                    self.open_button.configure(state="normal")
                    self.status_var.set(
                        f"{len(moved)}개 이동 · 중복 원본 {len(deduplicated)}개 정리 · 실패 {len(errors)}개 · 미처리 {unprocessed}개 · {destination}"
                        + f" · 기록 {report.name}"
                    )
                    self._set_busy(False)
                    if self.close_requested:
                        continue
                    if errors or unprocessed:
                        detail = "\n".join(f"{doc.source.name}: {error}" for doc, error in errors[:8])
                        messagebox.showwarning("정리 결과", f"이동 실패 {len(errors)}개, 미처리 {unprocessed}개. 남은 원본을 확인하고 미리보기를 다시 실행하세요.\n{detail}")
                    else:
                        messagebox.showinfo(
                            "정리 결과", f"{len(moved)}개 이동, 중복 원본 {len(deduplicated)}개 정리.\n{destination}"
                        )
                elif kind == "ai_setup":
                    self._handle_ai_setup(event[1], event[2])
                elif kind == "ollama_ready":
                    self._set_busy(False)
                    if event[1]:
                        self.ai_var.set(False)
                        self.status_var.set(event[1])
                        if not self.close_requested:
                            messagebox.showerror("로컬 AI 준비", event[1])
                    else:
                        # 실행되면 바로 모델 확인으로 이어서, 없으면 받기를 묻는다.
                        self.status_var.set("Ollama가 실행되었습니다. AI 모델을 확인하는 중입니다…")
                        self.on_ai_toggle()
                elif kind == "pulled":
                    self._set_busy(False)
                    if event[1]:
                        self.ai_var.set(False)
                        self.status_var.set(event[1])
                        if not self.close_requested:
                            messagebox.showerror("AI 모델 받기", event[1])
                    else:
                        self.status_var.set("AI 모델을 받았습니다. 로컬 AI 사용 가능")
                elif kind == "undone":
                    restored, errors, total = event[1:]
                    # 파일 위치가 바뀌었으므로 예전 미리보기로는 정리할 수 없다.
                    self.documents = []
                    self.preview_paths = None
                    self.refresh_table()
                    skipped = total - len(restored) - len(errors)
                    self.status_var.set(
                        f"되돌리기: {len(restored)}개 복원 · 실패 {len(errors)}개"
                        + (f" · 중단으로 미처리 {skipped}개" if skipped else "")
                    )
                    self._set_busy(False)
                    if self.close_requested:
                        continue
                    if errors or skipped:
                        detail = "\n".join(f"{path.name}: {error}" for path, error in errors[:8])
                        messagebox.showwarning(
                            "되돌리기 결과",
                            f"{len(restored)}개를 원래 위치로 되돌렸습니다. 실패 {len(errors)}개, 미처리 {skipped}개.\n{detail}",
                        )
                    else:
                        messagebox.showinfo("되돌리기 결과", f"{len(restored)}개를 모두 원래 위치로 되돌렸습니다.")
                elif kind == "error":
                    self.status_var.set(event[1])
                    self.preview_paths = None
                    self._set_busy(False)
                    if not self.close_requested:
                        messagebox.showerror("작업 오류", event[1])
        except queue.Empty:
            pass
        if self.close_requested and not self.busy:
            self.destroy()
            return
        self.after(100, self.poll_events)


if __name__ == "__main__":
    OrganizerApp().mainloop()
