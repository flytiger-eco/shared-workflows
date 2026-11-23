# Temporary Wheel Publishing Template

Download and extract the specified artifact from the current GitHub Actions run, recursively find all files ending in `.whl`, and upload them unchanged to:

```text
https://pkg.flytiger-eco.com/artifactory/pypi_tmp/{timestamp}/{filename}
```

The timestamp is generated once before the first upload, using Asia/Shanghai (UTC+08:00) in `YYYY-MM-DD-HHmm` format, for example `2026-11-02-1211`. Every file in the batch uses that same directory, even if uploading spans multiple minutes. Original filenames are preserved; artifact subdirectories are not included in the destination path.

- Workflow entry point: [`.github/workflows/push-tmp-wheel.yml`](../../.github/workflows/push-tmp-wheel.yml)
- Upload script: [`scripts/push_tmp_wheel.py`](scripts/push_tmp_wheel.py)
- Complete usage example: [`workflows/demo-push-tmp-wheel.yml`](workflows/demo-push-tmp-wheel.yml)

This is a reusable workflow called through `jobs.<job_id>.uses`, not a regular Action used in `steps`. The template only exposes `workflow_call`. It does not build, validate, install, repackage, or rename wheels, or create GitHub Releases, source distributions, or package indexes.

## Execution Flow

```text
Caller builds wheels
  → actions/upload-artifact@v4 saves the artifact
  → Call the upload template with needs on the build job
  → actions/download-artifact@v4 downloads and extracts the named artifact
  → Recursively find all regular files ending in .whl
  → Generate one Asia/Shanghai (UTC+08:00) timestamp for the entire batch
  → Upload each selected file unchanged, then set its Artifactory properties
  → Return the batch directory as upload_url after all uploads and property updates succeed
```

The reusable workflow runs in a separate job and does not share local files with the caller's previous job. Build outputs must be transferred through artifacts. The template currently does not support artifacts from other workflow runs and does not search the caller repository for wheels. Artifact extraction is handled by `actions/download-artifact@v4`; nested ZIP or tar files inside the artifact are not unpacked.

## Quick Start

1. Copy [`workflows/demo-push-tmp-wheel.yml`](workflows/demo-push-tmp-wheel.yml) to `.github/workflows/demo-push-tmp-wheel.yml` in the project to publish.
2. Configure `RELEASE_USERNAME` and `RELEASE_TOKEN` in the caller repository or organization Actions Secrets. The publishing account needs upload and item-property update permissions for the destination temporary repository.
3. Ensure the caller can use the template repository's workflows. Private template repositories must also allow cross-repository calls in Actions settings; configure `TEMPLATE_TOKEN` and uncomment the corresponding example line as needed.
4. Adjust the example build environment and command for your project. The example uses Python 3.12 and `python -m build --wheel`, so the selected source repository must be a buildable Python project. If CUDA, PPU SDK, or other system dependencies are required, configure the build runner/container and dependencies, and write build outputs to the directory specified in the example.
5. After committing the example to the caller default branch, trigger it manually through **Run workflow** on the Actions page. Select a trusted workflow branch and optionally enter `source_repository` (the actual build repository) and `source_ref` (its branch, tag, or SHA). Omitted values use the caller repository and selected workflow ref; set both explicitly when building another repository. The example records source metadata after checkout, passes it through build job outputs, and uploads all `.whl` files; no package name or wheel filename is required.
6. After a successful run, find the batch directory URL in the `result` job logs or job summary. Individual file URLs are printed in the upload step logs.

The example actually uploads to the temporary repository; it is not a dry run. Files under this directory's `workflows/` are provided for copying and are not automatically registered as Actions workflows in this repository.

### Template Version and Repository

The example uses the `main` branch of `flytiger-eco/shared-workflows`. Ensure the referenced revision contains this workflow and the helper script before running it.

- For production, replace both `@main` in `uses` and `template_ref: main` with the same full commit SHA from the template repository to prevent workflow and script version drift.
- Do not use the caller's `github.sha` as `template_ref` for cross-repository calls.
- When using a fork, update both the repository path in `uses` and `template_repository`.
- For same-repository relative-path calls, use `uses: ./.github/workflows/push-tmp-wheel.yml` with `template_repository: ${{ github.repository }}` and `template_ref: ${{ github.sha }}`.

The caller must grant at least `contents: read`. The template upload job uses `ubuntu-latest` and Python 3.12 with a 30-minute timeout. Its runner must be able to download Actions artifacts and access `pkg.flytiger-eco.com` over HTTPS. The upload script uses only the Python standard library and curl; no wheel validation dependencies are installed. Changing the example build runner does not change the template upload runner.

## Inputs

