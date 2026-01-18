# Task Logging System

**日本語** | [English](README.en.md)

サブエージェント（Task tool）の実行を自動でログ記録するフックシステム。

## 概要

Claude Code の Hooks 機能を使用して、サブエージェントの実行内容を自動的に Markdown 形式で記録します。

### 特徴

- **自動ログ記録**: サブエージェント（Task tool）の実行を自動でキャプチャ
- **Markdown形式**: 人間が読みやすい形式でログを保存
- **セッションサマリー**: セッション終了時に全体サマリーを自動生成
- **機密情報マスキング**: APIキー、トークン、パスワード等を自動的にマスク
- **ブランチ別整理**: Gitブランチごとにログを自動分類
- **クロスプラットフォーム**: Windows / macOS / Linux 対応

### 要件

- **Python**: 3.10以上
- **依存関係**: なし（標準ライブラリのみ使用）
- **Claude Code**: Hooks機能が利用可能なバージョン

### ファイル構成

| ファイル                 | 役割                                                 |
| ------------------------ | ---------------------------------------------------- |
| `config.py`              | 共通定数・ユーティリティ（FileLock、sanitize等）     |
| `task-logger.py`         | フック処理（UserPromptSubmit / SubagentStop / Stop） |
| `transcript-analyzer.py` | transcript 解析・Markdown 生成                       |
| `session-summary.py`     | セッションサマリー生成（Stopフック用）               |

### 動作フロー

```
1. UserPromptSubmit
   → ユーザープロンプトを user_prompts.jsonl に記録
   → サブエージェント呼び出しの文脈を保存

2. PreToolUse (matcher: Task)
   → セッション開始情報をキャッシュに保存

3. SubagentStop
   → バックグラウンドで transcript-analyzer.py を起動
   → transcript を解析して Markdown ログを生成

4. Stop
   → バックグラウンドで session-summary.py を起動
   → セッション全体のサマリーを生成
```

## 有効化 / 無効化

### 有効化する

`.claude/settings.json` に以下のフック設定を追加:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python .claude/hooks/task-logging/task-logger.py",
            "timeout": 5
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python .claude/hooks/task-logging/task-logger.py",
            "timeout": 5
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python .claude/hooks/task-logging/task-logger.py",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python .claude/hooks/task-logging/task-logger.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

**AI への依頼例:**

- 「サブエージェントのログ機能をオンにして」
- 「Task tool のログ記録を有効化して」
- 「settings.json に task-logger フックを追加して」

### 無効化する

`.claude/settings.json` から以下を削除:

- `PreToolUse` セクション
- `UserPromptSubmit` セクション
- `SubagentStop` セクション
- `Stop` セクション

**AI への依頼例:**

- 「サブエージェントのログ機能をオフにして」
- 「Task tool のログ記録を無効化して」
- 「settings.json から task-logger フックを削除して」

## ログの確認

### 保存場所

```
.claude/logs/
├── agents/
│   ├── index.jsonl                              # 全ログのサマリーインデックス
│   ├── user_prompts.jsonl                       # ユーザープロンプト履歴
│   └── {YYYY-MM-DD}/
│       └── {branch}/                            # ブランチ別ディレクトリ
│           └── {HHMMSS}_{subagent}_{uuid}.md    # サブエージェント詳細ログ
└── sessions/
    └── {YYYY-MM-DD}/
        └── {branch}/                            # ブランチ別ディレクトリ
            └── {HHMMSS}_{session_id}.md         # セッションサマリー
```

### サブエージェントログ内容

各 Markdown ログには以下が含まれます:

- **メタ情報**: 実行日時、サブエージェント名、モデル、実行時間
- **タスク内容**: 説明とプロンプト
- **実行過程**: ツール使用履歴（入力・結果）
- **最終結果**: サブエージェントの応答

### セッションサマリー内容

セッション終了時に生成されるサマリーには以下が含まれます:

- **概要**: セッションID、開始/終了時刻、呼び出し回数
- **ユーザープロンプト履歴**: セッション中のユーザー入力
- **サブエージェント呼び出し履歴**: 各サブエージェントの実行結果

**AI への依頼例:**

- 「今日のサブエージェント実行ログを見せて」
- 「.claude/logs/agents/ の最新ログを読んで」
- 「今日のセッションサマリーを見せて」

## ログのクリーンアップ

**AI への依頼例:**

- 「7日以上前のサブエージェントログを削除して」
- 「.claude/logs/agents/ を空にして」
- 「古いセッションサマリーを削除して」

## 設定値

`config.py` で以下の値を調整可能:

| 定数                           | デフォルト | 説明                           |
| ------------------------------ | ---------- | ------------------------------ |
| `MAX_CONTENT_LENGTH`           | 1000       | ツール結果の最大表示長         |
| `MAX_TOOL_RESULT_LENGTH`       | 500        | Markdown内のツール結果の最大長 |
| `MAX_EVENTS`                   | 1000       | 解析する最大イベント数         |
| `MAX_FILE_SIZE_MB`             | 10         | 解析する最大ファイルサイズ     |
| `MAX_PROMPT_LENGTH`            | 500        | プロンプトの最大保存長         |
| `CACHE_TTL_HOURS`              | 24         | キャッシュエントリの保持期間   |
| `MAX_PARENT_TRANSCRIPT_MB`     | 5          | 親transcript最大サイズ         |
| `MAX_PARENT_TRANSCRIPT_EVENTS` | 500        | 親transcript最大イベント数     |
| `MAX_TOOL_INPUT_LENGTH`        | 1000       | ツール入力の最大保存長         |

## フック一覧

| フック           | 処理内容                       | 処理方式         |
| ---------------- | ------------------------------ | ---------------- |
| PreToolUse       | Task開始情報をキャッシュ       | 同期（軽量）     |
| UserPromptSubmit | ユーザープロンプトを記録       | 同期（軽量）     |
| SubagentStop     | サブエージェント個別ログを生成 | バックグラウンド |
| Stop             | セッション全体のサマリーを生成 | バックグラウンド |

## セキュリティ

### 機密情報マスキング

ログ記録時に以下のパターンを自動的にマスキングします:

- **APIキー**: OpenAI (`sk-*`)、AWS (`AKIA*`)、Google (`AIza*`)
- **GitHubトークン**: Personal Access Token (`ghp_*`)、OAuth (`gho_*`)
- **パスワード**: `password=`, `secret=` などのパターン
- **Bearerトークン**: `Authorization: Bearer *`
- **Webhook URL**: Slack (`hooks.slack.com`)、Discord
- **JWT**: `eyJ`で始まるトークン
- **Supabase**: `sbp_*`、`service_role_key`
- **Stripe**: `sk_live_*`、`pk_live_*`

### キャッシュファイル

セッションキャッシュはユーザー固有のセキュアなディレクトリに保存されます:

- **Windows**: `%LOCALAPPDATA%\claude-task-logger\`
- **macOS/Linux**: `~/.cache/claude-task-logger/` (権限: 0700)

### パス検証

ディレクトリトラバーサル攻撃を防ぐため、すべてのファイルパスは許可されたディレクトリ内にあることを検証します。

## インストール

1. このディレクトリをプロジェクトの `.claude/hooks/task-logging/` にコピー:

```bash
# 例: GitHubからクローンした場合
cp -r task-logging /your-project/.claude/hooks/
```

2. `.claude/settings.json` にフック設定を追加（「有効化する」セクション参照）

3. Claude Code を再起動

## ライセンス

MIT License
