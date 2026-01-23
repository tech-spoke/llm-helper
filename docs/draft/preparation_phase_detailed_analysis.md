# 準備フェーズの詳細分析

## 実測データ

**時刻**: 16:00:43 → 16:02:40（約117秒）
**ツール呼び出し**: 3回

## タイムライン（推測を含む）

```
16:00:43  sync_index 開始
  ↓
  ↓ [11.5秒: ChromaDB同期実行]
  ↓   - 3ファイル変更検出
  ↓   - 約16チャンク再生成
  ↓   - Embedding計算（multilingual-e5-small）
  ↓
16:00:54  sync_index 完了（推測）
  ↓
  ↓ [数秒: LLMがレスポンス受信・処理]
  ↓
16:00:56  start_sessionのレスポンスに"doc_research.enabled: true"
          → LLMがDOCUMENT_RESEARCH実行を判断
  ↓
  ↓ [約30-40秒: DOCUMENT_RESEARCH実行]
  ↓   - Task tool で Explore agent起動
  ↓   - documents/ フォルダを探索
  ↓   - ls, grep, read ツール使用
  ↓   - マークダウンファイル検索・読み取り
  ↓   - mandatory_rules抽出
  ↓
16:01:26  DOCUMENT_RESEARCH 完了（推測）
  ↓
  ↓ [約60秒: LLM思考]
  ↓   - 調査結果の統合
  ↓   - mandatory_rulesの確認
  ↓   - 次のステップ（set_query_frame）の準備
  ↓
16:02:32  set_query_frame 開始
  ↓
  ↓ [数秒: クエリ構造化]
  ↓   - extraction_promptに従ってスロット抽出
  ↓   - target_feature, observed_issue など
  ↓
16:02:35  set_query_frame 完了（推測）
  ↓
  ↓ [数秒: LLM思考]
  ↓   - 次はbegin_phase_gateと判断
  ↓
16:02:40  begin_phase_gate 開始
  ↓
  ↓ [数秒: ブランチ作成]
  ↓   - Git操作
  ↓   - セッション状態更新
  ↓
16:02:48  EXPLORATION フェーズ開始
```

## 内訳（推測）

| 処理 | 所要時間 | 割合 |
|------|----------|------|
| sync_index 実行 | 11.5秒 | 10% |
| DOCUMENT_RESEARCH | 30-40秒 | 26-34% |
| LLM思考（全体） | 65-75秒 | 56-64% |
| - sync完了後の判断 | 2秒 | 2% |
| - DOCUMENT_RESEARCH後の統合 | 60秒 | 51% |
| - set_query_frame後の判断 | 3秒 | 3% |
| set_query_frame 実行 | 3秒 | 3% |
| begin_phase_gate 実行 | 5秒 | 4% |
| **合計** | **117秒** | **100%** |

## ボトルネック分析

### 1位: LLM思考時間（65-75秒、56-64%）

特に、**DOCUMENT_RESEARCH後の統合**（60秒）が最大のボトルネック。

**内容（推測）**:
- Sub-agentからの結果を読み取り
- mandatory_rulesを理解
- どのルールが今回のタスクに関連するか判断
- 次のステップを決定

**問題点**:
- ツール選択の思考が含まれている（不要）
- mandatory_rulesの統合に時間がかかりすぎ

### 2位: DOCUMENT_RESEARCH（30-40秒、26-34%）

**内容**:
- documents/ フォルダを探索
- モーダル関連のドキュメント検索
- 実際には関連ドキュメントが存在しない場合が多い

**問題点**:
- 毎回実行されるが、実際に有用な情報が得られないことが多い
- Explore agentの起動オーバーヘッド（2-3秒）
- 不要な探索を行っている

### 3位: sync_index（11.5秒、10%）

**内容**:
- 3ファイルの変更を検出
- 約16チャンクの再Embedding

**問題点（現時点では優先度低）**:
- Embedding関数が未設定（デフォルト使用）
- バッチ処理されていない

### 4位以下

- set_query_frame: 3秒（軽い）
- begin_phase_gate: 5秒（Git操作含む、削減余地小）

---

## 改善策の優先度

### 🔥 最優先（60秒削減）

**DOCUMENT_RESEARCH後のLLM思考を削減**

現在のフロー：
```
DOCUMENT_RESEARCH完了
  ↓ (60秒: LLMが結果を統合して次を判断)
set_query_frame
```

改善後：
```
DOCUMENT_RESEARCH + set_query_frame + begin_phase_gate を1ツールで
  ↓ (0秒: 思考不要、自動で次に進む)
全結果返却
```

**削減**: 60秒

---

### 🔥 高優先（30-40秒削減）

**DOCUMENT_RESEARCHのデフォルト無効化**

オプション1: context.yml で doc_research.enabled: false
オプション2: code.md で DOCUMENT_RESEARCH をデフォルトスキップ

**削減**: 30-40秒

---

### ⚡ 中優先（6-10秒削減）

**準備フェーズのバッチ化**

現在:
```
sync_index → LLM → set_query_frame → LLM → begin_phase_gate
```

改善:
```
prepare_session_batch()
  ├─ sync_index
  ├─ set_query_frame（LLMにクエリ解析依頼）
  └─ begin_phase_gate
→ 1往復で完了
```

**削減**: LLM往復削減で 6-10秒

---

### 💡 低優先（3-5秒削減）

**sync_index の高速化**
- Embeddingのバッチ処理
- embedding_function の明示的設定

**削減**: 3-5秒

---

## 合計削減見込み

| 施策 | 削減時間 | 実装難易度 | 優先度 |
|------|----------|------------|--------|
| DOCUMENT_RESEARCH後の思考削減 | 60秒 | 中 | 🔥🔥🔥 |
| DOCUMENT_RESEARCHのデフォルト無効化 | 30-40秒 | 低 | 🔥🔥 |
| 準備フェーズバッチ化 | 6-10秒 | 中 | ⚡ |
| sync_index高速化 | 3-5秒 | 低 | 💡 |
| **合計** | **99-115秒** | - | - |

**現在**: 117秒
**改善後**: 2-18秒（**83-98%削減**）

---

## 次のアクション候補

### A. 即効性重視（設定変更のみ）

1. **DOCUMENT_RESEARCHをデフォルト無効化**
   - code.md を修正
   - 削減: 30-40秒
   - 実装時間: 5分

2. **AVITOプロジェクトでテスト**
   - `/code --no-doc-research` で確認
   - 効果測定

### B. 根本対応（コード変更）

1. **prepare_session_batch ツール実装**
   - sync + query_frame + phase_gate を統合
   - 削減: 6-10秒
   - 実装時間: 1-2時間

2. **DOCUMENT_RESEARCHの選択的実行**
   - 必要な場合のみ実行（LLMが判断）
   - 削減: 平均20秒
   - 実装時間: 30分-1時間

---

## 推奨アプローチ

### Phase 1: 即効対策（今すぐ）
✅ DOCUMENT_RESEARCHをデフォルト無効化
→ 30-40秒削減

### Phase 2: バッチ化（数時間後）
✅ prepare_session_batch 実装
→ さらに6-10秒削減

### Phase 3: 最適化（余裕があれば）
✅ DOCUMENT_RESEARCHの選択的実行
✅ sync_index 高速化

---

どのアプローチから始めますか？
