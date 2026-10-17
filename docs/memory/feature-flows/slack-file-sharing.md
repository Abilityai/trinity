# Feature: Slack Inbound File Sharing (SLACK-FILES)

## Overview

Slack users can upload files (images, text, CSV, JSON, etc.) alongside messages. All accepted attachments are saved as original bytes in the agent container. Images are also passed as vision data to the selected runtime (Claude content blocks or Codex native `--image` options).

## User Story

As a Slack user chatting with a Trinity agent, I want to upload files so the agent can analyze images, read documents, and process data files.

## Entry Points

- **Slack**: User uploads file in DM, @mention, or thread reply
- **Transport**: Socket Mode or webhook delivers event with `files` array

## Flow

```
Slack event (message with files[])
  ↓
SlackAdapter.parse_message()
  → _extract_files(): id, name, mimetype, size, url
  → NormalizedMessage with files: [FileAttachment, ...]
  ↓
ChannelMessageRouter._handle_message_inner()
  ↓
Step 3b: File upload rate limit (5/min per user)
  ↓
Step 7b: _handle_file_uploads(adapter, message, agent_name, container, session_id)
  → downloads each file via adapter.download_file() → assembles raw_files list
  → delegates to services/upload_service.process_file_uploads() (#364)
  ↓
process_file_uploads() for each file:
  ├── Unsupported format? → skip with message
  ├── sanitize_filename(): NFKC + basename + safe-chars + 200-char
  │     truncation + collision dedup (-1, -2, …) (#487)
  ├── size check against limit
  ├── magic-byte MIME validation (python-magic, graceful fallback)
  ├── Every accepted attachment → tar archive → container_put_archive to uploads/{dispatch_uuid}/
  │           → "[File uploaded by {uploader}]: name (size) saved to {path}"
  └── Image with successful storage? → base64 vision data → image_data list
  ↓
If every workspace write attempt failed → reply
  "Sorry, I couldn't save the file(s) you sent…" and abort execution (#487 AC6)
  ↓
Context prompt += per-file lines (no extra header — each line self-describes)
  ↓
Step 9: TaskExecutionService.execute_task()
  - Images: vision data passed to the selected runtime
  - Saved attachments, including images: Read added to allowed_tools
  ↓
Finally: when execution is known terminal (or was not dispatched),
  _cleanup_uploads() → rm -rf uploads/{dispatch_uuid}/
  Active or unknown execution state → retain originals for operator cleanup
```

## Frontend Layer

No frontend changes. File handling is entirely backend (Slack → adapter → router → agent).

## Backend Layer

### Models (`adapters/base.py`)
- `FileAttachment`: id, name, mimetype, size, url
- `NormalizedMessage.files`: List[FileAttachment] (default empty)
- `ChannelAdapter.download_file()`: abstract method for channel-specific downloads

### Slack Adapter (`adapters/slack_adapter.py`)
- `_extract_files(event)`: parses Slack `files` array into FileAttachment list
- `_parse_dm`, `_parse_mention`, `_parse_thread_reply`: all extract files, accept file-only messages with `"(file upload)"` placeholder
- `download_file()`: calls `slack_service.download_file()` with bot token

