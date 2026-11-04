# flytiger-release-action

A Python package build and release tool for PPU environments. It uses a GitHub Actions reusable workflow to generate build matrices across Python, operating system, CUDA, PyTorch, and SDK versions, and publishes wheels, installation bootstrap source distributions, and accompanying metadata.

This repository provides a **reusable workflow**, which must be referenced via `jobs.<job_id>.uses` in the caller's workflow. It is not a regular Action used in `steps`, nor is it a directly installable Python package.

## Release Process

Workflow entry point: [`.github/workflows/matrix-release.yml`](../../.github/workflows/matrix-release.yml). Reusable workflows must live directly under `.github/workflows/` at the repository root, while helper scripts and tests stay in `ci-templates/release-pypi/`.

```text
Validate the package name, release version, and image mappings
  → Build installation bootstrap source distributions (deduplicated by release_version)
  → Build matrix → Collect wheels → Save raw wheel artifact → Repackage → Rename → Upload wheels
  → Publish source distributions after at least one matrix job succeeds
  → Update the package's index.json
  → Submit release data for the current run
```

Non-release path (`should_release=false`): skip the source distribution build and only run matrix compilation → collect wheels → save the raw wheel artifact, then finish.

- `prepare-matrix`: Normalizes the package name, applies version gating, and generates up to 32 matrix jobs.
- `build-sdist`: Generates `.tar.gz` files using this repository's `setup.py` template. These are installation bootstrap packages for matching and downloading prebuilt wheels, **not archives of the caller's complete source code**.
- `matrix-job`: Builds the caller's repository in the specified image, modifies wheel versions using `revpkg`, and uploads them to Artifactory.
- `publish-sdist`: Checks success markers for the current `run_id` / `run_attempt` and publishes all built source distribution versions via Twine only if at least one matrix job succeeds.
- `update-metadata`: Queries Artifactory for the package's wheels and generates an index.
- `post-release-data`: After the index is successfully updated, submits the saved release records one by one, generating installation commands and idempotency keys.

The matrix uses `fail-fast: false`. Failures in some jobs do not cancel other matrix jobs. Source distributions and metadata can still be published when the conditions above are met, but this does not mean the entire workflow succeeds, and already uploaded artifacts are not rolled back.

## Quick Start

### 1. Prepare the Environment and Credentials

The caller's repository requires:

- GitHub Actions enabled, with permission to use this repository's reusable workflow.
- A build command that can generate wheels in a Linux x86_64 container, along with the required build dependencies.
- A runner with access to the container registry and Artifactory at `pkg.flytiger-eco.com`.
- The required Secrets listed below. The release account must have permission to upload wheels / source distributions, run AQL queries, and read and write package indexes.

| Workflow Secret | Required | Purpose |
| --- | --- | --- |
| `release_username` | Yes | Artifactory release username |
| `release_token` | Yes | Artifactory release credential |
| `data_token` | Yes | Client RSA private key, Base64-encoded unencrypted DER format (PKCS8) |
| `template_token` | No | Read-only token for checking out a private template repository; falls back to `github.token` if omitted |

The example below maps the caller's uppercase Secrets to the corresponding workflow inputs. Never write credentials into YAML or build commands. A private template repository must also allow cross-repository calls in its Actions settings.

Release data uses `/open/sync/artifact/save`, encrypting the business data with AES-256-GCM, encrypting the AES key with RSA-OAEP-SHA256, and signing with RSA-SHA256. The workflow keeps the `data_token` Secret name whose value is the client RSA private key; the workflow maps it to the `CLIENT_PRIVATE_KEY_B64` environment variable. When running the script directly, pass the private key through the same environment variable.

Fill in the three constants `OPEN_API_BASE_URL`, `OPEN_API_CLIENT_ID`, and `SERVER_PUBLIC_KEY_B64` directly at the top of `ci-templates/release-pypi/scripts/post_data.py`; environment variables with the same names have no effect. The URL contains only the protocol, host, and optional port, and production must use HTTPS; the public key is in Base64-encoded DER format (SubjectPublicKeyInfo). These three values are currently empty, must be filled before use, and `template_ref` must point to a configured script version. Never write the private key into the script.

### 2. Add a Workflow to the Caller

Create `.github/workflows/release.yml` in the repository of the package to be published, and adjust the package name and build command for your project:

