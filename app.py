"""로컬 문서 분류기 GUI. 실행: python app.py"""

from __future__ import annotations

import queue
import os
from collections.abc import Callable
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from ai_groups import assign_ai_folders
from organizer import Document, default_destination, move_documents, reassign_documents, rename_document, rename_folder, rename_subfolder, scan, valid_folder_name, ResultReport
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
) -> str:
    if not enabled:
        return ""
    count = assign_ai_folders(documents, progress=progress, should_cancel=should_cancel, ai_only=True)
    return f"AI 분류 {count}개."

def group_preview_documents(documents: list[Document], mode: str) -> dict[str, dict[str, list[int]]]:
    """현재 필터의 파일을 주제와 하위 폴더별로 묶는다."""
    groups: dict[str, dict[str, list[int]]] = {}
    for index, doc in enumerate(documents):
        visible = (
            mode == "전체"
            or (mode == "미분류" and doc.folder == "미분류")
            or (mode == "읽기 실패" and bool(doc.error))
            or (mode == "중복" and doc.duplicate_of is not None)
            or (mode == "파일명 기준" and doc.source.suffix.lower() not in SUPPORTED)
        )
        if visible:
            groups.setdefault(doc.folder or "미분류", {}).setdefault(doc.subfolder, []).append(index)
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
    table: ttk.Treeview, folder_rows: dict[str, tuple[str, str | None]], item: str,
) -> str | None:
    """폴더 안의 파일 행에 놓아도 가장 가까운 부모 폴더를 대상으로 삼는다."""
    while item:
        if item in folder_rows:
            return item
        item = table.parent(item)
    return None


