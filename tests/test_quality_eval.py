import unittest

from quality_eval import score_labels


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


if __name__ == "__main__":
    unittest.main()
