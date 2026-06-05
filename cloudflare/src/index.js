import { createStorage, normalizePath } from './storage.js';

const DEFAULT_FILE_FROM = 1;
const DEFAULT_FILE_TO = 600;
const MAX_FILE_RESPONSE_LINES = 800;
const DEFAULT_INDEX_FROM = 1;
const DEFAULT_INDEX_TO = 500;

function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: { 'content-type': 'text/html; charset=utf-8' },
  });
}

function plainText(body, status = 200) {
  return new Response(body, {
    status,
    headers: { 'content-type': 'text/plain; charset=utf-8' },
  });
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function parsePositiveInt(rawValue, fallback) {
  if (rawValue === null || rawValue === undefined || rawValue === '') {
    return fallback;
  }
  const parsed = Number.parseInt(String(rawValue), 10);
  if (!Number.isInteger(parsed) || parsed < 1) {
    return null;
  }
  return parsed;
}

function formatBytes(bytes) {
  if (bytes < 1024) {
    return `${bytes}B`;
  }
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))}KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

function baseUrlFor(url, env) {
  return String(env.PUBLIC_BASE_URL || `${url.protocol}//${url.host}`).replace(/\/$/, '');
}

function adminKeyFrom(request, url, formData = null) {
  return (
    request.headers.get('x-admin-key') ||
    url.searchParams.get('admin_key') ||
    (formData ? String(formData.get('admin_key') || '') : '')
  );
}

function isAuthorized(request, url, env, formData = null) {
  if (!env.ADMIN_KEY) {
    return true;
  }
  const provided = adminKeyFrom(request, url, formData);
  return provided === env.ADMIN_KEY;
}

function adminUrl(adminKey, { token = '', path = '', folder = '' } = {}) {
  const params = new URLSearchParams();
  if (token) {
    params.set('token', token);
  }
  if (path) {
    params.set('path', path);
  }
  if (folder) {
    params.set('folder', folder);
  }
  if (adminKey) {
    params.set('admin_key', adminKey);
  }
  const query = params.toString();
  return `/admin${query ? `?${query}` : ''}`;
}

function redirectToAdmin(currentUrl, adminKey, options = {}) {
  return Response.redirect(new URL(adminUrl(adminKey, options), currentUrl).toString(), 303);
}

