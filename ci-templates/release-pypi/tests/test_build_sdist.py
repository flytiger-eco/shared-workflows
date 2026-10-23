"""源码包及安装模板的离线测试：验证打包、版本读取和运行时匹配，不编译或下载。"""

import ast
from email.parser import Parser
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec
import zipfile

from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import Version
import pytest
import yaml


CI_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = CI_ROOT.parents[1] / ".github/workflows/matrix-release.yml"
SETUP_TEMPLATE = CI_ROOT / "scripts/setup.py"

# tag 读取及源码包构建均使用真实代码；执行 setup.py 前断言 FAKE_WHEEL，防止下载。
PYTHON_SHIM = r'''
python() {
  if [[ "${1:-}" == "setup.py" ]]; then
    [[ "${__FAKE_BUILD:-}" == "FAKE_WHEEL" ]] || return 42
    [[ "${TEST_SDIST_FAIL:-}" != "1" ]] || return 23
    [[ "${PWD##*/}" != "${TEST_SDIST_FAIL_VERSION:-}" ]] || return 23
  fi
  "$TEST_PYTHON" "$@"
}
'''


@pytest.fixture
def workflow():
    # BaseLoader 保留 GitHub Actions 的 on 键，不按 YAML 1.1 转换为布尔值。
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.fixture
def build_step(workflow):
    return next(step for step in workflow["jobs"]["build-sdist"]["steps"]
                if step.get("name") == "填充模板并构建源码包")


@pytest.fixture
def workspace():
    # 临时构建只写入工作区，测试结束后自动清理。
    with tempfile.TemporaryDirectory(prefix=".sdist-test-", dir=CI_ROOT) as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        (source / "setup.py").write_text("# 调用方原始构建脚本\n", encoding="utf-8")
        template = root / "ci-template/ci-templates/release-pypi/scripts/setup.py"
        template.parent.mkdir(parents=True)
        template.write_bytes(SETUP_TEMPLATE.read_bytes())
        (template.parent / "prepare_matrix.py").write_bytes((CI_ROOT / "scripts/prepare_matrix.py").read_bytes())
        yield root


@pytest.fixture
def run_build(build_step, workspace):
    def run(version="1.2.3+ppu2.2.0", package_name="demo_package", framework="torch",
            versions=None, **overrides):
        versions = [version] if versions is None else versions
        env = dict(os.environ, TEST_PYTHON=sys.executable,
                   GITHUB_REF_TYPE="tag", GITHUB_REF_NAME="9.9.9",
                   MATRIX_JSON=json.dumps({"include": [{"release_version": v} for v in versions]}),
                   pkg_name=package_name, framework=framework, PYTHONDONTWRITEBYTECODE="1")
        env.pop("__FAKE_BUILD", None)
        env.pop("TEST_SDIST_FAIL", None)
        env.pop("TEST_SDIST_FAIL_VERSION", None)
        env.update(overrides)
        return subprocess.run(["bash"], input=PYTHON_SHIM + build_step["run"],
                              cwd=workspace, env=env, text=True, capture_output=True,
                              timeout=60)

    return run


def test_sdist_follows_matrix_preparation_and_precedes_wheel_build(workflow):
    jobs = workflow["jobs"]
    assert jobs["build-sdist"]["needs"] == "prepare-matrix"
    assert set(jobs["matrix-job"]["needs"]) == {"prepare-matrix", "build-sdist"}
    assert jobs["matrix-job"]["strategy"]["matrix"] == "${{ fromJSON(needs.prepare-matrix.outputs.matrix) }}"
    assert jobs["build-sdist"]["if"] == "${{ needs.prepare-matrix.outputs.should_release == 'true' }}"
    assert "continue-on-error" not in jobs["build-sdist"]


def test_source_and_template_checkouts_are_separate(workflow):
    source, template = workflow["jobs"]["build-sdist"]["steps"][:2]
    assert source["uses"] == template["uses"] == "actions/checkout@v4"
    assert source["with"]["path"] == "source"
    assert source["with"]["fetch-depth"] == "0"
    assert "repository" not in source["with"]
    assert "ref" not in source["with"]
    assert template["with"]["path"] == "ci-template"
    assert template["with"]["repository"] == "${{ inputs.template_repository }}"
    assert template["with"]["ref"] == "${{ inputs.template_ref }}"
    assert template["with"]["token"] == "${{ secrets.template_token || github.token }}"
    assert source["with"]["persist-credentials"] == template["with"]["persist-credentials"] == "false"


def test_inputs_and_artifact_configuration(workflow, build_step):
    job = workflow["jobs"]["build-sdist"]
    assert "container" not in job
    assert build_step["env"] == {
        "pkg_name": "${{ needs.prepare-matrix.outputs.package_name }}",
        "framework": "${{ inputs.framework }}",
        "MATRIX_JSON": "${{ needs.prepare-matrix.outputs.matrix }}",
    }
    assert "release_token" not in str(job)
    upload = job["steps"][-1]
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["name"] == "${{ needs.prepare-matrix.outputs.package_name }}-sdist"
    assert upload["with"]["path"] == "sdist/dist/*.tar.gz"
    assert upload["with"]["if-no-files-found"] == "error"


