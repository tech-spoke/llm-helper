# 設計ドラフト: コンテキスト提供と影響範囲分析

**Version:** Draft v1.03
**Date:** 2025-01-14
**Status:** 検討中
**Base Version:** Code Intelligence MCP Server v1.0

---

## 変更履歴

| バージョン | 日付 | 内容 |
|-----------|------|------|
| v0.1 | 2025-01-14 | 初版ドラフト |
| v1.02 | 2025-01-14 | LLM要約方式、マークアップ緩和、sync連携を追加 |
| v1.03 | 2025-01-14 | impact-rules.yml 廃止、project_rules 追加、extra_notes 追加、間接参照の扱いを明確化 |

---

## 背景と課題

### 現状の問題

1. **ドキュメント無視**: 現システムはシンボル探索に特化しており、設計ドキュメントやプロジェクトルールが無視されがち
2. **影響範囲の見落とし**: LLM は対象コードを修正するが、影響を受ける他のファイルの確認をスキップする傾向がある
3. **ルールのサボり**: CLAUDE.md 等にルールがあっても、LLM は読むのをサボりがち

### 目標

- 設計意図とプロジェクトルールを理解した上でコード探索を開始する
- 修正前に影響範囲を明示的に確認させる

---

## 機能1: 必須コンテキストの自動提供

### 概要

セッション開始時に、以下を自動的に LLM に提供する:

1. **設計ドキュメント要約** - アーキテクチャ、命名規則等
2. **プロジェクトルール** - CLAUDE.md 等の DO/DON'T

### 設計方針

| 項目 | 内容 |
|------|------|
| 提供形式 | LLM生成の要約 |
| 生成タイミング | sync_index 時（事前生成） |
| トークン消費 | 小（1,000-2,000トークン） |
| 詳細取得 | LLM が必要時に Read |

### 設定ファイル

```yaml
# .code-intel/context.yml

essential_docs:
  source: "docs/設計資料/アーキテクチャ"

project_rules:
  source: ".claude/CLAUDE.md"
```

設定は最小限。要約は自動生成される。

### 自動生成される構造

```yaml
# .code-intel/context.yml (sync_index により自動更新)

essential_docs:
  source: "docs/設計資料/アーキテクチャ"
  last_synced: "2025-01-14T10:30:00"
  summaries:
    - file: "全体アーキテクチャ.md"
      path: "docs/設計資料/アーキテクチャ/全体アーキテクチャ.md"
      summary: |
        3層レイヤード構成（Controller→Service→Repository）。
        ビジネスロジックは Service 層に集約。
        Filament Resource は CRUD 操作専用、ロジックを持たない。
      extra_notes: |
        # 手動追記欄（自動生成で漏れた暗黙知を補完）
        - Service 層では例外を throw せず Result 型で返す
        - DB トランザクションは Service 層で管理

    - file: "ドメイン境界.md"
      path: "docs/設計資料/アーキテクチャ/ドメイン境界.md"
      summary: |
        商品(Product)、会員(Customer)、受注(Order)の3ドメイン。
        ドメイン間の依存は Service 経由のみ。
        Model 間の直接リレーションは同一ドメイン内に限定。

project_rules:
  source: ".claude/CLAUDE.md"
  last_synced: "2025-01-14T10:30:00"
  summary: |
    DO:
    - Service 層でビジネスロジックを実装する
    - Repository パターンで DB アクセスを抽象化する
    - テストは Feature/Unit に分けて配置する
    - Model は app/Models/{Domain}/ 配下に配置する
    - Resource は app/Filament/Resources/{Domain}/ に作成する

    DON'T:
    - Controller に複雑なロジックを書かない
    - Model に直接クエリを書かない
    - Filament Resource でバリデーション以外のロジックを持たない
    - ドメインを跨ぐ Model 間リレーションを作らない
  extra_notes: |
    # 手動追記欄
```

### extra_notes フィールド

自動生成の要約で漏れる「暗黙の了解」を手動で補完するためのフィールド。

**用途:**
- LLM 要約が不十分な場合の補足
- ドキュメントに書かれていない暗黙のルール
- プロジェクト固有の注意事項

**運用:**
- 初回は空でOK
- 問題が発生したら追記していく
- sync_index で上書きされない（手動部分は保持）

### 要約生成の仕様

#### 設計ドキュメント用プロンプト

