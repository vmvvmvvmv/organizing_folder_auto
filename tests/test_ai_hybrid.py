import unittest
from pathlib import Path

from organizer import Document
from ai_hybrid import assign_hybrid_folders


class HybridFolderTests(unittest.TestCase):
    def test_repeated_document_purpose_overrides_project_and_duplicate_follows(self):
        documents = [
            Document(Path("출장보고1.pdf"), "기존", "", "PDF 텍스트", lead_text="출장 결과"),
            Document(Path("출장보고2.pdf"), "기존", "", "PDF 텍스트", lead_text="출장 결과"),
            Document(Path("행정구역연구.pdf"), "기존", "", "PDF 텍스트", lead_text="개편 연구"),
            Document(Path("출장보고 복사.pdf"), "기존", "", "중복 파일",
                     duplicate_of=Path("출장보고1.pdf")),
        ]
        seen_folders = []

        def base_assign(copies, **kwargs):
            for doc in copies:
                doc.folder = "행정구역 개편"
                doc.subfolder = "2025"
                doc.folder_reason = "기존 AI"
            return 3

        def classify(items, folders, instructions_override=None):
            seen_folders.append(folders)
            return [
                ("출장보고" if "출장" in item["documents"][0]["name"] else "연구자료", "파일 용도")
                for item in items
            ]

        def embed(texts):
            return [
                [1.0, 0.0] if "출장" in text else [0.0, 1.0]
                for text in texts
            ]

        count = assign_hybrid_folders(
            documents, base_assign=base_assign, classify=classify, embed=embed,
        )
        self.assertEqual(count, 3)
        self.assertEqual([doc.folder for doc in documents],
                         ["출장보고", "출장보고", "행정구역 개편", "출장보고"])
        self.assertEqual([doc.subfolder for doc in documents],
                         ["", "", "2025", ""])
        self.assertTrue(all(folders == [] for folders in seen_folders))

    def test_uncertain_ai_label_preserves_existing_preview(self):
        documents = [
            Document(Path("인구1.pdf"), "기존", "", "PDF 텍스트", lead_text="인구"),
            Document(Path("인구2.pdf"), "기존", "", "PDF 텍스트", lead_text="인구"),
        ]

        def base_assign(copies, **kwargs):
            copies[0].folder = "미분류"
            copies[1].folder = "인구 자료"
            return 1

        count = assign_hybrid_folders(
            documents,
            base_assign=base_assign,
            classify=lambda items, folders, instructions_override=None:
                [("미분류", "단서 부족") for _ in items],
            embed=lambda texts: [[1.0, 0.0] for _ in texts],
        )
        self.assertEqual(count, 1)
        self.assertEqual([doc.folder for doc in documents], ["미분류", "인구 자료"])

    def test_unrelated_repeated_label_does_not_replace_supported_subject(self):
        documents = [
            Document(Path("소상공인 심사.pdf"), "기존", "", "PDF 텍스트", lead_text="심사"),
            Document(Path("인구 통계1.pdf"), "기존", "", "PDF 텍스트", lead_text="인구"),
            Document(Path("인구 통계2.pdf"), "기존", "", "PDF 텍스트", lead_text="인구"),
        ]

        def base_assign(copies, **kwargs):
            for doc in copies:
                doc.folder = "소상공인 정책" if "소상공인" in doc.source.name else "인구 통계"
            return 3

        def classify(items, folders, instructions_override=None):
            return [
                ("소상공인 심사" if "소상공인" in item["documents"][0]["name"]
                 else "인구데이터", "파일 용도")
                for item in items
            ]

        def embed(texts):
            return [
                [0.0, 1.0] if text == "폴더: 소상공인 정책" else [1.0, 0.0]
                for text in texts
            ]

        assign_hybrid_folders(
            documents, base_assign=base_assign, classify=classify, embed=embed,
        )
        self.assertEqual(documents[0].folder, "소상공인 정책")

    def test_strong_filename_subject_is_not_replaced_by_unrelated_purpose(self):
        documents = [
            Document(Path("행정구역 개편 연구.pdf"), "기존", "", "PDF 텍스트", lead_text="연구"),
            Document(Path("지출증빙1.pdf"), "기존", "", "PDF 텍스트", lead_text="지출"),
            Document(Path("지출증빙2.pdf"), "기존", "", "PDF 텍스트", lead_text="지출"),
        ]

        def base_assign(copies, **kwargs):
            for doc in copies:
                doc.folder = "행정구역 개편"
            return 3

        def classify(items, folders, instructions_override=None):
            return [("지출증빙", "AI 판단") for _ in items]

        def embed(texts):
            return [
                [0.0, 1.0] if text == "폴더: 행정구역 개편" else [1.0, 0.0]
                for text in texts
            ]

        assign_hybrid_folders(
            documents, base_assign=base_assign, classify=classify, embed=embed,
        )
        self.assertEqual(documents[0].folder, "행정구역 개편")
        self.assertEqual(documents[1].folder, "지출증빙")

    def test_ai_folder_name_rejects_file_format_suffix(self):
        from ai_hybrid import _normal_name
        self.assertEqual(_normal_name("교하동ppt"), "미분류")

    def test_cancellation_leaves_original_preview_untouched(self):
        document = Document(Path("출장보고.pdf"), "기존", "사용자 선택", "PDF 텍스트",
                            lead_text="출장")

        def base_assign(copies, **kwargs):
            copies[0].folder = "출장"
            return 1

        with self.assertRaises(InterruptedError):
            assign_hybrid_folders(
                [document], base_assign=base_assign,
                should_cancel=lambda: True,
            )
        self.assertEqual((document.folder, document.folder_reason), ("기존", "사용자 선택"))


    def test_equivalent_folder_names_merge_without_changing_subject(self):
        docs = [
            Document(Path("인구1.pdf"), "기존", "", "PDF 텍스트", lead_text="인구"),
            Document(Path("인구2.pdf"), "기존", "", "PDF 텍스트", lead_text="인구"),
        ]

        def base_assign(copies, **kwargs):
            copies[0].folder = "인구통계"
            copies[1].folder = "인구 통계"
            return 2

        assign_hybrid_folders(
            docs, base_assign=base_assign,
            classify=lambda items, folders, instructions_override=None:
                [("미분류", "불확실") for _ in items],
        )
        self.assertEqual([doc.folder for doc in docs], ["인구통계", "인구통계"])

    def test_explicit_meeting_document_type_blocks_survey_relabel(self):
        docs = [
            Document(Path("장유 2동 설문 설명회 요약.hwpx"), "기존", "", "HWPX 텍스트",
                     lead_text="설문 조사 내용"),
            Document(Path("장유 발전 협의회 현장 설문조사 회의록.hwpx"), "기존", "", "HWPX 텍스트",
                     lead_text="설문 조사 내용"),
        ]

        def base_assign(copies, **kwargs):
            copies[0].folder = "행정구역 개편"
            copies[1].folder = "인구 자료"
            return 2

        def embed(texts):
            return [[0.0, 1.0] if text in ("폴더: 행정구역 개편", "폴더: 인구 자료")
                    else [1.0, 0.0] for text in texts]

        assign_hybrid_folders(
            docs, base_assign=base_assign,
            classify=lambda items, folders, instructions_override=None:
                [("설문조사", "본문에 설문") for _ in items],
            embed=embed,
        )
        self.assertEqual([doc.folder for doc in docs], ["행정구역 개편", "인구 자료"])

    def test_research_suffix_merges_only_when_base_folder_exists(self):
        docs = [
            Document(Path("도시계획.pdf"), "기존", "", "PDF 텍스트", lead_text="계획"),
            Document(Path("도시계획연구.pdf"), "기존", "", "PDF 텍스트", lead_text="연구"),
            Document(Path("해양연구.pdf"), "기존", "", "PDF 텍스트", lead_text="연구"),
        ]

        def base_assign(copies, **kwargs):
            for doc, name in zip(copies, ("도시 계획", "도시 계획 연구", "해양 연구")):
                doc.folder = name
            return 3

        assign_hybrid_folders(
            docs, base_assign=base_assign,
            classify=lambda items, folders, instructions_override=None:
                [("미분류", "불확실") for _ in items],
        )
        self.assertEqual([doc.folder for doc in docs],
                         ["도시 계획", "도시 계획", "해양 연구"])

if __name__ == "__main__":
    unittest.main()
