import unittest
from pathlib import Path

from organizer import Document
from quality_score import quality_check


def doc(name, folder, text="", duplicate=False):
    d = Document(Path(f"root/{name}"), folder, "", "", lead_text=text)
    if duplicate:
        d.duplicate_of = Path("root/original")
    return d


class QualityCheckTests(unittest.TestCase):
    def test_tidy_result_has_no_warnings(self):
        docs = [doc(f"출장 보고 {i}.pdf", "출장", "출장 결과") for i in range(6)]
        docs.append(doc("사본.pdf", "미분류", duplicate=True))  # 중복본은 세지 않는다
        result = quality_check(docs)
        self.assertEqual(result.warnings, [])
        self.assertIn("눈에 띄는 문제 없음", result.summary())

    def test_each_known_failure_signal_is_reported(self):
        docs = ([doc(f"메모{i}.pdf", "미분류") for i in range(3)]
                + [doc(f"IMG_{i}.jpg", "사진·이미지") for i in range(4)]
                + [doc(f"파일{i}.pdf", f"주제{i}", "관계없는 내용") for i in range(3)])
        result = quality_check(docs)
        self.assertEqual(len(result.warnings), 4)
        self.assertAlmostEqual(result.unclassified, 0.3)
        self.assertAlmostEqual(result.images_unsorted, 0.4)
        self.assertEqual((result.small_folders, result.no_evidence), (1.0, 1.0))
        self.assertIn("확인해 보세요", result.summary())

    def test_format_folders_are_not_judged_by_topic_evidence(self):
        docs = [doc(f"area{i}.shp", "GIS 자료") for i in range(3)]
        self.assertEqual(quality_check(docs).warnings, [])

    def test_empty_preview(self):
        self.assertEqual(quality_check([]).warnings, [])


if __name__ == "__main__":
    unittest.main()
