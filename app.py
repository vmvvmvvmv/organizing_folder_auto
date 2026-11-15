"""로컬 문서 분류기 GUI. 실행: python app.py"""

from __future__ import annotations

import queue
import os
from collections.abc import Callable
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import contextlib
import ai_groups
import organizer
import copy
import webbrowser

from quality_score import quality_check
from structure_score import structure_score
from ai_hierarchy import (FORMAT_FOLDERS as HIERARCHY_FORMAT_FOLDERS, MIN_TOPICS, add_top_level,
                          fit_user_folders, folder_counts, ollama_user_pick)
from ollama_setup import find_ollama, install_ollama, start_ollama, wait_until_running, winget_available
from folder_editor import FolderEditor
from local_cache import LocalCache, cached_hierarchy, cached_ollama, clear_cache, default_cache_path
from ai_groups import OLLAMA_DOWNLOAD, assign_ai_folders, ollama_missing_models, ollama_pull, ollama_unload
from organizer import load_preview, save_preview, space_needed
from organizer import Document, default_destination, move_documents, reassign_documents, rename_document, rename_detail, rename_folder, rename_subfolder, route, scan, source_files, undo_jobs, undo_report, ResultReport
from reader import SUPPORTED, find_tesseract, ocr_status
from progress import _duration, ai_seconds_per_document, progress_reporter, remember_ai_speed, size_text
from preview_model import (FILTERS, FOLDER_LEVEL_STYLES, drop_folder_row, folder_tag, group_preview_documents,
                           in_folder, preview_location, preview_selection, row_tags, selected_file_indices)
from settings import (FOLDERS_LABEL, last_preview_path, load_excludes, load_user_folders, save_excludes, save_user_folders,
                      user_folders_label)

CONFIRM_BEFORE_OPEN = {
    ".exe", ".com", ".scr", ".msi", ".msp", ".bat", ".cmd", ".ps1",
    ".vbs", ".js", ".wsf", ".hta", ".jar", ".py", ".lnk", ".url",
}


def move_confirmation_text(documents: list[Document], copy: bool = False) -> str:
    if copy:
        return f"파일 {len(documents)}개를 결과 폴더로 복사하시겠습니까?\n원본 파일은 그대로 남습니다."
    return (
        f"파일 {len(documents)}개를 결과 폴더로 옮기시겠습니까?\n"
        "옮긴 파일과 내용이 같은 중복 파일은 원래 자리에서 지워집니다."
    )


def ai_preview_note(
    documents: list[Document], enabled: bool,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    calls: dict[str, Callable] | None = None,
    source_root: Path | None = None,
    stage: Callable[[str], None] | None = None,
) -> str:
    """calls에 저장소를 쓰는 Ollama 함수 묶음(local_cache.cached_ollama)을 주면 같은 질문을 다시 하지 않는다."""
    if not enabled:
        return ""
    count = assign_ai_folders(documents, progress=progress, should_cancel=should_cancel, ai_only=True,
                              source_root=source_root, stage=stage, **(calls or {}))
    return f"AI 분류 {count}개"


UNDO_LIMIT = 50


