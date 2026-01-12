"""
Embedding Validator for v3.7.

v3.7: ベクトル類似度による意味的一致判定
- 3層判定ロジック（>0.6 FACT, 0.3-0.6 HIGH risk, <0.3 REJECT）
- 遅延ロード（Lazy Loading）
- E5モデルの特性を活かした query: 接頭辞
- キャメルケース分解による精度向上
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidationResult:
    """Embedding検証の結果"""
    approved: bool
    status: str  # "FACT", "FACT_HIGH_RISK", "REJECTED"
    similarity: float
    risk_adjustment: Optional[str] = None  # "HIGH" if gray zone
    warning: Optional[str] = None
    reinvestigation_guidance: Optional[dict] = None

    def to_dict(self) -> dict:
        result = {
            "approved": self.approved,
            "status": self.status,
            "similarity": round(self.similarity, 3),
        }
        if self.risk_adjustment:
            result["risk_adjustment"] = self.risk_adjustment
        if self.warning:
            result["warning"] = self.warning
        if self.reinvestigation_guidance:
            result["reinvestigation_guidance"] = self.reinvestigation_guidance
        return result


class EmbeddingValidator:
    """
    v3.7: ベクトル類似度による意味的一致判定。

    - 遅延ロード（初回呼び出し時にモデルをロード）
    - 3層判定ロジック
    - E5モデル用の query: 接頭辞
    - キャメルケース分解
    """

    # 3層判定の閾値
    THRESHOLD_HIGH = 0.6   # これ以上 → FACT として承認
    THRESHOLD_LOW = 0.3    # これ未満 → 物理的拒否

    def __init__(self, model_name: str = "intfloat/multilingual-e5-small"):
        self.model_name = model_name
        self._model = None
        self._available = None  # None = not checked, True/False = checked

    @property
    def model(self):
        """遅延ロード: 最初の呼び出し時にのみモデルをロード"""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
                self._available = True
            except ImportError:
                self._available = False
                raise ImportError(
                    "sentence-transformers is not installed. "
                    "Run: pip install sentence-transformers"
                )
        return self._model

    def is_available(self) -> bool:
        """Embeddingが利用可能か確認（モデルをロードせずに）"""
        if self._available is not None:
            return self._available
        try:
            import sentence_transformers
            self._available = True
        except ImportError:
            self._available = False
        return self._available

    def get_similarity(self, text1: str, text2: str) -> float:
        """2つのテキストのコサイン類似度を計算"""
        from sklearn.metrics.pairwise import cosine_similarity

        # E5モデルの特性を活かすため接頭辞を付与
        sentences = [f"query: {text1}", f"query: {text2}"]
        embeddings = self.model.encode(sentences)

        sim = cosine_similarity([embeddings[0]], [embeddings[1]])[0][0]
        return float(sim)

    def validate_relevance(
        self,
        nl_term: str,
        symbol: str,
        llm_approved: bool = True,
        code_evidence: Optional[str] = None,
    ) -> ValidationResult:
        """
        NL用語とシンボルの関連性を3層判定で検証。

        Args:
            nl_term: 自然言語の用語（「ログイン機能」）
            symbol: コード上のシンボル（「AuthService」）
            llm_approved: LLMが関連ありと判定したか
            code_evidence: LLMが提供したコード上の根拠

        Returns:
            ValidationResult with approval status and details
        """
        # キャメルケースを分解して類似度計算
        symbol_normalized = self.split_camel_case(symbol)
        similarity = self.get_similarity(nl_term, symbol_normalized)

        if similarity > self.THRESHOLD_HIGH:
            # 高信頼: FACT として承認
            return ValidationResult(
                approved=True,
                status="FACT",
                similarity=similarity,
            )

        elif similarity >= self.THRESHOLD_LOW:
            # グレーゾーン: 承認するがリスク引き上げ
            return ValidationResult(
                approved=True,
                status="FACT",
                similarity=similarity,
                risk_adjustment="HIGH",
                warning=f"類似度 {similarity:.2f} はグレーゾーン。探索を強化してください。",
            )

        else:
            # 物理的拒否
            return ValidationResult(
                approved=False,
                status="REJECTED",
                similarity=similarity,
                reinvestigation_guidance=self._create_guidance(nl_term, symbol),
            )

    def validate_multiple(
        self,
        nl_term: str,
        symbols: list[str],
    ) -> dict:
        """
        複数シンボルの関連性を一括検証。

        Returns:
            {
                "approved": [...],
                "rejected": [...],
                "risk_adjustment": "HIGH" or None,
            }
        """
        approved = []
        rejected = []
        needs_high_risk = False

        for symbol in symbols:
            result = self.validate_relevance(nl_term, symbol)
            if result.approved:
                approved.append({
                    "symbol": symbol,
                    "similarity": result.similarity,
                    "status": result.status,
                })
                if result.risk_adjustment == "HIGH":
                    needs_high_risk = True
            else:
                rejected.append({
                    "symbol": symbol,
                    "similarity": result.similarity,
                    "guidance": result.reinvestigation_guidance,
                })

        return {
            "approved": approved,
            "rejected": rejected,
            "risk_adjustment": "HIGH" if needs_high_risk else None,
        }

    def find_related_symbols(
        self,
        nl_term: str,
        symbols: list[str],
        top_k: int = 5,
    ) -> list[dict]:
        """
        自然言語用語に最も関連するシンボルをベクトル検索で取得。

        Returns:
            Sorted list of {symbol, similarity, rank}
        """
        results = []
        for symbol in symbols:
            symbol_normalized = self.split_camel_case(symbol)
            similarity = self.get_similarity(nl_term, symbol_normalized)
            results.append({
                "symbol": symbol,
                "similarity": round(similarity, 3),
            })

        # 類似度でソート
        results.sort(key=lambda x: x["similarity"], reverse=True)

        # ランク付け
        for i, r in enumerate(results[:top_k]):
            r["rank"] = i + 1

        return results[:top_k]

    def _create_guidance(self, nl_term: str, symbol: str) -> dict:
        """拒否時の再調査ガイダンスを生成"""
        return {
            "reason": f"'{nl_term}' と '{symbol}' の類似度が低すぎます（< {self.THRESHOLD_LOW}）",
            "next_actions": [
                f"search_text で '{nl_term}' に関連する別のシンボルを探す",
                f"find_references で '{symbol}' の使用箇所を確認し、本当に関連するか検証",
                "関連性を証明するコード片（コメント、命名規則）を見つける",
            ],
            "fallback": "事実検索で見つからない場合は submit_semantic で DEVRAG を使用",
        }

    @staticmethod
    def split_camel_case(symbol: str) -> str:
        """
        キャメルケースをスペース区切りに分解。

        AuthService → Auth Service
        getUserName → get User Name
        """
        # 小文字→大文字の境界にスペースを挿入
        result = re.sub(r'([a-z])([A-Z])', r'\1 \2', symbol)
        # 連続する大文字の後に小文字が来る場合もスペースを挿入
        result = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', result)
        return result


# シングルトンインスタンス（遅延初期化）
_validator_instance: Optional[EmbeddingValidator] = None


def get_embedding_validator() -> EmbeddingValidator:
    """EmbeddingValidatorのシングルトンを取得"""
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = EmbeddingValidator()
    return _validator_instance


def is_embedding_available() -> bool:
    """Embedding機能が利用可能か確認（モデルをロードせずに）"""
    validator = get_embedding_validator()
    return validator.is_available()
