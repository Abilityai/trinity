# Python 3.11 is provided by the official python image (Debian Bookworm
# slim). This eliminates the dependency on ppa:deadsnakes/ppa, which has
# no SLA and historically blocked deployment of critical agent-runtime
# fixes for days at a time when the PPA was unreachable. See #658.
FROM python:3.14-slim-bookworm

# Version tracking - set at build time via --build-arg VERSION=x.y.z
ARG VERSION=latest
LABEL trinity.base-image-version="${VERSION}"

ENV DEBIAN_FRONTEND=noninteractive
# Default to UTC so container-local time == UTC and matches /etc/localtime
# (Etc/UTC). Agents, the backend, and the scheduler all reason in UTC
# (Invariant #16); a non-UTC default made agents run hours off and file
# timestamps surface as naive local time (#1795). Override at deploy time
# with `docker run -e TZ=...` if a specific zone is required.
ENV TZ=Etc/UTC
ENV TRINITY_BASE_VERSION="${VERSION}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    git \
    vim \
    nano \
    htop \
    build-essential \
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release \
    sudo \
    openssh-server \
    jq \
    unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

RUN wget -q https://go.dev/dl/go1.23.4.linux-amd64.tar.gz && \
    tar -C /usr/local -xzf go1.23.4.linux-amd64.tar.gz && \
    rm go1.23.4.linux-amd64.tar.gz

# Docker apt repo for debian (was linux/ubuntu when the base was ubuntu:22.04).
RUN curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

# GitHub CLI (#1574): the managed PAT (GH_TOKEN/GITHUB_TOKEN, exported by
# startup.sh) authenticates `gh` + the REST API, not just git — but `gh` must be
# present. Install from GitHub's official apt repo (mirrors the docker-ce-cli
# stanza above).
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

# #1595: git auto-maintenance can never complete inside agent containers — a
# detached `git gc --auto` reparents to PID 1 and the orphan sweep SIGKILLs it
# within one tick, every time, leaving tmp_pack litter and unbounded .git
# growth (44GB / 97%-garbage repos observed). Disable it system-wide; the
# agent-server's registered maintenance pass (auto-sync loop, routers/git.py)
# owns repo maintenance instead. autoDetach=false is the belt: if a repo-local
# config re-enables auto gc/maintenance, it runs foreground as a child of its
# triggering command and inherits that command's sweep protection rather than
# detaching to PID 1.
RUN git config --system gc.auto 0 && \
    git config --system gc.autoDetach false && \
    git config --system maintenance.auto false && \
    git config --system maintenance.autoDetach false

RUN npm install -g @anthropic-ai/claude-code@latest

# Install Gemini CLI for multi-runtime support
RUN npm install -g @google/gemini-cli

# Install OpenAI Codex CLI for multi-runtime support (#1187).
# Pinned (not @latest) so base-image rebuilds are reproducible and a
# breaking Codex release can't silently change the agent runtime. The npm
# package resolves the platform-specific Rust binary via optional deps;
# the linux-x64 build lands here. Bump deliberately after testing.
RUN npm install -g @openai/codex@0.147.0

RUN useradd -m -s /bin/bash -u 1000 developer && \
    echo "developer:developer" | chpasswd && \
    usermod -aG sudo developer && \
    echo "developer ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

RUN mkdir /var/run/sshd && \
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config && \
    sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config

RUN mkdir -p /home/developer/.ssh /home/developer/.config /home/developer/mcp-servers && \
    chown -R developer:developer /home/developer

ENV PATH="/usr/local/go/bin:$PATH"
ENV HOME=/home/developer
ENV USER=developer

USER developer
WORKDIR /home/developer

RUN python3 -m pip install --user --upgrade pip && \
    python3 -m pip install --user \
        fastapi \
        uvicorn \
        httpx \
        pydantic \
        python-multipart \
        pyyaml \
        rich \
        cryptography

RUN mkdir -p /home/developer/.claude-agent