function loginPage() {
  return html(
    `<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
      `<title>Code Memo Cloud</title><body style="font-family:sans-serif;max-width:560px;margin:0 auto;padding:24px;">` +
      `<h1>Code Memo Cloud</h1><p>管理画面を開くには管理キーが必要です。</p>` +
      `<form method="get" action="/admin" style="display:grid;gap:12px;">` +
      `<label>ADMIN_KEY<input type="password" name="admin_key" style="width:100%;min-height:44px;"></label>` +
      `<button type="submit" style="min-height:44px;">管理画面を開く</button></form></body></html>`,
    401
  );
}

async function renderAdminPage(request, env, storage, message = '', forcedAdminKey = '') {
  const url = new URL(request.url);
  const adminKey = forcedAdminKey || adminKeyFrom(request, url);
  if (env.ADMIN_KEY && adminKey !== env.ADMIN_KEY) {
    return loginPage();
  }

  const memos = await storage.listMemos();
  const selectedToken = url.searchParams.get('token') || memos[0]?.token || '';
  const selectedMemo = selectedToken ? await storage.getMemo(selectedToken) : null;
  const folders = selectedMemo ? await storage.listFolders(selectedToken) : [];
  const selectedFolder = url.searchParams.get('folder') || '';
  const files = selectedMemo
    ? await storage.listFiles(selectedToken, { folderId: selectedFolder || null })
    : [];
  const defaultPath = url.searchParams.get('path') || files[0]?.path || '';
  const selectedFile = selectedMemo && defaultPath ? await storage.getFile(selectedToken, defaultPath) : null;
  const baseUrl = baseUrlFor(url, env);

  const folderOptions = folders
    .map((folder) => `<option value="${escapeHtml(folder.id)}">${escapeHtml(folder.name)}</option>`)
    .join('');
  const folderLinks = [
    `<a href="${adminUrl(adminKey, { token: selectedToken })}" style="padding:8px 12px;border:1px solid #aaa;text-decoration:none;">すべて</a>`,
    ...folders.map(
      (folder) =>
        `<a href="${adminUrl(adminKey, { token: selectedToken, folder: folder.id })}" style="padding:8px 12px;border:1px solid #aaa;text-decoration:none;">${escapeHtml(folder.name)}</a>`
    ),
  ].join(' ');

  const fileRows = files.length
    ? files
        .map((file) => {
          const fileUrl = adminUrl(adminKey, { token: selectedToken, path: file.path, folder: selectedFolder });
          const toggleAction = file.hidden ? 'unhide' : 'hide';
          const toggleLabel = file.hidden ? '戻す' : '隠す';
          return (
            `<li style="display:grid;gap:8px;padding:12px 0;border-top:1px solid #eee;">` +
            `<label><input type="checkbox" form="assign-folder-form" name="paths" value="${escapeHtml(file.path)}"> ` +
            `<a href="${fileUrl}">${escapeHtml(file.path)}</a> <small>${file.lines} lines / ${formatBytes(file.bytes)}${file.hidden ? ' / hidden' : ''}</small></label>` +
            `<div style="display:flex;gap:8px;flex-wrap:wrap;">` +
            `<form method="post" action="/admin/memos/${encodeURIComponent(selectedToken)}/files/${toggleAction}">` +
            `<input type="hidden" name="admin_key" value="${escapeHtml(adminKey)}"><input type="hidden" name="path" value="${escapeHtml(file.path)}">` +
            `<button type="submit" style="min-height:40px;">${toggleLabel}</button></form>` +
            `<form method="post" action="/admin/memos/${encodeURIComponent(selectedToken)}/files/delete" onsubmit="return confirm('このファイルを削除しますか？');">` +
            `<input type="hidden" name="admin_key" value="${escapeHtml(adminKey)}"><input type="hidden" name="path" value="${escapeHtml(file.path)}">` +
            `<button type="submit" style="min-height:40px;">削除</button></form></div></li>`
          );
        })
        .join('')
    : '<li>まだファイルがありません。</li>';

  const visibleFiles = files.filter((file) => !file.hidden);
  const samplePath = visibleFiles[0]?.path || '';
  const scopedShareUrl = selectedFolder
    ? `${baseUrl}/t/${selectedToken}/share/folder-${selectedFolder}/index`
    : `${baseUrl}/t/${selectedToken}/index`;
  const aiTemplate = selectedToken
    ? `まず ${scopedShareUrl} を読んでください。\n必要なファイルは ${baseUrl}/t/${selectedToken}/${selectedFolder ? `share/folder-${selectedFolder}/` : ''}file?path=${encodeURIComponent(samplePath || 'README.md')}&from=1&to=600 を使ってください。`
    : '';
  const filePreview = selectedFile
    ? selectedFile.text
        .replace(/\r\n/g, '\n')
        .replace(/\r/g, '\n')
        .split('\n')
        .map((line, index) => `${index + 1}| ${line}`)
        .join('\n')
    : 'ファイルを選ぶと本文を表示します。';

  const memoItems = memos.length
    ? memos
        .map(
          (memo) =>
            `<li><a href="${adminUrl(adminKey, { token: memo.token })}">${escapeHtml(memo.name)}</a> <small>${escapeHtml(
              memo.updatedAt
            )}</small></li>`
        )
        .join('')
    : '<li>まだメモがありません。</li>';

  return html(
    `<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
      `<title>Code Memo Cloud</title><body style="font-family:sans-serif;max-width:960px;margin:0 auto;padding:16px;line-height:1.5;">` +
      `<h1>Code Memo Cloud</h1><p>CloudflareでZIPを追加して、そのままコードを確認できます。</p>` +
      `<div style="display:flex;gap:12px;flex-wrap:wrap;margin:16px 0;">` +
      `<a href="#upload" style="display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 14px;border:1px solid #666;text-decoration:none;">ファイルを追加</a>` +
      `<a href="#share" style="display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 14px;border:1px solid #666;text-decoration:none;">シェア</a></div>` +
      `${message ? `<p style="background:#fff7d6;padding:12px;border:1px solid #e3c86a;">${escapeHtml(message)}</p>` : ''}` +
      `<section id="upload" style="display:grid;gap:12px;border:1px solid #ddd;padding:16px;border-radius:12px;">` +
      `<h2 style="margin:0;">ZIPを追加</h2><form method="post" action="/admin/memos/create" enctype="multipart/form-data" style="display:grid;gap:12px;">` +
      `<input type="hidden" name="admin_key" value="${escapeHtml(adminKey)}">` +
      `<label>メモ名<input name="name" required style="width:100%;min-height:44px;"></label>` +
      `<label>ZIPファイル<input type="file" name="file" accept=".zip,application/zip" required style="width:100%;min-height:44px;"></label>` +
      `<button type="submit" style="min-height:44px;">ファイルを追加</button></form></section>` +
      `<section style="margin-top:16px;display:grid;gap:12px;">` +
      `<h2 style="margin:0;">保存済みメモ</h2><ul>${memoItems}</ul></section>` +
      (selectedMemo
        ? `<section style="margin-top:16px;display:grid;gap:12px;border:1px solid #ddd;padding:16px;border-radius:12px;">` +
          `<h2 style="margin:0;">${escapeHtml(selectedMemo.name)}</h2>` +
          `<div><strong>フォルダ</strong><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">${folderLinks}</div></div>` +
          `<form method="post" action="/admin/memos/${encodeURIComponent(selectedToken)}/folders/create" style="display:flex;gap:8px;flex-wrap:wrap;">` +
          `<input type="hidden" name="admin_key" value="${escapeHtml(adminKey)}"><input name="name" placeholder="新しいフォルダ名" style="min-height:44px;flex:1 1 220px;">` +
          `<button type="submit" style="min-height:44px;">フォルダを作成</button></form>` +
          `<form id="assign-folder-form" method="post" action="/admin/memos/${encodeURIComponent(selectedToken)}/folders/assign" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">` +
          `<input type="hidden" name="admin_key" value="${escapeHtml(adminKey)}">` +
          `<select name="folder_id" style="min-height:44px;flex:1 1 220px;">${folderOptions || '<option value="">フォルダを先に作成してください</option>'}</select>` +
          `<button type="submit" style="min-height:44px;">選択ファイルをフォルダ分け</button></form>` +
          `<h3 style="margin:0;">ファイル一覧</h3><ul style="list-style:none;padding:0;margin:0;">${fileRows}</ul>` +
          `<h3 style="margin:0;">本文</h3><pre style="overflow:auto;background:#111;color:#f5f5f5;padding:12px;border-radius:8px;">${escapeHtml(filePreview)}</pre>` +
          `<h3 id="share" style="margin:0;">シェア</h3>` +
          `<label>index URL<input id="index-url" readonly value="${escapeHtml(scopedShareUrl)}" style="width:100%;min-height:44px;"></label>` +
          `<button type="button" onclick="copyById('index-url')" style="min-height:44px;">index URLをコピー</button>` +
          `<label>file URL<input id="file-url" readonly value="${escapeHtml(
            samplePath
              ? `${baseUrl}/t/${selectedToken}/${selectedFolder ? `share/folder-${selectedFolder}/` : ''}file?path=${encodeURIComponent(samplePath)}&from=1&to=600`
              : ''
          )}" style="width:100%;min-height:44px;"></label>` +
          `<button type="button" onclick="copyById('file-url')" style="min-height:44px;">file URLをコピー</button>` +
          `<label>AIに貼るテンプレート<textarea id="ai-template" readonly rows="4" style="width:100%;">${escapeHtml(aiTemplate)}</textarea></label>` +
          `<button type="button" onclick="copyById('ai-template')" style="min-height:44px;">テンプレートをコピー</button>` +
          `</section>`
        : '') +
      `<script>function copyById(id){const el=document.getElementById(id);if(!el){return;}const text=el.value??el.textContent;if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text);}else{el.focus();if(el.select){el.select();}document.execCommand('copy');}}</script>` +
      `</body></html>`
  );
}

