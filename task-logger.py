#!/usr/bin/env python3
"""
Task tool（サブエージェント）実行の自動ログ記録フック

使用フック:
- UserPromptSubmit - ユーザープロンプトを記録
- SubagentStop - サブエージェント終了時に transcript 解析を起動
- Stop - セッション終了時にサマリーを生成

Note:
- VSCode拡張版ではPreToolUseフックが動作しないため、
  SubagentStopのみで動作するよう設計
- Task情報は親transcriptから抽出

ログ出力先: .claude/logs/agents/
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# 共通設定をインポート
from config import (
    MAX_PARENT_TRANSCRIPT_EVENTS,
    MAX_PARENT_TRANSCRIPT_MB,
    MAX_PROMPT_LENGTH,
    SESSION_CACHE_FILE,
    SESSION_CACHE_LOCK,
    USER_PROMPTS_FILE,
    FileLock,
    cleanup_old_cache_entries,
    get_project_root,
    is_safe_path,
)


# =============================================================================
# セッションキャッシュ操作（ファイルロック付き）
# =============================================================================
def load_session_cache() -> dict[str, Any]:
    """
    セッションキャッシュを読み込み（ファイルロック付き）

    Returns:
        セッションキャッシュの辞書
    """
    try:
        with FileLock(SESSION_CACHE_LOCK, timeout=5.0):
            if SESSION_CACHE_FILE.exists():
                content = SESSION_CACHE_FILE.read_text(encoding="utf-8")
                if content.strip():
                    return json.loads(content)
    except TimeoutError:
        print("[task-logger] Warning: Failed to acquire cache lock (timeout)", file=sys.stderr)
    except json.JSONDecodeError as e:
        print(f"[task-logger] Warning: Invalid cache JSON: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[task-logger] Warning: Failed to load session cache: {e}", file=sys.stderr)
    return {}


def save_session_cache(cache: dict[str, Any]) -> None:
    """
    セッションキャッシュを保存（ファイルロック付き）

    Args:
        cache: 保存するキャッシュ辞書
    """
    try:
        with FileLock(SESSION_CACHE_LOCK, timeout=5.0):
            SESSION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_CACHE_FILE.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
    except TimeoutError:
        print("[task-logger] Warning: Failed to acquire cache lock for save (timeout)", file=sys.stderr)
    except Exception as e:
        print(f"[task-logger] Warning: Failed to save session cache: {e}", file=sys.stderr)


# =============================================================================
# イベントハンドラ
# =============================================================================
def handle_pre_tool_use(hook_input: dict[str, Any]) -> int:
    """
    PreToolUse イベント処理（Task 開始）

    セッションキャッシュに開始情報を保存（後で transcript-analyzer が使用）

    Args:
        hook_input: フックからの入力データ

    Returns:
        終了コード（0: 成功）
    """
    # Task tool 以外は無視
    if hook_input.get("tool_name") != "Task":
        return 0

    tool_input = hook_input.get("tool_input", {})
    subagent_type = tool_input.get("subagent_type", "unknown")
    session_id = hook_input.get("session_id", "unknown")
    tool_use_id = hook_input.get("tool_use_id", "")
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")

    # セッションキャッシュを読み込み、古いエントリをクリーンアップ
    cache = load_session_cache()
    cache = cleanup_old_cache_entries(cache)

    # 新規エントリを追加
    cache_key = f"{session_id}_{tool_use_id}"
    cache[cache_key] = {
        "start_ts": now.isoformat(),
        "subagent": subagent_type,
        "date": date_str,
        "description": tool_input.get("description", ""),
        "prompt": tool_input.get("prompt", "")[:MAX_PROMPT_LENGTH],
        "model": tool_input.get("model"),
        "cwd": hook_input.get("cwd", "")
    }
    save_session_cache(cache)

    return 0


def handle_user_prompt_submit(hook_input: dict[str, Any]) -> int:
    """
    UserPromptSubmit イベント処理（ユーザープロンプト記録）

    ユーザーの入力プロンプトを記録し、サブエージェント呼び出しの文脈を保存

    Args:
        hook_input: フックからの入力データ

    Returns:
        終了コード（0: 成功）
    """
    session_id = hook_input.get("session_id", "unknown")
    prompt = hook_input.get("prompt", "")
    now = datetime.now()

    if not prompt:
        return 0

    # プロンプトエントリを作成
    entry = {
        "timestamp": now.isoformat(),
        "session_id": session_id,
        "prompt": prompt[:MAX_PROMPT_LENGTH * 2],  # ユーザープロンプトは少し長めに保存
        "date": now.strftime("%Y-%m-%d")
    }

    # ファイルに追記（ロック付き）
    project_root = get_project_root()
    prompts_file = project_root / USER_PROMPTS_FILE
    lock_file = str(prompts_file) + ".lock"

    try:
        prompts_file.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(lock_file, timeout=5.0):
            with open(prompts_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except TimeoutError:
        print(f"[task-logger] Warning: Lock timeout for user prompts", file=sys.stderr)
    except Exception as e:
        print(f"[task-logger] Warning: Failed to write user prompt: {e}", file=sys.stderr)

    return 0


def extract_task_info_from_transcript(
    transcript_path: str,
    agent_id: str,
    project_root: Path
) -> tuple[dict[str, Any] | None, str]:
    """
    親transcriptからTask呼び出し情報を抽出（agent_idで紐付け）

    パフォーマンス対策:
    - ファイルサイズ上限（MAX_PARENT_TRANSCRIPT_MB）
    - イベント数上限（MAX_PARENT_TRANSCRIPT_EVENTS）

    Args:
        transcript_path: 親transcriptのパス
        agent_id: サブエージェントID（tool_use_idと一致）
        project_root: プロジェクトルート（パス検証用）

    Returns:
        (Task情報, tool_use_id) のタプル。見つからない場合は (None, "")
    """
    # パス検証: ホームディレクトリまたはプロジェクトルート内のみ許可
    expanded_path = os.path.expanduser(transcript_path)
    allowed_prefixes = [os.path.expanduser("~"), str(project_root)]
    if not is_safe_path(expanded_path, allowed_prefixes):
        print(f"[task-logger] Warning: Transcript path outside allowed directories", file=sys.stderr)
        return None, ""

    # ファイルサイズチェック（パフォーマンス対策）
    try:
        file_size = os.path.getsize(expanded_path)
        max_size_bytes = MAX_PARENT_TRANSCRIPT_MB * 1024 * 1024
        if file_size > max_size_bytes:
            print(f"[task-logger] Warning: Parent transcript too large ({file_size / 1024 / 1024:.1f}MB > {MAX_PARENT_TRANSCRIPT_MB}MB), skipping", file=sys.stderr)
            return None, ""
    except OSError:
        pass  # ファイルサイズ取得失敗は無視して続行

    task_infos: list[tuple[dict[str, Any], str]] = []  # (task_info, tool_use_id)
    event_count = 0
    try:
        with open(expanded_path, "r", encoding="utf-8") as f:
            for line in f:
                # イベント数上限チェック（パフォーマンス対策）
                event_count += 1
                if event_count > MAX_PARENT_TRANSCRIPT_EVENTS:
                    print(f"[task-logger] Warning: Parent transcript too many events (>{MAX_PARENT_TRANSCRIPT_EVENTS}), stopping scan", file=sys.stderr)
                    break

                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    # assistantメッセージ内のtool_useを探す
                    if event.get("type") == "assistant":
                        message = event.get("message", {})
                        content_list = message.get("content", [])
                        for content in content_list:
                            if isinstance(content, dict) and content.get("type") == "tool_use":
                                if content.get("name") == "Task":
                                    tool_use_id = content.get("id", "")
                                    tool_input = content.get("input", {})
                                    if "subagent_type" in tool_input:
                                        task_info = {
                                            "subagent": tool_input.get("subagent_type", "unknown"),
                                            "description": tool_input.get("description", ""),
                                            "prompt": tool_input.get("prompt", "")[:MAX_PROMPT_LENGTH],
                                            "model": tool_input.get("model"),
                                        }
                                        task_infos.append((task_info, tool_use_id))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"[task-logger] Warning: Failed to read transcript: {e}", file=sys.stderr)
        return None, ""

    # agent_id と一致する tool_use_id を探す（優先）
    for task_info, tool_use_id in task_infos:
        if tool_use_id and agent_id and tool_use_id in agent_id:
            return task_info, tool_use_id

    # 一致しない場合は最新のTask情報を返す（フォールバック）
    if task_infos:
        print(f"[task-logger] Warning: No matching tool_use_id found for agent_id={agent_id}, using latest Task", file=sys.stderr)
        return task_infos[-1]

    return None, ""


def handle_subagent_stop(hook_input: dict[str, Any]) -> int:
    """
    SubagentStop イベント処理（Task 終了）

    バックグラウンドで transcript-analyzer.py を起動

    Args:
        hook_input: フックからの入力データ

    Returns:
        終了コード（0: 成功, 1: エラー）
    """
    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path", "")
    agent_id = hook_input.get("agent_id", "")
    agent_transcript_path = hook_input.get("agent_transcript_path", "")

    if not transcript_path or not agent_transcript_path:
        return 0

    now = datetime.now()
    project_root = get_project_root()

    # 親transcriptからTask情報を取得（PreToolUseの代替）
    session_info, tool_use_id = extract_task_info_from_transcript(
        transcript_path, agent_id, project_root
    )

    if not session_info:
        return 0

    # PreToolUseキャッシュから開始時刻を取得（あれば）
    start_ts = now.isoformat()  # デフォルトは現在時刻
    cache = load_session_cache()
    cache_key = f"{session_id}_{tool_use_id}"
    if cache_key in cache:
        cached_info = cache[cache_key]
        start_ts = cached_info.get("start_ts", start_ts)

    # 開始時刻と日付を設定
    session_info["start_ts"] = start_ts
    session_info["date"] = now.strftime("%Y-%m-%d")
    session_info["cwd"] = hook_input.get("cwd", "")

    # バックグラウンドで transcript-analyzer.py を起動
    analyzer_script = project_root / ".claude" / "hooks" / "task-logging" / "transcript-analyzer.py"

    if not analyzer_script.exists():
        print(f"[task-logger] Error: {analyzer_script} not found", file=sys.stderr)
        return 1

    # 解析に必要な情報を JSON で渡す
    analyzer_input = {
        "session_id": session_id,
        "transcript_path": agent_transcript_path,  # サブエージェントのtranscript
        "session_info": session_info,
        "project_root": str(project_root),
        "end_ts": now.isoformat()
    }

    try:
        if sys.platform == "win32":
            # Windows: 一時ファイル経由でデータを渡す（stdin問題回避）
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                encoding="utf-8",
                delete=False
            ) as tmp_file:
                json.dump(analyzer_input, tmp_file, ensure_ascii=False)
                tmp_path = tmp_file.name

            # CREATE_NEW_PROCESS_GROUP で親から切り離し
            subprocess.Popen(
                [sys.executable, str(analyzer_script), "--input-file", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            )
        else:
            # Linux/Mac: nohup 相当、stdin で渡す
            process = subprocess.Popen(
                [sys.executable, str(analyzer_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            process.stdin.write(json.dumps(analyzer_input).encode("utf-8"))
            process.stdin.close()
    except Exception as e:
        print(f"[task-logger] Error starting analyzer: {e}", file=sys.stderr)
        return 1

    return 0


def handle_stop(hook_input: dict[str, Any]) -> int:
    """
    Stop イベント処理（セッション終了）

    バックグラウンドで session-summary.py を起動してサマリーを生成

    Args:
        hook_input: フックからの入力データ

    Returns:
        終了コード（0: 成功, 1: エラー）
    """
    # stop_hook_active が True の場合は無限ループ防止のためスキップ
    if hook_input.get("stop_hook_active"):
        return 0

    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path", "")
    now = datetime.now()

    # バックグラウンドで session-summary.py を起動
    project_root = get_project_root()
    summary_script = project_root / ".claude" / "hooks" / "task-logging" / "session-summary.py"

    if not summary_script.exists():
        print(f"[task-logger] Error: {summary_script} not found", file=sys.stderr)
        return 1

    # transcriptからブランチ情報と開始時刻を取得（パス検証付き）
    branch = ""
    start_ts = ""
    if transcript_path:
        expanded_path = os.path.expanduser(transcript_path)
        allowed_prefixes = [os.path.expanduser("~"), str(project_root)]
        if is_safe_path(expanded_path, allowed_prefixes):
            try:
                with open(expanded_path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        first_event = json.loads(first_line)
                        branch = first_event.get("gitBranch", "")
                        # 開始時刻を取得（timestamp または sessionStartTimestamp）
                        start_ts = first_event.get("sessionStartTimestamp", "")
                        if not start_ts:
                            start_ts = first_event.get("timestamp", "")
            except Exception:
                pass

    # サマリー生成に必要な情報を JSON で渡す
    summary_input = {
        "session_id": session_id,
        "project_root": str(project_root),
        "start_ts": start_ts,
        "end_ts": now.isoformat(),
        "branch": branch
    }

    try:
        if sys.platform == "win32":
            # Windows: 一時ファイル経由でデータを渡す
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                encoding="utf-8",
                delete=False
            ) as tmp_file:
                json.dump(summary_input, tmp_file, ensure_ascii=False)
                tmp_path = tmp_file.name

            subprocess.Popen(
                [sys.executable, str(summary_script), "--input-file", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            )
        else:
            # Linux/Mac: nohup 相当
            process = subprocess.Popen(
                [sys.executable, str(summary_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            process.stdin.write(json.dumps(summary_input).encode("utf-8"))
            process.stdin.close()
    except Exception as e:
        print(f"[task-logger] Error starting session-summary: {e}", file=sys.stderr)
        return 1

    return 0


# =============================================================================
# メイン処理
# =============================================================================
def main() -> int:
    """
    メイン処理: stdin から JSON を受け取り、イベントに応じて処理

    Returns:
        終了コード
    """
    try:
        # Windows環境でのエンコーディング問題対策
        if sys.platform == "win32":
            import io
            sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"[task-logger] Error: Invalid JSON input: {e}", file=sys.stderr)
        return 1

    event = hook_input.get("hook_event_name")

    if event == "PreToolUse":
        return handle_pre_tool_use(hook_input)
    elif event == "UserPromptSubmit":
        return handle_user_prompt_submit(hook_input)
    elif event == "SubagentStop":
        return handle_subagent_stop(hook_input)
    elif event == "Stop":
        return handle_stop(hook_input)
    else:
        # 対象外のイベント
        return 0


if __name__ == "__main__":
    sys.exit(main())
