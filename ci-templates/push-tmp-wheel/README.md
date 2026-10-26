# Temporary Wheel Publishing Template

Find `wheel_name` in the specified artifact from the current GitHub Actions run, validate it, and upload the wheel unchanged to:

```text
https://pkg.flytiger-eco.com/artifactory/pypi_tmp_generic/{package_name}/{wheel_name}
```

- Workflow entry point: [`.github/workflows/push-tmp-wheel.yml`](../../.github/workflows/push-tmp-wheel.yml)
- Validation and upload script: [`scripts/push_tmp_wheel.py`](scripts/push_tmp_wheel.py)
- Complete usage example: [`workflows/demo-push-tmp-wheel.yml`](workflows/demo-push-tmp-wheel.yml)

This is a reusable workflow called through `jobs.<job_id>.uses`, not a regular Action used in `steps`. The template only exposes `workflow_call`. It does not build, install, repackage, or rename wheels, or create GitHub Releases, source distributions, or package indexes.

## Execution Flow

```text
Caller builds the wheel
  → actions/upload-artifact@v4 saves the artifact
  → Call the upload template with needs on the build job
  → Download the named artifact from the current run
  → Recursively find the unique file matching wheel_name
  → Validate wheel structure, metadata, and integrity
  → Upload to the temporary repository
  → Return upload_url
```

The reusable workflow runs in a separate job and does not share local files with the caller's previous job. Build outputs must be transferred through artifacts. The template currently does not support artifacts from other workflow runs and does not search the caller repository for wheels.

## Quick Start

1. Copy [`workflows/demo-push-tmp-wheel.yml`](workflows/demo-push-tmp-wheel.yml) to `.github/workflows/demo-push-tmp-wheel.yml` in the project to publish.
2. Configure `RELEASE_USERNAME` and `RELEASE_TOKEN` in the caller repository or organization Actions Secrets. The publishing account needs upload permission for the destination temporary repository.
3. Ensure the caller can use the template repository's workflows. Private template repositories must also allow cross-repository calls in Actions settings; configure `TEMPLATE_TOKEN` and uncomment the corresponding example line as needed.
4. Adjust the example build environment and command for your project. The example uses Python 3.12 and `python -m build --wheel`, so the caller repository must be a buildable Python project. If CUDA, PPU SDK, or other system dependencies are required, configure the build runner/container and dependencies, and write build outputs to the directory specified in the example.
5. After committing the example to the caller default branch, trigger it manually through **Run workflow** on the Actions page. Select a trusted branch to build and enter the actual package name and wheel filename. For example:

   ```text
   package_name: demo_package
   wheel_name: demo_package-1.2.3.dev1-py3-none-any.whl
   ```

   These inputs only select and validate the artifact; they do not set the project build version or change filenames. Enter values matching the actual build output, including the actual Python/ABI/platform tags for platform-specific packages.
6. After a successful run, find the upload URL in the `result` job logs or job summary.

The example actually uploads to the temporary repository; it is not a dry run. Files under this directory's `workflows/` are provided for copying and are not automatically registered as Actions workflows in this repository.

### Template Version and Repository

The example uses the `main` branch of `flytiger-eco/shared-workflows`. Ensure the referenced revision contains this workflow and the helper script before running it.

- For production, replace both `@main` in `uses` and `template_ref: main` with the same full commit SHA from the template repository to prevent workflow and script version drift.
- Do not use the caller's `github.sha` as `template_ref` for cross-repository calls.
- When using a fork, update both the repository path in `uses` and `template_repository`.
- For same-repository relative-path calls, use `uses: ./.github/workflows/push-tmp-wheel.yml` with `template_repository: ${{ github.repository }}` and `template_ref: ${{ github.sha }}`.

The caller must grant at least `contents: read`. The template upload job uses `ubuntu-latest` and Python 3.12 with a 30-minute timeout. Its runner must be able to download Actions artifacts, install `packaging` and `wheel`, and access `pkg.flytiger-eco.com` over HTTPS. Changing the example build runner does not change the template upload runner.

## Inputs

All inputs have type `string`.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `package_name` | Yes | None | Valid Python package name, also used as the temporary repository directory name. Comparison with the wheel package name is case-insensitive and normalizes `-`, `_`, and `.`; the upload directory preserves the supplied value without renaming it. |
| `wheel_name` | Yes | None | Full `.whl` filename without directories or wildcards. A recursive exact search in the specified artifact must find exactly one match. |
| `artifact_name` | Yes | None | Name of the artifact saved by the caller using `actions/upload-artifact@v4`; must not be empty and must belong to the current workflow run. |
| `template_repository` | No | `flytiger-eco/shared-workflows` | Repository containing the template script, in `owner/repo` format. |
| `template_ref` | Yes | None | Template script version; must match the workflow version referenced by `uses`. |

