"""로컬 문서 분류기 GUI. 실행: python app.py"""

from __future__ import annotations

import queue
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from organizer import Document, default_destination, move_documents, rename_folder, scan, valid_folder_name, write_result_report
from reader import SUPPORTED, find_tesseract, ocr_status


def move_confirmation_text(documents: list[Document]) -> str:
    return (
        f"{len(documents)}개 파일을 결과 폴더로 이동할까요?\n"
        "이동한 원본과 확인된 중복 원본은 삭제됩니다."
    )


class OrganizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("문서 OCR 정리")
        self.geometry("1060x650")
        self.minsize(780, 480)
        self.documents: list[Document] = []
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
        self.folder_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="전체")
        self.count_var = tk.StringVar(value="표시 0개")

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

        ttk.Label(frame, text="원본과 하위 폴더를 검사합니다. 정리할 때 내용을 검증한 뒤 원본 파일을 이동합니다.").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(8, 3)
        )
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
        ttk.Label(filters, text="열 제목을 눌러 정렬").pack(side="left", padx=12)
        ttk.Label(filters, textvariable=self.count_var).pack(side="left")
        columns = ("category", "source", "reason", "method")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        for key, title, width in (
            ("category", "폴더", 170),
            ("source", "파일", 410),
            ("reason", "분류 조건", 175),
            ("method", "읽기 방식 / 오류", 280),
        ):
            self.table.heading(key, text=title, command=lambda column=key: self.sort_table(column))
            self.table.column(key, width=width, minwidth=70, stretch=False)
        self.table.grid(row=1, column=0, sticky="nsew")
        self.table.bind("<<TreeviewSelect>>", self.on_table_selection)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.table.configure(xscrollcommand=horizontal.set)

        controls = ttk.Frame(frame)
        controls.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        ttk.Label(controls, text="폴더 이름").pack(side="left")
        ttk.Entry(controls, textvariable=self.folder_var, width=22).pack(side="left", padx=8)
        ttk.Button(controls, text="선택 파일에 적용", command=self.apply_to_selected).pack(side="left")
        ttk.Button(controls, text="폴더 전체 이름 변경", command=self.rename_selected_folder).pack(side="left", padx=8)
        ttk.Label(frame, textvariable=self.status_var, wraplength=980).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(5, 0)
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

    def refresh_table(self) -> None:
        selected = set(self.table.selection())
        self.table.delete(*self.table.get_children())
        mode = self.filter_var.get()

        def visible(doc: Document) -> bool:
            return (
                mode == "전체" or
                (mode == "미분류" and doc.folder == "미분류") or
                (mode == "읽기 실패" and bool(doc.error)) or
                (mode == "중복" and doc.duplicate_of is not None) or
                (mode == "파일명 기준" and doc.source.suffix.lower() not in SUPPORTED)
            )

        keys = {
            "category": lambda doc: (doc.folder.casefold(), doc.source.name.casefold()),
            "source": lambda doc: (doc.source.name.casefold(), str(doc.source).casefold()),
            "reason": lambda doc: (doc.folder_reason.casefold(), doc.source.name.casefold()),
            "method": lambda doc: ((doc.error or doc.method).casefold(), doc.source.name.casefold()),
        }
        indices = [index for index, doc in enumerate(self.documents) if visible(doc)]
        indices.sort(key=lambda index: keys[self.sort_key](self.documents[index]), reverse=self.sort_reverse)
        for index in indices:
            doc = self.documents[index]
            self.table.insert("", "end", iid=str(index), values=(
                doc.folder, str(doc.source), doc.folder_reason, doc.error or doc.method,
            ))
        self.table.selection_set([str(index) for index in indices if str(index) in selected])
        self.count_var.set(f"표시 {len(indices)}/{len(self.documents)}개")

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
                )
                if self.cancel_event.is_set():
                    raise InterruptedError("검사가 중단되었습니다")
                self.events.put(("scanned", documents, paths))
            except InterruptedError:
                self.events.put(("cancelled_scan",))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=False).start()

    def on_table_selection(self, _event: tk.Event) -> None:
        selected = self.table.selection()
        if selected:
            self.folder_var.set(self.documents[int(selected[0])].folder)

    def apply_to_selected(self) -> None:
        if self.busy:
            return
        selected = self.table.selection()
        if not selected:
            messagebox.showinfo("파일 선택", "폴더를 바꿀 파일을 먼저 선택하세요")
            return
        chosen = self.folder_var.get().strip()
        if not valid_folder_name(chosen):
            messagebox.showerror("폴더 이름", "한 단계의 폴더 이름을 입력하세요. 특수문자와 예약 이름은 사용할 수 없습니다")
            return
        # 같은 내용의 파일은 한 번만 남기므로 폴더 수정도 함께 적용한다.
        originals = {
            self.documents[int(item)].duplicate_of or self.documents[int(item)].source
            for item in selected
        }
        selected = {
            str(index) for index, doc in enumerate(self.documents)
            if (doc.duplicate_of or doc.source) in originals
        }
        for item in selected:
            document = self.documents[int(item)]
            document.folder = chosen
            document.folder_reason = "사용자 지정"
        self.refresh_table()

    def rename_selected_folder(self) -> None:
        if self.busy:
            return
        selected = self.table.selection()
        if not selected:
            messagebox.showinfo("폴더 선택", "이름을 바꿀 폴더의 파일을 먼저 선택하세요")
            return
        old_name = self.documents[int(selected[0])].folder
        new_name = self.folder_var.get().strip()
        if not valid_folder_name(new_name):
            messagebox.showerror("폴더 이름", "한 단계의 폴더 이름을 입력하세요. 특수문자와 예약 이름은 사용할 수 없습니다")
            return
        changed = rename_folder(self.documents, old_name, new_name)
        self.refresh_table()
        self.status_var.set(f"{old_name} 폴더의 {len(changed)}개 파일을 {new_name} 폴더로 변경했습니다")

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
                moved, deduplicated, errors = move_documents(
                    self.documents, destination,
                    lambda done, total: self.events.put(("progress", f"이동 중: {done}/{total}")),
                    should_cancel=self.cancel_event.is_set,
                    source_root=source,
                )
                try:
                    report = write_result_report(source, destination, self.documents, moved, deduplicated, errors)
                    report_error = ""
                except OSError as exc:
                    report = None
                    report_error = str(exc)
                self.events.put(("moved", moved, deduplicated, errors, destination, report, report_error))
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
                    self.folder_var.set("")
                    self.refresh_table()
                    unknown = sum(doc.folder == "미분류" for doc in self.documents)
                    failed = sum(bool(document.error) for document in self.documents)
                    duplicates = sum(document.duplicate_of is not None for document in self.documents)
                    filename_only = sum(document.source.suffix.lower() not in SUPPORTED for document in self.documents)
                    folder_count = len({doc.folder for doc in self.documents if doc.folder and doc.folder != "미분류"})
                    self.status_var.set(
                        f"{len(self.documents)}개 확인 · 제안 폴더 {folder_count}개 · "
                        f"미분류 {unknown}개 · 읽기 실패 {failed}개 · "
                        f"동일 파일 {duplicates}개 · 파일명 기준 {filename_only}개. "
                        "분류를 확인한 뒤 정리를 시작하세요."
                    )
                    self._set_busy(False)
                elif kind == "cancelled_scan":
                    self.preview_paths = None
                    self.status_var.set("검사가 중단되었습니다. 원본 파일은 그대로 있습니다.")
                    self._set_busy(False)
                elif kind == "moved":
                    moved, deduplicated, errors, destination, report, report_error = event[1:]
                    unprocessed = len(self.documents) - len(moved) - len(deduplicated) - len(errors)
                    self.preview_paths = None
                    self.last_result_dir = destination
                    self.open_button.configure(state="normal")
                    self.status_var.set(
                        f"{len(moved)}개 이동 · 중복 원본 {len(deduplicated)}개 정리 · 실패 {len(errors)}개 · 미처리 {unprocessed}개 · {destination}"
                        + (f" · 기록 {report.name}" if report else " · 결과 기록 실패")
                    )
                    self._set_busy(False)
                    if self.close_requested:
                        continue
                    if errors or report_error or unprocessed:
                        detail = "\n".join(f"{doc.source.name}: {error}" for doc, error in errors[:8])
                        messagebox.showwarning("정리 결과", f"이동 실패 {len(errors)}개, 미처리 {unprocessed}개. 남은 원본을 확인하고 미리보기를 다시 실행하세요.\n{detail}\n{('결과 기록 실패: ' + report_error) if report_error else ''}")
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
