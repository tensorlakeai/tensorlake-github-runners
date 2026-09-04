#!/usr/bin/env bash
set -euo pipefail

# The runner image builds FROM `ubuntu-2204-base`: a plain ubuntu:22.04 imported
# into this project. Its glibc (2.35) satisfies the dataplane's GLIBC 2.34
# release floor, whereas the 24.04 tensorlake/ubuntu-systemd base ships glibc
# 2.39 and fails it. Import the base once if it is not already registered.
if ! tl sbx image describe ubuntu-2204-base >/dev/null 2>&1; then
  tl sbx image import ubuntu:22.04 --registered-name ubuntu-2204-base
fi

tl sbx image create sandbox-image/Dockerfile \
  --registered-name github-actions-runner \
  --build-arg "TENSORLAKE_CLI_VERSION=${TENSORLAKE_RUNNER_CLI_VERSION:-cli-v0.5.123}" \
  --cpus "${TENSORLAKE_IMAGE_BUILD_CPUS:-4}" \
  --memory "${TENSORLAKE_IMAGE_BUILD_MEMORY_MB:-4096}" \
  --disk_mb "${TENSORLAKE_IMAGE_DISK_MB:-10240}" \
  --builder_disk_mb "${TENSORLAKE_IMAGE_BUILDER_DISK_MB:-51200}"
