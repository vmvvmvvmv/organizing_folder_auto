import tempfile
import unittest
import zipfile
import hashlib
import csv
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import organizer
from organizer import Document, move_documents, scan
from reader import _ocr_image, find_tesseract, ocr_status, read_document


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_docx_and_hwpx_text(self):
        docx = self.folder / "sample.docx"
        hwpx = self.folder / "sample.hwpx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>텃밭</w:t></w:r></w:p></w:body></w:document>',
            )
        with zipfile.ZipFile(hwpx, "w") as archive:
            archive.writestr(
                "Contents/section0.xml",
                '<hs:sec xmlns:hs="urn:hs" xmlns:hp="urn:hp"><hp:t>건강</hp:t></hs:sec>',
            )
        self.assertIn("텃밭", read_document(docx, None)[0])
        self.assertIn("건강", read_document(hwpx, None)[0])

    def test_frozen_app_uses_bundled_tesseract(self):
        executable = self.folder / "문서정리.exe"
        bundled = self.folder / "ocr" / "tesseract.exe"
        bundled.parent.mkdir()
        bundled.write_bytes(b"test")
        with mock.patch.object(sys, "executable", str(executable)):
            self.assertEqual(find_tesseract(), str(bundled))

    def test_onefile_app_uses_extracted_tesseract(self):
        extracted = self.folder / "extracted"
        bundled = extracted / "ocr" / "tesseract.exe"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"test")
        executable = self.folder / "DocumentOrganizer.exe"
        with mock.patch.object(sys, "executable", str(executable)), \
             mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "_MEIPASS", str(extracted), create=True):
            self.assertEqual(find_tesseract(), str(bundled))
    def test_ocr_runs_without_console_window_or_stdin(self):
        result = mock.Mock(returncode=0, stdout="kor", stderr="")
        with mock.patch("reader.subprocess.run", return_value=result) as run:
            self.assertEqual(ocr_status("tesseract.exe"), "한국어 OCR 사용 가능")
            _ocr_image(self.folder / "image.png", "tesseract.exe")
        self.assertTrue(all(call.kwargs.get("stdin") == subprocess.DEVNULL for call in run.call_args_list))
        if sys.platform == "win32":
            self.assertTrue(all(
                call.kwargs.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
                for call in run.call_args_list
            ))

    def test_plain_text_files_are_read_in_common_korean_encodings(self):
        samples = {
            "memo.txt": "출장 결과 메모".encode("utf-8"),
            "notes.md": "# 회의록\n안건".encode("cp949"),
            "table.csv": "지역,인구\n장유,1000".encode("utf-16"),
        }
        for name, data in samples.items():
            (self.folder / name).write_bytes(data)
        self.assertEqual(read_document(self.folder / "memo.txt", None), ("출장 결과 메모", "텍스트 파일"))
        self.assertIn("회의록", read_document(self.folder / "notes.md", None)[0])
        self.assertIn("장유", read_document(self.folder / "table.csv", None)[0])
        (self.folder / "binary.txt").write_bytes(b"MZ\x00\x01\x02")
        with self.assertRaises(ValueError):
            read_document(self.folder / "binary.txt", None)

    def test_xlsx_and_pptx_text(self):
        xlsx = self.folder / "sample.xlsx"
        pptx = self.folder / "sample.pptx"
        with zipfile.ZipFile(xlsx, "w") as archive:
            archive.writestr("xl/sharedStrings.xml", '<sst xmlns="urn:x"><si><t>산책 일정</t></si></sst>')
            archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="urn:x"><sheetData/></worksheet>')
        with zipfile.ZipFile(pptx, "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", '<sld xmlns:a="urn:a"><a:t>기상 관측</a:t></sld>')
        self.assertIn("산책 일정", read_document(xlsx, None)[0])
        self.assertIn("기상 관측", read_document(pptx, None)[0])

    def test_text_pdf_can_be_read_without_ocr(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("PyMuPDF is not installed")
        path = self.folder / "notice.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 50), "Official document with enough text to extract from PDF")
            pdf.save(path)
        text, method = read_document(path, None)
        self.assertIn("Official document", text)
        self.assertEqual(method, "PDF 텍스트")

    def test_pdf_font_warning_is_visible_in_read_method(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("PyMuPDF is not installed")
        path = self.folder / "font-warning.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 50), "Official document with enough text to extract from PDF")
            pdf.save(path)
        with mock.patch.object(pymupdf.TOOLS, "mupdf_warnings", return_value="syntax error: unknown cid font type"):
            text, method = read_document(path, None)
        self.assertIn("Official document", text)
        self.assertIn("글꼴 경고", method)

    def test_scanned_pdf_renders_page_for_ocr(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("PyMuPDF is not installed")
        path = self.folder / "scan.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.save(path)
        original_temporary_directory = tempfile.TemporaryDirectory
        with mock.patch(
            "reader.tempfile.TemporaryDirectory",
            side_effect=lambda: original_temporary_directory(dir=self.folder),
        ), mock.patch("reader.ocr_status", return_value="한국어 OCR 사용 가능"), mock.patch(
            "reader._ocr_image", return_value="텃밭"
        ) as ocr:
            text, method = read_document(path, "tesseract.exe")
        self.assertEqual(text, "텃밭")
        self.assertEqual(method, "PDF 텍스트 + OCR")
        ocr.assert_called_once()

    def test_scanned_pdf_without_ocr_reports_setup_need(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("PyMuPDF is not installed")
        path = self.folder / "scan.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.save(path)
        with self.assertRaisesRegex(RuntimeError, "Tesseract"):
            read_document(path, None)

    def test_image_without_ocr_is_left_unclassified(self):
        source = self.folder / "images"
        source.mkdir()
        (source / "receipt.png").write_bytes(b"image contents")
        documents = scan(source, self.folder / "result", None)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].folder, "미분류")
        self.assertIn("한국어 OCR", documents[0].error)

    def test_corrupt_office_file_is_reported_without_stopping_scan(self):
        source = self.folder / "source"
        source.mkdir()
        (source / "broken.docx").write_bytes(b"not a zip")
        documents = scan(source, self.folder / "result", None)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].folder, "미분류")
        self.assertIn("zip", documents[0].error.lower())

    def test_preview_excludes_result_folder_and_move_verifies_content(self):
        source = self.folder / "원본"
        destination = source / "정리결과"
        source.mkdir()
        destination.mkdir()
        docx = source / "텃밭.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="urn:w"><w:t>텃밭 안내</w:t></w:document>',
            )
        (destination / "이전.pdf").write_bytes(b"old output")
        documents = scan(source, destination, None)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].folder, "미분류")
        original_bytes = docx.read_bytes()
        moved, deduplicated, errors = move_documents(documents, destination)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (1, 0, 0))
        self.assertFalse(docx.exists())
        self.assertEqual(original_bytes, moved[0][1].read_bytes())

    def test_filename_collision_gets_new_name(self):
        source = self.folder / "a.pdf"
        source.write_bytes(b"new")
        destination = self.folder / "result"
        (destination / "자료").mkdir(parents=True)
        (destination / "자료" / "a.pdf").write_bytes(b"old")
        moved, deduplicated, errors = move_documents([Document(source, "자료", "", "")], destination)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (1, 0, 0))
        self.assertEqual(moved[0][1].name, "a_2.pdf")
        self.assertFalse(source.exists())
        self.assertEqual((destination / "자료" / "a.pdf").read_bytes(), b"old")

    def test_same_content_with_different_names_or_folders_is_kept_once(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source.mkdir()
        destination.mkdir()
        first = source / "a.pdf"
        second = source / "b.pdf"
        first.write_bytes(b"same document")
        second.write_bytes(b"same document")
        documents = [
            Document(first, "자료", "", ""),
            Document(second, "사진", "", ""),
        ]
        moved, deduplicated, errors = move_documents(documents, destination)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (1, 1, 0))
        self.assertTrue((destination / "자료" / "a.pdf").exists())
        self.assertFalse((destination / "사진" / "b.pdf").exists())
        self.assertEqual(deduplicated[0][1], moved[0][1])
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_preview_marks_exact_duplicate(self):
        source = self.folder / "source"
        source.mkdir()
        content = b"identical image"
        (source / "a.png").write_bytes(content)
        (source / "b.png").write_bytes(content)
        documents = scan(source, self.folder / "result", None)
        self.assertEqual(len(documents), 2)
        self.assertIsNone(documents[0].duplicate_of)
        self.assertEqual(documents[1].duplicate_of, documents[0].source)

    def test_move_verifies_content_before_removing_original(self):
        source = self.folder / "source.pdf"
        source.write_bytes(b"original document")
        destination = self.folder / "result"
        destination.mkdir()
        moved, deduplicated, errors = move_documents([Document(source, "자료", "", "")], destination)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (1, 0, 0))
        self.assertFalse(source.exists())
        self.assertEqual(hashlib.sha256(moved[0][1].read_bytes()).digest(), hashlib.sha256(b"original document").digest())

    def test_failed_content_verification_keeps_original(self):
        source = self.folder / "source.pdf"
        source.write_bytes(b"original document")
        destination = self.folder / "result"
        destination.mkdir()
        with mock.patch("organizer.shutil.copyfileobj", side_effect=lambda original, output: output.write(b"altered")):
            moved, deduplicated, errors = move_documents([Document(source, "자료", "", "")], destination)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (0, 0, 1))
        self.assertEqual(source.read_bytes(), b"original document")
        self.assertFalse((destination / "자료" / source.name).exists())

    def test_corruption_after_publish_keeps_original(self):
        source = self.folder / "source.pdf"
        source.write_bytes(b"original document")
        destination = self.folder / "result"
        destination.mkdir()
        real_rename = Path.rename

        def corrupt_after_rename(path, target):
            result = real_rename(path, target)
            if path.suffix == ".partial":
                Path(target).write_bytes(b"altered")
            return result

        with mock.patch("organizer.Path.rename", new=corrupt_after_rename):
            moved, deduplicated, errors = move_documents([Document(source, "자료", "", "")], destination)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (0, 0, 1))
        self.assertEqual(source.read_bytes(), b"original document")
        self.assertFalse((destination / "자료" / source.name).exists())

    def test_existing_identical_result_allows_deduplicating_source(self):
        source = self.folder / "source.pdf"
        source.write_bytes(b"same document")
        destination = self.folder / "result"
        destination.mkdir()
        (destination / "자료").mkdir()
        existing = destination / "자료" / "already.pdf"
        existing.write_bytes(source.read_bytes())
        moved, deduplicated, errors = move_documents([Document(source, "자료", "", "")], destination)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (0, 1, 0))
        self.assertEqual(deduplicated[0][1], existing)
        self.assertFalse(source.exists())

    def test_existing_identical_file_in_other_folder_does_not_remove_source(self):
        source = self.folder / "source.pdf"
        source.write_bytes(b"same document")
        destination = self.folder / "result"
        (destination / "사진").mkdir(parents=True)
        (destination / "사진" / "old.pdf").write_bytes(source.read_bytes())
        moved, deduplicated, errors = move_documents([Document(source, "자료", "", "")], destination)
        # 미리보기에서 고른 '자료' 폴더에 실제로 파일이 생겨야 한다.
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (1, 0, 0))
        self.assertEqual(moved[0][1], destination / "자료" / "source.pdf")
        self.assertTrue((destination / "사진" / "old.pdf").exists())

    def test_failure_to_remove_original_keeps_both_verified_files(self):
        source = self.folder / "source.pdf"
        source.write_bytes(b"original document")
        destination = self.folder / "result"
        destination.mkdir()
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == source:
                raise PermissionError("cannot remove source")
            return real_unlink(path, *args, **kwargs)

        with mock.patch("organizer.Path.unlink", new=unlink):
            moved, deduplicated, errors = move_documents([Document(source, "자료", "", "")], destination)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (0, 0, 1))
        self.assertEqual(source.read_bytes(), (destination / "자료" / source.name).read_bytes())

    def test_move_report_records_every_result_and_keeps_previous_reports(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source.mkdir()
        destination.mkdir()
        files = [source / name for name in ("first.pdf", "same.pdf", "invalid.pdf")]
        for path, content in zip(files, (b"same", b"same", b"other")):
            path.write_bytes(content)
        documents = [
            Document(files[0], "자료", "공통 단어", "PDF 텍스트"),
            Document(files[1], "자료", "공통 단어", "중복 파일"),
            Document(files[2], "../outside", "사용자 지정", ""),
        ]
        with organizer.ResultReport(source, destination, documents) as first_report:
            moved, deduplicated, errors = move_documents(documents, destination, record=first_report.record)
        with organizer.ResultReport(source, destination, documents) as second_report:
            pass
        self.assertNotEqual(first_report.path, second_report.path)
        with first_report.path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["상태"] for row in rows], ["이동", "중복 정리", "이동 실패"])
        self.assertEqual(rows[1]["결과 파일"], str(moved[0][1]))
        self.assertIn("폴더 이름", rows[2]["이동 오류"])

    def test_move_can_stop_after_current_file_and_report_unprocessed_files(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source.mkdir()
        destination.mkdir()
        documents = []
        for index in range(3):
            path = source / f"file{index}.pdf"
            path.write_bytes(f"content {index}".encode())
            documents.append(Document(path, "자료", "", ""))
        stop = threading.Event()
        with organizer.ResultReport(source, destination, documents) as report:
            moved, deduplicated, errors = move_documents(
                documents, destination,
                lambda done, total: stop.set() if done == 1 else None,
                should_cancel=stop.is_set, record=report.record,
            )
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (1, 0, 0))
        self.assertFalse(documents[0].source.exists())
        self.assertTrue(all(doc.source.exists() for doc in documents[1:]))
        with report.path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["상태"] for row in rows], ["이동", "미처리", "미처리"])

    def organize(self, files):
        source = self.folder / "source"
        destination = self.folder / "result"
        destination.mkdir()
        for name, data in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        documents = scan(source, destination, None)
        for doc in documents:
            if not doc.preserve_structure:
                doc.folder = "자료"
        with organizer.ResultReport(source, destination, documents) as report:
            move_documents(documents, destination, source_root=source, record=report.record)
        return source, destination, report.path

    def test_undo_restores_moved_duplicate_and_gis_files_to_original_places(self):
        files = {"a/보고서.xyz": b"report", "b/사본.xyz": b"report", "c/메모.xyz": b"memo",
                 "maps/area.shp": b"shape", "maps/area.shx": b"index"}
        source, destination, report = self.organize(files)
        self.assertFalse(any(path.is_file() for path in source.rglob("*")))
        restored, errors = organizer.undo_report(report)
        self.assertEqual((len(restored), errors), (5, []))
        for name, data in files.items():
            self.assertEqual((source / name).read_bytes(), data)
        # 결과 폴더에는 기록 파일만 남고 비워진 하위 폴더는 지워진다.
        self.assertEqual([path.name for path in destination.iterdir()], [report.name])

    def test_undo_skips_file_whose_original_place_is_taken_again(self):
        source, destination, report = self.organize({"메모.xyz": b"memo", "표.xyz": b"table"})
        (source / "메모.xyz").write_bytes(b"new file created later")
        restored, errors = organizer.undo_report(report)
        self.assertEqual([original.name for _, original in restored], ["표.xyz"])
        self.assertIn("이미 있어", errors[0][1])
        self.assertEqual((source / "메모.xyz").read_bytes(), b"new file created later")
        self.assertTrue((destination / "자료" / "메모.xyz").exists())

    def test_undo_refuses_paths_outside_the_report_folder(self):
        source, destination, report = self.organize({"메모.xyz": b"memo"})
        outside = self.folder / "outside.xyz"
        outside.write_bytes(b"not part of the result")
        text = report.read_text(encoding="utf-8-sig")
        report.write_text(text.replace(str(destination / "자료" / "메모.xyz"), str(outside)),
                          encoding="utf-8-sig")
        restored, errors = organizer.undo_report(report)
        self.assertEqual(restored, [])
        self.assertIn("결과 폴더와 맞지 않습니다", errors[0][1])
        self.assertTrue(outside.exists())

    def test_undo_explains_old_reports_without_original_paths(self):
        report = self.folder / "정리_결과_old.csv"
        report.write_text("원본,폴더,상태,결과 파일\na.pdf,자료,이동,x\n", encoding="utf-8-sig")
        with self.assertRaisesRegex(ValueError, "원래 위치 정보"):
            organizer.undo_report(report)

    def test_report_keeps_finished_rows_when_move_crashes(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source.mkdir()
        destination.mkdir()
        documents = []
        for index in range(2):
            path = source / f"file{index}.pdf"
            path.write_bytes(f"content {index}".encode())
            documents.append(Document(path, "자료", "", ""))

        def crash(done, _total):
            if done == 1:
                raise RuntimeError("simulated crash")

        report = organizer.ResultReport(source, destination, documents)
        with self.assertRaises(RuntimeError):
            move_documents(documents, destination, crash, record=report.record)
        # 닫기 전(프로세스가 죽은 상황)에도 처리한 줄은 이미 디스크에 있다.
        with report.path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        report.close()
        self.assertEqual([row["상태"] for row in rows], ["이동"])

    def test_gis_group_failure_mid_copy_keeps_whole_set_in_source(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        maps = source / "maps"
        maps.mkdir(parents=True)
        destination.mkdir()
        for suffix in (".shp", ".shx", ".dbf"):
            (maps / f"area{suffix}").write_bytes(suffix.encode() * 10)
        documents = scan(source, destination, None)
        real_copy = organizer.shutil.copyfileobj

        def fail_on_dbf(original, output):
            if original.name.endswith(".dbf"):
                raise OSError("disk full")
            return real_copy(original, output)

        with mock.patch("organizer.shutil.copyfileobj", side_effect=fail_on_dbf):
            moved, deduplicated, errors = move_documents(documents, destination, source_root=source)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (0, 0, 3))
        self.assertTrue(all(doc.source.exists() for doc in documents))
        self.assertFalse([path for path in destination.rglob("*") if path.is_file()])
        self.assertIn("area.dbf", errors[0][1])

    def test_unreadable_file_fails_alone_instead_of_blocking_run(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source.mkdir()
        destination.mkdir()
        (source / "locked.pdf").write_bytes(b"locked")
        (source / "free.pdf").write_bytes(b"free")
        documents = scan(source, destination, None)
        real_digest = organizer._digest

        def digest(path):
            if path.name == "locked.pdf":
                raise PermissionError(13, "Permission denied")
            return real_digest(path)

        with mock.patch("organizer._digest", side_effect=digest):
            moved, deduplicated, errors = move_documents(documents, destination, source_root=source)
        self.assertEqual([doc.source.name for doc, _ in moved], ["free.pdf"])
        self.assertEqual([doc.source.name for doc, _ in errors], ["locked.pdf"])
        self.assertTrue((source / "locked.pdf").exists())

    def test_locked_file_during_preview_is_reported_and_never_moved(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source.mkdir()
        destination.mkdir()
        (source / "locked.xyz").write_bytes(b"locked")
        (source / "free.xyz").write_bytes(b"free")
        real_digest = organizer._digest
        locked = True

        def digest(path):
            if locked and path.name == "locked.xyz":
                raise PermissionError(13, "Permission denied")
            return real_digest(path)

        with mock.patch("organizer._digest", side_effect=digest):
            documents = scan(source, destination, None)
        by_name = {doc.source.name: doc for doc in documents}
        self.assertIn("읽을 수 없습니다", by_name["locked.xyz"].error)
        self.assertEqual(by_name["free.xyz"].error, "")
        locked = False  # 미리보기 뒤 잠금이 풀려도 확인하지 못한 파일은 옮기지 않는다
        moved, deduplicated, errors = move_documents(documents, destination, source_root=source)
        self.assertEqual([doc.source.name for doc, _ in moved], ["free.xyz"])
        self.assertEqual([doc.source.name for doc, _ in errors], ["locked.xyz"])
        self.assertTrue((source / "locked.xyz").exists())

    def test_code_projects_and_tool_folders_are_left_untouched(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        destination.mkdir()
        for name in ("메모.xyz", "web/package.json", "web/src/app.js", "tool/.git/HEAD",
                     "tool/main.py", "docs/node_modules/lib/index.js", "docs/보고.xyz"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        files, projects = organizer.source_files(source, destination)
        self.assertEqual([path.relative_to(source).as_posix() for path in files],
                         ["docs/보고.xyz", "메모.xyz"])
        self.assertEqual(sorted(path.name for path in projects), ["tool", "web"])
        documents = scan(source, destination, None)
        self.assertEqual(len(documents), 2)
        # 프로젝트 안 파일이 바뀌어도 미리보기와 비교할 목록에 들어가지 않는다.
        (source / "web" / "src" / "new.js").write_bytes(b"new")
        moved, _, errors = move_documents(documents, destination, source_root=source)
        self.assertEqual((len(moved), len(errors)), (2, 0))
        self.assertTrue((source / "web" / "src" / "app.js").exists())
        self.assertTrue((source / "docs" / "node_modules" / "lib" / "index.js").exists())

    def test_choosing_a_code_project_itself_is_refused(self):
        source = self.folder / "project"
        source.mkdir()
        (source / "package.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "코드 프로젝트"):
            scan(source, self.folder / "result", None)

    def test_scan_can_stop_between_files(self):
        source = self.folder / "source"
        source.mkdir()
        for index in range(2):
            (source / f"file{index}.png").write_bytes(f"content {index}".encode())
        stop = threading.Event()
        with self.assertRaises(InterruptedError):
            scan(source, self.folder / "result", None,
                 lambda done, total: stop.set() if done == 1 else None,
                 should_cancel=stop.is_set)

    def test_scan_ai_only_skips_keyword_folder_proposals(self):
        source = self.folder / "ai_source"
        source.mkdir()
        (source / "출장 보고서.txt").write_text("출장", encoding="utf-8")
        with mock.patch("organizer.propose_folders", side_effect=AssertionError("keyword rules used")):
            documents = scan(source, self.folder / "ai_result", None, classify=False)
        self.assertEqual([doc.folder for doc in documents], ["미분류"])

    def test_output_filename_changes_only_after_verified_move(self):
        source = self.folder / "memo.txt"
        source.write_text("original", encoding="utf-8")
        destination = self.folder / "renamed_result"
        destination.mkdir()
        doc = Document(source, "자료", "", "파일명·형식")
        rename = getattr(organizer, "rename_document", lambda *args: None)
        rename(doc, "새 메모.txt")
        self.assertTrue(source.is_file())
        self.assertEqual(doc.output_name, "새 메모.txt")
        moved, duplicates, errors = move_documents([doc], destination)
        self.assertEqual((len(moved), len(duplicates), len(errors)), (1, 0, 0))
        self.assertEqual(moved[0][1].name, "새 메모.txt")
        self.assertEqual(moved[0][1].read_text(encoding="utf-8"), "original")

    def test_output_filename_preserves_extension_and_rejects_structured_files(self):
        rename = getattr(organizer, "rename_document", lambda *args: None)
        doc = Document(Path("memo.pdf"), "자료", "", "PDF 텍스트")
        with self.assertRaises(ValueError):
            rename(doc, "memo.exe")
        with self.assertRaises(ValueError):
            rename(doc, "../memo.pdf")
        structured = Document(Path("map.shp"), "GIS 자료", "", "파일명·형식", preserve_structure=True)
        with self.assertRaises(ValueError):
            rename(structured, "other.shp")

    def test_reassign_preview_moves_duplicate_with_original(self):
        original = Document(Path("root/a.pdf"), "기존", "", "PDF 텍스트")
        duplicate = Document(Path("root/copy.pdf"), "기존", "", "중복 파일",
                             duplicate_of=original.source)
        other = Document(Path("root/other.pdf"), "다른 폴더", "", "PDF 텍스트")
        documents = [original, duplicate, other]
        assign = getattr(organizer, "reassign_documents", lambda *args: [])
        changed = assign(documents, [1], "새 폴더", "세부")
        self.assertEqual(changed, [0, 1])
        self.assertEqual([(doc.folder, doc.subfolder) for doc in documents],
                         [("새 폴더", "세부"), ("새 폴더", "세부"), ("다른 폴더", "")])
        self.assertEqual(original.folder_reason, "사용자 지정")

    def test_reassign_preview_keeps_gis_components_together(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        maps = source / "maps"
        maps.mkdir(parents=True)
        destination.mkdir()
        (maps / "area.shp").write_bytes(b"shape")
        (maps / "area.shx").write_bytes(b"index")
        (maps / "area.qmd").write_text("<qgis version='3.40'></qgis>", encoding="utf-8")
        documents = scan(source, destination, None)
        shp_index = next(index for index, doc in enumerate(documents) if doc.source.suffix == ".shp")
        assign = getattr(organizer, "reassign_documents", lambda *args: [])
        changed = assign(documents, [shp_index], "새 GIS 폴더", "")
        self.assertEqual(len(changed), 3)
        self.assertEqual({doc.folder for doc in documents}, {"새 GIS 폴더"})
        moved, duplicates, errors = move_documents(documents, destination, source_root=source)
        self.assertEqual((len(moved), len(duplicates), len(errors)), (3, 0, 0))

    def test_reassign_preview_rejects_invalid_folder_without_changes(self):
        doc = Document(Path("root/a.pdf"), "기존", "원래 분류", "PDF 텍스트")
        assign = getattr(organizer, "reassign_documents", lambda *args: [])
        with self.assertRaises(ValueError):
            assign([doc], [0], "../밖", "")
        self.assertEqual((doc.folder, doc.folder_reason), ("기존", "원래 분류"))

    def test_qgis_metadata_moves_with_its_map_layer(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source_group = source / "maps"
        source_group.mkdir(parents=True)
        destination.mkdir()
        (source_group / "area.shp").write_bytes(b"shape")
        (source_group / "area.qmd").write_text("<qgis version='3.40'></qgis>", encoding="utf-8")
        documents = scan(source, destination, None)
        self.assertEqual({doc.folder for doc in documents}, {"GIS 자료"})
        moved, duplicates, errors = move_documents(documents, destination, source_root=source)
        self.assertEqual((len(moved), len(duplicates), len(errors)), (2, 0, 0))
        self.assertEqual({target.parent for _, target in moved}, {destination / "GIS 자료" / "maps"})

    def test_other_file_types_are_previewed_and_moved_with_related_files(self):
        source = self.folder / "source"
        destination = source / "result"
        destination.mkdir(parents=True)
        for name, contents in {
            "maps/area1/first.shp": b"shape",
            "maps/area1/first.shx": b"index",
            "maps/area1/first.prj": b"shared projection",
            "maps/area2/second.prj": b"shared projection",
            "install/setup.exe": b"installer",
            "install/lib.dll": b"dependency",
            "legacy.hwp": b"old hwp",
            "archive.zip": b"archive",
            "notes.xyz": b"unknown",
        }.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        (destination / "previous.geojson").write_bytes(b"old result")
        documents = scan(source, destination, None)
        self.assertEqual(len(documents), 9)
        self.assertTrue(all(doc.method == "파일명·형식" and not doc.error for doc in documents))
        folders = {doc.source.name: doc.folder for doc in documents}
        self.assertEqual(folders["first.shp"], folders["first.shx"])
        self.assertEqual(folders["first.shp"], folders["first.prj"])
        self.assertNotEqual(folders["legacy.hwp"], "미분류")
        moved, deduplicated, errors = move_documents(documents, destination, source_root=source)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (9, 0, 0))
        for doc, target in moved:
            if doc.source.suffix in {".shp", ".shx", ".prj", ".exe", ".dll"}:
                self.assertEqual(target.parent.relative_to(destination).parts[1:], doc.source.parent.relative_to(source).parts)
        self.assertFalse(any(doc.source.exists() for doc in documents))

    def test_gis_sidecars_keep_their_route_after_shape_file_moves_first(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        maps = source / "maps"
        maps.mkdir(parents=True)
        destination.mkdir()
        (maps / "area.shp").write_bytes(b"shape")
        (maps / "area.fix").write_bytes(b"index")
        (maps / "area.cst").write_bytes(b"EUC-KR")
        documents = scan(source, destination, None)
        documents.sort(key=lambda doc: doc.source.suffix != ".shp")
        moved, duplicates, errors = move_documents(documents, destination, source_root=source)
        self.assertEqual((len(moved), len(duplicates), len(errors)), (3, 0, 0))
        self.assertEqual({target.parent for _, target in moved}, {destination / "GIS 자료" / "maps"})

    def test_related_gis_files_stay_in_source_if_one_target_conflicts(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source_group = source / "maps"
        result_group = destination / "GIS 자료" / "maps"
        source_group.mkdir(parents=True)
        result_group.mkdir(parents=True)
        for suffix in (".shp", ".shx"):
            (source_group / f"area{suffix}").write_bytes(b"new" + suffix.encode())
            (result_group / f"area{suffix}").write_bytes(
                (b"new" if suffix == ".shp" else b"old") + suffix.encode()
            )
        documents = scan(source, destination, None)
        moved, deduplicated, errors = move_documents(documents, destination, source_root=source)
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (0, 0, 2))
        self.assertTrue(all(doc.source.exists() for doc in documents))

    def test_stop_finishes_related_gis_group(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        (source / "maps").mkdir(parents=True)
        destination.mkdir()
        for name in ("maps/area.shp", "maps/area.shx", "next.zip"):
            (source / name).write_bytes(name.encode())
        documents = scan(source, destination, None)
        stop = threading.Event()
        moved, deduplicated, errors = move_documents(
            documents, destination,
            lambda done, total: stop.set() if done == 1 else None,
            should_cancel=stop.is_set, source_root=source,
        )
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (2, 0, 0))
        self.assertTrue((source / "next.zip").exists())

    def test_changed_file_after_preview_blocks_every_move_even_with_same_size_and_mtime(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source.mkdir()
        destination.mkdir()
        first = source / "first.txt"
        second = source / "second.txt"
        first.write_bytes(b"first")
        second.write_bytes(b"before")
        documents = scan(source, destination, None)
        previous = second.stat()
        second.write_bytes(b"after!")
        os.utime(second, ns=(previous.st_atime_ns, previous.st_mtime_ns))

        with self.assertRaisesRegex(ValueError, "미리보기 이후"):
            move_documents(documents, destination, source_root=source)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertFalse(any(destination.rglob("*")))

    def test_new_file_after_preview_blocks_every_move(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source.mkdir()
        destination.mkdir()
        first = source / "first.txt"
        first.write_bytes(b"first")
        documents = scan(source, destination, None)
        (source / "added.txt").write_bytes(b"added")

        with self.assertRaisesRegex(ValueError, "파일 목록"):
            move_documents(documents, destination, source_root=source)
        self.assertTrue(first.exists())
        self.assertFalse(any(destination.rglob("*")))

    def test_change_during_move_keeps_changed_original(self):
        source = self.folder / "source"
        destination = self.folder / "result"
        source.mkdir()
        destination.mkdir()
        first = source / "first.txt"
        second = source / "second.txt"
        first.write_bytes(b"first")
        second.write_bytes(b"before")
        documents = scan(source, destination, None)

        def change_after_first(done, _total):
            if done == 1:
                second.write_bytes(b"after!")

        moved, deduplicated, errors = move_documents(
            documents, destination, change_after_first, source_root=source,
        )
        self.assertEqual((len(moved), len(deduplicated), len(errors)), (1, 0, 1))
        self.assertTrue(second.exists())
        self.assertIn("미리보기 이후", errors[0][1])


if __name__ == "__main__":
    unittest.main()
