import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organizer import scan

import classification_rules as rules
from classification_rules import STRUCTURED_TYPES, load_rules, type_folder


class ClassificationRulesTests(unittest.TestCase):
    def test_common_file_types_have_plain_korean_names(self):
        self.assertEqual(type_folder(Path("회의 녹음.m4a")), "녹음 파일")
        self.assertEqual(type_folder(Path("바로가기.lnk")), "바로가기")
        self.assertEqual(type_folder(Path("설명 영상.mp4")), "동영상")

    def test_type_only_files_keep_their_folder_even_with_subject_words(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "출장 계획.lnk").write_bytes(b"shortcut")
            (source / "회의 녹음.m4a").write_bytes(b"audio")
            with mock.patch("organizer.propose_folders", return_value=[("출장", "공통 단어"), ("회의", "공통 단어")]):
                docs = scan(source, Path(temporary) / "result", None)
        self.assertEqual([doc.folder for doc in docs], ["바로가기", "녹음 파일"])
    def test_unfamiliar_extensions_and_git_settings_have_readable_names(self):
        self.assertEqual(type_folder(Path("seoul_pop.ipynb")), "분석 노트북")
        self.assertEqual(type_folder(Path("gis.msld")), "GIS 자료")
        self.assertEqual(type_folder(Path("area.fix")), "설정·보조 파일")
        self.assertEqual(type_folder(Path("area.cst")), "설정·보조 파일")
        self.assertEqual(type_folder(Path(".gitignore")), "버전 관리 설정")
        self.assertEqual(type_folder(Path(".gitattributes")), "버전 관리 설정")

    def test_qmd_is_gis_metadata_only_when_file_content_says_qgis(self):
        with tempfile.TemporaryDirectory() as temporary:
            qgis = Path(temporary) / "map.qmd"
            quarto = Path(temporary) / "article.qmd"
            qgis.write_text("<?xml version='1.0'?><qgis version='3.40'><title>Map</title></qgis>", encoding="utf-8")
            quarto.write_text("---\ntitle: Article\n---\nText", encoding="utf-8")
            self.assertEqual(type_folder(qgis), "GIS 자료")
            self.assertEqual(type_folder(quarto), "마크다운 문서")
            self.assertTrue(getattr(rules, "is_structured_file", lambda path: False)(qgis))
            self.assertFalse(getattr(rules, "is_structured_file", lambda path: True)(quarto))

    def test_fix_and_cst_are_gis_only_beside_matching_map_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            shape = parent / "area.shp"
            fix = parent / "area.fix"
            charset = parent / "area.cst"
            fix.write_bytes(b"index")
            charset.write_text("EUC-KR", encoding="ascii")
            self.assertEqual(type_folder(fix), "설정·보조 파일")
            self.assertFalse(rules.is_structured_file(fix))
            shape.write_bytes(b"shape")
            self.assertEqual(type_folder(fix), "GIS 자료")
            self.assertEqual(type_folder(charset), "GIS 자료")
            self.assertTrue(rules.is_structured_file(fix))
            self.assertTrue(rules.is_structured_file(charset))

    def test_qgis_metadata_and_notebook_ignore_unrelated_topic_guess(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "map.qmd").write_text("<qgis version='3.40'></qgis>", encoding="utf-8")
            (source / "seoul_pop.ipynb").write_text('{"cells": []}', encoding="utf-8")
            with mock.patch("organizer.propose_folders", return_value=[("소상공인 정책", "잘못된 제안")] * 2):
                docs = scan(source, Path(temporary) / "result", None)
        self.assertEqual({doc.source.suffix: doc.folder for doc in docs},
                         {".qmd": "GIS 자료", ".ipynb": "분석 노트북"})

    def test_file_groups_come_from_editable_configuration(self):
        self.assertEqual(type_folder(Path("map.shp")), "GIS 자료")
        self.assertIn(".shp", STRUCTURED_TYPES)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rules.json"
            path.write_text(json.dumps({
                "ignored_words": ["예시"],
                "file_groups": [{
                    "folder": "새 형식", "extensions": [".foo"],
                    "preserve_structure": True,
                }],
            }), encoding="utf-8")
            ignored, folders, structured, type_only = load_rules(path)
        self.assertEqual(ignored, {"예시"})
        self.assertEqual(folders, {".foo": "새 형식"})
        self.assertEqual(structured, {".foo"})
        self.assertEqual(type_only, set())


if __name__ == "__main__":
    unittest.main()
