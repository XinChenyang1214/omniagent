from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from omniagent.config import AppConfig, EmbeddingConfig
from omniagent.embeddings import (
    EmbeddingResult,
    LocalModelEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    build_embedding_provider,
)
from omniagent.memory import FileMemoryManager
from omniagent.vector_recall import VectorRecallEngine


def _fake_response(body: dict[str, Any], status: int = 200) -> MagicMock:
    data = json.dumps(body).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = data
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: None
    resp.status = status
    return resp


class StaticEmbeddingProvider:
    provider = "fake"
    model = "fake-model"
    version = "fake-v1"

    def __init__(self, dimensions: int = 2) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> EmbeddingResult:
        if self.dimensions == 2:
            vector = [1.0, 0.0] if "文件" in text or "保存" in text else [0.0, 1.0]
        else:
            vector = [1.0, 0.0, 0.0]
        return EmbeddingResult(
            embedding=vector,
            provider=self.provider,
            model=self.model,
            dimensions=len(vector),
            version=self.version,
            latency_ms=0.01,
        )


class CountingEmbeddingProvider(StaticEmbeddingProvider):
    def __init__(self, dimensions: int = 2) -> None:
        super().__init__(dimensions=dimensions)
        self.calls: list[str] = []

    def embed(self, text: str) -> EmbeddingResult:
        self.calls.append(text)
        return super().embed(text)


class EmbeddingConfigTest(unittest.TestCase):
    def test_embedding_config_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                "OMNIAGENT_EMBEDDING_ENABLED": "true",
                "OMNIAGENT_EMBEDDING_PROVIDER": "openai",
                "OMNIAGENT_EMBEDDING_MODEL": "text-embedding-3-small",
                "OMNIAGENT_EMBEDDING_API_BASE": "https://api.openai.com/v1",
                "OMNIAGENT_EMBEDDING_API_KEY": "sk-test",
                "OMNIAGENT_EMBEDDING_DIMENSIONS": "1536",
                "OMNIAGENT_EMBEDDING_TIMEOUT": "12",
                "OMNIAGENT_EMBEDDING_MAX_RETRIES": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                config = AppConfig.from_env(Path(tempdir))

        self.assertTrue(config.embedding.enabled)
        self.assertEqual(config.embedding.provider, "openai")
        self.assertEqual(config.embedding.model, "text-embedding-3-small")
        self.assertEqual(config.embedding.api_base, "https://api.openai.com/v1")
        self.assertEqual(config.embedding.api_key, "sk-test")
        self.assertEqual(config.embedding.dimensions, 1536)
        self.assertEqual(config.embedding.timeout_seconds, 12)
        self.assertEqual(config.embedding.max_retries, 0)

    def test_local_model_embedding_config_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            model_dir = Path(tempdir) / "tiny-embedding"
            tokenizer_path = Path(tempdir) / "tokenizer.json"
            env = {
                "OMNIAGENT_EMBEDDING_ENABLED": "true",
                "OMNIAGENT_EMBEDDING_PROVIDER": "local_model",
                "OMNIAGENT_EMBEDDING_MODEL": "tiny-local",
                "OMNIAGENT_EMBEDDING_MODEL_PATH": str(model_dir),
                "OMNIAGENT_EMBEDDING_TOKENIZER_PATH": str(tokenizer_path),
                "OMNIAGENT_EMBEDDING_POOLING": "cls",
                "OMNIAGENT_EMBEDDING_NORMALIZE": "false",
                "OMNIAGENT_EMBEDDING_MAX_TOKENS": "64",
            }
            with patch.dict(os.environ, env, clear=False):
                config = AppConfig.from_env(Path(tempdir))

        self.assertTrue(config.embedding.enabled)
        self.assertEqual(config.embedding.provider, "local_model")
        self.assertEqual(config.embedding.model, "tiny-local")
        self.assertEqual(config.embedding.model_path, model_dir)
        self.assertEqual(config.embedding.tokenizer_path, tokenizer_path)
        self.assertEqual(config.embedding.pooling, "cls")
        self.assertFalse(config.embedding.normalize)
        self.assertEqual(config.embedding.max_tokens, 64)


