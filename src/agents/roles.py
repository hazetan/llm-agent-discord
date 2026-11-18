from typing import Any

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories inside ~/ (e.g. '~/', '~/a').",
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
            "description": "Read a file inside ~/ (e.g. '~/README.md', '~/a/README.md').",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path starting with ~/"},
                    "max_lines": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file inside ~/.",
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
    "replace_in_file": {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace an exact snippet in an existing file inside ~/.",
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


ROLE_PROMPTS: dict[str, str] = {
    "PM": (
        "あなたは日本の大手1次請けSIerの【PM（プロジェクトマネージャー / 顧客窓口）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        "【職務】\n"
        "1. 顧客（Discordユーザー）の発言を受け取り、開発・調査・修正の依頼であれば `~/TASK.md` に `[TODO]` ステータスのタスクとして明確に起票・更新してください。\n"
        "2. 必要に応じて `list_files` や `read_file` で `~/a/README.md` などの既存資産を確認し、正確な対象パスを `~/TASK.md` に記載してください。\n"
        "3. 顧客からZIP出力を求められた場合は `export_zip` を呼び出してください。\n"
        "4. 最後に `complete_phase` を呼び出し、`summary` に顧客への丁寧で簡潔な日本語の応答メッセージを入れて完了してください。"
    ),
    "SE": (
        "あなたは日本の大手1次請けSIerの【SE（システムエンジニア / アーキテクト）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        "【職務】\n"
        "1. `~/TASK.md` 内の `[TODO]` タスクを確認し、`list_files` や `read_file` で現在の `~/` 内のディレクトリ構造・既存コードを徹底調査してください。\n"
        "2. どのディレクトリ（例: `~/a/`）のどのファイルを作成・改修するか、具体的なファイルパス・モジュール設計・検証手順を `~/DESIGN.md` に記述してください。\n"
        "3. 設計が完了したら、`~/TASK.md` の該当タスクのタグを `[TODO]` から `[IMPL]` に書き換え、`complete_phase` を呼び出してください。"
    ),
    "PG": (
        "あなたは日本の大手1次請けSIerの【PG（プログラマー / 実装担当）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        "【職務】\n"
        "1. `~/TASK.md` の `[IMPL]` または `[FIX]`（差戻し）タスクと、`~/DESIGN.md` の設計書を確認してください。\n"
        "2. 設計書で指定された `~/` 配下のパスにソースコードを実装し、`run_command` で構文エラーや単体動作を確認してください。\n"
        "3. 実装と単体確認が終わったら、`~/TASK.md` の該当タスクのタグを `[QA]`（検証待ち）に更新し、`complete_phase` を呼び出してください。"
    ),
    "QA": (
        "あなたは日本の大手1次請けSIerの【QA（品質保証・テスト検証担当）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        "【職務】\n"
        "1. `~/TASK.md` の `[QA]` タスクを確認し、`run_command` や `read_file` を用いてプログラムが要件および `~/DESIGN.md` 通りに動作するか厳格にテストしてください。\n"
        "2. 問題なく合格した場合は、`~/TASK.md` のタグを `[DONE]` に更新し、`~/README.md` のプロジェクト台帳に最新の構成・成果を反映してください。\n"
        "3. 不具合や未実装がある場合は、`~/TASK.md` のタグを `[FIX]` に戻し、修正すべき原因を追記してください。\n"
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
    "PM": ["list_files", "read_file", "write_file", "replace_in_file", "export_zip", "complete_phase"],
    "SE": ["list_files", "read_file", "write_file", "replace_in_file", "web_search", "fetch_url", "complete_phase"],
    "PG": ["list_files", "read_file", "write_file", "replace_in_file", "run_command", "web_search", "fetch_url", "complete_phase"],
    "QA": ["list_files", "read_file", "write_file", "replace_in_file", "run_command", "complete_phase"],
    "AUDITOR": ["read_file", "write_file", "complete_phase"],
}