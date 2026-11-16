import json
import math
from dataclasses import dataclass
from .db_system import get_db_session
from .db_models import TroubleshootingMemory

@dataclass
class MemoryVectorCache:
    id: int
    vector: list[float]
    success_score: float # 実績に基づく信頼度 (0.0 ~ 1.5 等)

class HighPerformanceRAGStore:
    """インメモリキャッシュとハイブリッド検索を備えた次世代RAG"""

    def __init__(self):
        self._vector_cache: list[MemoryVectorCache] = []
        self._is_loaded = False

    def _load_cache_if_needed(self, db_session):
        """DBからベクトルをメモリに一括ロード（キャッシュ）して全件スキャンを回避"""
        if self._is_loaded:
            return
        
        records = db_session.query(TroubleshootingMemory).all()
        for rec in records:
            if not rec.embedding_json:
                continue
            try:
                vec = json.loads(rec.embedding_json)
                # 実績スコアの計算 (例: 成功率が高いほどスコアにボーナス)
                total_uses = rec.success_count + rec.fail_count
                trust_bonus = 1.0 + (0.1 * rec.success_count) if total_uses > 0 else 1.0
                
                self._vector_cache.append(MemoryVectorCache(id=rec.id, vector=vec, success_score=trust_bonus))
            except Exception:
                pass
        self._is_loaded = True

    async def search_best_solutions(self, query_vec: list[float], top_k: int = 3) -> list[TroubleshootingMemory]:
        """純粋なベクトル類似度 × 解決実績スコアの『ハイブリッド検索』"""
        with get_db_session() as db:
            self._load_cache_if_needed(db)
            
            scored_ids = []
            for cache in self._vector_cache:
                # コサイン類似度
                sim = sum(a * b for a, b in zip(query_vec, cache.vector))
                
                # ハイブリッドスコア = 類似度 × 実績ボーナス
                final_score = sim * cache.success_score
                if final_score > 0.15:  # 閾値
                    scored_ids.append((final_score, cache.id))
            
            # スコア順にソートし、上位K件のIDを取得
            scored_ids.sort(key=lambda x: x[0], reverse=True)
            top_ids = [cid for score, cid in scored_ids[:top_k]]

            if not top_ids:
                return []
            
            # DBからO(1)で実体を取得
            return db.query(TroubleshootingMemory).filter(TroubleshootingMemory.id.in_(top_ids)).all()

    def mark_memory_success(self, memory_id: int, is_success: bool):
        """エージェントがノウハウを使って成功/失敗した際に評価をフィードバック（自己学習）"""
        with get_db_session() as db:
            mem = db.query(TroubleshootingMemory).filter_by(id=memory_id).first()
            if mem:
                if is_success:
                    mem.success_count += 1
                else:
                    mem.fail_count += 1
                db.commit()
                self._is_loaded = False # キャッシュを無効化して再計算させる