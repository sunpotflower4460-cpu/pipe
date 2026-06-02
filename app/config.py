import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_DIR", str(BASE_DIR / "workspace")))
TOKENS_FILE = Path(os.getenv("TOKENS_PATH", str(BASE_DIR / "tokens.json")))
BASE_PUBLIC_URL = os.getenv("BASE_PUBLIC_URL", "").rstrip("/")
_DEFAULT_TTL_ENV = os.getenv("DEFAULT_TOKEN_TTL_DAYS")
_LEGACY_TTL_ENV = os.getenv("TOKEN_TTL_DAYS", "7")
DEFAULT_TOKEN_TTL_DAYS = int(_DEFAULT_TTL_ENV if _DEFAULT_TTL_ENV is not None else _LEGACY_TTL_ENV)
ADMIN_KEY = os.getenv("ADMIN_KEY")

# ZIP ingest limits (override with environment variables when needed)
MAX_ZIP_BYTES = int(os.getenv("MAX_ZIP_BYTES", str(50 * 1024 * 1024)))
MAX_ZIP_ENTRIES = int(os.getenv("MAX_ZIP_ENTRIES", "10000"))
MAX_UNCOMPRESSED_BYTES = int(os.getenv("MAX_UNCOMPRESSED_BYTES", str(200 * 1024 * 1024)))
MAX_WORKSPACE_ALLOCATION_RETRIES = int(os.getenv("MAX_WORKSPACE_ALLOCATION_RETRIES", "5"))
TOKEN_TTL_DAYS = DEFAULT_TOKEN_TTL_DAYS

# /t/{token}/index defaults
MAX_INDEX_RESPONSE_FILES = int(os.getenv("MAX_INDEX_RESPONSE_FILES", "500"))

# /t/{token}/file defaults
DEFAULT_FILE_FROM = int(os.getenv("DEFAULT_FILE_FROM", "1"))
DEFAULT_FILE_TO = int(os.getenv("DEFAULT_FILE_TO", "600"))
MAX_FILE_RESPONSE_LINES = int(os.getenv("MAX_FILE_RESPONSE_LINES", "800"))
