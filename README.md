Code Relay — Claude が読めるコード中継サーバー

起動手順

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

はじめて使う場合は、先に使い方ガイドを参照してください: [docs/how-to-use.md](docs/how-to-use.md)

動作確認（別ターミナルで）:

```bash
# ヘルスチェック
curl -i http://localhost:8000/health
# 期待: 200 / content-type: text/plain; charset=utf-8 / 本文: ok

# 存在しないエンドポイント
curl -i http://localhost:8000/not-found
# 期待: 404 / content-type: text/plain; charset=utf-8 / JSON ではない本文
```

privateリポジトリのソースを公開せずに、Claude（チャット経由のクローラー）が
確実に読める形で中継するための軽量サーバー。

ユーザーは ZIP（またはリポジトリ）を登録するだけ。
Claude は発行された URL を叩くだけで、全ファイルを行番号付きで読める。

このアプリが解決する問題

- 売り物のプラグインDSPソースを GitHub に public で置きたくない。
- でも Claude にコードを見せてレビューを受けたい。
- Claude のチャットにはファイル添付の中身が届かない／巨大ファイルのコピペは非現実的。
- Genspark 等の配信は application/octet-stream で返すため Claude が読めない。

→ 「Claude が確実に読める形式」でソースを中継する専用サーバーを自前で持つ。

READMEを仕様の正とする

今後の実装・修正・レビューでは、この README.md を仕様の正とする。
Issue と README に差分がある場合は README を優先し、Issue 側を更新する。

絶対に守る鉄則（これを破ると全部動かなくなる）

実装・改修の際、以下6点は例外なく守ること。レビューでもここを最初に確認する。

1. Content-Type は必ず text/plain; charset=utf-8
   コードを返す全エンドポイントで固定。application/octet-stream や
   text/html で返した瞬間、Claude は中身を読めなくなる。
   FastAPI では HTTPException をそのまま投げると default が JSON 応答になるため、
   共通エラー関数や例外ハンドラを使い、4xx/5xx も必ず text/plain; charset=utf-8 で返す。

2. 本文はサーバー側で完成させてから返す（SSR / 静的）
   クライアント JS での描画は禁止。読み手は JS を実行しないため、
   SPA にすると Claude には空ページに見える。

3. 認証は URL 埋め込みトークンのみ
   Cookie・ヘッダー・ログインフォームは使わない（Claude が突破できない）。
   秘密保持は「推測不能な URL」で担保する。

4. レスポンスは行範囲で分割可能にする
   Claude のクローラーは大きなレスポンスを途中で切る。
   大きいファイルは from/to で分割取得できなければならない。

5. 1レスポンスの上限を設ける
   例: 最大800行。超過要求は自動で切り、末尾に次の取得範囲を案内する。

6. 秘密情報とバイナリを既定除外する
   少なくとも .git/・node_modules/・build/・dist/・.env・鍵/証明書系・
   フォント/画像/音声/動画/ZIP などの配信対象外を最初から固定する。

MVPで作るもの

- Python + FastAPI + uvicorn の最小サーバー
- GET /health
- ZIPアップロードによる POST /ingest
- 推測不能なURLトークン
- トークンの有効期限
- POST /revoke?token= による失効
- テキスト/バイナリ判定
- workspace/{token}/index.json 生成
- GET /t/{token}/index
- GET /t/{token}/file?path=&from=&to=
- Claude等での実読確認

現時点で作らないもの

- 自動 git pull
- Webhook
- 常設サーバー運用
- ログインフォーム
- Cookie認証
- Header認証
- SPA / リッチフロントエンド

システム構成

3層構成。

- 取り込み層 … ユーザーが ZIP（後にリポジトリ）を登録する受け口。
- 保管層 … 展開してファイルツリーとして保持。パス・行数・サイズをインデックス化。
                バイナリ（フォント・wav・png 等）と秘匿物（.git/.env/鍵）は除外。
- 配信層 … Claude が読みに来るエンドポイント群。すべて text/plain で返す。

エンドポイント仕様

AI向け配信エンドポイント（/t/{token}/... と /health, /revoke）は、
すべて text/plain; charset=utf-8 で返す。

管理画面（人間向けHTML）

- GET / または GET /admin
  - ZIP / リポジトリURL の登録フォーム
  - 保存済みパイプ一覧
  - index / file / symbol / changes のコピー用URL表示
  - revoke / delete 操作
  - ※ read-only token 入力値は保存・表示・ログ出力しない

GET /health
死活確認。ok を返すだけ。

POST /ingest
ZIP を multipart/form-data で受領 → workspace/{token}/ に展開 → トークンを返す。
- パストラバーサル（../ を含むエントリ）は必ず弾く。
- token ごとにディレクトリを隔離する。

POST /ingest-repo
repository_url（必要なら read-only access token）を multipart/form-data で受領し、
workspace/{token}/ に git clone --depth 1 で取り込む。
- clone 後は ZIP 取り込みと同じ index 生成処理を再利用する。
- 認証情報はログや配信対象に残さない（index.json に保存しない）。
- clone 失敗時は `ERROR: failed to clone repository` を返す。

GET /t/{token}/index
全テキストファイルの一覧。1ファイル1行：
Source/PluginProcessor.cpp | 4120 lines | 183KB
末尾に合計ファイル数・合計行数を出す。長い場合は from/to でページング可。

