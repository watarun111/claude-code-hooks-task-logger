#!/usr/bin/env python3
"""
サブエージェント transcript 解析スクリプト

task-logger.py からバックグラウンドで起動され、
transcript ファイルを解析して Markdown 形式のログを生成する。
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# 共通設定をインポート
from config import (
    INDEX_FILE,
    INDEX_LOCK_SUFFIX,
    LOG_BASE_DIR,
    MAX_CONTENT_LENGTH,
    MAX_EVENTS,
    MAX_FILE_SIZE_MB,
    MAX_TOOL_INPUT_LENGTH,
    MAX_TOOL_RESULT_LENGTH,
    FileLock,
    is_safe_path,
    redact_sensitive_data,
    sanitize_branch_name,
    sanitize_filename,
)


# =============================================================================
# transcript 解析
# =============================================================================
def parse_transcript(transcript_path: str, project_root: str = "") -> list[dict[str, Any]]:
    """
    transcript ファイルを解析して実行イベントのリストを返す

    Args:
        transcript_path: transcript ファイルのパス
        project_root: プロジェクトルート（パス検証用）

    Returns:
        実行イベントのリスト
    """
    events: list[dict[str, Any]] = []

    # ~ をホームディレクトリに展開
    expanded_path = os.path.expanduser(transcript_path)

    # パス検証: ホームディレクトリまたはプロジェクトルート内のみ許可
    allowed_prefixes = [os.path.expanduser("~")]
    if project_root:
        allowed_prefixes.append(project_root)

    if not is_safe_path(expanded_path, allowed_prefixes):
        print(f"[transcript-analyzer] Error: Path outside allowed directories: {expanded_path}", file=sys.stderr)
        return events

    if not os.path.exists(expanded_path):
        return events

    # ファイルサイズチェック（巨大ファイルはエラーとして処理中断）
    try:
        file_size_mb = os.path.getsize(expanded_path) / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            print(f"[transcript-analyzer] Error: File too large ({file_size_mb:.1f}MB > {MAX_FILE_SIZE_MB}MB), aborting", file=sys.stderr)
            return events
    except OSError:
        pass

    try:
        with open(expanded_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                # 最大イベント数チェック
                if i >= MAX_EVENTS:
                    print(f"[transcript-analyzer] Warning: Truncated at {MAX_EVENTS} events", file=sys.stderr)
                    break

                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    events.append(event)
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        print(f"[transcript-analyzer] Error reading transcript: {e}", file=sys.stderr)

    return events


def extract_git_branch(events: list[dict[str, Any]]) -> str:
    """
    transcriptイベントからgitBranchを取得

    Args:
        events: transcript のイベントリスト

    Returns:
        ブランチ名（見つからない場合は空文字列）
    """
    # 最初のイベントにgitBranchが含まれている
    if events and isinstance(events[0], dict):
        return events[0].get("gitBranch", "")
    return ""


def extract_execution_steps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    transcript イベントから実行ステップを抽出

    Claude Code サブエージェントのtranscriptフォーマットに対応:
    - type="assistant" の message.content 配列内に tool_use や text がある
    - type="tool_result" に toolUseId と content がある

    Args:
        events: transcript のイベントリスト

    Returns:
        実行ステップのリスト
    """
    steps: list[dict[str, Any]] = []
    tool_uses: dict[str, dict[str, Any]] = {}  # tool_use_id -> tool_use イベント

    for event in events:
        event_type = event.get("type")

        # Claude Code サブエージェント形式: assistantメッセージ内のcontent配列を解析
        if event_type == "assistant":
            message = event.get("message", {})
            content_list = message.get("content", [])

            for content in content_list:
                if not isinstance(content, dict):
                    continue

                content_type = content.get("type")

                # テキスト応答
                if content_type == "text":
                    text = content.get("text", "")
                    if text:
                        steps.append({
                            "type": "response",
                            "content": text
                        })

                # ツール使用
                elif content_type == "tool_use":
                    tool_id = content.get("id", "")
                    tool_name = content.get("name", "Unknown")
                    tool_input = content.get("input", {})

                    if tool_id:
                        tool_uses[tool_id] = {
                            "tool": tool_name,
                            "input": tool_input
                        }

        # ツール結果（Claude Code形式: userメッセージ内にtool_resultがネスト）
        elif event_type == "user":
            message = event.get("message", {})
            content_list = message.get("content", [])

            for content in content_list:
                if not isinstance(content, dict):
                    continue

                if content.get("type") == "tool_result":
                    tool_id = content.get("tool_use_id", "")
                    result = content.get("content", "")

                    # contentがリストの場合（画像等を含む場合）
                    if isinstance(result, list):
                        text_parts = []
                        for item in result:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text_parts.append(item.get("text", ""))
                            elif isinstance(item, str):
                                text_parts.append(item)
                        result = "\n".join(text_parts)

                    tool_info = tool_uses.get(tool_id, {})
                    tool_name = tool_info.get("tool", "Unknown")
                    tool_input = tool_info.get("input", {})

                    # 結果を truncate
                    if isinstance(result, str) and len(result) > MAX_CONTENT_LENGTH:
                        result = result[:MAX_CONTENT_LENGTH] + "..."

                    steps.append({
                        "type": "tool",
                        "tool": tool_name,
                        "input": tool_input,
                        "result": result
                    })

        # 旧形式: 直接 tool_result イベント
        elif event_type == "tool_result":
            tool_id = event.get("toolUseId") or event.get("tool_id") or event.get("tool_use_id")
            result = event.get("content") or event.get("result", "")

            tool_info = tool_uses.get(tool_id, {})
            tool_name = tool_info.get("tool", "Unknown")
            tool_input = tool_info.get("input", {})

            # 結果を truncate
            if isinstance(result, str) and len(result) > MAX_CONTENT_LENGTH:
                result = result[:MAX_CONTENT_LENGTH] + "..."

            steps.append({
                "type": "tool",
                "tool": tool_name,
                "input": tool_input,
                "result": result
            })

        # 旧形式との互換性: 直接 tool_use イベント
        elif event_type == "tool_use":
            tool_id = event.get("id") or event.get("tool_use_id")
            tool_name = event.get("tool") or event.get("name")
            tool_input = event.get("input") or event.get("tool_input", {})

            if tool_id:
                tool_uses[tool_id] = {
                    "tool": tool_name,
                    "input": tool_input
                }

    return steps


