import { unzipSync } from 'fflate';

const EXCLUDED_DIRECTORIES = new Set([
  '.git',
  'node_modules',
  'build',
  'dist',
  '.next',
  '.venv',
  'venv',
  '__pycache__',
  '.pytest_cache',
  '.mypy_cache',
  '__MACOSX',
]);

const EXCLUDED_FILE_NAMES = new Set([
  '.env',
  '.env.local',
  '.env.production',
  'id_rsa',
  'id_rsa.pub',
  'id_dsa',
  'id_dsa.pub',
  'id_ecdsa',
  'id_ecdsa.pub',
  'id_ed25519',
  'id_ed25519.pub',
  'authorized_keys',
]);

const EXCLUDED_FILE_EXTENSIONS = new Set([
  '.pem',
  '.key',
  '.p12',
  '.crt',
  '.cer',
  '.ttf',
  '.otf',
  '.woff',
  '.woff2',
  '.png',
  '.jpg',
  '.jpeg',
  '.gif',
  '.webp',
  '.svg',
  '.ico',
  '.wav',
  '.mp3',
  '.aiff',
  '.flac',
  '.m4a',
  '.mp4',
  '.mov',
  '.webm',
  '.zip',
  '.tar',
  '.gz',
  '.7z',
  '.rar',
]);

const TEXT_SNIFF_BYTES = 4096;

export function generateToken() {
  return `${crypto.randomUUID().replaceAll('-', '')}${crypto.randomUUID().replaceAll('-', '')}`;
}

