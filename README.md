# LLM-Agent-Discord

## 1. プロジェクト概要
- ローカルLLM（Ollama）をバックエンドとし、**日本の1次請けSIerの開発体制（PM / SE / PG / QA / AUDITOR）**を再現した自律型マルチエージェントシステムを Discord Bot として動作させる。
- データベース（SQLite）を使用せず、ワークスペース（エージェント視点では `~/`）直下の4つの標準ドキュメント（`README.md`, `TASK.md`, `DESIGN.md`, `REPORT.md`）を単一の真実のソースとして工程分業で開発を継続する。
- 隔離された Docker サンドボックス環境内で、要件定義・設計・実装・結合テスト・内部監査を自律的に遂行する。
- ワークスペース全体を **NAS（Web UI / WebDAV / Samba）** として公開し、PC・スマートフォンから直接ファイルの閲覧・編集・取得が可能。

---

## 2. 主な機能

### 自律型 SIer 5役職パイプライン
- **PM（プロジェクトマネージャー / 窓口）**: `.env` で指定されたテキストチャンネル・フォーラムチャンネル（スレッド含む）のメッセージに**メンション不要で自動反応**し、要件を整理して `~/TASK.md` へ起票・ZIP出力・顧客応答を行う（過去のチャット履歴による汚染を防ぐため、常に最新のドキュメント状態と今回の発言のみを参照）。
- **SE（システムエンジニア / 設計）**: `[TODO]` タスクを調査し、具体的なディレクトリ構成やモジュール仕様を `~/DESIGN.md` に設計して `[IMPL]` へ遷移させる。
- **PG（プログラマー / 実装）**: `~/DESIGN.md` に基づき `[IMPL]` / `[FIX]` タスクのコーディングと単体動作確認を行い、`[QA]` へ遷移させる。
- **QA（品質保証 / テスト）**: `[QA]` タスクを Docker サンドボックス内で厳格にテストし、合格なら `[DONE]` として `~/README.md` を更新、不合格なら原因を添えて `[FIX]` へ差戻す。
- **AUDITOR（PMO / 内部監査）**: 各部署のログ（`logs/*.log`）を定期監視し、エラー傾向や差戻しループを分析して `~/REPORT.md` に改善レポートを出力する。

### リアルタイム可視化モニター（稼働中のみ表示）
- **稼働状況モニターBox**: 指定チャンネルに固定サイズ（ANSIカラー対応）のダッシュボードを表示し、数分おきに自動更新する（Bot停止時に自動削除）。
  - Ollama サーバーの VRAM / RAM 消費量・空き容量、実行ホスト（Raspberry Pi 等）の空きメモリ
  - 各役職ごとのメモリ消費割合・コンテキスト長占有率
  - 過去・現在・未来のタスクパイプラインと、進行中タスクの実行ブランチツリー
- **リアルタイム生ログBox (`log-raw`)**: モデルの思考プロセス、実行ツール引数、コマンドの生出力をステップごとにリアルタイムで固定枠内に表示する。

### 統合ワークスペース NAS
- `workspace/` を以下の3プロトコルで共有可能（Cloudflare Tunnel 等を介した外部公開にも対応）。
  - **Web UI**: ブラウザからファイル閲覧・コード編集・アップロード・ZIPダウンロードが可能。
  - **WebDAV (`/dav/`)**: Windows / iOS / Android のファイルアプリからネットワークドライブとしてマウント可能。
  - **Samba (SMB)**: `.env` の `SAMBA_ENABLE=True` により、ローカルLAN向けに `\\<ホストIP>\workspace` でファイル共有が可能。

---

## 3. ディレクトリ構成
```text
.
├── .env.example             # 環境変数テンプレート
├── src/
│   ├── agents/              # 5役職エージェント（PM, SE, PG, QA, AUDITOR）とパイプライン制御
│   ├── core/                # 設定管理, Ollama通信, 構造化ロガー, テレメトリ, NASサーバー
│   ├── discord_app/         # Discord Bot本体, ライブステータスBox監視
│   └── sandbox/             # Dockerサンドボックス管理, ツール実行, パス正規化
├── workspace/               # エージェントの作業領域（コンテナ内では ~/ にマウント）
├── exports/                 # 生成されたZIPアーカイブ出力先
├── logs/                    # 役職別の稼働ログ出力先（pm.log, se.log, pg.log, qa.log, auditor.log）
└── main.py                  # エントリーポイント
```

---

## 4. 使用技術
- **言語**: Python 3.12+
- **フレームワーク・ライブラリ**: `discord.py`, `aiohttp`, `python-dotenv`, `ddgs`
- **インフラ・環境**:
  - Docker（コード実行サンドボックス ＆ Alpineベース Samba コンテナ）
  - 組み込み WebUI / WebDAV サーバー（`aiohttp`）
- **LLMバックエンド**: Ollama（OpenAI互換 `/v1/chat/completions` および `/api/ps` リソース監視）
- **外部API**: DuckDuckGo（Web検索・Webページ取得）

---

## 5. セットアップ手順

1. **Docker のインストールと権限設定**
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   ```
   *(反映のため一度再ログインしてください)*

2. **サンドボックス用 Docker イメージの取得**
   ```bash
   docker pull python:3.12-slim
   # Samba機能（SAMBA_ENABLE=True）を使用する場合
   docker pull alpine:latest
   ```

3. **Python 仮想環境の構築と依存ライブラリのインストール**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. **環境変数（`.env`）の設定**
   `.env.example` をコピーして `.env` を作成し、ご自身の環境に合わせて設定してください（モデル名や認証情報はすべて `.env` で管理されます）。
   ```bash
   cp .env.example .env
   ```
   主な設定項目：
   - `DISCORD_TOKEN` : Discord Bot トークン
   - `DISCORD_CHANNEL_IDS` : 自動反応するテキスト／フォーラムチャンネルID（カンマ区切り）
   - `DISCORD_STATUS_CHANNEL_ID` / `DISCORD_RAW_LOG_CHANNEL_ID` : 監視Box表示用チャンネルID
   - `OLLAMA_BASE_URL` : Ollama サーバーのURL（例: `http://192.168.x.x:11434`）
   - `MODEL_MAIN` / `MODEL_PM` / `MODEL_SE` / `MODEL_PG` / `MODEL_QA` / `MODEL_AUDITOR` : 使用する Ollama モデル名
   - `NAS_ENABLE`, `NAS_PORT`, `NAS_USER`, `NAS_PASSWORD`, `SAMBA_ENABLE` : NAS・Samba 設定

5. **起動**
   ```bash
   PYTHONPATH=src python src/main.py
   ```
   *(または実行スクリプト `./run` に権限を付与して `tmux` 上で起動)*