`artifact_name` and `wheel_name` are different names. For example:

```text
artifact_name: tmp-wheel-123456-1
artifact contents:
  dist/demo_package-1.2.3.dev1-py3-none-any.whl

wheel_name: demo_package-1.2.3.dev1-py3-none-any.whl
```

An artifact may contain other files or wheels with different names; only the file specified by `wheel_name` is uploaded. Multiple wheels with the same name in different subdirectories cause an error rather than automatic selection. The calling job must use `needs` to wait for the job that saves the artifact. The example combines `run_id` and `run_attempt` in the artifact name, then passes the actual name through the build job output to avoid using a new `run_attempt` to find an old artifact when rerunning only the publishing job.

## Secrets

| Template Secret | Required | Caller Secret in the Example | Description |
| --- | --- | --- | --- |
| `release_username` | Yes | `RELEASE_USERNAME` | Artifactory publishing username. |
| `release_token` | Yes | `RELEASE_TOKEN` | Artifactory publishing credential with upload permission for the destination path. |
| `template_token` | No | `TEMPLATE_TOKEN` | Read-only token for checking out a private template repository; defaults to `github.token`, which may lack read access across repositories. |

The example explicitly maps credentials through `jobs.publish.secrets` and passes publishing credentials only to the publishing job. Credential values are not written into YAML, build arguments, or output logs.

## Outputs

| Output | Description |
| --- | --- |
| `upload_url` | Full URL of the successfully uploaded wheel, available to downstream jobs through `${{ needs.publish.outputs.upload_url }}`; `publish` is the ID of the job calling the template. |

URL path segments are percent-encoded; for example, `+` in a version becomes `%2B` in the URL. No version directory is added, and uploaded file contents remain unchanged.

## Wheel Validation Rules

All wheel validation completes before any upload request is sent to Artifactory. Any validation failure fails the job without uploading the file.

1. **File selection**: The artifact directory exists, the filename is valid, and the package name matches `package_name`. There must be exactly one match, which must be a non-empty regular file, not a symlink.
2. **ZIP structure**: The file can be read as a wheel ZIP. Duplicate paths, path traversal, absolute paths, symlinks inside the archive, and encrypted files are not allowed.
3. **Metadata directory**: Exactly one `.dist-info` directory matches the filename and contains `METADATA`, `WHEEL`, and `RECORD`.
4. **Package metadata**: The package name and version in `METADATA` match the filename, required fields are non-empty and unique, and `Metadata-Version` has a valid format.
5. **Wheel metadata**: `Wheel-Version: 1.x` is supported, with a warning for versions above 1.0. `Root-Is-Purelib` is `true` or `false`, and `Tag` values match the Python/ABI/platform tags in the filename.
6. **Integrity**: The `RECORD` file list, sizes, and file hashes match, and ZIP CRC checks pass. `RECORD` itself and optional signature files are handled according to the wheel specification; signature provenance is not verified.

These checks validate packaging format and integrity without running package code. They do not verify runtime dependencies, hardware availability, or functional correctness, and do not require the package Python/platform tags to match the upload runner.

### Development Versions

PEP 440-compliant development versions are supported; a stable release, Git tag, or specific branch is not required. For example:

| Version | Valid |
| --- | --- |
| `1.2.3.dev0` | Yes |
| `1.2.3.dev12` | Yes |
| `1.2.3.dev12+gabc123` | Yes |
| `1.2.3.dev-feature` | No |

The version must be correctly written into the wheel during the build, and the filename, `.dist-info`, and `METADATA` must agree. Renaming the file alone to add `dev` or change the version may cause validation to fail.

## Upload and Failure Behavior

- Uploads use HTTPS PUT with the username and token sent through Basic Auth. Redirects are not followed to avoid sending files and publishing credentials to other destinations.
- Network or HTTP errors fail the job; only HTTP 2xx responses are accepted. curl is configured for 3 retries, but not every error is retried.
- The template does not check whether a file with the same name already exists on the server or guarantee overwrite prevention. The same `package_name` and `wheel_name` point to the same URL; reupload behavior depends on Artifactory overwrite permissions. Generate distinct development versions during the build if outputs from different builds must be stored separately.
- Missing or duplicate matching files, metadata mismatches, hash mismatches, empty credentials, upload failures, and similar errors do not return a successful `upload_url`.

## Local Tests

Run from the repository root in a prepared Python 3.12 environment:

```bash
python -m pip install --no-input pytest PyYAML packaging wheel
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-templates/push-tmp-wheel/tests -q -p no:cacheprovider
```

The tests mock uploads, require no real publishing credentials, and do not access a real Artifactory instance. They do not replace permission, network, and build verification in an actual GitHub Actions environment.
