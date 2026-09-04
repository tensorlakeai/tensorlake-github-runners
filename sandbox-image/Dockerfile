# Built from a plain ubuntu:22.04 base (imported into Tensorlake as
# `ubuntu-2204-base`) rather than `tensorlake/ubuntu-systemd`. The dataplane
# release verifies a GLIBC floor of 2.34 (check_glibc_floor.py); binaries are
# linked against the build host's glibc, so the runner must build on a host no
# newer than the target. Ubuntu 24.04 ships glibc 2.39 (fails the floor);
# Ubuntu 22.04 ships glibc 2.35 and yields <=2.34 symbol requirements, matching
# the nscloud-ubuntu-22.04 host `main` builds on. Because we no longer inherit a
# systemd base, this image installs systemd itself and boots it as PID 1
# (CMD /sbin/init) so Docker's systemd services start, and creates the tl-user
# account the base used to provide.
FROM ubuntu-2204-base

ARG TENSORLAKE_CLI_VERSION=cli-v0.5.123
ENV DEBIAN_FRONTEND=noninteractive

# The tl-user account (passwordless sudo + Docker access) that the orchestrator
# selects for every job. Previously supplied by the tensorlake/ubuntu-systemd
# base; recreate it here since we start from a plain ubuntu image.
RUN groupadd --gid 1000 tl-user \
  && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash tl-user

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    binutils \
    build-essential \
    ca-certificates \
    clang \
    cmake \
    curl \
    dbus \
    e2fsprogs \
    file \
    fuse3 \
    gh \
    git \
    gzip \
    jq \
    libclang-dev \
    libfuse3-dev \
    libprotobuf-dev \
    lld \
    musl-tools \
    nasm \
    pipx \
    pkg-config \
    protobuf-compiler \
    python3-pip \
    python3-pyelftools \
    rsync \
    sudo \
    systemd \
    systemd-sysv \
    tar \
    unzip \
    util-linux \
  && architecture="$(dpkg --print-architecture)" \
  && case "${architecture}" in \
    amd64) aws_arch="x86_64" ;; \
    arm64) aws_arch="aarch64" ;; \
    *) echo "unsupported architecture for AWS CLI: ${architecture}" >&2; exit 2 ;; \
  esac \
  && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}.zip" \
    -o /tmp/awscliv2.zip \
  && unzip -q /tmp/awscliv2.zip -d /tmp/awscliv2 \
  && /tmp/awscliv2/aws/install \
  && rm -r /tmp/awscliv2 \
  && rm /tmp/awscliv2.zip \
  && aws --version \
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
  && TENSORLAKE_VERSION="${TENSORLAKE_CLI_VERSION}" sh /tmp/install-tensorlake.sh --system --yes \
  && rm /tmp/install-tensorlake.sh \
  && test "$(tl --version)" = "tl ${TENSORLAKE_CLI_VERSION#cli-v}" \
  && install -d -o tl-user -g tl-user /mnt/tensorlake-cache

LABEL ai.tensorlake.cli-version="${TENSORLAKE_CLI_VERSION}"

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

# Boot systemd as PID 1 so Docker's systemd units start inside the sandbox.
# The tensorlake/ubuntu-systemd base did this implicitly; a plain ubuntu base
# defaults its CMD to bash, so set it explicitly.
CMD ["/sbin/init"]