```
以下の設計ドキュメントから、実装時に守るべき決定事項と制約を抽出してください。
箇条書きで、各項目は1文以内で簡潔に。
技術的な命名規則、アーキテクチャの制約、禁止事項を優先してください。

---
{document_content}
---
```

#### プロジェクトルール用プロンプト

```
以下のプロジェクトルールから、コード実装時に守るべき
「DO（すべきこと）」と「DON'T（禁止事項）」を抽出してください。
箇条書きで、各項目は命令形で簡潔に。
ディレクトリ構造や命名規則があれば必ず含めてください。

---
{claude_md_content}
---
```

### sync_index との連携

```python
def sync_index():
    # 既存: コードファイル（森）の差分チェック
    code_changes = check_code_changes()
    update_code_index(code_changes)

    # 追加: essential_docs の差分チェック
    context_config = load_context_yml()

    if context_config:
        # 設計ドキュメント
        if "essential_docs" in context_config:
            doc_changes = check_doc_changes(context_config["essential_docs"]["source"])
            if doc_changes:
                regenerate_doc_summaries(doc_changes)

        # プロジェクトルール
        if "project_rules" in context_config:
            rules_changed = check_file_changed(context_config["project_rules"]["source"])
            if rules_changed:
                regenerate_rules_summary()

        save_context_yml(context_config)
```

### start_session での提供

```python
def start_session(intent: str, query: str) -> dict:
    context = load_context_yml()

    essential_context = {}

    if context and "essential_docs" in context:
        essential_context["design_docs"] = {
            "source": context["essential_docs"]["source"],
            "summaries": context["essential_docs"]["summaries"]
        }

    if context and "project_rules" in context:
        essential_context["project_rules"] = {
            "source": context["project_rules"]["source"],
            "summary": context["project_rules"]["summary"],
            "extra_notes": context["project_rules"].get("extra_notes", "")
        }

    return {
        "success": True,
        "session_id": generate_session_id(),
        "essential_context": essential_context,
        "query_frame": { ... }
    }
```

### レスポンス例

```json
{
  "success": true,
  "session_id": "abc123",
  "essential_context": {
    "design_docs": {
      "source": "docs/設計資料/アーキテクチャ",
      "summaries": [
        {
          "file": "全体アーキテクチャ.md",
          "summary": "3層レイヤード構成。ビジネスロジックは Service 層に集約...",
          "extra_notes": "Service 層では例外を throw せず Result 型で返す"
        }
      ]
    },
    "project_rules": {
      "source": ".claude/CLAUDE.md",
      "summary": "DO:\n- Service 層でビジネスロジックを実装する\n...\nDON'T:\n- Controller に複雑なロジックを書かない\n...",
      "extra_notes": ""
    }
  },
  "query_frame": { ... }
}
```

### 設定がない場合

- `context.yml` が存在しない場合はスキップ
- `essential_docs` または `project_rules` がない場合は該当部分のみスキップ
- 警告は出さない（後方互換性維持）

---

## 機能2: 影響範囲分析の強制

### 概要

READY フェーズ移行前に、修正対象の影響範囲を明示的に確認させる。

### 設計方針

**v1.02 からの変更:**
- `impact-rules.yml` は廃止
- フレームワーク固有のパスパターンは定義しない
- LLM が `project_rules` と `design_docs` の要約から推論する

**理由:**
- パスパターンのメンテナンスは現実的でない
- CLAUDE.md に構造ルールが書いてあれば、LLM はそこから推論できる

**影響分析の深度:**
- ツールは **直接参照のみ** を検出する（1段階）
- 間接参照（2段階以上）は LLM の判断に委ねる
- LLM が必要と判断すれば `find_references` で追加調査可能

### 新ツール: `analyze_impact`

```
mcp__code-intel__analyze_impact
  target_files: ["app/Models/Product.php"]
  change_description: "price フィールドの型を変更"
```

### レスポンス

