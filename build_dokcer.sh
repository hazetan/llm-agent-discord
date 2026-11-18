# 1. 専用イメージをビルド
docker build -f Dockerfile.sandbox -t sier-sandbox:latest .

# 2. 動作確認（バージョンが表示されれば成功）
docker run --rm sier-sandbox:latest micropython --version
