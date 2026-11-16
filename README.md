# LLM-Agent-Discord


## 1. プロジェクト概要
- LLM APIを通したローカルエージェントAIをDiscord Botとして動作させる．
- セッション単位で隔離されたDockerサンドボックス環境内で，コード生成・コマンド実行・自己修正・Git管理を自律的に遂行する．


## 2. 主な機能
※ コマンドに`<>`記号を書く必要はありません．
- `!run <目標>` : 新規セッションを発行し，エージェントの自律タスクをバックグラウンドで開始する．
- `!stop [session_id]` : 指定した実行中または待機中のエージェントタスクを中断する．
- `!queue` : 非同期タスクキューの稼働状況および待機セッション一覧を確認する．
- `!tell <メッセージ>` : 質問待機中（`ask_human`）のエージェントに対して回答を送信する．
- `!inject <メッセージ>` : 実行中のエージェントに対して，人間の判断や追加の指示を次ステップへ割り込みで提供する．
- `!project_status` : 最新セッションのステータス，目標，直近のアクティビティ履歴を確認する．
- `@Botメンション <質問>` : エージェントループを介さず，LLMと単発の質疑応答を行う．


## 3. 使用技術
- **言語** : Python
- **フレームワーク・ライブラリ** : discord.py, SQLAlchemy, aiohttp, python-dotenv, ddgs, Docker SDK for Python
- **インフラ・環境** : Docker（実行環境のサンドボックス化）, SQLite（セッション・ログ・ナレッジの永続化 / WALモード）, Git（自動チェックポイント・ロールバック管理）
- **LLMバックエンド** : llama-server（OpenAI互換推論・Embeddingエンドポイント）, GGUF形式ローカルModel
- **外部API** : DuckDuckGo API（Web検索・スクレイピング）


## 4. セットアップ手順
- Dockerをインストールし，実行ユーザーをdockerグループへ追加
`curl -fsSL https://get.docker.com | sh`
`sudo usermod -aG docker $USER`

- サンドボックス用コンテナイメージをプル
`docker pull python:3.12-slim`

- Python仮想環境を構築
`python3 -m venv .venv`
`source .venv/bin/activate`

- 主要ライブラリをインストール
`pip install -r requirements.txt`

- 環境変数ファイル（`.env`）を作成し，各種パラメータを設定
`DISCORD_TOKEN` / `LLAMA_SERVER_URL` 等を記述

- 実行スクリプトに権限を付与
`chmod u+x run`

- コマンド実行（tmux推奨）
`./run`

