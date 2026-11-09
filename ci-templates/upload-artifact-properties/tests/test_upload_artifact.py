"""Offline tests for the upload artifact properties template; curl requests are mocked and never reach Artifactory."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import Mock
from urllib.parse import unquote

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/upload_artifact.py"
WORKFLOW = ROOT.parents[1] / ".github/workflows/upload-artifact-properties.yml"
DEMO = ROOT / "workflows/demo-upload-artifact-properties.yml"

BASE_URL = "https://pkg.flytiger-eco.com/artifactory/github_generic"
RUN_ID = "100"
RUN_ATTEMPT = "1"
VERSION = "1.2.3"
UPLOAD_DIR = f"{BASE_URL}/{RUN_ID}/{RUN_ATTEMPT}/{VERSION}/"
STORAGE_DIR = f"{BASE_URL}/api/storage/{RUN_ID}/{RUN_ATTEMPT}/{VERSION}/"
DEMO_FILENAME = "demo.txt"

DEFAULT_INPUTS = {
    "version": VERSION,
    "package_type": "raw",
    "package_name": "demo_package",
    "package_version": VERSION,
    "os": "ubuntu24.04",
    "python_version": "3.12",
    "runtime": "13.0.0",
    "torch_version": "",
    "sdk_version": "2.2.0",
    "package_edition": "standard",
    "extra_info": "",
}
# Values injected by the workflow YAML from GitHub Actions context; the script
# reads them from environment variables rather than inputs.
DEFAULT_CONTEXT_ENV = {
    "GITHUB_BRANCH": "main",
    "GITHUB_TAG": "",
    "GITHUB_SHA": "abcdef1234567890",
    "GITHUB_LOG_URL": "https://example.invalid/logs/100",
}
PROPERTY_ORDER = (
    "package_type", "package_name", "package_version", "os", "python_version",
    "runtime", "torch_version", "sdk_version", "package_edition", "branch",
    "tag", "commit", "log_url", "extra_info",
)
CONTEXT_SOURCED_PROPERTIES = ("branch", "tag", "commit", "log_url")
INPUT_SOURCED_PROPERTIES = tuple(k for k in PROPERTY_ORDER if k not in CONTEXT_SOURCED_PROPERTIES)
CONTEXT_PROPERTY_ENV = {
    "branch": "GITHUB_BRANCH",
    "tag": "GITHUB_TAG",
    "commit": "GITHUB_SHA",
    "log_url": "GITHUB_LOG_URL",
}


def expected_property_values():
    values = {}
    for key in PROPERTY_ORDER:
        if key in CONTEXT_PROPERTY_ENV:
            values[key] = DEFAULT_CONTEXT_ENV[CONTEXT_PROPERTY_ENV[key]]
        else:
            values[key] = DEFAULT_INPUTS[key]
    return values


@pytest.fixture
def uploader():
    spec = importlib.util.spec_from_file_location("upload_artifact", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory(prefix=".upload-artifact-test-", dir=ROOT) as directory:
        yield Path(directory).resolve()


@pytest.fixture
def make_file(workspace):
    def make(filename=DEMO_FILENAME, directory="artifact", content=b"payload"):
        path = workspace / directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path
    return make


@pytest.fixture
def run_upload(uploader, workspace, monkeypatch):
    transport = Mock(return_value=subprocess.CompletedProcess([], 0, "201", ""))
    monkeypatch.setattr(uploader.subprocess, "run", transport)

    def run(inputs=None, **environment):
        env = {
            "ARTIFACT_DIR": str(workspace / "artifact"),
            "GITHUB_RUN_ID": RUN_ID,
            "GITHUB_RUN_ATTEMPT": RUN_ATTEMPT,
            "INPUTS_JSON": json.dumps(DEFAULT_INPUTS if inputs is None else inputs),
            "RELEASE_USERNAME": "test-user",
            "RELEASE_TOKEN": "test-token",
            "GITHUB_OUTPUT": str(workspace / "outputs"),
            **DEFAULT_CONTEXT_ENV,
        }
        env.update(environment)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return uploader.main(), transport
    return run


def _curl_args(transport):
    return [call.args[0] for call in transport.call_args_list]


def _last_curl_flags(args):
    # Return (url, upload_source) for a curl invocation.
    url = args[args.index("--") + 1]
    source = args[args.index("--upload-file") + 1] if "--upload-file" in args else None
    return url, source


def _query_pairs(url):
    _, _, query = url.partition("?")
    assert query.startswith("properties="), url
    pairs = {}
    for chunk in query[len("properties="):].split(","):
        key, _, value = chunk.partition(";")
        pairs[unquote(key)] = unquote(value)
    return pairs


def test_uploads_and_sets_properties_for_every_regular_file(make_file, run_upload, workspace):
    path = make_file(directory="artifact/nested/build output")
    before = path.read_bytes()
    code, transport = run_upload()
    assert code == 0, _curl_args(transport)
    assert transport.call_count == 2
    upload_args, storage_args = _curl_args(transport)
    upload_url, upload_source = _last_curl_flags(upload_args)
    storage_url, storage_source = _last_curl_flags(storage_args)
    assert upload_url == UPLOAD_DIR + DEMO_FILENAME
    assert upload_source == str(path)
    assert storage_url.startswith(STORAGE_DIR + DEMO_FILENAME + "?properties=")
    assert storage_source == os.devnull
    for args in (upload_args, storage_args):
        assert args[:2] == ["curl", "--disable"]
        assert args[args.index("--user") + 1] == "test-user:test-token"
        assert args[args.index("--proto") + 1] == "=https"
        assert args[args.index("--retry") + 1] == "3"
        assert "--location" not in args
        assert "--insecure" not in args
    assert path.read_bytes() == before
    outputs = (workspace / "outputs").read_text()
    assert outputs.startswith(f"batch_url={UPLOAD_DIR}\nupload_urls<<")
    assert UPLOAD_DIR + DEMO_FILENAME in outputs


def test_property_query_carries_every_configured_field(make_file, run_upload):
    make_file()
    code, transport = run_upload()
    assert code == 0
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    pairs = _query_pairs(storage_url)
    assert tuple(pairs) == PROPERTY_ORDER
    expected = expected_property_values()
    for key in PROPERTY_ORDER:
        assert pairs[key] == expected[key], key


def test_property_values_are_percent_encoded_against_separator_injection(make_file, run_upload):
    make_file()
    inputs = dict(DEFAULT_INPUTS, extra_info='{"note":"a,b;c=d"}')
    code, transport = run_upload(inputs=inputs)
    assert code == 0
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    assert "%3B" in storage_url and "%2C" in storage_url
    assert _query_pairs(storage_url)["extra_info"] == '{"note":"a,b;c=d"}'


@pytest.mark.parametrize("filename,encoded", [
    ("demo+cu130.txt", "demo%2Bcu130.txt"),
    ("a b#%.txt", "a%20b%23%25.txt"),
    ("$(touch marker).txt", "%24%28touch%20marker%29.txt"),
])
def test_filenames_are_percent_encoded_without_shell_execution(filename, encoded, make_file, run_upload, workspace):
    make_file(filename=filename)
    code, transport = run_upload()
    assert code == 0
    args = _curl_args(transport)
    upload_url = _last_curl_flags(args[0])[0]
    storage_url = _last_curl_flags(args[1])[0]
    assert upload_url == UPLOAD_DIR + encoded
    assert storage_url.startswith(STORAGE_DIR + encoded + "?properties=")
    assert not transport.call_args.kwargs.get("shell", False)
    assert not (workspace / "marker").exists()


def test_multiple_files_upload_sequentially(make_file, run_upload):
    paths = [make_file(filename=name) for name in ("c.txt", "a.txt", "b.txt")]
    code, transport = run_upload()
    assert code == 0
    assert transport.call_count == 6
    args = _curl_args(transport)
    for index, path in enumerate(sorted(paths)):
        upload_url, upload_source = _last_curl_flags(args[index * 2])
        storage_url, _ = _last_curl_flags(args[index * 2 + 1])
        assert upload_url == UPLOAD_DIR + path.name
        assert upload_source == str(path)
        assert storage_url.startswith(STORAGE_DIR + path.name + "?properties=")


def test_duplicate_basenames_both_upload_to_the_same_destination(make_file, run_upload):
    first = make_file(filename=DEMO_FILENAME, directory="artifact/one", content=b"first")
    second = make_file(filename=DEMO_FILENAME, directory="artifact/two", content=b"second")
    code, transport = run_upload()
    assert code == 0
    args = _curl_args(transport)
    upload_urls = [_last_curl_flags(a)[0] for a in args[0::2]]
    upload_sources = [_last_curl_flags(a)[1] for a in args[0::2]]
    assert set(upload_urls) == {UPLOAD_DIR + DEMO_FILENAME}
    assert sorted(upload_sources) == sorted([str(first), str(second)])


@pytest.mark.parametrize("key,value", [
    ("version", "two/three"),
    ("version", ""),
    ("version", "$(touch marker)"),
])
def test_invalid_url_inputs_rejected_before_any_request(key, value, make_file, run_upload, workspace):
    make_file()
    code, transport = run_upload(inputs=dict(DEFAULT_INPUTS, **{key: value}))
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "marker").exists()


def test_artifact_base_url_is_not_a_workflow_input(make_file, run_upload):
    # The base URL is a script constant; supplying it through inputs must not change the destination.
    make_file()
    code, transport = run_upload(inputs=dict(DEFAULT_INPUTS, artifact_base_url="https://attacker.invalid"))
    assert code == 0
    args = [call.args[0] for call in transport.call_args_list]
    urls = [a[a.index("--") + 1] for a in args]
    assert all(url.startswith(BASE_URL) for url in urls)


@pytest.mark.parametrize("key", [
    "package_type", "package_name", "package_version", "os", "python_version",
    "runtime", "sdk_version", "package_edition",
])
def test_missing_required_input_property_stops_before_upload(key, make_file, run_upload):
    make_file()
    inputs = dict(DEFAULT_INPUTS)
    del inputs[key]
    code, transport = run_upload(inputs=inputs)
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("key", ["commit", "log_url"])
def test_missing_required_context_property_stops_before_upload(key, make_file, run_upload):
    make_file()
    code, transport = run_upload(**{CONTEXT_PROPERTY_ENV[key]: ""})
    assert code == 1
    transport.assert_not_called()


def test_context_derived_inputs_are_ignored_even_when_present(make_file, run_upload):
    # Callers must not be able to spoof branch/tag/commit/log_url through INPUTS_JSON.
    make_file()
    inputs = dict(DEFAULT_INPUTS, branch="evil", tag="evil", commit="evil", log_url="https://evil.invalid")
    code, transport = run_upload(inputs=inputs)
    assert code == 0
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    pairs = _query_pairs(storage_url)
    expected = expected_property_values()
    for key in CONTEXT_SOURCED_PROPERTIES:
        assert pairs[key] == expected[key], key


@pytest.mark.parametrize("environment", [
    {"GITHUB_RUN_ID": ""}, {"GITHUB_RUN_ID": "0"}, {"GITHUB_RUN_ID": "abc"},
    {"GITHUB_RUN_ATTEMPT": ""}, {"GITHUB_RUN_ATTEMPT": "-1"},
    {"INPUTS_JSON": "{"}, {"INPUTS_JSON": "[]"},
    {"ARTIFACT_DIR": ""},
])
def test_invalid_environment_or_json_stops_before_upload(environment, make_file, run_upload):
    make_file()
    code, transport = run_upload(**environment)
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("kind", ["missing-dir", "empty-dir", "only-subdirs", "symlink", "root-file", "root-symlink"])
def test_missing_artifact_or_no_regular_files_fails(kind, workspace, make_file, run_upload):
    root = workspace / "artifact"
    if kind == "root-file":
        root.write_bytes(b"not a directory")
    elif kind == "root-symlink":
        make_file(directory="outside")
        root.symlink_to(workspace / "outside", target_is_directory=True)
    elif kind != "missing-dir":
        root.mkdir()
        if kind == "only-subdirs":
            (root / "sub").mkdir()
        elif kind == "symlink":
            original = make_file(directory="outside")
            (root / DEMO_FILENAME).symlink_to(original)
    code, transport = run_upload()
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()


def test_directory_symlinks_are_not_followed(workspace, make_file, run_upload):
    make_file(directory="outside")
    (workspace / "artifact").mkdir()
    (workspace / "artifact/link").symlink_to(workspace / "outside", target_is_directory=True)
    code, transport = run_upload()
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("RELEASE_USERNAME", ""), ("RELEASE_TOKEN", ""),
    ("RELEASE_USERNAME", "user:bad"), ("RELEASE_TOKEN", "bad\ntoken"),
])
def test_missing_or_invalid_credentials_never_upload(key, value, make_file, run_upload):
    make_file()
    code, transport = run_upload(**{key: value})
    assert code == 1
    transport.assert_not_called()


def test_upload_failure_stops_before_setting_properties(make_file, run_upload, uploader, workspace):
    make_file()
    uploader.subprocess.run.return_value = subprocess.CompletedProcess([], 22, "500", "")
    code, transport = run_upload()
    assert code == 1
    assert transport.call_count == 1
    assert not (workspace / "outputs").exists()


def test_property_failure_stops_batch_without_success_output(make_file, run_upload, uploader, workspace):
    for name in ("a.txt", "b.txt"):
        make_file(filename=name)
    uploader.subprocess.run.side_effect = [
        subprocess.CompletedProcess([], 0, "201", ""),   # upload a succeeds
        subprocess.CompletedProcess([], 22, "500", ""),  # set properties a fails
    ]
    code, transport = run_upload()
    assert code == 1
    assert transport.call_count == 2
    assert not (workspace / "outputs").exists()


def test_timeout_is_reported_without_leaking_credentials(make_file, run_upload, uploader, capsys):
    make_file()
    uploader.subprocess.run.side_effect = subprocess.TimeoutExpired(["curl", "test-token"], 1500)
    assert run_upload()[0] == 1
    captured = capsys.readouterr()
    assert "timed out" in captured.err
    assert "test-token" not in captured.out + captured.err


def test_workflow_contract_and_step_order():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_call"}
    contract = workflow["on"]["workflow_call"]
    expected_inputs = {
        "artifact_name", "version",
        *INPUT_SOURCED_PROPERTIES, "template_repository", "template_ref",
    }
    assert set(contract["inputs"]) == expected_inputs
    for name in ("artifact_name", "version", "template_ref"):
        assert contract["inputs"][name]["required"] == "true", name
    for name in INPUT_SOURCED_PROPERTIES:
        if name != "extra_info":
            assert contract["inputs"][name]["required"] == "true", name
    for name in CONTEXT_SOURCED_PROPERTIES:
        assert name not in contract["inputs"], name
    assert "artifact_base_url" not in contract["inputs"]
    assert contract["inputs"]["extra_info"]["default"] == ""
    assert contract["inputs"]["template_repository"]["default"] == "flytiger-eco/shared-workflows"
    assert set(contract["secrets"]) == {"template_token", "release_username", "release_token"}
    for name in ("release_username", "release_token"):
        assert contract["secrets"][name]["required"] == "true"
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["upload-artifact"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "30"
    guard, checkout, setup, download, upload = job["steps"]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {
        "repository": "${{ inputs.template_repository }}", "ref": "${{ inputs.template_ref }}",
        "path": "ci-template", "token": "${{ secrets.template_token || github.token }}",
        "persist-credentials": "false",
    }
    assert setup["with"]["python-version"] == "3.12"
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"]["name"] == "${{ inputs.artifact_name }}"
    assert upload["env"]["ARTIFACT_DIR"] == download["with"]["path"]
    assert upload["env"]["GITHUB_RUN_ID"] == "${{ github.run_id }}"
    assert upload["env"]["GITHUB_RUN_ATTEMPT"] == "${{ github.run_attempt }}"
    assert upload["env"]["GITHUB_BRANCH"] == "${{ github.ref_type == 'branch' && github.ref_name || '' }}"
    assert upload["env"]["GITHUB_TAG"] == "${{ github.ref_type == 'tag' && github.ref_name || '' }}"
    assert upload["env"]["GITHUB_SHA"] == "${{ github.sha }}"
    assert upload["env"]["GITHUB_LOG_URL"] == (
        "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
    )
    assert upload["env"]["INPUTS_JSON"] == "${{ toJSON(inputs) }}"
    assert upload["env"]["RELEASE_USERNAME"] == "${{ secrets.release_username }}"
    assert upload["env"]["RELEASE_TOKEN"] == "${{ secrets.release_token }}"
    assert upload["run"] == (
        "python ci-template/ci-templates/upload-artifact-properties/scripts/upload_artifact.py"
    )
    assert job["outputs"]["batch_url"] == "${{ steps.upload.outputs.batch_url }}"
    assert job["outputs"]["upload_urls"] == "${{ steps.upload.outputs.upload_urls }}"
    for key in ("batch_url", "upload_urls"):
        assert contract["outputs"][key]["value"] == "${{ jobs.upload-artifact.outputs." + key + " }}"
    assert "env" not in job
    for step in job["steps"]:
        assert "continue-on-error" not in step
        assert "${{" not in step.get("run", "")
        if step is not upload:
            assert "RELEASE_TOKEN" not in str(step.get("env", {}))
    passed = subprocess.run(["bash", "-c", guard["run"]],
                            env=dict(os.environ, ARTIFACT_NAME="x", VERSION="1.0.0"),
                            capture_output=True, text=True, timeout=5)
    assert passed.returncode == 0, passed.stderr
    for name in ("ARTIFACT_NAME", "VERSION"):
        env = {"ARTIFACT_NAME": "x", "VERSION": "1.0.0"}
        env[name] = " "
        failed = subprocess.run(["bash", "-c", guard["run"]],
                                env=dict(os.environ, **env),
                                capture_output=True, text=True, timeout=5)
        assert failed.returncode == 1
        assert name.lower() in failed.stderr


def test_demo_workflow_matches_template_contract():
    template = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    demo = yaml.load(DEMO.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(demo["on"]) == {"workflow_dispatch"}
    assert demo["permissions"] == {"contents": "read"}
    build, publish, result = (demo["jobs"][name] for name in ("build", "publish", "result"))
    assert publish["needs"] == "build"
    assert "runs-on" not in publish and "steps" not in publish
    assert publish["uses"] == (
        publish["with"]["template_repository"] + "/.github/workflows/upload-artifact-properties.yml@"
        + publish["with"]["template_ref"]
    )
    contract = template["on"]["workflow_call"]
    assert set(publish["with"]) == set(contract["inputs"])
    required_secrets = {key for key, value in contract["secrets"].items() if value["required"] == "true"}
    assert required_secrets <= set(publish["secrets"]) <= set(contract["secrets"])
    assert publish["secrets"] == {
        "release_username": "${{ secrets.RELEASE_USERNAME }}",
        "release_token": "${{ secrets.RELEASE_TOKEN }}",
    }
    artifact = next(step for step in build["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert artifact["with"]["name"] == build["outputs"]["artifact_name"]
    assert "${{ github.run_id }}" in artifact["with"]["name"]
    assert "${{ github.run_attempt }}" in artifact["with"]["name"]
    assert publish["with"]["artifact_name"] == "${{ needs.build.outputs.artifact_name }}"
    assert artifact["with"]["if-no-files-found"] == "error"
    for key in CONTEXT_SOURCED_PROPERTIES:
        assert key not in publish["with"], key
    assert result["needs"] == "publish"
    step, = result["steps"]
    assert step["env"]["BATCH_URL"] == "${{ needs.publish.outputs.batch_url }}"
    assert step["env"]["UPLOAD_URLS"] == "${{ needs.publish.outputs.upload_urls }}"
    for job in (build, result):
        assert "secrets" not in job
        for step in job["steps"]:
            if "run" in step:
                assert "${{" not in step["run"]
                checked = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True,
                                         text=True, timeout=5)
                assert checked.returncode == 0, checked.stderr


def test_readme_documents_interface_and_local_links():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    contract = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["on"]["workflow_call"]
    for section in ("inputs", "secrets", "outputs"):
        for name in contract[section]:
            assert f"| `{name}` |" in text, name
    for relative in ("../../.github/workflows/upload-artifact-properties.yml",
                     "scripts/upload_artifact.py",
                     "workflows/demo-upload-artifact-properties.yml"):
        assert f"]({relative})" in text
        assert (ROOT / relative).is_file()
    assert "https://pkg.flytiger-eco.com/artifactory/github_generic/{run_id}/{run_attempt}/{version}/{filename}" in text
    assert "{repository_id}" not in text and "{artifact_base_url}" not in text
    assert "/api/storage" in text and "properties=" in text


def test_readme_documents_context_derived_property_source():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in CONTEXT_SOURCED_PROPERTIES:
        assert f"| `{name}` |" in text, name
    for env_name in CONTEXT_PROPERTY_ENV.values():
        assert env_name in text, env_name
    assert "github.ref_type" in text and "github.sha" in text and "github.server_url" in text
    for name in CONTEXT_SOURCED_PROPERTIES:
        assert name not in contract_input_names(), name


def contract_input_names():
    contract = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["on"]["workflow_call"]
    return set(contract["inputs"])


def test_cli_runs_without_third_party_packages_or_network(workspace):
    result = subprocess.run([sys.executable, "-S", str(SCRIPT)],
                            env=dict(os.environ, ARTIFACT_DIR=str(workspace / "missing"),
                                     INPUTS_JSON="{}",
                                     GITHUB_RUN_ID=RUN_ID, GITHUB_RUN_ATTEMPT=RUN_ATTEMPT,
                                     RELEASE_USERNAME="u", RELEASE_TOKEN="t"),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "::error::" in result.stderr