class OpenAICompatibleEmbeddingProviderTest(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_calls_openai_compatible_embeddings_endpoint(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _fake_response(
            {
                "model": "text-embedding-3-small",
                "data": [{"embedding": [0.1, 0.2, 0.3]}],
            }
        )
        provider = OpenAICompatibleEmbeddingProvider(
            EmbeddingConfig(
                enabled=True,
                provider="openai",
                model="text-embedding-3-small",
                api_key="sk-test",
                api_base="https://api.openai.com/v1",
                dimensions=3,
                max_retries=0,
            )
        )

        result = provider.embed("hello")

        self.assertEqual(result.embedding, [0.1, 0.2, 0.3])
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.model, "text-embedding-3-small")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/embeddings")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "text-embedding-3-small")
        self.assertEqual(body["input"], "hello")
        self.assertEqual(body["dimensions"], 3)

    def test_missing_real_embedding_config_falls_back_explicitly(self) -> None:
        provider = build_embedding_provider(
            EmbeddingConfig(
                enabled=True,
                provider="openai",
                model="",
                api_key=None,
            )
        )

        result = provider.embed("写一个文件并保存")

        self.assertEqual(result.provider, "local_hash")
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.requested_provider, "openai")
        self.assertIn("缺少 embedding model", result.error or "")


