# /sync - Private コミット & Public 配布

**Purpose**: private repo へのコミット・プッシュ → public repo への配布を一括実行

## フロー

### Step 1: Private repo へコミット & プッシュ

1. `git status` で変更を確認
2. 変更がなければ「コミットする変更はありません」と報告して Step 2 へ
3. 変更がある場合:
   - `git diff` と `git diff --cached` で差分を確認
   - `git log --oneline -5` でコミットメッセージのスタイルを確認
   - 変更内容を分析し、適切なコミットメッセージを作成
   - ユーザーに変更内容とコミットメッセージを提示して確認を求める
   - 承認後、`git add` → `git commit` → `git push origin` を実行

### Step 2: Public repo へ配布

1. `bash sync-to-public.sh --dry-run` を実行
2. dry-run の結果（同期ファイル一覧と差分）をユーザーに提示
3. 「変更なし」なら完了を報告して終了
4. 変更がある場合、ユーザーに確認を求める
5. 承認後、`bash sync-to-public.sh` を実行

## 重要ルール

- **各ステップでユーザー確認を必ず取る** — 自動で push しない
- コミットメッセージは `Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>` を末尾に付与
- `.env`, `.envrc`, `credentials`, `auth.json` 等の機密ファイルが含まれていないか確認する
- 機密ファイルが staging に含まれている場合は **警告して除外する**

## 使い方

```
/sync              # 通常実行（コミット → 配布）
/sync 配布のみ      # Step 2 のみ実行
```
