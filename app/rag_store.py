import hashlib
import json
import math
import re
from dataclasses import dataclass

import aiohttp

from .db_models import TroubleshootingMemory
from .db_system import get_db_session
from .secrets import get_llama_embedding_url, get_model_light_work, get_rag_top_k

LOCAL_VEC_DIM = 256


def _normalize_vector(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm < 1e-12:
        return vec
    return [v / norm for v in vec]


def _compute_local_hash_embedding(text: str, dim: int = LOCAL_VEC_DIM) -> list[float]:
    cleaned = re.sub(r"\s+", " ", text.lower()).strip()
    if not cleaned:
        return [0.0] * dim

    tokens = re.findall(r"[a-z0-9_]+|[\u3040-\u9fff]+", cleaned)
    features: list[str] = list(tokens)
    for i in range(len(cleaned) - 2):
        features.append(cleaned[i : i + 3])

    vec = [0.0] * dim
    for feat in features:
        digest = hashlib.md5(feat.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:2], "little") % dim
        sign = 1.0 if (digest[2] % 2 == 0) else -1.0
        vec[idx] += sign

    return _normalize_vector(vec)


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    return sum(a * b for a, b in zip(vec_a, vec_b))


@dataclass
class MemoryVectorCache:
    id: int
    vector: list[float]
    success_score: float


class TroubleshootingRAGStore:
    """インメモリキャッシュとハイブリッド検索（実績ボーナス）を備えた次世代RAGストア"""

    def __init__(self):
        self._vector_cache: list[MemoryVectorCache] = []
        self._is_loaded = False

    def _load_cache_if_needed(self, db) -> None:
        if self._is_loaded:
            return
        self._vector_cache.clear()
        records = db.query(TroubleshootingMemory).all()
        for rec in records:
            if not rec.embedding_json:
                continue
            try:
                vec = json.loads(rec.embedding_json)
                total_uses = rec.success_count + rec.fail_count
                trust_bonus = 1.0 + (0.1 * rec.success_count) if total_uses > 0 else 1.0
                self._vector_cache.append(MemoryVectorCache(id=rec.id, vector=vec, success_score=trust_bonus))
            except Exception:
                pass
        self._is_loaded = True

    async def get_embedding(self, text: str) -> list[float]:
        timeout = aiohttp.ClientTimeout(total=10)
        payload = {
            "model": get_model_light_work(),
            "input": text[:2000],
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(get_llama_embedding_url(), json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "data" in data and data["data"]:
                            raw_vec = data["data"][0].get("embedding")
                            if isinstance(raw_vec, list) and raw_vec:
                                return _normalize_vector([float(x) for x in raw_vec])
        except Exception:
            pass
        return _compute_local_hash_embedding(text)

    def add_memory_sync(self, category: str, error_pattern: str, root_cause: str, solution: str) -> int:
        combined_text = f"{category} | {error_pattern} | {root_cause} | {solution}"
        vec = _compute_local_hash_embedding(combined_text)
        with get_db_session() as db:
            memo = TroubleshootingMemory(
                category=category,
                error_pattern=error_pattern,
                root_cause=root_cause,
                solution=solution,
                embedding_json=json.dumps(vec),
                success_count=0,
                fail_count=0,
            )
            db.add(memo)
            db.commit()
            db.refresh(memo)
            self._is_loaded = False
            return memo.id

    async def search_relevant_memories(
        self, query_text: str, top_k: int | None = None, min_score: float = 0.15
    ) -> list[TroubleshootingMemory]:
        if not query_text.strip():
            return []
        top_k = top_k or get_rag_top_k()
        query_vec = await self.get_embedding(query_text)

        with get_db_session() as db:
            records = db.query(TroubleshootingMemory).all()
            backfilled = False
            for rec in records:
                rec_vec = []
                if rec.embedding_json:
                    try:
                        rec_vec = json.loads(rec.embedding_json)
                    except Exception:
                        pass
                if len(rec_vec) != len(query_vec):
                    combined = f"{rec.category} | {rec.error_pattern} | {rec.root_cause}"
                    if len(query_vec) == LOCAL_VEC_DIM:
                        rec_vec = _compute_local_hash_embedding(combined)
                    else:
                        rec_vec = await self.get_embedding(combined)
                    rec.embedding_json = json.dumps(rec_vec)
                    db.commit()
                    backfilled = True

            if backfilled:
                self._is_loaded = False

            self._load_cache_if_needed(db)

            scored_ids = []
            for cache in self._vector_cache:
                if len(query_vec) != len(cache.vector):
                    continue
                sim = _cosine_similarity(query_vec, cache.vector)
                final_score = sim * cache.success_score
                if final_score >= min_score:
                    scored_ids.append((final_score, cache.id))

            scored_ids.sort(key=lambda x: x[0], reverse=True)
            top_ids = [cid for score, cid in scored_ids[:top_k]]
            if not top_ids:
                return []
            return db.query(TroubleshootingMemory).filter(TroubleshootingMemory.id.in_(top_ids)).all()

    def format_as_prompt_context(
        self, memories: list[TroubleshootingMemory], header: str = "Relevant Past Troubleshooting Knowledge (RAG)"
    ) -> str:
        if not memories:
            return ""

        lines = [f"--- [{header}] ---"]
        for mem in memories:
            lines.append(
                f"• [{mem.category}] Pattern: {mem.error_pattern}\n"
                f"  - Root Cause : {mem.root_cause}\n"
                f"  - Resolution : {mem.solution}"
            )
        lines.append("--------------------------------------------------")
        return "\n".join(lines)

    def mark_memory_success(self, memory_ids: list[int], is_success: bool) -> None:
        if not memory_ids:
            return
        from datetime import UTC, datetime

        with get_db_session() as db:
            mems = db.query(TroubleshootingMemory).filter(TroubleshootingMemory.id.in_(memory_ids)).all()
            for mem in mems:
                if is_success:
                    mem.success_count += 1
                else:
                    mem.fail_count += 1
                mem.last_used_at = datetime.now(UTC)
            db.commit()
            self._is_loaded = False


rag_store = TroubleshootingRAGStore()