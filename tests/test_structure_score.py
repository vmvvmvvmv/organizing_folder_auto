import unittest
from pathlib import Path

from organizer import Document
from structure_score import structure_score


def place(folder, subfolder="", count=1, name=None):
    docs = []
    name = name or (subfolder or folder)  # 파일 이름에 폴더 이름의 단어가 들어 있다
    for index in range(count):
        doc = Document(Path(f"root/{folder}/{subfolder}/{name}{index}.pdf"), folder, "", "")
        doc.subfolder = subfolder
        docs.append(doc)
    return docs


class StructureScoreTests(unittest.TestCase):
    def test_tidy_function_based_structure_scores_full(self):
        docs = (place("인사", "채용", 4) + place("인사", "급여", 3) + place("재무", "지출증빙", 5)
                + place("영업", "견적", 3) + place("영업", "계약", 3))
        result = structure_score(docs)
        self.assertEqual(result.score, 100)

    def test_scattered_format_based_structure_scores_low(self):
        docs = [doc for i in range(45) for doc in place(f"대기시간 자료{i}", count=1, name="메모")]
        docs += place("사진·이미지", count=30) + place("미분류", count=25)
        result = structure_score(docs)
        self.assertEqual(result.parts["최상위 폴더 수"], 0.0)  # 46개: 40개 넘음
        self.assertEqual(result.parts["이름 겹침 없음"], 0.0)  # 모두 '대기시간' 공유
        self.assertLessEqual(result.score, 45)
        self.assertIn("최상위 폴더 수 0%", result.summary())

    def test_vague_names_and_names_unrelated_to_files_lose_points(self):
        clear = structure_score(place("채용 서류", count=3) + place("지출 정산", count=3))
        vague = structure_score(place("업무 운영", count=3, name="입사지원서")
                                + place("기술 자료", count=3, name="영수증"))
        self.assertEqual((clear.parts["구체적 이름"], clear.parts["이름 대표성"]), (1.0, 1.0))
        self.assertEqual((vague.parts["구체적 이름"], vague.parts["이름 대표성"]), (0.0, 0.0))

    def test_too_many_subfolders_in_one_parent(self):
        docs = [doc for i in range(30) for doc in place("업무", f"세부{i}", 3)]
        self.assertEqual(structure_score(docs).parts["하위 폴더 수"], 0.0)


if __name__ == "__main__":
    unittest.main()
