import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ai_groups import assign_ai_folders, ollama_embeddings
from app import ai_preview_note
from organizer import Document, scan


class AiGroupTests(unittest.TestCase):
    def test_similar_unclassified_document_joins_existing_folder_and_duplicate_follows(self):
        garden = Document(Path("garden.pdf"), "텃밭", "공통 단어", "PDF 텍스트", lead_text="식물 재배 안내")
        health = Document(Path("health.pdf"), "건강", "공통 단어", "PDF 텍스트", lead_text="운동 건강 관리")
        unknown = Document(Path("scan.pdf"), "미분류", "공통 단어 없음", "PDF 텍스트", lead_text="모종 재배 방법")
        duplicate = Document(Path("copy.pdf"), "미분류", "동일 파일", "중복 파일", duplicate_of=unknown.source)

        vectors = {
            "garden.pdf": [1.0, 0.0],
            "health.pdf": [0.0, 1.0],
            "scan.pdf": [0.99, 0.1],
        }

        def embed(texts):
            return [vectors[text.split("\n", 1)[0]] for text in texts]

        count = assign_ai_folders([garden, health, unknown, duplicate], embed)
        self.assertEqual(count, 1)
        self.assertEqual((unknown.folder, duplicate.folder), ("텃밭", "텃밭"))
        self.assertIn("AI", unknown.folder_reason)
        self.assertEqual(duplicate.folder_reason, unknown.folder_reason)

    def test_nested_folder_files_are_classified_individually(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            source = Path(temporary) / "source"
            nested = source / "받은 문서"
            nested.mkdir(parents=True)
            for name, title in (
                ("텃밭 신청.docx", "신청 방법"),
                ("텃밭 안내.docx", "이용 안내"),
                ("scan003.docx", "모종 재배"),
            ):
                folder = nested if name == "scan003.docx" else source
                with zipfile.ZipFile(folder / name, "w") as archive:
                    archive.writestr(
                        "word/document.xml",
                        f"<w:document xmlns:w='w'><w:t>{title}</w:t></w:document>",
                    )
            documents = scan(source, Path(temporary) / "result", None)
            self.assertEqual(next(doc.folder for doc in documents if doc.source.name == "scan003.docx"), "미분류")
            count = assign_ai_folders(
                documents,
                lambda texts: [[1.0, 0.0] for _ in texts],
            )
            self.assertEqual(count, 1)
            self.assertEqual(next(doc.folder for doc in documents if doc.source.name == "scan003.docx"), "텃밭")
    def test_ambiguous_and_unreadable_documents_stay_unclassified(self):
        garden = Document(Path("garden.pdf"), "텃밭", "공통 단어", "PDF 텍스트", lead_text="식물 재배")
        health = Document(Path("health.pdf"), "건강", "공통 단어", "PDF 텍스트", lead_text="운동 관리")
        ambiguous = Document(Path("scan.pdf"), "미분류", "공통 단어 없음", "PDF 텍스트", lead_text="기타")
        unreadable = Document(Path("broken.pdf"), "미분류", "공통 단어 없음", "", error="읽기 실패")

        vectors = {"garden.pdf": [1.0, 0.0], "health.pdf": [0.0, 1.0], "scan.pdf": [0.7, 0.7]}
        count = assign_ai_folders(
            [garden, health, ambiguous, unreadable],
            lambda texts: [vectors[text.split("\n", 1)[0]] for text in texts],
        )
        self.assertEqual(count, 0)
        self.assertEqual((ambiguous.folder, unreadable.folder), ("미분류", "미분류"))

    def test_embedding_failure_does_not_change_any_folder(self):
        garden = Document(Path("garden.pdf"), "텃밭", "공통 단어", "PDF 텍스트", lead_text="식물 재배")
        unknown = Document(Path("scan.pdf"), "미분류", "공통 단어 없음", "PDF 텍스트", lead_text="모종")
        def fail(_texts):
            raise OSError("Ollama 연결 실패")

        with self.assertRaises(OSError):
            assign_ai_folders([garden, unknown], fail)
        self.assertEqual((garden.folder, unknown.folder), ("텃밭", "미분류"))

    def test_preview_keeps_existing_result_when_ollama_is_unavailable(self):
        document = Document(Path("scan.pdf"), "미분류", "공통 단어 없음", "PDF 텍스트", lead_text="모종")
        with mock.patch("app.assign_ai_folders", side_effect=RuntimeError("Ollama 연결 실패")):
            note = ai_preview_note([document], enabled=True)
        self.assertIn("Ollama 연결 실패", note)
        self.assertEqual(document.folder, "미분류")
    def test_local_ollama_request_uses_expected_model_and_vectors(self):
        response = mock.Mock(status=200)
        response.read.return_value = json.dumps({"embeddings": [[1.0, 0.0], [0.0, 1.0]]}).encode()
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("ai_groups.http.client.HTTPConnection", return_value=connection) as constructor:
            result = ollama_embeddings(["문서 하나", "문서 둘"])
        self.assertEqual(result, [[1.0, 0.0], [0.0, 1.0]])
        constructor.assert_called_once_with("127.0.0.1", 11434, timeout=30)
        args, _ = connection.request.call_args
        self.assertEqual(args[:2], ("POST", "/api/embed"))
        payload = json.loads(args[2])
        self.assertEqual(payload["model"], "qwen3-embedding:0.6b")
        self.assertEqual(payload["input"], ["문서 하나", "문서 둘"])
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()