#!/usr/bin/env python3
"""
セッションサマリー生成スクリプト

Stopフックから呼び出され、セッション全体のサブエージェント呼び出しを
まとめたサマリーMarkdownを生成する。

ログ出力先: .claude/logs/sessions/
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 共通設定をインポート
from config import (
    INDEX_FILE,
    INDEX_LOCK_SUFFIX,
    LOG_BASE_DIR,
    SESSION_SUMMARY_DIR,
    USER_PROMPTS_FILE,
    FileLock,
    is_safe_path,
    redact_sensitive_data,
    sanitize_branch_name,
)


# =============================================================================
# データ収集
# =============================================================================
def load_session_entries(
    project_root: str,
    session_id: str,
    max_retries: int = 3,
    retry_delay: float = 0.5
) -> list[dict[str, Any]]:
    """
    index.jsonlから指定セッションのエントリを読み込み（再試行・ロック付き）

    SubagentStopの非同期書き込みが完了するのを待つため、再試行ロジックを含む。
    書き込みと同じFileLockを使用して部分読み取りを防止。

    Args:
        project_root: プロジェクトルート
        session_id: セッションID
        max_retries: 最大再試行回数
        retry_delay: 再試行間隔（秒）

    Returns:
        セッションに属するエントリのリスト
    """
    import time

    index_path = os.path.join(project_root, INDEX_FILE)
    lock_path = index_path + INDEX_LOCK_SUFFIX
    entries: list[dict[str, Any]] = []
    last_count = -1

    for attempt in range(max_retries + 1):
        entries = []

        if not os.path.exists(index_path):
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            return entries

        try:
            # 書き込みと同じFileLockを使用（部分読み取り防止）
            with FileLock(lock_path, timeout=5.0):
                with open(index_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("session") == session_id:
                                entries.append(entry)
                        except json.JSONDecodeError:
                            continue
        except TimeoutError:
            print(f"[session-summary] Warning: Lock timeout (attempt {attempt + 1})", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
        except OSError as e:
            print(f"[session-summary] Warning: Failed to read index (attempt {attempt + 1}): {e}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue

        # エントリ数が増えなくなったら安定したとみなす
        if len(entries) == last_count and len(entries) > 0:
            break
        last_count = len(entries)

        # まだ再試行できる場合は待機
        if attempt < max_retries:
            time.sleep(retry_delay)

    return entries


def load_user_prompts(project_root: str, session_id: str) -> list[dict[str, Any]]:
    """
    user_prompts.jsonlから指定セッションのプロンプトを読み込み

    Args:
        project_root: プロジェクトルート
        session_id: セッションID

    Returns:
        セッションに属するプロンプトのリスト
    """
    prompts_path = os.path.join(project_root, USER_PROMPTS_FILE)
    prompts: list[dict[str, Any]] = []

    if not os.path.exists(prompts_path):
        return prompts

    try:
        with open(prompts_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("session_id") == session_id:
                        prompts.append(entry)
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        print(f"[session-summary] Warning: Failed to read prompts: {e}", file=sys.stderr)

    return prompts


def read_subagent_log(project_root: str, log_file: str) -> str | None:
    """
    サブエージェントログファイルの最終結果を抽出

    Args:
        project_root: プロジェクトルート
        log_file: ログファイルの相対パス

    Returns:
        最終結果のテキスト（見つからない場合はNone）
    """
    import re

    log_path = os.path.join(project_root, LOG_BASE_DIR, log_file)

    # パス検証
    if not is_safe_path(log_path, [project_root]):
        return None

    if not os.path.exists(log_path):
        return None

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()

        # "## 最終結果" セクションを抽出
        if "## 最終結果" in content:
            parts = content.split("## 最終結果", 1)
            if len(parts) > 1:
                result_section = parts[1]
                # 次のセクションヘッダー（## または行頭の---）までを取得
                # 行頭の---のみをセクション区切りとして扱う
                match = re.search(r'\n(## |---\n)', result_section)
                if match:
                    result_section = result_section[:match.start()]
                result = result_section.strip()
                # 機密情報をマスキング
                result = redact_sensitive_data(result)
                # 最大500文字に制限
                if len(result) > 500:
                    result = result[:497] + "..."
                return result
    except OSError:
        pass

    return None


# =============================================================================
# Markdown生成
# =============================================================================
def generate_session_summary(
    session_id: str,
    entries: list[dict[str, Any]],
    prompts: list[dict[str, Any]],
    project_root: str,
    start_ts: str,
    end_ts: str
) -> str:
    """
    セッションサマリーMarkdownを生成

    Args:
        session_id: セッションID
        entries: サブエージェントエントリのリスト
        prompts: ユーザープロンプトのリスト
        project_root: プロジェクトルート
        start_ts: セッション開始時刻
        end_ts: セッション終了時刻

    Returns:
        Markdown形式のサマリー文字列
    """
    lines: list[str] = []

    # ヘッダー
    date_str = datetime.now().strftime("%Y-%m-%d")
    lines.append(f"# Session Summary: {date_str}")
    lines.append("")

    # 概要テーブル
    lines.append("## 概要")
    lines.append("")
    lines.append("| 項目 | 値 |")
    lines.append("|------|-----|")
    lines.append(f"| セッションID | `{session_id[:16]}...` |")
    lines.append(f"| 開始時刻 | {start_ts} |")
    lines.append(f"| 終了時刻 | {end_ts} |")
    lines.append(f"| サブエージェント呼び出し回数 | {len(entries)} |")
    lines.append(f"| ユーザープロンプト数 | {len(prompts)} |")

    # 合計実行時間
    total_duration_ms = sum(e.get("duration_ms", 0) or 0 for e in entries)
    if total_duration_ms > 0:
        total_duration_sec = total_duration_ms / 1000
        lines.append(f"| サブエージェント合計実行時間 | {total_duration_sec:.1f}秒 |")

    # ブランチ情報
    branches = set(e.get("branch", "unknown") for e in entries)
    if branches:
        lines.append(f"| ブランチ | {', '.join(branches)} |")

    lines.append("")
    lines.append("---")
    lines.append("")

    # ユーザープロンプト履歴
    if prompts:
        lines.append("## ユーザープロンプト履歴")
        lines.append("")
        for i, prompt in enumerate(prompts, 1):
            timestamp = prompt.get("timestamp", "")
            time_str = timestamp.split("T")[1][:8] if "T" in timestamp else timestamp
            prompt_text = prompt.get("prompt", "")[:200]
            lines.append(f"### {i}. [{time_str}]")
            lines.append("")
            lines.append(f"> {prompt_text}")
            if len(prompt.get("prompt", "")) > 200:
                lines.append("> ...")
            lines.append("")
        lines.append("---")
        lines.append("")

    # サブエージェント呼び出し履歴
    if entries:
        lines.append("## サブエージェント呼び出し履歴")
        lines.append("")

        # 時刻順にソート
        sorted_entries = sorted(entries, key=lambda x: x.get("start", ""))

        for i, entry in enumerate(sorted_entries, 1):
            subagent = entry.get("subagent", "unknown")
            start = entry.get("start", "")
            time_str = start.split("T")[1][:8] if "T" in start else start
            duration_ms = entry.get("duration_ms")
            duration_str = f" ({duration_ms / 1000:.1f}秒)" if duration_ms else ""
            log_file = entry.get("log_file", "")

            lines.append(f"### {i}. {subagent} [{time_str}]{duration_str}")
            lines.append("")

            # ログファイルへのリンク
            if log_file:
                lines.append(f"**ログ**: `{LOG_BASE_DIR}/{log_file}`")
                lines.append("")

                # 最終結果を抽出
                result = read_subagent_log(project_root, log_file)
                if result:
                    # 結果を引用形式で表示
                    result_lines = result.split("\n")
                    for line in result_lines[:5]:  # 最大5行
                        lines.append(f"> {line}")
                    if len(result_lines) > 5:
                        lines.append("> ...")
                    lines.append("")
    else:
        lines.append("## サブエージェント呼び出し履歴")
        lines.append("")
        lines.append("(サブエージェント呼び出しなし)")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*Generated at {datetime.now().isoformat()}*")
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# ファイル出力
# =============================================================================
def write_session_summary(
    project_root: str,
    session_id: str,
    content: str,
    branch: str = ""
) -> str:
    """
    セッションサマリーをファイルに書き込み

    Args:
        project_root: プロジェクトルート
        session_id: セッションID
        content: Markdown内容
        branch: Gitブランチ名（オプション）

    Returns:
        書き込んだファイルパス（失敗時は空文字列）
    """
    project_root = os.path.normpath(project_root)
    date_str = datetime.now().strftime("%Y-%m-%d")

    # ブランチ別ディレクトリ
    if branch:
        safe_branch = sanitize_branch_name(branch)
        summary_dir = os.path.join(project_root, SESSION_SUMMARY_DIR, date_str, safe_branch)
    else:
        summary_dir = os.path.join(project_root, SESSION_SUMMARY_DIR, date_str)

    os.makedirs(summary_dir, exist_ok=True)

    # ファイル名: セッションIDの先頭16文字 + タイムスタンプ
    timestamp = datetime.now().strftime("%H%M%S")
    short_session_id = session_id[:16] if len(session_id) > 16 else session_id
    summary_file = os.path.join(summary_dir, f"{timestamp}_{short_session_id}.md")

    try:
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(content)
        return summary_file
    except OSError as e:
        print(f"[session-summary] Error writing summary: {e}", file=sys.stderr)

    return ""


# =============================================================================
# メイン処理
# =============================================================================
def main() -> int:
    """
    メイン処理

    --input-file オプションまたは stdin から JSON を受け取り、
    セッションサマリーを生成

    Returns:
        終了コード
    """
    parser = argparse.ArgumentParser(description="Session summary generator")
    parser.add_argument(
        "--input-file",
        type=str,
        help="Path to input JSON file (if not specified, reads from stdin)"
    )
    args = parser.parse_args()

    # 入力を読み取り
    input_file_path = None
    try:
        if args.input_file:
            input_file_path = args.input_file
            with open(input_file_path, "r", encoding="utf-8") as f:
                input_data = json.load(f)
        else:
            # Windows環境でのエンコーディング問題対策
            if sys.platform == "win32":
                import io
                sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
            input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"[session-summary] Error: Invalid JSON input: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[session-summary] Error reading input file: {e}", file=sys.stderr)
        return 1
    finally:
        # 一時ファイルを削除
        if input_file_path and os.path.exists(input_file_path):
            try:
                os.remove(input_file_path)
            except OSError:
                pass

    session_id = input_data.get("session_id", "unknown")
    project_root = input_data.get("project_root", ".")
    start_ts = input_data.get("start_ts", "")
    end_ts = input_data.get("end_ts", datetime.now().isoformat())
    branch = input_data.get("branch", "")

    # project_root の検証
    project_root = os.path.realpath(os.path.normpath(project_root))
    allowed_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if allowed_root:
        allowed_root = os.path.realpath(os.path.normpath(allowed_root))
        if project_root != allowed_root:
            print("[session-summary] Error: Invalid project_root", file=sys.stderr)
            return 1

    # セッションデータを収集
    entries = load_session_entries(project_root, session_id)
    prompts = load_user_prompts(project_root, session_id)

    # サブエージェント呼び出しがない場合はスキップ（プロンプトのみでは無意味なため）
    if not entries:
        print("[session-summary] No subagent calls in session, skipping summary generation", file=sys.stderr)
        return 0

    # サマリー生成
    summary_content = generate_session_summary(
        session_id=session_id,
        entries=entries,
        prompts=prompts,
        project_root=project_root,
        start_ts=start_ts,
        end_ts=end_ts
    )

    # ファイル書き込み
    summary_file = write_session_summary(
        project_root=project_root,
        session_id=session_id,
        content=summary_content,
        branch=branch
    )

    if summary_file:
        print(f"[session-summary] Summary written to: {summary_file}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
