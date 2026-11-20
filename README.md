# LLM-Agent-Discord

## 1. プロジェクト概要
- ローカルLLM（Ollama）をバックエンドとし、自律型マルチエージェントシステムを Discord Bot として動作させる。
- **軽量モデル（VRAM完全収容）× 重量級モデル（RAMオフロード併用・30GBクラス）の3ステージ・ハイブリッド推論基盤**を採用。ファイル探索・コマンド実行・進行管理は高速な軽量モデル（例: 12B）が常駐処理し、高度な設計や大規模コーディング時のみ重量級モデル（例: 31B）をオンデマンド召喚して生コードを一発執筆させた後、即座にメモリ解放（`HEAVY_KEEP_ALIVE=0`）する。
- **アイドル時の自律行動モード（Autonomous Initiative）**を搭載。未完了タスクが0件になると、AIが自らワークスペースのファイル整理整頓・不要ファイル削除・台帳更新を行い、さらにリファクタリングや機能改善の新規 `[TODO]` タスクを自己起票して開発サイクルを自律継続する。
- データベース（SQLite）を使用せず、ワークスペース（エージェント視点では `~/`）直下の4つの標準ドキュメント（`README.md`, `TASK.md`, `DESIGN.md`, `REPORT.md`）を単一の真実のソース（Single Source of Truth）として工程分業で開発を継続する。
- 隔離された Docker サンドボックス環境内で、要件定義・設計・実装・結合テスト・内部監査を自律的に遂行する。`Dockerfile.sandbox` の拡張により Python / C / C++ / MicroPython / OpenSCAD（3Dモデル `.stl` 生成）等にも柔軟に対応可能。
- ワークスペース全体を **NAS（Web UI / WebDAV / Samba）** として公開し、PC・スマートフォンから直接ファイルの閲覧・編集・取得が可能。

---

## 2. 主な機能

### 3ステージ・ハイブリッドLLMパイプライン（SE / PG）
VRAM容量の制約と成果物の最高品質を両立するため、設計・実装工程を以下の3段階に分離：
1. **Stage 1: Scout（斥候・軽量モデル）**: VRAM内に常駐する高速モデル（`MODEL_SE` / `MODEL_PG`）が `list_files` や `search_files` でディレクトリ構造と対象ファイルパスを高速に特定し、`invoke_heavy_brain` を呼び出す。
2. **Stage 2: Pure Brain（一発執筆・重量級モデル）**: Python側が対象ファイルを一括バンドルし、30GBクラスの重量級モデル（`MODEL_HEAVY`）を**ツール呼び出し無し（純粋なMarkdown生コード出力）**かつ全並列枠の排他ロック下で1回だけ呼び出す。生成完了直後に `HEAVY_KEEP_ALIVE=0` でVRAM/RAMからアンロードする。
3. **Stage 3: Auto-Apply & Verify（Python自動配置＋軽量モデル検証）**: 重量級モデルが出力した `### FILE: ~/path/to/file` ブロックをPythonパーサーが0秒で各ディレクトリへ自動保存し、再び軽量モデルに切り替わって `run_command` によるテスト実行と `~/TASK.md` のステータス更新を行う。

### 自律型 SIer 5役職パイプライン
- **PM（プロジェクトマネージャー / 窓口）**: `.env` で指定されたテキストチャンネル・フォーラムチャンネル（スレッド含む）のメッセージに**メンション不要で自動反応**し、要件を整理して `~/TASK.md` へ起票・ZIP出力・顧客応答を行う（過去のチャット履歴による汚染を防ぐため、常に最新のドキュメント状態と今回の発言のみを参照）。
- **SE（システムエンジニア / 設計＆自律メンテナンス）**: `[TODO]` タスクを調査し、`invoke_heavy_brain` を用いて具体的なディレクトリ構成やモジュール仕様を `~/DESIGN.md` に設計して `[IMPL]` へ遷移させる。
- **PG（プログラマー / 実装）**: `~/DESIGN.md` に基づき、`invoke_heavy_brain` による生コード一括生成・自動配置と Docker サンドボックス内での単体動作確認を行い、`[QA]` へ遷移させる。
- **QA（品質保証 / テスト）**: `[QA]` タスクを Docker サンドボックス内で厳格にテストし、合格なら `[DONE]` として `~/README.md` を更新、不合格なら原因を添えて `[FIX]` へ差戻す。
- **AUDITOR（PMO / 内部監査）**: 各部署のログ（`logs/*.log`）を定期監視し、エラー傾向や差戻しループを分析して `~/REPORT.md` に改善レポートを出力する。

