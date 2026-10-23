# ComfyUI Wildcard Sync (`comfyui_wildcard_sync`)

A lightweight ComfyUI custom node that exposes HTTP endpoints to keep Impact Pack wildcard files in sync with an external orchestrator or editor.

## Features

- **Atomic folder swap:** Replaces the managed wildcard folder atomically in a single operation. A failed or interrupted sync will never leave wildcards half-written or corrupted.
- **Order-independent content hashing:** `GET /wildcard_sync/hash` returns a deterministic SHA-256 digest of all managed `.txt` files so clients can avoid redundant transfers.
- **Automatic cache refresh:** Triggers `impact.wildcards.wildcard_load()` on sync so wildcards take effect immediately without restarting ComfyUI.
- **Security & path sandboxing:**
  - Prevents directory traversal (`..`, absolute paths, drive letters).
  - Strict segment validation rejecting reserved Windows device names (`CON`, `PRN`, `NUL`, etc.) and unsafe characters.
  - Limits file types strictly to `.txt`.
  - Enforces payload size limits and maximum file counts.
- **Optional token authentication:** If `VP_WILDCARD_TOKEN` is set in the server environment, incoming requests must supply a matching `X-VP-Token` header. If unset, requests are permitted (suitable for trusted private LANs).

## Installation

Clone this repository into your ComfyUI `custom_nodes` directory:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/SmartyShark3/comfyui_wildcard_sync.git
```

Restart ComfyUI. You will see:
```text
[wildcard_sync] routes registered: GET /wildcard_sync/hash, PUT /wildcard_sync
```

## Configuration

| Environment Variable | Description | Default |
|---|---|---|
| `VP_WILDCARD_TOKEN` | Shared secret token required in `X-VP-Token` header. | Unset (authentication disabled). |
| `WILDCARD_SYNC_DIR` | Absolute path to folder where wildcards are stored. | Impact Pack's `custom_wildcards` directory. |

## HTTP API

### 1. `GET /wildcard_sync/hash`
Calculates and returns the SHA-256 hash of all managed `.txt` files in the managed directory.

**Headers:**
- `X-VP-Token: <token>` *(required if `VP_WILDCARD_TOKEN` is set)*

**Response (`200 OK`):**
```json
{
  "hash": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
  "files": 12,
  "version": "0.1.0"
}
```

### 2. `PUT /wildcard_sync`
Replaces the managed wildcard directory with the provided files, and reloads Impact Pack wildcards.

**Headers:**
- `Content-Type: application/json`
- `X-VP-Token: <token>` *(required if `VP_WILDCARD_TOKEN` is set)*

**Request Body:**
```json
{
  "files": [
    {
      "path": "actress.txt",
      "content": "Alice\nBob\nCharlie\n"
    },
    {
      "path": "hairstyles/short.txt",
      "content": "pixie cut\nbob\nbuzz cut\n"
    }
  ],
  "hash": "optional_precomputed_sha256_hash"
}
```

**Response (`200 OK`):**
```json
{
  "hash": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
  "files": 2,
  "refreshed": true
}
```

## Running Tests

From the repository directory:
```bash
pytest
```

## License

MIT
