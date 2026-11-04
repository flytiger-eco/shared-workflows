# Docker Image Publishing Template

Build a `linux/amd64` image from the caller repository, push it to GHCR, the Flytiger registry, or both according to `push_target`, and inspect its size. Defaults are `push_target: github`, `release_type: tmp`, and `publish_data: false`. The metadata publishing script runs only when the target includes `flytiger`, `release_type: release`, and `publish_data: true`.

- Reusable workflow: [`.github/workflows/build-and-push-docker.yml`](../../.github/workflows/build-and-push-docker.yml)
- Complete usage example: [`workflows/demo-build-and-push-docker.yml`](workflows/demo-build-and-push-docker.yml)
- Input validation: [`scripts/validate_inputs.py`](scripts/validate_inputs.py)
- Image size inspection: [`scripts/inspect_image.py`](scripts/inspect_image.py)
- Payload assembly and API client: [`scripts/post_data.py`](scripts/post_data.py)

This is a reusable workflow called through `jobs.<job_id>.uses`, not a regular Action used in `steps`.

## Quick Start

1. Commit the Dockerfile, required build files, and an appropriate `.dockerignore` in the caller repository.
2. Copy the [demo](workflows/demo-build-and-push-docker.yml) to `.github/workflows/demo-build-and-push-docker.yml` in the caller repository. This directory's `workflows/` contains examples only; GitHub does not execute them automatically.
3. Grant `contents: read` and `packages: write` permissions in the caller workflow. GHCR uses the caller's `GITHUB_TOKEN` without additional Secrets. `RELEASE_USERNAME` and `RELEASE_TOKEN` are required only when the target includes `flytiger`, and the account must have push and read permissions for the selected Flytiger repository. `DATA_TOKEN` is required only when `release_type: release` and `publish_data: true` are also set.
4. Update the fixed `os`, `python`, `cuda`, `sdk_version`, and `torch` values in the demo to match the actual image environment. These values are release metadata only; they do not automatically install dependencies or change the build.
5. Ensure the selected template revision contains the workflow and all scripts. The example uses `flytiger-eco/shared-workflows@main`; for production, replace both `@main` in `uses` and `template_ref: main` with the same full commit SHA from the template repository. When using a fork, update both the repository path in `uses` and `template_repository`.
6. Commit the caller workflow to the caller's default branch. On the Actions page, select **Run workflow**, choose a trusted branch, and enter the base image name, network type, and other parameters. The image tag is optional; leave it empty to use the current UTC date.
7. After a successful run, find the image address, digest, and size in the logs, job summary, or the demo's `result` job.

**The demo builds and pushes real images and publishes metadata when the conditions are met; it is not a dry run.** Use only trusted repositories, branches, and Dockerfiles.

Private templates must allow cross-repository calls in their Actions settings; enable the demo's `template_token` mapping as needed. For same-repository relative-path calls, use `uses: ./.github/workflows/build-and-push-docker.yml` with `template_repository: ${{ github.repository }}` and `template_ref: ${{ github.sha }}`. For cross-repository calls, do not use the caller's `github.sha` as the template revision.

## Execution Flow and Destination

```text
Check out caller source and template tools into separate directories
  -> Validate build inputs, release fields, and non-empty credentials
  -> Install API dependencies, set up Buildx, and log in to the registry
  -> Build and push a linux/amd64 image
  -> Inspect the manifest using the pushed digest and calculate size
  -> Output image, digest, and size
  -> Run pip freeze inside the pushed image and collect installed package names and versions
  -> Assemble DOCKER metadata, encrypt and sign the request, and call the publishing API
```

All combinations build and push the image and inspect its size. The data key is validated, API dependencies are installed, the package list is collected, and metadata is published only when the target includes `flytiger`, `release_type: release`, and `publish_data: true`.

| `push_target` | Image destination | Metadata publishing conditions |
| --- | --- | --- |
| `github` (default) | Only the caller repository's GHCR namespace | Always skipped |
| `flytiger` | Only `pkg.flytiger-eco.com` | `release_type: release` and `publish_data: true` |
| `github/flytiger` | GHCR and `pkg.flytiger-eco.com` | `release_type: release` and `publish_data: true` |

GHCR addresses use the lowercase form of the caller's `github.repository`, not the template repository. The image is linked to the caller repository through the `org.opencontainers.image.source` label:

