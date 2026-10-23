# flytiger-release-action

A Python package build and release tool for PPU environments. It uses a GitHub Actions reusable workflow to generate build matrices across Python, operating system, CUDA, PyTorch, and SDK versions, and publishes wheels, installation bootstrap source distributions, and accompanying metadata.

This repository provides a **reusable workflow**, which must be referenced via `jobs.<job_id>.uses` in the caller's workflow. It is not a regular Action used in `steps`, nor is it a directly installable Python package.

## Release Process

工作流入口： [`.github/workflows/matrix-release.yml`](../../.github/workflows/matrix-release.yml)。可复用工作流必须直接放在仓库根目录的 `.github/workflows/` 下，辅助脚本和测试保留在 `ci-templates/release-pypi/`。

```text
Validate the package name, release version, and image mappings
  → Build installation bootstrap source distributions (deduplicated by release_version)
  → Build matrix → Collect wheels → 保存原始 wheel artifact → Repackage → Rename → Upload wheels
  → Publish source distributions after at least one matrix job succeeds
  → Update the package's index.json
  → Submit release data for the current run
```

非发布路径（`should_release=false`）：跳过源码包构建，仅执行矩阵编译 → 收集 wheel → 保存原始 wheel artifact，然后结束。

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
| `data_token` | Yes | 客户端 RSA 私钥，Base64 编码的未加密 DER 格式（PKCS8）|
| `template_token` | No | Read-only token for checking out a private template repository; falls back to `github.token` if omitted |

下方示例将调用方的大写 Secrets 映射到对应的工作流参数。不要将凭据写入 YAML 或构建命令。私有模板仓库还需在 Actions 设置中允许跨仓库调用。

发布数据使用 `/open/sync/artifact/save`，通过 AES-256-GCM 加密业务数据、RSA-OAEP-SHA256 加密 AES 密钥，并使用 RSA-SHA256 签名。工作流保留 `data_token` Secret 名称，其值为客户端 RSA 私钥；工作流将其映射到 `CLIENT_PRIVATE_KEY_B64` 环境变量。直接运行脚本时也通过该环境变量传入私钥。

在 `ci-templates/release-pypi/scripts/post_data.py` 顶部直接填写 `OPEN_API_BASE_URL`、`OPEN_API_CLIENT_ID` 和 `SERVER_PUBLIC_KEY_B64` 三个常量；同名环境变量不生效。地址仅含协议、主机及可选端口，生产环境使用 HTTPS；公钥为 Base64 编码的 DER 格式（SubjectPublicKeyInfo）。这三个值当前为空，使用前必须填写，并通过 `template_ref` 引用已配置的脚本版本。私钥不得写入脚本。

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

This example uses the two image configurations currently available. `pip wheel` is only a generic example; ensure that the project's build dependencies are installed, or replace it with your project's own Bash build command.

For production use, **replace both `@main` in `uses` and `template_ref` with the same full commit SHA**. The former determines the workflow version, while the latter determines the version of the tool scripts checked out. For cross-repository calls, do not use the caller's `github.sha` as the template version.

`template_repository` 默认为 `flytiger-eco/shared-workflows`，与示例中的工作流仓库一致；使用 fork 时需要同时调整 `uses` 和 `template_repository`。该发布工作流仅提供 `workflow_call` 入口，手动运行时由调用方提供 `workflow_dispatch`。

## Inputs

All arrays are passed as **JSON strings**, not YAML arrays. All version arrays must be non-empty, and duplicate values are removed.

| Input | Required | Current Default | Description |
| --- | --- | --- | --- |
| `package_name` | Yes | None | Python package name; the entry point replaces `-` with `_`, e.g., `demo-package` → `demo_package` |
| `framework` | Yes | None | Framework module name detected by the installation template, such as `torch`; an empty string may be passed explicitly; not used in matrix expansion |
| `python_versions` | Yes | `'["310"]'` | Allowed: `"38"`, `"310"`, `"312"`; JSON integers are also accepted, e.g., `[38,310]` |
| `os_versions` | Yes | `'["ubuntu2204"]'` | Allowed: `ubuntu2004`, `ubuntu2204`, `ubuntu2404`, `alios7u2` |
| `runtime_versions` | Yes | `'["13.0.0"]'` | Allowed: `"13.0.0"`, `"13.2.0"`; elements must be strings |
| `torch_versions` | Yes | `'["2.8.0"]'` | Allowed: `"2.8.0"`, `"2.10.0"`, `"2.13.0"`; `'[""]'` means no Torch version is included |
| `sdk_versions` | Yes | `'["2.1.0"]'` | Allowed: `"2.1.0"`, `"2.2.0"` |
| `template_repository` | No | `flytiger-eco/shared-workflows` | 保存辅助脚本的仓库；使用 fork 时同步调整 |
| `template_ref` | Yes | None | Template repository revision; must match the revision referenced in `uses` |
| `build_command` | Yes | None | Bash command executed at the root of the caller's repository; supports multiple lines; must come from trusted configuration only |
| `tag_version` | No | Empty string | Overrides the release version without changing the code revision checked out |

A valid input value does not guarantee that a corresponding image exists. The matrix expands as the Cartesian product of the five version axes, and **every combination must have an image mapping**. Combinations without mappings are not automatically filtered out.

### Current Image Configuration

Image mappings are maintained in `IMAGES_MAP` in [`prepare_matrix.py`](ci-templates/release-pypi/scripts/prepare_matrix.py). Currently, only the following are available:

