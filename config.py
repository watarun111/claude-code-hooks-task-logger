#!/usr/bin/env python3
"""
サブエージェントログシステム共通設定

task-logger.py と transcript-analyzer.py で共有する定数とユーティリティ関数
"""
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# =============================================================================
# 定数
# =============================================================================
LOG_BASE_DIR = ".claude/logs/agents"
INDEX_FILE = ".claude/logs/agents/index.jsonl"
SESSION_SUMMARY_DIR = ".claude/logs/sessions"
USER_PROMPTS_FILE = ".claude/logs/agents/user_prompts.jsonl"

# transcript 解析設定
MAX_CONTENT_LENGTH = 1000  # ツール結果の最大表示長
MAX_TOOL_RESULT_LENGTH = 500  # Markdown 内のツール結果の最大表示長
MAX_EVENTS = 1000  # 最大イベント数
MAX_FILE_SIZE_MB = 10  # 最大ファイルサイズ（MB）
MAX_PROMPT_LENGTH = 500  # プロンプトの最大保存長

# キャッシュ設定
CACHE_TTL_HOURS = 24  # 古いキャッシュエントリの保持期間（時間）
STALE_LOCK_TIMEOUT_SEC = 60  # ロックファイルが古いと判断する秒数

# 親transcript読み込み制限（パフォーマンス対策）
MAX_PARENT_TRANSCRIPT_MB = 5  # 親transcript最大サイズ（MB）
MAX_PARENT_TRANSCRIPT_EVENTS = 500  # 親transcript最大イベント数

# ツール入力の最大表示長（ログ肥大化防止）
MAX_TOOL_INPUT_LENGTH = 1000  # ツール入力JSON最大長

# セキュアなキャッシュディレクトリ: ユーザー固有ディレクトリを使用
# シンボリックリンク攻撃対策として共有/tmp を避ける
def _get_secure_cache_dir() -> Path:
    """ユーザー固有のセキュアなキャッシュディレクトリを取得"""
    if sys.platform == "win32":
        # Windows: %LOCALAPPDATA%/claude-task-logger/
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            cache_dir = Path(localappdata) / "claude-task-logger"
        else:
            # フォールバック: ユーザーホームディレクトリ
            cache_dir = Path.home() / ".cache" / "claude-task-logger"
    else:
        # Unix: ~/.cache/claude-task-logger/
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache:
            cache_dir = Path(xdg_cache) / "claude-task-logger"
        else:
            cache_dir = Path.home() / ".cache" / "claude-task-logger"

    # ディレクトリ作成（権限0700）
    cache_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            os.chmod(cache_dir, 0o700)
        except OSError:
            pass

    return cache_dir

_SECURE_CACHE_DIR = _get_secure_cache_dir()
SESSION_CACHE_FILE = _SECURE_CACHE_DIR / "sessions.json"
SESSION_CACHE_LOCK = _SECURE_CACHE_DIR / "sessions.lock"
INDEX_LOCK_SUFFIX = ".lock"  # index.jsonl用ロックファイルサフィックス


# =============================================================================
# ユーティリティ関数
# =============================================================================
def get_project_root() -> Path:
    """プロジェクトルートを取得"""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))


def sanitize_filename(name: str) -> str:
    """
    ファイル名として安全な文字列に変換

    Args:
        name: サニタイズする文字列

    Returns:
        安全なファイル名文字列
    """
    # 英数字、ハイフン、アンダースコアのみ許可
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    # 先頭のドットを除去（隠しファイル防止）
    sanitized = sanitized.lstrip('.')
    # 連続するアンダースコアを1つに
    sanitized = re.sub(r'_+', '_', sanitized)
    # 空文字列の場合はデフォルト値
    return sanitized[:50] or "unknown"  # 最大50文字


