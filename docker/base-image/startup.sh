#!/bin/bash

echo "Starting Trinity Agent (Secure Mode)..."

# === Guardrails: render runtime config (GUARD-002/003) ===
# Merge the image-baked baseline with the AGENT_GUARDRAILS env var (if set)
# and write the result to /opt/trinity/guardrails-runtime.json as root, 0444.
# The agent user cannot rewrite this file at runtime, so hook rules cannot
# be disabled by the agent itself.
if [ -f /opt/trinity/hooks/write-runtime-config.py ]; then
    sudo -E /usr/bin/python3 /opt/trinity/hooks/write-runtime-config.py || \
        echo "Warning: failed to render guardrails-runtime.json (hooks will fall back to baseline)"
fi

# === Scratch space: ensure TMPDIR exists on the home volume (#1098) ===
# /tmp is a 100 MB RAM tmpfs mounted noexec,nosuid — too small and non-exec for
# pip/npm/build scratch (ML wheels hit "No space left on device"). The backend
# sets TMPDIR to a disk-backed, exec-capable path on the home volume; create it
# (idempotent) so tools that honor TMPDIR land there. Runs as `developer`, the
# UID-1000 owner of /home/developer.
AGENT_TMPDIR="${TMPDIR:-/home/developer/.tmp}"
mkdir -p "${AGENT_TMPDIR}" 2>/dev/null && chmod 700 "${AGENT_TMPDIR}" 2>/dev/null || \
    echo "Warning: could not create TMPDIR ${AGENT_TMPDIR}; scratch will fall back to /tmp"

