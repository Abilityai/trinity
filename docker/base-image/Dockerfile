FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=America/New_York

RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    vim \
    nano \
    htop \
    build-essential \
    software-properties-common \
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release \
    sudo \
    openssh-server \
    jq \
    unzip \
    && rm -rf /var/lib/apt/lists/*

RUN add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs

RUN wget -q https://go.dev/dl/go1.21.5.linux-amd64.tar.gz && \
    tar -C /usr/local -xzf go1.21.5.linux-amd64.tar.gz && \
    rm go1.21.5.linux-amd64.tar.gz

RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y docker-ce-cli

RUN npm install -g @anthropic-ai/claude-code@latest

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
        cryptography \
        chromadb \
        sentence-transformers \
        chroma-mcp

# Pre-download embedding model to cache in image (avoids download on first use)
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

RUN mkdir -p /home/developer/.claude-agent

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

