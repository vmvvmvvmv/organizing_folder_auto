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

    def test_big_top_level_gets_middle_level(self):
        # 대분류 '채용' 안에 세부 주제 8개 → 중분류 2개로 한 번 더 나눈다. '재무'는 주제 8개 중 2개만 남아 두 단계.
        hiring = {f"채용{i}": 3 for i in range(8)}
        finance = {f"정산{i}": 3 for i in range(8)}
        docs = docs_for(hiring | finance | {"정산작은것": 1})
        calls = []

        def taxonomy(folders, minimum, maximum, parent=""):
            calls.append(parent)
            # 중분류 앞에 대분류 이름을 붙여 와도 떼어 낸다.
            return {"": ["채용 서류", "지출 정산"],
                    "채용 서류": ["채용 서류_입사지원서", "면접 기록"]}.get(parent, [])

        def pick(items, categories):
            if "입사지원서" in categories:
                return ["입사지원서" if int(row["folder"][-1]) < 4 else "면접 기록" for row in items]
            return ["채용 서류" if row["folder"].startswith("채용") else "지출 정산" for row in items]

        middles = {}
        plan = add_top_level(docs, taxonomy, pick, embed=None, middles=middles)
        self.assertEqual(calls, ["", "지출 정산", "채용 서류"])  # 지출 정산은 중분류 이름을 못 받음
        self.assertEqual(middles["채용0"], "입사지원서")
        routes = {(doc.folder, doc.subfolder, doc.detail) for doc in docs}
        self.assertIn(("채용 서류", "입사지원서", "채용0"), routes)
        self.assertIn(("채용 서류", "면접 기록", "채용7"), routes)
        self.assertIn(("지출 정산", "정산3", ""), routes)  # 중분류가 없으면 대분류 / 세부 주제
        self.assertIn(("지출 정산", "", ""), routes)  # 파일 1개짜리 주제는 대분류에 바로
        self.assertEqual(len(plan), 17)

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


class UserFolderTests(unittest.TestCase):
    def docs(self):
        from pathlib import Path
        from organizer import Document
        names = [("부가세 신고", "세금"), ("종합소득세 신고", "세금"), ("원천세 신고", "세금"),
                 ("3월 급여", "급여"), ("면접 일정", "채용"), ("여행 사진 설명", "미분류")]
        return [Document(Path(f"root/{name}.pdf"), topic, "AI 분류: 이유", "PDF 텍스트", lead_text=name)
                for name, topic in names]

    def test_folder_counts_skip_unclassified_and_format_folders(self):
        from ai_hierarchy import folder_counts
        routes = [("세금", "")] * 3 + [("채용", "면접")] * 2 + [("미분류", ""), ("GIS 자료", "")]
        counts = folder_counts(routes)
        self.assertEqual(counts.most_common(), [("세금", 3), ("채용", 2), ("채용/면접", 2)])

    def test_tree_and_target_list_round_trip(self):
        from folder_editor import targets_to_tree, tree_to_targets
        tree = targets_to_tree(["회계/세금", "인사", "회계/급여", "회계/세금"])
        self.assertEqual(tree, [("회계", ["세금", "급여"]), ("인사", [])])
        self.assertEqual(tree_to_targets(tree), ["회계/세금", "회계/급여", "인사"])

    def test_cut_off_category_names_lose_unclosed_parenthesis(self):
        from ai_hierarchy import _clean_categories
        self.assertEqual(_clean_categories(["교육·학습 자료들 (학", "인사·급여 (채용)", " 영업 성과 "], set()),
                         ["교육·학습 자료들", "인사·급여 (채용)", "영업 성과"])

    def test_parse_lines(self):
        from ai_hierarchy import parse_user_folders
        good, bad = parse_user_folders("회계\n 회계 / 세금 \n인사\채용\n\n회계\n../밖\n미분류\na/b/c")
        self.assertEqual(good, ["회계", "회계/세금", "인사/채용"])
        self.assertEqual(bad, ["../밖", "미분류", "a/b/c"])

    def test_topics_move_under_picked_user_folder(self):
        from ai_hierarchy import fit_user_folders
        docs = self.docs()
        seen = []

        def pick(items, categories):
            seen.append([item["folder"] for item in items])
            answers = {"급여": "회계", "채용": "인사"}
            return [next((v for k, v in answers.items() if k in item["folder"]), "기타") for item in items]

        fit_user_folders(docs, ["회계/세금", "회계", "인사"], pick)
        self.assertNotIn("세금", seen[0])           # 이름이 같은 주제는 묻지 않고 바로 넣는다
        routes = [(d.folder, d.subfolder, d.detail) for d in docs]
        self.assertEqual(routes[:3], [("회계", "세금", "")] * 3)  # 주제 이름이 하위 폴더와 같으면 한 단계 줄임
        self.assertEqual(routes[3], ("회계", "", ""))            # 파일 1개짜리 주제는 하위 폴더를 만들지 않음
        self.assertEqual(routes[4], ("인사", "", ""))
        self.assertEqual(routes[5], ("미분류", "", ""))          # 미분류 파일도 물었지만 맞는 곳 없음

    def test_big_topic_becomes_subfolder_of_user_folder(self):
        from pathlib import Path
        from ai_hierarchy import fit_user_folders
        from organizer import Document
        docs = [Document(Path(f"root/독일 출장 {i}.pdf"), "독일 출장", "", "PDF 텍스트") for i in range(3)]
        fit_user_folders(docs, ["해외영업"], lambda items, cats: ["해외영업"] * len(items))
        self.assertEqual({(d.folder, d.subfolder) for d in docs}, {("해외영업", "독일 출장")})

    def test_allow_new_keeps_ai_topic_when_nothing_fits(self):
        from ai_hierarchy import fit_user_folders
        docs = self.docs()
        fit_user_folders(docs, ["인사"], lambda items, cats: ["기타"] * len(items), allow_new=True)
        self.assertEqual([d.folder for d in docs][:3], ["세금"] * 3)

    def test_filename_naming_one_user_folder_wins_over_ai_topic(self):
        from pathlib import Path
        from ai_hierarchy import _named_target, fit_user_folders
        from organizer import Document
        targets = ["검수 문서", "회의록", "설문 조사", "메뉴와 레시피", "Invoices", "행정/출장 보고"]
        self.assertEqual(_named_target("물품검수조서(명함)", targets), "검수 문서")
        self.assertEqual(_named_target("출장 결과보고서 v2", targets), "행정/출장 보고")
        self.assertEqual(_named_target("신메뉴 레시피 정리", targets), "메뉴와 레시피")
        self.assertEqual(_named_target("invoice_2026_03", targets), "Invoices")
        self.assertIsNone(_named_target("설문조사 회의록", targets))   # 두 폴더를 가리키면 AI 판단에 맡긴다
        self.assertIsNone(_named_target("메뉴 사진", targets))        # 핵심 말이 다 있어야 한다
        docs = [Document(Path(f"root/{name}.pdf"), "행정구역 개편", "", "PDF 텍스트")
                for name in ("행정구역 개편 연구", "주간 회의록", "용역검수조서")]
        fit_user_folders(docs, ["행정구역 개편", "회의록", "검수 문서"], lambda items, cats: ["기타"] * len(items))
        self.assertEqual([d.folder for d in docs], ["행정구역 개편", "회의록", "검수 문서"])
        self.assertIn("파일명이 '회의록'", docs[1].folder_reason)


if __name__ == "__main__":
    unittest.main()