def get_final_response(steps: list[dict[str, Any]]) -> str:
    """
    実行ステップから最終応答を取得

    Args:
        steps: 実行ステップのリスト

    Returns:
        最終応答のテキスト
    """
    # 後ろから探して最初の response を返す
    for step in reversed(steps):
        if step.get("type") == "response":
            return step.get("content", "")
    return "(応答なし)"


# =============================================================================
# Markdown 生成
# =============================================================================
def escape_code_block(text: str) -> str:
    """
    Markdown コードブロックのインジェクション対策

    Args:
        text: エスケープ対象のテキスト

    Returns:
        エスケープされたテキスト
    """
    return text.replace("```", "` ` `")


def generate_markdown_log(
    session_info: dict[str, Any],
    steps: list[dict[str, Any]],
    final_response: str,
    start_ts: str,
    end_ts: str,
    transcript_path: str
) -> str:
    """
    Markdown 形式のログを生成

    Args:
        session_info: セッション情報（開始時の情報）
            - subagent: サブエージェント名
            - description: タスクの説明
            - prompt: 実行プロンプト
            - model: 使用モデル名（オプション）
        steps: 実行ステップのリスト（extract_execution_steps の出力）
        final_response: 最終応答テキスト
        start_ts: 開始時刻（ISO 8601形式）
        end_ts: 終了時刻（ISO 8601形式）
        transcript_path: 元の transcript ファイルパス

    Returns:
        Markdown 形式のログ文字列
    """
    subagent = session_info.get("subagent", "Unknown")
    description = session_info.get("description", "")
    prompt = session_info.get("prompt", "")
    model = session_info.get("model") or "default"

    # 実行時間計算
    duration_str = ""
    try:
        start_dt = datetime.fromisoformat(start_ts)
        end_dt = datetime.fromisoformat(end_ts)
        duration_sec = (end_dt - start_dt).total_seconds()
        duration_str = f"{duration_sec:.1f}秒"
    except Exception:
        duration_str = "不明"

    # Markdown 生成
    lines: list[str] = []
    lines.append(f"# Agent Log: {subagent}")
    lines.append("")
    lines.append("## メタ情報")
    lines.append("")
    lines.append("| 項目 | 値 |")
    lines.append("|------|-----|")
    lines.append(f"| 実行日時 | {start_ts} |")
    lines.append(f"| サブエージェント | {subagent} |")
    lines.append(f"| モデル | {model} |")
    lines.append(f"| 実行時間 | {duration_str} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## タスク内容")
    lines.append("")
    if description:
        lines.append(f"**説明**: {description}")
        lines.append("")
    lines.append("```")
    lines.append(escape_code_block(prompt))
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 実行過程")
    lines.append("")

    # ツール使用ステップを出力
    tool_steps = [s for s in steps if s.get("type") == "tool"]
    if tool_steps:
        for i, step in enumerate(tool_steps, 1):
            tool_name = step.get("tool", "Unknown")
            tool_input = step.get("input", {})
            tool_result = step.get("result", "")

            lines.append(f"### {i}. [{tool_name}]")
            lines.append("")

            # 入力パラメータ（サイズ制限・機密情報マスキング付き）
            if tool_input:
                lines.append("**入力:**")
                lines.append("```json")
                try:
                    input_json = json.dumps(tool_input, ensure_ascii=False, indent=2)
                    # 機密情報をマスキング
                    input_json = redact_sensitive_data(input_json)
                    if len(input_json) > MAX_TOOL_INPUT_LENGTH:
                        input_json = input_json[:MAX_TOOL_INPUT_LENGTH] + "\n... (truncated)"
                    lines.append(input_json)
                except Exception:
                    input_str = str(tool_input)
                    input_str = redact_sensitive_data(input_str)
                    if len(input_str) > MAX_TOOL_INPUT_LENGTH:
                        input_str = input_str[:MAX_TOOL_INPUT_LENGTH] + "... (truncated)"
                    lines.append(input_str)
                lines.append("```")
                lines.append("")

            # 結果（エスケープ処理・機密情報マスキング適用）
            if tool_result:
                lines.append("**結果:**")
                lines.append("```")
                result_str = redact_sensitive_data(str(tool_result))
                result_str = escape_code_block(result_str)
                lines.append(result_str[:MAX_TOOL_RESULT_LENGTH])
                if len(result_str) > MAX_TOOL_RESULT_LENGTH:
                    lines.append("... (truncated)")
                lines.append("```")
                lines.append("")
    else:
        lines.append("(ツール使用なし)")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 最終結果")
    lines.append("")
    # final_responseのMarkdown特殊文字をエスケープ・機密情報マスキング
    # ---が行頭にある場合、Markdownの水平線と誤認されるのを防ぐ
    escaped_response = final_response
    if escaped_response:
        # 機密情報をマスキング
        escaped_response = redact_sensitive_data(escaped_response)
        # 行頭の---を\---に置換（水平線防止）
        escaped_response = escaped_response.replace("\n---", "\n\\---")
        if escaped_response.startswith("---"):
            escaped_response = "\\---" + escaped_response[3:]
        # コードブロックのエスケープ
        escaped_response = escape_code_block(escaped_response)
    lines.append(escaped_response)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 参照")
    lines.append("")
    lines.append(f"- Transcript: `{transcript_path}`")
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# ファイル出力
# =============================================================================
def write_markdown_log(
    project_root: str,
    date_str: str,
    session_id: str,
    subagent: str,
    content: str,
    branch: str = ""
) -> str:
    """
    Markdown ログをファイルに書き込み

    Args:
        project_root: プロジェクトルート
        date_str: 日付文字列
        session_id: セッションID
        subagent: サブエージェント名
        content: Markdown 内容
        branch: Gitブランチ名（オプション）

    Returns:
        書き込んだファイルパス（失敗時は空文字列）
    """
    # パスを正規化（Windows対応）
    project_root = os.path.normpath(project_root)

    # ブランチ別ディレクトリ構造: 日付/ブランチ/
    if branch:
        safe_branch = sanitize_branch_name(branch)
        log_dir = os.path.join(project_root, LOG_BASE_DIR, date_str, safe_branch)
    else:
        log_dir = os.path.join(project_root, LOG_BASE_DIR, date_str)
    os.makedirs(log_dir, exist_ok=True)

    # サブエージェント名をサニタイズ（パストラバーサル防止）
    safe_subagent = sanitize_filename(subagent)

    # タイムスタンプ + UUID でユニークにする（同一秒の衝突を回避）
    timestamp = datetime.now().strftime("%H%M%S")
    unique_id = uuid.uuid4().hex[:8]
    log_file = os.path.join(log_dir, f"{timestamp}_{safe_subagent}_{unique_id}.md")

    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(content)
        return log_file
    except OSError as e:
        print(f"[transcript-analyzer] Error writing log: {log_file}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[transcript-analyzer] Unexpected error writing log: {e}", file=sys.stderr)
    return ""