function buildFileResponse(file, fromLine, toLine) {
  const normalizedLines = file.text.replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n');
  if (normalizedLines.length > 0 && file.text.endsWith('\n')) {
    normalizedLines.pop();
  }
  const start = fromLine;
  const cappedEnd = Math.min(toLine, start + MAX_FILE_RESPONSE_LINES - 1, normalizedLines.length || start - 1);
  const body = normalizedLines
    .slice(start - 1, cappedEnd)
    .map((line, index) => `${start + index}| ${line}`)
    .join('\n');
  if (cappedEnd < normalizedLines.length) {
    const nextFrom = cappedEnd + 1;
    const span = Math.max(1, cappedEnd - start + 1);
    const nextTo = nextFrom + span - 1;
    return `${body}\n--- 続きは from=${nextFrom}&to=${nextTo} で取得 ---`;
  }
  return body;
}

async function handleIndexRoute(token, url, storage, scopePaths = null) {
  const fromIndex = parsePositiveInt(url.searchParams.get('from'), DEFAULT_INDEX_FROM);
  const toIndex = parsePositiveInt(url.searchParams.get('to'), DEFAULT_INDEX_TO);
  if (fromIndex === null || toIndex === null || toIndex < fromIndex) {
    return plainText('invalid range', 400);
  }
  let files = await storage.listFiles(token, { visibleOnly: true });
  if (scopePaths) {
    files = files.filter((file) => scopePaths.has(file.path));
  }
  if (files.length === 0) {
    return plainText('not found', 404);
  }
  const selected = files.slice(fromIndex - 1, toIndex);
  const lines = selected.map((file) => `${file.path} | ${file.lines} lines | ${formatBytes(file.bytes)}`);
  lines.push('', `total files: ${files.length}`, `total lines: ${files.reduce((sum, file) => sum + file.lines, 0)}`);
  return plainText(lines.join('\n'));
}