def preview_selection(
    documents: list[Document], folder_rows: dict[str, tuple[str, str | None]], item: str,
) -> list[Document]:
    """선택한 파일 또는 제안 폴더에 들어갈 파일을 돌려준다."""
    if item in folder_rows:
        folder, subfolder = folder_rows[item]
        return [doc for doc in documents
                if doc.folder == folder and (subfolder is None or doc.subfolder == subfolder)]
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
        self.folder_rows: dict[str, tuple[str, str | None]] = {}
        self.drag_origin: tuple[int, int] | None = None
        self.drag_indices: list[int] = []
        self.drag_active = False
        self.drag_target: str | None = None
        self.drag_hover: str | None = None
        self.drag_preview: tk.Toplevel | None = None
        self.drag_preview_label: tk.Label | None = None
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

        self.ai_checkbox = ttk.Checkbutton(frame, text="로컬 AI 분류 (규칙 검증 포함)", variable=self.ai_var)
        self.ai_checkbox.grid(row=2, column=2, sticky="e")
        self.scan_button = ttk.Button(frame, text="분류 미리보기", command=self.start_scan)
        self.scan_button.grid(row=3, column=0, sticky="w", pady=8)
        self.cancel_button = ttk.Button(frame, text="중단", command=self.cancel_current, state="disabled")
        self.cancel_button.grid(row=3, column=1, sticky="w", pady=8, padx=8)
        self.open_button = ttk.Button(frame, text="결과 폴더 열기", command=self.open_result, state="disabled")
        self.open_button.grid(row=3, column=1, sticky="e", pady=8, padx=8)
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
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
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
        for top in self.table.get_children(""):
            self.table.item(top, open=opened)
            for child in self.table.get_children(top):
                if child in self.folder_rows:
                    self.table.item(child, open=opened)

    def refresh_table(self) -> None:
        selected = set(self.table.selection())
        selected_folders = {self.folder_rows[item] for item in selected if item in self.folder_rows}
        open_folders = {
            name for item, name in self.folder_rows.items() if self.table.item(item, "open")
        }
        self.table.delete(*self.table.get_children())
        self.folder_rows.clear()
        groups = group_preview_documents(self.documents, self.filter_var.get())
        source_root = self.preview_paths[0] if self.preview_paths else Path(self.source_var.get() or ".")
        keys = {
            "category": lambda doc: (doc.folder.casefold(), (doc.output_name or doc.source.name).casefold()),
            "source": lambda doc: ((doc.output_name or doc.source.name).casefold(), str(doc.source).casefold()),
            "method": lambda doc: ((doc.error or doc.method).casefold(), doc.source.name.casefold()),
        }
        folders = sorted(
            groups, key=lambda name: (name != "미분류", name.casefold()),
            reverse=self.sort_reverse if self.sort_key == "category" else False,
        )
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

        for position, folder in enumerate(folders):
            children = groups[folder]
            all_indices = [index for indices in children.values() for index in indices]
            folder_item = f"folder:{position}"
            top_key = (folder, None)
            self.folder_rows[folder_item] = top_key
            opened = (
                top_key in open_folders or len(groups) == 1
                or any(f"file:{index}" in selected for index in all_indices)
            )
            self.table.insert(
                "", "end", iid=folder_item, text=f"{folder}  ({len(all_indices)}개)",
                values=("", ""), open=opened, tags=("folder",),
            )
            if top_key in selected_folders:
                restore.append(folder_item)
            insert_files(folder_item, children.get("", []))
            for child_position, subfolder in enumerate(
                sorted(name for name in children if name)
            ):
                indices = children[subfolder]
                child_item = f"sub:{position}:{child_position}"
                child_key = (folder, subfolder)
                self.folder_rows[child_item] = child_key
                child_open = (
                    child_key in open_folders
                    or any(f"file:{index}" in selected for index in indices)
                )
                self.table.insert(
                    folder_item, "end", iid=child_item,
                    text=f"{subfolder}  ({len(indices)}개)",
                    values=("", ""), open=child_open, tags=("folder",),
                )
                if child_key in selected_folders:
                    restore.append(child_item)
                insert_files(child_item, indices)
        if restore:
            self.table.selection_set(restore)
        count = sum(len(indices) for children in groups.values() for indices in children.values())
        folder_count = sum(1 + sum(bool(name) for name in children) for children in groups.values())
        self.count_var.set(f"{folder_count}개 폴더 · {count}/{len(self.documents)}개 파일")
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

    def start_scan(self) -> None:
        if self.busy:
            return
        paths = self._paths()
        if not paths:
            return
        source, destination = paths
        use_ai = self.ai_var.get()
        self.documents = []
        self.preview_paths = None
        self.refresh_table()
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("문서를 읽는 중입니다…")

        def work() -> None:
            try:
                documents = scan(
                    source, destination, find_tesseract(),
                    lambda done, total: self.events.put(("progress", f"문서 읽는 중: {done}/{total}")),
                    should_cancel=self.cancel_event.is_set,
                    classify=not use_ai,
                )
                if self.cancel_event.is_set():
                    raise InterruptedError("검사가 중단되었습니다")
                if use_ai:
                    self.events.put(("progress", "로컬 AI로 문서 임베딩과 폴더명을 계산하는 중입니다…"))
                ai_note = ai_preview_note(
                    documents, use_ai,
                    progress=lambda done, total: self.events.put((
                        "progress", f"로컬 AI 분류 중: {done}/{total}"
                    )),
                    should_cancel=self.cancel_event.is_set,
                )
                if self.cancel_event.is_set():
                    raise InterruptedError("검사가 중단되었습니다")
                self.events.put(("scanned", documents, paths, ai_note))
            except InterruptedError:
                self.events.put(("cancelled_scan",))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=False).start()

    def on_drag_press(self, event: tk.Event) -> None:
        """마우스를 누른 파일과 기존 다중 선택 파일을 기억한다."""
        self.drag_origin = None
        self.drag_indices = []
        self.drag_active = False
        if self.busy:
            return
        item = self.table.identify_row(event.y)
        pressed = selected_file_indices((item,))
        if not pressed:
            return
        selected = self.table.selection()
        self.drag_indices = (
            selected_file_indices(selected) if item in selected else pressed
        )
        self.drag_origin = (event.x, event.y)

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

    def _move_drag_preview(self, event: tk.Event, target: str | None) -> None:
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
            folder, subfolder = self.folder_rows[target]
            destination = folder if not subfolder else f"{folder} / {subfolder}"
            label += f"  →  {destination}"
        self.drag_preview_label.configure(
            text=label, bg="#d8f2dc" if target else "#e7f1fb",
        )
        self.drag_preview.geometry(f"+{event.x_root + 18}+{event.y_root + 18}")
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
        if event.y < 24:
            self.table.yview_scroll(-1, "units")
        elif event.y > self.table.winfo_height() - 24:
            self.table.yview_scroll(1, "units")
        row = self.table.identify_row(event.y)
        target = drop_folder_row(self.table, self.folder_rows, row)
        self._show_drag_target(target, row if target else None)
        self._move_drag_preview(event, target)

    def on_drag_release(self, event: tk.Event) -> str | None:
        if not self.drag_active:
            self.drag_origin = None
            self.drag_indices = []
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
        folder, subfolder = target
        changed = reassign_documents(self.documents, indices, folder, subfolder or "")
        self.refresh_table()
        display = folder if not subfolder else f"{folder} / {subfolder}"
        self.status_var.set(f"{len(changed)}개 파일 → {display}")
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
        route = self.folder_rows.get(item)
        if route:
            current = route[0] if route[1] is None else route[1]
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
        box = self.table.bbox(item, "#0")
        if not box:
            return
        x, y, width, height = box
        depth = 0
        parent = self.table.parent(item)
        while parent:
            depth += 1
            parent = self.table.parent(parent)
        offset = 20 * (depth + 1)
        entry = ttk.Entry(self.table)
        entry.insert(0, current)
        entry.place(x=x + offset, y=y, width=max(100, width - offset), height=height)
        self.rename_entry = entry
        self.rename_item = item
        entry.bind("<Return>", self.finish_rename)
        entry.bind("<Escape>", self.cancel_rename)
        entry.bind("<FocusOut>", self.finish_rename)
        entry.focus_set()
        entry.selection_range(0, "end")

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
            route = self.folder_rows.get(item)
            if route:
                folder, subfolder = route
                if subfolder is None:
                    if name != folder:
                        rename_folder(self.documents, folder, name)
                    new_route = (name, None)
                else:
                    if name != subfolder:
                        rename_subfolder(self.documents, folder, subfolder, name)
                    new_route = (folder, name)
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
            folder, subfolder = self.folder_rows[item]
            count = sum(
                doc.folder == folder and (subfolder is None or doc.subfolder == subfolder)
                for doc in self.documents
            )
            display = folder if subfolder is None else f"{folder} / {subfolder}"
            self.detail_var.set(f"폴더: {display} · {count}개 파일")
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
                body.insert("end", f" / {doc.subfolder}")
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
                        lambda done, total: self.events.put(("progress", f"이동 중: {done}/{total}")),
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
                    self.refresh_table()
                    unknown = sum(doc.folder == "미분류" for doc in self.documents)
                    failed = sum(bool(document.error) for document in self.documents)
                    duplicates = sum(document.duplicate_of is not None for document in self.documents)
                    filename_only = sum(document.source.suffix.lower() not in SUPPORTED for document in self.documents)
                    folder_count = len({doc.folder for doc in self.documents if doc.folder and doc.folder != "미분류"})
                    self.status_var.set(
                        f"{len(self.documents)}개 확인 · 제안 폴더 {folder_count}개 · "
                        f"미분류 {unknown}개 · 읽기 실패 {failed}개 · "
                        f"동일 파일 {duplicates}개 · 파일명 기준 {filename_only}개."
                        + (f" {event[3]}" if event[3] else "")
                    )
                    self._set_busy(False)
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