# Initialize from GitHub repository if specified
if [ -n "${GITHUB_REPO}" ] && [ -n "${GITHUB_PAT}" ]; then
    echo "Initializing agent from GitHub repository: ${GITHUB_REPO}"
    cd /home/developer || exit 1

    # Clone the repository using PAT authentication.
    # TRINITY_GIT_BASE_URL defaults to https://github.com; overridable for
    # self-hosted gitea / GHES / dev harnesses.
    GIT_BASE_URL="${TRINITY_GIT_BASE_URL:-https://github.com}"
    GIT_BASE_URL="${GIT_BASE_URL%/}"
    GIT_HOST_PATH="${GIT_BASE_URL#*://}"
    GIT_SCHEME="${GIT_BASE_URL%%://*}"
    CLONE_URL="${GIT_SCHEME}://oauth2:${GITHUB_PAT}@${GIT_HOST_PATH}/${GITHUB_REPO}.git"

    # Check if GIT_SYNC_ENABLED - if so, keep .git directory for bidirectional sync
    if [ "${GIT_SYNC_ENABLED}" = "true" ]; then
        # Check if repo is already cloned (persistent volume has existing .git)
        if [ -d "/home/developer/.git" ]; then
            # GIT-002: On restart, preserve the current branch state
            # The .git directory persists on the volume, so current branch (in .git/HEAD) is retained
            # We only fetch to update remote refs - no checkout, no branch change
            echo "Repository already exists on persistent volume - skipping clone"
            echo "Running git fetch to sync with remote..."
            cd /home/developer || exit 1
            CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
            echo "Current branch: ${CURRENT_BRANCH} (preserved from previous run)"
            # Update remote URL with current PAT (may have changed since last start)
            if [ -n "${GITHUB_PAT}" ]; then
                git remote set-url origin "${CLONE_URL}"
            fi
            git fetch origin 2>&1 || echo "Note: Could not fetch from remote"

            # #1439: .git exists ⇒ a prior clone succeeded; clear any stale
            # failure marker so health doesn't report a recovered agent unhealthy.
            rm -f /home/developer/.git-clone-status 2>/dev/null || true

            echo "Existing repository ready with persisted files"
        else
            echo "Git sync enabled - cloning with full history for bidirectional sync"
            echo "Cloning repository..."

            # #1439: clone into a temp dir on the DISK-BACKED home volume, then
            # MERGE into /home/developer — never `git clone` straight into the
            # live home dir. The home dir is populated concurrently at container
            # start (async named-volume-from-image copy), so a clone into it
            # intermittently races that writer, sees a non-empty dir, and aborts
            # (exit 128) → a silent empty agent. A temp subdir the base image
            # never ships is guaranteed empty (writer-immune) and same-filesystem
            # (fast merge). NOT /tmp: it is a 512 MB RAM tmpfs (#1098), too small
            # for a full-history clone of a large repo.
            CLONE_TMP="/home/developer/.trinity-clone-tmp"
            rm -rf "${CLONE_TMP}" 2>/dev/null || true  # clear any leftover from a prior partial boot

            # GIT-002: Clone the target branch if specified (source mode)
            CLONE_BRANCH="${GIT_SOURCE_BRANCH:-main}"
            if [ "${GIT_SOURCE_MODE}" = "true" ] && [ -n "${CLONE_BRANCH}" ]; then
                echo "Cloning branch: ${CLONE_BRANCH}"
                CLONE_CMD="git clone -b ${CLONE_BRANCH} ${CLONE_URL} ${CLONE_TMP}"
            else
                CLONE_CMD="git clone ${CLONE_URL} ${CLONE_TMP}"
            fi

            # Capture clone output for error reporting (#218)
            CLONE_OUTPUT=$(eval "${CLONE_CMD}" 2>&1)
            CLONE_EXIT=$?
            if [ $CLONE_EXIT -eq 0 ]; then
            echo "Repository cloned successfully with git history"

            # #1439: merge the clone (incl .git) into /home/developer. A tar-pipe
            # is a true recursive MERGE: file collisions overwrite, dir collisions
            # (e.g. .claude) merge in place — mv would fail "Directory not empty"
            # and cp -r would nest a colliding dir. Base-image dotfiles already in
            # place (.local pip cache, .ssh, mcp-servers) are left untouched
            # (gitignored, never committed); .git will not collide (image ships none).
            ( cd "${CLONE_TMP}" && tar cf - . ) | ( cd /home/developer && tar xf - )
            rm -rf "${CLONE_TMP}" 2>/dev/null || true

            cd /home/developer || exit 1

            # Configure git user for commits
            git config user.email "trinity-agent@ability.ai"
            git config user.name "Trinity Agent (${AGENT_NAME:-unknown})"

            # SOURCE MODE: Track the source branch directly (unidirectional pull only)
            # This is for agents that pull updates from GitHub but don't push back
            if [ "${GIT_SOURCE_MODE}" = "true" ]; then
                SOURCE_BRANCH="${GIT_SOURCE_BRANCH:-main}"
                echo "Source mode enabled - tracking branch: ${SOURCE_BRANCH}"

                # Checkout the source branch
                git checkout "${SOURCE_BRANCH}" 2>&1 || git checkout -b "${SOURCE_BRANCH}" "origin/${SOURCE_BRANCH}" 2>&1 || echo "Warning: Could not checkout ${SOURCE_BRANCH}"

                # Set up tracking for pull operations
                git branch --set-upstream-to="origin/${SOURCE_BRANCH}" "${SOURCE_BRANCH}" 2>&1 || true

                echo "Source mode ready - pull updates with 'git pull'"

            # LEGACY WORKING BRANCH MODE: Create unique working branch for bidirectional sync
            elif [ -n "${GIT_WORKING_BRANCH}" ]; then
                echo "Creating working branch: ${GIT_WORKING_BRANCH}"

                # Check if branch exists on remote
                if git ls-remote --heads origin "${GIT_WORKING_BRANCH}" | grep -q "${GIT_WORKING_BRANCH}"; then
                    echo "Branch exists on remote, checking out..."
                    git checkout "${GIT_WORKING_BRANCH}" || git checkout -b "${GIT_WORKING_BRANCH}" origin/main
                else
                    echo "Creating new branch from main..."
                    git checkout -b "${GIT_WORKING_BRANCH}"
                    # Push the new branch to establish tracking
                    git push -u origin "${GIT_WORKING_BRANCH}" 2>&1 || echo "Note: Could not push new branch (will push on first sync)"
                fi

                echo "Working branch '${GIT_WORKING_BRANCH}' ready"
            fi

            # Store git remote URL with credentials for push operations
            git remote set-url origin "${CLONE_URL}"

            # #1439: .local (base-image pip cache) is untouched — the merge never
            # removes it — so no backup/restore is needed (the old rm-then-clone
            # required it). This also drops a copy of a potentially-large .local
            # onto the 512 MB /tmp tmpfs.

            # #953: do NOT touch .gitignore from startup.sh. The canonical
            # pattern list lives in `_GITIGNORE_PATTERNS`
            # (src/backend/services/git_service.py) and is applied by
            # `_build_gitignore_merge_command` on first Trinity-orchestrated
            # init/push. The old shell-level append produced `M .gitignore`
            # against `origin/main` whenever the cloned template's
            # `.gitignore` already (correctly) contained the patterns —
            # because the unanchored/anchored greps gave false negatives on
            # trailing whitespace, CRLF, or missing newline-terminator.

            # #1439: clear any stale failure marker from a prior boot's failed
            # clone so a recovered agent stops reporting unhealthy (health treats
            # a lingering .git-clone-status=failed as an identity-clone failure).
            rm -f /home/developer/.git-clone-status 2>/dev/null || true

            echo "Git sync initialization complete"
            else
                echo "=========================================="
                echo "ERROR: Failed to clone GitHub repository: ${GITHUB_REPO}"
                echo "Exit code: ${CLONE_EXIT}"
                # Sanitize output to avoid leaking PAT in logs
                # shellcheck disable=SC2001  # regex char-class [^@]; ${//} glob can't express it
                echo "Clone output: $(echo "${CLONE_OUTPUT}" | sed "s|oauth2:[^@]*@|oauth2:***@|g")"
                echo "=========================================="
                echo "Possible causes:"
                echo "  - GitHub PAT does not have access to this repository"
                echo "  - Repository does not exist"
                echo "  - Branch '${CLONE_BRANCH}' does not exist"
                echo "  - Network connectivity issue"
                echo "=========================================="
                # Write status file for backend to check
                echo "{\"status\":\"failed\",\"error\":\"clone_failed\",\"repo\":\"${GITHUB_REPO}\",\"branch\":\"${CLONE_BRANCH}\"}" > /home/developer/.git-clone-status
                # #1439: remove the partial/failed temp clone so it can't break
                # the next boot's clone or leave a PAT-bearing .git/config behind.
                # (.local is untouched — the merge never removed it.)
                rm -rf "${CLONE_TMP}" 2>/dev/null || true
            fi
        fi
    else
        # Original behavior: shallow clone without .git for non-sync agents
        # Check if initialization marker exists (persistent volume already has files)
        if [ -f "/home/developer/.trinity-initialized" ]; then
            echo "Agent workspace already initialized on persistent volume - preserving user files"
        else
            echo "Git sync disabled - using shallow clone without git history"

        # Clone into a temp directory first
        # GIT-002: Support cloning specific branch for shallow clone
        CLONE_BRANCH="${GIT_SOURCE_BRANCH:-main}"
        if [ -n "${CLONE_BRANCH}" ] && [ "${CLONE_BRANCH}" != "main" ]; then
            echo "Cloning branch: ${CLONE_BRANCH}"
            SHALLOW_CLONE_CMD="git clone --depth 1 -b ${CLONE_BRANCH} ${CLONE_URL} /tmp/repo-clone"
        else
            SHALLOW_CLONE_CMD="git clone --depth 1 ${CLONE_URL} /tmp/repo-clone"
        fi

        SHALLOW_OUTPUT=$(eval "${SHALLOW_CLONE_CMD}" 2>&1)
        SHALLOW_EXIT=$?
        if [ $SHALLOW_EXIT -eq 0 ]; then
            echo "Repository cloned successfully"

            # Copy all files from the cloned repo to the working directory
            # Using rsync-like behavior with cp
            # Copy everything except .git directory. Absolute paths via find
            # -print0 / read -d '' so odd filenames can't word-split (SC2010/SC2045).
            find /tmp/repo-clone -mindepth 1 -maxdepth 1 ! -name '.git' -print0 |
                while IFS= read -r -d '' item; do
                    echo "Copying $(basename "${item}")..."
                    cp -r "${item}" /home/developer/ 2>/dev/null || true
                done

            # Clean up the clone
            rm -rf /tmp/repo-clone

            cd /home/developer || exit 1

            # Create initialization marker to prevent re-cloning on restart
            touch /home/developer/.trinity-initialized

            # #1439: clear any stale failure marker from a prior failed clone.
            rm -f /home/developer/.git-clone-status 2>/dev/null || true

            echo "GitHub repository initialization complete"
        else
            echo "=========================================="
            echo "ERROR: Failed to clone GitHub repository: ${GITHUB_REPO}"
            echo "Exit code: ${SHALLOW_EXIT}"
            # shellcheck disable=SC2001  # regex char-class [^@]; ${//} glob can't express it
            echo "Clone output: $(echo "${SHALLOW_OUTPUT}" | sed "s|oauth2:[^@]*@|oauth2:***@|g")"
            echo "=========================================="
            echo "Possible causes:"
            echo "  - GitHub PAT does not have access to this repository"
            echo "  - Repository does not exist"
            echo "  - Branch '${CLONE_BRANCH}' does not exist"
            echo "=========================================="
            echo "{\"status\":\"failed\",\"error\":\"clone_failed\",\"repo\":\"${GITHUB_REPO}\",\"branch\":\"${CLONE_BRANCH}\"}" > /home/developer/.git-clone-status
        fi
        fi
    fi