```yaml
name: Publish PPU Python Package

on:
  push:
    tags:
      - 'v*'
      - 'V*'
      - '[0-9]*'
  workflow_dispatch:
    inputs:
      tag_version:
        description: 'Optional release version; leave empty to use the version tag on the current commit'
        required: false
        type: string
        default: ''

permissions:
  contents: read

jobs:
  release:
    uses: flytiger-eco/shared-workflows/.github/workflows/matrix-release.yml@main
    with:
      package_name: demo-package
      framework: torch
      python_versions: '["312"]'
      os_versions: '["ubuntu2404"]'
      runtime_versions: '["13.0.0"]'
      torch_versions: '["2.13.0", "2.10.0"]'
      sdk_versions: '["2.2.0"]'
      template_repository: flytiger-eco/shared-workflows
      template_ref: main
      tag_version: ${{ inputs.tag_version || '' }}
      build_command: |
        python3 -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
    secrets:
      release_username: ${{ secrets.RELEASE_USERNAME }}
      release_token: ${{ secrets.RELEASE_TOKEN }}
      data_token: ${{ secrets.DATA_TOKEN }}
      # Enable as needed for a private template repository.
      # template_token: ${{ secrets.TEMPLATE_TOKEN }}
```

The two Torch versions in the example share the `ubuntu2404-py312` base image; the SDK and Torch are installed before the build according to the matrix parameters. `pip wheel` is only a generic example; install any other project dependencies yourself or use the project's actual Bash build command.

For production use, **replace both `@main` in `uses` and `template_ref` with the same full commit SHA**. The former determines the workflow version, while the latter determines the version of the tool scripts checked out. For cross-repository calls, do not use the caller's `github.sha` as the template version.

`template_repository` defaults to `flytiger-eco/shared-workflows`, matching the workflow repository in the example; when using a fork, adjust `uses` and `template_repository` together. This release workflow only provides a `workflow_call` entry point; for manual runs the caller must supply the `workflow_dispatch`.

## Inputs

All arrays are passed as **JSON strings**, not YAML arrays. All version arrays must be non-empty, and duplicate values are removed.

| Input | Required | Current Default | Description |
| --- | --- | --- | --- |
| `package_name` | Yes | None | Python package name; the entry point replaces `-` with `_`, e.g., `demo-package` → `demo_package` |
| `framework` | Yes | None | Framework module name detected by the installation template, such as `torch`; an empty string may be passed explicitly; not used in matrix expansion |
| `python_versions` | Yes | `'["310"]'` | Allowed: `"38"`, `"310"`, `"312"`; JSON integers are also accepted, e.g., `[38,310]` |
| `os_versions` | Yes | `'["ubuntu2204"]'` | Allowed: `ubuntu2004`, `ubuntu2204`, `ubuntu2404`, `alios7u2` |
| `runtime_versions` | Yes | `'["13.0.0"]'` | CUDA allowed values: `"12.6.0"`, `"12.8.0"`, `"12.9.0"`, `"13.0.0"`, `"13.2.0"`; elements must be strings |
| `torch_versions` | Yes | `'["2.13.0"]'` | Allowed values: `"2.6.0"`, `"2.7.0"`, `"2.8.0"`, `"2.9.0"`, `"2.10.0"`, `"2.11.0"`, `"2.13.0"`; `'[""]'` skips Torch installation and the Torch version in the wheel name |
| `sdk_versions` | Yes | `'["2.1.1"]'` | Allowed values: `"2.1.1"`, `"2.2.0"`, defined by `SDK_MAP` |
| `template_repository` | No | `flytiger-eco/shared-workflows` | Repository that stores the helper scripts; adjust it as well when using a fork |
| `template_ref` | Yes | None | Template repository revision; must match the revision referenced in `uses` |
| `build_command` | Yes | None | Bash command executed at the root of the caller's repository; supports multiple lines; must come from trusted configuration only |
| `tag_version` | No | Empty string | Overrides the release version without changing the code revision checked out |

The matrix still expands as the Cartesian product of the five version axes, up to 256 jobs; images are selected only by OS/Python. Every selected OS/Python combination must have a mapping; missing entries are not filtered out automatically. Whether the SDK download package and the Torch package are available is verified by the later installation steps.

### Current Image Configuration

The base image mapping is maintained in `IMAGES_MAP` in [`prepare_matrix.py`](scripts/prepare_matrix.py):

