# Cloud Deployment Guide

Code Relay can run as a long-lived cloud web app (not only local + tunnel).

## Required environment variables

At minimum, configure these values:

```bash
WORKSPACE_DIR=/var/data/workspace
TOKENS_PATH=/var/data/tokens.json
BASE_PUBLIC_URL=https://your-code-relay.example.com
DEFAULT_TOKEN_TTL_DAYS=7
```

- `WORKSPACE_DIR`: where ingested repositories/ZIP contents are stored.
- `TOKENS_PATH`: token metadata file location.
- `BASE_PUBLIC_URL`: public base URL used in `/ingest` and `/ingest-repo` response links.
- `DEFAULT_TOKEN_TTL_DAYS`: default token expiration days.
- `ADMIN_KEY` (optional): reserved for admin-protected operations.

## Startup command (PORT support)

Run the app with the cloud-provided `PORT`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

For local fallback:

```bash
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

## Persistent storage is mandatory

Cloud instances may lose local filesystem data on restart/redeploy.
Always mount persistent storage and place both `WORKSPACE_DIR` and `TOKENS_PATH` on it.

## Platform notes

### Render

- Deploy as a Web Service.
- Set start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Attach a Persistent Disk and point:
  - `WORKSPACE_DIR` to disk path (example: `/var/data/workspace`)
  - `TOKENS_PATH` to disk path (example: `/var/data/tokens.json`)

### Railway

- Deploy as a service with a Volume.
- Mount the Volume and set `WORKSPACE_DIR`/`TOKENS_PATH` under that mount path.
- Keep `BASE_PUBLIC_URL` set to your Railway public domain/custom domain.

### Fly.io

- Create and mount a Volume.
- Set `WORKSPACE_DIR`/`TOKENS_PATH` into the mounted path.
- Expose service URL in `BASE_PUBLIC_URL`.

### Vercel

Vercel serverless functions alone are not suitable for this filesystem-persistent workflow.
If deploying on Vercel, use external persistent storage/database for workspace and token state.