def test_sdist_shell_syntax(workflow):
    for step in workflow["jobs"]["build-sdist"]["steps"]:
        if "run" in step:
            result = subprocess.run(["bash", "-n"], input=step["run"],
                                    text=True, capture_output=True)
            assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("version,framework,expected_version", [
    ("1.2.3+ppu2.2.0", "torch", "1.2.3+ppu2.2.0"),
    ("1.0.0+v0.3.0.ppu2.2.0", "torch", "1.0.0+v0.3.0.ppu2.2.0"),
    ("1.2.4.dev5+gabcdef.ppu2.1.0", "torch", "1.2.4.dev5+gabcdef.ppu2.1.0"),
    ("2.0.0rc1+old.ppu2.2.0", "", "2.0.0rc1+old.ppu2.2.0"),
    ("v1.0.0+V0.3.0.ppu2.2.0", "torch", "1.0.0+v0.3.0.ppu2.2.0"),
])
def test_builds_sdist_with_rendered_metadata(workspace, run_build, version, framework, expected_version):
    result = run_build(version=version, framework=framework)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (workspace / "source/setup.py").read_text() == "# 调用方原始构建脚本\n"
    assert (workspace / "ci-template/ci-templates/release-pypi/scripts/setup.py").read_bytes() == SETUP_TEMPLATE.read_bytes()

    rendered = (workspace / "sdist/builds" / version / "setup.py").read_text(encoding="utf-8")
    assignments = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(rendered).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"PACKAGE_NAME", "framework", "build_version"}
    }
    assert assignments == {
        "PACKAGE_NAME": "demo_package",
        "framework": framework,
        "build_version": version,
    }
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        prefix = archive.getnames()[0].split("/")[0]
        metadata = Parser().parsestr(archive.extractfile(f"{prefix}/PKG-INFO").read().decode("utf-8"))
        assert canonicalize_name(metadata["Name"]) == "demo-package"
        assert metadata["Version"] == expected_version
        assert metadata["Requires-Python"] == ">=3.8"
        assert archive.extractfile(f"{prefix}/setup.py").read().decode("utf-8") == rendered
        assert not any("ci-template" in name or "/source/" in name for name in archive.getnames())


