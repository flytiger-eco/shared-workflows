# Upload Artifact With Properties Template

Download and extract the specified Actions artifact from the current GitHub Actions run, PUT every regular file inside it unchanged to a versioned Artifactory path, and set the configured fields as Artifactory item properties.

Upload destination:

```text
https://pkg.flytiger-eco.com/artifactory/github_generic/{owner}/{repository}/{run_id}_{run_attempt}/{version}/{filename}
```

`owner` and `repository` come from the caller's `${{ github.repository }}`, passed to the script as `GITHUB_REPOSITORY` in `owner/repository` format. The owner is the organization name (or username for a personal repository), not the template repository owner. `run_id` and `run_attempt` come from `${{ github.run_id }}` and `${{ github.run_attempt }}` and form one directory named `{run_id}_{run_attempt}`. These values require no caller inputs; the `version` and `filename` segments are unchanged. The service root (`ARTIFACTORY_URL`) and repository key (`ARTIFACTORY_REPOSITORY`, fixed to `github_generic`) are defined inside [`scripts/upload_artifact.py`](scripts/upload_artifact.py). Together they form `ARTIFACTORY_BASE_URL`; neither is a workflow input.

After each successful upload, the template sets properties on that same item with an empty-body PUT to:

```text
https://pkg.flytiger-eco.com/artifactory/api/storage/github_generic/{owner}/{repository}/{run_id}_{run_attempt}/{version}/{filename}?properties=key1=value1;key2=value2&recursive=0
```

