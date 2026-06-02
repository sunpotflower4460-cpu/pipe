# Code Relay はじめてガイド

## 1. Code Relayとは

Code Relayは、ClaudeやChatGPTにコードを読んでもらうための中継アプリです。  
あなたのコードを登録すると、AIが読める専用URLが作られます。

## 2. 最初に開く場所

クラウド版では、まず管理画面のURLを開きます。  
例: `https://your-code-relay.example.com/admin`

## 3. 新しいパイプを作る

1. 「新しいパイプを作る」を押す
2. パイプ名を入力する
3. GitHubリポジトリURLを貼る、またはZIPをアップロードする
4. 登録する

## 4. Claudeに渡すURL

まずは `index URL` を渡します。  
Claudeが全体像を読んだあと、必要に応じて `file URL` や `symbol URL` を使います。

- index URL: 最初にAIへ渡すURLです。コード全体の地図を見せます。
- file URL: 特定のファイルを行番号付きで読ませるURLです。
- symbol URL: 関数やクラスだけを読ませたい時のURLです。
- changes URL: 前回から変わったファイルだけを見せるURLです。

## 5. Claudeに貼る文章テンプレート

```txt
以下のCode Relay URLからコード全体を確認してください。
まず /index を読んで構成を把握し、必要なファイルは /file?path=...&from=...&to=... で分割して読んでください。

{INDEX_URL}
```

## 6. レビューが終わったら

レビューが終わったら「停止 / revoke」を押すと、パイプが止まり、そのURLではAIがコードを読めなくなります。
