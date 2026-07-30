#!/usr/bin/env bash
set -euo pipefail

tl sbx image create sandbox-image/Dockerfile \
  --registered-name github-actions-runner \
  --cpus "${TENSORLAKE_IMAGE_BUILD_CPUS:-4}" \
  --memory "${TENSORLAKE_IMAGE_BUILD_MEMORY_MB:-4096}" \
  --disk_mb "${TENSORLAKE_IMAGE_DISK_MB:-10240}" \
  --builder_disk_mb "${TENSORLAKE_IMAGE_BUILDER_DISK_MB:-51200}"
