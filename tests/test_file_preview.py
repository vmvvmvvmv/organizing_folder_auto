import unittest
from unittest import mock
from pathlib import Path

import app
from organizer import Document


class FilePreviewTests(unittest.TestCase):
    def test_selection_previews_one_file_or_all_files_in_selected_folder(self):
        docs = [
            Document(Path("root/a.pdf"), "출장", "", "PDF 텍스트", subfolder="2025"),
            Document(Path("root/b.pdf"), "출장", "", "PDF 텍스트", subfolder="2026"),
            Document(Path("root/c.pdf"), "회의", "", "PDF 텍스트"),
        ]
        rows = {"folder:0": ("출장",), "sub:0:0": ("출장", "2025")}
        select = getattr(app, "preview_selection", lambda *args: [])
        self.assertEqual(select(docs, rows, "file:1"), [docs[1]])
        self.assertEqual(select(docs, rows, "folder:0"), docs[:2])
        self.assertEqual(select(docs, rows, "sub:0:0"), [docs[0]])
        self.assertEqual(select(docs, rows, "file:99"), [])

    def test_previewless_document_opens_with_windows_default_app(self):
        surface = mock.Mock()
        surface.busy = False
        surface.table.selection.return_value = ("file:0",)
        surface.folder_rows = {}
        recording = Path("root/meeting.m4a")
        surface.documents = [Document(recording, "녹음", "", "파일명·형식")]
        with mock.patch.object(app.os, "startfile", create=True) as open_file, \
             mock.patch("app.tk.Toplevel") as window:
            app.OrganizerApp.show_selected_preview(surface)
        open_file.assert_called_once_with(recording)
        window.assert_not_called()

    def test_executable_requires_confirmation_before_opening(self):
        surface = mock.Mock()
        surface.busy = False
        surface.table.selection.return_value = ("file:0",)
        surface.folder_rows = {}
        executable = Path("root/setup.exe")
        surface.documents = [Document(executable, "프로그램 자료", "", "파일명·형식")]
        with mock.patch.object(app.os, "startfile", create=True) as open_file, \
             mock.patch("app.messagebox.askyesno", side_effect=[False, True]) as confirm:
            app.OrganizerApp.show_selected_preview(surface)
            open_file.assert_not_called()
            app.OrganizerApp.show_selected_preview(surface)
        self.assertEqual(confirm.call_count, 2)
        open_file.assert_called_once_with(executable)

    def test_file_with_text_also_opens_with_default_app(self):
        surface = mock.Mock()
        surface.busy = False
        surface.table.selection.return_value = ("file:0",)
        surface.folder_rows = {}
        document = Path("root/report.docx")
        surface.documents = [Document(document, "보고서", "", "DOCX 텍스트", lead_text="내용")]
        with mock.patch.object(app.os, "startfile", create=True) as open_file,              mock.patch("app.tk.Toplevel") as window:
            app.OrganizerApp.show_selected_preview(surface)
        open_file.assert_called_once_with(document)
        window.assert_not_called()


if __name__ == "__main__":
    unittest.main()