```json
{
  "impact_analysis": {
    "mode": "standard",
    "depth": "direct_only",
    "static_references": {
      "callers": [
        {"file": "app/Services/CartService.php", "line": 45, "context": "$product->price"},
        {"file": "app/Http/Controllers/Api/ProductController.php", "line": 23}
      ],
      "type_hints": [
        {"file": "app/Contracts/PricingInterface.php", "line": 12}
      ]
    },
    "naming_convention_matches": {
      "tests": ["tests/Feature/ProductTest.php", "tests/Unit/ProductModelTest.php"],
      "factories": ["database/factories/ProductFactory.php"],
      "seeders": ["database/seeders/ProductSeeder.php"]
    },
    "inference_hint": "project_rules に基づき、関連する Resource や Policy も確認してください"
  },
  "confirmation_required": {
    "must_verify": [
      "app/Services/CartService.php",
      "app/Http/Controllers/Api/ProductController.php"
    ],
    "should_verify": [
      "tests/Feature/ProductTest.php",
      "database/factories/ProductFactory.php"
    ],
    "llm_should_infer": [
      "project_rules の命名規則に従い、対応する Resource/Policy を確認"
    ],
    "indirect_note": "間接参照（2段階以上）が必要な場合は find_references で追加調査してください",
    "schema": {
      "verified_files": [
        {
          "file": "string",
          "status": "will_modify | no_change_needed | not_affected",
          "reason": "string (status != will_modify 時は必須)"
        }
      ]
    }
  }
}
```

### LLM の推論責任

`analyze_impact` は静的に検出できるものだけを返す。

**LLM が project_rules から推論すべきもの:**

```
project_rules に以下がある場合:
  "Model は app/Models/{Domain}/ 配下に配置"
  "Resource は app/Filament/Resources/{Domain}/ に作成"

app/Models/Shop/Product.php を修正するなら:
  → app/Filament/Resources/Shop/ProductResource.php も確認すべき
```

これは `impact-rules.yml` で定義するのではなく、LLM が要約から推論する。

### 間接参照の扱い

**問題:** 依存関係が連鎖する場合がある

```
Model A 変更
    ↓ (直接参照 - ツールが検出)
Service B が Model A を使用
    ↓ (間接参照 - ツールは検出しない)
Controller C が Service B を使用
    ↓ (2段階間接)
Job D が Controller C を呼び出し
```

**設計決定:**

| 参照レベル | 検出方法 | 確認義務 |
|-----------|----------|----------|
| 直接参照（1段階） | ツールが自動検出 | must_verify |
| 間接参照（2段階以上） | LLM が判断して追加調査 | LLM の裁量 |

**理由:**
- 再帰的な全探索はノイズが多く、パフォーマンスも悪い
- 直接参照を確認した時点で、LLM は追加調査の必要性を判断できる
- 必要なら `find_references` を追加で呼べば良い

**LLM の判断例:**

```
analyze_impact の結果:
  must_verify: ["app/Services/CartService.php"]

LLM の判断:
  "CartService は重要なビジネスロジックを含むため、
   これを使用している Controller も確認すべき"
  → find_references("CartService") を追加実行
```

### マークアップ緩和モード

v1.1 で導入済みの「マークアップコンテキスト緩和」を `analyze_impact` にも適用。

**対象ファイル:**
| 拡張子 | 緩和 |
|--------|------|
| `.html`, `.htm`, `.css`, `.scss`, `.md` | ✅ 緩和適用 |
| `.blade.php`, `.vue`, `.jsx`, `.tsx` | ❌ 緩和なし（ロジック結合） |
| `.php`, `.py`, `.js`, `.ts` 等 | ❌ 緩和なし |

**緩和時のレスポンス:**

```json
{
  "impact_analysis": {
    "mode": "relaxed_markup",
    "reason": "対象ファイルがマークアップのみのため緩和モード適用",
    "static_references": [],
    "naming_convention_matches": {},
    "inference_hint": null
  },
  "confirmation_required": {
    "must_verify": [],
    "should_verify": [],
    "llm_should_infer": [],
    "schema": { ... }
  }
}
```

### LLM の応答義務

`analyze_impact` 呼び出し後、LLM は以下を宣言する必要がある:

```json
{
  "verified_files": [
    {
      "file": "app/Services/CartService.php",
      "status": "will_modify",
      "reason": null
    },
    {
      "file": "app/Filament/Resources/Shop/ProductResource.php",
      "status": "no_change_needed",
      "reason": "price フィールドの表示形式は変更不要（既に decimal 対応済み）"
    },
    {
      "file": "tests/Feature/ProductTest.php",
      "status": "will_modify",
      "reason": null
    }
  ],
  "inferred_from_rules": [
    "project_rules の命名規則から ProductResource.php を確認対象に追加"
  ]
}
```

### 検証ルール

| 条件 | 結果 |
|------|------|
| `must_verify` の全ファイルに回答あり | ✅ READY へ移行可 |
| `must_verify` に未回答あり | ❌ ブロック、再確認を要求 |
| `should_verify` 未回答 | ⚠️ 警告のみ、移行は許可 |
| マークアップ緩和モード | ✅ must_verify 空のため即移行可 |