# Install Trinity guardrail hooks (GUARD-001/002/003).
# Hooks live in /opt/trinity/hooks/ (root-owned) so the agent cannot modify
# them at runtime. ~/.claude/settings.json registers them for Claude Code.
USER root
RUN mkdir -p /opt/trinity/hooks
COPY ./hooks/lib.py /opt/trinity/hooks/lib.py
COPY ./hooks/bash-guardrail.py /opt/trinity/hooks/bash-guardrail.py
COPY ./hooks/file-guardrail.py /opt/trinity/hooks/file-guardrail.py
COPY ./hooks/output-scanner.py /opt/trinity/hooks/output-scanner.py
COPY ./hooks/read-only-guard.py /opt/trinity/hooks/read-only-guard.py
COPY ./hooks/write-runtime-config.py /opt/trinity/hooks/write-runtime-config.py
COPY ./hooks/guardrails-baseline.json /opt/trinity/guardrails-baseline.json
RUN chmod 0555 /opt/trinity/hooks/*.py && \
    chmod 0444 /opt/trinity/guardrails-baseline.json && \
    chown -R root:root /opt/trinity

RUN mkdir -p /home/developer/.claude && \
    chown developer:developer /home/developer/.claude
COPY --chown=developer:developer ./hooks/claude-settings.json /home/developer/.claude/settings.json
RUN chmod 0644 /home/developer/.claude/settings.json
USER developer

# --- Trinity plugin pre-install (ent#411) ---------------------------------
# Bake the platform's own Claude Code plugin into the image so an agent created
# from a BARE GitHub repo — no template.yaml, no plugins: block — can run
# `/trinity:onboard` in its first session and make itself Trinity-compatible.
# Reading the plugin set only from a declaration is chicken-and-egg for exactly
# that agent: it declares nothing, so nothing installs, so the plugin that would
# write template.yaml is missing.
#
# This lands under /home/developer, which is a named volume at runtime. Docker
# populates an EMPTY named volume from the image on first mount, so a new agent
# gets it; an agent whose volume predates this image does not, and for those the
# #1704 boot hook (agent_server.plugins_reinstall) installs it on next start.
# Same ordering caveat as #1704: an old base image silently lacks the pre-install.
#
# Never fatal. An air-gapped or rate-limited build must still produce an image —
# the boot hook is the reconciler, so a missed pre-install costs subprocesses at
# first start, not correctness. Set TRINITY_PREINSTALL_PLUGINS=0 to skip it
# deliberately (and TRINITY_PLATFORM_PLUGINS=0 at runtime to keep the boot hook
# from installing it either).
ARG TRINITY_PREINSTALL_PLUGINS=1
RUN if [ "$TRINITY_PREINSTALL_PLUGINS" = "1" ]; then \
        (claude plugin marketplace add abilityai/abilities \
            && claude plugin install trinity@abilityai --yes \
            && echo "[base-image] pre-installed trinity@abilityai") \
        || echo "[base-image] withheld trinity@abilityai: marketplace unreachable at build time — the boot hook will retry"; \
    else \
        echo "[base-image] trinity@abilityai pre-install skipped (TRINITY_PREINSTALL_PLUGINS=$TRINITY_PREINSTALL_PLUGINS)"; \
    fi

# Copy agent server code to /app (outside persistent volume)
# This ensures code updates reach agents without volume conflicts
USER root
RUN mkdir -p /app && chown -R developer:developer /app
USER developer

COPY --chown=developer:developer ./agent-server.py /app/agent-server.py
COPY --chown=developer:developer ./agent_server /app/agent_server
COPY --chown=developer:developer ./startup.sh /app/startup.sh
RUN chmod +x /app/startup.sh /app/agent-server.py

# Add /app to Python path so agent_server module can be imported
ENV PYTHONPATH="/app:${PYTHONPATH}"

EXPOSE 22 8000

USER root

RUN mkdir -p /workspace /data /logs && \
    chown -R developer:developer /workspace /data /logs && \
    chmod 755 /workspace /data /logs

RUN mkdir -p /tmp/secure && \
    chmod 1777 /tmp/secure

# #1089: writable-layer dir for the subscription-token hot-reload override
# (/var/lib/trinity/oauth-token). Owned by the agent (UID 1000) so the
# agent-server process can write it; on the writable layer (NOT /home/developer)
# so it survives a plain stop+start but is wiped on recreate.
RUN mkdir -p /var/lib/trinity && \
    chown developer:developer /var/lib/trinity

USER developer

CMD ["/app/startup.sh"]