class LocalModelEmbeddingProviderTest(unittest.TestCase):
    def test_local_model_provider_runs_onnx_with_lazy_optional_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            model_dir = Path(tempdir) / "tiny-embedding"
            model_dir.mkdir()
            (model_dir / "model.onnx").write_bytes(b"fake-onnx")
            (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

            fake_np = types.ModuleType("numpy")
            fake_np.int64 = "int64"  # type: ignore[attr-defined]
            fake_np.asarray = lambda value, dtype=None: value  # type: ignore[attr-defined]

            class FakeEncoding:
                ids = [101, 102, 103]
                attention_mask = [1, 1, 1]
                type_ids = [0, 0, 0]

            class FakeTokenizer:
                truncation_length: int | None = None

                @classmethod
                def from_file(cls, path: str) -> "FakeTokenizer":
                    return cls()

                def enable_truncation(self, *, max_length: int) -> None:
                    self.truncation_length = max_length

                def encode(self, text: str) -> FakeEncoding:
                    return FakeEncoding()

            fake_tokenizers = types.ModuleType("tokenizers")
            fake_tokenizers.__version__ = "0.test"  # type: ignore[attr-defined]
            fake_tokenizers.Tokenizer = FakeTokenizer  # type: ignore[attr-defined]

            class FakeInput:
                def __init__(self, name: str) -> None:
                    self.name = name

            class FakeSession:
                last_inputs: dict[str, Any] = {}

                def __init__(self, path: str, providers: list[str]) -> None:
                    self.path = path
                    self.providers = providers

                def get_inputs(self) -> list[FakeInput]:
                    return [
                        FakeInput("input_ids"),
                        FakeInput("attention_mask"),
                        FakeInput("token_type_ids"),
                    ]

                def run(self, outputs: Any, inputs: dict[str, Any]) -> list[Any]:
                    FakeSession.last_inputs = inputs
                    return [[[[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]]]

            fake_ort = types.ModuleType("onnxruntime")
            fake_ort.InferenceSession = FakeSession  # type: ignore[attr-defined]

            with patch.dict(
                sys.modules,
                {
                    "numpy": fake_np,
                    "onnxruntime": fake_ort,
                    "tokenizers": fake_tokenizers,
                },
            ):
                provider = LocalModelEmbeddingProvider(
                    EmbeddingConfig(
                        enabled=True,
                        provider="local_model",
                        model="tiny-local",
                        model_path=model_dir,
                        pooling="mean",
                        normalize=False,
                        max_tokens=2,
                    )
                )
                result = provider.embed("hello")

        self.assertEqual(result.provider, "local_model")
        self.assertEqual(result.model, "tiny-local")
        self.assertEqual(result.embedding, [0.5, 0.5])
        self.assertEqual(result.dimensions, 2)
        self.assertEqual(result.extra_metadata["embedding_pooling"], "mean")
        self.assertEqual(result.extra_metadata["embedding_max_tokens"], 2)
        self.assertEqual(len(result.extra_metadata["embedding_model_fingerprint"]), 16)
        self.assertEqual(FakeSession.last_inputs["input_ids"], [[101, 102]])
        self.assertEqual(FakeSession.last_inputs["attention_mask"], [[1, 1]])

    def test_local_model_provider_falls_back_when_model_missing(self) -> None:
        provider = build_embedding_provider(
            EmbeddingConfig(
                enabled=True,
                provider="local_model",
                model="tiny-local",
                model_path=Path("/tmp/omniagent-missing-local-model"),
            )
        )

        result = provider.embed("写一个文件并保存")

        self.assertEqual(result.provider, "local_hash")
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.requested_provider, "local_model")
        self.assertIn("本地 embedding 模型文件不存在", result.error or "")


class VectorRecallEmbeddingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"

    def test_vector_entry_records_embedding_metadata(self) -> None:
        manager = FileMemoryManager(
            self.workspace,
            vector_engine=VectorRecallEngine(StaticEmbeddingProvider()),
        )

        path = manager.write_vector_entry(
            entry_id="task_write_file",
            text="写一个文件并保存到工作区",
            source_type="task",
            metadata={"task_id": "task_write_file"},
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["embedding_provider"], "fake")
        self.assertEqual(payload["embedding_model"], "fake-model")
        self.assertEqual(payload["embedding_dimensions"], 2)
        self.assertEqual(payload["embedding_version"], "fake-v1")
        matches = manager.recall_vector("创建一个文档保存下来", limit=1)
        self.assertEqual(matches[0]["entry_id"], "task_write_file")
        self.assertTrue(matches[0]["match_details"]["vector_compatible"])

    def test_vector_score_skips_incompatible_dimensions(self) -> None:
        manager = FileMemoryManager(
            self.workspace,
            vector_engine=VectorRecallEngine(StaticEmbeddingProvider(dimensions=2)),
        )
        manager.write_vector_entry(
            entry_id="task_write_file",
            text="写一个文件并保存到工作区",
            source_type="task",
            metadata={"task_id": "task_write_file"},
        )
        manager.vector_engine = VectorRecallEngine(StaticEmbeddingProvider(dimensions=3))

        matches = manager.recall_vector("写一个文件", limit=1)

        self.assertEqual(matches[0]["entry_id"], "task_write_file")
        self.assertFalse(matches[0]["match_details"]["vector_compatible"])
        self.assertEqual(matches[0]["match_details"]["vector_score"], 0.0)

    def test_recall_embeds_query_once_per_scan(self) -> None:
        provider = CountingEmbeddingProvider()
        manager = FileMemoryManager(
            self.workspace,
            vector_engine=VectorRecallEngine(provider),
        )
        manager.write_vector_entry(
            entry_id="task_write_file",
            text="写一个文件并保存到工作区",
            source_type="task",
            metadata={"task_id": "task_write_file"},
        )
        manager.write_vector_entry(
            entry_id="task_browser",
            text="抓取网页内容并生成摘要",
            source_type="task",
            metadata={"task_id": "task_browser"},
        )
        provider.calls.clear()

        manager.recall_vector("写一个文件", limit=2)

        self.assertEqual(provider.calls, ["写一个文件"])


if __name__ == "__main__":
    unittest.main()
