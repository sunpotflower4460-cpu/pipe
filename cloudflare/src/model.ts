export type FileItem = {
  path: string;
  lines: number;
  bytes: number;
};

export type MemoItem = {
  id: string;
  name: string;
  createdAt: string;
  updatedAt: string;
  files: FileItem[];
};
