import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = BASE_DIR / "workspace"
TOKENS_FILE = BASE_DIR / "tokens.json"

# ZIP ingest limits (override with environment variables when needed)
MAX_ZIP_BYTES = int(os.getenv("MAX_ZIP_BYTES", str(50 * 1024 * 1024)))
MAX_ZIP_ENTRIES = int(os.getenv("MAX_ZIP_ENTRIES", "10000"))
MAX_UNCOMPRESSED_BYTES = int(os.getenv("MAX_UNCOMPRESSED_BYTES", str(200 * 1024 * 1024)))
MAX_WORKSPACE_ALLOCATION_RETRIES = int(os.getenv("MAX_WORKSPACE_ALLOCATION_RETRIES", "5"))
TOKEN_TTL_DAYS = int(os.getenv("TOKEN_TTL_DAYS", "7"))

# /t/{token}/file defaults
DEFAULT_FILE_FROM = int(os.getenv("DEFAULT_FILE_FROM", "1"))
DEFAULT_FILE_TO = int(os.getenv("DEFAULT_FILE_TO", "600"))
MAX_FILE_RESPONSE_LINES = int(os.getenv("MAX_FILE_RESPONSE_LINES", "800"))