### アイドル時の自律行動モード（整理整頓＆自己タスク起票）
- `~/TASK.md` に未完了タスク（`TODO` / `IMPL` / `FIX` / `QA`）が存在しない状態が `AUTONOMOUS_IDLE_SEC` 秒続いた場合（かつPMが顧客対応中でない場合）、自律行動フェーズが自動起動する。
- 軽量モデルがワークスペース全体を点検し、散らかった一時ファイルや不要ログの削除・ディレクトリ整理、`~/README.md`（プロジェクト台帳）の最新化、完了済み `[DONE]` タスクのアーカイブ整理を実行する。
- 続けて、既存プロジェクトのテスト拡充・リファクタリング・監査指摘の解消・機能改善タスクを自ら考案し、`~/TASK.md` に新規 `[TODO]` として1件起票することで開発パイプラインを自律的に再始動させる。

### リアルタイム可視化モニター（稼働中のみ表示）
- **稼働状況モニターBox**: 指定チャンネルに固定サイズ（ANSIカラー対応）のダッシュボードを表示し、数秒おきに自動更新する（Bot停止時に自動削除）。
  - GPUホスト（Ollamaサーバー）のリアルな VRAM / RAM 消費量・空き容量（`tools/desktop_metrics_server.py` 連携、未設定時は `/api/ps` へフォールバック）および Bot ホストの空きメモリ
  - 各役職ごとの現在稼働モデル（Light / Heavy）・メモリ消費割合・推論速度（`tok/s`）・コンテキスト長占有率
  - 過去・現在・未来のタスクパイプラインと、進行中タスクの実行ブランチツリー
- **リアルタイム生ログBox (`log-raw`)**: モデルの Thinking（思考プロセス）、実行ツール引数、コマンドや重量級モデルの生出力をステップごとにリアルタイムで固定枠内に表示する。

### 統合ワークスペース NAS
- `workspace/` を以下の3プロトコルで共有可能（Cloudflare Tunnel 等を介した外部公開にも対応）。
  - **Web UI**: ブラウザからファイル閲覧・コード編集・アップロード・ZIPダウンロードが可能。
  - **WebDAV (`/dav/`)**: Windows / iOS / Android のファイルアプリからネットワークドライブとしてマウント可能。
  - **Samba (SMB)**: `.env` の `SAMBA_ENABLE=True` により、ローカルLAN向けに `\\<ホストIP>\workspace` でファイル共有が可能。

---

## 3. ディレクトリ構成
```text
.
├── .env.example                 # 環境変数テンプレート（全パラメータ集約・コード内デフォルト値なし）
├── Dockerfile.sandbox.example   # サンドボックス用Dockerfileテンプレート（MicroPython / 3D CAD対応可）
├── requirements.txt             # Python依存パッケージ一覧
├── setup.sh                     # 初回セットアップスクリプト
├── run.sh                       # Bot起動スクリプト
├── src/
│   ├── main.py                  # エントリーポイント
│   ├── agents/                  # 5役職エージェント、3ステージ実行ランナー、自律パイプライン制御
│   ├── core/                    # 設定管理, Ollama通信(Light/Heavy切替), 構造化ロガー, テレメトリ, NASサーバー
│   ├── discord_app/             # Discord Bot本体, ライブステータスBox監視
│   └── sandbox/                 # Dockerサンドボックス管理, ツール実行, 生コード自動配置パーサー
├── tools/
│   ├── desktop_metrics_server.py # GPU/LLMサーバー側の実VRAM/RAM取得用軽量HTTPサーバー
│   └── start_metrics.sh         # メトリクスサーバー起動スクリプト
├── workspace/                   # エージェントの作業領域（コンテナ内では ~/ にマウント）
├── exports/                     # 生成されたZIPアーカイブ出力先
└── logs/                        # 役職別の稼働ログ出力先（pm.log, se.log, pg.log, qa.log, auditor.log）
```

---

## 4. 使用技術
- **言語**: Python 3.12+
- **フレームワーク・ライブラリ**: `discord.py`, `aiohttp`, `python-dotenv`, `ddgs`
- **インフラ・環境**:
  - Docker（コード実行サンドボックス ＆ Alpineベース Samba コンテナ）
  - 組み込み WebUI / WebDAV サーバー（`aiohttp`）
