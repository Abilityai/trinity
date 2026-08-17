# Feature: Web Chat File Upload

## Overview
Adds drag-and-drop / file-picker to authenticated chat (ChatPanel.vue) and public chat (PublicChat.vue), encoding files as base64 in the JSON request body and reusing the same validation/write infrastructure as the Telegram/Slack/WhatsApp channel adapters.

## User Story
As a user chatting with an agent, I want to attach files to my message so that the agent can visually analyze images or read text/CSV/JSON files I provide.

## Entry Points
- **UI (auth)**: `src/frontend/src/components/chat/ChatInput.vue` — paperclip button / drag-and-drop on wrapper div
- **UI (public)**: `src/frontend/src/views/PublicChat.vue:745` — same ChatInput component embedded in public chat
- **API (auth)**: `POST /api/agents/{name}/task`
- **API (public)**: `POST /api/public/chat/{token}`

## Frontend Layer

### Components

**`src/frontend/src/components/chat/ChatInput.vue`**

Key state (lines 272-274):
- `pendingFiles` ref — `[{name, mimetype, size, data_base64}]`
- `fileInputRef` ref — hidden `<input type="file">`
- `dragOver` ref — boolean for drag-over highlight

Client-side limits (lines 226-227):
```javascript
const MAX_FILES = 3
const MAX_FILE_BYTES = 5 * 1024 * 1024  // 5 MB
```

File encoding (lines 404-425):
```javascript
function encodeFile(file) {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onload = (e) => resolve(e.target.result)  // data: URI
    reader.readAsDataURL(file)
  })
}

async function addFiles(fileList) {
  // Reads each file via FileReader.readAsDataURL → data: URI string
  // Parses MIME from data: URI prefix (not file.type — unreliable cross-browser)
  // Checks size <= MAX_FILE_BYTES; alerts and skips oversized files
  pendingFiles.value.push({ name, mimetype, size, data_base64 })
}
```

Drop / picker handlers (lines 428-442):
- `onFileInputChange` — triggered by hidden `<input>`; resets input value to allow re-selection
- `onDrop` — handles `@drop.prevent` on wrapper div; delegates to `addFiles`
- `removeFile(idx)` — removes chip from preview list

Submit (lines 445-457):
```javascript
function handleSubmit() {
  emit('submit', localMessage.value.trim(), [...pendingFiles.value])
  pendingFiles.value = []
}
```

**`src/frontend/src/components/ChatPanel.vue:606`**
```javascript
const sendMessage = async (userMessage, files = []) => {
  const payload = {
    message: contextPrompt,
    async_mode: true,
    files: files.length > 0 ? files : undefined,
    // ...other fields
  }
  // POST /api/agents/{name}/task
}
```

**`src/frontend/src/views/PublicChat.vue:745`**
```javascript
const sendMessage = async (userMessage, files = []) => {
  const payload = {
    message: userMessage,
    async_mode: true,
    files: files.length > 0 ? files : undefined,
  }
  // POST /api/public/chat/{token}
}
```

### nginx
`src/frontend/nginx.conf:8`:
```nginx
client_max_body_size 25m;
```
Required because 3 files × 5 MB each, base64-encoded in JSON, is approximately 21 MB. Without this directive nginx silently rejects the request with 413 before it reaches the backend.

## Backend Layer

### Models

**`src/backend/db_models.py:430`** — `WebFileUpload`:
```python
class WebFileUpload(BaseModel):
    name: str
    mimetype: str
    size: int
    data_base64: str  # raw base64 or data: URI from FileReader.readAsDataURL()
```

**`src/backend/db_models.py:444`** — `PublicChatRequest.files`:
```python
files: Optional[List[WebFileUpload]] = None  # (#364)
```

**`src/backend/models.py:99`** — `ParallelTaskRequest.files`:
```python
files: Optional[List[WebFileUpload]] = None  # (#364)
```

### Endpoints

**Authenticated chat** — `src/backend/routers/chat.py:858-894` (`execute_parallel_task`):
- File processing block runs synchronously before the async/sync fork
- `session_id` is `str(current_user.id)` (stable per user, scopes the upload directory)
- `uploader` is `current_user.email or current_user.username`
- On `all_writes_failed=True` → `502` with `"File upload failed: could not write to agent workspace."`
- Appends file descriptions to `request.message` as a newline-joined block

**Public chat** — `src/backend/routers/public.py:493-527` (`public_chat`):
- Same pattern; `session_id` is `session_identifier` (the anonymous token or email)
- `uploader` is `verified_email or f"anonymous ({client_ip})"`
- File descriptions appended to `context_prompt` (not `chat_request.message`) so the stored user message doesn't contain the injection block
- Images passed as `images=_pub_image_data` to both `_execute_public_chat_background` and `execute_task`

