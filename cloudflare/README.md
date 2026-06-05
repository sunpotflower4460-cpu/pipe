# Cloudflare Code Memo MVP

既存の FastAPI 版を壊さずに、Cloudflare Workers + R2 + D1 で使える最小クラウド版を `cloudflare/` に分離しています。

## できること

- `/` と `/admin` のスマホ向け管理画面
- ZIP アップロード
- ZIP 展開後のテキストファイルだけを R2 に保存
- D1 にメモ / ファイル一覧 / hidden 状態 / フォルダ情報を保存
- ファイル一覧表示 / 本文表示
- 隠す / 戻す / 削除
- フォルダ作成と選択ファイルのフォルダ分け
- `index URL` / `file URL` / AI テンプレートのコピー
- `GET /t/{token}/index`
- `GET /t/{token}/file?path=&from=&to=`
- `GET /t/{token}/share/folder-{folder_id}/index`
- `GET /t/{token}/share/folder-{folder_id}/file?path=&from=&to=`

## 保存先

- R2: `memos/{token}/files/{path}` と `memos/{token}/raw/{zip}`
- D1: `memos`, `files`, `folders`, `folder_files`

## セキュリティ方針

- `.env`, keys, certificates, `.git/`, `node_modules/`, `build/`, `dist`, ZIP, 画像, 音声などは既定除外
- 管理画面は `ADMIN_KEY` で保護（AI 向け `/t/...` は別 URL）
- AI 向けレスポンスは `text/plain; charset=utf-8`

## 初期セットアップ

```bash
cd cloudflare
npm install
npx wrangler r2 bucket create code-memo-files
npx wrangler d1 create code-memo-db
```

`wrangler d1 create` の結果で出る `database_id` を `wrangler.toml` に反映してください。

```bash
npx wrangler d1 migrations apply CODE_MEMO_DB
npx wrangler secret put ADMIN_KEY
npm run dev
```

本番反映:

```bash
npm run deploy
```
