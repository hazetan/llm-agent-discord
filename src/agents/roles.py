from typing import Any

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories inside ~/ (e.g. '~/', '~/a'). Ignores build/.git/node_modules.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string", "description": "Target directory path starting with ~/"}},
            },
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a single file inside ~/ with 1-based line numbers on the left ('   1 | ...').",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path starting with ~/"},
                    "start_line": {"type": "integer", "description": "1-based start line (default 1)"},
                    "end_line": {"type": "integer", "description": "1-based end line (inclusive)"},
                    "max_lines": {"type": "integer", "description": "Max lines to read (default 500)"},
                },
                "required": ["path"],
            },
        },
    },
    "read_files": {
        "type": "function",
        "function": {
            "name": "read_files",
            "description": (
                "Batch-read multiple files at once with '=== FILE: ... ===' headers and 1-based line numbers on the left. "
                "Automatically skips build/.git/node_modules/binaries. Use this to inspect an entire project or multiple files in 1 step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file or directory paths to read (e.g. ['~/a/main.py', '~/a/utils.py']).",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Directory to recursively read all text files from (e.g. '~/a').",
                    },
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional file extensions filter (e.g. ['.py', '.md']).",
                    },
                    "max_lines_per_file": {"type": "integer", "description": "Max lines per file (default 500)."},
                },
            },
        },
    },
    "search_files": {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a keyword or regex across all text files in ~/ (with 1-based line numbers).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or regex pattern"},
                    "directory": {"type": "string", "description": "Target directory (default '~/')"},
                    "extensions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a single file inside ~/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path starting with ~/"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "write_files": {
        "type": "function",
        "function": {
            "name": "write_files",
            "description": "Batch-create or overwrite multiple files in a single tool call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                },
                "required": ["files"],
            },
        },
    },
    "edit_lines": {
        "type": "function",
        "function": {
            "name": "edit_lines",
            "description": (
                "Replace lines from 'start_line' to 'end_line' (1-based, inclusive, as shown by read_file/read_files) "
                "with 'new_text'. Supports batch editing multiple ranges across one or multiple files in a single call. "
                "Multiple edits in the same file are automatically applied bottom-up to prevent line number shifts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "List of line-range edits to apply in batch.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "Target file path (e.g. '~/a/main.py')"},
                                "start_line": {"type": "integer", "description": "1-based start line number (inclusive)"},
                                "end_line": {"type": "integer", "description": "1-based end line number (inclusive)"},
                                "new_text": {"type": "string", "description": "Multi-line replacement string (use '' to delete lines)"},
                            },
                            "required": ["path", "start_line", "end_line", "new_text"],
                        },
                    },
                },
                "required": ["edits"],
            },
        },
    },
    "replace_in_file": {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace an exact snippet in an existing file inside ~/. Prefer 'edit_lines' for faster and safer partial edits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    "run_command": {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command inside the Docker sandbox (working directory is ~/ i.e. /root).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for technical documentation or error solutions.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    "fetch_url": {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch text content from a URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    "export_zip": {
        "type": "function",
        "function": {
            "name": "export_zip",
            "description": "Package ~/ or a subdirectory into a ZIP archive in the exports folder.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string", "description": "Directory to zip, default '~/'"}},
            },
        },
    },
    "complete_phase": {
        "type": "function",
        "function": {
            "name": "complete_phase",
            "description": "Conclude your current role's phase and hand over to the next department.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string", "description": "Concise Japanese summary of work done."}},
                "required": ["summary"],
            },
        },
    },
}


def get_tools_for_role(tool_names: list[str]) -> list[dict[str, Any]]:
    return [TOOL_SCHEMAS[name] for name in tool_names if name in TOOL_SCHEMAS]


BATCH_EFFICIENCY_NOTE = (
    "【大容量コンテキスト活用・一括処理ルール】\n"
    "- 推論回数を最小化するため、ファイルを1つずつ確認せず `read_files`（複数パスやディレクトリ指定）や `search_files` で一括閲覧してください。\n"
    "- ファイル閲覧時は左側に1始まりの行番号（`   1 | ...`）が表示されます。部分編集を行う際は `edit_lines` を使い、`start_line` と `end_line` を指定して複数箇所・複数ファイルを1回で一括置換してください。\n"
    "- 複数ファイルの新規作成には `write_files` を使用して1ステップで完了させてください。"
)

