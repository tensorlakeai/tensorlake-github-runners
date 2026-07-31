FROM tensorlake/ubuntu-systemd

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    fuse3 \
    gzip \
    jq \
    sudo \
    tar \
    util-linux \
  && install -m 0755 -d /etc/apt/keyrings \
  && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
  && chmod a+r /etc/apt/keyrings/docker.asc \
  && . /etc/os-release \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends \
    containerd.io \
    docker-buildx-plugin \
    docker-ce \
    docker-ce-cli \
    docker-compose-plugin \
  && printf '%s\n' 'tl-user ALL=(ALL:ALL) NOPASSWD:ALL' \
    > /etc/sudoers.d/tl-user-actions-runner \
  && chmod 0440 /etc/sudoers.d/tl-user-actions-runner \
  && visudo -cf /etc/sudoers.d/tl-user-actions-runner \
  && install -d /etc/systemd/system/docker.socket.d \
  && printf '%s\n' \
    '[Socket]' \
    'SocketGroup=tl-user' \
    'SocketMode=0660' \
    > /etc/systemd/system/docker.socket.d/tl-user.conf \
  && systemctl enable containerd.service docker.service docker.socket \
  && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://tensorlake.ai/install -o /tmp/install-tensorlake.sh \
  && sh /tmp/install-tensorlake.sh --system --yes \
  && rm /tmp/install-tensorlake.sh \
  && tl version \
  && install -d -o tl-user -g tl-user /mnt/tensorlake-cache

RUN mkdir -p /opt/actions-runner \
  && cd /opt/actions-runner \
  && arch="$(uname -m)" \
  && case "$arch" in \
    x86_64) runner_arch="x64" ;; \
    aarch64|arm64) runner_arch="arm64" ;; \
    *) echo "unsupported architecture: $arch" >&2; exit 2 ;; \
  esac \
  && latest="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name)" \
  && version="${latest#v}" \
  && curl -fsSL -o actions-runner.tgz \
    "https://github.com/actions/runner/releases/download/${latest}/actions-runner-linux-${runner_arch}-${version}.tar.gz" \
  && tar xzf actions-runner.tgz \
  && rm actions-runner.tgz \
  && chown -R tl-user:tl-user /opt/actions-runner

WORKDIR /opt/actions-runner
