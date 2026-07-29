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
5. `run_github_runner` mints GitHub App credentials, creates a JIT self-hosted runner config, starts a
   fresh Tensorlake Sandbox from a prebuilt runner image through `AsyncSandbox`, runs exactly one
   GitHub Actions job, and terminates the sandbox.

## Files

- `github_runner_orchestrator/app.py` - Tensorlake application and functions.
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
4. Store its client ID, installation ID, private key, runner group ID, and a newly generated webhook
   secret in Tensorlake. Tensorlake secrets exist at the project level and do not require an
   application deployment to exist first; see the
   [secrets documentation](https://docs.tensorlake.ai/applications/secrets).
5. Build the reusable runner sandbox image.
6. Deploy the Tensorlake application. The deployment reads the stored secrets and returns a public
   endpoint URL.
7. Create a separate organization webhook for `workflow_job` events using that endpoint and the
   same webhook secret stored in step 4.

The wizard performs all of these steps. It writes sensitive values only to permission-restricted
temporary files, deletes those files when it exits, and never prints the private key or webhook
secret.

If setup reaches deployment and stops, resume without repeating the GitHub App inputs or rebuilding
the runner image:

```bash
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}" ./scripts/configure-github-org.sh \
  --resume-from-step-6 your-organization
```

When `WEBHOOK_SECRET` is still available in the current shell, the resume command reuses it.
Otherwise it safely rotates the stored webhook secret before deploying, then creates or updates the
organization webhook with the same new value.

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
```

`RUNNER_GROUP_ID` defaults to `1` if omitted. Keep `WEBHOOK_SECRET` in the current shell until the
organization webhook is created; do not enter it in the disabled GitHub App webhook fields.
Tensorlake injects stored secrets when an application is deployed. If you change a secret later,
redeploy the application so the new value takes effect.

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
Every runner waits for Docker to become ready before invoking
`/opt/actions-runner/run.sh --jitconfig ...`.

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
from Tensorlake's request context. These APIs require `tensorlake>=0.5.92`.

## Runner Resources

Use `runs-on: [self-hosted, tensorlake]` to request a sandbox runner. Add one resource-profile label
to select its CPU, memory, and disk:

```yaml
runs-on: [self-hosted, tensorlake, tensorlake-medium]
```

| Label | CPUs | Memory | Disk |
|---|---:|---:|---:|
| no profile or `tensorlake-small` | 2 | 4 GiB | 50 GiB |
| `tensorlake-medium` | 4 | 8 GiB | 100 GiB |
| `tensorlake-large` | 8 | 16 GiB | 200 GiB |

Profiles are defined in `github_runner_orchestrator/resources.py`; edit that mapping to expose
different sizes. Requests with multiple resource-profile labels are rejected. Docker is installed
and started for every profile, so workflows do not need a Docker-specific label.

## Self-Test Workflow

`.github/workflows/build-reference.yml` runs on a `tensorlake-medium` runner. It installs and lints
the Python project, builds its distribution, validates the setup scripts, runs the test suite, and
runs Docker's `hello-world` image to verify the runner's Docker daemon. The reusable runner sandbox
image still builds through `scripts/build-runner-image.sh`, because `tensorlake/ubuntu-systemd` is a
Tensorlake registered base rather than a public Docker Hub image. Once the organization webhook is
configured, pushes to `main`, pull requests, and manual dispatches exercise the runner
implementation from its own repository.
