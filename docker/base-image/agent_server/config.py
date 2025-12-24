"""
Configuration constants for the agent server.
"""
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

# CORS settings
CORS_ORIGINS = ["*"]

# Thread pool for non-blocking subprocess execution
EXECUTOR = ThreadPoolExecutor(max_workers=4)

# Lock for thread-safe state updates
STATE_LOCK = threading.Lock()

# Workspace paths
WORKSPACE_DIR = Path("/home/developer")
PLANS_ACTIVE_DIR = WORKSPACE_DIR / "plans" / "active"
PLANS_ARCHIVE_DIR = WORKSPACE_DIR / "plans" / "archive"
TRINITY_DIR = WORKSPACE_DIR / ".trinity"
CLAUDE_COMMANDS_DIR = WORKSPACE_DIR / ".claude" / "commands" / "trinity"
TRINITY_META_PROMPT_DIR = Path("/trinity-meta-prompt")

# File size limits
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100MB

# Claude Code defaults
DEFAULT_CONTEXT_WINDOW = 200000

# Git configuration
GIT_TIMEOUT_SECONDS = 60

# Trinity injection marker
TRINITY_SECTION_MARKER = "## Trinity Planning System"
