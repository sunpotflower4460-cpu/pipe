import test from 'node:test';
import assert from 'node:assert/strict';
import { zipSync, strToU8 } from 'fflate';

import { handleRequest } from '../src/index.js';
import { createMemoryStorage } from '../src/storage.js';

function makeZip(entries) {
  const archive = {};
  for (const [path, content] of Object.entries(entries)) {
    archive[path] = typeof content === 'string' ? strToU8(content) : content;
  }
  return zipSync(archive);
}

function findTokenFromLocation(response) {
  const location = response.headers.get('location') || '';
  return new URL(`https://example.com${location}`).searchParams.get('token');
}

test('admin upload creates a memo and text endpoints serve filtered files', async () => {
  const storage = createMemoryStorage();
  const env = { ADMIN_KEY: 'secret', PUBLIC_BASE_URL: 'https://memo.example.com' };
  const lines = Array.from({ length: 610 }, (_, index) => `line ${index + 1}`).join('\n');
  const zipBytes = makeZip({
    'README.md': '# sample\n',
    'src/main.ts': `console.log('ok');\n${lines}`,
    '.env': 'SECRET=1\n',
    'node_modules/pkg/index.js': 'should be ignored\n',
  });

  const form = new FormData();
  form.set('admin_key', 'secret');
  form.set('name', 'Cloud Memo');
  form.set('file', new File([zipBytes], 'sample.zip', { type: 'application/zip' }));

  const created = await handleRequest(new Request('https://example.com/admin/memos/create', { method: 'POST', body: form }), env, storage);
  assert.equal(created.status, 303);
  const token = findTokenFromLocation(created);
  assert.ok(token);

  const adminPage = await handleRequest(new Request(`https://example.com/admin?token=${token}&admin_key=secret`), env, storage);
  const adminHtml = await adminPage.text();
  assert.match(adminHtml, /ファイルを追加/);
  assert.match(adminHtml, /シェア/);
  assert.match(adminHtml, /Cloud Memo/);

  const indexResponse = await handleRequest(new Request(`https://example.com/t/${token}/index`), env, storage);
  const indexText = await indexResponse.text();
  assert.equal(indexResponse.status, 200);
  assert.match(indexText, /README\.md \| 1 lines/);
  assert.match(indexText, /src\/main\.ts/);
  assert.doesNotMatch(indexText, /\.env/);
  assert.doesNotMatch(indexText, /node_modules/);

  const fileResponse = await handleRequest(
    new Request(`https://example.com/t/${token}/file?path=src%2Fmain.ts&from=1&to=600`),
    env,
    storage
  );
  const fileText = await fileResponse.text();
  assert.equal(fileResponse.status, 200);
  assert.match(fileText, /1\| console\.log\('ok'\);/);
  assert.match(fileText, /--- 続きは from=601&to=1200 で取得 ---/);
});

test('hide, unhide, delete, and folder share routes work', async () => {
  const storage = createMemoryStorage();
  const env = { ADMIN_KEY: 'secret', PUBLIC_BASE_URL: 'https://memo.example.com' };
  const created = await storage.ingestZip({
    name: 'Memo',
    filename: 'sample.zip',
    zipBytes: makeZip({ 'README.md': '# hello\n', 'src/app.js': 'console.log(1);\n' }),
  });
  const token = created.token;

  const hideForm = new FormData();
  hideForm.set('admin_key', 'secret');
  hideForm.set('path', 'README.md');
  const hidden = await handleRequest(new Request(`https://example.com/admin/memos/${token}/files/hide`, { method: 'POST', body: hideForm }), env, storage);
  assert.equal(hidden.status, 303);

  const hiddenIndex = await handleRequest(new Request(`https://example.com/t/${token}/index`), env, storage);
  assert.doesNotMatch(await hiddenIndex.text(), /README\.md/);

  const unhideForm = new FormData();
  unhideForm.set('admin_key', 'secret');
  unhideForm.set('path', 'README.md');
  await handleRequest(new Request(`https://example.com/admin/memos/${token}/files/unhide`, { method: 'POST', body: unhideForm }), env, storage);

  const folderForm = new FormData();
  folderForm.set('admin_key', 'secret');
  folderForm.set('name', 'Review Group');
  const folderCreated = await handleRequest(new Request(`https://example.com/admin/memos/${token}/folders/create`, { method: 'POST', body: folderForm }), env, storage);
  const folderId = new URL(`https://example.com${folderCreated.headers.get('location')}`).searchParams.get('folder');
  assert.equal(folderId, 'review-group');

  const assignForm = new FormData();
  assignForm.set('admin_key', 'secret');
  assignForm.set('folder_id', 'review-group');
  assignForm.append('paths', 'README.md');
  await handleRequest(new Request(`https://example.com/admin/memos/${token}/folders/assign`, { method: 'POST', body: assignForm }), env, storage);

  const shareIndex = await handleRequest(new Request(`https://example.com/t/${token}/share/folder-review-group/index`), env, storage);
  const shareIndexText = await shareIndex.text();
  assert.match(shareIndexText, /README\.md/);
  assert.doesNotMatch(shareIndexText, /src\/app\.js/);

  const deleteForm = new FormData();
  deleteForm.set('admin_key', 'secret');
  deleteForm.set('path', 'README.md');
  await handleRequest(new Request(`https://example.com/admin/memos/${token}/files/delete`, { method: 'POST', body: deleteForm }), env, storage);

  const deletedIndex = await handleRequest(new Request(`https://example.com/t/${token}/index`), env, storage);
  assert.doesNotMatch(await deletedIndex.text(), /README\.md/);
});

test('admin key is required for the management UI', async () => {
  const storage = createMemoryStorage();
  const env = { ADMIN_KEY: 'secret', PUBLIC_BASE_URL: 'https://memo.example.com' };

  const response = await handleRequest(new Request('https://example.com/admin'), env, storage);
  assert.equal(response.status, 401);
  assert.match(await response.text(), /ADMIN_KEY/);
});
