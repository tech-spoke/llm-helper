# Code Intelligence MCP Server v1.0 内部動作ドキュメント

このドキュメントは、システムの内部動作を他のAIが理解できるレベルで詳細に説明します。

---

## 目次

1. [Router](#router)
2. [SessionState](#sessionstate)
3. [QueryFrame](#queryframe)
4. [ChromaDB Manager](#chromadb-manager)
5. [AST Chunker](#ast-chunker)
6. [Embedding](#embedding)
7. [ツール実装詳細](#ツール実装詳細)
8. [改善サイクル](#改善サイクル)

---

## Router

`tools/router.py`

### 概要

Router は自然言語クエリを解析し、適切なツール実行計画（ExecutionPlan）を生成します。

### クラス構造

```python
class Router:
    """クエリを解析してツール実行計画を生成"""

    def create_plan(
        self,
        query_frame: QueryFrame,
        intent: str,
        context: dict
    ) -> ExecutionPlan:
        """
        QueryFrame と intent からExecutionPlanを生成

        処理フロー:
        1. QueryFrame のスロットを分析
        2. Intent に基づいて必要なフェーズを決定
        3. 各フェーズで使用するツールを選択
        4. DecisionLog を生成
        5. ExecutionPlan を返却
        """
```

### ExecutionPlan

```python
@dataclass
class ExecutionPlan:
    steps: list[ExecutionStep]      # 実行するツールのリスト
    reasoning: str                   # 計画の理由
    needs_bootstrap: bool            # 初期探索が必要か
    decision_log: DecisionLog | None # 決定ログ
    intent: IntentType               # Intent種別
    risk_level: str                  # HIGH/MEDIUM/LOW
    missing_slots: list[str]         # 未解決スロット
```

### DecisionLog

```python
@dataclass
class DecisionLog:
    """改善サイクル用の決定記録"""
    query: str                    # 元のクエリ
    timestamp: str                # タイムスタンプ
    intent: str                   # Intent種別
    required_phases: list[str]    # 必要なフェーズ
    missing_slots: list[str]      # 未解決スロット
    risk_level: str               # リスクレベル
    tools_planned: list[str]      # 計画されたツール
    needs_bootstrap: bool         # 初期探索要否
    bootstrap_reason: str | None  # 初期探索の理由
    session_id: str | None        # セッションID
```

### ツール選択ロジック

```python
def _select_tools(self, query_frame: QueryFrame, intent: str) -> list[str]:
    """
    スロットとIntentに基づいてツールを選択

    ルール:
    - target_feature が不明 → find_definitions, get_symbols
    - trigger_condition が不明 → search_text
    - observed_issue が不明 → search_text, query
    - desired_action が不明 → find_references, analyze_structure

    Intent による調整:
    - IMPLEMENT: 全ツール推奨
    - MODIFY: find_references 優先
    - INVESTIGATE: query, analyze_structure 優先
    - QUESTION: 最小限
    """
```

---

## SessionState

`tools/session.py`

### 概要

SessionState は、1つの実装セッションの状態を管理します。フェーズ遷移、バリデーション、ツール使用履歴を追跡します。

### クラス構造

```python
class SessionState:
    session_id: str
    intent: str                    # IMPLEMENT/MODIFY/INVESTIGATE/QUESTION
    query: str                     # 元のクエリ
    phase: Phase                   # 現在のフェーズ
    created_at: str
    repo_path: str                 # プロジェクトパス

    # フェーズ結果
    exploration: ExplorationResult | None
    semantic: SemanticResult | None
    verification: VerificationResult | None

    # QueryFrame
    query_frame: QueryFrame | None
    risk_level: str

    # ツール使用履歴
    tool_calls: list[dict]
```

### Phase Enum

```python
class Phase(Enum):
    EXPLORATION = "exploration"     # コード探索
    SEMANTIC = "semantic"           # セマンティック検索
    VERIFICATION = "verification"   # 仮説検証
    READY = "ready"                 # 実装許可
```

### フェーズ遷移

```python
def submit_understanding(self, ...) -> dict:
    """
    EXPLORATION フェーズ完了

    処理:
    1. 入力の整合性チェック（validate_exploration_consistency）
    2. 探索結果の評価（evaluate_exploration）
    3. NL→Symbol マッピング検証
    4. 次フェーズ決定

    遷移先:
    - 評価 "high" + 整合性OK → READY
    - それ以外 → SEMANTIC
    """

def submit_semantic(self, ...) -> dict:
    """
    SEMANTIC フェーズ完了

    処理:
    1. devrag_reason の妥当性チェック（validate_semantic_reason）
    2. 仮説の記録
    3. VERIFICATION へ遷移
    """

def submit_verification(self, ...) -> dict:
    """
    VERIFICATION フェーズ完了

    処理:
    1. 各仮説の検証結果を記録
    2. READY へ遷移
    """
```

### バリデーション関数

```python
def validate_exploration_consistency(
    symbols: list[str],
    entry_points: list[str],
    files: list[str],
    patterns: list[str]
) -> tuple[bool, list[str]]:
    """
    探索結果の整合性チェック

    チェック項目:
    - entry_points が symbols に紐付いているか
    - 重複した symbols/files がないか
    - patterns があるなら files も必須

    Returns:
        (is_valid, error_messages)
    """

def validate_semantic_reason(
    reason: str,
    missing_requirements: list[str]
) -> tuple[bool, str]:
    """
    SEMANTIC 理由の妥当性チェック

    許可される組み合わせ:
    - symbols_identified 不足 → no_definition_found, architecture_unknown
    - entry_points 不足 → no_definition_found, no_reference_found
    - patterns 不足 → no_similar_implementation, architecture_unknown
    - files 不足 → context_fragmented, architecture_unknown
    """

def validate_write_target(
    file_path: str,
    explored_files: list[str],
    allow_new_files: bool
) -> tuple[bool, str]:
    """
    書き込み対象の検証

    ルール:
    - 既存ファイル → explored_files に含まれている必要あり
    - 新規ファイル → allow_new_files=True かつ親ディレクトリが探索済み
    """
```

### 探索評価

```python
def evaluate_exploration(
    intent: str,
    symbols_count: int,
    entry_points_count: int,
    files_count: int,
    patterns_count: int,
    tools_used: set[str]
) -> tuple[str, list[str]]:
    """
    探索結果を評価

    Intent別要件:
    - IMPLEMENT/MODIFY:
        - symbols: 3個以上
        - entry_points: 1個以上
        - files: 2個以上
        - patterns: 1個以上
        - tools: find_definitions, find_references 使用済み

    - INVESTIGATE:
        - symbols: 1個以上
        - files: 1個以上

    - QUESTION:
        - 要件なし（即座に READY）

    Returns:
        (confidence: "high"/"low", missing_requirements)
    """
```

### 復帰機能

```python
def add_explored_files(self, files: list[str]) -> dict:
    """
    READY フェーズで探索済みファイルを追加

    ユースケース:
    - check_write_target でブロックされた場合
    - 新しいディレクトリにファイルを作成したい場合
    """

def revert_to_exploration(self, keep_results: bool = True) -> dict:
    """
    EXPLORATION フェーズに戻る

    ユースケース:
    - 追加の探索が必要な場合
    - 別のアプローチで再探索したい場合

    keep_results=True: 既存の探索結果を保持
    keep_results=False: 全てリセット
    """
```

---

## QueryFrame

`tools/query_frame.py`

### 概要

QueryFrame は自然言語クエリを構造化し、「何が分かっていて、何が不明か」を明確にします。

### クラス構造

```python
@dataclass
class QueryFrame:
    raw_query: str                           # 元のクエリ
    target_feature: str | None = None        # 対象機能
    trigger_condition: str | None = None     # 発生条件
    observed_issue: str | None = None        # 観察された問題
    desired_action: str | None = None        # 期待する動作

    # スロットのソース（FACT/HYPOTHESIS）
    slot_sources: dict[str, SlotSource] = field(default_factory=dict)

    # 検証済みスロット
    validated_slots: list[str] = field(default_factory=list)

    # NL→Symbol マッピング
    mapped_symbols: list[str] = field(default_factory=list)
```

### SlotSource

```python
class SlotSource(Enum):
    FACT = "fact"           # 探索から確定
    HYPOTHESIS = "hypothesis"  # セマンティック検索から推測
```

### スロット検証

```python
def validate_slot(
    slot_name: str,
    value: str,
    quote: str,
    original_query: str
) -> tuple[bool, str | None]:
    """
    スロットの妥当性を検証

    チェック:
    1. quote が original_query に存在するか
    2. value と quote の意味的一貫性

    Returns:
        (is_valid, error_message)
    """
```

### リスク評価

```python
def assess_risk_level(
    intent: str,
    query_frame: QueryFrame
) -> str:
    """
    リスクレベルを評価

    ルール:
    - MODIFY + observed_issue が不明 → HIGH
    - IMPLEMENT → MEDIUM
    - INVESTIGATE → LOW
    - 全スロット埋まっている → LOW
    """
```

### QueryDecomposer

```python
class QueryDecomposer:
    @staticmethod
    def get_extraction_prompt(query: str) -> str:
        """
        スロット抽出用プロンプトを生成

        LLM に渡すプロンプトで、以下を指示:
        1. query から4つのスロットを抽出
        2. 各スロットに quote（原文からの引用）を付与
        3. 該当なしの場合は省略
        """

    @staticmethod
    def decompose(query: str, llm_response: dict) -> QueryFrame:
        """
        LLM の応答から QueryFrame を構築

        処理:
        1. 各スロットの quote を検証
        2. 検証成功したスロットを validated_slots に追加
        3. slot_sources を FACT に設定
        """
```

---

## ChromaDB Manager

`tools/chromadb_manager.py`

### 概要

ChromaDB Manager は、Forest（森）と Map（地図）の2つのベクトルコレクションを管理します。

### クラス構造

```python
class ChromaDBManager:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self.code_intel_dir = self.repo_path / ".code-intel"
        self.chroma_dir = self.code_intel_dir / "chroma"

        # ChromaDB クライアント
        self.client = chromadb.PersistentClient(path=str(self.chroma_dir))

        # コレクション
        self.forest = self.client.get_or_create_collection("forest")
        self.map = self.client.get_or_create_collection("map")

        # 設定
        self.config = self._load_config()
```

### Forest（森）同期

```python
def sync_forest(self) -> SyncResult:
    """
    ソースコードを Forest コレクションに同期

    処理:
    1. source_dirs 内のファイルをスキャン
    2. SHA256 フィンガープリントで変更検出
    3. 変更ファイルのみ AST チャンキング
    4. チャンクを Embedding して ChromaDB に upsert
    5. sync_state.json を更新

    増分同期:
    - 新規ファイル → 追加
    - 変更ファイル → 更新
    - 削除ファイル → 削除
    """

def needs_sync(self) -> bool:
    """
    同期が必要かチェック

    条件:
    - sync_state.json がない
    - 最終同期から sync_ttl_hours 経過
    - ファイルのフィンガープリントが不一致
    """
```

### Map（地図）管理

```python
def index_agreements(self) -> int:
    """
    合意事項を Map コレクションにインデックス

    処理:
    1. .code-intel/agreements/*.md をスキャン
    2. 各ファイルを Embedding
    3. Map コレクションに追加

    Returns:
        追加された合意事項の数
    """

def add_agreement(
    self,
    nl_term: str,
    symbol: str,
    code_evidence: str,
    session_id: str
) -> str:
    """
    新しい合意事項を追加

    処理:
    1. .md ファイルを生成
    2. Map コレクションに追加

    Returns:
        生成されたファイルパス
    """
```

### 検索

```python
def search_forest(
    self,
    query: str,
    n_results: int = 10
) -> list[SearchHit]:
    """
    Forest コレクションを検索

    処理:
    1. query を Embedding
    2. ChromaDB で類似検索
    3. SearchHit リストを返却
    """

def search_map(
    self,
    query: str,
    n_results: int = 5
) -> list[SearchHit]:
    """
    Map コレクションを検索

    Short-circuit:
    - スコア >= 0.7 の結果があれば Forest 検索をスキップ
    """

def search(
    self,
    query: str,
    n_results: int = 10
) -> SearchResult:
    """
    統合検索

    処理:
    1. Map を検索
    2. Map で高スコア → short_circuit=True
    3. Forest を検索
    4. 結果をマージ
    """
```

### SearchResult

```python
@dataclass
class SearchResult:
    map_hits: list[SearchHit]      # Map からの結果
    forest_hits: list[SearchHit]   # Forest からの結果
    short_circuit: bool             # Map で十分だったか
    total_chunks: int               # 全チャンク数
    query: str                      # 検索クエリ
```

---

## AST Chunker

`tools/ast_chunker.py`

### 概要

AST Chunker は、ソースコードを構文解析し、意味のある単位（関数、クラス等）でチャンキングします。

### 対応言語

| 言語 | 拡張子 | チャンク単位 |
|------|--------|-------------|
| Python | .py | function, class, method |
| PHP | .php | function, class, method |
| JavaScript | .js, .jsx | function, class, arrow_function |
| TypeScript | .ts, .tsx | function, class, interface |
| Blade | .blade.php | component, directive |
| CSS | .css | rule, at_rule |

### チャンク構造

```python
@dataclass
class CodeChunk:
    file_path: str          # ファイルパス
    start_line: int         # 開始行
    end_line: int           # 終了行
    content: str            # コード内容
    symbol_name: str        # シンボル名（関数名、クラス名等）
    symbol_type: str        # シンボル種別（function, class等）
    language: str           # 言語
    fingerprint: str        # SHA256 ハッシュ
```

### チャンキング処理

```python
def chunk_file(file_path: str) -> list[CodeChunk]:
    """
    ファイルをチャンキング

    処理:
    1. 言語を拡張子から判定
    2. tree-sitter でパース
    3. 関数/クラス/メソッドノードを抽出
    4. 各ノードを CodeChunk に変換
    5. 最大トークン数を超えるチャンクは分割
    """

def chunk_directory(
    directory: str,
    exclude_patterns: list[str]
) -> list[CodeChunk]:
    """
    ディレクトリ内の全ファイルをチャンキング

    処理:
    1. ファイルをスキャン（exclude_patterns を除外）
    2. 各ファイルを chunk_file で処理
    3. 結果をマージ
    """
```

---

## Embedding

`tools/embedding.py`

### 概要

Embedding は、テキストをベクトル表現に変換します。multilingual-e5-small モデルを使用します。

### クラス構造

```python
class EmbeddingModel:
    def __init__(self, model_name: str = "multilingual-e5-small"):
        self.model = SentenceTransformer(model_name)
        self.dimension = 384  # multilingual-e5-small の次元数

    def encode(self, texts: list[str]) -> np.ndarray:
        """
        テキストを Embedding

        Returns:
            shape: (len(texts), 384)
        """

    def similarity(self, text1: str, text2: str) -> float:
        """
        2つのテキストの類似度を計算

        Returns:
            コサイン類似度 (0.0 - 1.0)
        """
```

### 類似度判定

```python
def validate_symbol_relevance(
    target_feature: str,
    symbols: list[str]
) -> dict:
    """
    シンボルの関連性を Embedding で検証

    3層判定:
    - 類似度 > 0.6: FACT として承認
    - 類似度 0.3-0.6: 承認するが risk_level を HIGH に引き上げ
    - 類似度 < 0.3: 拒否、再探索ガイダンスを提供
    """
```

---

## ツール実装詳細

### ctags_tool.py

Universal Ctags を使用してシンボル定義を検索します。

```python
async def find_definitions(
    symbol: str,
    path: str = ".",
    language: str | None = None,
    exact_match: bool = False
) -> dict:
    """
    シンボル定義を検索

    処理:
    1. ctags を実行（--output-format=json）
    2. 結果をパース
    3. symbol でフィルタ

    Returns:
        {
            "symbol": str,
            "definitions": [
                {
                    "name": str,
                    "file": str,
                    "line": int,
                    "kind": str,  # function, class, variable等
                    "scope": str,
                    "signature": str
                }
            ],
            "total": int
        }
    """
```

### ripgrep_tool.py

ripgrep を使用してテキスト検索を行います。

```python
async def search_text(
    pattern: str,
    path: str = ".",
    file_type: str | None = None
) -> dict:
    """
    テキスト検索

    処理:
    1. ripgrep を実行（--json）
    2. 結果をパース

    Returns:
        {
            "pattern": str,
            "matches": [
                {
                    "file": str,
                    "line": int,
                    "content": str,
                    "context_before": list[str],
                    "context_after": list[str]
                }
            ],
            "total": int
        }
    """

async def find_references(
    symbol: str,
    path: str = "."
) -> dict:
    """
    シンボル参照を検索（定義を除外）

    処理:
    1. find_definitions で定義位置を取得
    2. ripgrep で symbol を検索
    3. 定義位置を除外
    """
```

### treesitter_tool.py

tree-sitter を使用して構文解析を行います。

```python
async def analyze_structure(path: str) -> dict:
    """
    ファイル/ディレクトリの構造を解析

    Returns:
        {
            "path": str,
            "files": [
                {
                    "file": str,
                    "language": str,
                    "symbols": [
                        {
                            "name": str,
                            "type": str,
                            "start_line": int,
                            "end_line": int,
                            "children": [...]  # ネストした構造
                        }
                    ]
                }
            ]
        }
    """

async def get_function_at_line(
    file_path: str,
    line: int
) -> dict:
    """
    指定行を含む関数を取得

    Returns:
        {
            "file": str,
            "line": int,
            "function": {
                "name": str,
                "start_line": int,
                "end_line": int,
                "content": str
            }
        }
    """
```

---

## 改善サイクル

`tools/outcome_log.py`

### 概要

改善サイクルは、DecisionLog と OutcomeLog の2つのログを使用して、システムの改善に必要なデータを収集します。

### DecisionLog 記録

```python
def record_decision(decision_log: dict) -> dict:
    """
    決定を記録（自動）

    タイミング: query ツール実行時

    記録内容:
    - session_id
    - query
    - timestamp
    - intent
    - tools_planned
    - risk_level
    - missing_slots
    """
```

### OutcomeLog 記録

```python
def record_outcome(outcome_log: OutcomeLog) -> dict:
    """
    結果を記録（自動/手動）

    タイミング:
    - 自動: /code 開始時の失敗検出
    - 手動: /outcome スキル

    記録内容:
    - session_id
    - outcome (success/failure/partial)
    - phase_at_outcome
    - intent
    - devrag_used
    - analysis
    - trigger_message
    """
```

### 分析機能

```python
def get_session_analysis(session_id: str) -> dict:
    """
    セッションの決定+結果を結合

    Returns:
        {
            "session_id": str,
            "decision": dict | None,
            "outcomes": list[dict],
            "analysis": {
                "had_decision": bool,
                "had_outcome": bool,
                "final_outcome": str | None,
                "tools_planned": list[str],
                "failure_point": str | None
            }
        }
    """

def get_improvement_insights(limit: int = 100) -> dict:
    """
    失敗パターンを分析

    Returns:
        {
            "total_sessions_with_outcomes": int,
            "sessions_with_decisions": int,
            "tool_failure_correlation": dict,
            "risk_level_correlation": dict,
            "common_failure_points": dict
        }
    """
```

---

## データフロー全体図

```
[ユーザーリクエスト]
        │
        ▼
[/code スキル開始]
        │
        ├─→ [Step 0: 失敗チェック]
        │        │
        │        └─→ [record_outcome (failure)]
        │
        ├─→ [Step 1: Intent判定]
        │
        ├─→ [Step 2: start_session]
        │        │
        │        └─→ [SessionState 作成]
        │
        ├─→ [Step 3: set_query_frame]
        │        │
        │        └─→ [QueryFrame 設定]
        │
        ├─→ [Step 4: EXPLORATION]
        │        │
        │        ├─→ [find_definitions] → [ctags]
        │        ├─→ [find_references] → [ripgrep]
        │        └─→ [submit_understanding]
        │                  │
        │                  └─→ [evaluate_exploration]
        │                           │
        │                           ├─→ high → [READY]
        │                           └─→ low → [SEMANTIC]
        │
        ├─→ [Step 5: SEMANTIC]
        │        │
        │        ├─→ [semantic_search] → [ChromaDB]
        │        └─→ [submit_semantic]
        │
        ├─→ [Step 6: VERIFICATION]
        │        │
        │        └─→ [submit_verification]
        │
        └─→ [Step 7: READY]
                 │
                 ├─→ [check_write_target]
                 │        │
                 │        ├─→ allowed → [Edit/Write]
                 │        └─→ blocked → [add_explored_files] or [revert_to_exploration]
                 │
                 └─→ [実装完了]
```
