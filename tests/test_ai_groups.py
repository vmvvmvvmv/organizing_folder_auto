import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_groups import (
    EMBED_MODEL, MODEL, ContextSizeError, IncompleteResultError,
    assign_ai_folders, ollama_classify, ollama_embeddings,
)
import ai_groups
from app import ai_preview_note
from organizer import Document, scan


class AiGroupTests(unittest.TestCase):
    def test_merges_folder_names_with_only_spacing_differences(self):
        docs = [
            Document(Path("root/출장 보고서 하나.pdf"), "기존", "", "PDF 텍스트", lead_text="출장 보고서"),
            Document(Path("root/출장보고서 둘.pdf"), "기존", "", "PDF 텍스트", lead_text="출장보고서"),
        ]
        known_seen = []

        def classify(items, known):
            known_seen.append(known)
            name = "출장 보고" if len(known_seen) == 1 else "출장보고"
            return [(name, "출장 보고서")]

        assign_ai_folders(
            docs,
            embed=lambda texts: [[1.0, 0.0], [0.0, 1.0]],
            classify=classify,
            label_batch_size=1,
        )
        self.assertEqual(known_seen, [[], ["출장 보고"]])
        self.assertEqual([doc.folder for doc in docs], ["출장 보고", "출장 보고"])
    def test_rechecks_coherent_group_with_unsupported_folder_name(self):
        docs = [
            Document(Path(f"root/장유지역 행정구역 개편 GIS 분석 {i}.pdf"), "기존", "", "PDF 텍스트",
                     lead_text="장유지역 행정구역 개편 공간정보 분석")
            for i in range(3)
        ]
        calls = []

        def classify(items, known):
            calls.append(len(items))
            return [("민원 관리", "기존 이름") if len(calls) == 1
                    else ("행정구역 개편", "파일명과 본문")]

        assign_ai_folders(docs, embed=lambda texts: [[1.0, 0.0] for _ in texts],
                          classify=classify)
        self.assertEqual(calls, [1, 1])
        self.assertEqual([doc.folder for doc in docs], ["행정구역 개편"] * 3)

    def test_failed_recheck_keeps_coherent_scan_suggestion(self):
        docs = [
            Document(Path(f"root/장유지역 행정구역 개편 GIS 분석 {i}.pdf"),
                     "행정구역", "공통 제목", "PDF 텍스트",
                     lead_text="장유지역 행정구역 개편 공간정보 분석")
            for i in range(3)
        ]
        calls = []

        def classify(items, known):
            calls.append(len(items))
            if len(calls) == 2:
                raise RuntimeError("Ollama 응답 실패")
            return [("민원 관리", "기존 이름")]

        assign_ai_folders(docs, embed=lambda texts: [[1.0, 0.0] for _ in texts],
                          classify=classify)
        self.assertEqual(calls, [1, 1])
        self.assertEqual([doc.folder for doc in docs], ["행정구역"] * 3)

    def test_low_information_mixed_group_keeps_scan_suggestions(self):
        names = ["Desktop.zip", "Word.lnk", "화면자료.png", "음악파일.m4a",
                 "노트자료.md", "기본정보.xml", "장면사진.jpg", "서식예시.hwp"]
        previous = ["압축 파일", "바로가기", "미분류", "녹음 파일",
                    "기타 문서", "기타 문서", "미분류", "기타 문서"]
        docs = [Document(Path(name), folder, "기존 제안", "파일명·형식")
                for name, folder in zip(names, previous)]
        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0] for _ in texts],
            classify=lambda items, known: [("민원 관리", "유사한 업무")],
        )
        self.assertEqual([doc.folder for doc in docs], previous)

    def test_ollama_chat_uses_local_structured_response(self):
        response = mock.Mock(status=200)
        response.read.return_value = json.dumps({
            "message": {"content": json.dumps({"items": [
                {"id": 0, "folder": "출장", "reason": "현장 방문"}
            ]})}
        }).encode()
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("ai_groups.http.client.HTTPConnection", return_value=connection):
            result = ollama_classify([{"id": 0, "documents": [{"name": "보고.pdf"}]}], [])
        self.assertEqual(result, [("출장", "현장 방문")])
        args, _ = connection.request.call_args
        self.assertEqual(args[:2], ("POST", "/api/chat"))
        payload = json.loads(args[2])
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["format"]["properties"]["items"]["minItems"], 1)
        self.assertEqual(payload["format"]["properties"]["items"]["items"]["properties"]["reason"]["maxLength"], 80)
        self.assertIn("같은 업무나 주제", payload["messages"][0]["content"])
        connection.close.assert_called_once()

    def test_ollama_embed_rejects_missing_vector(self):
        response = mock.Mock(status=200)
        response.read.return_value = b'{"embeddings":[]}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("ai_groups.http.client.HTTPConnection", return_value=connection):
            with self.assertRaises(IncompleteResultError):
                ollama_embeddings(["문서"])

    def test_http_context_error_retries_smaller_embedding_batches(self):
        docs = [
            Document(Path(f"출장{index}.pdf"), "기존", "", "PDF 텍스트", lead_text="출장")
            for index in range(4)
        ]
        sizes = []

        def embed(texts):
            sizes.append(len(texts))
            if len(texts) > 2:
                raise ContextSizeError("문맥 초과")
            return [[1.0, 0.0] for _ in texts]

        self.assertEqual(assign_ai_folders(
            docs, embed=embed, classify=lambda groups, folders: [("출장", "방문")],
            batch_size=4,
        ), 4)
        self.assertEqual(sizes, [4, 2, 2])

    def test_incomplete_label_response_retries_smaller_batches(self):
        docs = [
            Document(Path("출장.pdf"), "기존", "", "PDF 텍스트", lead_text="출장"),
            Document(Path("계약.pdf"), "기존", "", "PDF 텍스트", lead_text="계약"),
        ]
        sizes = []

        def label(groups, folders):
            sizes.append(len(groups))
            if len(groups) > 1:
                raise IncompleteResultError("응답 누락")
            return [("출장" if groups[0]["documents"][0]["name"] == "출장.pdf" else "계약", "제목")]

        assign_ai_folders(docs, embed=lambda texts: [[1.0, 0.0], [0.0, 1.0]],
                          classify=label, label_batch_size=2)
        self.assertEqual(sizes, [2, 1, 1])
        self.assertEqual([doc.folder for doc in docs], ["출장", "계약"])

    def test_label_batch_recovers_after_one_incomplete_response(self):
        docs = [Document(Path(f"분류 {i}.pdf"), "기존", "", "PDF 텍스트",
                         lead_text=f"분류 {i}") for i in range(10)]
        position = 0
        sizes = []

        def embed(texts):
            nonlocal position
            result = [[float(index == dimension) for dimension in range(10)]
                      for index in range(position, position + len(texts))]
            position += len(texts)
            return result

        def classify(items, known):
            sizes.append(len(items))
            if len(sizes) == 1:
                raise IncompleteResultError("일시적인 응답 누락")
            return [(item["documents"][0]["name"].removesuffix(".pdf"), "제목")
                    for item in items]

        assign_ai_folders(docs, embed=embed, classify=classify,
                          label_batch_size=4)
        self.assertEqual(sizes, [4, 2, 2, 2, 4])
        self.assertEqual([doc.folder for doc in docs], [f"분류 {i}" for i in range(10)])

    def test_label_batch_recovery_restarts_after_second_failure(self):
        docs = [Document(Path(f"분류 {i}.pdf"), "기존", "", "PDF 텍스트",
                         lead_text=f"분류 {i}") for i in range(9)]
        position = 0
        sizes = []

        def embed(texts):
            nonlocal position
            result = [[float(index == dimension) for dimension in range(9)]
                      for index in range(position, position + len(texts))]
            position += len(texts)
            return result

        def classify(items, known):
            sizes.append(len(items))
            if len(sizes) in (1, 4):
                raise IncompleteResultError("일시적인 응답 누락")
            return [(item["documents"][0]["name"].removesuffix(".pdf"), "제목")
                    for item in items]

        assign_ai_folders(docs, embed=embed, classify=classify,
                          label_batch_size=4)
        self.assertEqual(sizes, [4, 2, 2, 2, 1, 1, 1, 2])
    def test_single_missing_label_result_is_unclassified(self):
        doc = Document(Path("출장.pdf"), "기존", "", "PDF 텍스트", lead_text="출장")
        count = assign_ai_folders(
            [doc], embed=lambda texts: [[1.0, 0.0]],
            classify=lambda groups, folders: (_ for _ in ()).throw(IncompleteResultError("누락")),
        )
        self.assertEqual(count, 0)
        self.assertEqual(doc.folder, "미분류")

    def test_invalid_folder_keeps_original_preview(self):
        doc = Document(Path("출장.pdf"), "기존", "", "PDF 텍스트", lead_text="출장")
        with self.assertRaises(ValueError):
            assign_ai_folders(
                [doc], embed=lambda texts: [[1.0, 0.0]],
                classify=lambda groups, folders: [("../밖", "제목")],
            )
        self.assertEqual(doc.folder, "기존")

    def test_ai_folder_name_is_readable_in_preview(self):
        doc = Document(Path("정책.pdf"), "기존", "", "PDF 텍스트", lead_text="정책")
        assign_ai_folders(
            [doc], embed=lambda texts: [[1.0, 0.0]],
            classify=lambda groups, folders: [("소상공인_정책", "본문")],
        )
        self.assertEqual(doc.folder, "소상공인 정책")

    def test_ai_only_asks_local_model_about_name_only_files(self):
        doc = Document(Path("meeting.m4a"), "미분류", "AI 대기", "파일명·형식")
        inputs = []
        def embed(texts):
            inputs.extend(texts)
            return [[1.0, 0.0]]
        assign_ai_folders(
            [doc], ai_only=True, embed=embed,
            classify=lambda items, known: [("녹음 파일", "확장자와 파일명")],
        )
        self.assertIn(".m4a", inputs[0])
        self.assertEqual(doc.folder, "녹음 파일")
        self.assertTrue(doc.folder_reason.startswith("AI 분류:"))

    def test_ai_only_uses_parent_name_for_numbered_images(self):
        doc = Document(Path("회의자료/1.png"), "미분류", "AI 대기", "이미지 OCR")
        assign_ai_folders(
            [doc], ai_only=True, embed=lambda texts: [[1.0, 0.0]],
            classify=lambda items, known: [("회의", "상위 폴더")],
        )
        self.assertEqual(doc.folder, "회의")

    def test_ai_only_normalizes_supported_type_label(self):
        doc = Document(Path("area.geojson"), "미분류", "AI 대기", "파일명·형식")
        assign_ai_folders(
            [doc], ai_only=True, embed=lambda texts: [[1.0, 0.0]],
            classify=lambda items, known: [("GIS_경계_데이터", "확장자와 파일명")],
        )
        self.assertEqual(doc.folder, "GIS 자료")

    def test_ai_only_retries_readable_unclassified_document_individually(self):
        doc = Document(
            Path("report.pdf"), "미분류", "", "PDF 텍스트",
            lead_text="출장 현장 점검 결과를 정리한 보고서입니다. " * 5,
        )
        calls = []
        def classify(items, known):
            calls.append(len(items))
            return [("무관", "묶음 제목 오류")] if len(calls) == 1 else [("출장 보고서", "출장 결과")]
        assign_ai_folders(
            [doc], ai_only=True, embed=lambda texts: [[1.0, 0.0]],
            classify=classify, subdivide=lambda items, parent: [("", "") for _ in items],
        )
        self.assertEqual(doc.folder, "출장 보고서")
        self.assertIn("재판정", doc.folder_reason)
        self.assertEqual(calls, [1, 1])

    def test_ai_only_retry_rejects_new_name_without_evidence(self):
        doc = Document(
            Path("a.pdf"), "미분류", "", "PDF 텍스트",
            lead_text="회의 일정과 참석자 명단을 정리한 내용입니다. " * 4,
        )
        answers = iter([[("예산", "추정")], [("우주 탐사 계획", "근거 없음")]])
        assign_ai_folders(
            [doc], ai_only=True, embed=lambda texts: [[1.0, 0.0]],
            classify=lambda items, known: next(answers),
            subdivide=lambda items, parent: [("", "") for _ in items],
        )
        self.assertEqual(doc.folder, "미분류")

    def test_ai_only_retry_may_pick_existing_folder(self):
        docs = [
            Document(Path("출장 계획.pdf"), "미분류", "", "PDF 텍스트", lead_text="출장"),
            Document(Path("b.pdf"), "미분류", "", "PDF 텍스트",
                     lead_text="현장 방문 일정과 숙소, 교통편을 정리했습니다. " * 4),
        ]
        answers = iter([[("출장", "파일명")], [("예산", "추정")], [("출장", "현장 방문 일정")]])
        assign_ai_folders(
            docs, ai_only=True, embed=lambda texts: [[1.0, 0.0], [0.0, 1.0]],
            classify=lambda items, known: next(answers),
            subdivide=lambda items, parent: [("", "") for _ in items],
            label_batch_size=1,
        )
        self.assertEqual([doc.folder for doc in docs], ["출장", "출장"])

    def test_ai_only_retry_asks_one_document_per_request(self):
        docs = [
            Document(Path(f"{name}.pdf"), "미분류", "", "PDF 텍스트",
                     lead_text=f"{name} 관련 내용을 길게 정리한 문서입니다. " * 4)
            for name in ("가나다", "라마바", "사아자")
        ]
        sizes = []

        def classify(items, known):
            sizes.append(len(items))
            return [("미분류", "단서 부족") for _ in items]

        assign_ai_folders(
            docs, ai_only=True, embed=lambda texts: [[1.0, 0.0]] * len(texts),
            classify=classify, subdivide=lambda items, parent: [("", "") for _ in items],
        )
        self.assertEqual(sizes, [1, 1, 1, 1])  # 묶음 1회 + 재판정 3회

    def test_ai_only_retry_never_puts_readable_document_in_type_folder(self):
        docs = [
            Document(Path("maps/area.geojson"), "미분류", "", "파일명·형식"),
            Document(Path("물품검수조서_A4.jpg"), "미분류", "", "이미지 OCR",
                     lead_text="사무용품 구입 물품 검수 결과를 기록한 문서입니다. " * 4),
        ]
        seen = []

        def classify(items, known):
            seen.append(list(known))
            return [("GIS 자료", "형식") for _ in items]

        assign_ai_folders(
            docs, ai_only=True, embed=lambda texts: [[1.0, 0.0], [0.0, 1.0]],
            classify=classify, subdivide=lambda items, parent: [("", "") for _ in items],
            label_batch_size=1,
        )
        self.assertEqual([doc.folder for doc in docs], ["GIS 자료", "미분류"])
        self.assertNotIn("GIS 자료", seen[-1])  # 재판정 후보 목록에서 형식 폴더 제외

    def test_small_folder_matches_nearest_large_folder(self):
        from ai_groups import _small_folder_matches
        docs = [Document(Path(f"{index}.pdf"), "", "", "") for index in range(6)]
        folders = ["출장 결과"] * 3 + ["출장 관리", "예비군 교육", "GIS 자료"]
        vectors = [[1.0, 0.0, 0.0]] * 3 + [[0.6, 0.8, 0.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]]
        matches = _small_folder_matches(
            [(doc, folder, "") for doc, folder in zip(docs, folders)],
            {doc.source: vector for doc, vector in zip(docs, vectors)}, {"gis자료"},
        )
        self.assertEqual(set(matches), {"출장 관리", "예비군 교육"})  # 형식 폴더 제외
        self.assertEqual(matches["출장 관리"], ("출장 결과", 0.6))
        self.assertEqual(matches["예비군 교육"], ("출장 결과", 0.0))

    def test_folders_merge_only_with_shared_name_word_and_similar_content(self):
        from ai_groups import _consolidated_names, _consolidation_pairs
        names = ["대기시간 조회 시스템"] * 3 + ["대기시간 예측"] * 2 + ["대기시간 이미지", "요금제 기획"]
        vectors = [[1.0, 0.0, 0.0]] * 3 + [[0.9, 0.1, 0.0]] * 2 + [[0.0, 0.0, 1.0], [0.95, 0.05, 0.0]]
        docs = [Document(Path(f"{index}.pdf"), "", "", "") for index in range(len(names))]
        proposals = [(doc, name, "") for doc, name in zip(docs, names)]
        pairs = _consolidation_pairs(proposals, {doc.source: v for doc, v in zip(docs, vectors)}, set())
        # '요금제 기획'은 내용이 비슷해도 이름에 같은 단어가 없어 후보가 아니다.
        self.assertEqual({(left, right) for _, left, right in pairs}, {
            ("대기시간 예측", "대기시간 이미지"), ("대기시간 예측", "대기시간 조회 시스템"),
            ("대기시간 이미지", "대기시간 조회 시스템"),
        })
        accepted = [(left, right) for score, left, right in pairs if score >= 0.75]
        # 내용이 다른 '대기시간 이미지'는 남고, 작은 폴더가 큰 폴더 이름으로 합쳐진다.
        self.assertEqual(_consolidated_names(proposals, accepted), {"대기시간 예측": "대기시간 조회 시스템"})

    def test_ai_only_merges_only_close_small_folders(self):
        docs = [Document(Path(f"{name}.pdf"), "미분류", "", "PDF 텍스트", lead_text=name)
                for name in ("출장 1", "출장 2", "출장 3", "출장 관리", "예비군")]
        answers = iter([("출장", "출장")] * 3 + [("출장 관리", "출장"), ("예비군", "예비군")])
        with mock.patch("ai_groups._small_folder_matches", return_value={
            "출장 관리": ("출장", 0.9), "예비군": ("출장", 0.3),
        }):
            assign_ai_folders(
                docs, ai_only=True,
                embed=lambda texts: [[float(index == position) for position in range(5)]
                                     for index in range(len(texts))],
                classify=lambda items, known: [next(answers) for _ in items],
                subdivide=lambda items, parent: [("", "") for _ in items],
                label_batch_size=1,
            )
        self.assertEqual([doc.folder for doc in docs], ["출장"] * 4 + ["예비군"])
        self.assertIn("작은 폴더 병합: 출장 관리", docs[3].folder_reason)

    def test_unplaced_images_stay_together_by_original_folder(self):
        docs = [
            Document(Path("root/매장사진/IMG_1.jpg"), "미분류", "", "이미지 OCR"),
            Document(Path("root/매장사진/IMG_2.jpg"), "미분류", "", "이미지 OCR"),
            Document(Path("root/외톨이/IMG_3.png"), "미분류", "", "이미지 OCR"),
            Document(Path("root/screen.png"), "미분류", "", "이미지 OCR"),
            Document(Path("root/출장 사진.jpg"), "미분류", "", "이미지 OCR"),
        ]
        assign_ai_folders(
            docs, ai_only=True,
            embed=lambda texts: [[float(i == j) for j in range(5)] for i in range(len(texts))],
            classify=lambda items, known: [
                ("출장", "파일명") if "출장" in item["documents"][0]["name"] else ("미분류", "단서 없음")
                for item in items
            ],
            subdivide=lambda items, parent: [("", "") for _ in items],
        )
        self.assertEqual([(doc.folder, doc.subfolder) for doc in docs], [
            ("사진·이미지", "매장사진"), ("사진·이미지", "매장사진"),  # 원래 함께 있던 사진
            ("사진·이미지", ""),  # 혼자 있던 사진은 하위 폴더를 만들지 않는다
            ("사진·이미지", ""),  # 원본 폴더 바로 아래 사진
            ("출장", ""),  # 근거가 있으면 주제 폴더
        ])
        self.assertIn("원래 폴더 유지", docs[0].folder_reason)

    def test_ai_only_puts_unlabelled_videos_in_type_folder(self):
        docs = [
            Document(Path("Cap_triggers_ball_roll_1080p.mp4"), "미분류", "", "파일명·형식"),
            Document(Path("출장 영상.mp4"), "미분류", "", "파일명·형식"),
        ]
        assign_ai_folders(
            docs, ai_only=True, embed=lambda texts: [[1.0, 0.0], [0.0, 1.0]],
            classify=lambda items, known: [
                ("출장", "파일명") if "출장" in item["documents"][0]["name"] else ("영상 제작", "근거 없음")
                for item in items
            ],
            subdivide=lambda items, parent: [("", "") for _ in items],
        )
        # 근거가 있는 영상은 주제 폴더, 없는 영상은 미분류 대신 형식 폴더에 둔다.
        self.assertEqual([doc.folder for doc in docs], ["동영상", "출장"])
        self.assertIn("파일 형식", docs[0].folder_reason)

    def test_ai_only_keeps_topic_folder_for_gis_components(self):
        docs = [
            Document(Path("도시계획/도시계획_경계.shp"), "미분류", "", "파일명·형식"),
            Document(Path("도시계획/도시계획_경계.dbf"), "미분류", "", "파일명·형식"),
        ]
        assign_ai_folders(
            docs, ai_only=True, embed=lambda texts: [[1.0, 0.0]] * len(texts),
            classify=lambda items, known: [("도시계획", "파일명") for _ in items],
            subdivide=lambda items, parent: [("", "") for _ in items],
        )
        self.assertEqual([doc.folder for doc in docs], ["도시계획", "도시계획"])

    def test_ai_only_normalizes_geographic_ai_label(self):
        doc = Document(Path("area.geojson"), "미분류", "", "파일명·형식")
        assign_ai_folders(
            [doc], ai_only=True, embed=lambda texts: [[1.0, 0.0]],
            classify=lambda items, known: [("인구 및 지리 데이터", "지리 데이터")],
        )
        self.assertEqual(doc.folder, "GIS 자료")

    def test_ai_only_keeps_related_gis_components_in_one_folder(self):
        docs = [
            Document(Path("maps/area.shp"), "미분류", "", "파일명·형식"),
            Document(Path("maps/area.dbf"), "미분류", "", "파일명·형식"),
        ]
        assign_ai_folders(
            docs, ai_only=True, embed=lambda texts: [[1.0, 0.0], [0.0, 1.0]],
            classify=lambda items, known: [("GIS 경계 데이터", "공간 정보") for _ in items],
        )
        self.assertEqual([doc.folder for doc in docs], ["GIS 자료", "GIS 자료"])

    def test_ai_only_checks_mixed_group_per_file(self):
        docs = [
            Document(Path("maps/area.geojson"), "미분류", "", "파일명·형식"),
            Document(Path("other/0001.png"), "미분류", "", "이미지 OCR"),
        ]
        assign_ai_folders(
            docs, ai_only=True, embed=lambda texts: [[1.0, 0.0], [1.0, 0.0]],
            classify=lambda items, known: [("GIS 자료", "공간 정보") for _ in items],
            subdivide=lambda items, parent: [("", "") for _ in items],
        )
        # 글자 없는 이미지는 GIS 자료가 아니라 사진 폴더로 간다.
        self.assertEqual([doc.folder for doc in docs], ["GIS 자료", "사진·이미지"])

    def test_ai_only_rejects_parent_folder_copy_for_gis_files(self):
        doc = Document(Path("maps/area.geojson"), "미분류", "AI 대기", "파일명·형식")
        assign_ai_folders(
            [doc], ai_only=True, embed=lambda texts: [[1.0, 0.0]],
            classify=lambda items, known: [("maps", "상위 폴더명")],
        )
        # 상위 폴더명은 거부하고, 형식이 분명한 구성 파일이므로 형식 폴더에 둔다.
        self.assertEqual(doc.folder, "GIS 자료")
        self.assertIn("파일 형식", doc.folder_reason)

    def test_ai_only_removes_previous_keyword_folder_when_ai_has_no_answer(self):
        doc = Document(Path("출장 보고서.pdf"), "출장", "공통 단어", "PDF 텍스트", lead_text="출장")
        assign_ai_folders(
            [doc], ai_only=True, embed=lambda texts: [[1.0, 0.0]],
            classify=lambda groups, folders: [("미분류", "단서 부족")],
        )
        self.assertEqual(doc.folder, "미분류")

    def test_preview_stops_when_local_ai_fails(self):
        doc = Document(Path("출장.pdf"), "미분류", "AI 대기", "PDF 텍스트")
        with mock.patch("app.assign_ai_folders", side_effect=RuntimeError("연결 실패")):
            with self.assertRaisesRegex(RuntimeError, "연결 실패"):
                ai_preview_note([doc], enabled=True)
        self.assertEqual(doc.folder, "미분류")

    def test_preview_forwards_progress_and_cancellation(self):
        doc = Document(Path("출장.pdf"), "기존", "", "PDF 텍스트")
        progress = lambda done, total: None
        cancel = lambda: False
        with mock.patch("app.assign_ai_folders", return_value=1) as assign:
            note = ai_preview_note([doc], True, progress=progress, should_cancel=cancel)
        self.assertIn("AI 분류 1개", note)
        assign.assert_called_once_with([doc], progress=progress, should_cancel=cancel, ai_only=True)

    def test_preview_does_not_swallow_cancellation(self):
        doc = Document(Path("출장.pdf"), "기존", "", "PDF 텍스트")
        with mock.patch("app.assign_ai_folders", side_effect=InterruptedError("중단")):
            with self.assertRaises(InterruptedError):
                ai_preview_note([doc], True)

    def test_scan_keeps_enough_text_for_embedding(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "long.pdf").write_bytes(b"sample")
            with mock.patch("organizer.read_document", return_value=("가" * 1200, "PDF 텍스트")):
                documents = scan(source, Path(temporary) / "result", None)
        self.assertEqual(len(documents[0].lead_text), 1200)


    def test_local_ai_keeps_qgis_metadata_and_notebooks_out_of_topic_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            qmd = Path(temporary) / "map.qmd"
            qmd.write_text("<qgis version='3.40'></qgis>", encoding="utf-8")
            docs = [
                Document(qmd, "기존", "", "파일명·형식"),
                Document(Path("seoul_pop.ipynb"), "기존", "", "파일명·형식"),
                Document(Path("출장 계획.pdf"), "기존", "", "PDF 텍스트", lead_text="출장 계획"),
            ]
            calls = []

            def embed(texts):
                calls.append(len(texts))
                return [[1.0, 0.0] for _ in texts]

            assign_ai_folders(docs, embed=embed,
                              classify=lambda items, known: [("출장", "출장 계획") for _ in items])
        self.assertEqual(calls, [1])
        self.assertEqual([doc.folder for doc in docs], ["GIS 자료", "분석 노트북", "출장"])

    def test_local_ai_keeps_recordings_in_plain_type_folder(self):
        docs = [
            Document(Path("회의 녹음.m4a"), "기존", "", "파일명·형식"),
            Document(Path("출장 계획.pdf"), "기존", "", "PDF 텍스트", lead_text="출장 계획"),
        ]
        seen = []

        def embed(texts):
            seen.append(len(texts))
            return [[1.0, 0.0] for _ in texts]

        assign_ai_folders(docs, embed=embed,
                          classify=lambda items, known: [("출장", "출장 계획") for _ in items])
        self.assertEqual(seen, [1])
        self.assertEqual([doc.folder for doc in docs], ["녹음 파일", "출장"])
    def test_font_file_uses_configured_type_folder_without_ai_label(self):
        docs = [
            Document(Path("NanumFontSetup.otf"), "기존", "", "파일명·형식"),
            Document(Path("출장.pdf"), "기존", "", "PDF 텍스트", lead_text="출장"),
        ]
        calls = []

        def embed(texts):
            calls.extend(texts)
            return [[1.0, 0.0] for _ in texts]

        assign_ai_folders(
            docs, embed=embed,
            classify=lambda groups, folders: [("출장", "출장 결과") for _ in groups],
        )
        self.assertEqual(docs[0].folder, "글꼴")
        self.assertEqual(docs[1].folder, "출장")
        self.assertEqual(len(calls), 1)

if __name__ == "__main__":
    unittest.main()
