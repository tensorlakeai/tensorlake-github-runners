# Tensorlake GitHub Workers

This is a simplified version of
[`donkersgoed/github-runner-ochestrator`](https://github.com/donkersgoed/github-runner-ochestrator)
using Tensorlake Orchestrate for scheduling and Tensorlake Sandboxes for ephemeral runner isolation.

The original project uses API Gateway, Lambda, SQS, CDK, SSM, and Lambda MicroVM images. This version
keeps the GitHub contract and removes the AWS scheduling layer:

1. A GitHub organization webhook sends a `workflow_job` event.
2. The webhook signature is verified with `GITHUB_WEBHOOK_SECRET`.
3. Only queued jobs with `REQUIRED_RUNNER_LABEL` are accepted.
4. An async Tensorlake Application schedules `run_github_runner` as durable work.
5. `run_github_runner` mints GitHub App credentials, creates a JIT self-hosted runner config,
   mounts that repository's persistent cache volume, starts a fresh Tensorlake Sandbox from a
   prebuilt runner image through `AsyncSandbox`, runs exactly one GitHub Actions job, and terminates
   the sandbox.

## Files

- `github_runner_orchestrator/app.py` - Tensorlake application and functions.
- `github_runner_orchestrator/cache.py` - Per-repository cache volume naming and provisioning.
- `github_runner_orchestrator/github.py` - GitHub App JWT, installation token, and JIT runner APIs.
- `github_runner_orchestrator/webhook.py` - HMAC verification and `workflow_job` filtering.
- `sandbox-image/Dockerfile` - Reusable GitHub Actions runner sandbox image built from an OCI base.
- `uv.lock` - Locked application and development dependency versions.

The Orchestrate functions are `async def` and use Tensorlake async function calls. Blocking GitHub
REST helpers run via `asyncio.to_thread()`, while sandbox operations use the async Sandbox SDK.

## Configuration

### Interactive setup

Run the organization setup wizard from the repository root:

```bash
./scripts/configure-github-org.sh
```

The wizard checks for the GitHub (`gh`) and Tensorlake (`tl`) CLIs and offers to install either one
when missing. It also installs `uv` when needed, uses it to install Python 3.11, and synchronizes the
locked project dependencies. It shows all seven installation phases before changing anything and
explains every manual GitHub step before asking for a value. You need organization-owner access in
GitHub and a Tensorlake project where you can create secrets, images, and applications.

#### Why there is no webhook/deployment cycle

The GitHub App and the webhook are separate GitHub resources with different responsibilities:

| Resource | Purpose | When it is configured |
|---|---|---|
| GitHub App | Gives the Tensorlake application credentials to create ephemeral organization runners | Before deployment; its own webhook is disabled |
| Organization webhook | Sends `workflow_job` events to the Tensorlake endpoint | After deployment returns the endpoint URL |

When [registering the GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app),
deselect **Webhook → Active**. No webhook URL or webhook secret is entered on the GitHub App. Give
it only the **Self-hosted runners: Read and write** organization permission, create a private key,
and install it on the target organization.

The exact installation order is:

1. Install the local tools and sync the locked Python environment.
2. Authenticate `gh` and `tl`, then confirm the GitHub identity and Tensorlake organization/project.
3. Create and install the credentials-only GitHub App with its webhook disabled.
4. Enter a Tensorlake project API key, then store it and the active Tensorlake project context with
   the GitHub App client ID, installation ID, private key, runner group ID, and a newly generated
   webhook secret. The deployed runner uses the API key and project context to create sandboxes and
   per-repository cache volumes. Tensorlake secrets exist at the project level and do not require an
   application deployment to exist first; see the
   [secrets documentation](https://docs.tensorlake.ai/applications/secrets).
5. Build the reusable runner sandbox image.
6. Deploy the Tensorlake application. The deployment reads the stored secrets and returns a public
   endpoint URL.
7. Create a separate organization webhook for `workflow_job` events using that endpoint and the
   same webhook secret stored in step 4.

The wizard performs all of these steps. The API-key prompt is hidden; for a non-interactive run,
provide it in `TENSORLAKE_API_KEY`. The wizard writes sensitive values only to
permission-restricted temporary files, deletes those files when it exits, and never prints the API
key, private key, or webhook secret.

If setup reaches deployment and stops, resume without repeating the GitHub App inputs or rebuilding
the runner image:

```bash
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}" ./scripts/configure-github-org.sh \
  --resume-from-step-6 your-organization
```

When `WEBHOOK_SECRET` is still available in the current shell, the resume command reuses it.
Otherwise it safely rotates the stored webhook secret before deploying, then creates or updates the
organization webhook with the same new value.

### Upgrade an existing installation

After pulling a newer version of this repository, upgrade the deployed application with:

```bash
git switch main
git pull --ff-only
./scripts/configure-github-org.sh --upgrade
```

Confirm that `tl whoami` shows the Tensorlake project containing the existing installation. The
upgrade command syncs the locked Python environment, reuses the stored Tensorlake API key, refreshes
the organization and project context secrets, rebuilds the runner image, and redeploys the
application. It does not ask for the GitHub App inputs, rotate the webhook secret, or modify the
organization webhook. The upgrade prints the deployed endpoint; if it differs from the organization
webhook URL, run `--resume-from-step-6 your-organization` to update the webhook.

### Manual setup

The wizard is recommended because it creates or updates the matching organization webhook for you.
For a manual installation, first create and install the GitHub App exactly as described above, then
store the secrets before deploying:

```bash
WEBHOOK_SECRET="$(openssl rand -hex 32)"

tl secrets set GITHUB_WEBHOOK_SECRET="${WEBHOOK_SECRET}"
tl secrets set GITHUB_APP_CLIENT_ID='Iv1...'
tl secrets set GITHUB_APP_INSTALLATION_ID='12345678'
tl secrets set GITHUB_APP_PRIVATE_KEY="$(cat private-key.pem)"
tl secrets set RUNNER_GROUP_ID='1'
tl secrets set TENSORLAKE_API_KEY='tl_apiKey_...'
tl secrets set TENSORLAKE_ORGANIZATION_ID='org_...'
tl secrets set TENSORLAKE_PROJECT_ID='project_...'
```

Use a project-scoped Tensorlake API key for `TENSORLAKE_API_KEY`; the deployed function uses it to
create and manage runner sandboxes. Run `tl whoami -o json` to find the organization and project IDs
for the active CLI context. `RUNNER_GROUP_ID` defaults to `1` if omitted. Keep `WEBHOOK_SECRET` in
the current shell until the organization webhook is created; do not enter it in the disabled GitHub
App webhook fields. Tensorlake injects stored secrets when an application is deployed. If you
change a secret later, redeploy the application so the new value takes effect.

Build the runner image, then use the resumable deployment path to deploy the application and
configure the organization webhook:

```bash
./scripts/build-runner-image.sh
GITHUB_ORG='your-organization'
WEBHOOK_SECRET="${WEBHOOK_SECRET}" ./scripts/configure-github-org.sh \
  --resume-from-step-6 "${GITHUB_ORG}"
```

The resume command uses the repository-root `app.py` deployment entrypoint so the complete
`github_runner_orchestrator` package is included. It also uses the locked project SDK to deploy,
retrieves the generated public endpoint, and updates an existing matching webhook instead of
duplicating it.

The runner image name, timeout, required label, and optional GitHub organization override are
ordinary constants near the top of `github_runner_orchestrator/app.py`. They are configuration,
not secrets.

## Build the Runner Image

The runner image is built once from the `tensorlake/ubuntu-systemd` base, then registered as the
`github-actions-runner` Tensorlake sandbox image. Following the
[Tensorlake Docker guide](https://docs.tensorlake.ai/sandboxes/docker#install-docker), it installs
Docker CE from Docker's Ubuntu repository and enables the Docker and containerd systemd services.
It also installs the Tensorlake CLI and FUSE support used to mount Cloud Volumes. Every runner waits
for Docker and, when a volume is available, its repository cache mount before invoking
`/opt/actions-runner/run.sh --jitconfig ...`.

Workflow steps run as `tl-user`, matching GitHub-hosted Linux runners' non-root privilege model.
The account has passwordless `sudo` and access to the Docker daemon. Use `sudo` explicitly for
system-level operations:

```yaml
- name: Install system dependencies
  run: |
    sudo apt-get update
    sudo apt-get install -y protobuf-compiler
```

The orchestrator explicitly selects `tl-user`; it does not depend on the sandbox image's default
process user. The Actions work directory and persistent cache mount are owned and mounted by that
same account.

The initial setup wizard performs this build after storing the secrets and before deploying the
application. Run it directly only for a manual installation or to rebuild the image:

```bash
./scripts/build-runner-image.sh
```

For a manual or repeat deployment, make sure the required secrets have already been stored, then
run the resumable deployment command:

```bash
./scripts/configure-github-org.sh --resume-from-step-6 your-organization
```

The Tensorlake application allows unauthenticated invocation for GitHub webhooks. Configure GitHub
to send `workflow_job` events directly to the deployed `github_runner_webhook` application endpoint.
The application receives the exact request bytes as an SDK `HttpBody`, verifies GitHub's HMAC
signature before accepting work, and reads the case-insensitive, sanitized `Headers` collection
from Tensorlake's request context. These APIs require `tensorlake>=0.5.95`.

## Runner Resources

Use `runs-on: [self-hosted, tensorlake]` to request a sandbox runner. Add one resource-profile label
to select its CPU, memory, and disk:

```yaml
runs-on: [self-hosted, tensorlake, tensorlake-medium]
```

| Label | CPUs | Memory | Disk |
|---|---:|---:|---:|
| no profile or `tensorlake-small` | 2 | 4 GiB | 10 GiB |
| `tensorlake-medium` | 4 | 8 GiB | 50 GiB |
| `tensorlake-large` | 8 | 16 GiB | 100 GiB |
| `tensorlake-xlarge` | 16 | 32 GiB | 100 GiB |

Profiles are defined in `github_runner_orchestrator/resources.py`; edit that mapping to expose
different sizes. Tensorlake runner disks are capped at 100 GiB. Requests with multiple
resource-profile labels are rejected. Docker is installed and started for every profile, so
workflows do not need a Docker-specific label.

## Persistent Workflow Cache

Every GitHub repository gets its own Tensorlake Cloud Volume on its first runner job. The
application finds or creates the volume with Tensorlake's `FilesystemClient`. Empty volumes are
initialized with a small `.tensorlake-cache-initialized` marker so they have a generation that can
be mounted. The runner image then starts `tl fs mount` inside the sandbox at
`/mnt/tensorlake-cache`, and the orchestrator exports that path to the job as
`TENSORLAKE_CACHE_DIR`. Later sandboxes for the same repository mount the same volume, while other
repositories receive separate volumes.

The mount receives a short-lived credential scoped to that one volume. The project API key remains
inside the orchestrator function and is not passed to the runner sandbox or GitHub workflow.

The cache is a normal writable directory rather than an `actions/cache`-compatible archive
service. A workflow opts in by pointing a tool's cache directory at a namespaced child directory:

```yaml
jobs:
  build:
    runs-on: [self-hosted, tensorlake, tensorlake-medium]
    steps:
      - uses: actions/checkout@v6

      - name: Configure a persistent tool cache
        shell: bash
        run: |
          tool_cache="${TENSORLAKE_CACHE_DIR}/my-tool/${RUNNER_OS}-${RUNNER_ARCH}"
          mkdir -p "${tool_cache}"
          echo "MY_TOOL_CACHE=${tool_cache}" >> "${GITHUB_ENV}"

      - run: my-tool build
```

Use distinct directories for operating systems, CPU architectures, toolchains, build targets,
profiles, features, or other inputs that make cached files incompatible. Cache only reproducible,
disposable data. Never write tokens, package-manager credentials, Docker configuration, signing
material, or other secrets into the mounted directory. Pull requests and branches in the same
repository share this cache security boundary, so do not expose the self-hosted runner to
untrusted workflow code.

Cloud Volume writes autosave while the job runs. After the Actions runner exits, the orchestrator
syncs the sandbox and retries a safe unmount until the filesystem confirms that no unsaved work
would be lost. Cache provisioning and mounting are best-effort: if Tensorlake cannot prepare the
volume, the job still runs without `TENSORLAKE_CACHE_DIR`. The application emits structured logs
with repository, sandbox, volume, stage, and error fields for provisioning or mount failures.

The reference workflow deliberately verifies both `TENSORLAKE_CACHE_DIR` and its mountpoint before
using the cache. This makes a cache regression fail visibly instead of writing to an identically
named directory on the sandbox's ephemeral disk. Workflows that prefer best-effort caching can omit
that verification.

### uv

The runner deliberately exports only the generic `TENSORLAKE_CACHE_DIR`; it does not set
tool-specific cache variables. Point `setup-uv` at a child directory with its `cache-local-path`
input. Its default `enable-cache: auto` mode does not upload a GitHub Actions cache from a
self-hosted runner:

```yaml
- uses: actions/checkout@v6

- uses: astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990 # v8.3.2
  with:
    cache-local-path: /mnt/tensorlake-cache/uv

- run: uv sync --locked
```

`setup-uv` sets `UV_CACHE_DIR` for the job from this input. If cache provisioning is unavailable,
the same path is created on the sandbox's ephemeral disk, so a workflow that omits the verification
step still runs without persistence.

The cache and `.venv` are on different file systems, so uv may copy artifacts instead of
hard-linking them. The volume still avoids repeated downloads and source builds, but benchmark the
result for dependency sets dominated by small prebuilt wheels.

### Rust, Cargo, and sccache

For Rust compiler artifacts, prefer sccache over sharing a complete mutable Cargo `target` tree.
Give each compatible build family its own cache namespace:

```yaml
- uses: actions/checkout@v6

- uses: actions-rust-lang/setup-rust-toolchain@v1
  with:
    cache: false

# Install sccache with your preferred pinned action, package, or runner image.
- name: Configure persistent Rust caches
  shell: bash
  env:
    CACHE_NAMESPACE: rust-workspace-v1
  run: |
    platform="${RUNNER_OS}-${RUNNER_ARCH}"
    cargo_home="${TENSORLAKE_CACHE_DIR}/cargo-home/${platform}"
    sccache_dir="${TENSORLAKE_CACHE_DIR}/sccache/${platform}/${CACHE_NAMESPACE}"
    mkdir -p "${cargo_home}" "${sccache_dir}"
    echo "CARGO_HOME=${cargo_home}" >> "${GITHUB_ENV}"
    echo "SCCACHE_DIR=${sccache_dir}" >> "${GITHUB_ENV}"
    echo "SCCACHE_CACHE_SIZE=50G" >> "${GITHUB_ENV}"
    echo "SCCACHE_BASEDIRS=${GITHUB_WORKSPACE}" >> "${GITHUB_ENV}"
    echo "RUSTC_WRAPPER=sccache" >> "${GITHUB_ENV}"

- name: Build
  run: |
    sccache --start-server
    cargo build --locked --workspace
    sccache --show-stats

- name: Stop sccache
  if: always()
  run: sccache --stop-server || true
```

Persisting `CARGO_HOME` reuses registry downloads, Git dependencies, and installed Cargo tools.
Authenticate private registries with environment variables; do not run `cargo login` against the
persistent `CARGO_HOME`, because it writes credentials. Cloud Volumes reconcile concurrent
same-path writes with last-writer-wins semantics; local Cargo and sccache locks should therefore not
be treated as a distributed lock across sandboxes. Use separate namespaces for concurrently running
job families, or serialize writers to a shared namespace.

Some release builds benefit from reusing the complete `target` directory. Give each ABI-compatible
build family an explicit versioned namespace:

```yaml
- name: Select Cargo target cache
  shell: bash
  env:
    # Include the runtime ABI, Rust target, profile, and a manual version bump.
    CACHE_NAMESPACE: rust-release-glibc-2.35-x86_64-unknown-linux-gnu-v2
  run: |
    target_dir="${TENSORLAKE_CACHE_DIR}/cargo-target/${CACHE_NAMESPACE}"
    mkdir -p "${target_dir}"
    echo "CARGO_TARGET_DIR=${target_dir}" >> "${GITHUB_ENV}"

- run: cargo build --locked --release --target x86_64-unknown-linux-gnu
```

Cargo target directories contain mutable incremental state. Do not let incompatible jobs share a
namespace, and serialize concurrent writers to the same namespace with a GitHub `concurrency`
group. For broadly parallel checks, use sccache and keep `target` on the sandbox's local disk.

For repositories with several Rust workflows:

- Remove an existing cache action only after pointing the same tool directories at the mounted
  cache.
- Set `cache: false` on toolchain setup actions when the mounted volume owns those files.
- Do not combine the mounted cache with another cache backend for the same directory.
- Preserve compatibility distinctions such as workspace, runtime ABI, architecture, Rust target,
  build profile, and a manual cache version.
- Keep Docker BuildKit `registry` or `gha` cache backends. Do not place `/var/lib/docker` on a
  Cloud Volume.

### Exact binary caches

For outputs keyed by an immutable source commit, store the completed file under that key and publish
it with a rename:

```yaml
- name: Restore or build Firecracker
  shell: bash
  run: |
    source_sha="$(git -C firecracker rev-parse HEAD)"
    cache_dir="${TENSORLAKE_CACHE_DIR}/binaries/firecracker/${RUNNER_OS}-${RUNNER_ARCH}"
    cached_binary="${cache_dir}/${source_sha}"
    output="firecracker/build/cargo_target/x86_64-unknown-linux-musl/release/firecracker"
    mkdir -p "${cache_dir}" "$(dirname "${output}")"

    if [[ -x "${cached_binary}" ]]; then
      cp "${cached_binary}" "${output}"
    else
      cargo build \
        --manifest-path firecracker/Cargo.toml \
        --release \
        --target x86_64-unknown-linux-musl \
        -p firecracker
      temporary="${cached_binary}.tmp.${GITHUB_RUN_ID}.${GITHUB_RUN_ATTEMPT}"
      cp "${output}" "${temporary}"
      chmod +x "${temporary}"
      mv -f "${temporary}" "${cached_binary}"
    fi
```

### Retention and cleanup

Cloud Volumes are mutable shared storage, so GitHub cache eviction rules do not apply. Package
managers should prune their own caches, and obsolete manually versioned directories should be
removed explicitly. Use `tl fs ls` to list the generated `github-actions-cache-*` volumes and
`tl fs rm <name>` to delete a repository cache that is no longer needed. Do not create permanent
volume snapshots for disposable cache data.

## Self-Test Workflow

`.github/workflows/build-reference.yml` runs on a `tensorlake-small` runner. It points uv at the
persistent volume automatically when available and uses the sandbox-local uv cache otherwise. It
installs and lints the Python project, builds its distribution, validates the setup scripts, runs
the test suite, and runs Docker's `hello-world` image to verify the runner's Docker daemon. The
reusable runner sandbox image still builds through `scripts/build-runner-image.sh`, because
`tensorlake/ubuntu-systemd` is a Tensorlake registered base rather than a public Docker Hub image.
Once the organization webhook is configured, pushes to `main`, pull requests, and manual dispatches
exercise the runner implementation from its own repository.
