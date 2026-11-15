"""실제 Tk 창에 합성 이벤트를 보내 끌기·이름 변경 동작을 확인한다. 화면이 없으면 건너뛴다."""

import gc
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import messagebox
from unittest import mock

import settings
from app import OrganizerApp
from organizer import Document


class GuiInteractionTests(unittest.TestCase):
    def setUp(self):
        try:
            tk.Tk().destroy()
        except tk.TclError as exc:
            self.skipTest(f"화면이 없어 GUI 테스트를 건너뜁니다: {exc}")
        # 앱 창을 만들다 나는 오류는 건너뛰지 않고 실패로 보여야 한다(실행 즉시 꺼지는 오류를 놓쳤다).
        with mock.patch("app.load_excludes", return_value=[]), \
                mock.patch("app.load_user_folders", return_value=([], False)):
            self.app = OrganizerApp()
        # 창을 닫은 뒤 tk 객체를 이 스레드에서 정리한다. 다음 테스트의 작업 스레드에서 정리되면 Tcl이 죽는다.
        self.addCleanup(gc.collect)
        self.addCleanup(self.app.destroy)
        self.app.geometry("1060x400+40+40")
        self.app.documents = [
            Document(Path(f"C:/root/file{index:02}.pdf"), "출장" if index < 30 else "회의", "", "PDF 텍스트")
            for index in range(40)
        ]
        self.app.refresh_table()
        self.app.set_all_folders_open(True)
        self.app.update()
        self.table = self.app.table

    def point(self, item):
        self.table.see(item)
        self.app.update()
        x, y, _, height = self.table.bbox(item, "#0")
        return x + 60, y + height // 2

    def send(self, kind, x, y, **options):
        self.table.event_generate(
            kind, x=x, y=y, rootx=self.table.winfo_rootx() + x,
            rooty=self.table.winfo_rooty() + y, **options,
        )
        self.app.update()

    def test_dragging_multi_selection_keeps_it_and_moves_every_file(self):
        self.table.selection_set(("file:31", "file:32", "file:33"))
        x, y = self.point("file:32")
        self.send("<ButtonPress-1>", x, y)
        self.assertEqual(self.table.selection(), ("file:31", "file:32", "file:33"))
        target = next(row for row, route in self.app.folder_rows.items() if route == ("출장",))
        tx, ty = self.point(target)
        self.send("<B1-Motion>", tx, ty, state=0x100)
        self.send("<ButtonRelease-1>", tx, ty, state=0x100)
        self.assertEqual([self.app.documents[index].folder for index in (31, 32, 33)], ["출장"] * 3)

    def test_click_without_drag_selects_only_pressed_row(self):
        self.table.selection_set(("file:1", "file:2"))
        x, y = self.point("file:2")
        self.send("<ButtonPress-1>", x, y)
        self.send("<ButtonRelease-1>", x, y, state=0x100)
        self.assertEqual(self.table.selection(), ("file:2",))

    def test_holding_pointer_at_bottom_edge_keeps_scrolling(self):
        self.table.yview_moveto(0)
        x, y = self.point("file:0")
        self.send("<ButtonPress-1>", x, y)
        edge = self.table.winfo_height() - 10
        self.send("<B1-Motion>", x, edge, state=0x100)
        first = self.table.yview()[0]
        for _ in range(6):
            self.app.after(70)
            self.app.update()
        self.assertGreater(self.table.yview()[0], first)
        self.send("<ButtonRelease-1>", x, edge, state=0x100)
        self.assertIsNone(self.app.drag_scroll_job)

    def test_rename_entry_follows_its_row_when_scrolling(self):
        self.table.yview_moveto(0)
        self.app.update()
        self.app.begin_rename("file:3")
        self.app.update()
        self.table.yview_scroll(2, "units")
        self.app.update()
        self.assertEqual(self.app.rename_entry.winfo_y(), self.table.bbox("file:3", "#0")[1])
        self.table.yview_moveto(1.0)
        self.app.update()
        self.assertFalse(self.app.rename_entry.winfo_ismapped())

    def test_undo_button_restores_files_from_chosen_report(self):
        import tempfile
        import time
        from organizer import ResultReport, move_documents, scan

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            destination = Path(folder) / "result"
            (source / "a").mkdir(parents=True)
            destination.mkdir()
            (source / "a" / "메모.xyz").write_bytes(b"memo")
            documents = scan(source, destination, None)
            documents[0].folder = "자료"
            with ResultReport(source, destination, documents) as report:
                move_documents(documents, destination, source_root=source, record=report.record)
            self.assertFalse((source / "a" / "메모.xyz").exists())
            with mock.patch("app.filedialog.askopenfilename", return_value=str(report.path)), \
                    mock.patch.object(messagebox, "askyesno", return_value=True) as ask, \
                    mock.patch.object(messagebox, "showinfo") as done:
                self.app.start_undo()
                deadline = time.monotonic() + 10
                while self.app.busy and time.monotonic() < deadline:
                    self.app.update()
                    time.sleep(0.05)
                self.app.update()
            self.assertIn("1개 파일", ask.call_args.args[1])
            done.assert_called_once()
            self.assertEqual((source / "a" / "메모.xyz").read_bytes(), b"memo")
            self.assertIn("1개 복원", self.app.status_var.get())

    def test_group_button_adds_top_level_and_ungroups_back(self):
        import time
        self.app.documents = [
            Document(Path(f"C:/root/주제{topic:02d}_{index}.pdf"), f"주제{topic:02d}", "AI 분류: 이유", "PDF 텍스트")
            for topic in range(16) for index in range(3)
        ]
        self.app.refresh_table()
        self.app.cache_var.set(False)  # 테스트가 사용자 프로필의 저장소를 건드리지 않게 한다
        before = [(doc.folder, doc.subfolder) for doc in self.app.documents]

        def fake_group(documents, **_calls):
            plan = {f"주제{topic:02d}": "짝수 주제" if topic % 2 == 0 else "홀수 주제" for topic in range(16)}
            for doc in documents:
                doc.folder, doc.subfolder = plan[doc.folder], doc.folder
            return plan

        with mock.patch("app.add_top_level", side_effect=fake_group), \
                mock.patch("app.ollama_unload") as unload, \
                mock.patch("app.ollama_missing_models", return_value=[]):
            self.app.toggle_top_level()
            deadline = time.monotonic() + 10
            while self.app.busy and time.monotonic() < deadline:
                self.app.update()
                time.sleep(0.05)
            self.app.update()
            deadline = time.monotonic() + 5
            while not unload.called and time.monotonic() < deadline:
                time.sleep(0.02)
        tops = [self.table.item(row, "text") for row in self.table.get_children("")]
        self.assertEqual(tops, ["짝수 주제  (24개)", "홀수 주제  (24개)"])
        unload.assert_called_once()  # 끝나면 모델을 메모리에서 내린다
        self.assertEqual(self.app.group_button.cget("text"), "대분류 풀기")
        self.assertIn("대분류 2개", self.app.status_var.get())
        with mock.patch.object(messagebox, "askyesno", return_value=True):
            self.app.toggle_top_level()
        self.assertEqual([(doc.folder, doc.subfolder) for doc in self.app.documents], before)
        self.assertEqual(self.app.group_button.cget("text"), "대분류로 묶기")

    def test_group_button_explains_when_there_are_few_topics(self):
        with mock.patch.object(messagebox, "showinfo") as info:
            self.app.toggle_top_level()  # 기본 데이터는 주제 폴더가 2개
        self.assertIn("이미 한눈에", info.call_args.args[1])
        self.assertFalse(self.app.busy)

    def test_three_level_tree_and_drop_into_detail_folder(self):
        docs = self.app.documents
        docs[0].folder, docs[0].subfolder, docs[0].detail = "채용", "면접", "1차"
        docs[1].folder, docs[1].subfolder = "채용", "면접"
        self.app.refresh_table()
        paths = set(self.app.folder_rows.values())
        self.assertTrue({("채용",), ("채용", "면접"), ("채용", "면접", "1차")} <= paths)
        detail_row = next(row for row, path in self.app.folder_rows.items() if path == ("채용", "면접", "1차"))
        self.assertTrue(self.table.item(detail_row, "text").startswith("1차  (1개)"))
        self.table.selection_set(("file:5",))
        x, y = self.point("file:5")
        self.send("<ButtonPress-1>", x, y)
        tx, ty = self.point(detail_row)
        self.send("<B1-Motion>", tx, ty, state=0x100)
        self.send("<ButtonRelease-1>", tx, ty, state=0x100)
        moved = self.app.documents[5]
        self.assertEqual((moved.folder, moved.subfolder, moved.detail), ("채용", "면접", "1차"))

    def test_fresh_button_reuses_reading_but_asks_ai_again(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            source.mkdir()
            (source / "메모.txt").write_text("출장 결과 메모", encoding="utf-8")
            self.app.source_var.set(str(source))
            self.app.destination_var.set(str(Path(folder) / "result"))
            self.app.ai_var.set(False)
            seen = {}

            def fake_preview(documents, enabled, calls=None, **_options):
                seen["enabled"] = enabled
                return "AI 분류 0개."

            with mock.patch("app.default_cache_path", return_value=Path(folder) / "cache.sqlite3"),                     mock.patch("app.ollama_missing_models", return_value=[]),                     mock.patch("app.ollama_unload"),                     mock.patch("app.ai_preview_note", side_effect=fake_preview),                     mock.patch("app.LocalCache.responses", autospec=True,
                               side_effect=lambda cache, fresh=False: seen.setdefault("store_fresh", fresh) or {}):
                self.assertEqual(self.app.fresh_button.cget("text"), "AI에게 새로 묻기")
                self.app.fresh_button.invoke()
                deadline = time.monotonic() + 10
                while self.app.busy and time.monotonic() < deadline:
                    self.app.update()
                    time.sleep(0.05)
                self.app.update()
            self.assertTrue(self.app.ai_var.get())  # AI 분류를 켠다
            self.assertTrue(seen["enabled"])
            self.assertTrue(seen["store_fresh"])  # 저장된 AI 답을 쓰지 않는 저장소로 물었다
            self.assertIn("AI 새로 물음", self.app.status_var.get())

    def open_editor(self):
        docs = self.app.documents
        self.app.ai_snapshot = [(doc.folder, doc.subfolder, doc.detail, doc.folder_reason) for doc in docs]
        self.app.user_folders = []
        self.app.edit_user_folders()
        editor = self.app.folder_editor
        self.addCleanup(lambda: editor.winfo_exists() and editor.destroy())
        return editor, editor.tree

    def test_folder_editor_is_prefilled_with_ai_folders_and_counts(self):
        editor, tree = self.open_editor()
        rows = [(tree.item(item, "text"), tree.item(item, "values")[0]) for item in tree.get_children()]
        self.assertEqual(rows, [("출장", "30개"), ("회의", "10개")])

    def test_folder_editor_group_merge_rename_delete(self):
        editor, tree = self.open_editor()
        travel, meeting = tree.get_children()
        tree.selection_set((travel, meeting))
        with mock.patch("folder_editor.simpledialog.askstring", return_value="업무"):
            editor.group()                                      # 두 폴더를 '업무' 아래로
        self.assertEqual(editor.current(), [("업무", ["출장", "회의"])])
        top = tree.get_children()[0]
        travel, meeting = tree.get_children(top)
        tree.selection_set((travel, meeting))
        with mock.patch("folder_editor.simpledialog.askstring", return_value="출장·회의"):
            editor.merge()                                      # 하위 둘을 하나로
        self.assertEqual(editor.current(), [("업무", ["출장·회의"])])
        self.assertEqual(tree.item(tree.get_children(top)[0], "values")[0], "40개")
        tree.selection_set((top,))
        with mock.patch("folder_editor.simpledialog.askstring", return_value="회사 업무"):
            editor.rename()
        with mock.patch("folder_editor.simpledialog.askstring", return_value="개인"):
            editor.add()
        self.assertEqual(editor.current(), [("회사 업무", ["출장·회의"]), ("개인", [])])
        tree.selection_set((tree.get_children()[1],))
        editor.delete()
        self.assertEqual(editor.current(), [("회사 업무", ["출장·회의"])])

    def test_folder_editor_select_delete_and_delete_all(self):
        editor, tree = self.open_editor()
        editor.fill([("업무", ["출장", "회의"]), ("개인", []), ("학교", [])])
        work, personal, school = tree.get_children()
        tree.selection_set((personal,))
        editor.delete()                                          # 한 개는 묻지 않고 지운다
        self.assertEqual([name for name, _ in editor.current()], ["업무", "학교"])
        tree.selection_set((work, school))
        with mock.patch("folder_editor.messagebox.askyesno", return_value=False) as ask:
            editor.delete()                                      # 여러 개는 개수를 보여 주고 묻는다
        self.assertIn("폴더 2개 (하위 2개 포함)", ask.call_args.args[1])
        self.assertEqual(len(editor.current()), 2)               # 취소하면 그대로
        editor.select_all()
        self.assertEqual(set(tree.selection()), set(tree.get_children()))
        with mock.patch("folder_editor.messagebox.askyesno", return_value=True) as ask:
            editor.clear()
        self.assertIn("폴더 3개 모두 삭제", ask.call_args.args[1])  # 업무/출장, 업무/회의, 학교
        self.assertEqual(editor.current(), [])
        self.assertEqual(str(editor.buttons["전체 삭제"].cget("state")), "disabled")

    def test_folder_editor_save_refits_preview(self):
        import time
        editor, tree = self.open_editor()
        editor.fill([("업무", ["출장", "회의"])])
        with mock.patch("app.save_user_folders"), mock.patch("app.ollama_unload"), \
                mock.patch("app.LocalCache"), mock.patch("app.cached_hierarchy") as hierarchy:
            hierarchy.return_value = {"user_pick": lambda items, cats: ["업무/출장"] * len(items)}
            editor.save()
            deadline = time.monotonic() + 10
            while self.app.busy and time.monotonic() < deadline:
                self.app.update()
                time.sleep(0.05)
            self.app.update()
        self.assertEqual(self.app.user_folders, ["업무/출장", "업무/회의"])
        self.assertEqual({(d.folder, d.subfolder) for d in self.app.documents[:30]}, {("업무", "출장")})
        self.assertEqual({(d.folder, d.subfolder) for d in self.app.documents[30:]}, {("업무", "회의")})

    def test_status_is_one_line_with_details_and_empty_list_shows_steps(self):
        self.app.status_var.set(self.app.preview_summary())
        status = self.app.status_var.get()
        self.assertNotIn("\n", status)
        self.assertIn("파일 40", status)
        self.assertNotIn("구조 점수", status)                  # 기록관리 용어는 [자세히]로
        with mock.patch.object(messagebox, "showinfo") as info:
            self.app.detail_button.invoke()
        self.assertIn("구조 점수", info.call_args.args[1])      # 자세한 점검 항목은 '자세히'에
        self.assertFalse(self.app.empty_hint.winfo_ismapped())
        self.app.documents = []
        self.app.refresh_table()
        self.app.update()
        self.assertTrue(self.app.empty_hint.winfo_ismapped())   # 빈 목록에는 순서 안내

    def test_preview_saved_on_close_can_be_opened_again(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "원본"
            source.mkdir()
            self.app.preview_paths = (source, Path(folder) / "결과")
            self.app.documents[0].folder = "직접 고친 폴더"
            with mock.patch("settings.default_cache_path", return_value=Path(folder) / "cache.sqlite3"):
                self.app.autosave_preview()
                self.app.documents = []
                self.app.refresh_table()
                self.app.open_preview(settings.last_preview_path())
            self.assertEqual(len(self.app.documents), 40)
            self.assertEqual(self.app.documents[0].folder, "직접 고친 폴더")
            self.assertEqual(self.app.source_var.get(), str(source))
            self.assertIn("미리보기 불러옴", self.app.status_var.get())
            self.assertEqual(str(self.app.move_button.cget("state")), "normal")   # 바로 정리할 수 있다

    def test_refit_warns_before_dropping_manual_edits(self):
        docs = self.app.documents
        self.app.ai_snapshot = [(doc.folder, doc.subfolder, doc.detail, doc.folder_reason) for doc in docs]
        self.app.remember_routes()
        docs[0].folder = docs[1].folder = "직접 옮김"
        self.assertEqual(self.app.manual_edit_count(), 2)
        self.app.user_folders = ["업무"]
        with mock.patch.object(messagebox, "askyesno", return_value=False) as ask:
            self.app.apply_user_folders()
        self.assertIn("직접 고친 파일 2개", ask.call_args.args[1])
        self.assertEqual(docs[0].folder, "직접 옮김")          # 취소하면 그대로
        self.assertFalse(self.app.busy)

    def test_folder_editor_ai_group_suggestion(self):
        import time
        editor, tree = self.open_editor()
        editor.fill([("출장", []), ("회의", []), ("세금", []), ("개인", ["운동"])])
        seen = {}

        def fake_top(documents, min_topics, **calls):
            seen["topics"] = sorted({doc.folder for doc in documents})
            seen["calls"] = sorted(calls)
            return {"출장": "업무", "회의": "업무"}

        with mock.patch("app.add_top_level", side_effect=fake_top), mock.patch("app.ollama_unload"), \
                mock.patch("app.ollama_missing_models", return_value=[]), mock.patch("app.LocalCache"), \
                mock.patch("app.cached_hierarchy", return_value={"taxonomy": 1, "pick": 2, "embed": 3, "user_pick": 4}):
            editor.buttons["AI가 묶어 주기"].invoke()
            deadline = time.monotonic() + 10
            while "묶음" not in editor.status.cget("text") and time.monotonic() < deadline:
                self.app.update()
                time.sleep(0.05)
        self.assertEqual(seen["topics"], ["세금", "출장", "회의"])        # 하위가 있는 폴더는 보내지 않음
        self.assertEqual(seen["calls"], ["embed", "pick", "taxonomy"])
        self.assertEqual(editor.current(), [("업무", ["출장", "회의"]), ("세금", []), ("개인", ["운동"])])
        self.assertFalse(self.app.group_button.winfo_ismapped())          # 메인 화면에서는 버튼을 뺐다

    def test_search_box_filters_list_and_escape_clears(self):
        self.app.documents[35].output_name = "특별한 계약서.pdf"
        self.app.focus_search()
        self.app.search_var.set("특별한")
        self.app._run_search()
        self.assertEqual(self.app.count_var.get(), "1개 폴더 · 1/40개 파일")
        self.assertTrue(self.table.exists("file:35"))
        self.app.search_entry.focus_force()
        self.app.update()
        self.app.search_entry.event_generate("<KeyPress-Escape>", when="now")
        self.app.update()
        self.assertEqual(self.app.search_var.get(), "")
        self.assertIn("40/40", self.app.count_var.get())

    def test_ctrl_z_undoes_manual_edits_in_order(self):
        self.app.remember_routes()
        self.assertTrue(self.rename_folder_to("회의", "회의록", answer=True) is False)   # 1) 이름 바꾸기
        self.table.selection_set(("file:0",))
        with mock.patch("app.simpledialog.askstring", return_value="새 폴더"):
            self.app.create_folder_for_selection()                                      # 2) 새 폴더로
        self.assertEqual(self.app.documents[0].folder, "새 폴더")
        self.app.undo_edit()
        self.assertEqual(self.app.documents[0].folder, "출장")
        self.assertEqual(self.app.documents[35].folder, "회의록")
        self.table.focus_force()
        self.app.update()
        self.table.event_generate("<Control-z>", when="now")                            # 목록에서 Ctrl+Z
        self.app.update()
        self.assertEqual(self.app.documents[35].folder, "회의")
        self.app.undo_edit()
        self.assertEqual(self.app.status_var.get(), "되돌릴 수정 없음")

    def test_folder_editor_templates(self):
        from folder_editor import TEMPLATES, tree_to_targets
        editor, tree = self.open_editor()
        with mock.patch("folder_editor.messagebox.askyesno", return_value=True):
            editor.load_template("직장인")
        self.assertEqual(tree_to_targets(editor.current()), TEMPLATES["직장인"])
        for name, targets in TEMPLATES.items():
            self.assertLessEqual(len({t.split("/")[0] for t in targets}), 10, name)   # 한 단계 10개 이내

    def test_folder_editor_imports_existing_structure(self):
        import tempfile
        editor, tree = self.open_editor()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "회사 공용"
            for rel in ("경영지원/인사", "경영지원/회계", "영업/계약", ".git/objects", "_old", "홍보"):
                (root / rel).mkdir(parents=True)
            (root / "메모.txt").write_text("x", encoding="utf-8")
            with mock.patch("folder_editor.messagebox.askyesno", return_value=True):
                editor.import_structure(root)
        self.assertEqual(editor.current(), [("경영지원", ["인사", "회계"]), ("영업", ["계약"]), ("홍보", [])])

    def test_toolbar_shows_steps_and_undo_and_settings_window(self):
        labels = [self.app.scan_button.cget("text"), self.app.folders_button.cget("text"),
                  self.app.move_button.cget("text")]
        self.assertEqual([label[0] for label in labels], ["①", "②", "③"])
        self.assertTrue(self.app.undo_button.winfo_ismapped())            # 정리 되돌리기는 늘 보인다
        self.app.open_settings()
        window = self.app.winfo_children()[-1]
        self.addCleanup(window.destroy)
        texts = [w.cget("text") for w in window.winfo_children()[0].winfo_children() if w.winfo_class() == "TCheckbutton"]
        self.assertEqual(texts, ["읽은 내용 기억 (다음 미리보기가 빨라짐)",
                                 "다른 작업 우선 (분류가 느려지는 대신 PC가 덜 버벅임)"])

    def test_folder_editor_has_few_buttons_and_leftover_choice(self):
        editor, tree = self.open_editor()
        self.assertLessEqual(len(editor.buttons), 9)
        editor.leftover.current(1)
        with mock.patch("app.save_user_folders"), mock.patch.object(self.app, "apply_user_folders"):
            editor.save()
        self.assertTrue(self.app.allow_new_folders)                     # 'AI가 만든 폴더 그대로'

    def rename_folder_to(self, old, new, answer):
        row = next(item for item, route in self.app.folder_rows.items() if route == (old,))
        with mock.patch.object(messagebox, "askyesno", return_value=answer) as ask:
            self.app.begin_rename(row)
            self.app.rename_entry.delete(0, "end")
            self.app.rename_entry.insert(0, new)
            self.app.finish_rename()
        return ask.called

    def test_rename_to_existing_folder_asks_before_merging(self):
        self.assertTrue(self.rename_folder_to("회의", "출장", answer=False))
        self.assertEqual({doc.folder for doc in self.app.documents}, {"출장", "회의"})
        self.assertTrue(self.rename_folder_to("회의", "출장", answer=True))
        self.assertEqual({doc.folder for doc in self.app.documents}, {"출장"})

    def test_rename_to_new_name_does_not_ask(self):
        self.assertFalse(self.rename_folder_to("회의", "회의록", answer=True))
        self.assertEqual({doc.folder for doc in self.app.documents}, {"출장", "회의록"})


if __name__ == "__main__":
    unittest.main()