### Shared Upload Service

**`src/backend/services/upload_service.py`** — extracted from `adapters/message_router._handle_file_uploads()`.

**`decode_web_file(f: dict) -> Optional[bytes]`** (line 345):
- Strips `data:mime;base64,` prefix from FileReader output
- Falls back to raw base64 if no prefix present
- Returns `None` on decode failure

**`sanitize_filename(name, file_id, used_names) -> str`** (line 62):
- NFKC normalize → `os.path.basename` → strip unsafe chars via `[^\w.\-()]` regex
- Rejects hidden filenames starting with `.` (e.g., `.env`, `.gitignore`)
- Truncates to 200 chars (preserves extension up to 16 chars)
- Collision dedup: appends `-1`, `-2`, … suffix

**`process_file_uploads(raw_files, agent_name, container, session_id, uploader, source, ...) -> (descriptions, upload_dir, all_writes_failed, image_data)`** (line 119):

```
for each file (up to max_files):
  1. sanitize_filename() — NFKC, path traversal, dedup
  2. Reject unsupported MIME categories (PDF, ZIP, TAR, video/, audio/)
  3. Actual size check against declared limit (TOCTOU defense)
  4. Magic-byte MIME validation via python-magic (graceful fallback):
     - Image MIME mislabel (JPEG vs PNG) → accept with detected MIME
     - text/plain vs text/csv → accept
     - Other mismatch → reject with "file type mismatch"
  5a. If image → base64-encode → append to image_data list (vision blocks)
  5b. If non-image → container mkdir -p + put_archive to /home/developer/uploads/{session_id}/
  6. Emit platform_audit_service.log(event_type=EXECUTION, event_action="file_upload")
```

Returns:
- `descriptions` — list of context strings injected into the agent prompt
- `upload_dir` — container path for cleanup, or `None` if no non-image writes occurred
- `all_writes_failed` — `True` when at least one write was attempted but all failed
- `image_data` — `[{"media_type": str, "data": base64_str}]` for `execute_task(images=...)`

**Constants** (lines 35-43):
| Constant | Value |
|----------|-------|
| `WEB_MAX_FILES` | 3 |
| `WEB_MAX_FILE_SIZE` | 5 MB |
| `WEB_MAX_IMAGE_SIZE` | 5 MB |
| `WEB_MAX_TOTAL_IMAGE_SIZE` | 10 MB |
| `CHANNEL_MAX_FILES` | 10 |
| `CHANNEL_MAX_FILE_SIZE` | 10 MB |

### Business Logic

1. Frontend encodes file via `FileReader.readAsDataURL` → `data: URI`
2. `decode_web_file()` strips prefix → raw bytes
3. `process_file_uploads()` validates, MIME-checks, and routes:
   - Images → base64 collected in `image_data` list
   - Non-images → written to `/home/developer/uploads/{session_id}/{filename}` in the running container via Docker `put_archive`
4. File descriptions appended to the message text
5. `task_execution_service.execute_task(images=image_data)` invokes Claude Code with `--input-format stream-json`, delivering images as vision content blocks (not embedded text)
6. Text files are readable by the agent at `/home/developer/uploads/{session_id}/`

### Docker Operations
- `container_exec_run(container, f"mkdir -p {upload_dir}", user="developer")` — create upload directory
- `container_put_archive(container, upload_dir, tar_bytes)` — write file into container via tar stream with uid/gid 1000, mode 0o644

## Agent Layer

Images are passed through to Claude Code via `--input-format stream-json` as content blocks. There is no agent-server endpoint involved; the backend writes files directly into the agent container (Docker SDK) and passes image bytes through the existing `execute_task` path.

## Side Effects

- **Audit log**: `platform_audit_service.log(event_type=EXECUTION, event_action="file_upload")` fires once per successfully processed file; records filename, size, MIME, storage type (`stream_json_vision` for images, `container_file` for non-images), uploader, sender_id, channel_id, agent_name
- **No WebSocket broadcast** specific to file uploads; the enclosing chat execution broadcasts normally via activity tracking

## Error Handling