- **LLMバックエンド**: Ollama ネイティブ `/api/chat`（Thinking モード・Light/Heavy 動的モデル切替・`keep_alive` 制御）および `/api/ps`
- **外部API**: DuckDuckGo（Web検索・Webページ取得）

---

## 5. セットアップ手順

1. **Docker のインストールと権限設定**
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   ```
   *(反映のため一度再ログインしてください)*

2. **セットアップスクリプトの実行**
   仮想環境の作成、依存ライブラリのインストール、`.env` および `Dockerfile.sandbox` の雛形生成、Dockerイメージのビルドを一括で行います。
   ```bash
   chmod +x setup.sh run.sh tools/start_metrics.sh
   ./setup.sh
   ```

3. **環境変数（`.env`）の設定**
   本システムはコード内にハードコードされたデフォルト値を持たず、すべてのパラメータを `.env` で一元管理します。生成された `.env` を編集し、ご自身の環境に合わせて設定してください。
   主な設定項目：
   - **Discord / 監視設定**:
     - `DISCORD_TOKEN` : Discord Bot トークン
     - `DISCORD_CHANNEL_IDS` : 自動反応するテキスト／フォーラムチャンネルID（カンマ区切り）
     - `DISCORD_STATUS_CHANNEL_ID` / `DISCORD_RAW_LOG_CHANNEL_ID` : 監視Box表示用チャンネルID
   - **Ollama / 軽量モデル（常駐オペレーター）設定**:
     - `OLLAMA_BASE_URL` : Ollama サーバーのURL（例: `http://192.168.x.x:11434`）
     - `MODEL_MAIN` / `MODEL_PM` / `MODEL_SE` / `MODEL_PG` / `MODEL_QA` / `MODEL_AUDITOR` : 常駐させる軽量モデル名（例: `Gemma4-12B-it:Q4_K_M`）
     - `OLLAMA_THINK` / `THINK_PROMPT_BOOST` / `OLLAMA_KEEP_ALIVE` : Thinking モード・常駐時間設定
     - `SERVER_CTX_SIZE` / `MAX_TOKEN` / `CTX_CHAR_BUDGET_LIGHT` : 軽量モデル用コンテキスト長・最大出力トークン・文字数予算
   - **重量級モデル（Stage 2: Pure Brain 生コード・設計書一発執筆用）設定**:
     - `MODEL_HEAVY`（および任意で `MODEL_HEAVY_SE`, `MODEL_HEAVY_PG`）: 設計・生コード生成専用の重量級モデル名（例: `Gemma4-31B-it:Q4_K_M`）
     - `HEAVY_CTX_SIZE` / `HEAVY_MAX_TOKEN` / `CTX_CHAR_BUDGET_HEAVY` : 重量級モデル用コンテキスト長・最大出力トークン・文字数予算
     - `HEAVY_THINK` / `HEAVY_KEEP_ALIVE` : 重量級モデルの Thinking 設定および生成完了後のメモリ保持時間（`0` 指定で生成完了直後にVRAM/RAMから即解放）
   - **自律行動モード（Autonomous Idle-Time Initiative）設定**:
     - `AUTONOMOUS_ENABLE` : 未処理タスク0件時の自律整理整頓・自己タスク起票の有効化（`True` / `False`）
     - `AUTONOMOUS_IDLE_SEC` : 全タスク完了後、自律行動を開始するまでのアイドル待機秒数
     - `AUTONOMOUS_MAX_STEPS` : 自律行動フェーズ1回あたりの最大ステップ数
   - **サンドボックス / NAS / メトリクス設定**:
     - `SANDBOX_ENV_HINT` : サンドボックス内の利用可能ツールをエージェントへ伝えるヒント文
     - `DESKTOP_METRICS_URL` : GPUサーバーの実VRAM/RAMを取得する場合のURL（例: `http://192.168.x.x:11435/metrics`）
     - `NAS_ENABLE`, `NAS_PORT`, `NAS_USER`, `NAS_PASSWORD`, `SAMBA_ENABLE`, `SAMBA_PORT` : NAS・Samba 設定

4. **（任意）GPU / Ollama サーバー側でメトリクスサーバーを起動**
   Bot と別マシンで Ollama を動かしており、正確な VRAM / RAM 実測値をモニターに表示したい場合は、GPU サーバー側で以下を実行します。
   ```bash
   ./tools/start_metrics.sh
   ```

5. **Bot の起動**
   `tmux` 等の上で以下を実行して起動します。
   ```bash
   ./run.sh
   ```