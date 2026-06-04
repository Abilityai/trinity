# Python 3.11 is provided by the official python image (Debian Bookworm
# slim). This eliminates the dependency on ppa:deadsnakes/ppa, which has
# no SLA and historically blocked deployment of critical agent-runtime
# fixes for days at a time when the PPA was unreachable. See #658.
FROM python:3.14-slim-bookworm

# Version tracking - set at build time via --build-arg VERSION=x.y.z
ARG VERSION=latest
LABEL trinity.base-image-version="${VERSION}"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=America/New_York
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

RUN npm install -g @anthropic-ai/claude-code@latest

# Install Gemini CLI for multi-runtime support
RUN npm install -g @google/gemini-cli

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

USER developer

CMD ["/app/startup.sh"]