```text
ghcr.io/<owner>/<repo>/<docker_name>-<git_tag_or_commit>:<docker_tag>
```

The Flytiger address is determined by `release_type`: `release` maps to `docker_release`, and `tmp` maps to `docker_tmp`:

```text
pkg.flytiger-eco.com/<docker_release_or_docker_tmp>/<docker_name>-<git_tag_or_commit>:<docker_tag>
```

Dual-target mode builds once and tags the same build result with both destination addresses. Size inspection and metadata publishing use the Flytiger address. A failure to push to either destination fails the job and prevents metadata publishing; images already pushed are not automatically rolled back. GHCR addresses do not distinguish `release_type`, so the same revision and Docker tag use the same address.

The suffix comes from the caller source's checked-out `HEAD`, not the template repository: use a Git tag pointing exactly at that commit, or the full commit SHA when no exact tag exists. Both lightweight and annotated tags are supported. If multiple tags point at the commit, the first in lexicographic name order is used; tags on ancestor commits are ignored. The Git tag must be a valid lowercase Docker name component; unsupported characters (such as uppercase letters, `/`, or `+`) fail validation rather than being silently rewritten.

`docker_tag` is independent of the Git tag. If omitted or set to an empty string, it defaults to the UTC date at input validation time in `YYYY-MM-DD` format. The resolved suffix and Docker tag are reused for building, inspection, and metadata publishing, even if the build crosses midnight.

Examples with `docker_name: team/demo` (all use the prefix `pkg.flytiger-eco.com/docker_release/`):

| Checked-out commit | `docker_tag` input | Image name and tag |
| --- | --- | --- |
| Tagged `v1.2.3` | `1.0.0` | `team/demo-v1.2.3:1.0.0` |
| Untagged | `1.0.0` | `team/demo-<full_commit_sha>:1.0.0` |
| Tagged `v1.2.3` | Omitted, on UTC 2026-10-28 | `team/demo-v1.2.3:2026-10-28` |
| Untagged | Omitted, on UTC 2026-10-28 | `team/demo-<full_commit_sha>:2026-10-28` |

Repeated builds of the same revision on the same UTC day target the same image address unless a distinct `docker_tag` is supplied.

The Flytiger Artifactory URL is `https://pkg.flytiger-eco.com/artifactory/docker_release/` or `https://pkg.flytiger-eco.com/artifactory/docker_tmp/`, while the Docker login address is only `pkg.flytiger-eco.com`. Image addresses do not include `https://` or `/artifactory/`; the server must configure the corresponding path mappings.

## Inputs

`publish_data` has type `boolean` and accepts only unquoted `true`/`false`. All other inputs have type `string`; quote version numbers. `workflow_call` does not support `choice`, so the script strictly validates both enum inputs. The manually triggered demo uses dropdown options.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `release_type` | No | `tmp` | Only `release` or `tmp`; selects the Flytiger repository path without changing the GHCR address. |
| `publish_data` | No | `false` | Enables metadata publishing only when the target includes `flytiger` and the release type is `release`. |
| `push_target` | No | `github` | Only `github`, `flytiger`, or `github/flytiger`, case-sensitive. |
| `dockerfile` | Yes | None | Dockerfile path relative to the caller repository root, such as `docker/Dockerfile`. |
| `build_context` | No | `.` | Local build context directory relative to the caller repository root. |
| `build_args` | No | Empty string | One `KEY=VALUE` per line, passed to Docker ARG; not shell or Buildx command-line options. |
| `docker_name` | Yes | None | Lowercase base image name, optionally namespaced, such as `team/demo`; without a registry, `docker_release/` or `docker_tmp/` prefix, or tag. The Git tag or full commit SHA is appended automatically. |
| `docker_tag` | No | Current UTC date (`YYYY-MM-DD`) | Omitted or empty uses the date. Otherwise, a single tag of up to 128 characters, such as `1.0.0`; `+`, spaces, `/`, and `:` are not supported. |
| `os` | Yes | None | Operating system release metadata, such as `ubuntu24.04`; does not select the runner. |
| `python` | Yes | None | Python version, such as `3.12`. |
| `cuda` | Yes | None | CUDA version, such as `13.0.0`. |
| `sdk_version` | Yes | None | SDK version, such as `2.2.0`. |
| `torch` | Yes | None | PyTorch version, such as `2.13.0`. |
| `network` | Yes | None | Exactly one of `mellanox`, `EIC`, or `mellanox/EIC`, case-sensitive. |
| `template_repository` | No | `flytiger-eco/shared-workflows` | Template repository in `owner/repo` format. |
| `template_ref` | Yes | None | Template script revision; must match the ref used in `uses`. |