### Message Router (`adapters/message_router.py`)
- Step 3b: File upload rate limit (`_FILE_UPLOAD_RATE_LIMIT_MAX=5`, 60s window)
- Step 7b: `_handle_file_uploads(verified_email=...)` — downloads each file via
  `adapter.download_file()`, then delegates to `upload_service.process_file_uploads()` (#364)
  The inline validation/write logic was extracted to the shared service so web chat
  (ChatPanel, PublicChat) reuses the same path
- Filename sanitization and size formatting live in
  `services/upload_service.sanitize_filename()` / `format_file_size()`
- Chat injection format: `[File uploaded by {uploader}]: {name} ({size}) saved
  to {path}` — `uploader` is the verified email (Issue #311) or
  `adapter.get_source_identifier(message)`
- All-writes-failed handling: when every write attempt fails, reply via
  channel with explicit error and skip agent execution (#487 AC6)
- Cleanup after known terminal outcomes or before dispatch; active or unknown execution state retains originals

### Upload Service (`services/upload_service.py`) — shared since #364
- `process_file_uploads()` — core logic: sanitize, size-check, MIME-validate, image/file dispatch
- `sanitize_filename()`: NFKC normalize → `os.path.basename` → safe-chars → hidden-dotfile
  rejection → 200-char truncation → collision dedup with `-1`, `-2`, … suffix (#487)
- Channel limits: `CHANNEL_MAX_FILES=10`, `CHANNEL_MAX_FILE_SIZE=10MB`
- Image budget: `CHANNEL_MAX_IMAGE_SIZE=5MB`, `CHANNEL_MAX_TOTAL_IMAGE_SIZE=10MB`
- Unsupported MIME rejection: PDF, ZIP, tar, gzip, rar, video/*, audio/*
- Magic-byte MIME validation via python-magic (graceful fallback if unavailable)
- Storage directory uses a generated UUID (`uuid.uuid4().hex`), not caller-supplied session text, for shell safety and dispatch isolation

### Slack Service (`services/slack_service.py`)
- `download_file(bot_token, url, max_size)`: GET with Authorization header, follow redirects, 10MB cap
- OAuth scopes: `files:read` added (requires workspace reinstall)

### Docker Utils (`services/docker_utils.py`)
- `container_put_archive(container, path, data)`: async wrapper for Docker `put_archive`

## Side Effects

- Original attachment bytes written to a unique agent-container directory at `/home/developer/uploads/{dispatch_uuid}/`
- The router attempts removal after a known terminal outcome or before dispatch. Active or unknown execution state retains originals for operator cleanup; cleanup errors can also retain files. See [Codex uploaded images](codex-runtime.md#uploaded-images) for cancellation and retention details
- Rate limit buckets tracked in memory (pruned every 5 min)

## Error Handling

| Condition | Behavior |
|-----------|----------|
| No `files:read` scope | Download returns HTML instead of file → logged as download failure |
| File too large | Skipped with description in prompt |
| Unsupported format (PDF, etc.) | Skipped with user-friendly message |
| Download fails | Logged, description says "download failed" |
| Container copy fails | Logged, description prefixed `[File upload failed]:` |
| All container writes fail | Channel reply with explicit error, agent execution skipped (#487 AC6) |
| Image budget exceeded | Remaining images skipped with note |
| >10 files | Excess truncated with count message |
| Path traversal filename | Sanitized via NFKC + basename + safe-chars regex |
| Filename collision in batch | De-duped with `-1`, `-2`, … suffix |
| File upload rate limited | Slack user gets "uploading too quickly" message |

## Security Considerations

- **Path traversal**: `../../.env` → NFKC normalize (defeats fullwidth-encoded
  `．．／` variants) → `os.path.basename` → safe-chars regex → hidden-dotfile
  rejection (#487)
- **Filename length**: capped at 200 chars, extension preserved
- **Filename collision**: per-message dedup with `-1`, `-2`, … suffix (#487)
- **Shell injection**: Storage and cleanup paths use a generated dispatch UUID rather than caller-supplied session text
- **Allowed tools escalation**: `Read` is added for saved attachments, including image originals. Images also receive vision data. Agent can still read `.env` with Read — accepted trade-off for now.
- **Rate limiting**: Separate file upload rate limit (5/min) in addition to message rate limit (30/min)
- **Size limits**: 5MB/image inline, 10MB/file container, 10MB total images, max 10 files
- **Audit trail**: Every successful upload logs `dest_path`, `storage`, and
  `uploader` (verified email or channel-native identity) via
  `platform_audit_service` (#487)

## Testing

### Unit Tests (`tests/unit/test_slack_file_uploads.py`)
36 tests covering:
- Filename sanitization (traversal, hidden files, special chars, empty)
- File type routing (image, text, unsupported)
- Slack event file extraction
- Size limits and file count caps
- Format file size helper
- Per-session directory naming

### Manual Tests
- [x] Image upload via Slack → agent describes image content
- [x] Text file via API → agent reads and summarizes
- [x] Text message without files → works unchanged
- [x] PDF upload → rejected with user message

**Last Tested**: 2026-03-31
**Status**: ✅ Working

## Related Flows

- [slack-channel-routing.md](slack-channel-routing.md) — Channel adapter abstraction (SLACK-002)
- [slack-integration.md](slack-integration.md) — Original Slack integration (SLACK-001)
- [web-chat-file-upload.md](web-chat-file-upload.md) — Web chat file upload (ChatPanel + PublicChat), uses same shared upload_service (#364)
