import JSZip from "jszip";

type Env = {
  CODE_MEMO_BUCKET: R2Bucket;
  APP_NAME?: string;
  MAX_FILE_BYTES?: string;
  MAX_FILES_PER_ZIP?: string;
  DEFAULT_TO?: string;
  MAX_LINES_PER_RESPONSE?: string;
};

type FileInfo = { path: string; lines: number; bytes: number; hidden?: boolean; deleted?: boolean };
type Memo = { id: string; name: string; createdAt: string; updatedAt: string; files: FileInfo[] };

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      if (!env.CODE_MEMO_BUCKET) return html(setupPage(), 500);
      return await route(request, env);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "unknown error";
      return plain(`ERROR: ${msg}`, 500);
    }
  },
};

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (url.pathname === "/health") return plain("ok");
  if (request.method === "POST" && url.pathname === "/memos/create") return createMemo(request, env);

  const action = url.pathname.match(/^\/memos\/([^/]+)\/(hide|unhide|delete-file|delete)$/);
  if (request.method === "POST" && action) return changeMemo(request, env, action[1], action[2]);

  const index = url.pathname.match(/^\/t\/([^/]+)\/index$/);
  if (request.method === "GET" && index) return shareIndex(env, index[1]);

  const file = url.pathname.match(/^\/t\/([^/]+)\/file$/);
  if (request.method === "GET" && file) return shareFile(request, env, file[1]);

  return html(await renderHome(request, env));
}

async function createMemo(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const form = await request.formData();
  const upload = form.get("file");
  const name = String(form.get("name") || "Code Memo").trim() || "Code Memo";
  if (!(upload instanceof File)) return html(await renderHome(request, env, "ZIPファイルを選んでください。"), 400);

  const zip = await JSZip.loadAsync(await upload.arrayBuffer());
  const id = crypto.randomUUID().replaceAll("-", "").slice(0, 24);
  const files: FileInfo[] = [];
  const maxFiles = readInt(env.MAX_FILES_PER_ZIP, 800);
  const maxBytes = readInt(env.MAX_FILE_BYTES, 2500000);

  for (const entry of Object.values(zip.files)) {
    if (entry.dir || files.length >= maxFiles) continue;
    const path = cleanPath(entry.name);
    if (!path || skipPath(path)) continue;
    const data = await entry.async("uint8array");
    if (data.byteLength > maxBytes || hasZero(data)) continue;
    const text = decodeText(data);
    if (text === null) continue;
    await env.CODE_MEMO_BUCKET.put(fileKey(id, path), text, { httpMetadata: { contentType: "text/plain; charset=utf-8" } });
    files.push({ path, lines: countLines(text), bytes: data.byteLength });
  }

  const now = new Date().toISOString();
  const memo: Memo = { id, name, createdAt: now, updatedAt: now, files };
  await saveMemo(env, memo);
  return Response.redirect(`${url.origin}/?id=${encodeURIComponent(id)}`, 303);
}

async function changeMemo(request: Request, env: Env, id: string, action: string): Promise<Response> {
  const url = new URL(request.url);
  const memo = await getMemo(env, id);
  if (!memo) return Response.redirect(url.origin + "/", 303);
  if (action === "delete") {
    await Promise.all(memo.files.map((f) => env.CODE_MEMO_BUCKET.delete(fileKey(id, f.path))));
    await env.CODE_MEMO_BUCKET.delete(memoKey(id));
    await removeMemoFromIndex(env, id);
    return Response.redirect(url.origin + "/", 303);
  }
  const form = await request.formData();
  const path = String(form.get("path") || "");
  const target = memo.files.find((f) => f.path === path);
  if (target) {
    if (action === "hide") target.hidden = true;
    if (action === "unhide") target.hidden = false;
    if (action === "delete-file") {
      target.deleted = true;
      await env.CODE_MEMO_BUCKET.delete(fileKey(id, target.path));
    }
    memo.updatedAt = new Date().toISOString();
    await saveMemo(env, memo);
  }
  return Response.redirect(`${url.origin}/?id=${encodeURIComponent(id)}`, 303);
}

