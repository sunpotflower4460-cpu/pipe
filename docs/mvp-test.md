# MVP確認手順（Claude実読 + revoke）

この手順は、MVPが **AIレビュー用の中継アプリとして実戦投入できるか** を
ローカル + 一時トンネルで確認するための受け入れテストです。

- 初期運用は「ローカル起動 + 一時トンネル（ngrok等）」に固定する
- 常設サーバー化はこのIssueでは行いません
- 常設サーバー化（VPS常設・Webhook常時受信・永続URL公開）は Post-MVP で検討します
- README.md が仕様の正です
- レビュー終了後は必ず `/revoke` で token を失効します

## 0. 事前準備

### 依存インストール

```bash
pip install -r requirements.txt
```

### サンプルZIPを作る

`sample.zip` が手元にない場合は、以下で再現用ZIPを作れます。

```bash
mkdir -p /tmp/pipe-mvp-sample/src
python - <<'PY'
from pathlib import Path

base = Path("/tmp/pipe-mvp-sample")
(base / "README.md").write_text("# sample\n", encoding="utf-8")
(base / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
(base / "large.cpp").write_text(
    "".join(f"int line_{i} = {i};\n" for i in range(1, 1601)),
    encoding="utf-8",
)
PY
cd /tmp/pipe-mvp-sample && zip -r /tmp/pipe-mvp-sample.zip .
```

以降の例では `/tmp/pipe-mvp-sample.zip` を使います。既存の `sample.zip` を使う場合は
適宜読み替えてください。

## 1. サーバー起動

```bash
uvicorn app.main:app --reload --port 8000
```

## 2. health確認

```bash
curl -i http://localhost:8000/health
```

確認すること:

- `200 OK`
- `content-type: text/plain; charset=utf-8`
- 本文が `ok`

## 2.1 FastAPIデフォルトJSONエラーの確認（最重要）

```bash
curl -i http://localhost:8000/not-found
curl -i "http://localhost:8000/t/not-a-real-token/file"
curl -i "http://localhost:8000/t/not-a-real-token/file?path=README.md&from=abc&to=def"
```

確認すること:

- JSON（`{"detail": ...}`）ではない
- `content-type: text/plain; charset=utf-8`
- 本文が `ERROR: ...` の plain text
  - 例: `ERROR: not found` / `ERROR: invalid request`

## 3. ZIP取り込み

```bash
curl -i -X POST http://localhost:8000/ingest \
  -F "file=@/tmp/pipe-mvp-sample.zip"
```

確認すること:

- 本文に `TOKEN=...` が返る
- 本文に `INDEX=/t/{token}/index` が返る
- `workspace/{token}/` が作られる
- `workspace/{token}/index.json` が作られる

token を控える:

```bash
TOKEN="ここに返ってきたtokenを入れる"
```

## 4. index確認

```bash
curl -i "http://localhost:8000/t/${TOKEN}/index"
```

確認すること:

- `content-type: text/plain; charset=utf-8`
- plain text でファイル一覧が見える
- `README.md` や `src/main.py` が見える
- 除外対象が出ていない
- `Total files:` と `Total lines:` が出る

## 5. file確認

```bash
curl -i "http://localhost:8000/t/${TOKEN}/file?path=README.md&from=1&to=200"
```

確認すること:

- `content-type: text/plain; charset=utf-8`
- 行番号付きで本文が見える
- `# README.md lines 1-...` が出る
- `from` / `to` が効く

## 6. 長いファイルの分割確認

```bash
curl -i "http://localhost:8000/t/${TOKEN}/file?path=large.cpp&from=1&to=1200"
```

確認すること:

- 1レスポンス最大行数で切られる（MVPでは最大800行）
- 末尾に `--- 続きは from=...&to=... で取得 ---` が出る

続き取得の例:

```bash
curl -i "http://localhost:8000/t/${TOKEN}/file?path=large.cpp&from=801&to=1600"
```

## 7. トンネリング確認

一時公開する:

```bash
ngrok http 8000
```

Claude / ChatGPT 等に渡すURL例:

```text
https://xxxx.ngrok-free.app/t/{token}/index
https://xxxx.ngrok-free.app/t/{token}/file?path=README.md&from=1&to=200
```

確認すること:

- AIが `/index` を読める
- AIが `/file` を読める
- JS実行なしで本文が見える
- AIが読んでいる内容が Code Relay の plain text 本文である

## 7.1 ngrok等の警告ページ確認

ngrok無料プラン等では、初回アクセス時にブラウザ向け警告HTMLページが挟まることがあります。
この場合、AIが Code Relay 本体ではなく警告HTMLを読んでしまいます。

必ず確認すること:

- AIに `/index` URL を渡して、実際にファイル一覧が読めるか
- HTML警告文やブラウザ向けページが返っていないか
- `content-type: text/plain; charset=utf-8` の本文をAIが読めているか
- 必要なら `ngrok-skip-browser-warning` 相当の回避策、別トンネル、別公開手段を検討する
- 回避策が必要だった場合は、このファイルまたは README に結果を追記する

## 8. レビュー終了後の失効確認

```bash
curl -i -X POST "http://localhost:8000/revoke?token=${TOKEN}"
```

確認すること:

- `200 OK`
- 本文が `revoked`

その後:

```bash
curl -i "http://localhost:8000/t/${TOKEN}/index"
```

確認すること:

- `403`
- revoke後は同じtokenで再アクセスできない
- revoke後、`workspace/{token}/` が削除されるか、少なくとも配信経路から到達不能になる

## 9. 無効tokenの確認

```bash
curl -i "http://localhost:8000/t/not-a-real-token/index"
```

確認すること:

- `403`
- `content-type: text/plain; charset=utf-8`
- 無効tokenでも JSON や HTML ではなく plain text エラーになる

## 合格条件

- `/health` が plain text で動く
- ZIP取り込みができる
- `/index` がAIから読める
- `/file` がAIから読める
- 行番号が付いている
- 長いファイルを分割取得できる
- JS実行なしで本文が見える
- ngrok等の警告HTMLではなく、Code Relay 本体が読める
- 無効tokenで `403`
- `/revoke` 後に同じtokenが `403`
- トンネルを閉じれば外部から到達不能になる
