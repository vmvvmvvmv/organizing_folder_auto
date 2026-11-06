import json
import unittest
from pathlib import Path
from unittest import mock

import ai_hierarchy
from ai_hierarchy import OTHER, add_top_level, category_range, ollama_pick
from organizer import Document


def docs_for(folders):
    return [Document(Path(f"root/{folder}_{index}.pdf"), folder, "AI 분류: 이유", "PDF 텍스트")
            for folder, count in folders.items() for index in range(count)]


class HierarchyTests(unittest.TestCase):
    def test_topic_folders_move_under_chosen_top_level(self):
        docs = docs_for({"대기시간 조회": 3, "대기시간 예측": 2, "요금제 기획": 2, "요금제 비교": 2,
                         "자기소개서": 2, "토익 노트": 1})
        docs.append(Document(Path("root/IMG_1.jpg"), "사진·이미지", "", "이미지 OCR"))
        picks = {"대기시간 조회": "매장 서비스", "대기시간 예측": "매장 서비스", "요금제 기획": "요금제",
                 "요금제 비교": "요금제", "자기소개서": "취업", "토익 노트": "엉뚱한 이름"}
        seen = {}

        def taxonomy(folders, minimum, maximum):
            seen["topics"] = [row["folder"] for row in folders]
            return ["매장 서비스", "요금제", "취업", OTHER]

        plan = add_top_level(docs, taxonomy, lambda items, cats: [picks[row["folder"]] for row in items],
                             embed=None, min_topics=6)
        self.assertNotIn("사진·이미지", seen["topics"])  # 형식 폴더는 대분류 대상이 아니다
        self.assertEqual(plan, {"대기시간 조회": "매장 서비스", "대기시간 예측": "매장 서비스",
                                "요금제 기획": "요금제", "요금제 비교": "요금제"})
        routes = {(doc.folder, doc.subfolder) for doc in docs}
        self.assertIn(("매장 서비스", "대기시간 조회"), routes)
        # 파일 2개뿐인 세부 주제는 하위 폴더를 만들지 않고 대분류 폴더에 바로 둔다.
        self.assertIn(("매장 서비스", ""), routes)
        self.assertNotIn(("매장 서비스", "대기시간 예측"), routes)
        # 세부 주제가 하나뿐인 대분류(취업)와 목록에 없는 답(토익)은 그대로 둔다.
        self.assertIn(("자기소개서", ""), routes)
        self.assertIn(("토익 노트", ""), routes)
        self.assertIn("대분류: 요금제", next(d for d in docs if d.folder == "요금제").folder_reason)

    def test_few_topics_or_failed_taxonomy_leave_preview_unchanged(self):
        docs = docs_for({"출장": 3, "회의": 3})
        self.assertEqual(add_top_level(docs, mock.Mock(), mock.Mock(), embed=None), {})
        # 주제 폴더가 15개 미만이면 대분류를 만들지 않는다(폴더 6~10개에서는 오히려 나빠졌다).
        docs = docs_for({f"주제{i}": 2 for i in range(14)})
        self.assertEqual(add_top_level(docs, mock.Mock(), mock.Mock(), embed=None), {})
        docs = docs_for({f"주제{i}": 2 for i in range(16)})
        failing = mock.Mock(side_effect=RuntimeError("Ollama 꺼짐"))
        self.assertEqual(add_top_level(docs, failing, mock.Mock(), embed=None), {})
        self.assertTrue(all(doc.subfolder == "" for doc in docs))

    def test_failed_pick_batch_is_split_and_asked_again(self):
        docs = docs_for({f"주제{i:02d}": 2 for i in range(16)})
        sizes = []

        def pick(items, categories):
            sizes.append(len(items))
            if len(items) > 4:
                raise RuntimeError("응답 id가 맞지 않음")
            return ["채용 서류" if int(row["folder"][2:]) % 2 else "지출 정산" for row in items]

        plan = add_top_level(docs, lambda *args: ["채용 서류", "지출 정산"], pick, embed=None)
        self.assertEqual(len(plan), 16)
        self.assertEqual(sizes[:3], [15, 7, 3])

    def test_topic_far_from_its_category_stays_on_top(self):
        docs = docs_for({f"요금제{i}": 2 for i in range(15)} | {"증명사진": 2})

        def embed(texts):
            return [[0.0, 1.0] if "증명사진" in text else [1.0, 0.0] for text in texts]

        scores = {}
        plan = add_top_level(docs, lambda *args: ["요금제 기획", "고객 상담"],
                             lambda items, cats: ["요금제 기획"] * len(items), embed=embed, scores=scores)
        self.assertNotIn("증명사진", plan)
        self.assertLess(scores["증명사진"], 0.5)
        self.assertEqual(len(plan), 15)

    def test_vague_category_names_are_dropped(self):
        docs = docs_for({f"주제{i:02d}": 2 for i in range(16)})
        seen = []

        def pick(items, categories):
            seen.append(list(categories))
            return [categories[0]] * len(items)

        add_top_level(docs, lambda *args: ["업무 운영", "기술", "채용 서류", "지출 정산"], pick, embed=None)
        # '업무 운영'·'기술'처럼 넓은 말뿐인 이름은 안에 무엇이 있는지 알 수 없어 고를 수 없다.
        self.assertEqual(seen[0], ["채용 서류", "지출 정산"])

    def test_category_count_grows_with_topic_count(self):
        self.assertEqual(category_range(6), (3, 4))
        self.assertEqual(category_range(30), (3, 8))
        self.assertEqual(category_range(100), (3, 10))

    def test_pick_constrains_answers_to_the_category_list(self):
        response = {"message": {"content": json.dumps({"items": [{"id": 0, "category": "요금제"}]})}}
        with mock.patch.object(ai_hierarchy, "_ollama_request", return_value=response) as request:
            self.assertEqual(ollama_pick([{"id": 0, "folder": "요금제 기획"}], ["요금제", "취업"]), ["요금제"])
        schema = request.call_args.args[2]["format"]
        enum = schema["properties"]["items"]["items"]["properties"]["category"]["enum"]
        self.assertEqual(enum, ["요금제", "취업", OTHER])


if __name__ == "__main__":
    unittest.main()