All inputs have type `string`.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `artifact_name` | Yes | None | Name of the artifact saved by the caller using `actions/upload-artifact@v4`; must not be empty and must belong to the current workflow run. |
| `repo` | Yes | None | Actual source repository used for the build; non-empty values must use `owner/repo` format. Must be passed explicitly; empty skips the property. |
| `commit` | Yes | None | Actual build source commit from `git rev-parse HEAD`; non-empty values must be full 40-character hexadecimal SHAs. Must be passed explicitly; empty skips the property. |
| `branch` | Yes | None | Actual source branch; must be passed explicitly. Empty skips the property when unknown or not applicable, such as detached HEAD. |
| `tag` | Yes | None | Tag pointing to the build commit; must be passed explicitly. Empty skips the property when none. Multiple tags may be passed as one comma-separated string. |
| `extra_info` | No | `''` | Retained for caller compatibility; accepted but currently ignored and not written as an Artifactory property. |
| `template_repository` | No | `flytiger-eco/shared-workflows` | Repository containing the template script, in `owner/repo` format. |
| `template_ref` | Yes | None | Template script version; must match the workflow version referenced by `uses`. |

`repo`, `commit`, `branch`, and `tag` are required inputs with no defaults. Callers must explicitly pass all four fields; callers omitting any of them are no longer supported. All four fields may be explicitly passed as empty strings; empty values are omitted from the property request rather than sent as `key=`. Non-empty `repo` and `commit` values are validated before uploading. Values are not trimmed; whitespace-only `repo` and `commit` remain invalid. `extra_info` remains optional with an empty-string default and is ignored. Source metadata never falls back to GitHub context. Do not substitute the triggering workflow's repository or SHA when the actual build source is different. Source metadata must describe the same build as the uploaded artifact, including when rerunning only the publishing job. The previously removed `package_name` and `wheel_name` inputs remain unsupported.

For example, an artifact containing:

```text
artifact_name: tmp-wheel-123456-1
artifact contents:
  dist/demo_package-1.2.3.dev1-py3-none-any.whl
  nested/other_package-2.0-py3-none-any.whl
  notes.txt
```

uploads both `.whl` files to the same directory and ignores `notes.txt`:

```text
https://pkg.flytiger-eco.com/artifactory/pypi_tmp/2026-11-02-1211/demo_package-1.2.3.dev1-py3-none-any.whl
https://pkg.flytiger-eco.com/artifactory/pypi_tmp/2026-11-02-1211/other_package-2.0-py3-none-any.whl
```

The calling job must use `needs` to wait for the job that saves the artifact. The example combines `run_id` and `run_attempt` in the artifact name, then passes the actual name through the build job output to avoid using a new `run_attempt` to find an old artifact when rerunning only the publishing job.

## Secrets

| Template Secret | Required | Caller Secret in the Example | Description |
| --- | --- | --- | --- |
| `release_username` | Yes | `RELEASE_USERNAME` | Artifactory publishing username. |
| `release_token` | Yes | `RELEASE_TOKEN` | Artifactory publishing credential with upload and item-property update permissions for the destination path. |
| `template_token` | No | `TEMPLATE_TOKEN` | Read-only token for checking out a private template repository; defaults to `github.token`, which may lack read access across repositories. |

The example explicitly maps credentials through `jobs.publish.secrets` and passes publishing credentials only to the publishing job. Credential values are not written into YAML, build arguments, or output logs.

## Outputs

| Output | Description |
| --- | --- |
| `upload_url` | Batch directory URL with a trailing slash, emitted only after all files upload and all property updates succeed, available through `${{ needs.publish.outputs.upload_url }}`; `publish` is the ID of the job calling the template. |

For example, `upload_url` is `https://pkg.flytiger-eco.com/artifactory/pypi_tmp/2026-11-02-1211/`. It is now a directory URL, not a single wheel URL. Individual file URLs are logged after each successful upload.

Filenames are percent-encoded in URLs; for example, `+` becomes `%2B`. No package or version directory is added, and uploaded file contents remain unchanged.

## Artifactory Properties

Every uploaded wheel receives the same property set for the batch, with up to seven properties. `repo`, `commit`, `branch`, and `tag` come only from required caller inputs. Any explicitly empty source value is omitted entirely; the request never includes empty entries such as `repo=`, `commit=`, `branch=`, or `tag=`. Writing the `extra_info` property is currently suspended; the input remains accepted for compatibility, but its value is ignored and no `extra_info` property is sent, even with an empty value. Existing server properties are not deleted. The following three properties come directly from GitHub context and are not inputs:

| Property | Source | Description |
| --- | --- | --- |
| `log_url` | `${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}` | Current caller workflow run page, not the source or template repository. |
| `run_id` | `${{ github.run_id }}` | ID of the current publishing run. |
| `run_attempt` | `${{ github.run_attempt }}` | Current attempt, including when rerunning only the publishing job. |

