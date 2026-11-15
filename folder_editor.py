"""폴더명 지정 창. 목록에서 추가·이름 바꾸기·삭제·순서·묶기·합치기로 지정 폴더를 고친다.

지정 폴더는 '상위' 또는 '상위/하위' 문자열 목록이다. 창 안에서는 두 단계 트리로 보여 준다.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from organizer import valid_folder_name

HELP = "더블클릭: 이름 바꾸기 · 우클릭: 더 많은 기능 · Ctrl/Shift로 여러 개 선택"


# 처음 시작할 때 고를 수 있는 폴더 틀. 업무 기능 기준(기록관리 원칙)으로, 한 단계 10개 이내.
TEMPLATES: dict[str, list[str]] = {
    "직장인": ["업무/회의", "업무/보고", "업무/프로젝트", "인사/급여·연말정산", "인사/교육", "계약·구매",
             "회계/경비·영수증", "참고자료", "개인"],
    "학생": ["수업/강의자료", "수업/과제", "시험·공부", "취업/자기소개서", "취업/자격증", "장학금·행정",
            "동아리·대외활동", "개인"],
    "자영업·소상공인": ["매출·정산", "세금/부가가치세", "세금/종합소득세", "직원/근무표", "직원/급여",
                  "거래처·발주", "계약·임대", "위생·점검", "홍보·메뉴"],
    "프리랜서": ["프로젝트", "견적·계약", "청구·입금", "세금", "포트폴리오", "참고자료"],
    "가정": ["금융·보험", "주거·계약", "의료·건강", "자녀·교육", "차량", "여행", "사진"],
}


LEFTOVER_CHOICES = ("미분류로", "AI가 만든 폴더 그대로")


def targets_to_tree(targets: list[str]) -> list[tuple[str, list[str]]]:
    """['회계/세금', '회계/급여', '인사'] → [('회계', ['세금', '급여']), ('인사', [])]. 순서를 지킨다."""
    tree: dict[str, list[str]] = {}
    for target in targets:
        parent, _, child = target.partition("/")
        children = tree.setdefault(parent, [])
        if child and child not in children:
            children.append(child)
    return list(tree.items())


def folder_structure(root: Path, limit: int = 30) -> list[tuple[str, list[str]]]:
    """폴더 안의 하위 폴더 이름을 두 단계까지 읽는다. 숨김·도구 폴더와 쓸 수 없는 이름은 뺀다."""
    def children(folder: Path) -> list[str]:
        try:
            names = sorted(entry.name for entry in folder.iterdir() if entry.is_dir() and not entry.is_symlink())
        except OSError:
            return []
        return [name for name in names
                if not name.startswith((".", "_", "~", "$")) and valid_folder_name(name)][:limit]

    return [(name, children(root / name)) for name in children(root)]


def tree_to_targets(tree: list[tuple[str, list[str]]]) -> list[str]:
    """트리를 지정 폴더 목록으로. 하위가 있는 상위 폴더는 하위만 남긴다(상위에 바로 들어갈 파일이 없도록)."""
    targets: list[str] = []
    for parent, children in tree:
        targets.extend(f"{parent}/{child}" for child in children) if children else targets.append(parent)
    return list(dict.fromkeys(targets))


class FolderEditor(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        targets: list[str],
        suggestions: list[str],
        counts: dict[str, int],
        allow_var: tk.BooleanVar,
        on_save: Callable[[list[str]], None],
        px: Callable[[int], int] = lambda size: size,
        suggest_groups: Callable[[list[str]], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title("폴더 구조 정하기")
        self.geometry(f"{px(640)}x{px(600)}")
        self.minsize(px(520), px(580))
        self.transient(master)
        self.grab_set()
        self.suggestions, self.counts, self.on_save = suggestions, counts, on_save
        # AI 묶기 제안: 맨 위 폴더 이름을 넘기면, 끝났을 때 앱이 apply_groups({폴더: 상위})를 부른다.
        self.suggest_groups = suggest_groups

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        # tk 변수(StringVar 등)를 창에 두지 않는다. 창이 닫힌 뒤 작업 스레드에서 정리되면 Tcl이 죽는다.
        self.status = ttk.Label(outer)
        self.status.grid(row=0, column=0, columnspan=2, sticky="w")

        self.tree = ttk.Treeview(outer, columns=("count",), show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="폴더")
        self.tree.heading("count", text="들어갈 파일")
        self.tree.column("count", width=px(90), anchor="e", stretch=False)
        self.tree.tag_configure("empty", foreground="#999")
        self.tree.grid(row=1, column=0, sticky="nsew", pady=6)
        self.tree.bind("<Double-1>", lambda _e: self.rename())
        self.tree.bind("<F2>", lambda _e: self.rename())
        self.tree.bind("<Delete>", lambda _e: self.delete())
        self.tree.bind("<Control-a>", lambda _e: self.select_all() or "break")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        self.tree.bind("<Button-3>", self._show_context_menu)

        side = ttk.Frame(outer)
        side.grid(row=1, column=1, sticky="n", padx=(8, 0), pady=6)
        self.buttons: dict[str, ttk.Button] = {}
        # 처음 목록을 채우는 방법 세 가지를 메뉴 하나로 모은다.
        fill = ttk.Menubutton(side, text="목록 채우기")
        menu = tk.Menu(fill, tearoff=0)
        menu.add_command(label="AI가 만든 폴더", command=self.load_suggestions)
        templates = tk.Menu(menu, tearoff=0)
        for name in TEMPLATES:
            templates.add_command(label=name, command=lambda name=name: self.load_template(name))
        menu.add_cascade(label="템플릿", menu=templates)
        menu.add_command(label="내 폴더 구조 가져오기…", command=self.import_structure)
        fill.configure(menu=menu)
        fill.pack(fill="x", pady=(0, 2))
        for text, command in (("추가", self.add), ("하위 추가", self.add_child), ("삭제", self.delete),
                              (None, None), ("▲ 위로", lambda: self.move(-1)), ("▼ 아래로", lambda: self.move(1)),
                              (None, None), ("상위 폴더로 묶기", self.group), ("하나로 합치기", self.merge),
                              ("AI가 묶어 주기", self.ask_groups),
                              (None, None), ("전체 삭제", self.clear)):
            if text is None:
                ttk.Separator(side).pack(fill="x", pady=4)
                continue
            self.buttons[text] = ttk.Button(side, text=text, command=command)
            self.buttons[text].pack(fill="x", pady=2)

        # tk 변수를 새로 만들지 않도록 콤보 상자 값은 저장할 때 읽어 allow_var에 넣는다.
        self.allow_var = allow_var
        leftovers = ttk.Frame(outer)
        leftovers.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(leftovers, text="목록에 안 맞는 파일:").pack(side="left")
        self.leftover = ttk.Combobox(leftovers, state="readonly", width=18, values=LEFTOVER_CHOICES)
        self.leftover.current(1 if allow_var.get() else 0)
        self.leftover.pack(side="left", padx=6)
        # 도움말은 버튼과 한 줄에 두면 창이 좁을 때 '취소'를 가린다.
        ttk.Label(outer, text=HELP, foreground="#666").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        bottom = ttk.Frame(outer)
        bottom.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="저장", command=self.save).pack(side="right")
        ttk.Button(bottom, text="취소", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Escape>", lambda _e: self.destroy())

        self.fill(targets_to_tree(targets) if targets else [(name, []) for name in suggestions])

    # ---------------------------------------------------------------- 트리 읽고 쓰기
    def fill(self, tree: list[tuple[str, list[str]]]) -> None:
        self.tree.delete(*self.tree.get_children())
        for parent, children in tree:
            item = self._insert("", parent)
            for child in children:
                self._insert(item, child)
            self.tree.item(item, open=True)
        self._update_buttons()

    def _insert(self, parent: str, name: str, index: str | int = "end") -> str:
        path = f"{self.tree.item(parent, 'text')}/{name}" if parent else name
        count = self.counts.get(path, self.counts.get(name, 0))
        return self.tree.insert(parent, index, text=name, values=(f"{count}개",), tags=() if count else ("empty",))

    def current(self) -> list[tuple[str, list[str]]]:
        return [(self.tree.item(item, "text"), [self.tree.item(child, "text") for child in self.tree.get_children(item)])
                for item in self.tree.get_children()]

    def _names(self, parent: str) -> set[str]:
        return {self.tree.item(item, "text") for item in self.tree.get_children(parent)}

    def _ask(self, title: str, initial: str = "", taken: set[str] = frozenset()) -> str | None:
        while True:
            name = simpledialog.askstring(title, "폴더 이름", initialvalue=initial, parent=self)
            if name is None:
                return None
            name = " ".join(name.split())
            if not valid_folder_name(name) or "/" in name or name in {"미분류", "기타"}:
                messagebox.showerror(title, "사용 불가 이름", parent=self)
            elif name in taken:
                messagebox.showerror(title, f"'{name}' 이미 있음", parent=self)
            else:
                return name

    def _update_buttons(self) -> None:
        selected = self.tree.selection()
        tops = [item for item in selected if not self.tree.parent(item)]
        same_level = len({self.tree.parent(item) for item in selected}) == 1
        enable = {
            "하위 추가": len(selected) == 1 and bool(tops),
            "삭제": bool(selected),
            "전체 삭제": bool(self.tree.get_children()),
            "▲ 위로": len(selected) == 1, "▼ 아래로": len(selected) == 1,
            "상위 폴더로 묶기": bool(tops) and len(tops) == len(selected)
            and not any(self.tree.get_children(item) for item in tops),
            "하나로 합치기": len(selected) >= 2 and same_level,
            "AI가 묶어 주기": self.suggest_groups is not None
            and sum(not self.tree.get_children(item) for item in self.tree.get_children()) >= 3,
        }
        for text, on in enable.items():
            self.buttons[text].configure(state="normal" if on else "disabled")
        total = len(tree_to_targets(self.current()))
        self.status.configure(text=f"지정 폴더 {total}개" if total else "비어 있음 · 저장하면 자동 분류")

    # ---------------------------------------------------------------- 동작
    def add(self) -> None:
        name = self._ask("추가", taken=self._names(""))
        if name:
            self.tree.selection_set(self._insert("", name))

    def add_child(self) -> None:
        parent = self.tree.selection()[0]
        name = self._ask("하위 추가", taken=self._names(parent))
        if name:
            self.tree.item(parent, open=True)
            self.tree.selection_set(self._insert(parent, name))

    def rename(self) -> None:
        selected = self.tree.selection()
        if len(selected) != 1:
            return
        item = selected[0]
        old = self.tree.item(item, "text")
        name = self._ask("이름 바꾸기", old, self._names(self.tree.parent(item)) - {old})
        if name:
            self.tree.item(item, text=name)
            self._update_buttons()

    def _show_context_menu(self, event: tk.Event) -> str:
        """우클릭: 버튼에 없는 동작까지 모두. 누른 줄이 선택에 없으면 그 줄만 고른다."""
        item = self.tree.identify_row(event.y)
        if item and item not in self.tree.selection():
            self.tree.selection_set(item)
        self._update_buttons()
        menu = tk.Menu(self, tearoff=0)
        for label, command, key in (("이름 바꾸기", self.rename, "이름 바꾸기"), ("하위 추가", self.add_child, "하위 추가"),
                                    ("상위 폴더로 묶기", self.group, "상위 폴더로 묶기"),
                                    ("하나로 합치기", self.merge, "하나로 합치기"), (None, None, None),
                                    ("전체 선택", self.select_all, None), ("삭제", self.delete, "삭제")):
            if label is None:
                menu.add_separator()
                continue
            enabled = (len(self.tree.selection()) == 1 if key == "이름 바꾸기"
                       else str(self.buttons[key].cget("state")) == "normal" if key else bool(self.tree.get_children()))
            menu.add_command(label=label, command=command, state="normal" if enabled else "disabled")
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def select_all(self) -> None:
        """맨 위 폴더를 모두 고른다(하위 폴더는 맨 위 폴더와 함께 지워진다)."""
        self.tree.selection_set(self.tree.get_children())

    def delete(self) -> None:
        """선택 삭제. 2개 이상이거나 하위 폴더가 딸려 있으면 개수를 보여 주고 묻는다."""
        selected = [item for item in self.tree.selection() if self.tree.exists(item)]
        # 상위와 그 하위를 함께 골랐으면 상위만 센다.
        selected = [item for item in selected if self.tree.parent(item) not in selected]
        if not selected:
            return
        nested = sum(len(self.tree.get_children(item)) for item in selected)
        if (len(selected) > 1 or nested) and not messagebox.askyesno(
                "선택 삭제", f"폴더 {len(selected)}개" + (f" (하위 {nested}개 포함)" if nested else "") + " 삭제?",
                parent=self):
            return
        self.tree.delete(*selected)
        self._update_buttons()

    def move(self, step: int) -> None:
        item = self.tree.selection()[0]
        parent = self.tree.parent(item)
        index = max(0, self.tree.index(item) + step)
        self.tree.move(item, parent, index)
        self.tree.see(item)

    def group(self) -> None:
        """고른 맨 위 폴더들을 새(또는 같은 이름의) 상위 폴더 아래로 넣는다."""
        selected = list(self.tree.selection())
        existing = {self.tree.item(item, "text"): item for item in self.tree.get_children()}
        name = simpledialog.askstring("묶기", "상위 폴더 이름 (있는 이름이면 그 아래로)", parent=self)
        name = " ".join((name or "").split())
        if not name:
            return
        if not valid_folder_name(name) or "/" in name:
            messagebox.showerror("묶기", "사용 불가 이름", parent=self)
            return
        parent = existing.get(name)
        if parent in selected:
            selected.remove(parent)
        if parent is None:
            parent = self._insert("", name, self.tree.index(selected[0]))
        taken = self._names(parent)
        for item in selected:
            child = self.tree.item(item, "text")
            if child not in taken:
                self._insert(parent, child)
                taken.add(child)
            self.tree.delete(item)
        self.tree.item(parent, open=True)
        self.tree.selection_set(parent)

    def merge(self) -> None:
        """같은 단계의 폴더 여러 개를 하나로. 하위 폴더는 모두 남은 폴더로 옮긴다."""
        selected = list(self.tree.selection())
        keep = selected[0]
        parent = self.tree.parent(keep)
        first = self.tree.item(keep, "text")
        name = self._ask("합치기", first, self._names(parent) - {self.tree.item(item, "text") for item in selected})
        if not name:
            return
        total = sum(int(str(self.tree.item(item, "values")[0]).rstrip("개") or 0) for item in selected)
        taken = self._names(keep)
        for item in selected[1:]:
            for child in self.tree.get_children(item):
                text = self.tree.item(child, "text")
                if text not in taken:
                    self.tree.move(child, keep, "end")
                    taken.add(text)
            self.tree.delete(item)
        self.tree.item(keep, text=name, values=(f"{total}개",), tags=() if total else ("empty",), open=True)
        self.tree.selection_set(keep)

    def ask_groups(self) -> None:
        """하위가 없는 맨 위 폴더들을 AI에게 보내 상위 폴더로 묶는 안을 받는다(대분류로 묶기와 같은 방식)."""
        names = [self.tree.item(item, "text") for item in self.tree.get_children() if not self.tree.get_children(item)]
        self.buttons["AI가 묶어 주기"].configure(state="disabled")
        self.status.configure(text=f"AI가 폴더 {len(names)}개를 묶는 중…")
        self.suggest_groups(names)

    def apply_groups(self, plan: dict[str, str], error: str = "") -> None:
        """{폴더: 상위} 안을 트리에 넣는다. 안에 없는 폴더와 이미 하위가 있는 폴더는 그대로 둔다."""
        if not self.winfo_exists():
            return
        if error or not plan:
            self._update_buttons()
            self.status.configure(text=error or "묶을 만한 상위 폴더 없음 · 그대로")
            return
        tree: list[tuple[str, list[str]]] = []
        index: dict[str, list[str]] = {}
        for parent, children in self.current():
            category = plan.get(parent) if not children else None
            if category is None:
                tree.append((parent, children))
                continue
            if category not in index:
                index[category] = []
                tree.append((category, index[category]))
            index[category].append(parent)
        self.fill(tree)
        self.status.configure(text=f"상위 {len(index)}개로 묶음 · 고친 뒤 저장")

    def import_structure(self, root: Path | None = None) -> None:
        """이미 잘 정리된 폴더(예: 회사 공용 폴더)를 고르면 그 하위 폴더 이름을 그대로 가져온다."""
        if root is None:
            name = filedialog.askdirectory(title="구조를 가져올 폴더 선택", parent=self)
            if not name:
                return
            root = Path(name)
        tree = folder_structure(root)
        if not tree:
            messagebox.showinfo("폴더 구조 불러오기", "하위 폴더 없음", parent=self)
            return
        if self.tree.get_children() and not messagebox.askyesno(
                "폴더 구조 불러오기", f"지금 목록을 '{root.name}'의 폴더 {len(tree)}개로 바꿀까요?", parent=self):
            return
        self.fill(tree)
        self.status.configure(text=f"'{root.name}' 구조 · 고친 뒤 저장")

    def load_template(self, name: str) -> None:
        if self.tree.get_children() and not messagebox.askyesno(
                "템플릿", f"지금 목록을 '{name}' 템플릿으로 바꿀까요?", parent=self):
            return
        self.fill(targets_to_tree(TEMPLATES[name]))
        self.status.configure(text=f"'{name}' 템플릿 · 고친 뒤 저장")

    def load_suggestions(self) -> None:
        if self.tree.get_children() and not messagebox.askyesno("AI 제안 불러오기", "지금 목록을 AI 제안으로 바꿀까요?",
                                                                parent=self):
            return
        self.fill([(name, []) for name in self.suggestions])

    def clear(self) -> None:
        """전체 삭제. 저장하면 지정 폴더 없이 자동 분류로 돌아간다."""
        if self.tree.get_children() and messagebox.askyesno(
                "전체 삭제", f"폴더 {len(tree_to_targets(self.current()))}개 모두 삭제?\n(저장하면 자동 분류)",
                parent=self):
            self.fill([])

    def save(self) -> None:
        self.allow_var.set(self.leftover.current() == 1)
        targets = tree_to_targets(self.current())
        self.destroy()
        self.on_save(targets)
