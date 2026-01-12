# Code Intelligence MCP Server v3.8

Cursor IDE のようなコードインテリジェンス機能をオープンソースツールで実現する MCP サーバー。

## 目的

同じ Opus 4.5 モデルでも、呼び出し元によって挙動が異なる：

| 呼び出し元 | 挙動 |
|-----------|------|
| **Cursor** | コードベース全体を理解した上で修正する |
| **Claude Code** | 修正箇所だけを見て修正する傾向がある |

この MCP サーバーは、Claude Code に「コードベースを理解させる」ための仕組みを提供する。

---

## 設計思想

```
LLM に判断をさせない。守らせるのではなく、守らないと進めない設計。
そして、失敗から学ぶ仕組みを持つ。
```

| 原則 | 実装 |
|------|------|
| フェーズ強制 | ツール使用制限（EXPLORATION で devrag 禁止等） |
| サーバー評価 | confidence はサーバーが算出、LLM の自己申告を排除 |
| 構造化入力 | Quote 検証による幻覚防止 |
| Embedding 検証 | NL→Symbol の関連性をベクトル類似度で客観評価 |
| Write 制限 | 探索済みファイルのみ許可 |
| 改善サイクル | Outcome Log + agreements による学習 |
| プロジェクト分離 | 各プロジェクトごとに独立した学習データ |

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────┐
│                    MCP Clients (Claude Code)                 │
└─────────────────────────────────────────────────────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        ┌─────────────┐ ┌─────────────┐ ┌─────────────────┐
        │ devrag-map  │ │devrag-forest│ │   code-intel    │
        │ (地図)      │ │ (森)        │ │ (オーケストレータ)│
        └─────────────┘ └─────────────┘ └─────────────────┘
               │               │                 │
               └───────────────┼─────────────────┘
                               ▼
                    ┌───────────────────┐
                    │ Project/.code-intel│  ← プロジェクト固有
                    │ ├─ vectors-map.db  │
                    │ ├─ vectors-forest.db│
                    │ ├─ agreements/     │
                    │ └─ learned_pairs.json│
                    └───────────────────┘
```

### プロジェクト分離

MCP サーバー（ロジック）は共有、学習データ（記憶）はプロジェクトごとに分離：

```
llm-helper/                    ← MCP サーバー本体（共有）
├── code_intel_server.py
├── setup.sh                   ← サーバーセットアップ
└── init-project.sh            ← プロジェクト初期化

ProjectA/.code-intel/          ← ProjectA 固有の学習データ
ProjectB/.code-intel/          ← ProjectB 固有の学習データ
```

### 森と地図

| 名称 | MCP サーバー | 役割 | データの性質 |
|------|-------------|------|-------------|
| **森 (Forest)** | devrag-forest | ソースコード全体の意味検索 | 生データ・HYPOTHESIS |
| **地図 (Map)** | devrag-map | 過去の成功ペア・合意事項 | 確定データ・FACT |

**Short-circuit Logic**: 地図でスコア ≥ 0.7 → 森の探索をスキップ

---

## フェーズゲート

```
EXPLORATION → VALIDATION → SEMANTIC → VERIFICATION → READY
     ↓            ↓           ↓           ↓           ↓
  code-intel  Embedding    devrag      検証       実装許可
   ツール      検証        (仮説)     (確定)
```

| フェーズ | 許可 | 禁止 |
|----------|------|------|
| EXPLORATION | code-intel ツール | devrag |
| SEMANTIC | devrag-forest | code-intel |
| VERIFICATION | code-intel ツール | devrag |
| READY | すべて | - |

---

## ツール一覧

### コードインテリジェンス

| ツール | 用途 |
|--------|------|
| `search_text` | テキスト検索 (ripgrep) |
| `search_files` | ファイル名検索 |
| `find_definitions` | シンボル定義検索 (ctags) |
| `find_references` | シンボル参照検索 (ctags) |
| `get_symbols` | シンボル一覧取得 |
| `analyze_structure` | コード構造解析 (tree-sitter) |
| `get_function_at_line` | 特定行の関数取得 |
| `repo_pack` | リポジトリパック (Repomix) |
| `query` | 自然言語でのインテリジェントクエリ |

### セッション管理

| ツール | 用途 |
|--------|------|
| `start_session` | セッション開始、extraction_prompt を返す |
| `set_query_frame` | QueryFrame 設定（Quote 検証） |
| `get_session_status` | 現在のフェーズ・状態を確認 |
| `submit_understanding` | EXPLORATION 完了、mapped_symbols 自動追加 |
| `validate_symbol_relevance` | Embedding 提案を取得 |
| `confirm_symbol_relevance` | 検証結果確定、confidence 更新 |
| `submit_semantic` | SEMANTIC 完了 |
| `submit_verification` | VERIFICATION 完了 |
| `check_write_target` | Write 可否確認 |
| `record_outcome` | 結果記録、agreements 自動生成 |

---

## 依存関係

### システムツール

| ツール | 必須 | 用途 |
|--------|------|------|
| ripgrep (rg) | Yes | search_text, search_files, find_references |
| universal-ctags | Yes | find_definitions, find_references, get_symbols |
| Python 3.10+ | Yes | サーバー本体 |
| devrag | Yes | 森/地図の意味検索 |
| ONNX Runtime 1.22.0+ | Yes | devrag の Embedding 処理 |
| repomix | No | repo_pack |

### Python パッケージ

```
mcp>=1.0.0
tree-sitter>=0.21.0
tree-sitter-languages>=1.10.0
sentence-transformers>=2.2.0
scikit-learn>=1.0.0
pytest>=7.0.0
```

---

## セットアップ

### Step 1: MCP サーバーのセットアップ（1回のみ）

```bash
# リポジトリをクローン
git clone https://github.com/tech-spoke/llm-helper.git
cd llm-helper