The `/api/storage/` prefix belongs before the repository key, not inside the repository. The property request must return `204 No Content`; a `201 Created` response is not accepted as a successful property update. See the [Set Item Properties API](https://docs.jfrog.com/artifactory/reference/setitemproperties).

Four properties (`branch`, `tag`, `commit`, `log_url`) are derived from GitHub Actions context in the template workflow and injected into the script through environment variables (`GITHUB_BRANCH`, `GITHUB_TAG`, `GITHUB_SHA`, `GITHUB_LOG_URL`); they are not caller inputs and cannot be overridden by the calling repository.

- Workflow entry point: [`.github/workflows/upload-artifact-properties.yml`](../../.github/workflows/upload-artifact-properties.yml)
- Upload script: [`scripts/upload_artifact.py`](scripts/upload_artifact.py)
- Complete usage example: [`workflows/demo-upload-artifact-properties.yml`](workflows/demo-upload-artifact-properties.yml)

This is a reusable workflow called through `jobs.<job_id>.uses`, not a regular Action used in `steps`. The template only exposes `workflow_call`. It does not build, validate, repackage, or rename files, and does not create GitHub Releases or package indexes.

## Execution Flow

```text
Caller produces the artifact directory
  → actions/upload-artifact@v4 saves it in the current run
  → Call the template with needs on the producing job
  → actions/download-artifact@v4 downloads and extracts the named artifact
  → Recursively collect every regular file (symlinks are skipped, directory symlinks are not followed)
  → For each file:
      → HTTPS PUT to the versioned Artifactory path
      → HTTPS PUT to /api/storage/...?properties=... to attach metadata
  → Emit batch_url and upload_urls after all files succeed
```

The reusable workflow runs in a separate job and does not share local files with the caller's previous job. Build outputs must be transferred through Actions artifacts. Nested ZIP or tar files inside the artifact are not unpacked.

## Quick Start

1. Copy [`workflows/demo-upload-artifact-properties.yml`](workflows/demo-upload-artifact-properties.yml) to `.github/workflows/demo-upload-artifact-properties.yml` in the project to publish.
2. Configure `RELEASE_USERNAME` and `RELEASE_TOKEN` in the caller repository or organization Actions Secrets. The account needs PUT permission on both the destination path and on the item properties in the target Artifactory repository.
3. Ensure the caller can use the template repository workflows. Private template repositories must also allow cross-repository calls in Actions settings; configure `TEMPLATE_TOKEN` and uncomment the corresponding example line as needed.
4. Replace the demo staging step with your real build command. Every regular file inside the saved artifact directory is uploaded; if only a subset should be published, upload a filtered artifact.
5. Adjust the manual inputs to describe the artifact: `version` and the 10 property fields listed below (`branch`, `tag`, `commit`, `log_url` come from GitHub context automatically).
6. After committing the example to the caller default branch, trigger it manually through **Run workflow** on the Actions page and select a trusted branch.

The example actually uploads to Artifactory; it is not a dry run. Files under this directory's `workflows/` are provided for copying and are not automatically registered as Actions workflows in this repository.

### Template Version and Repository

The example uses the `main` branch of `flytiger-eco/shared-workflows`. Ensure the referenced revision contains this workflow and the helper script before running it.

- For production, replace both `@main` in `uses` and `template_ref: main` with the same full commit SHA from the template repository to prevent workflow and script version drift.
- Do not use the caller's `github.sha` as `template_ref` for cross-repository calls.
- When using a fork, update both the repository path in `uses` and `template_repository`.
- For same-repository relative-path calls, use `uses: ./.github/workflows/upload-artifact-properties.yml` with `template_repository: ${{ github.repository }}` and `template_ref: ${{ github.sha }}`.

The caller must grant at least `contents: read`. The template job uses `ubuntu-latest` and Python 3.12 with a 30-minute timeout. Its runner must reach `pkg.flytiger-eco.com` over HTTPS and download Actions artifacts. The upload script uses only the Python standard library and curl.

## Inputs

All inputs have type `string`.

### Path and Transport Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `artifact_name` | Yes | None | Name of the artifact saved by the caller using `actions/upload-artifact@v4`; must belong to the current workflow run. |
| `version` | Yes | None | Version path segment placed after `{run_id}_{run_attempt}`; may differ from the `package_version` property when the release layout groups by a different value. |
| `template_repository` | No | `flytiger-eco/shared-workflows` | Repository containing the template script, in `owner/repo` format. |
| `template_ref` | Yes | None | Template script version; must match the workflow version referenced by `uses`. |

### Property Inputs

Every field below is written as an Artifactory item property with the same name. Pass an empty string when a value does not apply; the property is still set with an empty value except for the required fields listed in the file selection section.

| Input | Required | Description |
| --- | --- | --- |
| `package_type` | Yes | Package type, such as `wheel`, `sdist`, `docker`, or `raw`. |
| `package_name` | Yes | Package name. |
| `package_version` | Yes | Package version, typically matching the released distribution. |
| `os` | Yes | Operating system, such as `ubuntu24.04`. |
| `python_version` | Yes | Python version, such as `3.12`. |
| `runtime` | Yes | Runtime version, such as a CUDA version. |
| `torch_version` | Yes | PyTorch version; pass an empty string when Torch is not applicable. |
| `sdk_version` | Yes | SDK version. |
| `package_edition` | Yes | Package edition, such as `standard` or `enterprise`. |
| `extra_info` | No | Free-form extra information, such as a JSON string. |

The query uses `properties=key1=value1;key2=value2`. Each field remains a single property value: literal commas, backslashes, pipes, and equals signs are backslash-escaped according to the Artifactory protocol before URL encoding. For example, `1=1` becomes `1%5C%3D1`, and `a,b` becomes `a%5C%2Cb`. Semicolons and other URL-sensitive characters inside values are percent-encoded; only the separators between property pairs remain literal semicolons.

### Context-derived Properties

The following item properties are populated by the template workflow from GitHub Actions context and are not exposed as inputs:

| Property | Source expression | Notes |
| --- | --- | --- |
| `branch` | `${{ github.ref_type == 'branch' && github.ref_name || '' }}` | Empty on tag triggers. |
| `tag` | `${{ github.ref_type == 'tag' && github.ref_name || '' }}` | Empty on branch triggers. |
| `commit` | `${{ github.sha }}` | Always set to the SHA of the triggering revision. |
| `log_url` | `${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}` | Link to the workflow run page. |

## Secrets

| Template Secret | Required | Caller Secret in the Example | Description |
| --- | --- | --- | --- |
| `release_username` | Yes | `RELEASE_USERNAME` | Artifactory publishing username. |
| `release_token` | Yes | `RELEASE_TOKEN` | Artifactory publishing credential with PUT permission for both the destination path and the item properties. |
| `template_token` | No | `TEMPLATE_TOKEN` | Read-only token for checking out a private template repository; defaults to `github.token`, which may lack cross-repository read access. |

The example explicitly maps credentials through `jobs.publish.secrets` and passes publishing credentials only to the upload job. Credential values are not written into YAML, environment echoes, or output logs.

## Outputs

| Output | Description |
| --- | --- |
| `batch_url` | Directory URL shared by every uploaded file, ending with a trailing slash; available through `${{ needs.publish.outputs.batch_url }}`. |
| `upload_urls` | Newline-separated list of the uploaded file URLs, in sorted local-path order; available through `${{ needs.publish.outputs.upload_urls }}`. |

Both outputs are emitted only after every file uploads and every property setter succeeds. Individual file URLs are also logged line by line in the upload step.

## File Selection and Properties

Every regular file inside the downloaded artifact is uploaded, including files in nested or hidden directories. Empty files, arbitrary names, and unsupported formats are not rejected. The script does not open, extract, install, or execute files; the caller is responsible for their contents. Other file types (directories, symlinks) are skipped, and directory symlinks are not followed so files outside the artifact are never uploaded.

The upload URL always uses only the file basename. Files with the same basename in different artifact subdirectories target the same URL; later uploads may overwrite earlier ones depending on Artifactory permissions. Use distinct filenames to preserve every file.

Required property inputs (`artifact_name`, `version`, plus every input-sourced property field except `extra_info`) must not be empty; empty values abort the job before any PUT request is sent. Context-derived properties follow the same rule: `commit` and `log_url` must be non-empty, while `branch` and `tag` may be empty depending on the trigger ref type.

## Upload and Failure Behavior

- Both requests use HTTPS PUT with the username and token sent through Basic Auth. Redirects are not followed to avoid forwarding files or publishing credentials to other destinations.
- Files upload sequentially in sorted local-path order. If any upload or property-set request fails, the job stops and emits neither `batch_url` nor `upload_urls`; files already uploaded are not rolled back.
- Network or HTTP errors fail the job. File uploads require HTTP 2xx; property updates require HTTP 204 specifically, including when another 2xx response is returned. curl is configured for 3 retries, but not every error is retried.
- Artifactory accepts repeated property updates. Rerunning the workflow uploads the same URL again and overwrites existing properties, subject to repository permissions.
- Empty or malformed publishing credentials also fail the job before any request is sent.

## Local Tests

Run from the repository root in a prepared Python 3.12 environment:

```bash
python -m pip install --no-input pytest PyYAML
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-templates/upload-artifact-properties/tests -q -p no:cacheprovider
```

The tests mock uploads and property setters, require no real publishing credentials, and do not access a real Artifactory instance. They do not replace permission, network, and build verification in an actual GitHub Actions environment.