All six release fields must be non-empty printable strings without surrounding whitespace. Apart from the `network` enum, no additional version format restrictions apply. These fields do not automatically modify tags, become Docker ARG values, set image labels, select a different runner, or configure the build network.

Both the Dockerfile and build context paths are relative to the caller repository root, not to each other. The Dockerfile must be a non-empty regular file, and the context must be a directory. Absolute paths, `..`, remote URLs, and symlinks pointing outside the repository are not supported. The full image repository name, including the registry, repository namespace, and Git tag or commit suffix, must not exceed 255 characters.

Variable names in `build_args` must match `[A-Za-z_][A-Za-z0-9_]*` and must not be duplicated; empty values and blank lines are allowed. Never pass passwords or tokens, because build arguments may appear in image history or provenance. The template does not provide BuildKit secret mount inputs. The Dockerfile must declare and use the corresponding ARG values; `docker_tag` does not automatically become a build argument.

## Secrets

| Template Secret | Required | Caller Secret in the Demo | Purpose |
| --- | --- | --- | --- |
| `release_username` | When the target includes `flytiger` | `RELEASE_USERNAME` | Flytiger image registry username. |
| `release_token` | When the target includes `flytiger` | `RELEASE_TOKEN` | Token with push and read permissions for the selected Flytiger repository. |
| `data_token` | When the target includes `flytiger`, `release_type: release`, and `publish_data: true` | `DATA_TOKEN` | Base64-encoded unencrypted DER (PKCS8) client RSA private key, not a Bearer token. |
| `template_token` | No | `TEMPLATE_TOKEN` | Token for reading a private template repository; defaults to `github.token`, which may lack cross-repository read access. |

The workflow declares `contents: read` and `packages: write`; the caller must also grant both permissions. GHCR login uses `github.actor` and the caller's `github.token`. If an existing GHCR package is not linked to the caller repository, grant that repository Actions write access in the package settings. Organization policies must also allow package publishing. Credentials are injected only into the relevant steps and must not be placed in source code, logs, or build arguments.

## Outputs and Size Definition

| Workflow Output | Description |
| --- | --- |
| `image` | Primary image address: the Flytiger address when the target includes `flytiger`, otherwise the GHCR address. |
| `github_image` | Full GHCR image address; empty when `github` is not selected. |
| `flytiger_image` | Full Flytiger image address; empty when `flytiger` is not selected. |
| `digest` | Raw digest returned by Buildx, including the `sha256:` prefix; may identify a single image manifest or an image index. |
| `size` | Total size in bytes of the `linux/amd64` image config and stored layers; exposed as a decimal string in GitHub Actions outputs. |

The demo reads outputs through `${{ needs.publish.outputs.image }}`, `${{ needs.publish.outputs.digest }}`, and `${{ needs.publish.outputs.size }}`. By default, downstream jobs run only after the entire caller job succeeds, including metadata publishing.

Size inspection uses `repository@digest`, without relying on a mutable tag or downloading image layers. If the response is an image index, it selects the unique `linux/amd64` runtime image and ignores other platforms and attestations. `size` includes the config and stored layers, which are typically compressed. It excludes manifest JSON, other platforms, and provenance/SBOM; it is not the locally unpacked image size. The manifest JSON size from an index descriptor is not used as the image size.

## Payload Assembly and Publishing API

`build_payload(record)` is implemented and produces the business object below. The API client is maintained independently in this template and does not depend on the PyPI template.

Top-level fields:

| Field | Source or Fixed Value |
| --- | --- |
| `artifactName` | `docker_name`. |
| `businessType` | Fixed to `DOCKER`. |
| `idempotentKey` | MD5 of the full UTF-8-encoded `image` string, represented as 32 lowercase hexadecimal characters. |
| `attributes` | Attribute object described below; all values are single-element lists of strings. |
| `packageList` | Packages installed in the pushed image, as objects containing `packageName` and `packageVersion`. |