def sanitize_branch_name(branch: str) -> str:
    """
    ブランチ名をディレクトリ名として安全な文字列に変換

    Args:
        branch: ブランチ名（例: "feature/some-feature", "develop"）

    Returns:
        安全なディレクトリ名（例: "feature-some-feature", "develop"）
    """
    if not branch:
        return "unknown"
    # スラッシュをハイフンに変換
    sanitized = branch.replace("/", "-")
    # 英数字、ハイフン、アンダースコアのみ許可
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', sanitized)
    # 先頭のドットを除去
    sanitized = sanitized.lstrip('.')
    # 連続するハイフン/アンダースコアを1つに
    sanitized = re.sub(r'[-_]+', '-', sanitized)
    # 先頭・末尾のハイフンを除去
    sanitized = sanitized.strip('-')
    # 空文字列の場合はデフォルト値
    return sanitized[:50] or "unknown"


# 機密情報マスキング用パターン
SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    # APIキー・トークン
    (r'(?i)(api[_-]?key|apikey|api_token|access[_-]?token|auth[_-]?token|bearer)\s*[=:]\s*["\']?([a-zA-Z0-9_-]{20,})["\']?', r'\1=***REDACTED***'),
    (r'(?i)(sk-[a-zA-Z0-9-]{20,})', r'***REDACTED_API_KEY***'),  # OpenAI API key (including sk-proj-)
    (r'(?i)(ghp_[a-zA-Z0-9]{36,})', r'***REDACTED_GITHUB_TOKEN***'),  # GitHub personal access token
    (r'(?i)(gho_[a-zA-Z0-9]{36,})', r'***REDACTED_GITHUB_OAUTH***'),  # GitHub OAuth token
    # パスワード
    (r'(?i)(password|passwd|pwd|secret)\s*[=:]\s*["\']?([^\s"\']{8,})["\']?', r'\1=***REDACTED***'),
    # AWS credentials
    (r'(?i)(AKIA[A-Z0-9]{16})', r'***REDACTED_AWS_KEY***'),
    (r'(?i)(aws[_-]?secret[_-]?access[_-]?key)\s*[=:]\s*["\']?([a-zA-Z0-9/+=]{40})["\']?', r'\1=***REDACTED***'),
    # 一般的なシークレット
    (r'(?i)(private[_-]?key|secret[_-]?key|encryption[_-]?key)\s*[=:]\s*["\']?([^\s"\']{16,})["\']?', r'\1=***REDACTED***'),
    # Bearer token in headers
    (r'(?i)(Authorization:\s*Bearer\s+)([a-zA-Z0-9._-]{20,})', r'\1***REDACTED***'),
    # Slack webhook URL
    (r'(https://hooks\.slack\.com/services/[A-Za-z0-9/]+)', r'***REDACTED_SLACK_WEBHOOK***'),
    # Discord webhook URL
    (r'(https://discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+)', r'***REDACTED_DISCORD_WEBHOOK***'),
    # JWT token (eyJ で始まる Base64 エンコードされた JSON)
    (r'(eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)', r'***REDACTED_JWT***'),
    # Supabase keys
    (r'(?i)(sbp_[a-zA-Z0-9]{20,})', r'***REDACTED_SUPABASE_KEY***'),
    (r'(?i)(service_role[_-]?key)\s*[=:]\s*["\']?([a-zA-Z0-9._-]{30,})["\']?', r'\1=***REDACTED***'),
    # Google API key
    (r'(AIza[A-Za-z0-9_-]{35})', r'***REDACTED_GOOGLE_API_KEY***'),
    # Stripe keys
    (r'(?i)(sk_live_[a-zA-Z0-9]{24,})', r'***REDACTED_STRIPE_SECRET***'),
    (r'(?i)(pk_live_[a-zA-Z0-9]{24,})', r'***REDACTED_STRIPE_PUBLISHABLE***'),
]


def redact_sensitive_data(text: str) -> str:
    """
    テキスト内の機密情報をマスキング

    API キー、トークン、パスワードなどの機密情報を検出し、
    ***REDACTED*** に置換する。

    Args:
        text: マスキング対象のテキスト

    Returns:
        機密情報がマスキングされたテキスト
    """
    if not text:
        return text

    result = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = re.sub(pattern, replacement, result)

    return result


