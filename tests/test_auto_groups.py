import tempfile
import unittest
import shutil
import zipfile
import csv
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import batch
from auto_groups import propose_folders
from organizer import Document, move_documents, rename_folder, scan


class AutomaticFolderTests(unittest.TestCase):
    def test_unrelated_topics_get_folders_from_shared_words(self):
        files = [
            ("텃밭 신청.pdf", "2025년 안내문"),
            ("텃밭 참여.docx", "신청 방법"),
            ("텃밭 위치.png", "위치 안내"),
            ("건강 상담.pdf", "2025년 안내문"),
            ("건강 교육.docx", "상담 일정"),
            ("건강 관리.png", "관리 안내"),
            ("기타 문서.pdf", "내용 없음"),
        ]
        folders = [folder for folder, _ in propose_folders(files)]
        self.assertEqual(folders, ["텃밭"] * 3 + ["건강"] * 3 + ["미분류"])

    def test_title_can_group_numbered_scans(self):
        folders = [folder for folder, _ in propose_folders([
            ("scan_001.pdf", "반려식물 관리 신청서"),
            ("scan_002.pdf", "반려식물 재배 안내"),
            ("scan_003.pdf", "여행 일정 안내"),
        ])]
        self.assertEqual(folders, ["반려식물", "반려식물", "미분류"])

    def test_dates_and_generic_words_do_not_make_folders(self):
        folders = [folder for folder, _ in propose_folders([
            ("2025 보고서 1.pdf", "문서 내용"),
            ("2026 보고서 2.pdf", "문서 내용"),
            ("2027 보고서 3.pdf", "문서 내용"),
        ])]
        self.assertEqual(folders, ["미분류"] * 3)

    def test_four_related_files_still_group_in_small_folder(self):
        files = [(f"텃밭 {name}.pdf", "") for name in ("신청", "안내", "위치", "일정")]
        files.append(("다른 내용.pdf", ""))
        self.assertEqual([folder for folder, _ in propose_folders(files)], ["텃밭"] * 4 + ["미분류"])

    def test_scan_labels_and_table_units_are_not_topics(self):
        files = [
            ("SKM_C250101.pdf", "단위 원"),
            ("SKM_C250102.pdf", "단위 원"),
            ("페이지1.png", "그림 1"),
            ("페이지2.png", "그림 2"),
        ]
        self.assertEqual([folder for folder, _ in propose_folders(files)], ["미분류"] * 4)

    def test_word_shared_by_every_document_does_not_hide_smaller_topics(self):
        files = [(f"한빛 {topic} {number}.pdf", "") for topic in ("텃밭", "건강", "음악") for number in range(4)]
        folders = [folder for folder, _ in propose_folders(files)]
        self.assertEqual(folders, ["텃밭"] * 4 + ["건강"] * 4 + ["음악"] * 4)

    def test_joined_korean_words_can_share_a_folder(self):
        files = [(name, "") for name in (
            "출장결과보고서.pdf", "출장명령서.pdf", "출장계획서.pdf", "기상관측표.pdf",
        )]
        self.assertEqual([folder for folder, _ in propose_folders(files)], ["출장"] * 3 + ["미분류"])

    def test_broad_prefix_does_not_swallow_more_specific_topics(self):
        files = [(f"장유지역 행정구역 개편{i}.pdf", "") for i in range(4)]
        files += [(f"장유산책로 사진{i}.pdf", "") for i in range(4)]
        folders = [folder for folder, _ in propose_folders(files)]
        self.assertEqual(len(set(folders[:4])), 1)
        self.assertEqual(len(set(folders[4:])), 1)
        self.assertNotEqual(folders[0], folders[4])

    def test_batch_uses_a_separate_preview_for_each_source_folder(self):
        first = batch.default_manifest(Path("C:/documents/first"))
        second = batch.default_manifest(Path("C:/documents/second"))
        self.assertNotEqual(first, second)
        self.assertEqual(first, batch.default_manifest(Path("C:/documents/first")))

    def test_move_keeps_discovered_folder_and_rejects_path_escape(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            root = Path(root)
            source = root / "original.pdf"
            source.write_bytes(b"example")
            destination = root / "result"
            destination.mkdir()
            document = Document(source, "텃밭", "사용자 지정", "")
            moved, deduplicated, errors = move_documents([document], destination)
            self.assertEqual((len(moved), len(deduplicated), len(errors)), (1, 0, 0))
            self.assertTrue((destination / "텃밭" / source.name).is_file())
            source.write_bytes(b"example")
            self.assertEqual(len(move_documents([document], destination)[1]), 1)

            source.write_bytes(b"example")
            unsafe = Document(source, "../outside", "사용자 지정", "")
            self.assertEqual(len(move_documents([unsafe], destination)[2]), 1)
            self.assertTrue(source.exists())
            self.assertFalse((root / "outside").exists())

    def test_rename_folder_updates_every_file_in_that_group(self):
        paths = [Path(f"sample{i}.pdf") for i in range(3)]
        documents = [
            Document(paths[0], "텃밭", "공통 단어", ""),
            Document(paths[1], "텃밭", "공통 단어", ""),
            Document(paths[2], "건강", "공통 단어", ""),
        ]
        self.assertEqual(rename_folder(documents, "텃밭", "도시 농업"), [0, 1])
        self.assertEqual([doc.folder for doc in documents], ["도시 농업", "도시 농업", "건강"])
        self.assertEqual(documents[0].folder_reason, "사용자 지정")
        with self.assertRaises(ValueError):
            rename_folder(documents, "도시 농업", "../outside")

    def test_scan_groups_real_documents_and_keeps_duplicates_together(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            root = Path(root)
            source = root / "source"
            source.mkdir()
            destination = root / "result"
            for name, body in (("텃밭 신청.docx", "신청 방법"), ("텃밭 안내.docx", "운영 장소"), ("건강 교육.docx", "운동 방법")):
                with zipfile.ZipFile(source / name, "w") as archive:
                    archive.writestr("word/document.xml", f"<w:document xmlns:w='w'><w:t>{body}</w:t></w:document>")
            shutil.copyfile(source / "텃밭 신청.docx", source / "다른 이름.docx")

            documents = scan(source, destination, None)
            folders = {doc.source.name: doc.folder for doc in documents}
            self.assertEqual(folders["텃밭 신청.docx"], "텃밭")
            self.assertEqual(folders["텃밭 안내.docx"], "텃밭")
            self.assertEqual(folders["다른 이름.docx"], "텃밭")
            self.assertEqual(folders["건강 교육.docx"], "미분류")
            destination.mkdir()
            moved, deduplicated, errors = move_documents(documents, destination)
            self.assertEqual((len(moved), len(deduplicated), len(errors)), (3, 1, 0))

    def test_batch_preview_and_apply_use_same_proposed_folders(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            root = Path(root)
            source = root / "source"
            source.mkdir()
            for name, body in (("텃밭 신청.docx", "신청서"), ("텃밭 안내.docx", "위치"), ("건강 교육.docx", "운동")):
                with zipfile.ZipFile(source / name, "w") as archive:
                    archive.writestr("word/document.xml", f"<w:document xmlns:w='w'><w:t>{body}</w:t></w:document>")
            manifest = root / "preview.json"
            for action in ("preview", "apply"):
                with mock.patch.object(sys, "argv", ["batch.py", action, str(source), "--manifest", str(manifest)]):
                    with redirect_stdout(io.StringIO()):
                        batch.main()
            destination = root / "source_자동정리결과"
            report, = destination.glob("정리_결과_*.csv")
            with report.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row["폴더"] for row in rows], ["미분류", "텃밭", "텃밭"])
            self.assertEqual([row["상태"] for row in rows], ["이동", "이동", "이동"])
            self.assertTrue(all(row["분류 조건"] for row in rows))
            self.assertTrue((destination / "텃밭" / "텃밭 신청.docx").is_file())
            self.assertFalse((source / "텃밭 신청.docx").exists())

    def test_old_copy_preview_cannot_move_files(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            root = Path(root)
            source = root / "source"
            source.mkdir()
            original = source / "example.pdf"
            original.write_bytes(b"example")
            manifest = root / "preview.json"
            manifest.write_text(json.dumps({"format_version": 2}), encoding="utf-8")
            with mock.patch.object(sys, "argv", ["batch.py", "apply", str(source), "--manifest", str(manifest)]):
                with self.assertRaisesRegex(ValueError, "preview를 다시 실행"):
                    batch.main()
            self.assertTrue(original.exists())

    def test_batch_rejects_same_size_content_change_after_preview(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            root = Path(root)
            source = root / "source"
            source.mkdir()
            original = source / "note.txt"
            original.write_bytes(b"before")
            manifest = root / "preview.json"
            with mock.patch.object(sys, "argv", ["batch.py", "preview", str(source), "--manifest", str(manifest)]):
                with redirect_stdout(io.StringIO()):
                    batch.main()
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(data["format_version"], 5)
            self.assertEqual(len(data["documents"][0]["sha256"]), 64)
            previous = original.stat()
            original.write_bytes(b"after!")
            os.utime(original, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            with mock.patch.object(sys, "argv", ["batch.py", "apply", str(source), "--manifest", str(manifest)]):
                with self.assertRaisesRegex(ValueError, "미리보기 이후"):
                    batch.main()
            self.assertTrue(original.exists())


if __name__ == "__main__":
    unittest.main()