APP_VERSION = "2026.11"
HELP_URL = "https://github.com/vmvvmvvmv/organizing_folder_auto#사용-방법"
class OrganizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("문서 OCR 정리")
        # 화면 배율(125%·150% 등)에 맞춰 크기를 키운다. 배율 인식을 켜지 않은 실행에서는 1.0이다.
        scale = self.winfo_fpixels("1i") / 96
        self.px = px = lambda size: round(size * scale)
        self.geometry(f"{px(1060)}x{px(650)}")
        self.minsize(px(780), px(480))
        # 행 높이를 글자 높이에 맞춘다(기본 20픽셀은 큰 배율에서 글자가 겹친다).
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        ttk.Style(self).configure("Treeview", rowheight=max(20, line + px(5)))
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
        self.search_var = tk.StringVar(value="")
        self.rename_entry: ttk.Entry | None = None
        self.rename_item: str | None = None
        self.context_item: str | None = None
        self.ai_var = tk.BooleanVar(value=False)
        # 대분류로 묶기 전의 (폴더, 하위 폴더, 분류 이유). 묶기를 풀 때 되돌린다.
        self.grouped_snapshot: list[tuple[str, str, str, str]] | None = None
        self.cache_var = tk.BooleanVar(value=True)
        self.gentle_var = tk.BooleanVar(value=False)
        # 복사 모드: 원본을 지우지 않는다. 옮기는 것이 불안한 사람을 위해 정리 시작 옆에 둔다.
        self.copy_var = tk.BooleanVar(value=False)
        # 폴더명 지정 창의 체크박스. 창마다 만들면 작업 스레드에서 정리되며 Tcl이 죽는다.
        self.allow_new_var = tk.BooleanVar(value=False)
        # 사용자가 미리 정한 폴더명. AI가 이 목록에 맞춰 넣는다. 다음 실행을 위해 이 PC에 저장한다.
        self.user_folders, self.allow_new_folders = load_user_folders()
        organizer.EXCLUDE[:] = load_excludes()

        self._build_menu()
        style = ttk.Style(self)
        # 굵은 글씨도 기본 글꼴에서 만든다("TkDefaultFont"를 글꼴 이름으로 쓰면 다른 글꼴로 바뀐다).
        # Tcl에 바로 만든다. tkfont.Font 객체는 정리될 때 글꼴을 지우고, 작업 스레드에서 정리되면 Tcl이 죽는다.
        self.tk.call("font", "create", "AppBold", *self.tk.call("font", "actual", "TkDefaultFont"))
        self.tk.call("font", "configure", "AppBold", "-weight", "bold")
        self.option_add("*Text.font", "TkDefaultFont")
        style.configure("Status.TFrame", relief="sunken")

        # 아래 상태 표시줄을 먼저 붙여야 창을 줄여도 가려지지 않는다.
        status_bar = ttk.Frame(self, style="Status.TFrame", padding=(px(8), px(3)))
        status_bar.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate", length=px(140))
        self.detail_button = ttk.Button(status_bar, text="자세히", width=6, state="disabled",
                                        command=self.show_summary_detail)
        self.detail_button.pack(side="right")
        ttk.Label(status_bar, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)

        frame = ttk.Frame(self, padding=(px(10), px(8), px(10), px(6)))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        paths = ttk.LabelFrame(frame, text="폴더", padding=(px(8), px(4)))
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        for row, (label, variable, command) in enumerate((
                ("원본", self.source_var, self.choose_source),
                ("결과", self.destination_var, self.choose_destination))):
            ttk.Label(paths, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(paths, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=px(6), pady=2)
            ttk.Button(paths, text="찾아보기…", command=command).grid(row=row, column=2, pady=2)

        toolbar = ttk.Frame(frame, padding=(0, px(6)))
        toolbar.grid(row=1, column=0, sticky="ew")
        self.scan_button = ttk.Button(toolbar, text="1. 미리보기", command=self.start_scan)
        self.scan_button.pack(side="left")
        # 읽은 본문·OCR은 저장된 것을 쓰고, 로컬 AI 분류만 처음부터 다시 받는다.
        self.fresh_button = ttk.Button(toolbar, text="AI에게 새로 묻기", command=lambda: self.start_scan(fresh_ai=True))
        self.fresh_button.pack(side="left", padx=(px(4), 0))
        self.folders_button = ttk.Button(toolbar, text=user_folders_label(self.user_folders),
                                         command=self.edit_user_folders)
        self.folders_button.pack(side="left", padx=(px(4), 0))
        self.cancel_button = ttk.Button(toolbar, text="중단", command=self.cancel_current, state="disabled")
        self.cancel_button.pack(side="left", padx=(px(12), 0))
        self.move_button = ttk.Button(toolbar, text="3. 파일 정리 실행",
                                      command=self.start_move, state="disabled")
        self.move_button.pack(side="right")
        self.open_button = ttk.Button(toolbar, text="결과 폴더 열기", command=self.open_result, state="disabled")
        self.open_button.pack(side="right", padx=(0, px(4)))
        # 정리를 되돌리는 안전장치는 메뉴에 숨기지 않는다.
        self.undo_button = ttk.Button(toolbar, text="정리 되돌리기…", command=self.start_undo)
        self.undo_button.pack(side="right", padx=(0, px(4)))
        ttk.Checkbutton(toolbar, text="원본 유지 (복사)", variable=self.copy_var).pack(side="right", padx=(0, px(6)))
        ttk.Separator(toolbar, orient="vertical").pack(side="right", fill="y", padx=px(8))
        self.ai_checkbox = ttk.Checkbutton(toolbar, text="AI 사용", variable=self.ai_var, command=self.on_ai_toggle)
        self.ai_checkbox.pack(side="right")

        table_frame = ttk.Frame(frame)
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(1, weight=1)
        filters = ttk.Frame(table_frame)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, px(4)))
        ttk.Label(filters, text="표시").pack(side="left")
        filter_box = ttk.Combobox(
            filters, textvariable=self.filter_var, state="readonly", width=12, values=FILTERS,
        )
        filter_box.pack(side="left", padx=px(6))
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_table())
        ttk.Label(filters, text="검색").pack(side="left", padx=(px(10), 0))
        self.search_entry = ttk.Entry(filters, textvariable=self.search_var, width=22)
        self.search_entry.pack(side="left", padx=px(6))
        self.search_entry.bind("<KeyRelease>", lambda _event: self._schedule_search())
        self.search_entry.bind("<Escape>", lambda _event: (self.search_var.set(""), self.refresh_table(), "break")[-1])
        ttk.Label(filters, textvariable=self.count_var).pack(side="left")
        ttk.Button(filters, text="모두 접기", command=lambda: self.set_all_folders_open(False)).pack(side="right")
        ttk.Button(filters, text="모두 펼치기", command=lambda: self.set_all_folders_open(True)).pack(
            side="right", padx=px(4))
        ttk.Button(filters, text="파일 열기", command=self.show_selected_preview).pack(side="right", padx=px(4))
        self.group_button = ttk.Button(filters, text="대분류로 묶기", command=self.toggle_top_level,
                                       state="disabled")
        # '대분류로 묶기'는 폴더명 지정 창의 'AI 묶기 제안'과 도구 메뉴로 옮겼다. 상태 관리용으로 위젯만 둔다.
        self.table = ttk.Treeview(
            table_frame, columns=("location", "method"), show="tree headings", selectmode="extended"
        )
        self.table.heading("#0", text="폴더 / 파일", command=lambda: self.sort_table("category"))
        self.table.heading("location", text="원래 위치", command=lambda: self.sort_table("source"))
        self.table.heading("method", text="상태", command=lambda: self.sort_table("method"))
        self.table.column("#0", width=px(340), minwidth=px(190), stretch=True)
        self.table.column("location", width=px(410), minwidth=px(160), stretch=True)
        self.table.column("method", width=px(180), minwidth=px(130), stretch=False)
        # 폴더 단계마다 색을 달리한다: 대분류(진한 파랑) → 중분류(연한 초록) → 소분류(연한 노랑).
        for level, (background, font) in enumerate(FOLDER_LEVEL_STYLES, 1):
            self.table.tag_configure(f"folder{level}", background=background, **({"font": font} if font else {}))
        self.table.tag_configure("drop_target", background="#cdebcf")
        self.table.grid(row=1, column=0, sticky="nsew")
        self.table.bind("<<TreeviewSelect>>", self.on_table_selection)
        self.table.bind("<ButtonPress-1>", self.on_drag_press)
        self.table.bind("<B1-Motion>", self.on_drag_motion)
        self.table.bind("<ButtonRelease-1>", self.on_drag_release)
        self.table.bind("<Double-1>", self.on_table_double_click)
        self.table.bind("<Button-3>", self.on_table_right_click)
        self.table.bind("<F2>", lambda _event: self.begin_rename_selected())
        # 입력 칸(경로·검색·이름 바꾸기)에서는 Ctrl+Z가 글자 되돌리기여야 하므로 목록에만 건다.
        self.table.bind("<Control-z>", lambda _event: self.undo_edit())
        self.table.bind("<Return>", lambda _event: self.show_selected_preview())
        self.empty_hint = ttk.Label(
            table_frame, justify="center", foreground="#777", background="white",
            text="먼저 위 '원본' 칸에 정리할 폴더를 고르세요 (Ctrl+O)\n\n"
                 "1. 미리보기 (F5) — 파일은 아직 움직이지 않습니다\n\n"
                 "2. 폴더 구조 정하기 — 원하는 폴더 이름이 있을 때만\n\n"
                 "3. 파일 정리 실행 — 목록을 확인·수정한 뒤")
        self.table_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table_scrollbar.grid(row=1, column=1, sticky="ns")
        self.table.configure(yscrollcommand=self._on_table_yview)
        ttk.Label(
            table_frame, textvariable=self.detail_var, wraplength=px(950), justify="left", anchor="w",
            foreground="#555",
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(4), 0))
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="이름 바꾸기", accelerator="F2", command=self.rename_context_item)
        self.context_menu.add_command(label="보기 / 열기", accelerator="Enter", command=self.preview_context_item)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="새 주제 폴더로 분류", command=self.create_folder_for_selection)
        self.context_menu.add_command(label="새 하위 폴더로 분류", command=self.create_subfolder_for_selection)
        # 작업 스레드의 결과는 큐로 받아 화면 스레드에서 처리한다.
        self.after(100, self.poll_events)

    def _build_menu(self) -> None:
        """파일·편집·보기·도구·도움말 메뉴와 단축키."""
        bar = tk.Menu(self)
        self.configure(menu=bar)
        self.menus: dict[str, tk.Menu] = {}

        def menu(name: str, entries: list) -> None:
            item = tk.Menu(bar, tearoff=0)
            for entry in entries:
                if entry is None:
                    item.add_separator()
                elif entry[0] == "check":
                    item.add_checkbutton(label=entry[1], variable=entry[2], command=entry[3] if len(entry) > 3 else None)
                else:
                    label, command, key = (*entry, "")[:3]
                    item.add_command(label=label, command=command, accelerator=key)
            bar.add_cascade(label=name, menu=item)
            self.menus[name] = item

        menu("파일", [("원본 폴더 선택…", self.choose_source, "Ctrl+O"), ("결과 폴더 선택…", self.choose_destination),
                     None, ("지난 미리보기 열기", lambda: self.open_preview(last_preview_path())),
                     None, ("결과 폴더 열기", self.open_result), ("정리 되돌리기…", self.start_undo),
                     None, ("끝내기", self.request_close, "Alt+F4")])
        menu("편집", [("이름 바꾸기", self.begin_rename_selected, "F2"), ("파일 열기", self.show_selected_preview, "Enter"),
                     ("검색", self.focus_search, "Ctrl+F"), ("수정 되돌리기", self.undo_edit, "Ctrl+Z"),
                     None, ("새 폴더로 옮기기", self.create_folder_for_selection),
                     ("새 하위 폴더로 옮기기", self.create_subfolder_for_selection),
                     None, (FOLDERS_LABEL + "…", self.edit_user_folders, "Ctrl+L")])
        menu("보기", [("모두 펼치기", lambda: self.set_all_folders_open(True)),
                     ("모두 접기", lambda: self.set_all_folders_open(False)), None])
        for name in FILTERS:
            self.menus["보기"].add_radiobutton(label=f"표시: {name}", variable=self.filter_var, value=name,
                                              command=self.refresh_table)
        menu("도구", [("1. 미리보기", self.start_scan, "F5"),
                     ("AI에게 새로 묻기", lambda: self.start_scan(fresh_ai=True), "Shift+F5"),
                     ("3. 파일 정리 실행", self.start_move), ("중단", self.cancel_current, "Esc"),
                     None, ("check", "AI 사용", self.ai_var, self.on_ai_toggle),
                     ("check", "원본 유지 (복사)", self.copy_var),
                     None, ("설정…", self.open_settings)])
        menu("도움말", [("사용 방법", lambda: webbrowser.open(HELP_URL)), None, ("정보", self.show_about)])

        for key, action in (("<Control-o>", self.choose_source), ("<F5>", self.start_scan),
                            ("<Shift-F5>", lambda: self.start_scan(fresh_ai=True)),
                            ("<Control-l>", self.edit_user_folders), ("<Escape>", self.cancel_current),
                            ("<Control-f>", self.focus_search)):
            self.bind(key, lambda _event, action=action: action())

    def _schedule_search(self) -> None:
        """글자를 칠 때마다 목록을 다시 그리면 느리므로 잠깐 멈췄을 때 한 번만 그린다."""
        if getattr(self, "_search_job", None):
            self.after_cancel(self._search_job)
        self._search_job = self.after(250, self._run_search)

    def _run_search(self) -> None:
        self._search_job = None
        self.refresh_table()
        if self.search_var.get().strip():
            self.set_all_folders_open(True)

    def focus_search(self) -> None:
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def _menu_state(self, menu: str, label: str, enabled: bool) -> None:
        self.menus[menu].entryconfigure(label, state="normal" if enabled else "disabled")

    def _preview_extra(self) -> dict:
        return {"ai_snapshot": getattr(self, "ai_snapshot", None), "scan_note": getattr(self, "scan_note", ""),
                "grouped_snapshot": self.grouped_snapshot}

    def autosave_preview(self) -> None:
        """미리보기가 있으면 이 PC에 자동 저장한다(파일 ▸ 지난 미리보기 열기). 실패해도 넘어간다."""
        if self.documents and self.preview_paths:
            try:
                save_preview(last_preview_path(), *self.preview_paths, self.documents, self._preview_extra())
            except (OSError, ValueError):
                pass

    def open_preview(self, path: Path) -> None:
        if self.busy:
            return
        if not path.is_file():
            messagebox.showinfo("미리보기 불러오기", "저장된 미리보기가 없습니다.")
            return
        try:
            source, destination, documents, extra = load_preview(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("미리보기 불러오기", str(exc))
            return
        if not source.is_dir():
            messagebox.showerror("미리보기 불러오기", f"원본 폴더를 찾을 수 없습니다.\n{source}")
            return
        self.source_var.set(str(source))
        self.destination_var.set(str(destination))
        self.documents = documents
        self.preview_paths = (source, destination)
        snapshot = extra.get("ai_snapshot")
        self.ai_snapshot = [tuple(row) for row in snapshot] if snapshot else None
        grouped = extra.get("grouped_snapshot")
        self.grouped_snapshot = [tuple(row) for row in grouped] if grouped else None
        self.group_button.configure(text="대분류 풀기" if self.grouped_snapshot else "대분류로 묶기")
        self.scan_note = extra.get("scan_note", "")
        self.remember_routes()
        self.refresh_table()
        self._set_busy(False)
        self.status_var.set("미리보기 불러옴 · 바뀐 파일은 정리할 때 걸러짐 · " + self.preview_summary())

    def open_settings(self) -> None:
        """자주 바꾸지 않는 설정을 한 창에 모은다. 체크박스는 앱이 들고 있는 변수를 쓴다(창마다 만들면 Tcl이 죽는다)."""
        if self.busy:
            return
        window = tk.Toplevel(self)
        window.title("설정")
        window.transient(self)
        window.grab_set()
        outer = ttk.Frame(window, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Checkbutton(outer, text="읽은 내용 기억 (다음 미리보기가 빨라짐)", variable=self.cache_var).pack(anchor="w")
        ttk.Checkbutton(outer, text="다른 작업 우선 (분류가 느려지는 대신 PC가 덜 버벅임)",
                        variable=self.gentle_var).pack(anchor="w", pady=(4, 0))
        ttk.Label(outer, text="\n정리에서 뺄 폴더·파일 이름 · 한 줄에 하나\n예) 개인   임시*   *.tmp   ~$*").pack(anchor="w")
        box = tk.Text(outer, width=44, height=8, wrap="none")
        box.pack(fill="both", expand=True, pady=6)
        box.insert("1.0", "\n".join(organizer.EXCLUDE))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")

        def save() -> None:
            patterns = list(dict.fromkeys(line.strip() for line in box.get("1.0", "end").splitlines() if line.strip()))
            changed = patterns != organizer.EXCLUDE
            organizer.EXCLUDE[:] = patterns
            save_excludes(patterns)
            window.destroy()
            if changed:
                self.status_var.set(f"제외 {len(patterns)}개 · 다음 미리보기부터 적용" if patterns else "제외 없음")

        ttk.Button(buttons, text="기억 지우기…", command=self.clear_saved_cache).pack(side="left")
        ttk.Button(buttons, text="저장", command=save).pack(side="right")
        ttk.Button(buttons, text="취소", command=window.destroy).pack(side="right", padx=6)

    def show_about(self) -> None:
        messagebox.showinfo(
            "정보", f"문서 OCR 정리 {APP_VERSION}\n\n"
                   "문서는 이 PC 안에서만 읽고 분류합니다. 인터넷으로 나가지 않습니다.\n"
                   "OCR: Tesseract · 로컬 AI: Ollama\n\n라이선스: AGPL-3.0")

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
        routes = group_preview_documents(self.documents, self.filter_var.get(), self.search_var.get())
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
                            "읽기 실패" if doc.error else "중복" if doc.duplicate_of else ""),
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
                              values=("", ""), open=opened, tags=(folder_tag(path),))
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
        if self.documents:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(in_=self.table, relx=0.5, rely=0.45, anchor="center")
        if not restore:
            self.detail_var.set("")

    def _paths(self) -> tuple[Path, Path] | None:
        source = Path(self.source_var.get().strip())
        destination = Path(self.destination_var.get().strip())
        if not self.source_var.get().strip() or not source.is_dir():
            messagebox.showerror("폴더 확인", "원본 폴더를 선택해 주세요.")
            return None
        if not self.destination_var.get().strip():
            messagebox.showerror("폴더 확인", "결과 폴더를 선택해 주세요.")
            return None
        source = source.resolve()
        destination = destination.resolve()
        if source == destination or source.is_relative_to(destination):
            messagebox.showerror("폴더 확인", "결과 폴더는 원본 폴더와 다른 곳으로 지정해 주세요.")
            return None
        return source, destination

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.scan_button.configure(state="disabled" if busy else "normal")
        self.fresh_button.configure(state="disabled" if busy else "normal")
        self.ai_checkbox.configure(state="disabled" if busy else "normal")
        self.group_button.configure(state="disabled" if busy or not self.documents else "normal")
        self.move_button.configure(state="disabled" if busy or not self.documents or self.preview_paths is None else "normal")
        self.cancel_button.configure(state="normal" if busy and not self.cancel_event.is_set() else "disabled")
        if busy:
            self.progress.pack(side="right", padx=(self.px(8), 0))
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
        for menu, label in (("도구", "1. 미리보기"), ("도구", "AI에게 새로 묻기"), ("파일", "정리 되돌리기…"),
                            ("편집", FOLDERS_LABEL + "…"), ("도구", "설정…")):
            self._menu_state(menu, label, not busy)
        self.undo_button.configure(state="disabled" if busy else "normal")
        self._menu_state("도구", "3. 파일 정리 실행", not busy and bool(self.documents) and self.preview_paths is not None)
        self._menu_state("도구", "중단", busy)
        self._menu_state("파일", "결과 폴더 열기", self.last_result_dir is not None)

    def cancel_current(self) -> None:
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("중단 중… (현재 파일 마무리)")

    def request_close(self) -> None:
        if not self.busy:
            self.autosave_preview()  # 손으로 고친 분류도 남긴다.
            self.destroy()
            return
        if not messagebox.askyesno(
            "작업 중 창 닫기",
            "지금 처리 중인 파일까지만 마치고 닫습니다.\n이미 옮긴 파일과 기록은 그대로 남습니다.\n\n창을 닫으시겠습니까?",
        ):
            return
        self.close_requested = True
        self.cancel_current()

    def on_ai_toggle(self) -> None:
        """AI 분류를 켜면 Ollama와 모델이 준비됐는지 먼저 확인한다."""
        if not self.ai_var.get() or self.busy:
            return
        self.status_var.set("AI 준비 확인 중…")

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
                    "로컬 AI 준비", "Ollama가 꺼져 있습니다. 실행하시겠습니까?",
                ):
                    self.start_ollama_setup(install=False)
                    return
            elif winget_available():
                if messagebox.askyesno(
                    "로컬 AI 준비",
                    "AI 분류에는 Ollama(무료)가 필요합니다.\n\n"
                    "winget으로 설치하시겠습니까?\n"
                    "약 1GB를 인터넷에서 받으며, Ollama 사용 조건에 동의하게 됩니다.",
                ):
                    self.start_ollama_setup(install=True)
                    return
            elif messagebox.askyesno(
                "로컬 AI 준비",
                f"{error}\n\nAI 분류에는 Ollama(무료)가 필요합니다. 설치하고 실행한 뒤 'AI 사용'을 다시 켜 주세요.\n\n다운로드 페이지를 여시겠습니까?",
            ):
                webbrowser.open(OLLAMA_DOWNLOAD)
            self.ai_var.set(False)
            self.status_var.set(f"{error} · Ollama 설치·실행 후 'AI 사용' 다시 켜기")
            return
        if not missing:
            self.status_var.set("AI 준비 완료")
            return
        if messagebox.askyesno(
            "로컬 AI 모델 받기",
            f"AI 모델이 없습니다: {', '.join(missing)}\n\n"
            "처음 한 번만 약 3GB를 인터넷에서 받습니다.\n지금 받으시겠습니까?",
        ):
            self.start_model_pull(missing)
        else:
            self.ai_var.set(False)
            self.status_var.set("AI 모델 없음 · AI 분류 끔")

    def start_ollama_setup(self, install: bool) -> None:
        """Ollama를 설치(필요하면)하고 실행한 뒤, 이어서 모델 확인 단계로 넘어간다."""
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("Ollama 설치 중…" if install else "Ollama 실행 중…")

        def running() -> bool:
            try:
                ollama_missing_models(timeout=2)
                return True
            except RuntimeError:
                return False

        def work() -> None:
            try:
                if install:
                    install_ollama(lambda line: self.events.put(("progress", f"Ollama 설치: {line}")),
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
            "기억 지우기",
            f"읽은 내용과 AI 답 기억을 지우시겠습니까?\n{path}\n\n"
            "원본 파일은 그대로 남지만, 다음 미리보기는 처음처럼 오래 걸립니다.",
        ):
            return
        try:
            clear_cache(path)
        except OSError as exc:
            messagebox.showerror("기억 지우기", str(exc))
            return
        self.status_var.set("기억 지움")

    def preview_summary(self, counts: bool = True) -> str:
        """상태 표시줄 한 줄 요약. 전체 내용(점검 항목·구조 점수 세부·AI 메모)은 self.summary_detail에 둔다."""
        quality = quality_check(self.documents)
        unknown = sum(doc.folder == "미분류" for doc in self.documents)
        note = getattr(self, "scan_note", "")
        short = [f"파일 {len(self.documents)}",
                 f"폴더 {len({doc.folder for doc in self.documents if doc.folder and doc.folder != '미분류'})}",
                 f"미분류 {unknown}"]
        if "AI 새로 물음" in note:
            short.append("AI 새로 물음")
        if quality.warnings:
            short.append(f"⚠ 확인 {len(quality.warnings)}개 → 자세히")
        self.summary_detail = "\n\n".join(part for part in (
            self._full_summary(counts), "확인할 것:\n" + "\n".join(f"· {w}" for w in quality.warnings)
            if quality.warnings else "") if part)
        self.detail_button.configure(state="normal")
        return " · ".join(short)

    def _full_summary(self, counts: bool = True) -> str:
        lines = []
        if counts:
            unknown = sum(doc.folder == "미분류" for doc in self.documents)
            failed = sum(bool(document.error) for document in self.documents)
            duplicates = sum(document.duplicate_of is not None for document in self.documents)
            filename_only = sum(document.source.suffix.lower() not in SUPPORTED for document in self.documents)
            folder_count = len({doc.folder for doc in self.documents if doc.folder and doc.folder != "미분류"})
            lines.append(
                f"파일 {len(self.documents)} · 폴더 {folder_count} · "
                f"미분류 {unknown} · 읽기 실패 {failed} · "
                f"중복 {duplicates} · 파일명만 {filename_only}"
                + (f" · {self.scan_note}" if getattr(self, "scan_note", "") else ""))
        lines.append(quality_check(self.documents).summary())
        lines.append(structure_score(self.documents).summary() + " (기록관리 기준)")
        return "\n".join(lines)

    def show_summary_detail(self) -> None:
        messagebox.showinfo("분류 요약", getattr(self, "summary_detail", "") or "아직 미리보기를 하지 않았습니다.")

    def toggle_top_level(self) -> None:
        """세부 주제 폴더를 AI가 만든 대분류 아래로 묶거나, 묶기 전으로 되돌린다."""
        if self.busy or not self.documents:
            return
        if self.grouped_snapshot is not None:
            if not messagebox.askyesno(
                "대분류 풀기", "묶기 전으로 되돌립니다. 묶은 뒤에 고친 내용도 함께 취소됩니다.\n\n풀겠습니까?",
            ):
                return
            for doc, (folder, subfolder, detail, reason) in zip(self.documents, self.grouped_snapshot):
                doc.folder, doc.subfolder, doc.detail, doc.folder_reason = folder, subfolder, detail, reason
            self.grouped_snapshot = None
            self.group_button.configure(text="대분류로 묶기")
            self.refresh_table()
            self.status_var.set("대분류 해제 · " + self.preview_summary(counts=False))
            return
        topics = {doc.folder for doc in self.documents if doc.duplicate_of is None
                  and doc.folder not in {"", "미분류"} and doc.folder not in HIERARCHY_FORMAT_FOLDERS}
        if len(topics) < MIN_TOPICS:
            messagebox.showinfo(
                "대분류로 묶기",
                f"주제 폴더가 {len(topics)}개라 이미 한눈에 보입니다.\n{MIN_TOPICS}개 이상일 때 묶을 수 있습니다.")
            return
        snapshot = [(doc.folder, doc.subfolder, doc.detail, doc.folder_reason) for doc in self.documents]
        fresh_ai = getattr(self, "fresh_ai_last", False)
        documents = copy.deepcopy(self.documents)  # 작업 스레드는 사본만 고친다.
        use_cache = self.cache_var.get()
        gentle = self.gentle_var.get()
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("AI 대분류 생성 중…")

        def work() -> None:
            set_gentle(gentle)
            try:
                if ollama_missing_models():
                    raise RuntimeError("AI 모델 없음 · 'AI 사용'을 켜서 준비")
                with contextlib.ExitStack() as stack:
                    cache = stack.enter_context(LocalCache(default_cache_path())) if use_cache else None
                    # 'AI로 새로 분류'한 결과라면 대분류도 새로 묻는다.
                    calls = cached_hierarchy(cache.responses(fresh=fresh_ai)) if cache else {}
                    reporter = progress_reporter(self.events, "대분류")
                    plan = add_top_level(documents, stage=reporter.stage, **calls)
                self.events.put(("grouped", documents, snapshot, plan, ""))
            except (OSError, RuntimeError, ValueError) as exc:
                self.events.put(("grouped", None, snapshot, {}, f"대분류를 만들지 못했습니다: {exc}"))
            finally:
                ollama_unload()
                set_gentle(False)

        threading.Thread(target=work, daemon=False).start()

    def start_scan(self, fresh_ai: bool = False) -> None:
        """fresh_ai=True면 읽은 본문은 저장된 것을 쓰고 AI 분류만 새로 받는다."""
        if self.busy:
            return
        paths = self._paths()
        if not paths:
            return
        source, destination = paths
        use_ai = self.ai_var.get()
        use_cache = self.cache_var.get()
        gentle = self.gentle_var.get()
        targets, allow_new = list(self.user_folders), self.allow_new_folders
        if fresh_ai:
            self.ai_var.set(True)
            use_ai = True
        self.fresh_ai_last = fresh_ai
        self.documents = []
        self.preview_paths = None
        self.refresh_table()
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("읽기 시작…")

        def work() -> None:
            set_gentle(gentle)
            try:
                if use_ai:
                    # 긴 읽기를 마친 뒤에야 모델이 없다고 알리지 않도록 먼저 확인한다.
                    missing = ollama_missing_models()
                    if missing:
                        raise RuntimeError(
                            f"AI 모델 없음: {', '.join(missing)} · 'AI 사용'을 껐다 켜서 받기")
                # 저장소는 이 작업 스레드에서만 열고 닫는다.
                with contextlib.ExitStack() as stack:
                    cache = stack.enter_context(LocalCache(default_cache_path())) if use_cache else None
                    documents = scan(
                        source, destination, find_tesseract(),
                        progress_reporter(self.events, "읽기"),
                        should_cancel=self.cancel_event.is_set,
                        classify=not use_ai, cache=cache,
                    )
                    if self.cancel_event.is_set():
                        raise InterruptedError("검사가 중단되었습니다")
                    count = sum(doc.duplicate_of is None for doc in documents)
                    # 천천히 실행은 요청마다 일한 시간의 절반을 쉬므로 1.5배로 잡는다.
                    expected = count * ai_seconds_per_document(cache) * (1.5 if gentle else 1) if use_ai else None
                    if use_ai:
                        self.events.put(("progress", f"AI 분류 · {count}개 · 예상 {_duration(expected)}"))
                    reporter = progress_reporter(self.events, "AI 분류", whole=expected)
                    ai_note = ai_preview_note(
                        documents, use_ai,
                        progress=reporter, stage=lambda name: reporter.stage(f"AI · {name}"),
                        should_cancel=self.cancel_event.is_set,
                        calls=cached_ollama(cache.responses(fresh=fresh_ai)) if cache else None,
                        source_root=source,
                    )
                    if use_ai and not gentle:
                        remember_ai_speed(cache, time.monotonic() - reporter.began, count)
                    ai_snapshot = ([(d.folder, d.subfolder, d.detail, d.folder_reason) for d in documents]
                                   if use_ai else None)
                    if use_ai and targets:
                        if self.cancel_event.is_set():
                            raise InterruptedError("검사가 중단되었습니다")
                        pick = (cached_hierarchy(cache.responses(fresh=fresh_ai))["user_pick"] if cache
                                else ollama_user_pick)
                        fit_user_folders(documents, targets, pick, allow_new,
                                         stage=lambda name: reporter.stage(f"AI · {name}"))
                        ai_note += f" · 지정 폴더 {len(targets)}개"
                if self.cancel_event.is_set():
                    raise InterruptedError("검사가 중단되었습니다")
                projects = source_files(source, destination)[1]
                if projects:
                    names = ", ".join(project.name for project in projects[:3])
                    more = f" 외 {len(projects) - 3}개" if len(projects) > 3 else ""
                    ai_note = (f"제외: 코드·압축 폴더 {len(projects)}개({names}{more}) · "
                               + ai_note)
                if fresh_ai:
                    ai_note = "AI 새로 물음 · " + ai_note
                self.events.put(("scanned", documents, paths, ai_note, ai_snapshot))
            except InterruptedError:
                self.events.put(("cancelled_scan",))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                if use_ai:
                    ollama_unload()
                set_gentle(False)

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
                self.table.item(row, tags=row_tags(self.folder_rows, row))
        self.drag_target, self.drag_hover = item, hover
        for row in (item, hover):
            if row and self.table.exists(row):
                self.table.item(row, tags=row_tags(self.folder_rows, row) + ("drop_target",))
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
                font="TkDefaultFont",
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
            self.status_var.set("폴더 위에 놓기")
            return "break"
        folder, subfolder, detail = (*target, "", "")[:3]
        before = self._edit_state()
        changed = reassign_documents(self.documents, indices, folder, subfolder, detail)
        self._remember_edit(before)
        self.refresh_table()
        self.status_var.set(f"{len(changed)}개 파일 → {' / '.join(target)}")
        return "break"

    def on_table_double_click(self, event: tk.Event) -> str | None:
        """파일은 기본 프로그램으로 연다. 폴더는 이름 칸이면 이름 바꾸기, 나머지 칸이면 들어갈 파일 목록.
        파일 이름 바꾸기는 F2·우클릭으로 한다."""
        if self.busy:
            return None
        item = self.table.identify_row(event.y)
        if not item:
            return None
        self.table.selection_set(item)
        if item in self.folder_rows and self.table.identify_column(event.x) == "#0":
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
                messagebox.showinfo("이름 바꾸기", "이 항목은 이름을 바꿀 수 없습니다.")
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
        before = self._edit_state()
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
                    "폴더 합치기", f"'{name}' 폴더가 이미 있습니다. 합치시겠습니까?",
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
        self._remember_edit(before)
        self.refresh_table()
        if new_route:
            target = next((row for row, route in self.folder_rows.items() if route == new_route), None)
            if target:
                self.table.selection_set(target)
                self.table.see(target)
        self.status_var.set(f"이름 변경: {name}")
        return "break"

    def edit_user_folders(self) -> None:
        """폴더명 지정 창. 처음에는 AI가 만든 폴더를 파일 수와 함께 채워 둔다."""
        if self.busy:
            return
        snapshot = getattr(self, "ai_snapshot", None)
        if snapshot and len(snapshot) == len(self.documents):
            routes = [(folder, sub) for (folder, sub, _, _), doc in zip(snapshot, self.documents)
                      if doc.duplicate_of is None]
        else:
            routes = [(doc.folder, doc.subfolder) for doc in self.documents if doc.duplicate_of is None]
        ai_counts = folder_counts(routes)
        suggestions = [name for name, _ in ai_counts.most_common() if "/" not in name]
        # 지정 폴더별 파일 수는 지금 미리보기 기준으로 보여 준다.
        counts = folder_counts([(doc.folder, doc.subfolder) for doc in self.documents if doc.duplicate_of is None])
        counts.update({name: n for name, n in ai_counts.items() if name not in counts})
        self.allow_new_var.set(self.allow_new_folders)

        def save(targets: list[str]) -> None:
            self.user_folders, self.allow_new_folders = targets, self.allow_new_var.get()
            save_user_folders(targets, self.allow_new_folders)
            self.folders_button.configure(text=user_folders_label(targets))
            self.apply_user_folders()

        self.folder_editor = FolderEditor(self, self.user_folders, suggestions, counts, self.allow_new_var, save,
                                          px=self.px, suggest_groups=self.suggest_folder_groups)

    def remember_routes(self) -> None:
        """지금 분류를 기준으로 기억한다. 이후 달라진 파일은 사용자가 직접 고친 것이다."""
        self.base_routes = [(doc.folder, doc.subfolder, doc.detail, doc.output_name) for doc in self.documents]
        self.undo_edits = []

    def _edit_state(self) -> list[tuple[str, str, str, str, str]]:
        return [(doc.folder, doc.subfolder, doc.detail, doc.output_name, doc.folder_reason) for doc in self.documents]

    def _remember_edit(self, before: list[tuple[str, str, str, str, str]]) -> None:
        if before != self._edit_state():
            self.undo_edits = (getattr(self, "undo_edits", []) + [before])[-UNDO_LIMIT:]

    def undo_edit(self) -> None:
        """마지막으로 손으로 고친 것을 되돌린다(최대 UNDO_LIMIT번)."""
        edits = getattr(self, "undo_edits", [])
        if self.busy or not edits or len(edits[-1]) != len(self.documents):
            self.status_var.set("되돌릴 수정 없음")
            return
        for doc, (folder, sub, detail, name, reason) in zip(self.documents, edits.pop()):
            doc.folder, doc.subfolder, doc.detail, doc.output_name, doc.folder_reason = folder, sub, detail, name, reason
        self.refresh_table()
        self.status_var.set(f"수정 되돌림 · 남은 되돌리기 {len(edits)}번")

    def manual_edit_count(self) -> int:
        base = getattr(self, "base_routes", None)
        if not base or len(base) != len(self.documents):
            return 0
        return sum(route != (doc.folder, doc.subfolder, doc.detail, doc.output_name)
                   for route, doc in zip(base, self.documents))

    def suggest_folder_groups(self, names: list[str]) -> None:
        """폴더명 지정 창의 'AI 묶기 제안'. 대분류로 묶기와 같은 AI 단계로 {폴더: 상위} 안을 만든다."""
        by_folder: dict[str, list[Document]] = {}
        for doc in self.documents:
            by_folder.setdefault(doc.folder, []).append(doc)
        documents = [copy.deepcopy(doc) for name in names for doc in by_folder.get(name, [])[:5]]
        # 사용자가 새로 적은 이름은 파일이 없으니 이름만 가진 문서로 보낸다.
        documents += [Document(Path(f"{name}.txt"), name, "", "") for name in names if name not in by_folder]
        use_cache = self.cache_var.get()

        def work() -> None:
            try:
                if ollama_missing_models():
                    raise RuntimeError("AI 모델 없음 · 'AI 사용'을 켜서 준비")
                with contextlib.ExitStack() as stack:
                    cache = stack.enter_context(LocalCache(default_cache_path())) if use_cache else None
                    calls = cached_hierarchy(cache.responses()) if cache else {}
                    calls = {key: call for key, call in calls.items() if key in {"taxonomy", "pick", "embed"}}
                    plan = add_top_level(documents, min_topics=3, **calls)
                self.events.put(("group_suggestion", plan, ""))
            except (OSError, RuntimeError, ValueError) as exc:
                self.events.put(("group_suggestion", {}, f"AI 묶기 실패: {exc}"))
            finally:
                ollama_unload()

        threading.Thread(target=work, daemon=False).start()

    def apply_user_folders(self) -> None:
        """지금 미리보기의 AI 결과에 지정 폴더를 다시 맞춘다. 읽기·AI 묶기는 다시 하지 않는다."""
        snapshot = getattr(self, "ai_snapshot", None)
        if not snapshot or not self.documents or len(snapshot) != len(self.documents):
            self.status_var.set(f"지정 폴더 {len(self.user_folders)}개 · 다음 미리보기부터 적용"
                                if self.user_folders else "지정 폴더 없음 · 자동 분류")
            return
        edited = self.manual_edit_count()
        if edited and not messagebox.askyesno(
                "폴더 구조 정하기", f"직접 고친 파일 {edited}개가 AI 결과대로 돌아갑니다.\n새 폴더 구조에 다시 맞추시겠습니까?"):
            self.status_var.set(f"지정 폴더 {len(self.user_folders)}개 저장 · 다음 미리보기부터 적용")
            return
        documents = copy.deepcopy(self.documents)
        for doc, (folder, sub, detail, reason) in zip(documents, snapshot):
            doc.folder, doc.subfolder, doc.detail, doc.folder_reason = folder, sub, detail, reason
        targets, allow_new, use_cache = list(self.user_folders), self.allow_new_folders, self.cache_var.get()
        if not targets:
            self.events.put(("fitted", "지정 폴더 없음 · 자동 분류", documents))
            self._set_busy(True)
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set(f"지정 폴더 {len(targets)}개에 맞추는 중…")

        def work() -> None:
            try:
                with contextlib.ExitStack() as stack:
                    cache = stack.enter_context(LocalCache(default_cache_path())) if use_cache else None
                    pick = cached_hierarchy(cache.responses())["user_pick"] if cache else ollama_user_pick
                    fit_user_folders(documents, targets, pick, allow_new,
                                     stage=lambda name: self.events.put(("progress", f"AI · {name}…")))
                self.events.put(("fitted", f"지정 폴더 {len(targets)}개 적용", documents))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                ollama_unload()

        threading.Thread(target=work, daemon=False).start()

    def create_folder_for_selection(self) -> None:
        indices = selected_file_indices(self.table.selection())
        if not indices:
            return
        name = simpledialog.askstring("새 주제 폴더", "폴더 이름", parent=self)
        if name is None:
            return
        before = self._edit_state()
        try:
            changed = reassign_documents(self.documents, indices, name.strip(), "")
        except ValueError as exc:
            messagebox.showerror("폴더 이름", str(exc))
            return
        self._remember_edit(before)
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
        before = self._edit_state()
        try:
            changed = reassign_documents(self.documents, indices, folder, name.strip())
            self._remember_edit(before)
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
            messagebox.showinfo("미리보기", "파일이나 폴더를 먼저 선택해 주세요.")
            return
        item = self.table.selection()[0]
        selected = preview_selection(self.documents, self.folder_rows, item)
        if not selected:
            messagebox.showinfo("미리보기", "파일이 없습니다.")
            return
        folder_row = self.folder_rows.get(item)
        if not folder_row:
            # 파일은 연결된 프로그램으로 바로 연다. 실행 파일 같은 형식만 먼저 묻는다.
            document = selected[0]
            if document.source.suffix.casefold() in CONFIRM_BEFORE_OPEN:
                if not messagebox.askyesno("파일 열기", f"{document.source.name}\n\n실행 파일입니다. 여시겠습니까?"):
                    return
            try:
                os.startfile(document.source)
            except OSError as exc:
                messagebox.showerror("파일 열기", f"파일을 열지 못했습니다.\n{exc}")
            return
        window = tk.Toplevel(self)
        window.title("폴더 미리보기")
        window.geometry(f"{self.px(880)}x{self.px(660)}")
        window.minsize(self.px(560), self.px(380))
        window.transient(self)
        window.bind("<Escape>", lambda _event: window.destroy())
        outer = ttk.Frame(window, padding=12)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        title = " / ".join(part for part in folder_row if part)
        ttk.Label(header, text=title, font="AppBold").pack(side="left")
        content = ttk.Frame(outer)
        content.pack(fill="both", expand=True)
        body = tk.Text(content, wrap="word", padx=10, pady=10)
        body.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(content, orient="vertical", command=body.yview)
        scrollbar.pack(side="right", fill="y")
        body.configure(yscrollcommand=scrollbar.set)
        source_root = self.preview_paths[0] if self.preview_paths else Path(self.source_var.get())
        body.insert("end", f"정리 후 이 폴더에 들어갈 파일 {len(selected)}개\n\n")
        for doc in selected:
            body.insert("end", f"{doc.output_name or doc.source.name}  ·  {preview_location(source_root, doc.source)}\n")
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
            messagebox.showerror("정리 되돌리기", f"기록 파일을 읽지 못했습니다.\n{exc}")
            return
        if not jobs:
            messagebox.showinfo("정리 되돌리기", "되돌릴 파일이 없습니다.")
            return
        if not messagebox.askyesno(
            "정리 되돌리기",
            f"파일 {len(jobs)}개를 원래 자리로 되돌리시겠습니까?\n"
            "원래 자리에 같은 이름의 새 파일이 있으면 그 파일은 건너뜁니다.",
        ):
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("되돌리는 중…")

        def work() -> None:
            try:
                restored, errors = undo_report(
                    report,
                    progress_reporter(self.events, "되돌리기"),
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
            messagebox.showerror("미리보기 확인", "미리보기 뒤에 폴더가 바뀌었습니다. 미리보기를 다시 해 주세요.")
            return
        copy_mode = self.copy_var.get()
        try:
            short, free = space_needed(self.documents, destination, copy_mode)
        except OSError:
            short, free = 0, 0  # 공간을 못 재면 막지 않는다. 옮기다 실패하면 파일마다 실패로 남는다.
        if short:
            messagebox.showerror("공간 부족", f"결과 폴더가 있는 드라이브에 공간이 부족합니다.\n남은 공간: {size_text(free)}\n"
                                              f"더 필요한 공간: {size_text(short)}\n\n다른 드라이브를 고르거나 공간을 비워 주세요.")
            return
        if not messagebox.askyesno("복사 확인" if copy_mode else "이동 확인",
                                   move_confirmation_text(self.documents, copy_mode)):
            return
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("폴더 오류", str(exc))
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("복사 중 (검증)…" if copy_mode else "이동 중 (검증)…")

        def work() -> None:
            try:
                # 기록 파일을 만들 수 없으면 아무것도 옮기지 않는다.
                with ResultReport(source, destination, self.documents) as report:
                    moved, deduplicated, errors = move_documents(
                        self.documents, destination,
                        progress_reporter(self.events, "복사" if copy_mode else "이동"),
                        should_cancel=self.cancel_event.is_set,
                        source_root=source,
                        record=report.record,
                        keep_originals=copy_mode,
                    )
                self.events.put(("moved", moved, deduplicated, errors, destination, report.path, copy_mode))
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
                    self.ai_snapshot = event[4] if len(event) > 4 else None
                    self.grouped_snapshot = None
                    self.group_button.configure(text="대분류로 묶기")
                    self.refresh_table()
                    self.scan_note = event[3]
                    self.status_var.set(self.preview_summary())
                    self._set_busy(False)
                    self.remember_routes()
                    self.autosave_preview()
                elif kind == "grouped":
                    grouped, snapshot, plan, error = event[1:]
                    self._set_busy(False)
                    if error:
                        self.status_var.set(error)
                        if not self.close_requested:
                            messagebox.showerror("대분류로 묶기", error)
                    elif not plan:
                        self.status_var.set("묶을 대분류 없음 · 그대로")
                        if not self.close_requested:
                            messagebox.showinfo("대분류로 묶기", "묶을 대분류가 없어 그대로 둡니다.")
                    else:
                        self.documents = grouped
                        self.grouped_snapshot = snapshot
                        self.group_button.configure(text="대분류 풀기")
                        self.refresh_table()
                        self.status_var.set(
                            f"대분류 {len(set(plan.values()))}개 ← 세부 주제 {len(plan)}개 · 끌어서 수정 / '대분류 풀기'\n"
                            .rstrip("\n") + " · " + self.preview_summary(counts=False))
                elif kind == "group_suggestion":
                    editor = getattr(self, "folder_editor", None)
                    if editor is not None:
                        editor.apply_groups(event[1], event[2])
                elif kind == "fitted":
                    self.documents = event[2]
                    self.remember_routes()
                    self._set_busy(False)
                    self.grouped_snapshot = None
                    self.group_button.configure(text="대분류로 묶기")
                    self.refresh_table()
                    self.status_var.set(event[1] + " · " + self.preview_summary(counts=False))
                elif kind == "cancelled_scan":
                    self.preview_paths = None
                    self.status_var.set("중단됨 · 원본 그대로")
                    self._set_busy(False)
                elif kind == "moved":
                    moved, deduplicated, errors, destination, report = event[1:6]
                    verb, dup = ("복사", "중복 건너뜀") if len(event) > 6 and event[6] else ("이동", "중복 정리")
                    unprocessed = len(self.documents) - len(moved) - len(deduplicated) - len(errors)
                    self.preview_paths = None
                    self.last_result_dir = destination
                    self.open_button.configure(state="normal")
                    self.status_var.set(
                        f"{verb} {len(moved)} · {dup} {len(deduplicated)} · 실패 {len(errors)} · 미처리 {unprocessed} · {destination}"
                        + f" · 기록 {report.name}"
                    )
                    self._set_busy(False)
                    if self.close_requested:
                        continue
                    if errors or unprocessed:
                        detail = "\n".join(f"{doc.source.name}: {error}" for doc, error in errors[:8])
                        messagebox.showwarning("정리 결과", f"실패 {len(errors)}개, 처리하지 못한 파일 {unprocessed}개가 있습니다.\n남은 원본을 확인한 뒤 미리보기를 다시 해 주세요.\n\n{detail}")
                    else:
                        if messagebox.askyesno(
                                "정리 결과",
                                f"정리를 마쳤습니다.\n{verb} {len(moved)}개 · {dup} {len(deduplicated)}개\n{destination}\n\n결과 폴더를 여시겠습니까?"):
                            self.open_result()
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
                        self.status_var.set("Ollama 실행됨 · 모델 확인 중…")
                        self.on_ai_toggle()
                elif kind == "pulled":
                    self._set_busy(False)
                    if event[1]:
                        self.ai_var.set(False)
                        self.status_var.set(event[1])
                        if not self.close_requested:
                            messagebox.showerror("AI 모델 받기", event[1])
                    else:
                        self.status_var.set("AI 모델 받음 · 준비 완료")
                elif kind == "undone":
                    restored, errors, total = event[1:]
                    # 파일 위치가 바뀌었으므로 예전 미리보기로는 정리할 수 없다.
                    self.documents = []
                    self.preview_paths = None
                    self.refresh_table()
                    skipped = total - len(restored) - len(errors)
                    self.status_var.set(
                        f"되돌리기: {len(restored)}개 복원 · 실패 {len(errors)}개"
                        + (f" · 중단 미처리 {skipped}개" if skipped else "")
                    )
                    self._set_busy(False)
                    if self.close_requested:
                        continue
                    if errors or skipped:
                        detail = "\n".join(f"{path.name}: {error}" for path, error in errors[:8])
                        messagebox.showwarning(
                            "되돌리기 결과",
                            f"복원 {len(restored)}개 · 실패 {len(errors)}개 · 처리 못 함 {skipped}개\n\n{detail}",
                        )
                    else:
                        messagebox.showinfo("되돌리기 결과", f"파일 {len(restored)}개를 원래 자리로 되돌렸습니다.")
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


def set_gentle(enabled: bool) -> None:
    """'천천히 실행': 이 앱과 OCR 프로세스의 우선순위를 낮추고, AI 요청도 가볍게 보낸다."""
    ai_groups.GENTLE = enabled
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        # BELOW_NORMAL_PRIORITY_CLASS / NORMAL_PRIORITY_CLASS. 낮춘 우선순위는 OCR 자식 프로세스도 물려받는다.
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000 if enabled else 0x20)
    except (AttributeError, OSError):
        pass


def enable_sharp_display() -> None:
    """Windows 화면 배율을 직접 처리해 글자가 흐리게 늘어나지 않게 한다. 실패해도 예전처럼 동작한다."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


if __name__ == "__main__":
    enable_sharp_display()
    OrganizerApp().mainloop()
