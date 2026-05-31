import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = BASE_DIR / "workspace"

# ZIP ingest limits (override with environment variables when needed)
MAX_ZIP_BYTES = int(os.getenv("MAX_ZIP_BYTES", str(50 * 1024 * 1024)))
MAX_ZIP_ENTRIES = int(os.getenv("MAX_ZIP_ENTRIES", "10000"))
MAX_UNCOMPRESSED_BYTES = int(os.getenv("MAX_UNCOMPRESSED_BYTES", str(200 * 1024 * 1024)))
MAX_WORKSPACE_ALLOCATION_RETRIES = int(os.getenv("MAX_WORKSPACE_ALLOCATION_RETRIES", "5"))
