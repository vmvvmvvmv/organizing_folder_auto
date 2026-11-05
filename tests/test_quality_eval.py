import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ai_groups
from quality_eval import cached_ollama, score_labels


class QualityEvalTests(unittest.TestCase):
    def test_pairwise_score_detects_merges_and_splits(self):
        gold = ["증빙", "증빙", "인구", "인구"]
        predicted = ["서류", "서류", "서류", "데이터"]
        result = score_labels(gold, predicted)
        self.assertEqual(result["true_positive_pairs"], 1)
        self.assertEqual(result["predicted_pairs"], 3)
        self.assertEqual(result["expected_pairs"], 2)
        self.assertAlmostEqual(result["pair_precision"], 1 / 3)
        self.assertAlmostEqual(result["pair_recall"], 1 / 2)
        self.assertAlmostEqual(result["pair_f1"], 0.4)

    def test_unclassified_files_are_not_treated_as_a_folder(self):
        result = score_labels(["증빙", "증빙"], ["미분류", "미분류"])
        self.assertEqual(result["pair_recall"], 0)
        self.assertEqual(result["predicted_pairs"], 0)

    def test_unclassified_count_is_reported(self):
        result = score_labels(["증빙", "인구"], ["미분류", "인구"])
        self.assertEqual(result["unclassified"], 1)
        self.assertEqual(result["folder_count"], 1)

    def test_cache_replays_answers_and_errors_without_calling_ollama_again(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "cache.json"
            items = [{"id": 0, "documents": [], "count": 1}]
            with mock.patch("ai_groups.ollama_classify", return_value=[("출장", "이유")]) as chat, \
                    mock.patch("ai_groups.ollama_embeddings",
                               side_effect=ai_groups.IncompleteResultError("누락")) as embed:
                calls, save = cached_ollama(cache)
                with self.assertRaises(ai_groups.IncompleteResultError):
                    calls["embed"](["본문"])
                self.assertEqual(calls["classify"](items, []), [("출장", "이유")])
                save()
                # 새로 읽은 캐시는 Ollama를 부르지 않고 같은 응답과 오류를 돌려준다.
                again, _ = cached_ollama(cache)
                self.assertEqual(again["classify"](items, []), [("출장", "이유")])
                with self.assertRaises(ai_groups.IncompleteResultError):
                    again["embed"](["본문"])
            self.assertEqual((chat.call_count, embed.call_count), (1, 1))
            # 앱과 같은 AI 전용 지시문을 쓴다.
            self.assertEqual(chat.call_args.kwargs["instructions_override"],
                             ai_groups.AI_ONLY_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
