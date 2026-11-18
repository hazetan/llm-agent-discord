from src.core.config import get_discord_token
from src.discord_app.bot import bot
from src.sandbox.container import ensure_sandbox_container
from src.sandbox.tools import init_workspace_files


def main() -> None:
    print("[Init] Preparing ~/ (workspace) standard SIer documents...")
    init_workspace_files(reset_on_restart=False)

    print("[Init] Starting clean Docker sandbox container mounted at ~/ (/root)...")
    ensure_sandbox_container(recreate=True)

    print("[Init] Launching Discord Bot & SIer Multi-Agent Pipeline...")
    bot.run(get_discord_token())


if __name__ == "__main__":
    main()