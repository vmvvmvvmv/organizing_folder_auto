"""실제 Tk 창에 합성 이벤트를 보내 끌기·이름 변경 동작을 확인한다. 화면이 없으면 건너뛴다."""

import tkinter as tk
import unittest
from pathlib import Path
from tkinter import messagebox
from unittest import mock

from app import OrganizerApp
from organizer import Document


class GuiInteractionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.app = OrganizerApp()
        except tk.TclError as exc:
            self.skipTest(f"화면이 없어 GUI 테스트를 건너뜁니다: {exc}")
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
                mock.patch("app.ollama_missing_models", return_value=[]):
            self.app.toggle_top_level()
            deadline = time.monotonic() + 10
            while self.app.busy and time.monotonic() < deadline:
                self.app.update()
                time.sleep(0.05)
            self.app.update()
        tops = [self.table.item(row, "text") for row in self.table.get_children("")]
        self.assertEqual(tops, ["짝수 주제  (24개)", "홀수 주제  (24개)"])
        self.assertEqual(self.app.group_button.cget("text"), "대분류 풀기")
        self.assertIn("구조 점수", self.app.status_var.get())
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
