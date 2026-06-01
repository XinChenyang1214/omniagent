from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omniagent.memory import FileMemoryManager


class VectorRecallSemanticTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.manager = FileMemoryManager(self.workspace)

    def test_recall_vector_matches_semantic_aliases(self) -> None:
        self.manager.write_vector_entry(
            entry_id="task_write_file",
            text="写一个文件并保存到工作区",
            source_type="task",
            metadata={"task_id": "task_write_file"},
        )
        self.manager.write_vector_entry(
            entry_id="task_browser",
            text="抓取网页内容并生成摘要",
            source_type="task",
            metadata={"task_id": "task_browser"},
        )

        matches = self.manager.recall_vector("创建一个文档保存下来", limit=2)
        self.assertTrue(matches, "expected semantic vector recall matches")
        self.assertEqual(matches[0]["entry_id"], "task_write_file")
        self.assertGreater(matches[0]["score"], 0)
        self.assertIn("write", matches[0]["match_details"]["matched_concepts"])
        self.assertIn("file", matches[0]["match_details"]["matched_concepts"])


if __name__ == "__main__":
    unittest.main()
