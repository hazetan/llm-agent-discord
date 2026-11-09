from typing import Any

AGENT_SYSTEM_PROMPT = (
    "You are an elite autonomous engineering agent operating inside an isolated Docker sandbox.\n"
    "Environment constraints:\n"
    "- All command executions strictly run in Docker container (/workspace).\n"
    "- Before starting complex operations, use 'record_plan' to document issue and tasks.\n"
    "- Use 'list_files' to inspect directory and 'read_file' to view code.\n"
    "- For modifying existing files, PREFER 'replace_in_file' for small edits or 'write_file' for whole files.\n"
    "- Keep code modular. If implementing complex applications (like games or web apps), divide logic into separate files (e.g. state.js, pieces.js, ui.js) instead of dumping thousands of lines into a single call.\n"
    "- Ensure that JSON payloads and string contents are completed and not abruptly cut off.\n"
    "- When developing large single-page applications or complex HTML/JS:\n"
    "  1. Create a minimal skeleton first via 'write_file' (e.g. basic HTML structure with placeholders or comments like '<!-- JS_LOGIC -->' or '</body>').\n"
    "  2. Use 'insert_into_file' to incrementally inject functions, event listeners, and data models in small chunks instead of overwriting the whole file.\n"
    "  3. This avoids output token truncation and keeps tool calls concise.\n"
    "- Use 'git_diff' to inspect code changes and 'git_revert' if an action broke the project.\n"
    "- When using 'web_search', keep queries concise and limited to 2-3 key terms.\n"
    "- If you lack domain info or need latest docs, invoke 'web_search' or 'fetch_url'.\n"
    "- If requirements or credentials are truly missing, ask human directly via 'ask_human'.\n"
    "- Test code syntax and behavior via 'run_command'.\n"
    "- After resolving an unexpected error or roadblock, save the lesson via 'record_troubleshooting'.\n"
    "- Conclude sub-steps using 'update_plan_result'.\n"
    "- Once fully verified and satisfied, call 'finish_task'. The 'result' parameter MUST be written in polite, clear Japanese detailing accomplishments and key files.\n"
    "- IMPORTANT: Invoke tools directly. Do NOT output markdown code blocks in text."
)

AVAILABLE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command inside the isolated Docker sandbox workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command string to execute (e.g. 'python app.py', 'pip install requests').",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read content from a file inside the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Relative path to the file.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Max lines to read (default 150).",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with full text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Relative path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Raw text content.",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Precisely replace specific text snippet inside an existing file without rewriting whole content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Relative path to the file.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text snippet to be replaced.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "New replacement text.",
                    },
                },
                "required": ["filename", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_into_file",
            "description": "Insert a code snippet before or after a specific target marker in an existing file. Perfect for adding <script> tags or JavaScript functions without rewriting the entire file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Relative path to the target file (e.g. 'index.html').",
                    },
                    "target_marker": {
                        "type": "string",
                        "description": "The exact unique string to locate the insertion point (e.g. '</body>', '</script>', or a comment '// INSERT_HERE').",
                    },
                    "content": {
                        "type": "string",
                        "description": "The code snippet or text content to insert.",
                    },
                    "position": {
                        "type": "string",
                        "enum": ["before", "after"],
                        "description": "Whether to insert 'before' or 'after' the target_marker (default is 'before').",
                    },
                },
                "required": ["filename", "target_marker", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and subdirectories inside the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Relative directory path (default is root '.').",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_troubleshooting",
            "description": "Save a troubleshooting memo to the database after resolving an unexpected error or roadblock. Future tasks will reference this knowledge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Short identifier (e.g. 'docker_env', 'port_conflict', 'syntax_error').",
                    },
                    "error_pattern": {
                        "type": "string",
                        "description": "The error text or symptoms faced.",
                    },
                    "root_cause": {
                        "type": "string",
                        "description": "Explanation of why the error occurred.",
                    },
                    "solution": {
                        "type": "string",
                        "description": "How to resolve or avoid this error.",
                    },
                },
                "required": ["category", "error_pattern", "root_cause", "solution"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Inspect git diff to see recent code modifications since last checkpoint.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_revert",
            "description": "Revert workspace to the previous git commit checkpoint if recent changes caused unexpected failure.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for up-to-date documentation, solutions to unfamiliar errors, or libraries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query terms.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch text content from a web URL for documentation or reference.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full HTTP/HTTPS URL.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_human",
            "description": "Ask the human user for clarification, required API keys, or design choices when requirements are ambiguous.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Clear and polite question in Japanese addressed to the user.",
                    }
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_plan",
            "description": "Record the current issue, planned task, and anticipated future steps into the persistent Planner DB before executing complex operations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_issue": {
                        "type": "string",
                        "description": "What problem are we solving right now?",
                    },
                    "task": {
                        "type": "string",
                        "description": "Specific action plan to execute.",
                    },
                    "future_issue": {
                        "type": "string",
                        "description": "Next challenges or backlog to tackle later.",
                    },
                },
                "required": ["current_issue", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_result",
            "description": "Update the latest plan record with actual results and refined future issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "Outcome of the executed task.",
                    },
                    "future_issue": {
                        "type": "string",
                        "description": "Updated next issues based on what was learned.",
                    },
                },
                "required": ["result"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "Declare the task finished. The 'result' argument MUST be written in Japanese summarizing accomplishments and deliverables.",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "Final summary and explanation written in Japanese.",
                    }
                },
                "required": ["result"],
            },
        },
    },
]