def write_index_entry(
    project_root: str,
    date_str: str,
    session_id: str,
    subagent: str,
    start_ts: str,
    end_ts: str,
    log_file: str,
    branch: str = ""
) -> None:
    """
    インデックスファイルにエントリを追加（ファイルロック付き）

    Args:
        project_root: プロジェクトルート
        date_str: 日付文字列
        session_id: セッションID
        subagent: サブエージェント名
        start_ts: 開始時刻
        end_ts: 終了時刻
        log_file: ログファイルパス
        branch: Gitブランチ名（オプション）
    """
    index_file = os.path.join(os.path.normpath(project_root), INDEX_FILE)
    lock_file = index_file + INDEX_LOCK_SUFFIX
    os.makedirs(os.path.dirname(index_file), exist_ok=True)

    # 実行時間計算
    duration_ms = None
    try:
        start_dt = datetime.fromisoformat(start_ts)
        end_dt = datetime.fromisoformat(end_ts)
        duration_ms = int((end_dt - start_dt).total_seconds() * 1000)
    except Exception:
        pass

    entry = {
        "date": date_str,
        "session": session_id,
        "subagent": subagent,
        "branch": branch or "unknown",
        "start": start_ts,
        "end": end_ts,
        "duration_ms": duration_ms,
        "status": "success",
        "log_file": log_file
    }
    entry_line = json.dumps(entry, ensure_ascii=False) + "\n"

    try:
        with FileLock(lock_file, timeout=10.0):
            with open(index_file, "a", encoding="utf-8") as f:
                f.write(entry_line)
    except TimeoutError:
        print(f"[transcript-analyzer] Warning: Failed to acquire index lock (timeout)", file=sys.stderr)
    except OSError as e:
        print(f"[transcript-analyzer] Error writing index: {e}", file=sys.stderr)