async function renderHome(request: Request, env: Env, notice = ""): Promise<string> {
  const url = new URL(request.url);
  const appName = env.APP_NAME || "Code Memo";
  const memos = await listMemos(env);
  const selectedId = url.searchParams.get("id") || memos[0]?.id || "";
  const selected = selectedId ? await getMemo(env, selectedId) : null;
  const selectedPath = url.searchParams.get("path") || "";
  const indexUrl = selected ? `${url.origin}/t/${selected.id}/index` : "";
  const fileUrl = selected && selectedPath ? `${url.origin}/t/${selected.id}/file?path=${encodeURIComponent(selectedPath)}&from=1&to=600` : "";

  const memoList = memos.map((m) => `<li><a href="/?id=${m.id}">${escapeHtml(m.name)}</a><br><small>${m.files.filter((f) => !f.hidden && !f.deleted).length} files</small></li>`).join("");
  const fileList = selected ? selected.files.filter((f) => !f.hidden && !f.deleted).map((f) => `<li><a href="/?id=${selected.id}&path=${encodeURIComponent(f.path)}">${escapeHtml(f.path)}</a> <small>${f.lines} lines</small></li>`).join("") : "";
  const hiddenList = selected ? selected.files.filter((f) => f.hidden && !f.deleted).map((f) => `<li>${escapeHtml(f.path)} <form method="post" action="/memos/${selected.id}/unhide" style="display:inline"><input type="hidden" name="path" value="${escapeHtml(f.path)}"><button>戻す</button></form></li>`).join("") : "";
  const bodyView = selected && selectedPath ? await renderFile(env, selected, selectedPath) : "<p>ファイルを選ぶとコード本文が表示されます。</p>";
  const template = indexUrl ? `以下のCode Memo URLからコードを確認してください。\nまずindexを読んで、必要なファイルはfile URLで分割して読んでください。\n\n${indexUrl}` : "ZIPを追加するとここにAI用文章が出ます。";

  return page(appName, `
    <h1>${escapeHtml(appName)}</h1>
    <p>ZIPを追加して、コードを読めるメモ帳にします。必要な時だけAIに見せるURLをコピーできます。</p>
    ${notice ? `<section class="card"><strong>${escapeHtml(notice)}</strong></section>` : ""}
    <section class="card"><h2>ファイルを追加</h2><form id="uploadForm" method="post" enctype="multipart/form-data" action="/memos/create"><label>メモ名<input name="name" value="Code Memo"></label><br><br><label>ZIPファイル<input id="zipInput" type="file" name="file" accept=".zip,application/zip" required></label><br><br><div class="row"><button id="uploadButton">ファイルを追加</button><a class="button" href="#share">シェア</a></div><div id="uploadPanel" class="progress-panel" hidden><div class="progress-label"><span id="uploadStatus">待機中...</span><span id="uploadPercent">0%</span></div><progress id="uploadProgress" max="100" value="0"></progress><small id="uploadHint">アップロード後、ZIPを展開してR2へ保存します。大きいZIPでは少し時間がかかります。</small></div></form></section>
    <section class="card"><h2>メモ</h2><ul>${memoList || "<li>まだありません。</li>"}</ul></section>
    ${selected ? `<section class="card"><h2>${escapeHtml(selected.name)}</h2><form method="post" action="/memos/${selected.id}/delete" onsubmit="return confirm('このメモを削除します。')"><button>このメモを削除</button></form><h3>ファイル一覧</h3><ul>${fileList || "<li>表示できるファイルがありません。</li>"}</ul><h3>隠したファイル</h3><ul>${hiddenList || "<li>まだありません。</li>"}</ul></section><section class="card"><h2>コード本文</h2>${bodyView}</section><section class="card" id="share"><h2>シェア</h2><label>index URL<input id="indexUrl" readonly value="${escapeHtml(indexUrl)}"></label><button onclick="copyText('indexUrl')">index URLをコピー</button><br><br><label>file URL<input id="fileUrl" readonly value="${escapeHtml(fileUrl)}"></label><button onclick="copyText('fileUrl')">file URLをコピー</button><br><br><label>AIに貼る文章<textarea id="tpl" rows="5" readonly>${escapeHtml(template)}</textarea></label><button onclick="copyText('tpl')">テンプレートをコピー</button></section>` : ""}
  `);
}

async function renderFile(env: Env, memo: Memo, path: string): Promise<string> {
  const record = memo.files.find((f) => f.path === path && !f.hidden && !f.deleted);
  if (!record) return "<p>このファイルは表示できません。</p>";
  const obj = await env.CODE_MEMO_BUCKET.get(fileKey(memo.id, path));
  if (!obj) return "<p>本文が見つかりません。</p>";
  const text = await obj.text();
  const lines = text.split(/\r?\n/).slice(0, 600);
  const width = String(lines.length).length;
  const code = lines.map((line, i) => `${String(i + 1).padStart(width, " ")}| ${escapeHtml(line)}`).join("\n");
  return `<h3>${escapeHtml(path)}</h3><div class="row"><form method="post" action="/memos/${memo.id}/hide"><input type="hidden" name="path" value="${escapeHtml(path)}"><button>隠す</button></form><form method="post" action="/memos/${memo.id}/delete-file" onsubmit="return confirm('このファイルを削除します。')"><input type="hidden" name="path" value="${escapeHtml(path)}"><button>削除</button></form></div><pre>${code}</pre>`;
}