async function handleFileRoute(token, url, storage, scopePaths = null) {
  const rawPath = url.searchParams.get('path');
  const normalizedPath = normalizePath(rawPath || '');
  if (!normalizedPath) {
    return plainText('invalid path', 400);
  }
  if (scopePaths && !scopePaths.has(normalizedPath)) {
    return plainText('not found', 404);
  }
  const fromLine = parsePositiveInt(url.searchParams.get('from'), DEFAULT_FILE_FROM);
  const toLine = parsePositiveInt(url.searchParams.get('to'), DEFAULT_FILE_TO);
  if (fromLine === null || toLine === null || toLine < fromLine) {
    return plainText('invalid range', 400);
  }
  const file = await storage.getFile(token, normalizedPath);
  if (!file || file.hidden) {
    return plainText('not found', 404);
  }
  return plainText(buildFileResponse(file, fromLine, toLine));
}

async function shareScope(token, shareId, storage) {
  if (!shareId.startsWith('folder-')) {
    return null;
  }
  const folderId = shareId.slice('folder-'.length);
  const folder = await storage.getFolder(token, folderId);
  if (!folder) {
    return null;
  }
  return new Set(folder.paths);
}

export async function handleRequest(request, env, storage = createStorage(env)) {
  const url = new URL(request.url);
  const pathname = url.pathname;

  if (pathname === '/' || pathname === '/admin') {
    return renderAdminPage(request, env, storage);
  }

  if (request.method === 'POST' && pathname === '/admin/memos/create') {
    const formData = await request.formData();
    if (!isAuthorized(request, url, env, formData)) {
      return loginPage();
    }
    const name = String(formData.get('name') || '').trim();
    const file = formData.get('file');
    if (!name || !(file instanceof File)) {
      return renderAdminPage(request, env, storage, 'メモ名とZIPファイルを入力してください。', adminKeyFrom(request, url, formData));
    }
    try {
      const created = await storage.ingestZip({
        name,
        filename: file.name,
        zipBytes: new Uint8Array(await file.arrayBuffer()),
      });
      return redirectToAdmin(url, adminKeyFrom(request, url, formData), { token: created.token });
    } catch (error) {
      return renderAdminPage(
        request,
        env,
        storage,
        error instanceof Error ? error.message : 'ZIP の取り込みに失敗しました。',
        adminKeyFrom(request, url, formData)
      );
    }
  }

  const fileActionMatch = pathname.match(/^\/admin\/memos\/([^/]+)\/files\/(hide|unhide|delete)$/);
  if (request.method === 'POST' && fileActionMatch) {
    const [, rawToken, action] = fileActionMatch;
    const token = decodeURIComponent(rawToken);
    const formData = await request.formData();
    if (!isAuthorized(request, url, env, formData)) {
      return loginPage();
    }
    const path = normalizePath(String(formData.get('path') || ''));
    if (!path) {
      return redirectToAdmin(url, adminKeyFrom(request, url, formData), { token });
    }
    if (action === 'delete') {
      await storage.deleteFile(token, path);
      return redirectToAdmin(url, adminKeyFrom(request, url, formData), { token });
    }
    await storage.setFileHidden(token, path, action === 'hide');
    return redirectToAdmin(url, adminKeyFrom(request, url, formData), { token, path });
  }

  const folderCreateMatch = pathname.match(/^\/admin\/memos\/([^/]+)\/folders\/create$/);
  if (request.method === 'POST' && folderCreateMatch) {
    const token = decodeURIComponent(folderCreateMatch[1]);
    const formData = await request.formData();
    if (!isAuthorized(request, url, env, formData)) {
      return loginPage();
    }
    const name = String(formData.get('name') || '').trim();
    if (!name) {
      return redirectToAdmin(url, adminKeyFrom(request, url, formData), { token });
    }
    const folderId = await storage.createFolder(token, name);
    return redirectToAdmin(url, adminKeyFrom(request, url, formData), { token, folder: folderId });
  }

  const folderAssignMatch = pathname.match(/^\/admin\/memos\/([^/]+)\/folders\/assign$/);
  if (request.method === 'POST' && folderAssignMatch) {
    const token = decodeURIComponent(folderAssignMatch[1]);
    const formData = await request.formData();
    if (!isAuthorized(request, url, env, formData)) {
      return loginPage();
    }
    const folderId = String(formData.get('folder_id') || '').trim();
    const paths = formData
      .getAll('paths')
      .map((entry) => normalizePath(String(entry || '')))
      .filter(Boolean);
    if (folderId) {
      await storage.assignFilesToFolder(token, folderId, paths);
    }
    return redirectToAdmin(url, adminKeyFrom(request, url, formData), { token, folder: folderId });
  }

  const indexMatch = pathname.match(/^\/t\/([^/]+)\/index$/);
  if (request.method === 'GET' && indexMatch) {
    return handleIndexRoute(decodeURIComponent(indexMatch[1]), url, storage);
  }

  const fileMatch = pathname.match(/^\/t\/([^/]+)\/file$/);
  if (request.method === 'GET' && fileMatch) {
    return handleFileRoute(decodeURIComponent(fileMatch[1]), url, storage);
  }

  const shareIndexMatch = pathname.match(/^\/t\/([^/]+)\/share\/([^/]+)\/index$/);
  if (request.method === 'GET' && shareIndexMatch) {
    const token = decodeURIComponent(shareIndexMatch[1]);
    const scopePaths = await shareScope(token, decodeURIComponent(shareIndexMatch[2]), storage);
    if (!scopePaths) {
      return plainText('not found', 404);
    }
    return handleIndexRoute(token, url, storage, scopePaths);
  }

  const shareFileMatch = pathname.match(/^\/t\/([^/]+)\/share\/([^/]+)\/file$/);
  if (request.method === 'GET' && shareFileMatch) {
    const token = decodeURIComponent(shareFileMatch[1]);
    const scopePaths = await shareScope(token, decodeURIComponent(shareFileMatch[2]), storage);
    if (!scopePaths) {
      return plainText('not found', 404);
    }
    return handleFileRoute(token, url, storage, scopePaths);
  }

  return plainText('not found', 404);
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },
};