GET /t/{token}/file?path=&from=&to=  ★最重要
指定ファイルを行番号付きで返す。
  200| if (bypassed) {
  201|     resetDspState();
- from/to 未指定時はデフォルト範囲（1〜600行）。
- 1レスポンスの上限行数を固定（例：800行）。超過分は自動で切る。
- 切った場合は末尾に必ず：--- 続きは from=601&to=1200 で取得 ---
- path はワークスペース直下に正規化し、外部参照を拒否。

GET /t/{token}/symbol?path=&name=
関数・クラス単位で該当範囲だけ抽出して返す。
簡易方式（正規表現＋波括弧の対応カウント）で可。返却時に行範囲を明記。

GET /t/{token}/changes?since=
前回以降に更新されたファイル一覧。継続レビュー用。

POST /revoke?token=
トークンを即時失効。レビュー終了後に必ず実行する運用。

セキュリティ

- トークンは secrets.token_urlsafe(32)（256bit相当）。推測不能にする。
- トークンに有効期限を持たせる（既定7日）。期限切れ・無効は 403。
- .git/・build/・dist/・node_modules/・.env・鍵類は配信対象から既定除外。
- バイナリは配信対象外（フォント・音源・画像が漏れない）。
- リポジトリ clone 方式で受け取った認証情報は、配信にもログにも残さない。
- URL が漏れると中身も漏れる。 チャットに貼る相手以外に URL を渡さない。
  レビューが済んだら /revoke でトークンを失効させる。

技術スタック

- Python + FastAPI + uvicorn を推奨（テキスト応答とルーティングが最短・依存が軽い）。
- 重厚なフロントエンドフレームワークは使わない（鉄則2に反するため）。
- サーバーが直接テキストを吐く素朴な構成にする。

ディレクトリ構成（想定）
code-relay/
├─ app/
│  ├─ main.py            # FastAPI エントリ・ルーティング
│  ├─ ingest.py          # ZIP/リポジトリ取り込み・展開・パストラバーサル対策
│  ├─ index.py           # テキスト/バイナリ判定・インデックス生成
│  ├─ serve.py           # /index, /file, /symbol, /changes
│  ├─ tokens.py          # トークン生成・検証・失効・有効期限
│  ├─ responses.py       # text/plain レスポンスヘルパー（plain_text / error）
│  └─ config.py          # 除外パターン・上限行数などの定数
├─ workspace/            # token ごとに展開（git管理外）
├─ tokens.json           # 発行トークン（git管理外）
├─ requirements.txt
└─ README.md
実装順（MVP → 継続運用）

MVP（完了）:
1. 最小サーバー（/health が text/plain を返す）
2. ZIP 取り込み（POST /ingest・パストラバーサル対策）
3. トークン生成・検証・失効・有効期限
4. テキスト/バイナリ判定・インデックス生成
5. GET /t/{token}/index
6. GET /t/{token}/file（行範囲・行番号・上限・続き案内）★最重要
7. 受け入れテスト（Claude 実読確認）

継続運用（実装済み）:
8. GET /symbol（関数単位抽出）
9. リポジトリ clone 取り込み（POST /ingest-repo）
10. GET /changes（変更差分取得）

未着手（Post-MVP）:
- 更新自動反映（git pull 連携）
- 常設サーバー化

受け入れ条件（完成判定）

- curl -i .../health のヘッダーに content-type: text/plain; charset=utf-8。
- ZIP を POST するとトークンが返り、workspace/{token}/ に展開される。
- 正しいトークンで 200、無効・期限切れで 403。
- インデックスにフォント・wav が現れず、.cpp/.h/.md 等が行数付きで現れる。
- Claude が /index を読めて全体像を把握できる。
- Claude が /file?path=...&from=1&to=600 を読めて行番号付きコードが見える。

最後の2つが本番の合格ライン。ここを通れば実戦投入可能。

デプロイ形態

- ローカル＋トンネリング（推奨・初期）:
  自分のマシンで起動し、ngrok 等で一時 URL を発行 → チャットに貼る。
  レビュー後はトンネルを閉じれば外部到達不能。秘密保持の観点で最も安全。

- 常設サーバー（継続運用時）:
  VPS 等に常駐し git pull 自動反映まで含める。/changes による継続レビュー向き。
  常時口が開くため、トークン運用・有効期限・アクセスログを厳格に。

クラウド常設運用の設定（`PORT`、永続ディスク、`WORKSPACE_DIR`/`TOKENS_PATH`/`BASE_PUBLIC_URL`）は
`docs/cloud-deploy.md` を参照。

推奨フロー：まずローカル＋トンネリングで MVP を完成させ、Claude が実際に読めることを
確認してから、必要なら常設へ移す。

レビュー運用メモ（Claude に渡すとき）

1. MVP 完成後、/index の URL をチャットに貼る。
2. Claude が一覧から「次はこのファイルを from=X&to=Y で」と指定する。
3. その URL を貼る。Claude が行番号付きで読み、指摘する。
4. レビュー終了後、/revoke でトークンを失効させる。

READMEは以上です。リポジトリ直下に README.md として置けば、エージェントが作業のたびに参照でき、鉄則・仕様・受け入れ条件がぶれにくくなります。特に冒頭の「絶対に守る鉄則」6点と、末尾の「受け入れ条件」が、実装がずれたときに立ち返る基準になります。