export function slugify(value) {
  const normalized = String(value ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return normalized || 'folder';
}

export function countLines(text) {
  if (!text) {
    return 0;
  }
  const normalized = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const parts = normalized.split('\n');
  if (normalized.endsWith('\n')) {
    parts.pop();
  }
  return parts.length;
}

function looksLikePrivateKey(bytes) {
  const sniff = bytes.slice(0, TEXT_SNIFF_BYTES);
  const text = new TextDecoder().decode(sniff);
  return text.includes('-----BEGIN ') && text.includes('PRIVATE KEY-----');
}

function looksBinary(bytes) {
  const sniff = bytes.slice(0, TEXT_SNIFF_BYTES);
  return sniff.includes(0);
}

export function normalizePath(rawPath) {
  const normalized = String(rawPath ?? '').replaceAll('\\', '/').trim();
  if (!normalized || normalized.startsWith('/')) {
    return null;
  }
  const parts = normalized.split('/');
  if (parts.some((part) => !part || part === '.' || part === '..')) {
    return null;
  }
  return parts.join('/');
}

function isExcludedPath(path) {
  const parts = path.split('/');
  const lowerParts = parts.map((part) => part.toLowerCase());
  if (lowerParts.some((part) => EXCLUDED_DIRECTORIES.has(part))) {
    return true;
  }
  const name = lowerParts[lowerParts.length - 1];
  if (EXCLUDED_FILE_NAMES.has(name) || name.startsWith('.env.')) {
    return true;
  }
  const dot = name.lastIndexOf('.');
  const extension = dot >= 0 ? name.slice(dot) : '';
  return EXCLUDED_FILE_EXTENSIONS.has(extension);
}

export function parseZipArchive(zipBytes) {
  let archive;
  try {
    archive = unzipSync(zipBytes instanceof Uint8Array ? zipBytes : new Uint8Array(zipBytes));
  } catch {
    throw new Error('ZIP を読み取れませんでした。');
  }

  const decoder = new TextDecoder('utf-8', { fatal: true });
  const files = [];

  for (const [entryName, payload] of Object.entries(archive)) {
    if (!payload || entryName.endsWith('/')) {
      continue;
    }
    const normalized = normalizePath(entryName);
    if (normalized === null) {
      throw new Error('ZIP に危険なパスが含まれています。');
    }
    if (isExcludedPath(normalized)) {
      continue;
    }
    if (looksBinary(payload) || looksLikePrivateKey(payload)) {
      continue;
    }
    let text;
    try {
      text = decoder.decode(payload);
    } catch {
      continue;
    }
    files.push({
      path: normalized,
      text,
      lines: countLines(text),
      bytes: payload.byteLength,
    });
  }

  files.sort((left, right) => left.path.localeCompare(right.path));
  return files;
}

function fileObjectKey(token, path) {
  return `memos/${token}/files/${path}`;
}

function rawZipObjectKey(token, filename) {
  const safeName = String(filename ?? 'upload.zip').replace(/[^A-Za-z0-9._-]+/g, '-');
  return `memos/${token}/raw/${safeName || 'upload.zip'}`;
}

function rowToFile(row) {
  return {
    path: String(row.path),
    lines: Number(row.lines),
    bytes: Number(row.bytes),
    hidden: Number(row.hidden) === 1,
  };
}

export function createStorage(env) {
  const db = env.CODE_MEMO_DB;
  const bucket = env.CODE_MEMO_BUCKET;

  return {
    async ingestZip({ name, filename, zipBytes }) {
      const files = parseZipArchive(zipBytes);
      if (files.length === 0) {
        throw new Error('ZIP に読み取れるテキストファイルがありません。');
      }
      const token = generateToken();
      const now = new Date().toISOString();
      const rawZipKey = rawZipObjectKey(token, filename);

      await bucket.put(rawZipKey, zipBytes, {
        httpMetadata: { contentType: 'application/zip' },
      });
      await db
        .prepare('INSERT INTO memos (token, name, created_at, updated_at, raw_zip_key) VALUES (?, ?, ?, ?, ?)')
        .bind(token, name, now, now, rawZipKey)
        .run();

      for (const file of files) {
        await bucket.put(fileObjectKey(token, file.path), file.text, {
          httpMetadata: { contentType: 'text/plain; charset=utf-8' },
        });
      }
      if (files.length > 0) {
        await db.batch(
          files.map((file) =>
            db
              .prepare('INSERT INTO files (token, path, lines, bytes, hidden) VALUES (?, ?, ?, ?, 0)')
              .bind(token, file.path, file.lines, file.bytes)
          )
        );
      }
      return { token, files };
    },

    async listMemos() {
      const result = await db
        .prepare('SELECT token, name, created_at, updated_at FROM memos ORDER BY updated_at DESC, created_at DESC')
        .all();
      return (result.results ?? []).map((row) => ({
        token: String(row.token),
        name: String(row.name),
        createdAt: String(row.created_at),
        updatedAt: String(row.updated_at),
      }));
    },

    async getMemo(token) {
      const row = await db
        .prepare('SELECT token, name, created_at, updated_at FROM memos WHERE token = ?')
        .bind(token)
        .first();
      if (!row) {
        return null;
      }
      return {
        token: String(row.token),
        name: String(row.name),
        createdAt: String(row.created_at),
        updatedAt: String(row.updated_at),
      };
    },

    async listFiles(token, { visibleOnly = false, folderId = null } = {}) {
      const result = await db
        .prepare('SELECT path, lines, bytes, hidden FROM files WHERE token = ? ORDER BY path')
        .bind(token)
        .all();
      let files = (result.results ?? []).map(rowToFile);
      if (visibleOnly) {
        files = files.filter((file) => !file.hidden);
      }
      if (folderId) {
        const folderPaths = await this.getFolderPaths(token, folderId);
        files = files.filter((file) => folderPaths.has(file.path));
      }
      return files;
    },

    async getFile(token, path) {
      const row = await db
        .prepare('SELECT path, lines, bytes, hidden FROM files WHERE token = ? AND path = ?')
        .bind(token, path)
        .first();
      if (!row) {
        return null;
      }
      const object = await bucket.get(fileObjectKey(token, path));
      if (!object) {
        return null;
      }
      return {
        ...rowToFile(row),
        text: await object.text(),
      };
    },

    async setFileHidden(token, path, hidden) {
      const result = await db
        .prepare('UPDATE files SET hidden = ? WHERE token = ? AND path = ?')
        .bind(hidden ? 1 : 0, token, path)
        .run();
      if ((result.meta?.changes ?? 0) > 0) {
        await this.touchMemo(token);
        return true;
      }
      return false;
    },

    async deleteFile(token, path) {
      const result = await db
        .prepare('DELETE FROM files WHERE token = ? AND path = ?')
        .bind(token, path)
        .run();
      if ((result.meta?.changes ?? 0) === 0) {
        return false;
      }
      await bucket.delete(fileObjectKey(token, path));
      await db.prepare('DELETE FROM folder_files WHERE token = ? AND path = ?').bind(token, path).run();
      await this.touchMemo(token);
      return true;
    },

    async createFolder(token, name) {
      const folders = await this.listFolders(token);
      let folderId = slugify(name);
      const existing = new Set(folders.map((folder) => folder.id));
      let suffix = 2;
      while (existing.has(folderId)) {
        folderId = `${slugify(name)}-${suffix}`;
        suffix += 1;
      }
      await db
        .prepare('INSERT INTO folders (token, folder_id, name, created_at) VALUES (?, ?, ?, ?)')
        .bind(token, folderId, name, new Date().toISOString())
        .run();
      await this.touchMemo(token);
      return folderId;
    },

    async listFolders(token) {
      const foldersResult = await db
        .prepare('SELECT folder_id, name, created_at FROM folders WHERE token = ? ORDER BY name, folder_id')
        .bind(token)
        .all();
      const pathsResult = await db
        .prepare('SELECT folder_id, path FROM folder_files WHERE token = ? ORDER BY folder_id, path')
        .bind(token)
        .all();
      const mapping = new Map();
      for (const row of foldersResult.results ?? []) {
        mapping.set(String(row.folder_id), {
          id: String(row.folder_id),
          name: String(row.name),
          createdAt: String(row.created_at),
          paths: [],
        });
      }
      for (const row of pathsResult.results ?? []) {
        const folder = mapping.get(String(row.folder_id));
        if (folder) {
          folder.paths.push(String(row.path));
        }
      }
      return [...mapping.values()];
    },

    async assignFilesToFolder(token, folderId, paths) {
      const validPaths = new Set((await this.listFiles(token)).map((file) => file.path));
      const statements = [];
      for (const path of paths) {
        if (!validPaths.has(path)) {
          continue;
        }
        statements.push(
          db
            .prepare('INSERT OR IGNORE INTO folder_files (token, folder_id, path) VALUES (?, ?, ?)')
            .bind(token, folderId, path)
        );
      }
      if (statements.length > 0) {
        await db.batch(statements);
        await this.touchMemo(token);
      }
      return statements.length;
    },

    async getFolder(token, folderId) {
      const folders = await this.listFolders(token);
      return folders.find((folder) => folder.id === folderId) ?? null;
    },

    async getFolderPaths(token, folderId) {
      const result = await db
        .prepare('SELECT path FROM folder_files WHERE token = ? AND folder_id = ? ORDER BY path')
        .bind(token, folderId)
        .all();
      return new Set((result.results ?? []).map((row) => String(row.path)));
    },

    async touchMemo(token) {
      await db.prepare('UPDATE memos SET updated_at = ? WHERE token = ?').bind(new Date().toISOString(), token).run();
    },
  };
}

export function createMemoryStorage() {
  const memos = new Map();

  return {
    async ingestZip({ name, filename, zipBytes }) {
      const files = parseZipArchive(zipBytes);
      if (files.length === 0) {
        throw new Error('ZIP に読み取れるテキストファイルがありません。');
      }
      const token = generateToken();
      const now = new Date().toISOString();
      memos.set(token, {
        token,
        name,
        createdAt: now,
        updatedAt: now,
        rawZipKey: rawZipObjectKey(token, filename),
        files: new Map(files.map((file) => [file.path, { ...file, hidden: false }])),
        folders: new Map(),
      });
      return { token, files };
    },

    async listMemos() {
      return [...memos.values()]
        .map((memo) => ({ token: memo.token, name: memo.name, createdAt: memo.createdAt, updatedAt: memo.updatedAt }))
        .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    },

    async getMemo(token) {
      const memo = memos.get(token);
      return memo
        ? { token: memo.token, name: memo.name, createdAt: memo.createdAt, updatedAt: memo.updatedAt }
        : null;
    },

    async listFiles(token, { visibleOnly = false, folderId = null } = {}) {
      const memo = memos.get(token);
      if (!memo) {
        return [];
      }
      let files = [...memo.files.values()].map((file) => ({
        path: file.path,
        lines: file.lines,
        bytes: file.bytes,
        hidden: Boolean(file.hidden),
      }));
      if (visibleOnly) {
        files = files.filter((file) => !file.hidden);
      }
      if (folderId) {
        const folder = memo.folders.get(folderId);
        const scoped = new Set(folder?.paths ?? []);
        files = files.filter((file) => scoped.has(file.path));
      }
      return files.sort((left, right) => left.path.localeCompare(right.path));
    },

    async getFile(token, path) {
      const memo = memos.get(token);
      const file = memo?.files.get(path);
      if (!file) {
        return null;
      }
      return { ...file, hidden: Boolean(file.hidden) };
    },

    async setFileHidden(token, path, hidden) {
      const memo = memos.get(token);
      const file = memo?.files.get(path);
      if (!file) {
        return false;
      }
      file.hidden = Boolean(hidden);
      memo.updatedAt = new Date().toISOString();
      return true;
    },

    async deleteFile(token, path) {
      const memo = memos.get(token);
      if (!memo || !memo.files.delete(path)) {
        return false;
      }
      for (const folder of memo.folders.values()) {
        folder.paths = folder.paths.filter((item) => item !== path);
      }
      memo.updatedAt = new Date().toISOString();
      return true;
    },

    async createFolder(token, name) {
      const memo = memos.get(token);
      if (!memo) {
        throw new Error('memo not found');
      }
      let folderId = slugify(name);
      let suffix = 2;
      while (memo.folders.has(folderId)) {
        folderId = `${slugify(name)}-${suffix}`;
        suffix += 1;
      }
      memo.folders.set(folderId, { id: folderId, name, createdAt: new Date().toISOString(), paths: [] });
      memo.updatedAt = new Date().toISOString();
      return folderId;
    },

    async listFolders(token) {
      const memo = memos.get(token);
      if (!memo) {
        return [];
      }
      return [...memo.folders.values()].map((folder) => ({ ...folder, paths: [...folder.paths] }));
    },

    async assignFilesToFolder(token, folderId, paths) {
      const memo = memos.get(token);
      const folder = memo?.folders.get(folderId);
      if (!memo || !folder) {
        return 0;
      }
      const valid = paths.filter((path) => memo.files.has(path));
      folder.paths = [...new Set([...folder.paths, ...valid])].sort();
      memo.updatedAt = new Date().toISOString();
      return valid.length;
    },

    async getFolder(token, folderId) {
      const memo = memos.get(token);
      const folder = memo?.folders.get(folderId);
      return folder ? { ...folder, paths: [...folder.paths] } : null;
    },

    async getFolderPaths(token, folderId) {
      const folder = await this.getFolder(token, folderId);
      return new Set(folder?.paths ?? []);
    },

    async touchMemo(token) {
      const memo = memos.get(token);
      if (memo) {
        memo.updatedAt = new Date().toISOString();
      }
    },
  };
}
