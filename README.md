# Tensorlake GitHub Workers

Ephemeral GitHub Actions runners on Tensorlake. An organization `workflow_job` webhook schedules a
durable Tensorlake function that boots a fresh sandbox from a prebuilt runner image, runs exactly one
job, and tears the sandbox down. Adapted from
[`donkersgoed/github-runner-ochestrator`](https://github.com/donkersgoed/github-runner-ochestrator),
replacing its AWS scheduling layer (API Gateway, Lambda, SQS, CDK) with Tensorlake Orchestrate and
Sandboxes while keeping the GitHub contract.

**Flow:** GitHub sends a `workflow_job` event → the webhook signature is verified with
`GITHUB_WEBHOOK_SECRET` → only `queued` jobs carrying the `tensorlake` label are accepted →
`run_github_runner` mints GitHub App JIT runner credentials, provisions and mounts the repository's
cache volume, starts the sandbox, supervises the single job, and terminates the sandbox.

## Files

- `github_runner_orchestrator/app.py` — Tensorlake application and functions.
- `github_runner_orchestrator/cache.py` — per-repository cache volume naming and provisioning.
- `github_runner_orchestrator/github.py` — GitHub App JWT, installation token, and JIT runner APIs.
- `github_runner_orchestrator/webhook.py` — HMAC verification and `workflow_job` filtering.
- `github_runner_orchestrator/resources.py` — runner resource profiles.
- `actions/setup-rust-cache/action.yml`, `actions/setup-uv-cache/action.yml` — reusable cache actions.
- `sandbox-image/Dockerfile`, `scripts/build-runner-image.sh` — the runner sandbox image.

Orchestrate functions are `async`; blocking GitHub REST calls run via `asyncio.to_thread()`, sandbox
calls use the async Sandbox SDK.

## Setup

Run the wizard from the repository root:

```bash
./scripts/configure-github-org.sh
```

It installs the `gh`, `tl`, and `uv` tools as needed, explains each GitHub step, stores the secrets,
builds the runner image, deploys the application, and creates the organization webhook. You need
organization-owner access and a Tensorlake project.

**GitHub App vs. webhook.** Register the GitHub App with **Webhook → Active disabled** and only the
**Self-hosted runners: Read and write** organization permission — it supplies credentials only. A
*separate* organization webhook (created after deploy, using the endpoint the deploy returns) delivers
`workflow_job` events. Both use the same `GITHUB_WEBHOOK_SECRET`.

**Secrets** (set by the wizard, or manually with `tl secrets set`):
`GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_INSTALLATION_ID`,
`GITHUB_APP_PRIVATE_KEY`, `RUNNER_GROUP_ID` (default `1`), `TENSORLAKE_API_KEY` (project-scoped),
`TENSORLAKE_ORGANIZATION_ID`, `TENSORLAKE_PROJECT_ID`. Redeploy after changing a secret so the new
value takes effect. Find the org/project IDs with `tl whoami -o json`.

- **Upgrade:** `git pull --ff-only && ./scripts/configure-github-org.sh --upgrade`.
  The script defaults to Tensorlake organization `org_T7MwTdFrBRHdpQPWf8Jdh` and project
  `project_Bn6BzggtncBfqQPFHJC8T`. Set `TENSORLAKE_ORGANIZATION_ID` and
  `TENSORLAKE_PROJECT_ID` to use a different destination. The script also installs the Python
  Tensorlake SDK version that matches the installed `tl` version.
- **Resume a stopped deploy** (skips the GitHub App inputs and image rebuild):
  `./scripts/configure-github-org.sh --resume-from-step-6 <org>`.

Runner image name, timeout, maximum concurrent runner count, and required label are plain constants
near the top of `app.py`.

## Runner image

```bash
./scripts/build-runner-image.sh
```

Builds and registers the `github-actions-runner` sandbox image. It is based on **Ubuntu 22.04**
(imported into the project as `ubuntu-2204-base`; the script imports it if missing) and installs
systemd — booted as PID 1 so Docker's systemd units start — Docker CE, the `tl` CLI with FUSE
support, and the GitHub Actions runner, and creates the `tl-user` account.
The TLFS-capable `tl` binary is pinned in the Dockerfile and recorded as an OCI label so rebuilding
an image cannot silently select a different mount implementation. Set
`TENSORLAKE_RUNNER_CLI_VERSION=cli-vX.Y.Z` only when deliberately qualifying an upgrade.

Base the image on an OS whose glibc matches your release ABI target: Ubuntu 22.04 ships glibc 2.35,
which keeps release binaries within a GLIBC ≤ 2.34 floor. A newer base (e.g. Ubuntu 24.04 / glibc
2.39) links binaries that fail such a compatibility check.

Workflow steps run as `tl-user` (passwordless `sudo`, Docker access), matching GitHub-hosted runners:

```yaml
- run: sudo apt-get update && sudo apt-get install -y protobuf-compiler
```

## Runner resources

`runs-on: [self-hosted, tensorlake, <profile>]` selects CPU, memory, and disk:

| Label | CPUs | Memory | Disk |
|---|---:|---:|---:|
| none or `tensorlake-small` | 2 | 4 GiB | 10 GiB |
| `tensorlake-medium` | 4 | 8 GiB | 50 GiB |
| `tensorlake-large` | 8 | 16 GiB | 100 GiB |
| `tensorlake-xlarge` | 16 | 32 GiB | 100 GiB |

Profiles live in `github_runner_orchestrator/resources.py` (disks capped at 100 GiB). Docker is
available on every profile. Multiple profile labels are rejected.

## Persistent cache

Each repository gets its own Tensorlake Cloud Volume, mounted by the orchestrator at
`/mnt/tensorlake-cache` and exported to the job as `TENSORLAKE_CACHE_DIR`. It is a plain writable
directory (not an `actions/cache` service); point a tool's cache directory at a namespaced child:

```yaml
- run: |
    d="${TENSORLAKE_CACHE_DIR}/my-tool/${RUNNER_OS}-${RUNNER_ARCH}"
    mkdir -p "$d"
    echo "MY_TOOL_CACHE=$d" >> "$GITHUB_ENV"
```

- Namespace by anything that makes cached files incompatible: OS, arch, toolchain, target, profile,
  and a manual version you can bump.
- Never write secrets (tokens, registry credentials, signing material) into the volume. Pull requests
  and branches of a repository share its cache, so do not expose the runner to untrusted code.
- Writes autosave during the job; the orchestrator syncs and unmounts on exit. Provisioning is
  best-effort unless a workflow asserts the mount (see below).
- Cleanup: `tl fs ls` lists the `github-actions-cache-*` volumes; `tl fs rm <name>` deletes one.
- Readiness is an authenticated write/read/delete round trip, not merely a mount-table check. A
  mounted but unauthorized or disconnected TLFS session is detached, given one freshly minted
  credential retry, and never exported to a job unless that probe succeeds. Attempt logs include
  only a non-reversible credential fingerprint and expiry, never the credential.

Two reusable actions wrap the common cases — see each `action.yml` for inputs:

```yaml
# Rust: persist CARGO_HOME + sccache (keep `target` on local disk for parallel jobs).
- uses: actions-rust-lang/setup-rust-toolchain@v1
  with: { cache: false }
- uses: tensorlakeai/tensorlake-github-runners/actions/setup-rust-cache@main
  with:
    cache-namespace: rust-release-glibc-2.35-x86_64-unknown-linux-gnu-v2
    disable-incremental: "true"
- run: cargo build --locked --release
```

```yaml
# uv: store a compatibility-keyed compressed environment archive (avoids small-file reads over FUSE).
- uses: tensorlakeai/tensorlake-github-runners/actions/setup-uv-cache@main
  with: { python-version: "3.11", sync-args: --locked }
- run: uv run --no-sync pytest
```

To make a cache regression fail loudly instead of silently writing to ephemeral disk, assert the
mount before use:

```yaml
- run: |
    test -n "${TENSORLAKE_CACHE_DIR}" && mountpoint -q "${TENSORLAKE_CACHE_DIR}" \
      || { echo "::error::TLFS cache not mounted"; exit 1; }
```

## Running long or heavy jobs

These are the non-obvious requirements for jobs that run for many minutes or do lock-heavy,
small-file I/O against the cache — keep them if you fork the orchestrator:

- **Supervise, don't stream.** `run_github_runner` launches the runner as a *detached* managed
  process (`sandbox.start_process`) and polls `sandbox.get_process()` for completion, writing a
  progress update each poll. The progress updates extend the function's execution timeout so the
  orchestrator outlives the job. Awaiting a single long-lived `sandbox.run("run.sh")` instead
  streams the whole job over one connection that drops on long builds — which tears the sandbox down
  mid-job and surfaces as "runner lost communication".
- **Mount the cache as root.** The orchestrator mounts via `sudo tl fs mount` so the mount daemon
  can raise its open-file limit (each open file on the volume pins a descriptor). `tl fs mount` still
  presents the volume to the invoking `tl-user`. A non-root mount is capped too low and refused.
- **Match the base-OS glibc to your release target** (see [Runner image](#runner-image)).

## Self-test

`.github/workflows/build-reference.yml` runs on a `tensorlake-small` runner: it lints and builds the
Python project, runs the tests, exercises the cache actions, and runs Docker's `hello-world` to
verify the runner's Docker daemon.