| OS | Python | Base image |
| --- | --- | --- |
| `ubuntu2404` | `312` | `pkg.flytiger-eco.com/docker_tool/ubuntu2404-py312:06` |
| `ubuntu2204` | `310` | `pkg.flytiger-eco.com/docker_tool/ubuntu2204-py310:05` |
| `ubuntu2004` | `38` | `pkg.flytiger-eco.com/docker_tool/ubuntu2004-py38:01` |
| `alios7u2` | `312` | `pkg.flytiger-eco.com/docker_tool/alios7u2-py312:03` |
| `alios7u2` | `310` | `pkg.flytiger-eco.com/docker_tool/alios7u2-py310:08` |
| `alios7u2` | `38` | `pkg.flytiger-eco.com/docker_tool/alios7u2-py38:10` |

The default OS/Python combinations already have mappings. When adding a base image, use `{os_version}-py{python_version}` as the key; different CUDA, SDK, and Torch versions share the same base image.

`SDK_MAP` maintains the correspondence between SDK versions and package-name hashes; the matrix generates `sdk_url` for both release and non-release builds in the format:

```text
https://pkg.flytiger-eco.com/artifactory/generic-local/CUDA_SDK/v{sdk_version}/PPU_SDK_cuda-{runtime_version}-{os_version}-{sdk_version}-{hash}.tar.gz
```

The `BUILD_CONTAINER_IMAGES` missing-entry error points to the image mapping above; the script does not load its configuration from an environment variable of the same name. `torch_versions: '[""]'` uses the same base image but skips Torch installation.

## Versioning and Artifact Naming

The release version is selected in the following order of precedence:

1. A non-empty `tag_version`, which must conform to the tag format defined by the script and PEP 440; invalid values cause an error with no fallback.
2. `github.ref_name` for tag-triggered runs.
3. For other triggers, only tags on the caller's current commit are checked. The first correctly formatted version without `rc` is selected in descending Git version order; historical commits are not searched.

Every version source strips one leading `v` / `V` while keeping inner content such as `+v0.3.0`. When the specified version or the triggering tag contains `rc` (case-insensitive), or the current commit has no qualifying tag, `should_release=false`: build parameters and image mappings are still validated, and the matrix is generated to compile, collect wheels, and save Actions artifacts; no source distribution is built, and Repack, renaming, and all subsequent publishing tasks are skipped. In this case `RELEASE_VERSION` and `WHL_NAME` are empty, and the wheel version and file name are determined by the caller's build command.

`tag_version` only determines the release version, artifact paths, and release data; it does not check out source code from the tag with the same name. For manual runs, the actual build uses the triggering revision of the selected branch / ref.

Using SDK `2.2.0` as an example:

| Input Version | Normalized Tag | `release_version` |
| --- | --- | --- |
| `v1.0.0` | `1.0.0` | `1.0.0+ppu2.2.0` |
| `V1.0.0+v0.3.0` | `1.0.0+v0.3.0` | `1.0.0+v0.3.0.ppu2.2.0` |

Source distribution versions and repackaged wheel versions use `release_version`. The final wheel filename uses the part of the tag before `+` as the base version and appends environment information, for example:

```text
demo_package-1.0.0+cuda13.0.0torch2.13.0ubuntu2404oe-cp312-cp312-linux_x86_64.whl
```

The final wheel renaming step does not modify internal metadata again. The SDK version and the tag's original local version suffix are reflected in the release directory, rather than being written directly into the wheel filename above.

## Build Conventions and Publishing Locations

Each matrix job first downloads the CUDA SDK `.tar.gz` package, extracts it into a temporary directory with `tar -xzf`, verifies `PPU_SDK/envsetup.sh` inside the package, and then moves `PPU_SDK` to `/usr/local/` with `mv`; it aborts if the target directory already exists. Afterwards `${PPU_SDK}/lib` and `${CUDA_SDK}/lib64` are appended to `/etc/ld.so.conf.d/ppu.conf` and `ldconfig` is run. When these two environment variables are unset or empty, `/usr/local/PPU_SDK` and `/usr/local/PPU_SDK/CUDA_SDK` are used respectively.

The build step refreshes the dynamic library cache again, sources `/usr/local/PPU_SDK/envsetup.sh`, installs the specified Torch version through this repository's PyPI index, and finally runs `build_command`. Installation is skipped when the Torch version is empty.

Base images must come with a preinstalled compiler toolchain, the matching Python/pip, plus Bash, wget, tar, gzip, mv, cp, and ldconfig; the installation process needs write permission for `/usr/local` and `/etc/ld.so.conf.d`. Failures in SDK download, extraction, layout verification, dynamic library initialization, or Torch installation abort the build; release and non-release builds follow the same rules.

The build command can read:

