# Task Logging System

[日本語](README.md) | **English**

A hook system that automatically logs subagent (Task tool) executions.

## Overview

Uses Claude Code's Hooks feature to automatically record subagent execution details in Markdown format.

### Features

- **Automatic Logging**: Automatically captures Task tool (subagent) executions
- **Markdown Format**: Saves logs in human-readable format
- **Session Summaries**: Auto-generates session summaries on session end
- **Sensitive Data Masking**: Automatically masks API keys, tokens, passwords, etc.
- **Branch Organization**: Auto-categorizes logs by Git branch
- **Cross-Platform**: Windows / macOS / Linux support

### Requirements

- **Python**: 3.10+
- **Dependencies**: None (standard library only)
- **Claude Code**: Version with Hooks feature available

### File Structure

| File                     | Purpose                                                |
| ------------------------ | ------------------------------------------------------ |
| `config.py`              | Common constants & utilities (FileLock, sanitize)      |
| `task-logger.py`         | Hook handlers (UserPromptSubmit / SubagentStop / Stop) |
| `transcript-analyzer.py` | Transcript parsing & Markdown generation               |
| `session-summary.py`     | Session summary generation (for Stop hook)             |

### Workflow

```
1. UserPromptSubmit
   → Records user prompt to user_prompts.jsonl
   → Preserves context for subagent calls

2. PreToolUse (matcher: Task)
   → Caches session start info

3. SubagentStop
   → Launches transcript-analyzer.py in background
   → Parses transcript and generates Markdown log

4. Stop
   → Launches session-summary.py in background
   → Generates session-wide summary
```

## Installation

1. Copy this directory to your project's `.claude/hooks/task-logging/`:

```bash
cp -r task-logging /your-project/.claude/hooks/
```

2. Add hook configuration to `.claude/settings.json` (see "Enable" section)

3. Restart Claude Code

## Enable / Disable

### Enable

Add the following hook configuration to `.claude/settings.json`:

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

### Disable

Remove the following from `.claude/settings.json`:

- `PreToolUse` section
- `UserPromptSubmit` section
- `SubagentStop` section
- `Stop` section

## Log Locations

```
.claude/logs/
├── agents/
│   ├── index.jsonl                              # Summary index of all logs
│   ├── user_prompts.jsonl                       # User prompt history
│   └── {YYYY-MM-DD}/
│       └── {branch}/                            # Branch-specific directory
│           └── {HHMMSS}_{subagent}_{uuid}.md    # Subagent detail log
└── sessions/
    └── {YYYY-MM-DD}/
        └── {branch}/                            # Branch-specific directory
            └── {HHMMSS}_{session_id}.md         # Session summary
```

### Subagent Log Contents

Each Markdown log includes:

- **Metadata**: Execution time, subagent name, model, duration
- **Task Content**: Description and prompt
- **Execution Process**: Tool usage history (inputs & results)
- **Final Result**: Subagent response

### Session Summary Contents

Summaries generated at session end include:

- **Overview**: Session ID, start/end time, call count
- **User Prompt History**: User inputs during session
- **Subagent Call History**: Execution results for each subagent

## Configuration

Adjustable values in `config.py`:

| Constant                       | Default | Description                         |
| ------------------------------ | ------- | ----------------------------------- |
| `MAX_CONTENT_LENGTH`           | 1000    | Max display length for tool results |
| `MAX_TOOL_RESULT_LENGTH`       | 500     | Max tool result length in Markdown  |
| `MAX_EVENTS`                   | 1000    | Max events to parse                 |
| `MAX_FILE_SIZE_MB`             | 10      | Max file size to parse              |
| `MAX_PROMPT_LENGTH`            | 500     | Max prompt storage length           |
| `CACHE_TTL_HOURS`              | 24      | Cache entry retention period        |
| `MAX_PARENT_TRANSCRIPT_MB`     | 5       | Max parent transcript size          |
| `MAX_PARENT_TRANSCRIPT_EVENTS` | 500     | Max parent transcript events        |
| `MAX_TOOL_INPUT_LENGTH`        | 1000    | Max tool input storage length       |

## Hook Reference

| Hook             | Processing                       | Mode               |
| ---------------- | -------------------------------- | ------------------ |
| PreToolUse       | Cache Task start info            | Sync (lightweight) |
| UserPromptSubmit | Record user prompt               | Sync (lightweight) |
| SubagentStop     | Generate individual subagent log | Background         |
| Stop             | Generate session-wide summary    | Background         |

## Security

### Sensitive Data Masking

The following patterns are automatically masked during logging:

- **API Keys**: OpenAI (`sk-*`), AWS (`AKIA*`), Google (`AIza*`)
- **GitHub Tokens**: Personal Access Token (`ghp_*`), OAuth (`gho_*`)
- **Passwords**: `password=`, `secret=` patterns
- **Bearer Tokens**: `Authorization: Bearer *`
- **Webhook URLs**: Slack (`hooks.slack.com`), Discord
- **JWT**: Tokens starting with `eyJ`
- **Supabase**: `sbp_*`, `service_role_key`
- **Stripe**: `sk_live_*`, `pk_live_*`

### Cache Files

Session cache is stored in user-specific secure directories:

- **Windows**: `%LOCALAPPDATA%\claude-task-logger\`
- **macOS/Linux**: `~/.cache/claude-task-logger/` (permissions: 0700)

### Path Validation

All file paths are validated to be within allowed directories to prevent directory traversal attacks.

## License

MIT License