| OS | Python | Runtime | SDK | Torch |
| --- | --- | --- | --- | --- |
| `ubuntu2404` | `312` | `13.0.0` | `2.2.0` | `2.13.0` |
| `ubuntu2404` | `312` | `13.0.0` | `2.2.0` | `2.10.0` |

The corresponding images are, respectively:

```text
pkg.flytiger-eco.com/docker_release/pytorch:sdk2.2.0-2.13.0-ubuntu24.04-cuda13.0-py312-20261001
pkg.flytiger-eco.com/docker_release/pytorch:sdk2.2.0-2.10.0-ubuntu24.04-cuda13.0-py312-20261001
```

The default version combination in the inputs currently has no corresponding mapping. Explicitly use the versions in the Quick Start example. To add a combination, update `IMAGES_MAP` using the following key format:

```text
{os_version}-py{python_version}-sdk{sdk_version}-cuda{runtime_version}-torch{torch_version}
```

Errors about missing entries in `BUILD_CONTAINER_IMAGES` refer to this mapping table; the current implementation does not load image configuration from an environment variable of the same name. Although `torch_versions: '[""]'` is a valid input, no corresponding image is currently available.

## Versioning and Artifact Naming

The release version is selected in the following order of precedence:

1. A non-empty `tag_version`, which must conform to the tag format defined by the script and PEP 440; invalid values cause an error with no fallback.
2. `github.ref_name` for tag-triggered runs.
3. For other triggers, only tags on the caller's current commit are checked. The first correctly formatted version without `rc` is selected in descending Git version order; historical commits are not searched.

所有版本来源均去除开头的一个 `v` / `V`，保留 `+v0.3.0` 等内部内容。指定版本或触发 tag 含 `rc`（忽略大小写），或当前提交没有合格 tag 时，`should_release=false`：仍校验构建参数和镜像映射，生成矩阵并编译、收集 wheel、保存 Actions artifact；不构建源码包，不执行 Repack、重命名及任何后续发布任务。此时 `RELEASE_VERSION` 和 `WHL_NAME` 为空，wheel 版本与文件名由调用方的构建命令决定。

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

The build step first sources `/usr/local/PPU_SDK/envsetup.sh`, then executes `build_command`. The build image must have a working SDK, build toolchain, and the corresponding Python / Torch environment preinstalled.

The build command can read:

```text
PYTHON_VERSION  OS_VERSION  RUNTIME_VERSION  TORCH_VERSION  SDK_VERSION
RELEASE_VERSION  WHL_NAME  CONTAINER_IMAGE
```

`TORCH_URL` and `SDK_URL` remain available as environment variable placeholders in the workflow, but their lookup logic is not enabled, so no download URLs are currently available. The "Install SDK and Torch" step is also commented out and cannot be relied on to complete the environment automatically.

The caller's code is checked out with its full Git history and recursive submodules. After the build, the workflow recursively scans the source workspace for regular `.whl` files. If multiple files exist, they are filtered by package name prefix, and exactly one must remain. The selected artifact is collected into `dist/`, which retains only the selected wheel. Avoid leaving multiple artifacts for the same package in the build directory.

These addresses are currently hardcoded in the workflow or scripts and are not configurable inputs. Different matrix combinations must use non-conflicting upload paths.

每个矩阵任务在成功收集 wheel 后都会保存原始包到 GitHub Actions artifacts，名称为 `{package_name}-collected-wheels-{run_id}-{run_attempt}-{job-index}`，按包名、运行、重试及矩阵索引隔离。保存发生在 Repack 和重命名之前，保留原始文件名与内容；仅上传 `dist/*.whl`，构建或收集失败时不上传。

发布流程还会保存源码包、发布数据 JSON 和成功标记。仅在 `should_release=true` 且由 tag 触发或显式指定 `tag_version` 时，额外保存重打包后的 `release-wheels` artifact。非发布构建仅保存上述原始 wheel artifact。

**Currently, no GitHub Release is created, and no assets are uploaded to a Release.** The workflow step named "Save GitHub Release wheel artifacts" actually uses `actions/upload-artifact`.

## Installation Bootstrap Package Behavior

[`setup.py`](ci-templates/release-pypi/scripts/setup.py) is a source distribution template and should not be run directly as this repository's installation entry point. The workflow fills in the package name, framework, and version, then sets `__FAKE_BUILD=FAKE_WHEEL` to generate the bootstrap package. No wheels are downloaded at this stage.

When a user installs the bootstrap package, the template detects the Python, OS, Runtime, and framework versions. By default, it downloads and executes a remote `strategy.py` to match a prebuilt wheel, then extracts its dependency information. This process requires a trusted remote strategy service, an accessible artifact repository, and `wget`; it does not compile the caller's source code on the user's machine.

Related environment variables:

| Variable | Description |
| --- | --- |
| `CUDA_SDK_VER` | Explicitly specifies the Runtime, e.g., `cuda-13.0.0`; detected via `nvcc --version` if unset or incorrectly formatted |
| `SAIL_SDK_MATCH_STRATEGY` | When set to `off`, uses local naming rules to construct the wheel URL directly |
| `SAIL_PYPI_SDK_VESION` | SDK version input provided by the template; `VESION` is the existing spelling in the variable name, and whether it is used depends on the matching strategy |

## Repository Structure

```text
.github/workflows/
└── matrix-release.yml          # 可复用发布工作流

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