```text
PYTHON_VERSION  OS_VERSION  RUNTIME_VERSION  TORCH_VERSION  SDK_VERSION
RELEASE_VERSION  WHL_NAME  CONTAINER_IMAGE
```

`SDK_URL` comes from the SDK download address generated by the matrix and is used for the pre-build installation. Torch is installed directly from PyPI by `TORCH_VERSION`; `TORCH_URL` is no longer used.

The caller's code is checked out with its full Git history and recursive submodules. After the build, the workflow recursively scans the source workspace for regular `.whl` files. If multiple files exist, they are filtered by package name prefix, and exactly one must remain. The selected artifact is collected into `dist/`, which retains only the selected wheel. Avoid leaving multiple artifacts for the same package in the build directory.

These addresses are currently hardcoded in the workflow or scripts and are not configurable inputs. Different matrix combinations must use non-conflicting upload paths.

After each matrix job collects a wheel successfully, the original package is saved to GitHub Actions artifacts named `{package_name}-collected-wheels-{run_id}-{run_attempt}-{job-index}`, isolated by package name, run, retry, and matrix index. The save happens before Repack and renaming, preserving the original file name and content; only `dist/*.whl` is uploaded, and nothing is uploaded when the build or collection fails.

The release flow also saves the source distributions, release data JSON, and success markers. Only when `should_release=true` and the run is triggered by a tag or an explicit `tag_version`, the repackaged `release-wheels` artifact is saved additionally. Non-release builds only save the raw wheel artifact described above.

**Currently, no GitHub Release is created, and no assets are uploaded to a Release.** The workflow step named "Upload GitHub Release wheel artifacts" actually uses `actions/upload-artifact`.

## Installation Bootstrap Package Behavior

[`setup.py`](ci-templates/release-pypi/scripts/setup.py) is a source distribution template and should not be run directly as this repository's installation entry point. The workflow fills in the package name, framework, and version, then sets `__FAKE_BUILD=FAKE_WHEEL` to generate the bootstrap package. No wheels are downloaded at this stage.

When a user installs the bootstrap package, the template detects the Python, OS, Runtime, and framework versions. By default, it downloads and executes a remote `strategy.py` to match a prebuilt wheel, then extracts its dependency information. This process requires a trusted remote strategy service, an accessible artifact repository, and `wget`; it does not compile the caller's source code on the user's machine.

Related environment variables:

| Variable | Description |
| --- | --- |
| `CUDA_SDK_VER` | Explicitly specifies the Runtime, e.g., `cuda-13.0.0`; detected via `nvcc --version` if unset or incorrectly formatted |
| `SAIL_SDK_MATCH_STRATEGY` | When set to `off`, uses local naming rules to construct the wheel URL directly |
| `SAIL_PYPI_SDK_VERSION` | SDK version input provided by the template; `VESION` is the existing spelling in the variable name, and whether it is used depends on the matching strategy |

## Repository Structure

```text
.github/workflows/
└── matrix-release.yml          # Reusable release workflow

ci-templates/release-pypi/
├── scripts/
│   ├── prepare_matrix.py       # Version gating, input validation, image mappings, and artifact naming
│   ├── setup.py                # Installation bootstrap source distribution template
│   ├── get_metadata.py         # Artifactory queries, SDK configuration merging, and index uploads
│   └── post_data.py            # Release data transformation, idempotency key generation, and API submission
└── tests/
    ├── test_prepare_matrix.py  # Matrix, version, and input validation
    ├── test_build_sdist.py     # Source distribution generation and installation template
    ├── test_upload_wheels.py   # Wheel collection, repackaging, renaming, and uploading
    ├── test_publish_sdist.py   # Conditional publishing, source distribution validation, and index updates
    └── test_github_release.py  # Artifact storage, release data, and template dependency resolution
```

## Local Testing

Use **Python 3.12**, matching the workflow's tooling jobs. The current `post_data.py` uses f-string syntax supported by Python 3.12, so Python 3.11 cannot run the full test suite. The tests also require Git and Bash.

Run the following from the repository root (if `.venv` already exists, check its Python version first):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --no-input pytest PyYAML packaging setuptools wheel requests cryptography
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-templates/release-pypi/tests -q -p no:cacheprovider
```

These tests use mocks / shell shims to simulate the SDK, `revpkg`, `curl`, Twine, and HTTP requests. They do not require real release credentials, PPU hardware, or build images, and they do not upload artifacts to release services. They do not replace build and release validation in actual images.
