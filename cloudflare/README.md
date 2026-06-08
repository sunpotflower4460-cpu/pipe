# Cloudflare Code Memo

Cloudflare Workers + R2 + KV で動く Code Memo 版です。

## できること

- ブラウザで Code Memo を開く
- ZIP をアップロードする
- ZIP 内のテキストコードを R2 に保存する
- メモ情報を KV に保存する
- アプリ内でコード一覧と本文を見る
- ファイルを隠す / 戻す / 削除する
- AI に貼る index URL / file URL をコピーする

## 初回セットアップ

```bash
cd cloudflare
npm install
npx wrangler r2 bucket create code-memo-files
npx wrangler kv namespace create CODE_MEMO_KV
```

作成された KV の id を `wrangler.toml` の `id` に入れてください。
preview 用 id も必要なら作成して `preview_id` に入れます。

## 開発

```bash
cd cloudflare
npm run dev
```

## デプロイ

```bash
cd cloudflare
npm run deploy
```

## 開く場所

デプロイ後の Worker URL を開きます。

```txt
https://code-memo.<your-subdomain>.workers.dev/
```

## AI に見せるURL

ZIP をアップロードすると、画面内に以下が出ます。

- index URL
- file URL
- AI に貼る文章テンプレート

AI にはまず index URL を貼ってください。

## 注意

このMVPは個人利用向けの最小版です。
URLを知っている人は共有URLを読めます。
大事なコードを扱う時は、Cloudflare側のアクセス制御や一時運用を検討してください。
