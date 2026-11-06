import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ai_groups
import organizer
from app import progress_text
from local_cache import LocalCache, cached_ollama, clear_cache
from organizer import move_documents, scan


class LocalCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source"
        self.source.mkdir()
        (self.source / "메모.txt").write_text("출장 결과 메모", encoding="utf-8")
        self.cache_path = self.folder / "cache.sqlite3"

    def scan_counting(self):
        with LocalCache(self.cache_path) as cache, \
                mock.patch("organizer.read_document", wraps=organizer.read_document) as read, \
                mock.patch("organizer._digest", wraps=organizer._digest) as digest:
            documents = scan(self.source, self.folder / "result", None, classify=False, cache=cache)
        return documents, read.call_count, digest.call_count

    def test_second_preview_skips_hashing_and_reading(self):
        first, reads, hashes = self.scan_counting()
        self.assertEqual((reads, hashes), (1, 1))
        second, reads, hashes = self.scan_counting()
        self.assertEqual((reads, hashes), (0, 0))
        self.assertEqual((second[0].lead_text, second[0].method), ("출장 결과 메모", "텍스트 파일"))
        self.assertEqual(second[0].preview_digest, first[0].preview_digest)

    def test_changed_file_is_hashed_and_read_again(self):
        self.scan_counting()
        (self.source / "메모.txt").write_text("회의록으로 바뀐 내용입니다", encoding="utf-8")
        documents, reads, hashes = self.scan_counting()
        self.assertEqual((reads, hashes), (1, 1))
        self.assertIn("회의록", documents[0].lead_text)

    def test_wrong_saved_hash_blocks_move_instead_of_deleting(self):
        # 크기와 수정 시각을 그대로 두고 내용만 바꾸면 저장된 해시가 틀린다.
        path = self.source / "메모.txt"
        self.scan_counting()
        before = path.stat()
        path.write_bytes("다른 내용 메모".encode("utf-8")[:before.st_size].ljust(before.st_size, b" "))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        documents, _, _ = self.scan_counting()
        documents[0].folder = "자료"
        destination = self.folder / "result"
        destination.mkdir()
        with self.assertRaisesRegex(ValueError, "미리보기 이후"):
            move_documents(documents, destination, source_root=self.source)
        self.assertTrue(path.exists())

    def test_ollama_answers_are_reused_from_the_store(self):
        items = [{"id": 0, "documents": [], "count": 1}]
        with mock.patch("ai_groups.ollama_classify", return_value=[("출장", "이유")]) as chat:
            with LocalCache(self.cache_path) as cache:
                self.assertEqual(cached_ollama(cache.responses())["classify"](items, []), [("출장", "이유")])
            with LocalCache(self.cache_path) as cache:
                self.assertEqual(cached_ollama(cache.responses())["classify"](items, []), [("출장", "이유")])
        self.assertEqual(chat.call_count, 1)
        clear_cache(self.cache_path)
        self.assertFalse(self.cache_path.exists())

    def test_progress_shows_remaining_time_after_a_few_seconds(self):
        with mock.patch("app.time.monotonic", return_value=130.0):
            self.assertEqual(progress_text("읽는 중", 100.0, 30, 90), "읽는 중: 30/90 · 남은 시간 약 1분")
            self.assertEqual(progress_text("읽는 중", 129.0, 1, 90), "읽는 중: 1/90")
            self.assertEqual(progress_text("읽는 중", 100.0, 90, 90), "읽는 중: 90/90")


if __name__ == "__main__":
    unittest.main()