# Initialize from local template if specified (fallback)
elif [ -n "${TEMPLATE_NAME}" ] && [ -d "/template" ]; then
    echo "Initializing agent from local template: ${TEMPLATE_NAME}"
    cd /home/developer || exit 1

    # Check if workspace is already initialized (persistent volume has files from previous start)
    if [ -f "/home/developer/.trinity-initialized" ]; then
        echo "Agent workspace already initialized on persistent volume - preserving user files"
    else
        # Copy ALL template files to workspace (including template.yaml - it's a required Trinity file)
        # This ensures custom directories (src/, lib/, docs/, etc.) are included
        echo "Copying template files..."
        find /template -mindepth 1 -maxdepth 1 -print0 |
            while IFS= read -r -d '' item; do
                echo "  Copying $(basename "${item}")..."
                cp -r "${item}" /home/developer/ 2>/dev/null || true
            done
        cd /home/developer || exit 1

        # Make scripts executable if present
        if [ -d "/home/developer/scripts" ]; then
            chmod +x scripts/*.sh 2>/dev/null || true
            chmod +x scripts/*.py 2>/dev/null || true
        fi

        # Create initialization marker to prevent re-copying on restart
        touch /home/developer/.trinity-initialized

        echo "Template initialization complete"
    fi
fi

# NOTE: Trinity platform instructions are injected at runtime via --append-system-prompt
# on every Claude Code invocation (Issue #136). No file-based injection needed.

# Copy generated credential files (with real values, generated by backend)
if [ -d "/generated-creds" ]; then
    echo "Copying generated credential files..."
    cd /home/developer || exit 1

    # Copy .mcp.json with real credentials
    if [ -f "/generated-creds/.mcp.json" ]; then
        echo "  Copying .mcp.json (with credentials)..."
        cp /generated-creds/.mcp.json . 2>/dev/null || true
    fi

    # Copy .env with real credentials
    if [ -f "/generated-creds/.env" ]; then
        echo "  Copying .env (with credentials)..."
        cp /generated-creds/.env . 2>/dev/null || true
    fi

    # Copy any other generated config files (preserving directory structure)
    find /generated-creds -type f ! -name ".mcp.json" ! -name ".env" -print0 2>/dev/null |
        while IFS= read -r -d '' file; do
        # Get relative path from /generated-creds
        rel_path="${file#/generated-creds/}"

        # Skip credential-files directory (handled separately below)
        if [[ "$rel_path" == credential-files/* ]]; then
            continue
        fi

        target_dir=$(dirname "$rel_path")

        if [ "$target_dir" != "." ]; then
            mkdir -p "/home/developer/$target_dir"
        fi

        echo "  Copying $rel_path..."
        cp "$file" "/home/developer/$rel_path" 2>/dev/null || true
    done

    # Copy credential files from credential-files/ subdirectory
    # These are file-type credentials (e.g., service account JSON files)
    # The path structure inside credential-files/ maps to the target path in /home/developer/
    if [ -d "/generated-creds/credential-files" ]; then
        echo "Copying credential files..."
        find /generated-creds/credential-files -type f -print0 2>/dev/null |
            while IFS= read -r -d '' file; do
            # Get path relative to credential-files/
            rel_path="${file#/generated-creds/credential-files/}"
            target_dir=$(dirname "$rel_path")

            if [ "$target_dir" != "." ]; then
                mkdir -p "/home/developer/$target_dir"
            fi

            echo "  Copying credential file: $rel_path"
            cp "$file" "/home/developer/$rel_path" 2>/dev/null || true
            # Set restrictive permissions on credential files
            chmod 600 "/home/developer/$rel_path" 2>/dev/null || true
        done
    fi

    echo "Credential files copied"
fi

# === Codex runtime setup (#1187) ===
# Codex is the third agent runtime. Two Codex-specific quirks are fixed here:
#  1. Identity: Codex reads AGENTS.md (NOT CLAUDE.md). Mirror the agent's
#     instructions so a Codex agent gets the same platform identity Claude does
#     (the per-turn system prompt is additionally prepended by codex_runtime.py).
#  2. CODEX_HOME defaults to ~/.codex — inside the git-tracked repo, which would
#     dirty auto-sync. Relocate it onto the disk-backed scratch dir (#1098).
# NOTE: startup.sh must NOT write to .gitignore (#953) — the canonical list
# (`_GITIGNORE_PATTERNS` in src/backend/services/git_service.py, applied on git
# init/push by `_build_gitignore_merge_command`) carries `.tmp/`, so the
# relocated CODEX_HOME under $TMPDIR is excluded from git without a shell write.
if [ "${AGENT_RUNTIME}" = "codex" ]; then
    echo "Configuring Codex runtime..."

    if [ -f "/home/developer/CLAUDE.md" ] && [ ! -f "/home/developer/AGENTS.md" ]; then
        cp /home/developer/CLAUDE.md /home/developer/AGENTS.md 2>/dev/null && \
            echo "  Mirrored CLAUDE.md -> AGENTS.md for Codex" || \
            echo "  Warning: could not create AGENTS.md"
    fi

    CODEX_HOME_DIR="${CODEX_HOME:-${AGENT_TMPDIR}/codex}"
    mkdir -p "${CODEX_HOME_DIR}" 2>/dev/null && chmod 700 "${CODEX_HOME_DIR}" 2>/dev/null || \
        echo "  Warning: could not create CODEX_HOME ${CODEX_HOME_DIR}"
fi

# Ensure core agent-server dependencies are installed correctly
# This prevents template repos from breaking the agent server with incompatible packages
echo "Verifying agent-server dependencies..."
python3 -m pip install --user --quiet --upgrade \
    fastapi \
    uvicorn \
    httpx \
    pydantic \
    python-multipart \
    pyyaml

# Start SSH if enabled
if [ "${ENABLE_SSH}" = "true" ]; then
    echo "Starting SSH server..."
    # Ensure privilege separation directory exists (tmpfs may clear it)
    sudo mkdir -p /var/run/sshd
    sudo chmod 0755 /var/run/sshd
    sudo /usr/sbin/sshd -D &
fi

# Load agent configuration
if [ -f "/config/agent-config.yaml" ]; then
    echo "Loading agent configuration..."
    python3 /app/configure_agent.py
fi

# Configure MCP servers
if [ -d "/config/mcp-servers" ]; then
    for mcp in /config/mcp-servers/*.yaml; do
        if [ -f "$mcp" ]; then
            echo "Configuring MCP: $mcp"
            python3 /app/setup_mcp.py "$mcp"
        fi
    done
fi

# === Rotated subscription token: durable override (#1089) ===
# A hot-reload (POST /api/credentials/reload-token) persists the rotated
# CLAUDE_CODE_OAUTH_TOKEN to this writable-layer path so it survives a plain
# stop+start. The container's baked Config.Env still holds the OLD token and a
# fleet restart (ops.py) does a raw stop+start that bypasses start_agent_internal
# — so export the override (when present and non-empty) BEFORE launching the
# agent server, so the rotated token wins. The file is wiped on recreate (fresh
# writable layer), so a DB-driven recreate cleanly reverts to the freshly-baked
# Config.Env token — no marker logic needed.
if [ -s /var/lib/trinity/oauth-token ]; then
    CLAUDE_CODE_OAUTH_TOKEN="$(cat /var/lib/trinity/oauth-token)"
    export CLAUDE_CODE_OAUTH_TOKEN
    echo "Applied rotated subscription token from durable override"
fi

# Start Agent Web Server (self-contained UI)
if [ "${ENABLE_AGENT_UI}" = "true" ]; then
    echo "Starting Agent Web UI on port ${AGENT_SERVER_PORT:-8000}..."
    python3 /app/agent-server.py &
fi

# === Persistent Setup Script ===
# Run user/agent setup script if exists (reinstalls packages after container recreation)
# This allows agents to persist apt-get, npm -g, and other system-level packages
if [ -f "/home/developer/.trinity/setup.sh" ]; then
    echo "Running persistent setup script to restore packages..."
    chmod +x /home/developer/.trinity/setup.sh
    bash /home/developer/.trinity/setup.sh 2>&1 || echo "Warning: setup.sh had errors (continuing startup)"
fi

# === Auto-Import Encrypted Credentials ===
# If .credentials.enc exists but .env doesn't, decrypt and inject credentials
# This enables portable credential storage via git-committed encrypted files
cd /home/developer || exit 1
if [ -f ".credentials.enc" ] && [ ! -f ".env" ]; then
    echo "Found .credentials.enc without .env - attempting auto-import..."

    # Wait for agent server to be ready (started above)
    sleep 2

    # Call internal backend endpoint to decrypt and inject
    # Note: This requires CREDENTIAL_ENCRYPTION_KEY to be set on the backend
    IMPORT_RESULT=$(curl -s -X POST "http://backend:8000/api/internal/decrypt-and-inject" \
         -H "Content-Type: application/json" \
         -d "{\"agent_name\": \"$AGENT_NAME\"}" 2>/dev/null || echo '{"status":"error"}')

    if echo "$IMPORT_RESULT" | grep -q '"status":"success"'; then
        echo "Auto-imported credentials from .credentials.enc"
    elif echo "$IMPORT_RESULT" | grep -q '"status":"skipped"'; then
        echo "Credential import skipped: $(echo "$IMPORT_RESULT" | grep -o '"reason":"[^"]*"' | cut -d'"' -f4)"
    else
        echo "Warning: Could not auto-import credentials (key may not be configured)"
    fi
fi

# === Content Folder Convention ===
# Create content/ directory for large generated assets (videos, audio, images, exports)
# These files persist across restarts but are NOT synced to GitHub
echo "Setting up content folder convention..."
mkdir -p /home/developer/content/{videos,audio,images,exports}

# #953: do NOT touch .gitignore here. `content/` and `.local/` are in the
# canonical `_GITIGNORE_PATTERNS` list applied by
# `_build_gitignore_merge_command` on first Trinity-orchestrated init and
# again on every push via `_migrate_workspace_gitignore`. The previous
# shell-level append created `M .gitignore` drift for freshly-deployed
# agents whose template already shipped the patterns correctly.

echo "Agent ready. Keeping container alive..."
tail -f /dev/null