ROLE_PROMPTS: dict[str, str] = {
    "PM": (
        "あなたは日本の大手1次請けSIerの【PM（プロジェクトマネージャー / 顧客窓口）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        f"{BATCH_EFFICIENCY_NOTE}\n"
        "【職務】\n"
        "1. 顧客（Discordユーザー）の発言を受け取り、開発・調査・修正の依頼であれば `~/TASK.md` に `[TODO]` ステータスのタスクとして明確に起票・更新してください。\n"
        "2. 必要に応じて `read_files` 等で既存資産を一括確認し、正確な対象パスを `~/TASK.md` に記載してください。\n"
        "3. 顧客からZIP出力を求められた場合は `export_zip` を呼び出してください。\n"
        "4. 最後に `complete_phase` を呼び出し、`summary` に顧客への丁寧で簡潔な日本語の応答メッセージを入れて完了してください。"
    ),
    "SE": (
        "あなたは日本の大手1次請けSIerの【SE（システムエンジニア / アーキテクト）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        f"{BATCH_EFFICIENCY_NOTE}\n"
        "【職務】\n"
        "1. `~/TASK.md` 内の `[TODO]` タスクを確認し、`read_files` や `search_files` で現在の `~/` 内のディレクトリ構造・既存コードを一括調査してください。\n"
        "2. どのディレクトリ（例: `~/a/`）のどのファイルを作成・改修するか、具体的なファイルパス・モジュール設計・検証手順を `~/DESIGN.md` に記述してください。\n"
        "3. 設計が完了したら、`edit_lines` 等で `~/TASK.md` の該当タスクのタグを `[TODO]` から `[IMPL]` に書き換え、`complete_phase` を呼び出してください。"
    ),
    "PG": (
        "あなたは日本の大手1次請けSIerの【PG（プログラマー / 実装担当）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        f"{BATCH_EFFICIENCY_NOTE}\n"
        "【職務】\n"
        "1. `~/TASK.md` の `[IMPL]` または `[FIX]`（差戻し）タスクと、`~/DESIGN.md` の設計書を確認してください。\n"
        "2. 設計書で指定された `~/` 配下のパスに対し、新規作成は `write_files`、既存コードの改修は `read_files` で行番号を確認した上で `edit_lines` を使って一括実装し、`run_command` で動作を確認してください。\n"
        "3. 実装と単体確認が終わったら、`~/TASK.md` の該当タスクのタグを `[QA]`（検証待ち）に更新し、`complete_phase` を呼び出してください。"
    ),
    "QA": (
        "あなたは日本の大手1次請けSIerの【QA（品質保証・テスト検証担当）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        f"{BATCH_EFFICIENCY_NOTE}\n"
        "【職務】\n"
        "1. `~/TASK.md` の `[QA]` タスクを確認し、`read_files` および `run_command` を用いてプログラムが要件および `~/DESIGN.md` 通りに動作するか厳格にテストしてください。\n"
        "2. 問題なく合格した場合は、`~/TASK.md` のタグを `[DONE]` に更新し、`~/README.md` のプロジェクト台帳に最新の構成・成果を反映してください。\n"
        "3. 不具合や未実装がある場合は、`~/TASK.md` のタグを `[FIX]` に戻し、修正すべき原因と対象行番号を追記してください。\n"
        "4. 最後に `complete_phase` を呼び出してください。"
    ),
    "AUDITOR": (
        "あなたは日本の大手1次請けSIerの【PMO / 内部監査担当】です。\n"
        "各部門（PM, SE, PG, QA）の最新実行ログと `~/TASK.md` を分析し、以下の観点で監査レポートを `~/REPORT.md` に上書き出力してください：\n"
        "- 同じエラーやパス指定ミスの繰り返しがないか\n"
        "- PGとQAの間で無限差戻しループが発生していないか\n"
        "- 次のサイクルでSE/PGが注意すべき具体的な技術的改善策\n"
        "`~/REPORT.md` を更新したら `complete_phase` を呼び出してください。"
    ),
}


ROLE_ALLOWED_TOOLS: dict[str, list[str]] = {
    "PM": [
        "list_files", "read_file", "read_files", "search_files",
        "write_file", "write_files", "edit_lines", "replace_in_file",
        "export_zip", "complete_phase",
    ],
    "SE": [
        "list_files", "read_file", "read_files", "search_files",
        "write_file", "write_files", "edit_lines", "replace_in_file",
        "web_search", "fetch_url", "complete_phase",
    ],
    "PG": [
        "list_files", "read_file", "read_files", "search_files",
        "write_file", "write_files", "edit_lines", "replace_in_file",
        "run_command", "web_search", "fetch_url", "complete_phase",
    ],
    "QA": [
        "list_files", "read_file", "read_files", "search_files",
        "write_file", "write_files", "edit_lines", "replace_in_file",
        "run_command", "complete_phase",
    ],
    "AUDITOR": ["read_file", "read_files", "write_file", "edit_lines", "complete_phase"],
}