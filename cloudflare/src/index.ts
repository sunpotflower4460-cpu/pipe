type Env = {
  APP_NAME?: string;
};

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return text("ok");
    }
    return html(renderHome(env.APP_NAME || "Code Memo"));
  },
};

function renderHome(appName: string): string {
  return `<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(appName)}</title>
  <style>
    body{margin:0;background:#f7f5f0;color:#1f2933;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.6;}
    main{max-width:840px;margin:0 auto;padding:18px;}
    .card{background:#fff;border:1px solid #e5e1d8;border-radius:16px;padding:16px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.04);}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
    button,.button{min-height:44px;padding:0 16px;border:1px solid #8b8578;border-radius:12px;background:#fff;color:#111;text-decoration:none;font-size:16px;display:inline-flex;align-items:center;}
    input{font-size:16px;width:100%;box-sizing:border-box;padding:12px;border:1px solid #cfc8ba;border-radius:12px;background:#fff;}
    small{color:#667085;}
  </style>
</head>
<body><main>
  <h1>${escapeHtml(appName)}</h1>
  <p>ZIPを追加して、コードを読めるメモ帳にします。次の更新でCloudflare R2/KV保存とAI共有URLをつなぎます。</p>

  <section class="card">
    <h2>ファイルを追加</h2>
    <form method="post" enctype="multipart/form-data" action="/memos/create">
      <label>メモ名<input name="name" value="Code Memo"></label><br><br>
      <label>ZIPファイル<input type="file" name="file" accept=".zip,application/zip"></label><br><br>
      <div class="row"><button type="submit">ファイルを追加</button><a class="button" href="#share">シェア</a></div>
    </form>
    <p><small>今はUI接続の最小版です。ZIP処理は次のコミットで追加します。</small></p>
  </section>

  <section class="card" id="share">
    <h2>シェア</h2>
    <p>アップロード後に、AIへ貼る index URL / file URL をここに表示します。</p>
  </section>
</main></body></html>`;
}

function text(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
}

function html(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8" } });
}

function escapeHtml(value: unknown): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