def test_hyphenated_input_uses_normalized_name_in_sdist(workflow, workspace, run_build):
    step = workflow["jobs"]["prepare-matrix"]["steps"][0]
    output = workspace / "normalized-output"
    env = dict(os.environ, INPUTS_JSON=json.dumps({"package_name": "demo-package"}),
               GITHUB_OUTPUT=str(output), TEST_PYTHON=sys.executable)
    shim = 'python3() { "$TEST_PYTHON" "$@"; }\n'
    result = subprocess.run(["bash"], input=shim + step["run"], cwd=workspace,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["package_name"] == "demo_package"
    result = run_build(package_name=values["package_name"])
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = workspace / "sdist/builds/1.2.3+ppu2.2.0/setup.py"
    assert 'PACKAGE_NAME = "demo_package"' in rendered.read_text()
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert [path.name for path in archives] == ["demo_package-1.2.3+ppu2.2.0.tar.gz"]
    with tarfile.open(archives[0]) as archive:
        template = archive.extractfile("demo_package-1.2.3+ppu2.2.0/setup.py").read().decode()
        assert 'PACKAGE_NAME = "demo_package"' in template


@pytest.mark.parametrize("field,value", [
    ("package_name", ""),
    ("package_name", "../outside"),
    ("package_name", "bad|name"),
    ("package_name", 'bad"name'),
    ("package_name", "$(printf unsafe)"),
    ("package_name", "name\nother"),
    ("framework", "torch|other"),
    ("framework", 'torch"'),
    ("framework", "$(printf unsafe)"),
    ("framework", "torch\nother"),
])
def test_rejects_unsafe_metadata_before_rendering(workspace, run_build, field, value):
    result = run_build(**{field: value})
    assert result.returncode != 0
    assert f"::error::{field}" in result.stderr
    assert not (workspace / "sdist").exists()


@pytest.mark.parametrize("field", ["PACKAGE_NAME", "framework", "build_version"])
def test_missing_placeholder_fails(workspace, run_build, field):
    template = workspace / "ci-template/ci-templates/release-pypi/scripts/setup.py"
    template.write_text(template.read_text().replace(f'{field} = ""', f'{field} = "changed"'), encoding="utf-8")
    result = run_build()
    assert result.returncode != 0
    assert "::error::源码包模板缺少预期占位字段" in result.stderr
    assert not (workspace / "sdist").exists()


@pytest.mark.parametrize("version", [
    "", "not-a-version", "1..2", "1.0.0+v0.3.0|other", '1.0.0+bad"',
    "1.0.0+$(id)", "1.0.0+bad\nvalue", "../1.0.0",
])
def test_invalid_release_version_stops_build(workspace, run_build, version):
    result = run_build(version=version)
    assert result.returncode != 0
    assert not (workspace / "sdist").exists()


@pytest.mark.parametrize("matrix", [
    "", "[", "null", "[]", "{}", '{"include": []}', '{"include": {}}',
    '{"include": [null]}', '{"include": [{}]}',
    '{"include": [{"release_version": null}]}',
    '{"include": [{"release_version": 123}]}',
    '{"include": [{"release_version": "1.0.0+ppu2.2.0"}, {}]}',
])
def test_invalid_matrix_fails_before_any_build(workspace, run_build, matrix):
    result = run_build(MATRIX_JSON=matrix)
    assert result.returncode != 0
    assert "release_version" in result.stderr
    assert not (workspace / "sdist").exists()


@pytest.fixture
def generate_matrix(workspace):
    def run(tag_version="", **overrides):
        inputs = {
            "tag_version": tag_version,
            "package_name": "demo_package", "python_versions": '["312"]',
            "os_versions": '["ubuntu2404"]', "runtime_versions": '["13.0.0"]',
            "torch_versions": '["2.10.0", "2.13.0"]', "sdk_versions": '["2.2.0"]',
        }
        output = workspace / "matrix-output"
        env = dict(os.environ, INPUTS_JSON=json.dumps(inputs), SOURCE_DIR="source",
                   GITHUB_OUTPUT=str(output), GITHUB_REF_TYPE="tag",
                   GITHUB_REF_NAME="1.0.0+v0.3.0", PYTHONDONTWRITEBYTECODE="1")
        env.update(overrides)
        result = subprocess.run(
            [sys.executable, "ci-template/ci-templates/release-pypi/scripts/prepare_matrix.py"],
            cwd=workspace, env=env, text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return dict(line.split("=", 1) for line in output.read_text().splitlines())["matrix"]
    return run


@pytest.fixture
def tag_repository(workspace, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Tag Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "tag-test@example.invalid")

    def git(*args, directory="source"):
        return subprocess.run(["git", *args], cwd=workspace / directory, check=True,
                              text=True, capture_output=True)

    for directory in ("source", "ci-template"):
        git("init", "--quiet", directory=directory)
        git("add", ".", directory=directory)
        git("commit", "--quiet", "-m", "初始化测试仓库", directory=directory)
    git("tag", "9.9.9", directory="ci-template")
    return git


@pytest.mark.parametrize("annotated", [False, True])
@pytest.mark.parametrize("prefix", ["", "v", "V"])
def test_branch_sdist_uses_matrix_release_version(
    workspace, run_build, tag_repository, generate_matrix, annotated, prefix
):
    tag_repository("tag", "0.9.0")
    tag_repository("commit", "--allow-empty", "--quiet", "-m", "待发布提交")
    tag = prefix + "1.0.0+v0.3.0"
    if annotated:
        tag_repository("tag", "-a", tag, "-m", "版本 tag")
    else:
        tag_repository("tag", tag)
    matrix = generate_matrix(GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main")
    assert len(json.loads(matrix)["include"]) == 2
    result = run_build(MATRIX_JSON=matrix, GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main")
    assert result.returncode == 0, result.stdout + result.stderr
    release_version = "1.0.0+v0.3.0.ppu2.2.0"
    rendered = workspace / "sdist/builds" / release_version / "setup.py"
    assert f'build_version = "{release_version}"' in rendered.read_text()
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert [path.name for path in archives] == [f"demo_package-{release_version}.tar.gz"]
    assert result.stdout.count("已构建源码包版本：") == 1


@pytest.mark.parametrize("prefix", ["", "v", "V"])
def test_tag_sdist_uses_prepared_release_version(workspace, run_build, tag_repository, generate_matrix, prefix):
    tag = prefix + "1.0.0+v0.3.0"
    tag_repository("tag", tag)
    tag_repository("tag", "-a", "2.0.0", "-m", "同提交的其他 tag")
    matrix = generate_matrix(GITHUB_REF_NAME=tag)
    result = run_build(MATRIX_JSON=matrix)
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = workspace / "sdist/builds/1.0.0+v0.3.0.ppu2.2.0/setup.py"
    assert 'build_version = "1.0.0+v0.3.0.ppu2.2.0"' in rendered.read_text()


@pytest.mark.parametrize("prefix", ["", "v", "V"])
@pytest.mark.parametrize("tag_version,release_version", [
    ("2.4.0", "2.4.0+ppu2.2.0"),
    ("2.4.0+v0.5.0", "2.4.0+v0.5.0.ppu2.2.0"),
])
def test_tag_version_drives_sdist_build_and_publication(
    workflow, workspace, generate_matrix, run_build, tag_version, release_version, prefix
):
    # source 目录没有 Git 仓库；覆盖版本不依赖 tag 存在，两个 Torch 矩阵只构建一个源码包。
    matrix = generate_matrix(tag_version=prefix + tag_version, GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main")
    entries = json.loads(matrix)["include"]
    assert len(entries) == 2
    assert {entry["tag"] for entry in entries} == {tag_version}
    assert {entry["release_version"] for entry in entries} == {release_version}
    result = run_build(MATRIX_JSON=matrix)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("已构建源码包版本：") == 1
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert len(archives) == 1
    archive_path = archives[0]
    assert parse_sdist_filename(archive_path.name)[1] == Version(release_version)
    with tarfile.open(archive_path) as archive:
        root = archive.getnames()[0].split("/")[0]
        metadata = Parser().parsestr(archive.extractfile(f"{root}/PKG-INFO").read().decode())
        assert metadata["Version"] == str(Version(release_version))
        template = archive.extractfile(f"{root}/setup.py").read().decode()
        assert f'build_version = "{release_version}"' in template

    download = workspace / "sdist-dist"
    download.mkdir()
    shutil.copy2(archive_path, download / archive_path.name)
    step = workflow["jobs"]["publish-sdist"]["steps"][-1]
    calls = workspace / "upload-calls"
    shim = '''python3() { "$TEST_PYTHON" "$@"; }
    twine() { printf '%s\\n' "${@: -1}" >> "$TEST_UPLOAD_CALLS"; }
    '''
    env = dict(os.environ, TEST_PYTHON=sys.executable, TEST_UPLOAD_CALLS=str(calls),
               MATRIX_JSON=matrix, PACKAGE_NAME="demo_package", TWINE_USERNAME="test-user",
               TWINE_PASSWORD="test-token", ARTIFACTORY_URL=step["env"]["ARTIFACTORY_URL"])
    result = subprocess.run(["bash"], input=shim + step["run"], cwd=workspace,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["sdist-dist/" + archive_path.name]
    assert (download / archive_path.name).read_bytes() == archive_path.read_bytes()


def test_sdist_does_not_read_git_again(workspace, run_build):
    result = run_build(GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (workspace / "sdist/dist/demo_package-1.2.3+ppu2.2.0.tar.gz").is_file()


def test_sdist_failure_propagates(workspace, run_build):
    result = run_build(TEST_SDIST_FAIL="1")
    assert result.returncode == 23
    assert not list((workspace / "sdist").glob("dist/*.tar.gz"))


def test_later_sdist_failure_stops_remaining_versions(workspace, run_build):
    versions = ["1.2.3+ppu2.1.0", "1.2.3+ppu2.2.0", "1.2.4+ppu2.2.0"]
    result = run_build(versions=versions, TEST_SDIST_FAIL_VERSION=versions[1])
    assert result.returncode == 23
    assert result.stdout.count("已构建源码包版本：") == 1
    assert not (workspace / "sdist/builds" / versions[2]).exists()
    assert [p.name for p in (workspace / "sdist/dist").glob("*.tar.gz")] == [
        f"demo_package-{versions[0]}.tar.gz"
    ]


@pytest.mark.parametrize("versions", [
    ["1.0.0+v0.3.0.ppu2.2.0", "1.0.0+v0.3.0.ppu2.1.0", "1.0.0+v0.3.0.ppu2.2.0"],
    ["1.2.3+ppu2.1.0", "1.2.3+ppu2.2.0", "1.2.3+ppu2.1.0"],
    ["v1.0.0+V0.3.0.ppu2.2.0", "v1.0.0+V0.3.0.ppu2.2.0"],
])
def test_builds_and_publishes_each_distinct_release_version(workflow, workspace, run_build, versions):
    result = run_build(versions=versions)
    assert result.returncode == 0, result.stdout + result.stderr
    unique_versions = list(dict.fromkeys(versions))
    assert result.stdout.count("已构建源码包版本：") == len(unique_versions)
    assert {p.name for p in (workspace / "sdist/builds").iterdir()} == set(unique_versions)
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert len(archives) == len(unique_versions)
    expected = {Version(v): v for v in unique_versions}
    download = workspace / "sdist-dist"
    download.mkdir()
    original_bytes = {}
    for path in archives:
        name, version = parse_sdist_filename(path.name)
        assert name == "demo-package"
        with tarfile.open(path) as archive:
            root = archive.getnames()[0].split("/")[0]
            metadata = Parser().parsestr(archive.extractfile(f"{root}/PKG-INFO").read().decode())
            assert metadata["Version"] == str(version)
            setup = archive.extractfile(f"{root}/setup.py").read().decode()
            assert f'build_version = "{expected[version]}"' in setup
        shutil.copy2(path, download / path.name)
        original_bytes[path.name] = path.read_bytes()

    step = workflow["jobs"]["publish-sdist"]["steps"][-1]
    calls = workspace / "upload-calls"
    shim = '''python3() { "$TEST_PYTHON" "$@"; }
    twine() { printf '%s\\n' "${@: -1}" >> "$TEST_UPLOAD_CALLS"; }
    '''
    env = dict(os.environ, TEST_PYTHON=sys.executable, TEST_UPLOAD_CALLS=str(calls),
               MATRIX_JSON=json.dumps({"include": [{"release_version": v} for v in versions]}),
               PACKAGE_NAME="demo_package", TWINE_USERNAME="test-user", TWINE_PASSWORD="test-token",
               ARTIFACTORY_URL=step["env"]["ARTIFACTORY_URL"])
    result = subprocess.run(["bash"], input=shim + step["run"], cwd=workspace,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    uploaded = [workspace / path for path in calls.read_text().splitlines()]
    assert len(uploaded) == len(unique_versions)
    assert {path.name for path in uploaded} == set(original_bytes)
    for path in uploaded:
        assert path.parent == download
        assert path.read_bytes() == original_bytes[path.name]


def test_wheel_build_uses_caller_command_and_full_history(workflow):
    steps = workflow["jobs"]["matrix-job"]["steps"]
    checkout = next(step for step in steps if step.get("name") == "检出待编译仓库")
    build = next(step for step in steps if step.get("name") == "编译")
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["submodules"] == "recursive"
    assert checkout["with"]["persist-credentials"] == "false"
    assert "repository" not in checkout["with"]
    assert steps.index(checkout) < steps.index(build)
    assert build["env"] == {"BUILD_COMMAND": "${{ inputs.build_command }}"}
    assert workflow["on"]["workflow_call"]["inputs"]["build_command"]["required"] == "true"


@pytest.fixture
def template_module(monkeypatch):
    spec = importlib.util.spec_from_file_location("sdist_template_under_test", SETUP_TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    # 在模块边界阻断外部命令，不影响其他脚本的集成测试。
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(
        PIPE=subprocess.PIPE, run=create_autospec(
            subprocess.run, side_effect=AssertionError("测试禁止真实执行外部命令"))))
    return module


@pytest.fixture
def setup_template(template_module, monkeypatch):
    # 仅导入安装模板，阻断真实下载和外部命令；需要的环境探测由测试显式模拟。
    module = template_module
    for name in ("download_file", "run_cmd"):
        monkeypatch.setattr(module, name, create_autospec(
            getattr(module, name), side_effect=AssertionError("测试禁止真实下载或外部命令")))
    return module


@pytest.mark.parametrize("version", ["2", "3"])
def test_runtime_environment_takes_precedence_over_hgcc(setup_template, monkeypatch, version):
    monkeypatch.setenv("HGGCRT_VERSION", f"hggcrt{version}")
    assert setup_template.get_runtime_version() == version
    setup_template.subprocess.run.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "3", "hggcrt3.0", "hggcrt3\n"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_runtime_falls_back_to_hgcc_for_missing_or_invalid_env(setup_template, monkeypatch, value, stream):
    if value is None:
        monkeypatch.delenv("HGGCRT_VERSION", raising=False)
    else:
        monkeypatch.setenv("HGGCRT_VERSION", value)
    runner = setup_template.subprocess.run
    runner.side_effect = None
    output = {"stdout": "", "stderr": ""}
    output[stream] = "编译器版本信息\nRuntime API version v2\n"
    runner.return_value = subprocess.CompletedProcess(["hgcc", "--version"], 0, **output)
    assert setup_template.get_runtime_version() == "2"
    runner.assert_called_once_with(["hgcc", "--version"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, check=True)


@pytest.mark.parametrize("output", ["", "Runtime API version v3.0", "Runtime API version v3 extra"])
def test_hgcc_rejects_non_integer_runtime_versions(setup_template, output):
    runner = setup_template.subprocess.run
    runner.side_effect = None
    runner.return_value = subprocess.CompletedProcess(["hgcc", "--version"], 0, stdout=output, stderr="")
    assert setup_template.get_runtime_version_by_hgcc() is None
    runner.assert_called_once()


@pytest.mark.parametrize("error", [FileNotFoundError("hgcc"), subprocess.CalledProcessError(1, "hgcc")])
def test_runtime_detection_handles_hgcc_failure(setup_template, monkeypatch, error):
    monkeypatch.delenv("HGGCRT_VERSION", raising=False)
    setup_template.subprocess.run.side_effect = error
    assert setup_template.get_runtime_version() is None
    setup_template.subprocess.run.assert_called_once()


@pytest.mark.parametrize("tag", ["1.0.0+v0.3.0", "1.0.0+v0.3.0"])
@pytest.mark.parametrize("framework,version,suffix", [("torch", "2.10.0", "torch2.10.0"), ("", "", "")])
def test_local_wheel_selection_uses_cuda_detection(setup_template, monkeypatch, framework, version, suffix, tag):
    monkeypatch.setenv("SAIL_SDK_MATCH_STRATEGY", "off")
    monkeypatch.setenv("CUDA_SDK_VER", "cuda-13.0")
    monkeypatch.setattr(setup_template, "get_os_name", lambda: "ubuntu2404")
    monkeypatch.setattr(setup_template, "get_platform", lambda: "linux_x86_64")
    monkeypatch.setattr(setup_template, "get_framework_version", lambda: version)
    monkeypatch.setattr(setup_template, "framework", framework)
    monkeypatch.setattr(setup_template, "PACKAGE_NAME", "demo_package")
    release_version = f"{tag}.ppu2.2.0"
    monkeypatch.setattr(setup_template, "build_version", release_version)
    monkeypatch.setattr(setup_template, "BASE_WHEEL_URL", "https://example.invalid/wheels")
    python_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    filename = f"demo_package-1.0.0+cu130{suffix}ubuntu2404oe-{python_tag}-{python_tag}-linux_x86_64.whl"
    url = f"https://example.invalid/wheels/demo_package/{release_version}/{filename}"
    assert setup_template.get_wheel_url() == (url, filename)
    assert tuple(setup_template.g_wheel_result) == (url, filename, release_version)
    setup_template.download_file.assert_not_called()
    setup_template.subprocess.run.assert_not_called()


@pytest.mark.parametrize("version,expected", [("12.8", "128"), ("13.0", "130"), ("13.2", "132")])
@pytest.mark.parametrize("framework", ["", "torch"])
def test_cuda_environment_precedes_framework_and_nvcc(setup_template, monkeypatch, version, expected, framework):
    monkeypatch.setenv("CUDA_SDK_VER", f"cuda-{version}")
    monkeypatch.setattr(setup_template, "framework", framework)
    monkeypatch.setitem(sys.modules, "torch", None)
    assert setup_template.get_cuda_version() == expected
    setup_template.subprocess.run.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "13.0", "cuda-invalid"])
def test_cuda_falls_back_to_nvcc(setup_template, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CUDA_SDK_VER", raising=False)
    else:
        monkeypatch.setenv("CUDA_SDK_VER", value)
    monkeypatch.setattr(setup_template, "framework", "")
    runner = setup_template.subprocess.run
    runner.side_effect = None
    runner.return_value = subprocess.CompletedProcess(
        ["nvcc", "--version"], 0, stdout="Cuda compilation tools, release 13.0, V13.0.88", stderr="")
    assert setup_template.get_cuda_version() == "130"
    runner.assert_called_once_with(["nvcc", "--version"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, check=True)


@pytest.mark.parametrize("version,expected", [("12.8", "128"), ("13.0", "130")])
def test_cuda_falls_back_to_torch_without_importing_real_torch(setup_template, monkeypatch, version, expected):
    monkeypatch.delenv("CUDA_SDK_VER", raising=False)
    monkeypatch.setattr(setup_template, "framework", "torch")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(version=SimpleNamespace(cuda=version)))
    assert setup_template.get_cuda_version() == expected
    setup_template.subprocess.run.assert_not_called()


@pytest.mark.parametrize("error", [FileNotFoundError("nvcc"), subprocess.CalledProcessError(1, "nvcc")])
def test_cuda_detection_handles_nvcc_failure(setup_template, monkeypatch, error):
    monkeypatch.delenv("CUDA_SDK_VER", raising=False)
    monkeypatch.setattr(setup_template, "framework", "")
    setup_template.subprocess.run.side_effect = error
    assert setup_template.get_cuda_version() is None
    setup_template.subprocess.run.assert_called_once()


@pytest.mark.parametrize("runtime", [
    "13.0",
    pytest.param("13.0.0", marks=pytest.mark.xfail(
        strict=True, raises=AssertionError,
        reason="安装缺陷：三段 CUDA 环境版本生成 cu1300，但矩阵发布 cu130")),
])
def test_installer_filename_matches_real_release_matrix(
    generate_matrix, setup_template, monkeypatch, runtime
):
    entries = json.loads(generate_matrix())["include"]
    monkeypatch.setenv("SAIL_SDK_MATCH_STRATEGY", "off")
    monkeypatch.setenv("CUDA_SDK_VER", f"cuda-{runtime}")
    monkeypatch.setattr(setup_template, "sys", SimpleNamespace(
        version_info=SimpleNamespace(major=3, minor=12)))
    monkeypatch.setattr(setup_template, "get_os_name", lambda: "ubuntu2404")
    monkeypatch.setattr(setup_template, "get_platform", lambda: "linux_x86_64")
    monkeypatch.setattr(setup_template, "PACKAGE_NAME", "demo_package")
    monkeypatch.setattr(setup_template, "framework", "torch")
    for entry in entries:
        monkeypatch.setattr(setup_template, "build_version", entry["release_version"])
        monkeypatch.setattr(setup_template, "get_framework_version", lambda: entry["torch_version"])
        url, filename = setup_template.get_wheel_url()
        assert filename == entry["whl_name"]
        assert url.endswith(f"/demo_package/{entry['release_version']}/{filename}")
    setup_template.download_file.assert_not_called()


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="安装缺陷：预编译二进制 wheel 被错误重命名为 py3-none-any")
@pytest.mark.parametrize("python_tag", ["cp310", "cp312"])
def test_cached_binary_wheel_preserves_compatibility_tags(setup_template, workspace, monkeypatch, python_tag):
    from wheel.wheelfile import WheelFile

    monkeypatch.chdir(workspace)
    version = "1.0.0+ppu2.2.0"
    filename = f"demo_package-{version}-{python_tag}-{python_tag}-linux_x86_64.whl"
    dist_info = f"demo_package-{version}.dist-info"
    # 使用真实 wheel ZIP、METADATA、WHEEL 和自动生成的 RECORD，不执行伪二进制内容。
    with WheelFile(filename, "w") as wheel:
        wheel.writestr("demo_package/native.so", b"offline binary fixture")
        wheel.writestr(f"{dist_info}/METADATA",
                       f"Metadata-Version: 2.1\nName: demo_package\nVersion: {version}\n")
        wheel.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
                       f"Tag: {python_tag}-{python_tag}-linux_x86_64\n")
    original = (workspace / filename).read_bytes()
    monkeypatch.setattr(setup_template, "g_wheel_result", setup_template.WheelResult(
        "https://example.invalid/" + filename, filename, version))
    distribution = setup_template.setuptools.Distribution({"name": "demo_package", "version": version})
    command = setup_template.CachedWheelsCommand(distribution)
    command.ensure_finalized()
    command.dist_dir = str(workspace / "cached")
    command.run()
    output, = (workspace / "cached").glob("*.whl")
    assert output.read_bytes() == original
    assert parse_wheel_filename(output.name)[3] == parse_wheel_filename(filename)[3]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="安装缺陷：PEP 517 需求探测在声明构建依赖前强制导入隔离环境中不存在的 Torch")
def test_pep517_requirements_hook_without_host_torch(workspace):
    template = SETUP_TEMPLATE.read_text().replace('PACKAGE_NAME = ""', 'PACKAGE_NAME = "demo_package"')
    template = template.replace('build_version = ""', 'build_version = "1.0.0+ppu2.2.0"')
    template = template.replace('framework = ""', 'framework = "torch"')
    project = workspace / "isolated-build"
    project.mkdir()
    (project / "setup.py").write_text(template)
    # 真实执行 PEP 517 后端；模拟隔离环境中没有 Torch，并阻断所有 SDK/下载命令。
    probe = '''
import socket
import subprocess
import sys
from setuptools.build_meta import get_requires_for_build_wheel
sys.modules["torch"] = None
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("禁止网络"))
def run(*args, **kwargs):
    if kwargs.get("args") == "cat /etc/os-release":
        return subprocess.CompletedProcess(args, 0, 'NAME="Ubuntu"\\nVERSION_ID="24.04"', "")
    raise AssertionError("禁止运行外部命令")
subprocess.run = run
get_requires_for_build_wheel()
'''
    env = dict(os.environ, CUDA_SDK_VER="cuda-13.0", SAIL_SDK_MATCH_STRATEGY="off",
               PYTHONDONTWRITEBYTECODE="1", PIP_NO_INDEX="1")
    env.pop("__FAKE_BUILD", None)
    result = subprocess.run([sys.executable, "-I", "-c", probe], cwd=project, env=env,
                            text=True, capture_output=True, timeout=30)
    output = result.stdout + result.stderr
    expected = "Import torch failed: import of torch halted; None in sys.modules"
    if result.returncode and (result.returncode != 1 or expected not in output):
        pytest.fail(output)
    assert result.returncode == 0, output


@pytest.fixture
def strategy_environment(setup_template, workspace, monkeypatch):
    module = setup_template
    monkeypatch.delenv("SAIL_SDK_MATCH_STRATEGY", raising=False)
    monkeypatch.setattr(module, "PACKAGE_NAME", "demo_package")
    monkeypatch.setattr(module, "build_version", "1.0.0+ppu2.2.0")
    monkeypatch.setattr(module, "get_os_name", lambda: "ubuntu2404")
    monkeypatch.setattr(module, "get_platform", lambda: "linux_x86_64")
    monkeypatch.setattr(module, "get_framework_version", lambda: "2.10.0")
    monkeypatch.setattr(module, "get_cuda_version", lambda: "130")
    monkeypatch.setattr(module, "tempfile", SimpleNamespace(
        TemporaryDirectory=lambda **kwargs: tempfile.TemporaryDirectory(dir=workspace)))
    module.download_file.side_effect = None
    return module


def test_remote_strategy_receives_environment_and_caches_result(strategy_environment, monkeypatch):
    module = strategy_environment
    expected = ("https://example.invalid/demo.whl", "demo.whl", module.build_version)
    plugin = SimpleNamespace(run=Mock(return_value=expected))
    monkeypatch.setattr(module, "load_plugin", Mock(return_value=plugin))
    assert module.get_wheel_url() == expected[:2]
    assert tuple(module.g_wheel_result) == expected
    data, namespace = plugin.run.call_args.args
    assert data["package_name"] == "demo_package"
    assert data["runtime_version"] == "130"
    assert data["build_version"] == module.build_version
    assert namespace is module.__dict__
    module.download_file.assert_called_once()
    assert module.download_file.call_args.args == (module.STRATEGY_URL, "strategy.py")
    assert not Path(module.load_plugin.call_args.args[0]).parent.exists()


@pytest.mark.parametrize("plugin", [
    None, SimpleNamespace(),
    SimpleNamespace(run=Mock(return_value=(None, None, None))),
    SimpleNamespace(run=Mock(return_value=("", "", ""))),
    SimpleNamespace(run=Mock(return_value=("wrong-shape",))),
    SimpleNamespace(run=Mock(side_effect=ValueError("策略错误"))),
])
def test_invalid_remote_strategy_fails_closed(strategy_environment, monkeypatch, plugin):
    module = strategy_environment
    monkeypatch.setattr(module, "load_plugin", Mock(return_value=plugin))
    with pytest.raises(SystemExit) as error:
        module.get_wheel_url()
    assert error.value.code == 1
    assert not module.g_wheel_result.url
    assert not module.g_wheel_result.filename


@pytest.mark.parametrize("kind,expected", [
    ("valid", "Metadata-Version: 2.1\nRequires-Dist: numpy>=1.26\n"),
    ("missing", ""), ("corrupt", ""), ("invalid-encoding", ""),
])
def test_extracts_metadata_from_real_wheel_zip(setup_template, workspace, kind, expected):
    wheel = workspace / "metadata.whl"
    if kind == "corrupt":
        wheel.write_bytes(b"not a ZIP")
    else:
        with zipfile.ZipFile(wheel, "w") as archive:
            if kind != "missing":
                content = expected if kind == "valid" else b"\xff"
                archive.writestr("demo_package-1.0.0.dist-info/METADATA", content)
    assert setup_template.extract_metadata_from_wheel(wheel) == expected


@pytest.mark.parametrize("metadata,required,extras", [
    ("Requires-Dist: numpy>=1.26\nRequires-Dist: packaging\n", ["numpy>=1.26", "packaging"], {}),
    ('Requires-Dist: pytest>=8; extra == "test"\n', [], {"test": ["pytest>=8"]}),
    ('Requires-Dist: numpy; python_version < "3.12"\n', ['numpy; python_version < "3.12"'], {}),
    ('Requires-Dist: pytest; python_version >= "3.10" and extra == "test"\n',
     [], {"test": ['pytest; python_version >= "3.10"']}),
    ('Requires-Dist: numpy; "arm" not in platform_machine and extra == "test"\n',
     [], {"test": ['numpy; "arm" not in platform_machine']}),
    ("Requires-Dist: numpy\n\nRequires-Dist: injected\n", ["numpy"], {}),
    ("Description: 示例\n        Requires-Dist: injected\nRequires-Dist: numpy\n", ["numpy"], {}),
    ("Requires-Dist: ???\nRequires-Dist: \nRequires-Dist: numpy\n", ["numpy"], {}),
    ("", [], {}),
])
def test_dependency_metadata_preserves_markers_and_ignores_body(setup_template, metadata, required, extras):
    assert setup_template.parse_requires_dist(metadata) == (required, extras)


@pytest.mark.parametrize("output,manylinux,status,expected", [
    ('NAME="Ubuntu"\nVERSION_ID="20.04"', False, 0, "ubuntu2004"),
    ('NAME="Ubuntu"\nVERSION_ID="22.04"', False, 0, "ubuntu2204"),
    ('NAME="Ubuntu"\nVERSION_ID="24.04"', False, 0, "ubuntu2404"),
    ('NAME="alios"', False, 0, "alios7u2"),
    ('NAME="Debian"', False, 0, "alios7u2"),
    ('NAME="Ubuntu"\nVERSION_ID="24.04"', True, 0, "alios7u2"),
    ("", False, 1, "alios7u2"),
])
def test_os_detection_with_mocked_release_file(setup_template, monkeypatch, output, manylinux, status, expected):
    monkeypatch.setattr(setup_template, "USE_MANYLINUX", manylinux)
    setup_template.run_cmd.side_effect = None
    setup_template.run_cmd.return_value = subprocess.CompletedProcess([], status, output, "")
    assert setup_template.get_os_name() == expected
    setup_template.run_cmd.assert_called_once_with("cat /etc/os-release")


@pytest.mark.parametrize("platform_name,expected", [
    ("linux", "linux_x86_64"), ("win32", "win_amd64"), ("darwin", "macosx_14.4_x86_64"),
])
def test_platform_detection_does_not_depend_on_test_host(setup_template, monkeypatch, platform_name, expected):
    monkeypatch.setattr(setup_template, "sys", SimpleNamespace(platform=platform_name))
    monkeypatch.setattr(setup_template, "platform", SimpleNamespace(mac_ver=lambda: ("14.4.1", (), "")))
    assert setup_template.get_platform() == expected


@pytest.mark.parametrize("framework,version,expected", [
    ("", None, ""), (None, None, ""),
    ("torch", "2.10.0+cu130", "2.10.0"), ("torch", "2.13.0rc1", "2.13.0"),
    ("offline_framework", "3.2.1", "3.2.1"), ("offline_framework", None, ""),
])
def test_framework_detection_uses_isolated_modules(setup_template, monkeypatch, framework, version, expected):
    monkeypatch.setattr(setup_template, "framework", framework)
    if framework:
        monkeypatch.setitem(sys.modules, framework, SimpleNamespace(__version__=version) if version else None)
    assert setup_template.get_framework_version() == expected


@pytest.mark.parametrize("value", [None, "2.2.0"])
def test_sdk_environment_is_optional(setup_template, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SAIL_PYPI_SDK_VESION", raising=False)
    else:
        monkeypatch.setenv("SAIL_PYPI_SDK_VESION", value)
    assert setup_template.get_sdk_version_by_env() == value


@pytest.mark.parametrize("source,expected", [
    ("def run(data, context):\n    return data['value']\n", 42),
    ("raise ValueError('无效插件')\n", None), ("def broken(:\n", None),
])
def test_loads_local_strategy_or_reports_invalid_plugin(setup_template, workspace, source, expected):
    plugin_path = workspace / "strategy.py"
    plugin_path.write_text(source, encoding="utf-8")
    plugin = setup_template.load_plugin(plugin_path)
    if expected is None:
        assert plugin is None
    else:
        assert plugin.run({"value": 42}, {}) == expected


@pytest.mark.parametrize("temporary", [False, True])
def test_download_returns_existing_file_without_real_network(template_module, workspace, monkeypatch, temporary):
    monkeypatch.chdir(workspace)
    filename = "demo.whl"
    target = workspace / filename
    target.write_bytes(b"offline wheel")
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(template_module, "run_cmd", runner)
    result = template_module.download_file("https://example.invalid/demo.whl", filename,
                                          temp_file_path=str(workspace) if temporary else None)
    assert result == str(target)
    assert target.read_bytes() == b"offline wheel"
    assert runner.call_count == 1
    template_module.subprocess.run.assert_not_called()


@pytest.mark.parametrize("status,exception,expected_exit", [
    (0, None, 1), (1, None, 1), (None, OSError("下载器不可用"), -1),
])
def test_download_errors_abort_without_running_network(template_module, workspace, monkeypatch,
                                                      status, exception, expected_exit):
    monkeypatch.chdir(workspace)
    runner = Mock(return_value=subprocess.CompletedProcess([], status, "", ""), side_effect=exception)
    monkeypatch.setattr(template_module, "run_cmd", runner)
    with pytest.raises(SystemExit) as error:
        template_module.download_file("https://example.invalid/missing.whl", "missing.whl")
    assert error.value.code == expected_exit
    template_module.subprocess.run.assert_not_called()


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="安全缺陷：bootstrap 源码包包含硬编码的制品仓库下载密码")
def test_bootstrap_sdist_does_not_embed_download_password(workspace, run_build):
    result = run_build()
    assert result.returncode == 0, result.stderr
    path, = (workspace / "sdist/dist").glob("*.tar.gz")
    with tarfile.open(path) as archive:
        setup_path, = [name for name in archive.getnames() if name.endswith("/setup.py")]
        tree = ast.parse(archive.extractfile(setup_path).read().decode())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str) and node.value.value
                and any(isinstance(target, ast.Name) and target.id == "password" for target in node.targets)):
            # 不把发现的凭据写入断言或测试日志。
            raise AssertionError("发布的源码包包含硬编码下载密码")