async function shareIndex(env: Env, id: string): Promise<Response> {
  const memo = await getMemo(env, id);
  if (!memo) return plain("ERROR: memo not found", 404);
  const visible = memo.files.filter((f) => !f.hidden && !f.deleted);
  return plain([`# ${memo.name}`, "", ...visible.map((f) => `${f.path} | ${f.lines} lines | ${f.bytes} bytes`), "", `Total files: ${visible.length}`].join("\n"));
}

async function shareFile(request: Request, env: Env, id: string): Promise<Response> {
  const memo = await getMemo(env, id);
  if (!memo) return plain("ERROR: memo not found", 404);
  const url = new URL(request.url);
  const path = url.searchParams.get("path") || "";
  const record = memo.files.find((f) => f.path === path && !f.hidden && !f.deleted);
  if (!record) return plain("ERROR: file not found", 404);
  const obj = await env.CODE_MEMO_BUCKET.get(fileKey(id, path));
  if (!obj) return plain("ERROR: file content not found", 404);
  const lines = (await obj.text()).split(/\r?\n/);
  const from = Math.max(readInt(url.searchParams.get("from"), 1), 1);
  const max = readInt(env.MAX_LINES_PER_RESPONSE, 800);
  const requestedTo = readInt(url.searchParams.get("to"), 600);
  const to = Math.min(requestedTo, from + max - 1, lines.length);
  const width = String(to).length;
  const body = lines.slice(from - 1, to).map((line, i) => `${String(from + i).padStart(width, " ")}| ${line}`).join("\n");
  const next = to < lines.length ? `\n\n--- 続きは from=${to + 1}&to=${to + 600} で取得 ---` : "";
  return plain(`# ${path} lines ${from}-${to}\n\n${body}${next}`);
}

