import tempfile
import unittest
from pathlib import Path

from ai_groups import assign_ai_folders
from organizer import Document, move_documents, rename_folder, rename_subfolder


def document(path, text="문서 내용"):
    return Document(Path(path), "기존", "원래 분류", "PDF 텍스트", lead_text=text)


class HierarchyTests(unittest.TestCase):
    def test_move_creates_only_valid_two_level_destination(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source"
            destination = root / "result"
            source.mkdir()
            destination.mkdir()
            path = source / "2017.01.pdf"
            path.write_bytes(b"monthly")
            doc = document(path)
            doc.folder = "민원 접수"
            doc.subfolder = "2017"
            moved, duplicates, errors = move_documents([doc], destination, source_root=source)
            self.assertEqual((len(moved), len(duplicates), len(errors)), (1, 0, 0))
            self.assertTrue((destination / "민원 접수" / "2017" / path.name).is_file())

    def test_unsafe_subfolder_keeps_source(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source"
            destination = root / "result"
            source.mkdir()
            destination.mkdir()
            path = source / "report.pdf"
            path.write_bytes(b"document")
            doc = document(path)
            doc.folder = "민원 접수"
            doc.subfolder = "../outside"
            moved, duplicates, errors = move_documents([doc], destination, source_root=source)
            self.assertEqual((len(moved), len(duplicates), len(errors)), (0, 0, 1))
            self.assertTrue(path.exists())
            self.assertFalse((root / "outside").exists())

    def test_structured_files_cannot_be_split_between_subfolders(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source"
            destination = root / "result"
            source.mkdir()
            destination.mkdir()
            docs = []
            for suffix, child in ((".shp", "2017"), (".shx", "2018")):
                path = source / f"map{suffix}"
                path.write_bytes(suffix.encode())
                doc = document(path)
                doc.folder = "GIS 자료"
                doc.subfolder = child
                docs.append(doc)
            with self.assertRaisesRegex(ValueError, "구성 파일"):
                move_documents(docs, destination, source_root=source)
            self.assertTrue(all(doc.source.exists() for doc in docs))
            self.assertFalse((destination / "GIS 자료").exists())

    def test_unrelated_structured_types_in_same_source_folder_can_go_to_separate_routes(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source"
            destination = root / "result"
            source.mkdir()
            destination.mkdir()
            docs = []
            for name, folder in (("map.shp", "GIS 자료"), ("tool.exe", "프로그램 자료")):
                path = source / name
                path.write_bytes(name.encode())
                doc = document(path)
                doc.folder = folder
                docs.append(doc)
            moved, duplicates, errors = move_documents(docs, destination, source_root=source)
            self.assertEqual((len(moved), len(duplicates), len(errors)), (2, 0, 0))
            self.assertTrue((destination / "GIS 자료" / "map.shp").exists())
            self.assertTrue((destination / "프로그램 자료" / "tool.exe").exists())

    def test_rename_top_and_subfolder_are_scoped(self):
        docs = [document("a.pdf"), document("b.pdf"), document("c.pdf")]
        for doc in docs:
            doc.folder = "연구"
        docs[0].subfolder = "설문"
        docs[1].subfolder = "인구"
        docs[2].folder = "기타"
        docs[2].subfolder = "설문"
        self.assertEqual(rename_folder(docs, "연구", "장유 연구"), [0, 1])
        self.assertEqual([doc.subfolder for doc in docs], ["설문", "인구", "설문"])
        self.assertEqual(rename_subfolder(docs, "장유 연구", "설문", "현장 조사"), [0])
        self.assertEqual([doc.subfolder for doc in docs], ["현장 조사", "인구", "설문"])
        with self.assertRaises(ValueError):
            rename_subfolder(docs, "장유 연구", "현장 조사", "../밖")

    def test_filename_year_overrides_misleading_parent_year(self):
        docs = [
            document(f"root/동별 민원접수 건수/2017/{year}.01.pdf", "동별 민원접수 통계")
            for year in (2016, 2017, 2018)
        ]
        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0] for _ in texts],
            classify=lambda groups, folders: [("동별 민원접수 건수", "민원 통계")],
            subdivide=lambda groups, parent: self.fail("연도가 명확하면 AI 세부 분류를 호출하지 않습니다"),
        )
        self.assertEqual([doc.folder for doc in docs], ["동별 민원접수 건수"] * 3)
        self.assertEqual([doc.subfolder for doc in docs], ["2016", "2017", "2018"])

    def test_one_year_does_not_create_unneeded_subfolder(self):
        docs = [document(f"root/보고서/2017.{month:02}.pdf") for month in (1, 2, 3)]
        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0] for _ in texts],
            classify=lambda groups, folders: [("월별 보고서", "같은 주제")],
        )
        self.assertEqual([doc.subfolder for doc in docs], [""] * 3)

    def test_group_label_sees_representatives_across_whole_group(self):
        docs = [document(f"root/민원/{2016 + index}.01.pdf", "민원 통계") for index in range(8)]
        seen = []

        def classify(groups, folders):
            seen.extend(groups[0]["documents"])
            return [("민원 접수", "민원 통계")]

        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0] for _ in texts], classify=classify,
            subdivide=lambda items, parent: [("", "같은 용도") for _ in items],
        )
        self.assertIn("2023.01.pdf", [item["name"] for item in seen])

    def test_distinct_embedding_groups_get_subtopics_only_when_meaningful(self):
        docs = [
            document(f"root/설문 조사 {index}.pdf", "현장 설문 응답") for index in range(4)
        ] + [
            document(f"root/인구 통계 {index}.pdf", "연령별 인구 추이") for index in range(4)
        ]
        def classify(groups, folders):
            return [("장유 연구", "연구 문서") for _ in groups]
        def subdivide(groups, parent):
            self.assertEqual(parent, "장유 연구")
            return [("설문", "설문 조사"), ("인구", "인구 통계")]

        assign_ai_folders(
            docs,
            embed=lambda texts: [[1.0, 0.0]] * 4 + [[0.0, 1.0]] * 4,
            classify=classify, subdivide=subdivide,
        )
        self.assertEqual([doc.subfolder for doc in docs], ["설문"] * 4 + ["인구"] * 4)

    def test_same_subtopic_label_keeps_one_level(self):
        docs = [document(f"root/자료 {index}.pdf") for index in range(8)]
        assign_ai_folders(
            docs,
            embed=lambda texts: [[1.0, 0.0]] * 4 + [[0.0, 1.0]] * 4,
            classify=lambda groups, folders: [("연구", "연구") for _ in groups],
            subdivide=lambda groups, parent: [("자료", "같은 내용") for _ in groups],
        )
        self.assertEqual([doc.subfolder for doc in docs], [""] * 8)

    def test_duplicate_follows_original_subfolder(self):
        original = document("root/2017.01.pdf", "월별 민원")
        duplicate = document("root/copy.pdf")
        duplicate.duplicate_of = original.source
        other = document("root/2018.01.pdf", "월별 민원")
        assign_ai_folders(
            [original, duplicate, other],
            embed=lambda texts: [[1.0, 0.0]] * 2,
            classify=lambda groups, folders: [("민원", "월별 통계")],
        )
        self.assertEqual(original.subfolder, "2017")
        self.assertEqual(duplicate.subfolder, original.subfolder)
        self.assertEqual(other.subfolder, "2018")


    def test_single_embedding_group_splits_repeated_document_purposes(self):
        docs = [document(f"root/프로젝트 기능 기술서 {i}.pdf") for i in range(3)]
        docs += [document(f"root/프로젝트 발표자료 {i}.pdf") for i in range(4)]
        docs += [document("root/프로젝트 메모.pdf")]
        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0] for _ in texts],
            classify=lambda groups, folders: [("프로젝트", "같은 프로젝트")],
            subdivide=lambda items, parent: self.fail("파일명 용도가 명확하면 추가 AI 호출이 없어야 합니다"),
        )
        self.assertEqual([d.folder for d in docs], ["프로젝트"] * 8)
        self.assertEqual([d.subfolder for d in docs],
                         ["기능기술서"] * 3 + ["발표자료"] * 4 + [""])

    def test_incomplete_purpose_batch_keeps_parent_without_partial_children(self):
        docs = [document(f"root/프로젝트 기술서 {i}.pdf") for i in range(8)]
        docs += [document(f"root/프로젝트 발표자료 {i}.pdf") for i in range(8)]
        calls = []

        def subdivide(items, parent):
            calls.append(len(items))
            return [("기술서", "문서 용도")]

        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0]] * 8 + [[0.0, 1.0]] * 8,
            classify=lambda groups, folders: [("프로젝트", "같은 프로젝트") for _ in groups],
            subdivide=subdivide, batch_size=16,
        )
        self.assertEqual(calls, [2])
        self.assertEqual([d.folder for d in docs], ["프로젝트"] * 16)
        self.assertEqual([d.subfolder for d in docs], [""] * 16)

    def test_cancelling_purpose_batches_preserves_original_preview(self):
        docs = [document(f"root/프로젝트 기술서 {i}.pdf") for i in range(8)]
        docs += [document(f"root/프로젝트 발표자료 {i}.pdf") for i in range(8)]
        cancelled = [False]
        calls = []

        def subdivide(items, parent):
            calls.append(len(items))
            cancelled[0] = True
            return [("기술서", "문서 용도"), ("발표자료", "문서 용도")]

        with self.assertRaises(InterruptedError):
            assign_ai_folders(
                docs, embed=lambda texts: [[1.0, 0.0]] * 8 + [[0.0, 1.0]] * 8,
                classify=lambda groups, folders: [("프로젝트", "같은 프로젝트") for _ in groups],
                subdivide=subdivide, batch_size=16,
                should_cancel=lambda: cancelled[0],
            )
        self.assertEqual(calls, [2])
        self.assertEqual([d.folder for d in docs], ["기존"] * 16)
        self.assertEqual([d.subfolder for d in docs], [""] * 16)

    def test_each_stage_is_named_and_reports_its_own_progress(self):
        docs = [document(f"root/프로젝트 기능 기술서 {i}.pdf") for i in range(4)]
        docs += [document(f"root/프로젝트 발표자료 {i}.pdf") for i in range(4)]
        updates, stages = [], []
        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0] for _ in texts],
            classify=lambda groups, folders: [("프로젝트", "같은 프로젝트")],
            progress=lambda current, total: updates.append((stages[-1], current, total)),
            stage=stages.append,
        )
        # 단계마다 이름을 알려, 오래 걸리는 단계에서도 화면이 멈춘 것처럼 보이지 않게 한다.
        self.assertEqual(stages, ["문서 내용 비교(임베딩)", "묶음 이름 짓기", "하위 폴더 나누기"])
        self.assertIn(("문서 내용 비교(임베딩)", 8, 8), updates)
        self.assertIn(("묶음 이름 짓기", 8, 8), updates)
        self.assertTrue(all(0 <= current <= total for _, current, total in updates))

    def test_retry_stage_reports_progress_and_sends_only_frequent_folders(self):
        import ai_groups
        docs = [document(f"root/주제{i:02d} 보고서.pdf", text=f"주제{i:02d} 관련 내용을 정리한 보고서입니다. " * 4)
                for i in range(40)]
        docs.append(document("root/알수없음.pdf", text="어느 주제에도 맞지 않는 긴 본문입니다. " * 6))
        known_sizes, updates, stages = [], [], []

        def classify(groups, folders):
            if len(groups) == 1 and "알수없음" in groups[0]["documents"][0]["name"]:
                known_sizes.append(len(folders))
                return [("미분류", "단서 없음")]
            return [(group["documents"][0]["name"][:4], "파일명") for group in groups]

        counter = iter(range(1000))

        def embed(texts):  # 문서마다 서로 다른 방향(묶이지 않게), 길이는 64로 고정
            return [[float(k % 64 == j) for j in range(64)] for k in (next(counter) for _ in texts)]

        assign_ai_folders(
            docs, ai_only=True,
            embed=embed,
            classify=classify, subdivide=lambda items, parent: [("", "") for _ in items],
            progress=lambda current, total: updates.append((stages[-1], current, total)), stage=stages.append,
        )
        self.assertIn("미분류 문서 다시 보기", stages)
        self.assertTrue(any(name == "미분류 문서 다시 보기" for name, _, _ in updates))
        # 마지막 요청이 재판정이다. 기존 폴더 40개 중 자주 쓰인 30개만 보낸다.
        self.assertEqual(known_sizes[-1], ai_groups.RETRY_KNOWN_FOLDERS)

    def test_parent_topic_is_not_repeated_as_subfolder(self):
        docs = [document(f"root/지역인구 보고서 {i}.pdf") for i in range(4)]
        docs += [document(f"root/지역 인구 통계 {i}.pdf") for i in range(4)]
        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0] for _ in texts],
            classify=lambda groups, folders: [("지역 인구 분석", "같은 주제")],
            subdivide=lambda items, parent: self.fail("한 임베딩 묶음은 추가 AI 호출이 없어야 합니다"),
        )
        self.assertEqual([d.folder for d in docs], ["지역 인구 분석"] * 8)
        self.assertEqual([d.subfolder for d in docs], ["보고서"] * 4 + ["통계"] * 4)

if __name__ == "__main__":
    unittest.main()

