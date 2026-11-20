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
                    "max_lines": {"type": "integer", "description": "Max lines to read (default 2000)"},
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
                    "max_lines_per_file": {"type": "integer", "description": "Max lines per file (default 1000)."},
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
    "invoke_heavy_brain": {
        "type": "function",
        "function": {
            "name": "invoke_heavy_brain",
            "description": (
                "【SE/PG専用・メイン執筆ツール】指定した context_paths の既存ファイルをPython側で自動収集し、"
                "30GBクラスの重量級モデル (MODEL_HEAVY) に設計書や生コード全文を一括執筆させ、"
                "出力された全ファイル ('### FILE: ~/path' ブロック) をPythonが0秒でワークスペースへ自動保存します。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "重量モデルに参照させる既存ファイルやディレクトリのパス一覧 (例: ['~/src', '~/DESIGN.md'])",
                    },
                    "target_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "重量モデルに新規作成または全面更新させる対象ファイルパス一覧 (例: ['~/app/main.py'])",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "重量モデルに対する具体的な設計・実装指示（要件、仕様、修正ポイントなど）",
                    },
                },
                "required": ["target_files", "instruction"],
            },
        },
    },
}


def get_tools_for_role(tool_names: list[str]) -> list[dict[str, Any]]:
    return [TOOL_SCHEMAS[name] for name in tool_names if name in TOOL_SCHEMAS]


BATCH_EFFICIENCY_NOTE = (
    "【大容量コンテキスト活用・一括処理＆並列安全ルール】\n"
    "- 推論回数を最小化するため、ファイルを1つずつ確認せず `read_files`（複数パスやディレクトリ指定）や `search_files` で一括閲覧してください。\n"
    "- ファイル閲覧時は左側に1始まりの行番号（`   1 | ...`）が表示されます。部分編集を行う際は `edit_lines` を使い、`start_line` と `end_line` を指定して複数箇所・複数ファイルを1回で一括置換してください。\n"
    "- 複数ファイルの新規作成には `write_files` を使用して1ステップで完了させてください。\n"
    "- PMと他部門は並列稼働するため、`~/TASK.md` を更新する際は他部門の追記を消さないよう `write_file` で全文上書きせず、直前に `read_file` で行番号を確認して `edit_lines` または `replace_in_file` で該当箇所のみ部分更新してください。"
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
        "あなたは日本の大手1次請けSIerの【SE（オペレーター / 斥候＆進行管理）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        "【3ステージ実行ルール（厳守）】\n"
        "1. [Stage 1: 斥候] `list_files` や `search_files` で最小限のステップでディレクトリ構造を把握してください（重いファイルを1つずつ `read_file` する必要はありません）。\n"
        "2. [Stage 2: 重量級設計] あなた自身が `write_file` で設計書を書いてはいけません。必ず `invoke_heavy_brain` を呼び出し、`context_paths`（参照すべき既存パス）と `target_files=['~/DESIGN.md']` を指定して、重量級モデルに `~/DESIGN.md` を一発執筆＆自動配置させてください。\n"
        "3. [Stage 3: ステータス更新] 自動配置された `~/DESIGN.md` を確認後、`edit_lines` 等で `~/TASK.md` の該当タスクを `[TODO]` から `[IMPL]` に書き換え、`complete_phase` を呼び出してください。"
    ),
    "PG": (
        "あなたは日本の大手1次請けSIerの【PG（オペレーター / 配置・テスト検証担当）】です。\n"
        "作業空間のルートは常に `~/` です。\n"
        "【3ステージ実行ルール（厳守）】\n"
        "1. [Stage 1: 斥候] `~/TASK.md` の `[IMPL]` / `[FIX]` タスクと `~/DESIGN.md` を確認し、必要なら `list_files` で構成を確認してください。\n"
        "2. [Stage 2: 重量級コーディング] あなた自身がゼロから長いコードを書いてはいけません。必ず `invoke_heavy_brain` を呼び出し、`context_paths` と作成・改修すべき `target_files` を指定して、重量級モデルに生コード全文を執筆＆自動配置させてください。\n"
        "3. [Stage 3: テスト＆完了] 自動配置完了後、`run_command` でテスト・動作確認を行ってください。軽微な修正は `edit_lines` で直し、合格したら `~/TASK.md` のタグを `[QA]` に更新して `complete_phase` を呼び出してください。"
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
        "invoke_heavy_brain", "write_file", "edit_lines", "replace_in_file",
        "run_command", "web_search", "fetch_url", "complete_phase",
    ],
    "PG": [
        "list_files", "read_file", "read_files", "search_files",
        "invoke_heavy_brain", "edit_lines", "replace_in_file", "write_file",
        "run_command", "web_search", "fetch_url", "complete_phase",
    ],
    "QA": [
        "list_files", "read_file", "read_files", "search_files",
        "write_file", "write_files", "edit_lines", "replace_in_file",
        "run_command", "complete_phase",
    ],
    "AUDITOR": ["read_file", "read_files", "write_file", "edit_lines", "complete_phase"],
}