| `attributes` Field | Value |
| --- | --- |
| `name` | `[docker_name]` |
| `os` | `[os]` |
| `python` | `[python]` |
| `cuda` | `[cuda]` |
| `image` | `[image]`, including the full image name and tag. |
| `digest` | A list containing the 64-character lowercase hexadecimal digest with the `sha256:` prefix removed. |
| `ppu_sdk_version` | `[sdk_version]` |
| `package_size` | `[str(size)]`, the byte count as a string. |
| `torch` | `[torch]` |
| `nicType` | `[network]`; `mellanox/EIC` remains a single value rather than being split into two elements. |

`read_record()` returns 13 keys: `os`, `python`, `cuda`, `sdk_version`, `torch`, `network`, `docker_name`, `docker_tag`, `image`, `digest`, `size`, `idempotentKey`, and `packageList`. `size` is an integer and `packageList` is a list of objects; all other values are strings. `docker_name` remains the caller's base name for `artifactName` and `attributes.name`; `image` includes the Git tag or commit suffix. `docker_tag` contains the resolved explicit tag or UTC date and is included in `image` rather than mapped to a separate attribute. Build arguments and credentials are excluded from the business object.

Package collection runs `pip freeze --all` inside a `linux/amd64` container addressed by the pushed `repository@digest`, not the mutable tag or the runner's Python environment. `--all` also includes packaging tools such as pip. Lines such as `torch==2.5.8` become `{"packageName": "torch", "packageVersion": "2.5.8"}`; version suffixes such as `+cu124` are preserved. Blank lines, comments, malformed entries, and URL or editable references without a parseable version are skipped individually. Valid entries are deduplicated and sorted by normalized package name; the first version is retained for duplicate names. Parsing never blocks publishing, and no fallback command is run for skipped entries.

Package collection uses the `pip` on the image's configured `PATH`; only that Python environment is inspected, and an unavailable pip produces an empty list. The original image entrypoint is bypassed, so virtual environments must already be configured through the image environment. Containers have networking disabled and receive no host mounts or publishing credentials. The Docker daemon can pull the image using the existing registry login. Each collection command has a 15-minute timeout; failed or timed-out runs attempt container cleanup. Collection failures (including missing pip, Docker errors, timeouts, and output decoding errors) yield `packageList: []` without logging an error or blocking API publishing. Cleanup failures are also ignored. If parsing finds no valid entries, the list is empty; otherwise, successfully parsed entries are retained. Existing release metadata, image, digest, and size validation remains mandatory. `packageList` is independent of the caller-supplied `torch` release metadata.

The same image address produces the same `idempotentKey`, even if the digest or size changes; a different image name or tag produces a newly calculated key. This rule defines only the client-side key; actual deduplication or overwrite behavior depends on the server. Use distinct tags for different releases.

The publishing request is `POST https://www.flytiger-eco.com/open/flyingTiger/synchronize/artifact/save`. The business object is encrypted with AES-256-GCM, the AES key is encrypted with the server's RSA public key, and the request is signed with the client's private key. Public API configuration is maintained in `post_data.py`; the private key is injected from a Secret. Requests are not retried automatically and do not follow redirects. Non-2xx or HTML responses fail the request. The client currently does not validate business success codes in JSON responses; server-side field constraints and success response formats still require live integration verification.

## Failure Behavior and Limitations

- Build, push, or size inspection failures prevent metadata publishing. API publishing failures also fail the workflow, but already-pushed images are not rolled back.
- The runner is fixed to `ubuntu-latest`, the platform to `linux/amd64`, and the job timeout to 120 minutes. The template does not provide login configuration for other registries.
- Dockerfile RUN instructions execute code; path validation is not a security audit. Build only trusted code and exclude sensitive files with `.dockerignore`.
- Caller source and template tools are checked out separately and do not share local build artifacts from other jobs. Required files must be committed or generated by Dockerfile build steps.
- The template does not perform image functional tests, vulnerability scans, tag overwrite prevention, or automatic rollback. Network connectivity, registry permissions, and actual build results must be verified in the runtime environment.

## Local Offline Tests

Run from the repository root in a prepared Python 3.12 environment:

```bash
python -m pip install --no-input pytest PyYAML requests cryptography
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-templates/build-and-push-docker/tests ci-templates/build-and-push-tmp-docker/tests -q -p no:cacheprovider
```

Tests cover input validation, field mapping, all three network enum values, encryption and signing, publishing success and failure propagation, manifest size calculation, workflows, and usage examples. Tests do not connect to live APIs or registries, build images, or push images.