async function listMemos(env: Env): Promise<Memo[]> {
  const ids = await loadIndex(env);
  const memos: Memo[] = [];
  for (const id of ids) {
    const memo = await getMemo(env, id);
    if (memo) memos.push(memo);
  }
  return memos.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

async function getMemo(env: Env, id: string): Promise<Memo | null> {
  const obj = await env.CODE_MEMO_BUCKET.get(memoKey(id));
  if (!obj) return null;
  try { return JSON.parse(await obj.text()) as Memo; } catch { return null; }
}

async function saveMemo(env: Env, memo: Memo): Promise<void> {
  await env.CODE_MEMO_BUCKET.put(memoKey(memo.id), JSON.stringify(memo), { httpMetadata: { contentType: "application/json; charset=utf-8" } });
  const ids = await loadIndex(env);
  if (!ids.includes(memo.id)) {
    ids.push(memo.id);
    await saveIndex(env, ids);
  }
}

async function loadIndex(env: Env): Promise<string[]> {
  const obj = await env.CODE_MEMO_BUCKET.get(indexKey());
  if (!obj) return [];
  try { const parsed = JSON.parse(await obj.text()); return Array.isArray(parsed) ? parsed.filter((x) => typeof x === "string") : []; } catch { return []; }
}

async function saveIndex(env: Env, ids: string[]): Promise<void> {
  await env.CODE_MEMO_BUCKET.put(indexKey(), JSON.stringify(ids), { httpMetadata: { contentType: "application/json; charset=utf-8" } });
}

async function removeMemoFromIndex(env: Env, id: string): Promise<void> {
  await saveIndex(env, (await loadIndex(env)).filter((x) => x !== id));
}

function indexKey(): string { return "memos/index.json"; }
function memoKey(id: string): string { return `memos/${id}/memo.json`; }
function fileKey(id: string, path: string): string { return `memos/${id}/files/${path}`; }
function cleanPath(raw: string): string | null { const p = raw.replaceAll("\\", "/").replace(/^\/+/, ""); if (!p || p.includes("..") || p.startsWith("/")) return null; return p; }
function skipPath(path: string): boolean { const l = path.toLowerCase(); return l.includes("node_modules/") || l.includes(".git/") || l.includes("/dist/") || l.includes("/build/") || l.endsWith(".png") || l.endsWith(".jpg") || l.endsWith(".jpeg") || l.endsWith(".gif") || l.endsWith(".webp") || l.endsWith(".mp3") || l.endsWith(".wav") || l.endsWith(".zip"); }
function hasZero(bytes: Uint8Array): boolean { for (let i = 0; i < Math.min(bytes.length, 4096); i++) if (bytes[i] === 0) return true; return false; }
function decodeText(bytes: Uint8Array): string | null { try { return new TextDecoder("utf-8", { fatal: true }).decode(bytes); } catch { return null; } }
function countLines(text: string): number { return text ? text.split(/\r?\n/).length : 0; }
function readInt(raw: string | null | undefined, fallback: number): number { if (!raw) return fallback; const n = Number.parseInt(raw, 10); return Number.isFinite(n) && n > 0 ? n : fallback; }
function plain(body: string, status = 200): Response { return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } }); }
function html(body: string, status = 200): Response { return new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8" } }); }
function escapeHtml(value: unknown): string { return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\"/g, "&quot;").replace(/'/g, "&#39;"); }
function setupPage(): string { return page("Code Memo setup", `<h1>Code Memo</h1><section class="card"><h2>R2 binding が未設定です</h2><p>Cloudflare の Bindings で R2 bucket を追加してください。</p><p><strong>Variable name:</strong> CODE_MEMO_BUCKET<br><strong>Bucket:</strong> code-memo-files</p><p>設定後に Redeploy してください。</p></section>`); }
function page(title: string, body: string): string { return `<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${escapeHtml(title)}</title><style>body{margin:0;background:#f7f5f0;color:#1f2933;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.6}main{max-width:920px;margin:0 auto;padding:16px}.card{background:#fff;border:1px solid #e5e1d8;border-radius:16px;padding:16px;margin:14px 0}.row{display:flex;gap:10px;flex-wrap:wrap}button,.button{min-height:44px;padding:0 16px;border:1px solid #777;border-radius:12px;background:white;color:#111;text-decoration:none;display:inline-flex;align-items:center}button:disabled{opacity:.55;cursor:not-allowed}input,textarea{font-size:16px;width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccc;border-radius:10px}pre{white-space:pre;overflow:auto;background:#111827;color:white;border-radius:12px;padding:12px;font-size:13px}small{color:#667085}.progress-panel{margin-top:16px;padding:12px;border:1px solid #d8d1c3;border-radius:12px;background:#fbfaf7}.progress-label{display:flex;justify-content:space-between;gap:12px;font-weight:700;margin-bottom:8px}progress{width:100%;height:18px}</style><script>function copyText(id){const el=document.getElementById(id);if(el)navigator.clipboard.writeText(el.value)}document.addEventListener('DOMContentLoaded',function(){const form=document.getElementById('uploadForm');if(!form)return;const panel=document.getElementById('uploadPanel');const progress=document.getElementById('uploadProgress');const status=document.getElementById('uploadStatus');const percent=document.getElementById('uploadPercent');const hint=document.getElementById('uploadHint');const button=document.getElementById('uploadButton');const fileInput=document.getElementById('zipInput');form.addEventListener('submit',function(event){event.preventDefault();const file=fileInput&&fileInput.files&&fileInput.files[0];if(!file){status.textContent='ZIPファイルを選んでください';if(panel)panel.hidden=false;return}if(panel)panel.hidden=false;if(button)button.disabled=true;status.textContent='アップロード準備中...';percent.textContent='0%';progress.value=0;hint.textContent='選択中: '+file.name+' / '+Math.round(file.size/1024)+' KB';const xhr=new XMLHttpRequest();xhr.open('POST',form.action);xhr.upload.onprogress=function(e){if(e.lengthComputable){const p=Math.round((e.loaded/e.total)*100);progress.value=p;percent.textContent=p+'%';status.textContent='アップロード中...'}else{status.textContent='アップロード中...';percent.textContent='計算中'}};xhr.onload=function(){if(xhr.status>=200&&xhr.status<400){progress.value=100;percent.textContent='100%';status.textContent='アップロード完了。ZIPを展開・保存しました。';hint.textContent='画面を更新します...';window.location.href=xhr.responseURL||'/'}else{status.textContent='失敗しました';hint.textContent=xhr.responseText||('HTTP '+xhr.status);if(button)button.disabled=false}};xhr.onerror=function(){status.textContent='通信エラー';hint.textContent='ネットワークを確認して、もう一度試してください。';if(button)button.disabled=false};status.textContent='送信中...';xhr.send(new FormData(form))})})</script></head><body><main>${body}</main></body></html>`; }