HEAVY_BRAIN_PROMPTS: dict[str, str] = {
    "SE": (
        "あなたは日本の大手1次請けSIerの【チーフアーキテクト (Heavy Brain)】です。\n"
        "あなたにはツールは与えられていません。提供されたコンテキストと要件から、完璧で具体的な基本設計・詳細設計を執筆してください。\n"
        "出力するファイルは必ず以下の厳密なMarkdownフォーマットで記述してください（Pythonパーサーが自動的にファイルへ書き出します）：\n\n"
        "### FILE: ~/DESIGN.md\n"
        "```markdown\n"
        "（ここに省略なしの完全な設計書全文）\n"
        "```\n"
    ),
    "PG": (
        "あなたは日本の大手1次請けSIerの【リードプログラマー (Heavy Brain)】です。\n"
        "あなたにはツールは与えられていません。コマンド実行や探索は一切不要です。\n"
        "提供された設計書・既存コード・要件に基づき、製品レベルの完全な生コード（省略やTODOコメント厳禁）のみを執筆してください。\n"
        "作成・更新するすべてのファイルは、必ず以下の厳密なフォーマットで出力してください（Pythonパーサーが自動的に各パスへ配置します）：\n\n"
        "### FILE: ~/path/to/filename.ext\n"
        "```language\n"
        "（ここに省略なしの完全なソースコード全文）\n"
        "```\n"
    ),
}

AUTONOMOUS_INITIATIVE_PROMPT = (
    "現在 `~/TASK.md` に未完了タスク（TODO / IMPL / FIX / QA）が存在せず、システムがアイドル状態です。\n"
    "自律行動モードとして、ワークスペース (`~/`) 全体を調査し、以下の1〜3を順番に実行してください：\n\n"
    "1. 【ファイル整理整頓・クリーニング】\n"
    "   - `list_files` で `~/` 配下を確認し、散らかった一時ファイル・不要なログ・重複ファイル・未整理のディレクトリがあれば、`run_command`（`mv`, `rm`, `mkdir` 等）で美しく整理整頓してください。\n"
    "   - `~/README.md`（プロジェクト台帳）が現在の実際のディレクトリ構成・ファイル一覧と一致するように最新化してください。\n"
    "   - `~/TASK.md` に完了済みの `[DONE]` タスクが大量に溜まっている場合は、直近の履歴を残してコンパクトに整理してください。\n\n"
    "2. 【品質点検・改善タスクの自己起票】\n"
    "   - `~/REPORT.md` の監査指摘や既存プロジェクトのコード・テスト状況を確認してください。\n"
    "   - リファクタリング、単体テスト・自動検証スクリプトの拡充、エラーハンドリング強化、ドキュメント整備、または機能改善など、"
    "次に開発ラインが取り組むべき具体的で価値の高いタスクを1件考案し、`~/TASK.md` に `- [TODO] ...` として起票（追記）してください。\n\n"
    "3. 【完了報告】\n"
    "   - 実施した整理整頓の内容と、新たに起票した `[TODO]` タスクの概要を `complete_phase` で報告してください。"
)