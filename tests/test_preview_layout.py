import unittest
from pathlib import Path
from unittest import mock

from app import OrganizerApp, drop_folder_row, group_preview_documents, preview_location, selected_file_indices
from organizer import Document


class PreviewLayoutTests(unittest.TestCase):
    def setUp(self):
        self.documents = [
            Document(Path("root/a/출장 결과.pdf"), "출장", "방문", "PDF 텍스트"),
            Document(Path("root/b/출장 계획.pdf"), "출장", "계획", "PDF 텍스트"),
            Document(Path("root/자료.pdf"), "미분류", "단서 부족", "PDF 텍스트"),
        ]

    def test_grouping_shows_folder_counts_without_repeating_folder_per_file(self):
        groups = group_preview_documents(self.documents, "전체")
        self.assertEqual(groups, {("출장",): [0, 1], ("미분류",): [2]})
        self.assertEqual(group_preview_documents(self.documents, "미분류"), {("미분류",): [2]})

    def test_second_level_grouping_keeps_top_level_files(self):
        self.documents[0].subfolder = "2017"
        self.documents[1].subfolder = "2018"
        groups = group_preview_documents(self.documents, "전체")
        self.assertEqual(groups[("출장", "2017")], [0])
        self.assertEqual(groups[("출장", "2018")], [1])
        self.assertEqual(groups[("미분류",)], [2])

    def test_third_level_groups_files_under_detail_folder(self):
        self.documents[0].subfolder, self.documents[0].detail = "국내 출장", "결과 보고"
        self.documents[1].subfolder = "국내 출장"
        groups = group_preview_documents(self.documents, "전체")
        self.assertEqual(groups[("출장", "국내 출장", "결과 보고")], [0])
        self.assertEqual(groups[("출장", "국내 출장")], [1])
    def test_file_location_is_relative_and_full_path_remains_in_document(self):
        self.assertEqual(preview_location(Path("root"), self.documents[0].source), "a")
        self.assertEqual(preview_location(Path("root"), self.documents[2].source), "원본 폴더")
        self.assertEqual(str(self.documents[0].source), "root\\a\\출장 결과.pdf"
                         if __import__("os").name == "nt" else "root/a/출장 결과.pdf")

    def test_drag_press_keeps_selected_files_or_uses_pressed_file(self):
        handler = getattr(OrganizerApp, "on_drag_press", lambda app, event: None)
        surface = mock.Mock()
        surface.busy = False
        surface.table.identify_row.return_value = "file:2"
        surface.table.selection.return_value = ("file:1", "file:2", "folder:0")
        # 다중 선택을 누르면 기본 선택 변경을 막아 끄는 동안 선택이 그대로 보인다.
        self.assertEqual(handler(surface, mock.Mock(x=40, y=50, state=0)), "break")
        self.assertEqual(getattr(surface, "drag_indices", None), [1, 2])
        self.assertEqual(surface.drag_click_item, "file:2")
        # Ctrl/Shift 클릭은 Treeview 기본 선택 동작에 맡긴다.
        self.assertIsNone(handler(surface, mock.Mock(x=40, y=50, state=0x0004)))
        surface.table.selection.return_value = ("file:1",)
        self.assertIsNone(handler(surface, mock.Mock(x=40, y=50, state=0)))
        self.assertEqual(getattr(surface, "drag_indices", None), [2])

    def test_drop_onto_file_row_uses_its_containing_subfolder(self):
        table = mock.Mock()
        parents = {"file:2": "sub:0:1", "sub:0:1": "folder:0", "folder:0": ""}
        table.parent.side_effect = lambda item: parents[item]
        rows = {"folder:0": ("출장",), "sub:0:1": ("출장", "회의")}
        self.assertEqual(drop_folder_row(table, rows, "file:2"), "sub:0:1")
        self.assertEqual(drop_folder_row(table, rows, "sub:0:1"), "sub:0:1")
        self.assertIsNone(drop_folder_row(table, rows, ""))

    def test_drag_highlights_hovered_file_and_containing_folder(self):
        surface = mock.Mock()
        surface.drag_target = None
        surface.drag_hover = None
        surface.folder_rows = {"folder:0": ("출장",)}
        OrganizerApp._show_drag_target(surface, "folder:0", "file:2")
        surface.table.item.assert_any_call("folder:0", tags=("folder", "drop_target"))
        surface.table.item.assert_any_call("file:2", tags=("drop_target",))
        OrganizerApp._show_drag_target(surface, None)
        surface.table.item.assert_any_call("folder:0", tags=("folder",))
        surface.table.item.assert_any_call("file:2", tags=())

    def test_double_click_name_edits_and_other_columns_preview(self):
        for item in ("file:1", "folder:0", "sub:0:0"):
            surface = mock.Mock()
            surface.busy = False
            surface.table.identify_row.return_value = item
            surface.table.identify_column.return_value = "#0"
            surface.table.identify_element.return_value = ""
            self.assertEqual(OrganizerApp.on_table_double_click(surface, mock.Mock(x=25, y=42)), "break")
            surface.begin_rename.assert_called_once_with(item)
            surface.show_selected_preview.assert_not_called()
            surface.table.identify_column.return_value = "#1"
            self.assertEqual(OrganizerApp.on_table_double_click(surface, mock.Mock(x=320, y=42)), "break")
            surface.show_selected_preview.assert_called_once_with()

    def test_right_click_selects_row_and_opens_context_menu(self):
        surface = mock.Mock()
        surface.busy = False
        surface.table.identify_row.return_value = "file:1"
        surface.table.selection.return_value = ()
        event = mock.Mock(y=30, x_root=100, y_root=200)
        handler = getattr(OrganizerApp, "on_table_right_click", lambda app, event: None)
        self.assertEqual(handler(surface, event), "break")
        surface.table.selection_set.assert_called_once_with("file:1")
        surface.context_menu.tk_popup.assert_called_once_with(100, 200)

    def test_right_click_tracks_clicked_file_with_multiple_selected(self):
        surface = mock.Mock()
        surface.busy = False
        surface.table.identify_row.return_value = "file:1"
        surface.table.selection.return_value = ("file:0", "file:1")
        OrganizerApp.on_table_right_click(surface, mock.Mock(y=30, x_root=10, y_root=20))
        self.assertEqual(getattr(surface, "context_item", None), "file:1")
        surface.table.selection_set.assert_not_called()

    def test_selected_folder_row_is_not_treated_as_a_file(self):
        self.assertEqual(selected_file_indices(("folder:0", "sub:0:0", "file:1", "file:2")), [1, 2])


if __name__ == "__main__":
    unittest.main()
