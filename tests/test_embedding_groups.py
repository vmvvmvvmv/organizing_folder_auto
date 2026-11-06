import unittest
from pathlib import Path
from unittest import mock

from ai_groups import assign_ai_folders, ollama_embeddings
from organizer import Document


def document(name, text="", folder="기존"):
    return Document(Path(name), folder, "기존 분류", "PDF 텍스트", lead_text=text)


class EmbeddingGroupTests(unittest.TestCase):
    def test_similar_documents_are_named_once_per_group(self):
        docs = [
            document("root/출장 계획.pdf", "방문 일정"),
            document("root/출장 결과.pdf", "방문 결과"),
            document("root/지출 영수증.pdf", "결제 내역"),
        ]
        seen = []

        def label(groups, folders):
            seen.extend(groups)
            return [("출장", "방문 문서"), ("지출 증빙", "결제 문서")]

        count = assign_ai_folders(
            docs,
            embed=lambda texts: [[1.0, 0.0], [0.99, 0.1], [0.0, 1.0]],
            classify=label,
        )
        self.assertEqual(count, 3)
        self.assertEqual([doc.folder for doc in docs], ["출장", "출장", "지출 증빙"])
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(seen[0]["documents"]), 2)
        self.assertEqual(len(seen[1]["documents"]), 1)

    def test_each_selected_root_starts_without_previous_folders(self):
        calls = []

        def label(groups, folders):
            calls.append(list(folders))
            return [("새 주제", "문서 내용")]

        for root in ("root_a", "root_b"):
            assign_ai_folders(
                [document(f"{root}/보고.pdf", "보고 내용")],
                embed=lambda texts: [[1.0, 0.0]],
                classify=label,
            )
        self.assertEqual(calls, [[], []])

    def test_hidden_structured_and_duplicate_files_are_not_embedded(self):
        hidden = document(".gitignore")
        gis = document("map.shp")
        report = document("출장.pdf", "출장 결과")
        duplicate = document("출장 복사.pdf")
        duplicate.duplicate_of = report.source
        names = []

        def embed(texts):
            names.extend(texts)
            return [[1.0, 0.0]]

        count = assign_ai_folders(
            [hidden, gis, report, duplicate], embed=embed,
            classify=lambda groups, folders: [("출장", "방문 결과")],
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(names), 1)
        self.assertEqual([doc.folder for doc in (hidden, gis, report, duplicate)],
                         ["버전 관리 설정", "GIS 자료", "출장", "출장"])

    def test_embedding_error_keeps_original_preview(self):
        docs = [document("a.pdf", "출장"), document("b.pdf", "지출")]

        def fail(texts):
            raise RuntimeError("Ollama 연결 실패")

        with self.assertRaises(RuntimeError):
            assign_ai_folders(docs, embed=fail)
        self.assertEqual([doc.folder for doc in docs], ["기존", "기존"])

    def test_cancel_after_embedding_batch_keeps_original_preview(self):
        docs = [document(f"{index}.pdf", "출장 내용") for index in range(3)]
        progress = []
        with self.assertRaises(InterruptedError):
            assign_ai_folders(
                docs, embed=lambda texts: [[1.0, 0.0] for _ in texts],
                classify=lambda groups, folders: [("출장", "내용")],
                batch_size=2,
                progress=lambda done, total: progress.append((done, total)),
                should_cancel=lambda: bool(progress),
            )
        self.assertEqual([doc.folder for doc in docs], ["기존"] * 3)

    def test_dissimilar_documents_are_not_forced_together(self):
        docs = [document("인사.pdf", "채용"), document("계약.pdf", "임대")]
        groups = []

        def label(items, folders):
            groups.extend(items)
            return [("인사", "채용"), ("계약", "임대")]

        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0], [0.0, 1.0]], classify=label,
        )
        self.assertEqual(len(groups), 2)
        self.assertEqual([doc.folder for doc in docs], ["인사", "계약"])

    def test_singleton_without_usable_name_or_text_stays_unclassified(self):
        doc = document("12345.bin")
        count = assign_ai_folders(
            [doc], embed=lambda texts: [[1.0, 0.0]],
            classify=lambda groups, folders: self.fail("빈 문서는 AI에 보내지 않아야 합니다"),
        )
        self.assertEqual(count, 0)
        self.assertEqual(doc.folder, "미분류")

    def test_group_label_failure_does_not_partially_change_preview(self):
        docs = [document("a.pdf", "A"), document("b.pdf", "B")]

        def label(items, folders):
            if items[0]["documents"][0]["name"] == "b.pdf":
                raise RuntimeError("AI 응답 실패")
            return [("새 폴더", "내용")]

        with self.assertRaises(RuntimeError):
            assign_ai_folders(
                docs, embed=lambda texts: [[1.0, 0.0], [0.0, 1.0]],
                classify=label, label_batch_size=1,
            )
        self.assertEqual([doc.folder for doc in docs], ["기존", "기존"])

    def test_embed_api_uses_local_model_and_checks_result_count(self):
        response = mock.Mock(status=200)
        response.read.return_value = b'{"embeddings":[[1.0,0.0]]}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("ai_groups.http.client.HTTPConnection", return_value=connection):
            vectors = ollama_embeddings(["출장 결과"])
        self.assertEqual(vectors, [[1.0, 0.0]])
        args, _ = connection.request.call_args
        self.assertEqual(args[:2], ("POST", "/api/embed"))
        self.assertIn(b"qwen3-embedding:0.6b", args[2])
        connection.close.assert_called_once()


    def test_moderately_similar_documents_share_one_group(self):
        docs = [document("출장 계획.pdf", "현장 방문"), document("출장 결과.pdf", "방문 보고")]
        seen = []

        def label(groups, folders):
            seen.extend(groups)
            return [("출장", "방문")]

        assign_ai_folders(
            docs, embed=lambda texts: [[1.0, 0.0], [0.6, 0.8]], classify=label,
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual([doc.folder for doc in docs], ["출장", "출장"])

    def test_ambiguous_similarity_does_not_force_a_group(self):
        docs = [
            document("인사.pdf", "채용"),
            document("계약.pdf", "임대"),
            document("혼합.pdf", "채용과 임대"),
        ]
        seen = []

        def label(groups, folders):
            seen.extend(groups)
            return [("미분류", "검토 필요") for _ in groups]

        assign_ai_folders(
            docs,
            embed=lambda texts: [[1.0, 0.0], [0.0, 1.0], [0.7071, 0.7071]],
            classify=label,
        )
        self.assertEqual(len(seen), 3)
    def test_non_korean_descriptive_filename_is_eligible(self):
        doc = document("請求書.pdf")
        count = assign_ai_folders(
            [doc], embed=lambda texts: [[1.0, 0.0]],
            classify=lambda groups, folders: [("Invoices", "descriptive filename")],
        )
        self.assertEqual(count, 1)
        self.assertEqual(doc.folder, "Invoices")
if __name__ == "__main__":
    unittest.main()