---

## 影響検出のロジック

### 1. 静的参照（汎用）

```python
def find_static_references(symbol: str) -> list:
    """
    既存の find_references を活用
    - メソッド呼び出し
    - プロパティアクセス
    - 型ヒント
    - import/use 文
    """
    return ctags_find_references(symbol)
```

### 2. 命名規則ベース（汎用・固定）

```python
def find_by_naming_convention(file_path: str) -> dict:
    """
    ファイル名から関連ファイルを推測（汎用パターンのみ）
    """
    base_name = extract_base_name(file_path)  # "Product"

    return {
        "tests": glob(f"**/tests/**/*{base_name}*Test.*"),
        "factories": glob(f"**/*{base_name}Factory.*"),
        "seeders": glob(f"**/*{base_name}Seeder.*"),
    }
```

### 3. LLM 推論（project_rules ベース）

フレームワーク固有のパターン（Model→Resource 等）は、LLM が `project_rules` の要約から推論する。

**ツールが行うこと:**
- 静的参照の検出
- 汎用命名規則の適用
- 「project_rules に基づき追加確認せよ」のヒント提示

**LLM が行うこと:**
- project_rules の要約を読んで関連ファイルを推論
- 推論結果を `inferred_from_rules` として宣言

---

## フェーズ統合

### 改訂版フロー

```
Step 0: 失敗チェック
    ↓
Step 1: Intent 判定
    ↓
Step 2: セッション開始
    ├── essential_docs 要約を読み込み ← NEW
    ├── project_rules 要約を読み込み ← NEW
    └── session_id 発行
    ↓
Step 3: QueryFrame 設定
    ↓
Step 4: EXPLORATION
    ↓
Step 5: シンボル検証
    ↓
Step 6: SEMANTIC（必要時）
    ↓
Step 7: VERIFICATION（必要時）
    ↓
Step 8: IMPACT ANALYSIS ← NEW
    ├── analyze_impact 呼び出し
    ├── マークアップ緩和判定
    ├── 静的参照 + 命名規則の確認
    └── project_rules から追加影響を推論・宣言
    ↓
Step 9: READY（実装許可）
```

### IMPACT ANALYSIS フェーズの要件

| 条件 | 要件 |
|------|------|
| 通常モード | `analyze_impact` 呼び出し + `must_verify` 全回答 + 推論宣言 |
| マークアップ緩和 | `analyze_impact` 呼び出しのみ |
| INVESTIGATE intent | フェーズ自体をスキップ |

---

## 実装計画

### Phase 1: 必須コンテキスト提供

1. `context.yml` パーサー実装
2. 設計ドキュメント要約生成ロジック
3. プロジェクトルール要約生成ロジック（DO/DON'T形式）
4. `sync_index` へのドキュメント差分検出統合
5. `start_session` での要約提供
6. `extra_notes` の手動追記サポート

### Phase 2: 影響範囲分析

1. `analyze_impact` ツール実装
2. マークアップ緩和判定ロジック
3. 静的参照検出（find_references 活用）
4. 汎用命名規則マッチング
5. LLM 応答の検証ロジック
6. `inferred_from_rules` の記録

### Phase 3: 運用改善（将来）

1. 要約品質のフィードバックループ
2. extra_notes の蓄積と活用
3. 失敗パターンからの学習

---

## 未解決の検討事項

1. **要約の品質保証**: LLM 生成の要約が不十分な場合 → `extra_notes` で補完
2. **大量ドキュメント**: essential_docs 配下に大量のファイルがある場合の制限
3. **推論の検証**: LLM が project_rules から正しく推論しているかの確認方法

※ 影響分析の深度については「間接参照の扱い」セクションで設計決定済み

---

## 参考: v1.02 からの変更点

| 項目 | v1.02 | v1.03 |
|------|-------|-------|
| impact-rules.yml | あり（パスパターン定義） | **廃止** |
| project_rules | なし | **追加**（CLAUDE.md 要約） |
| extra_notes | なし | **追加**（手動補完欄） |
| フレームワークヒント | ツールが提供 | **LLM が推論** |
| プリセット | 将来検討 | **不要**（project_rules で代替） |
| 間接参照 | 未定義 | **直接のみ検出、間接は LLM 判断** |