`run_id` and `run_attempt` must be positive integers. These three fields cannot be overridden by caller inputs; the source metadata still describes the original build.

For compatibility with older workflow wrappers, when `GITHUB_LOG_URL` is missing, empty, or whitespace-only, the script builds `log_url` from the runner's native `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY`, and `GITHUB_RUN_ID` variables. A non-empty `GITHUB_LOG_URL` takes precedence. The native server URL is preserved, including on GitHub Enterprise, and trailing slashes are removed before joining the path. `run_id` and `run_attempt` already use the native `GITHUB_RUN_ID` and `GITHUB_RUN_ATTEMPT` variables and do not need an explicit step-level `env` declaration. The compatibility logic never infers any source metadata from the triggering context and does not bypass required source input validation. Missing both the explicit link and native context remains an error before uploading. Keep workflow and script refs aligned as described above; this fallback only handles the missing log-link variable.

For each wheel, an empty-body PUT sets the properties using:

```text
https://pkg.flytiger-eco.com/artifactory/api/storage/pypi_tmp/{timestamp}/{filename}?properties=repo=...;commit=...;branch=...;tag=...;log_url=...;run_id=...;run_attempt=...&recursive=0
```

The property request must return `204 No Content`. Empty source values are skipped before joining the property pairs, so no empty pairs or extra separators are added. Values are escaped for Artifactory (backslashes, commas, pipes, and equals signs) and percent-encoded. `extra_info` is not included in the request. For example, with empty `branch` and `tag`, the query is `properties=repo=owner%2Frepo;commit={sha};log_url={encoded_url};run_id={id};run_attempt={attempt}&recursive=0`. If all four source inputs are empty, only `log_url`, `run_id`, and `run_attempt` are sent. Skipping a property does not delete any existing server value.

Example caller inputs, with source outputs recorded after checkout in the build job:

```yaml
with:
  artifact_name: ${{ needs.build.outputs.artifact_name }}
  repo: ${{ needs.build.outputs.repo }}
  commit: ${{ needs.build.outputs.commit }}
  branch: ${{ needs.build.outputs.branch }}
  tag: ${{ needs.build.outputs.tag }}
  extra_info: '{"runtime":"cuda","version":"13.0","flags":["a","b"]}'
  template_repository: flytiger-eco/shared-workflows
  template_ref: main
```

The complete demo checks out the requested `source_repository` and `source_ref`, records the actual HEAD SHA, reads the symbolic branch when attached, and joins all tags pointing at HEAD into one comma-separated string. When adapting it to a checkout with `path`, run the metadata commands and build in that source directory. Detached HEAD does not identify a unique source branch; pass the known build branch explicitly or leave it empty. Cross-repository private sources need a suitable read-only credential on the source checkout; the default `github.token` usually cannot read them.

## File Selection

No wheel validation is performed. Every regular file whose name ends with the case-sensitive suffix `.whl` is uploaded, including files in nested or hidden directories. Empty files, arbitrary wheel names, malformed ZIP contents, and invalid or missing metadata are not rejected. The script does not open, extract, install, or execute wheels; the caller is responsible for their contents.

Other suffixes, directories, and symlinks are skipped. Directory symlinks are not followed, so files outside the downloaded artifact are not uploaded. The job fails if the artifact directory is missing or no matching regular files are found.

## Upload and Failure Behavior

- Uploads use HTTPS PUT with the username and token sent through Basic Auth. Redirects are not followed to avoid sending files and publishing credentials to other destinations.
- Network or HTTP errors fail the job. File uploads require HTTP 2xx; property updates require HTTP 204 specifically. curl is configured for 3 retries, but not every error is retried.
- Files are uploaded sequentially in sorted local-path order, with properties set immediately after each upload. On the first upload or property-update failure, the job stops and emits no successful `upload_url`; files already uploaded are not rolled back. Empty or malformed publishing credentials also fail the job. Existing publishing credentials must allow item-property updates as well as uploads.
- The template does not check for duplicate filenames or existing server files. Files with the same basename in different artifact subdirectories target the same URL and are all uploaded; later uploads may overwrite earlier ones depending on Artifactory permissions. Use distinct filenames to preserve every file.
- Separate batches whose timestamps are captured in the same Asia/Shanghai (UTC+08:00) minute share the same destination directory. Reruns capture the current timestamp again but may still use the same directory. Minute-level timestamps do not guarantee unique directories or overwrite prevention.

## Local Tests

Run from the repository root in a prepared Python 3.12 environment:

```bash
python -m pip install --no-input pytest PyYAML
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-templates/push-tmp-wheel/tests -q -p no:cacheprovider
```

The tests mock uploads, require no real publishing credentials, and do not access a real Artifactory instance. They do not replace permission, network, and build verification in an actual GitHub Actions environment.
