from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")

_SEMANTIC_GROUPS: dict[str, tuple[str, ...]] = {
    "write": ("写", "写入", "创建", "生成", "保存", "落盘", "write", "create", "generate", "save"),
    "read": ("读", "读取", "查看", "打开", "read", "open", "view", "show"),
    "list": ("列出", "列表", "目录", "遍历", "list", "scan", "browse"),
    "delete": ("删除", "移除", "清理", "delete", "remove", "cleanup"),
    "python": ("python", "脚本", "代码", "执行代码", "script", "code"),
    "browser": ("网页", "页面", "网站", "浏览器", "url", "http", "https", "browser", "web", "page"),
    "search": ("搜索", "查找", "检索", "查询", "search", "find", "lookup", "query"),
    "status": ("状态", "进度", "做到哪", "status", "progress"),
    "failure": ("失败", "报错", "错误", "异常", "failure", "failed", "error"),
    "approval": ("审批", "批准", "同意", "允许", "approval", "approve", "allow"),
    "retry": ("重试", "再试", "retry"),
    "replan": ("重规划", "重新规划", "换个方案", "replan"),
    "task": ("任务", "工作", "task", "job"),
    "file": ("文件", "文档", "file", "document", "txt", "md", "json"),
}

_ALIAS_TO_CONCEPT = {
    alias.lower(): concept for concept, aliases in _SEMANTIC_GROUPS.items() for alias in aliases
}


def _normalize_text(text: str) -> str:
    normalized = text.lower()
    normalized = normalized.replace("http://", " http ")
    normalized = normalized.replace("https://", " https ")
    normalized = re.sub(r"[\s/\\:;,，。！？()\[\]{}<>]+", " ", normalized)
    return normalized.strip()


def tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(_normalize_text(text))


def extract_concepts(text: str) -> list[str]:
    lowered = _normalize_text(text)
    concepts: set[str] = set()
    for concept, aliases in _SEMANTIC_GROUPS.items():
        if any(alias.lower() in lowered for alias in aliases):
            concepts.add(concept)
    for token in tokenize(text):
        alias = _ALIAS_TO_CONCEPT.get(token)
        if alias:
            concepts.add(alias)
    return sorted(concepts)


def _char_ngrams(text: str, n: int = 3) -> list[str]:
    compact = _normalize_text(text).replace(" ", "")
    if len(compact) <= n:
        return [compact] if compact else []
    return [compact[index : index + n] for index in range(len(compact) - n + 1)]


def _feature_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    tokens = tokenize(text)
    for token, count in Counter(tokens).items():
        counts[f"token:{token}"] += float(count)
        concept = _ALIAS_TO_CONCEPT.get(token)
        if concept:
            counts[f"concept:{concept}"] += 2.0 * count
    for concept in extract_concepts(text):
        counts[f"concept:{concept}"] += 2.5
    for gram, count in Counter(_char_ngrams(text)).items():
        counts[f"gram:{gram}"] += 0.35 * count
    return counts


def build_embedding(text: str, *, dimensions: int = 96) -> list[float]:
    counts = _feature_counts(text)
    vector = [0.0] * dimensions
    for feature, value in counts.items():
        bucket = hash(feature) % dimensions
        sign = 1.0 if hash(f"sign:{feature}") % 2 == 0 else -1.0
        vector[bucket] += sign * float(value)
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0:
        return vector
    return [component / norm for component in vector]


def cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    if not vector_a or not vector_b:
        return 0.0
    dot = sum(left * right for left, right in zip(vector_a, vector_b, strict=False))
    norm_a = math.sqrt(sum(value * value for value in vector_a))
    norm_b = math.sqrt(sum(value * value for value in vector_b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def lexical_similarity(query: str, candidate: str) -> float:
    query_counts = Counter(tokenize(query))
    candidate_counts = Counter(tokenize(candidate))
    if not query_counts or not candidate_counts:
        return 0.0
    all_terms = set(query_counts) | set(candidate_counts)
    dot = sum(query_counts.get(term, 0) * candidate_counts.get(term, 0) for term in all_terms)
    query_norm = math.sqrt(sum(value * value for value in query_counts.values()))
    candidate_norm = math.sqrt(sum(value * value for value in candidate_counts.values()))
    if not query_norm or not candidate_norm:
        return 0.0
    return dot / (query_norm * candidate_norm)


class SemanticScorer:
    """Pre-compute query features once, then score many candidates.

    META-006: ``semantic_similarity(query, candidate)`` rebuilt the
    query's embedding + token counts + concept set on every call. In
    ``recommend_workflows`` the same query was scored against ~500
    candidates, so the query-side feature extraction ran 500× more than
    needed. This scorer lets the caller amortize that work across all
    candidates in one scan.
    """

    __slots__ = (
        "query",
        "_query_vector",
        "_query_token_counts",
        "_query_norm",
        "_query_concepts",
    )

    def __init__(self, query: str) -> None:
        self.query = query
        self._query_vector = build_embedding(query)
        self._query_token_counts = Counter(tokenize(query))
        self._query_norm = math.sqrt(
            sum(value * value for value in self._query_token_counts.values())
        )
        self._query_concepts = set(extract_concepts(query))

    def score(self, candidate: str) -> float:
        candidate_vector = build_embedding(candidate)
        vector_score = cosine_similarity(self._query_vector, candidate_vector)
        candidate_counts = Counter(tokenize(candidate))
        if not self._query_token_counts or not candidate_counts:
            lexical_score = 0.0
        else:
            candidate_norm = math.sqrt(sum(value * value for value in candidate_counts.values()))
            if not self._query_norm or not candidate_norm:
                lexical_score = 0.0
            else:
                all_terms = set(self._query_token_counts) | set(candidate_counts)
                dot = sum(
                    self._query_token_counts.get(term, 0) * candidate_counts.get(term, 0)
                    for term in all_terms
                )
                lexical_score = dot / (self._query_norm * candidate_norm)
        candidate_concepts = set(extract_concepts(candidate))
        concept_overlap = len(self._query_concepts & candidate_concepts)
        concept_bonus = 0.08 * concept_overlap if concept_overlap else 0.0
        score = 0.72 * vector_score + 0.28 * lexical_score + concept_bonus
        return max(0.0, min(score, 1.0))


def semantic_similarity(query: str, candidate: str) -> float:
    # Preserved for callers that score a single candidate (and for any
    # external users). Hot loops should use SemanticScorer directly.
    return SemanticScorer(query).score(candidate)


def describe_match(query: str, candidate: str) -> dict[str, Any]:
    query_concepts = set(extract_concepts(query))
    candidate_concepts = set(extract_concepts(candidate))
    return {
        "matched_concepts": sorted(query_concepts & candidate_concepts),
        "query_concepts": sorted(query_concepts),
        "candidate_concepts": sorted(candidate_concepts),
        "lexical_score": round(lexical_similarity(query, candidate), 4),
    }


class VectorRecallEngine:
    def __init__(self, embedding_provider: Any | None = None) -> None:
        self.embedding_provider = embedding_provider

    def build_query(self, query: str) -> dict[str, Any]:
        return self._embed(query)

    def build_entry(self, text: str) -> dict[str, Any]:
        result = self._embed(text)
        return {
            "embedding": result["embedding"],
            "concepts": extract_concepts(text),
            "embedding_provider": result["provider"],
            "embedding_model": result["model"],
            "embedding_dimensions": result["dimensions"],
            "embedding_version": result["version"],
            "embedding_latency_ms": result["latency_ms"],
            "embedding_fallback_used": result["fallback_used"],
            **({"embedding_error": result["error"]} if result.get("error") else {}),
            **(
                {"embedding_requested_provider": result["requested_provider"]}
                if result.get("requested_provider")
                else {}
            ),
            **(
                {"embedding_requested_model": result["requested_model"]}
                if result.get("requested_model")
                else {}
            ),
            **dict(result.get("extra_metadata", {})),
        }

    def score(
        self,
        query: str,
        payload: dict[str, Any],
        *,
        query_result: dict[str, Any] | None = None,
    ) -> tuple[float, dict[str, Any]]:
        text = str(payload.get("text", ""))
        stored_embedding = payload.get("embedding")
        query_result = query_result or self.build_query(query)
        query_vector = query_result["embedding"]
        stored_vector = _coerce_vector(stored_embedding)
        stored_meta = _embedding_meta_from_payload(payload)
        vector_compatible = (
            bool(stored_vector)
            and len(stored_vector) == len(query_vector)
            and stored_meta["provider"] == query_result["provider"]
            and stored_meta["model"] == query_result["model"]
            and stored_meta["version"] == query_result["version"]
            and stored_meta["fingerprint"] == query_result["fingerprint"]
        )
        if vector_compatible:
            vector_score = cosine_similarity(query_vector, stored_vector)
        else:
            vector_score = 0.0
        lexical_score = lexical_similarity(query, text)
        details = describe_match(query, text)
        score = max(
            0.0,
            min(
                0.72 * vector_score
                + 0.28 * lexical_score
                + 0.08 * len(details["matched_concepts"]),
                1.0,
            ),
        )
        details["vector_score"] = round(vector_score, 4)
        details["vector_compatible"] = vector_compatible
        details["stored_embedding_provider"] = stored_meta["provider"]
        details["stored_embedding_model"] = stored_meta["model"]
        details["stored_embedding_dimensions"] = stored_meta["dimensions"]
        details["stored_embedding_version"] = stored_meta["version"]
        if stored_meta["fingerprint"] or query_result["fingerprint"]:
            details["stored_embedding_model_fingerprint"] = stored_meta["fingerprint"]
            details["query_embedding_model_fingerprint"] = query_result["fingerprint"]
        details["query_embedding_provider"] = query_result["provider"]
        details["query_embedding_model"] = query_result["model"]
        details["query_embedding_dimensions"] = query_result["dimensions"]
        details["query_embedding_version"] = query_result["version"]
        details["embedding_fallback_used"] = query_result["fallback_used"]
        if query_result.get("error"):
            details["embedding_error"] = query_result["error"]
        details["score"] = round(score, 4)
        return score, details

    def _embed(self, text: str) -> dict[str, Any]:
        if self.embedding_provider is None:
            embedding = build_embedding(text)
            return {
                "embedding": embedding,
                "provider": "local_hash",
                "model": "local-hash-v1",
                "dimensions": len(embedding),
                "version": "local_hash_v1",
                "latency_ms": 0.0,
                "fallback_used": False,
                "error": None,
                "requested_provider": None,
                "requested_model": None,
                "fingerprint": "",
                "extra_metadata": {},
            }
        result = self.embedding_provider.embed(text)
        embedding = [float(value) for value in result.embedding]
        extra_metadata = dict(getattr(result, "extra_metadata", {}) or {})
        return {
            "embedding": embedding,
            "provider": str(result.provider),
            "model": str(result.model),
            "dimensions": len(embedding),
            "version": str(result.version),
            "latency_ms": float(getattr(result, "latency_ms", 0.0)),
            "fallback_used": bool(getattr(result, "fallback_used", False)),
            "error": getattr(result, "error", None),
            "requested_provider": getattr(result, "requested_provider", None),
            "requested_model": getattr(result, "requested_model", None),
            "fingerprint": str(extra_metadata.get("embedding_model_fingerprint") or ""),
            "extra_metadata": extra_metadata,
        }


def _coerce_vector(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return []


def _embedding_meta_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    embedding = _coerce_vector(payload.get("embedding"))
    return {
        "provider": str(payload.get("embedding_provider") or "local_hash"),
        "model": str(payload.get("embedding_model") or "local-hash-v1"),
        "dimensions": int(payload.get("embedding_dimensions") or len(embedding)),
        "version": str(payload.get("embedding_version") or "local_hash_v1"),
        "fingerprint": str(payload.get("embedding_model_fingerprint") or ""),
    }