def is_safe_path(path: str, allowed_prefixes: list[str]) -> bool:
    """
    パスが許可されたプレフィックス内にあるか検証

    シンボリックリンクを解決し、ディレクトリトラバーサル攻撃を防ぐ。
    Windows では大文字小文字を無視して比較する。

    Args:
        path: 検証するパス
        allowed_prefixes: 許可されたディレクトリのリスト

    Returns:
        安全な場合True
    """
    try:
        # シンボリックリンクを解決して絶対パスを取得
        abs_path = os.path.realpath(os.path.normpath(path))

        for prefix in allowed_prefixes:
            abs_prefix = os.path.realpath(os.path.normpath(prefix))
            # パスの末尾にセパレータを付けて完全一致を確認
            # (例: /tmp/test が /tmp/testing にマッチしないように)
            prefix_with_sep = abs_prefix.rstrip(os.sep) + os.sep

            if sys.platform == "win32":
                # Windows では大文字小文字を無視
                if abs_path.lower() == abs_prefix.lower():
                    return True
                if abs_path.lower().startswith(prefix_with_sep.lower()):
                    return True
            else:
                if abs_path == abs_prefix:
                    return True
                if abs_path.startswith(prefix_with_sep):
                    return True

        return False
    except Exception:
        return False


def cleanup_old_cache_entries(cache: dict[str, Any]) -> dict[str, Any]:
    """
    古いキャッシュエントリを削除

    Args:
        cache: セッションキャッシュ

    Returns:
        クリーンアップ後のキャッシュ
    """
    now = datetime.now()
    cutoff = now - timedelta(hours=CACHE_TTL_HOURS)

    cleaned: dict[str, Any] = {}
    for key, value in cache.items():
        try:
            start_ts_str = value.get("start_ts", "")
            if start_ts_str:
                start_ts = datetime.fromisoformat(start_ts_str)
                if start_ts > cutoff:
                    cleaned[key] = value
        except (ValueError, TypeError, AttributeError):
            # パース失敗したエントリは削除
            pass

    return cleaned


# =============================================================================
# ファイルロック（クロスプラットフォーム）
# =============================================================================
class FileLock:
    """
    シンプルなファイルロック実装（標準ライブラリのみ使用）

    プラットフォーム動作の違い:
        - Unix: ロックファイルは即座に削除される
        - Windows: ファイルハンドルを閉じた後、削除前に 10ms 待機する
          （ファイルシステムの遅延によるエラーを回避）

    使用例:
        with FileLock(lock_path):
            # ロック中の処理
    """

    def __init__(self, lock_path: str | Path, timeout: float = 10.0):
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self._lock_file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    def acquire(self) -> None:
        """ロックを取得"""
        import time

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        start_time = time.time()

        while True:
            try:
                # O_CREAT | O_EXCL で排他的に作成
                fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                self._lock_file = fd
                # ロックファイルにPIDを書き込み
                os.write(fd, str(os.getpid()).encode())
                return
            except FileExistsError:
                # ロックファイルが既に存在する場合
                if time.time() - start_time > self.timeout:
                    # タイムアウト: 古いロックファイルを強制削除
                    try:
                        # ロックファイルが古すぎる場合はリネームしてから削除（TOCTOU対策）
                        mtime = self.lock_path.stat().st_mtime
                        if time.time() - mtime > STALE_LOCK_TIMEOUT_SEC:
                            # アトミックにリネームしてから削除
                            stale_path = self.lock_path.with_suffix(".stale")
                            try:
                                os.rename(str(self.lock_path), str(stale_path))
                                stale_path.unlink(missing_ok=True)
                            except FileNotFoundError:
                                # 他のプロセスが既に削除した
                                pass
                            continue
                    except FileNotFoundError:
                        # ロックファイルが消えた場合は再試行
                        continue
                    except Exception:
                        pass
                    raise TimeoutError(f"Failed to acquire lock: {self.lock_path}")
                time.sleep(0.01)  # 10ms待機

    def release(self) -> None:
        """ロックを解放"""
        # まず参照をクリア（他のスレッドからの二重解放を防ぐ）
        lock_file = self._lock_file
        self._lock_file = None

        if lock_file is not None:
            try:
                os.close(lock_file)
            except Exception:
                pass

        # Windows ではファイルハンドルを閉じた後、削除前に少し待機
        if sys.platform == "win32":
            import time
            time.sleep(0.01)  # 10ms

        try:
            self.lock_path.unlink(missing_ok=True)
        except Exception:
            pass