# サーバーをセットアップ（venv、依存関係、ONNX Runtime）
./setup.sh
```

### Step 2: devrag のインストール（1回のみ）

```bash
# Linux x64
wget https://github.com/tomohiro-owada/devrag/releases/latest/download/devrag-linux-x64.tar.gz
tar xzf devrag-linux-x64.tar.gz
sudo mv devrag /usr/local/bin/
```

### Step 3: プロジェクトの初期化（プロジェクトごと）

```bash
# 対象プロジェクトを初期化
./init-project.sh /path/to/your-project

# オプション: ソースディレクトリを指定
./init-project.sh /path/to/your-project --src-dirs=src,packages,modules
```

これにより以下が作成されます：

```
your-project/
└── .code-intel/
    ├── devrag-forest.json   ← 森（コード検索）設定
    ├── devrag-map.json      ← 地図（合意事項）設定
    └── agreements/          ← 合意事項ディレクトリ
```

### Step 4: .mcp.json の設定

`init-project.sh` が出力する設定を `.mcp.json` に追加：

```json
{
  "mcpServers": {
    "devrag-map": {
      "type": "stdio",
      "command": "/usr/local/bin/devrag",
      "args": ["--config", "/path/to/your-project/.code-intel/devrag-map.json"],
      "env": {"LD_LIBRARY_PATH": "/usr/local/lib"}
    },
    "devrag-forest": {
      "type": "stdio",
      "command": "/usr/local/bin/devrag",
      "args": ["--config", "/path/to/your-project/.code-intel/devrag-forest.json"],
      "env": {"LD_LIBRARY_PATH": "/usr/local/lib"}
    },
    "code-intel": {
      "type": "stdio",
      "command": "/path/to/llm-helper/venv/bin/python",
      "args": ["/path/to/llm-helper/code_intel_server.py"],
      "env": {"PYTHONPATH": "/path/to/llm-helper"}
    }
  }
}
```

### Step 5: devrag データベースの初期化

```bash
cd /path/to/your-project/.code-intel

# 森（コード検索）を初期化
devrag --config devrag-forest.json sync

# 地図（合意事項）を初期化
devrag --config devrag-map.json sync
```

### Step 6: スキルの設定（任意）

```bash
mkdir -p /path/to/your-project/.claude/commands
cp /path/to/llm-helper/.claude/commands/*.md /path/to/your-project/.claude/commands/
```

### Step 7: Claude Code を再起動

MCP サーバーを読み込むために再起動。

---

## 利用方法

### /code スキルを使う（推奨）

```
/code AuthServiceのlogin関数でパスワードが空のときエラーが出ないバグを直して
```

スキルが自動的に：
1. Intent 判定（MODIFY）
2. セッション開始
3. QueryFrame 抽出・検証
4. 地図を検索（過去の成功パターン）
5. EXPLORATION（find_definitions, find_references 等）
6. シンボル検証（Embedding）
7. 必要に応じて SEMANTIC（devrag-forest）
8. VERIFICATION（仮説検証）
9. READY（実装）
10. 成功時に地図を更新

### 直接ツールを呼び出す

```
# テキスト検索
mcp__code-intel__search_text でパターン "Router" を検索して

# 定義検索
mcp__code-intel__find_definitions で "SessionState" の定義を探して

# 構造解析
mcp__code-intel__analyze_structure で tools/router.py を解析して
```

---

## スキル

### /code

コード実装を支援するエージェント。フェーズゲートに従って探索→実装を行う。

```
/code ログイン機能でパスワードが空のときエラーが出ないバグを直して
```

### /outcome

実装結果を記録するエージェント。失敗パターンの分析に使用。

```
/outcome この実装は失敗だった
```

---

## 合意事項（Agreements）

成功した NL→Symbol ペアは `.code-intel/agreements/` に Markdown として保存され、devrag-map でベクトル検索可能になる：

```markdown
---
doc_type: agreement
nl_term: ログイン機能
symbol: AuthService
similarity: 0.85
session_id: session_20250112_143000
---

# ログイン機能 → AuthService

## 根拠 (Code Evidence)

AuthService.login() がユーザー認証を処理

## 関連ファイル

- `src/auth/service.py`
```

次回以降の探索で優先的に参照される。

---

## プロジェクト構造

### MCP サーバー（llm-helper/）

```
llm-helper/
├── code_intel_server.py    ← MCP サーバー本体
├── tools/                  ← ツール実装
│   ├── session.py
│   ├── query_frame.py
│   ├── agreements.py
│   └── learned_pairs.py
├── setup.sh                ← サーバーセットアップ
├── init-project.sh         ← プロジェクト初期化
└── .claude/commands/       ← スキル定義
    ├── code.md
    └── outcome.md
```

### 対象プロジェクト

```
your-project/
├── .mcp.json               ← MCP 設定（手動設定）
├── .code-intel/            ← Code Intel データ（自動生成）
│   ├── devrag-forest.json
│   ├── devrag-map.json
│   ├── vectors-forest.db   ← devrag sync で生成
│   ├── vectors-map.db      ← devrag sync で生成
│   ├── agreements/         ← 成功ペア（自動生成）
│   └── learned_pairs.json  ← キャッシュ（自動生成）
├── .claude/commands/       ← スキル（任意コピー）
└── src/                    ← あなたのソースコード
```

---

## ドキュメント

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) - システム設計
- [ROUTER.md](docs/ROUTER.md) - Router 詳細
- [DESIGN_v3.8.md](docs/DESIGN_v3.8.md) - v3.8 設計詳細

---

## ライセンス

MIT
