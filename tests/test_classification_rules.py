import json
import tempfile
import unittest
from pathlib import Path

from classification_rules import STRUCTURED_TYPES, load_rules, type_folder


class ClassificationRulesTests(unittest.TestCase):
    def test_file_groups_come_from_editable_configuration(self):
        self.assertEqual(type_folder(Path("map.shp")), "GIS 자료")
        self.assertIn(".shp", STRUCTURED_TYPES)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rules.json"
            path.write_text(json.dumps({
                "ignored_words": ["예시"],
                "file_groups": [{
                    "folder": "새 형식", "extensions": [".foo"],
                    "preserve_structure": True,
                }],
            }), encoding="utf-8")
            ignored, folders, structured = load_rules(path)
        self.assertEqual(ignored, {"예시"})
        self.assertEqual(folders, {".foo": "새 형식"})
        self.assertEqual(structured, {".foo"})


if __name__ == "__main__":
    unittest.main()
