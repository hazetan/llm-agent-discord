import hashlib
import json
import math
import re

import aiohttp

from .db_models import TroubleshootingMemory
from .db_system import get_db_session
from .secrets import get_llama_embedding_url, get_rag_top_k

LOCAL_VEC_DIM = 256


def _normalize_vector(vec: list[float]) -> list[float]:
    """ベクトルを L2 正規化する．"""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm < 1e-12:
        return vec
    return [v / norm for v in vec]


def _compute_local_hash_embedding(text: str, dim: int = LOCAL_VEC_DIM) -> list[float]:
    """
    Embedding API が利用できない環境向けの軽量ローカル特徴量ベクトル生成（Feature Hashing）．
    単語トークンと文字 bi-gram/tri-gram を組み合わせて日英混在のエラー文を高精度にベクトル化する．
    """
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
    """2つの正規化済みベクトルのコサイン類似度を算出する（次元数が異なる場合は 0.0）．"""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    return sum(a * b for a, b in zip(vec_a, vec_b))


class TroubleshootingRAGStore:
    """トラブルシューティング知識のベクトル化・保存・類似検索を担う RAG ストア．"""

    async def get_embedding(self, text: str) -> list[float]:
        """llama-server の Embedding API を試行し，失敗時はローカルベクトルへフォールバックする．"""
        timeout = aiohttp.ClientTimeout(total=10)
        payload = {"input": text[:2000]}
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

    def add_memory_sync(
        self,
        category: str,
        error_pattern: str,
        root_cause: str,
        solution: str,
    ) -> int:
        """新規トラブルシューティング知見をベクトル化して DB に保存する．"""
        combined_text = f"{category} | {error_pattern} | {root_cause} | {solution}"
        vec = _compute_local_hash_embedding(combined_text)

        with get_db_session() as db:
            memo = TroubleshootingMemory(
                category=category,
                error_pattern=error_pattern,
                root_cause=root_cause,
                solution=solution,
                embedding_json=json.dumps(vec),
            )
            db.add(memo)
            db.commit()
            db.refresh(memo)
            return memo.id

    async def search_relevant_memories(
        self,
        query_text: str,
        top_k: int | None = None,
        min_score: float = 0.18,
    ) -> list[TroubleshootingMemory]:
        """クエリ文字列（エラー出力や目標）に類似したトラブルシューティング知見を上位 K 件取得する．"""
        if not query_text.strip():
            return []

        top_k = top_k or get_rag_top_k()
        query_vec = await self.get_embedding(query_text)

        with get_db_session() as db:
            records = db.query(TroubleshootingMemory).all()
            if not records:
                return []

            scored_records: list[tuple[float, TroubleshootingMemory]] = []
            for rec in records:
                combined = f"{rec.category} | {rec.error_pattern} | {rec.root_cause}"
                rec_vec: list[float] = []

                if rec.embedding_json:
                    try:
                        rec_vec = json.loads(rec.embedding_json)
                    except Exception:
                        rec_vec = []

                # 未ベクトル化または次元数不一致（API⇔ローカル切替時）ならオンザフライで再計算して更新
                if len(rec_vec) != len(query_vec):
                    if len(query_vec) == LOCAL_VEC_DIM:
                        rec_vec = _compute_local_hash_embedding(combined)
                    else:
                        rec_vec = await self.get_embedding(combined)
                    rec.embedding_json = json.dumps(rec_vec)
                    db.commit()

                score = _cosine_similarity(query_vec, rec_vec)
                if score >= min_score:
                    db.expunge(rec)
                    scored_records.append((score, rec))

        scored_records.sort(key=lambda item: item[0], reverse=True)
        return [rec for _, rec in scored_records[:top_k]]

    def format_as_prompt_context(
        self, memories: list[TroubleshootingMemory], header: str = "Relevant Past Troubleshooting Knowledge (RAG)"
    ) -> str:
        """検索された知見リストをプロンプト注入用のテキストに整形する．"""
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


# シングルトンインスタンス
rag_store = TroubleshootingRAGStore()