| Error Case | HTTP Status | Detail |
|------------|-------------|--------|
| All non-image writes fail | 502 | `"File upload failed: could not write to agent workspace."` |
| Single file rejected (size) | — | Description appended to prompt: `"{name} — rejected (exceeds X limit)"` |
| Single file rejected (MIME mismatch) | — | Description: `"{name} — rejected (file type mismatch)"` |
| Unsupported format (PDF, tar/gzip/rar, video, audio) | — | Description: `"{name} — unsupported format ({mime}). Text, CSV, JSON, ZIP, and image files are supported."` (ZIP allowed since 2026-08) |
| File count exceeds max | — | Description: `"({n} more file(s) skipped — max {max} per message)"` |
| Total image size exceeded | — | Description: `"{name} — skipped (total image size limit reached)"` |
| Client-side oversized file | — | `alert()` shown in browser, file not added to `pendingFiles` |
| nginx body too large | 413 | nginx rejects before backend (prevented by `client_max_body_size 25m`) |
| base64 decode failure | — | `decode_web_file()` returns `None`; file processed as download-failed |

## Complete Flow Diagram

```
User selects/drops files in ChatInput.vue
  → FileReader.readAsDataURL → data: URI string
  → addFiles() checks size <= 5 MB, parses MIME from URI prefix
  → pendingFiles [{name, mimetype, size, data_base64}]
  → handleSubmit() emits ('submit', message, files[])
    ↓
ChatPanel.vue sendMessage(msg, files)          PublicChat.vue sendMessage(msg, files)
  payload = { message, files, async_mode, … }   payload = { message, files, async_mode, … }
  POST /api/agents/{name}/task                   POST /api/public/chat/{token}
    ↓
nginx (client_max_body_size 25m)
    ↓
routers/chat.py:execute_parallel_task           routers/public.py:public_chat
  line 858-894                                    line 493-527
  decode_web_file() — strips data: URI prefix
  process_file_uploads() [services/upload_service.py]
    ↓
upload_service.process_file_uploads():
  for each file (max 3):
    sanitize_filename() — unicode NFKC, path traversal, dedup
    reject unsupported MIME (PDF, ZIP, video, audio)
    size check vs WEB_MAX_FILE_SIZE / WEB_MAX_IMAGE_SIZE
    magic-byte MIME validation (python-magic; fallback graceful)
    if image:
      base64-encode → append to image_data list
      audit log: storage=stream_json_vision
    if non-image:
      docker exec mkdir -p /home/developer/uploads/{session_id}/
      docker put_archive → file written to container
      audit log: storage=container_file
  returns (descriptions, upload_dir, all_writes_failed, image_data)
    ↓
router appends descriptions to message text
router calls task_execution_service.execute_task(images=image_data)
    ↓
execute_task → claude code --input-format stream-json
  images delivered as vision content blocks (not text data URIs)
  text files readable at /home/developer/uploads/{session_id}/
```

## Testing

### Prerequisites
- Backend running with Docker socket access
- At least one running agent container
- python-magic installed (or tests confirm graceful fallback path)

### Unit Tests
`tests/unit/test_web_file_upload.py` — 17 unit tests covering:
- `sanitize_filename`: path traversal, unicode normalization, hidden-file rejection, truncation, collision dedup
- `decode_web_file`: data: URI prefix stripping, raw base64, empty input
- `process_file_uploads`: size limits, unsupported MIME gating, image dispatch, non-image container write, `all_writes_failed` detection

### Integration Test Steps

1. **Drag an image onto the chat input**
   Expected: preview chip appears above textarea with filename; chip is removable
   Verify: `pendingFiles` has one entry with `mimetype` parsed from data: URI

2. **Submit message with image attachment**
   Expected: POST payload includes `files` array; image appears in agent context as a vision block
   Verify: Claude Code receives image via `--input-format stream-json`; agent can describe image content

3. **Attach a text file (`.csv`)**
   Expected: file written to `/home/developer/uploads/{session_id}/file.csv` in container
   Verify: `docker exec {container} ls /home/developer/uploads/` shows the file

4. **Exceed 5 MB limit client-side**
   Expected: `alert()` shown; file not added to pending list

5. **Attempt to attach a PDF**
   Expected: file sent to backend; rejected by `process_file_uploads` with unsupported format description injected into prompt

6. **Exceed 3-file limit**
   Expected: only first 3 files processed; overflow count injected into prompt

7. **Public chat file upload**
   Expected: same behavior via `POST /api/public/chat/{token}`; `uploader` shows email or anonymous IP

## Related Flows
- [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) — ChatPanel context and session management
- [public-agent-links.md](feature-flows/public-agent-links.md) — public chat route and session handling
- [slack-file-sharing.md](feature-flows/slack-file-sharing.md) — original channel adapter file upload (shared infrastructure)
- [telegram-integration.md](feature-flows/telegram-integration.md) — Telegram file upload Phase 1/2 (shared `process_file_uploads`)
- [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) — `execute_task` and `--input-format stream-json`
- [audit-trail.md](feature-flows/audit-trail.md) — platform_audit_service used for file upload events
