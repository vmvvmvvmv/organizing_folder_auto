import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ollama_setup
from ai_groups import assign_ai_folders
from organizer import Document


class MeaningEvidenceTests(unittest.TestCase):
    """영어 본문에 한국어 폴더명이 붙어도 뜻이 가까우면 받아들인다."""

    def run_with(self, folder_vector):
        doc = Document(Path("Purchase Order PO-222.pdf"), "미분류", "", "PDF 텍스트",
                       lead_text="Please supply 2,400 units of KX-200 sensors. Unit price USD 14.50. " * 2)

        def embed(texts):
            return [folder_vector if text.startswith("폴더 주제:") else [1.0, 0.0] for text in texts]

        assign_ai_folders([doc], ai_only=True, embed=embed,
                          classify=lambda items, known: [("구매 주문서", "발주 문서") for _ in items],
                          subdivide=lambda items, parent: [("", "") for _ in items])
        return doc.folder

    def test_close_meaning_is_accepted_for_english_text(self):
        self.assertEqual(self.run_with([0.9, 0.1]), "구매 주문서")

    def test_unrelated_meaning_is_rejected(self):
        self.assertEqual(self.run_with([0.0, 1.0]), "미분류")


class OllamaSetupTests(unittest.TestCase):
    def test_finds_installed_tray_app(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "Programs" / "Ollama" / "ollama app.exe"
            app.parent.mkdir(parents=True)
            app.write_bytes(b"")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                self.assertEqual(ollama_setup.find_ollama(), app)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(Path(folder) / "없음")}), \
                    mock.patch("shutil.which", return_value=None):
                self.assertIsNone(ollama_setup.find_ollama())

    def test_install_uses_official_winget_package_and_reports_progress(self):
        process = mock.Mock()
        process.stdout = iter(["  Found Ollama [Ollama.Ollama]\n", "  ████ 40%\r  ██████ 100%\n",
                               "Successfully installed\n"])
        process.wait.return_value = 0
        lines = []
        with mock.patch("shutil.which", return_value="winget"), \
                mock.patch("subprocess.Popen", return_value=process) as popen:
            ollama_setup.install_ollama(lines.append)
        command = popen.call_args.args[0]
        self.assertEqual(command[:5], ["winget", "install", "--exact", "--id", "Ollama.Ollama"])
        self.assertIn("--accept-package-agreements", command)
        self.assertEqual(lines[-1], "Successfully installed")
        self.assertIn("100%", lines[-2])

    def test_failed_install_explains_why(self):
        process = mock.Mock(stdout=iter(["No package found matching input criteria.\n"]))
        process.wait.return_value = 1
        with mock.patch("shutil.which", return_value="winget"), \
                mock.patch("subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "No package found"):
                ollama_setup.install_ollama()
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "winget이 없어"):
                ollama_setup.install_ollama()

    def test_start_waits_until_ollama_answers(self):
        answers = iter([False, False, True])
        with mock.patch.object(ollama_setup, "find_ollama", return_value=Path("C:/x/ollama.exe")), \
                mock.patch("subprocess.Popen") as popen, mock.patch("time.sleep"):
            ollama_setup.start_ollama(lambda: next(answers))
        self.assertEqual(popen.call_args.args[0], ["C:\\x\\ollama.exe", "serve"]
                         if os.name == "nt" else ["C:/x/ollama.exe", "serve"])
        with mock.patch.object(ollama_setup, "find_ollama", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "찾지 못했습니다"):
                ollama_setup.start_ollama(lambda: True)


if __name__ == "__main__":
    unittest.main()