# =============================================================================
# メイン処理
# =============================================================================
def main() -> int:
    """
    メイン処理

    --input-file オプションまたは stdin から JSON を受け取り、
    transcript を解析して Markdown ログを生成

    Returns:
        終了コード
    """
    # 引数解析
    parser = argparse.ArgumentParser(description="Transcript analyzer for subagent logs")
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
            input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"[transcript-analyzer] Error: Invalid JSON input: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[transcript-analyzer] Error reading input file: {e}", file=sys.stderr)
        return 1
    finally:
        # 一時ファイルを削除
        if input_file_path and os.path.exists(input_file_path):
            try:
                os.remove(input_file_path)
            except OSError:
                pass

    session_id = input_data.get("session_id", "unknown")
    transcript_path = input_data.get("transcript_path", "")
    session_info = input_data.get("session_info", {})
    project_root = input_data.get("project_root", ".")
    end_ts = input_data.get("end_ts", datetime.now().isoformat())

    # project_root の検証
    project_root = os.path.realpath(os.path.normpath(project_root))
    allowed_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if allowed_root:
        allowed_root = os.path.realpath(os.path.normpath(allowed_root))
        if project_root != allowed_root:
            print(f"[transcript-analyzer] Error: Invalid project_root", file=sys.stderr)
            return 1

    start_ts = session_info.get("start_ts", "")
    date_str = session_info.get("date", datetime.now().strftime("%Y-%m-%d"))
    subagent = session_info.get("subagent", "unknown")

    # transcript 解析
    events = parse_transcript(transcript_path, project_root)

    # 空のtranscriptの場合は警告してスキップ
    if not events:
        print(f"[transcript-analyzer] Warning: Empty or unreadable transcript, skipping log generation", file=sys.stderr)
        return 0

    # gitブランチを取得
    git_branch = extract_git_branch(events)

    steps = extract_execution_steps(events)
    final_response = get_final_response(steps)

    # Markdown 生成
    markdown_content = generate_markdown_log(
        session_info=session_info,
        steps=steps,
        final_response=final_response,
        start_ts=start_ts,
        end_ts=end_ts,
        transcript_path=transcript_path
    )

    # ファイル書き込み（ブランチ別ディレクトリ）
    log_file = write_markdown_log(
        project_root=project_root,
        date_str=date_str,
        session_id=session_id,
        subagent=subagent,
        content=markdown_content,
        branch=git_branch
    )

    # インデックス更新
    if log_file:
        # 相対パスに変換
        try:
            relative_log_file = str(Path(log_file).relative_to(Path(project_root) / LOG_BASE_DIR))
        except ValueError:
            # フォールバック: ファイル名のみ使用
            relative_log_file = Path(log_file).name

        write_index_entry(
            project_root=project_root,
            date_str=date_str,
            session_id=session_id,
            subagent=subagent,
            start_ts=start_ts,
            end_ts=end_ts,
            log_file=relative_log_file,
            branch=git_branch
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
