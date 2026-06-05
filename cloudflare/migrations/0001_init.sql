CREATE TABLE IF NOT EXISTS memos (
  token TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw_zip_key TEXT
);

CREATE TABLE IF NOT EXISTS files (
  token TEXT NOT NULL,
  path TEXT NOT NULL,
  lines INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  hidden INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (token, path),
  FOREIGN KEY (token) REFERENCES memos(token) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS folders (
  token TEXT NOT NULL,
  folder_id TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (token, folder_id),
  FOREIGN KEY (token) REFERENCES memos(token) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS folder_files (
  token TEXT NOT NULL,
  folder_id TEXT NOT NULL,
  path TEXT NOT NULL,
  PRIMARY KEY (token, folder_id, path),
  FOREIGN KEY (token, folder_id) REFERENCES folders(token, folder_id) ON DELETE CASCADE,
  FOREIGN KEY (token, path) REFERENCES files(token, path) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_files_token_hidden ON files (token, hidden, path);
CREATE INDEX IF NOT EXISTS idx_folder_files_token_folder ON folder_files (token, folder_id, path);
