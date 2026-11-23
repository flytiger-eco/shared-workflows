"""Offline tests for temporary wheel publishing; uploads are mocked without accessing a real Artifactory instance."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import Mock
from zipfile import ZipFile
from urllib.parse import quote, unquote

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/push_tmp_wheel.py"
WORKFLOW = ROOT.parents[1] / ".github/workflows/push-tmp-wheel.yml"
WHEEL_NAME = "demo_package-1.2.3-py3-none-any.whl"
BATCH_URL = "https://pkg.flytiger-eco.com/artifactory/pypi_tmp/2026-11-02-1211/"
SOURCE_REPO = "source-owner/build-source"
SOURCE_COMMIT = "abcdef0123456789" * 2 + "abcdef01"
LOG_URL = "https://github.com/ci-owner/ci-repo/actions/runs/123456"
STORAGE_URL = "https://pkg.flytiger-eco.com/artifactory/api/storage/pypi_tmp/2026-11-02-1211/"


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location("push_tmp_wheel", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory(prefix=".wheel-test-", dir=ROOT) as directory:
        yield Path(directory).resolve()


@pytest.fixture
def make_wheel(workspace):
    def make(filename=WHEEL_NAME, directory="artifact", content=b"not a wheel ZIP"):
        path = workspace / directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path
    return make


@pytest.fixture
def run_publish(publisher, workspace, monkeypatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(workspace / "artifact"))
    monkeypatch.delenv("PACKAGE_NAME", raising=False)
    monkeypatch.delenv("WHEEL_NAME", raising=False)
    clock = Mock()
    clock.now.return_value = datetime(2026, 11, 2, 12, 11, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(publisher, "datetime", clock)
    monkeypatch.setenv("RELEASE_USERNAME", "test-user")
    monkeypatch.setenv("RELEASE_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(workspace / "outputs"))
    monkeypatch.setenv("SOURCE_REPO", SOURCE_REPO)
    monkeypatch.setenv("SOURCE_COMMIT", SOURCE_COMMIT)
    monkeypatch.setenv("SOURCE_BRANCH", "")
    monkeypatch.setenv("SOURCE_TAG", "")
    monkeypatch.setenv("GITHUB_LOG_URL", LOG_URL)
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    for key in ("EXTRA_INFO", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(key, raising=False)
    transport = Mock(side_effect=lambda args, **kwargs: subprocess.CompletedProcess(
        [], 0, "204" if args[args.index("--upload-file") + 1] == os.devnull else "201", "",
    ))
    monkeypatch.setattr(publisher.subprocess, "run", transport)

    def run(**environment):
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        return publisher.main(), transport
    return run


def test_upload_is_exact_and_https_only(make_wheel, run_publish, workspace):
    path = make_wheel(directory="artifact/nested/build output")
    before = path.read_bytes()
    code, transport = run_publish()
    assert code == 0
    assert transport.call_count == 2
    args = transport.call_args_list[0].args[0]
    assert args[:2] == ["curl", "--disable"]
    assert args[args.index("--upload-file") + 1] == str(path)
    assert args[args.index("--user") + 1] == "test-user:test-token"
    assert args[args.index("--proto") + 1] == "=https"
    assert args[args.index("--retry") + 1] == "3"
    assert "--location" not in args
    assert "--insecure" not in args
    assert args[-2:] == ["--", BATCH_URL + WHEEL_NAME]
    assert path.read_bytes() == before
    assert (workspace / "outputs").read_text() == f"upload_url={BATCH_URL}\n"


def test_all_nested_wheels_share_one_timestamp_across_minutes(make_wheel, run_publish, publisher, workspace):
    paths = [make_wheel(filename="b.whl"), make_wheel(filename="a.whl", directory="artifact/nested")]
    publisher.datetime.now.side_effect = [
        datetime(2026, 11, 2, 12, 11, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
        datetime(2026, 11, 2, 12, 12, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    ]
    code, transport = run_publish()
    assert code == 0
    publisher.datetime.now.assert_called_once_with(ZoneInfo("Asia/Shanghai"))
    assert transport.call_count == 4
    queries = []
    for index, path in enumerate(sorted(paths)):
        args = transport.call_args_list[index * 2].args[0]
        assert args[args.index("--upload-file") + 1] == str(path)
        assert args[-1] == BATCH_URL + path.name
        property_url = transport.call_args_list[index * 2 + 1].args[0][-1]
        assert property_url.startswith(STORAGE_URL + path.name + "?properties=")
        queries.append(property_url.split("?", 1)[1])
    assert len(set(queries)) == 1
    assert (workspace / "outputs").read_text() == f"upload_url={BATCH_URL}\n"


def test_timestamp_is_zero_padded_and_uses_24_hour_time(make_wheel, run_publish, publisher):
    make_wheel()
    publisher.datetime.now.return_value = datetime(2026, 1, 2, 23, 4, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    code, transport = run_publish()
    assert code == 0
    assert transport.call_args_list[0].args[0][-1].endswith("/2026-01-02-2304/" + WHEEL_NAME)


@pytest.mark.parametrize("utc_hour,expected", [(4, "2026-11-02-1211"), (20, "2026-11-03-0411")])
def test_timestamp_uses_shanghai_time(make_wheel, run_publish, publisher, utc_hour, expected):
    make_wheel()
    instant = datetime(2026, 11, 2, utc_hour, 11, 59, tzinfo=timezone.utc)
    publisher.datetime.now.side_effect = lambda tz: instant.astimezone(tz)
    code, transport = run_publish()
    assert code == 0
    publisher.datetime.now.assert_called_once_with(ZoneInfo("Asia/Shanghai"))
    assert transport.call_args_list[0].args[0][-1].endswith(f"/{expected}/{WHEEL_NAME}")


def test_only_exact_whl_suffix_files_are_uploaded(make_wheel, run_publish, workspace):
    make_wheel(filename=".hidden.whl", directory="artifact/.hidden")
    for filename in ("notes.txt", "archive.zip", "source.tar.gz", "upper.WHL", "wheel.whl.bak"):
        make_wheel(filename=filename)
    (workspace / "artifact/directory.whl").mkdir()
    code, transport = run_publish()
    assert code == 0
    assert transport.call_count == 2
    assert transport.call_args_list[0].args[0][-1] == BATCH_URL + ".hidden.whl"


@pytest.mark.parametrize("content", [b"", b"not a ZIP", b"PK\x03\x04broken ZIP"])
def test_empty_or_invalid_wheel_contents_are_uploaded_unchanged(content, make_wheel, run_publish):
    path = make_wheel(filename="arbitrary-name.whl", content=content)
    code, transport = run_publish()
    assert code == 0
    assert transport.call_count == 2
    assert path.read_bytes() == content


def test_wheel_archive_is_neither_validated_nor_extracted(make_wheel, run_publish, workspace):
    path = make_wheel()
    with ZipFile(path, "w") as archive:
        archive.writestr("../outside.txt", "not extracted")
        archive.writestr("wrong.dist-info/METADATA", "invalid metadata")
        archive.writestr("wrong.dist-info/RECORD", "invalid hashes")
    before = path.read_bytes()
    code, transport = run_publish()
    assert code == 0
    assert transport.call_count == 2
    assert path.read_bytes() == before
    assert not (workspace / "outside.txt").exists()
    assert not (path.parent / "wrong.dist-info").exists()


@pytest.mark.parametrize("filename,encoded", [
    ("demo+cu130.whl", "demo%2Bcu130.whl"),
    ("a b#%.whl", "a%20b%23%25.whl"),
    ("*.whl", "%2A.whl"),
    ("line\nbreak.whl", "line%0Abreak.whl"),
    ("$(touch marker).whl", "%24%28touch%20marker%29.whl"),
])
def test_arbitrary_filenames_are_encoded_without_shell_execution(filename, encoded, make_wheel, run_publish, workspace):
    make_wheel(filename=filename)
    code, transport = run_publish()
    assert code == 0
    assert transport.call_args_list[0].args[0][-1] == BATCH_URL + encoded
    assert transport.call_args_list[1].args[0][-1].startswith(STORAGE_URL + encoded + "?properties=")
    assert all(not call.kwargs.get("shell", False) for call in transport.call_args_list)
    assert not (workspace / "marker").exists()


def test_duplicate_basenames_are_all_uploaded_to_the_same_destination(make_wheel, run_publish):
    first = make_wheel(directory="artifact/one", content=b"first")
    second = make_wheel(directory="artifact/two", content=b"second")
    code, transport = run_publish()
    assert code == 0
    assert transport.call_count == 4
    for call, path in zip(transport.call_args_list[::2], [first, second]):
        args = call.args[0]
        assert args[args.index("--upload-file") + 1] == str(path)
        assert args[-1] == BATCH_URL + WHEEL_NAME


@pytest.mark.parametrize("kind", ["missing-dir", "empty-dir", "no-wheels", "directory", "symlink", "root-file", "root-symlink"])
def test_missing_artifact_or_no_regular_wheels_fails(kind, workspace, make_wheel, run_publish):
    root = workspace / "artifact"
    if kind == "root-file":
        root.write_bytes(b"not a directory")
    elif kind == "root-symlink":
        make_wheel(directory="outside")
        root.symlink_to(workspace / "outside", target_is_directory=True)
    elif kind != "missing-dir":
        root.mkdir()
        if kind == "no-wheels":
            (root / "notes.txt").write_text("unrelated")
        elif kind == "directory":
            (root / "directory.whl").mkdir()
        elif kind == "symlink":
            original = make_wheel(directory="outside")
            (root / WHEEL_NAME).symlink_to(original)
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()


def test_directory_symlinks_are_not_followed(workspace, make_wheel, run_publish):
    make_wheel(directory="outside")
    (workspace / "artifact").mkdir()
    (workspace / "artifact/link").symlink_to(workspace / "outside", target_is_directory=True)
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


def test_empty_artifact_environment_fails_without_upload(run_publish):
    code, transport = run_publish(ARTIFACT_DIR="")
    assert code == 1
    transport.assert_not_called()


def test_scan_errors_are_not_silently_ignored(make_wheel, run_publish, publisher, monkeypatch):
    make_wheel()

    def fail_walk(root, *, onerror, followlinks):
        onerror(PermissionError("Cannot read artifact directory"))

    monkeypatch.setattr(publisher.os, "walk", fail_walk)
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


def test_partial_failure_stops_batch_without_success_output(make_wheel, run_publish, publisher, workspace):
    for filename in ("a.whl", "b.whl", "c.whl"):
        make_wheel(filename=filename)
    publisher.subprocess.run.side_effect = [
        subprocess.CompletedProcess([], 0, "201", ""),
        subprocess.CompletedProcess([], 0, "204", ""),
        subprocess.CompletedProcess([], 22, "500", ""),
    ]
    code, transport = run_publish()
    assert code == 1
    assert transport.call_count == 3
    assert not (workspace / "outputs").exists()


def test_success_without_github_output(make_wheel, run_publish, capsys):
    make_wheel()
    code, transport = run_publish(GITHUB_OUTPUT="")
    assert code == 0
    assert transport.call_count == 2
    assert BATCH_URL in capsys.readouterr().out


@pytest.mark.parametrize("key,value", [("RELEASE_USERNAME", ""), ("RELEASE_TOKEN", ""),
                                       ("RELEASE_USERNAME", "user:bad"), ("RELEASE_TOKEN", "bad\ntoken")])
def test_missing_or_invalid_credentials_never_upload(key, value, make_wheel, run_publish):
    make_wheel()
    code, transport = run_publish(**{key: value})
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("status,returncode", [("302", 0), ("401", 22), ("500", 22), ("000", 7), ("not-http", 0)])
def test_http_or_network_failure_fails_without_outputs(status, returncode, make_wheel, run_publish,
                                                     publisher, workspace, capsys):
    make_wheel()
    publisher.subprocess.run.side_effect = None
    publisher.subprocess.run.return_value = subprocess.CompletedProcess([], returncode, status, "test-token")
    code, transport = run_publish()
    assert code == 1
    transport.assert_called_once()
    assert not (workspace / "outputs").exists()
    captured = capsys.readouterr()
    assert "test-token" not in captured.out + captured.err


def test_timeout_does_not_log_credentials(make_wheel, run_publish, publisher, capsys):
    make_wheel()
    publisher.subprocess.run.side_effect = subprocess.TimeoutExpired(["curl", "test-token"], 1500)
    assert run_publish()[0] == 1
    captured = capsys.readouterr()
    assert "timed out" in captured.err
    assert "test-token" not in captured.out + captured.err


def test_properties_use_explicit_source_and_current_run(make_wheel, run_publish):
    make_wheel()
    code, transport = run_publish(
        SOURCE_BRANCH="release/1.2", SOURCE_TAG="v1.2.3",
        GITHUB_REPOSITORY="ci-owner/ci-repo", GITHUB_SHA="0" * 40,
    )
    assert code == 0
    call = transport.call_args_list[1]
    args = call.args[0]
    assert args[:2] == ["curl", "--disable"]
    assert args[args.index("--upload-file") + 1] == os.devnull
    assert args[args.index("--user") + 1] == "test-user:test-token"
    assert args[args.index("--proto") + 1] == "=https"
    assert args[args.index("--retry") + 1] == "3"
    assert "--location" not in args and "--insecure" not in args
    assert not call.kwargs.get("shell", False)
    assert args[-2] == "--"
    assert args[-1] == (
        STORAGE_URL + WHEEL_NAME + "?properties="
        + f"repo={quote(SOURCE_REPO, safe='')};commit={SOURCE_COMMIT};"
        + "branch=release%2F1.2;tag=v1.2.3;"
        + f"log_url={quote(LOG_URL, safe='')};run_id=123456;run_attempt=2&recursive=0"
    )
    assert "workflow=" not in args[-1]


@pytest.mark.parametrize("extra_info", [
    '{"runtime":"cuda","flags":["a","b"],"enabled":true,"value":null}',
    '{\n  "text": "a=b;c,d|e&x=%+?#",\n  "path": "C:\\\\tmp"\n}',
    '[1,{"nested":[false,null,"\\u2603"]}]',
    '"a=b,c|d\\\\e"',
    "null", "42", "", "  { }  ",
    "build_tool=vllm-sail/scripts/build_wheels.sh;vllm_commit=abcdef0;image=registry/image:tag",
])
def test_extra_info_is_accepted_but_not_written(extra_info, make_wheel, run_publish, publisher, workspace):
    for name in ("a.whl", "b.whl"):
        make_wheel(filename=name)
    code, transport = run_publish(EXTRA_INFO=extra_info)
    assert code == 0
    assert transport.call_count == 4
    assert "extra_info" not in publisher.PROPERTY_ENV
    for call, filename in zip(transport.call_args_list[1::2], ("a.whl", "b.whl")):
        url = call.args[0][-1]
        assert url == (
            STORAGE_URL + filename + "?properties="
            + f"repo={quote(SOURCE_REPO, safe='')};commit={SOURCE_COMMIT};"
            + f"log_url={quote(LOG_URL, safe='')};run_id=123456;run_attempt=2&recursive=0"
        )
        assert "extra_info" not in url
    assert (workspace / "outputs").read_text() == f"upload_url={BATCH_URL}\n"


@pytest.mark.parametrize("key,value", [
    ("SOURCE_COMMIT", " \n\t"),
    ("SOURCE_REPO", " \n\t"), ("SOURCE_REPO", "owner"),
    ("SOURCE_REPO", "https://github.com/owner/repo"), ("SOURCE_REPO", "owner/.."),
    ("SOURCE_REPO", "owner/repo/extra"),
    ("SOURCE_COMMIT", "abcdef0"),
    ("SOURCE_COMMIT", "a" * 39), ("SOURCE_COMMIT", "a" * 41), ("SOURCE_COMMIT", "z" * 40),
    ("GITHUB_LOG_URL", ""), ("GITHUB_RUN_ID", ""), ("GITHUB_RUN_ID", "0"),
    ("GITHUB_RUN_ID", "-1"), ("GITHUB_RUN_ID", "abc"),
    ("GITHUB_RUN_ATTEMPT", ""), ("GITHUB_RUN_ATTEMPT", "0"), ("GITHUB_RUN_ATTEMPT", "1.5"),
])
def test_invalid_metadata_fails_before_any_request(key, value, make_wheel, run_publish, workspace):
    make_wheel()
    code, transport = run_publish(**{key: value})
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()


@pytest.mark.parametrize("key,property_name", [
    ("SOURCE_REPO", "repo"), ("SOURCE_COMMIT", "commit"),
    ("SOURCE_BRANCH", "branch"), ("SOURCE_TAG", "tag"),
])
def test_missing_source_fails_without_falling_back_to_trigger_context(
    key, property_name, make_wheel, run_publish, monkeypatch, workspace, capsys,
):
    make_wheel()
    monkeypatch.delenv(key)
    code, transport = run_publish(
        GITHUB_LOG_URL="", GITHUB_SERVER_URL="https://github.com",
        GITHUB_REPOSITORY=SOURCE_REPO, GITHUB_SHA=SOURCE_COMMIT, GITHUB_REF_NAME="main",
    )
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()
    assert f"{property_name} must be provided" in capsys.readouterr().err


@pytest.mark.parametrize("repo", ["", SOURCE_REPO])
@pytest.mark.parametrize("commit", ["", SOURCE_COMMIT])
@pytest.mark.parametrize("branch", ["", "release/1.2"])
@pytest.mark.parametrize("tag", ["", "v1.2.3"])
def test_empty_source_properties_are_omitted(repo, commit, branch, tag, make_wheel, run_publish, workspace):
    for filename in ("a.whl", "b.whl"):
        make_wheel(filename=filename)
    code, transport = run_publish(
        SOURCE_REPO=repo, SOURCE_COMMIT=commit, SOURCE_BRANCH=branch, SOURCE_TAG=tag,
        GITHUB_REPOSITORY="trigger-owner/trigger-repo", GITHUB_SHA="0" * 40, GITHUB_REF_NAME="main",
    )
    assert code == 0
    assert transport.call_count == 4
    expected_properties = {key: value for key, value in (
        ("repo", repo), ("commit", commit), ("branch", branch), ("tag", tag),
        ("log_url", LOG_URL), ("run_id", "123456"), ("run_attempt", "2"),
    ) if value != ""}
    expected_query = "properties=" + ";".join(
        f"{key}={quote(value, safe='')}" for key, value in expected_properties.items()
    )
    for call, filename in zip(transport.call_args_list[1::2], ("a.whl", "b.whl")):
        url = call.args[0][-1]
        assert url == STORAGE_URL + filename + "?" + expected_query + "&recursive=0"
        pairs = url.split("?properties=", 1)[1].removesuffix("&recursive=0").split(";")
        assert all(pair.split("=", 1)[1] for pair in pairs)
        assert {key: unquote(value) for key, value in (pair.split("=", 1) for pair in pairs)} == expected_properties
    assert (workspace / "outputs").read_text() == f"upload_url={BATCH_URL}\n"


@pytest.mark.parametrize("log_url", [None, "", " \n\t"])
@pytest.mark.parametrize("server_url,expected_server", [
    ("https://github.com", "https://github.com"),
    ("https://github.com/", "https://github.com"),
    ("https://github.example.com/", "https://github.example.com"),
])
def test_native_run_context_fallback_preserves_required_source(
    log_url, server_url, expected_server, make_wheel, run_publish, monkeypatch, workspace,
):
    for filename in ("a.whl", "b.whl"):
        make_wheel(filename=filename)
    for key in ("EXTRA_INFO", "GITHUB_LOG_URL"):
        monkeypatch.delenv(key, raising=False)
    if log_url is not None:
        monkeypatch.setenv("GITHUB_LOG_URL", log_url)
    code, transport = run_publish(
        GITHUB_SERVER_URL=server_url, GITHUB_REPOSITORY="ci-owner/ci-repo",
        GITHUB_RUN_ID="44707911317", GITHUB_RUN_ATTEMPT="3",
        GITHUB_SHA="0" * 40, GITHUB_REF_NAME="main",
    )
    assert code == 0
    assert transport.call_count == 4
    expected_log = f"{expected_server}/ci-owner/ci-repo/actions/runs/44707911317"
    for call, filename in zip(transport.call_args_list[1::2], ("a.whl", "b.whl")):
        assert call.args[0][-1] == (
            STORAGE_URL + filename + "?properties="
            + f"repo={quote(SOURCE_REPO, safe='')};commit={SOURCE_COMMIT};"
            + f"log_url={quote(expected_log, safe='')};run_id=44707911317;run_attempt=3&recursive=0"
        )
    assert (workspace / "outputs").read_text() == f"upload_url={BATCH_URL}\n"


def test_explicit_log_url_takes_precedence_over_native_context(make_wheel, run_publish):
    make_wheel()
    code, transport = run_publish(
        GITHUB_SERVER_URL="https://github.example.com", GITHUB_REPOSITORY="other-owner/other-repo",
    )
    assert code == 0
    property_url = transport.call_args_list[1].args[0][-1]
    assert f"log_url={quote(LOG_URL, safe='')};" in property_url
    assert "github.example.com" not in unquote(property_url)


@pytest.mark.parametrize("missing", ["GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"])
def test_log_url_fallback_requires_complete_native_context(missing, make_wheel, run_publish, workspace, capsys):
    make_wheel()
    environment = {
        "GITHUB_LOG_URL": "", "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "ci-owner/ci-repo", "GITHUB_RUN_ID": "123456",
    }
    environment[missing] = ""
    code, transport = run_publish(**environment)
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()
    assert "GITHUB_LOG_URL must not be empty" in capsys.readouterr().err


@pytest.mark.parametrize("key,value", [
    ("GITHUB_RUN_ID", "0"), ("GITHUB_RUN_ID", "abc"),
    ("GITHUB_RUN_ATTEMPT", ""), ("GITHUB_RUN_ATTEMPT", "0"), ("GITHUB_RUN_ATTEMPT", "1.5"),
])
def test_log_url_fallback_does_not_bypass_run_identifier_validation(key, value, make_wheel, run_publish):
    make_wheel()
    code, transport = run_publish(
        GITHUB_LOG_URL="", GITHUB_SERVER_URL="https://github.com", GITHUB_REPOSITORY="ci-owner/ci-repo",
        **{key: value},
    )
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("status,returncode", [
    ("200", 0), ("201", 0), ("302", 0), ("401", 22), ("500", 22), ("000", 7), ("test-token", 0),
])
def test_property_failure_stops_batch_without_success_output(status, returncode, make_wheel, run_publish,
                                                           publisher, workspace, capsys):
    for name in ("a.whl", "b.whl"):
        make_wheel(filename=name)
    publisher.subprocess.run.side_effect = [
        subprocess.CompletedProcess([], 0, "201", ""),
        subprocess.CompletedProcess([], returncode, status, "test-token"),
    ]
    code, transport = run_publish()
    assert code == 1
    assert transport.call_count == 2
    assert not (workspace / "outputs").exists()
    captured = capsys.readouterr()
    assert "expected HTTP 204" in captured.err
    assert "test-token" not in captured.out + captured.err


def test_property_timeout_does_not_log_credentials(make_wheel, run_publish, publisher, workspace, capsys):
    make_wheel()
    publisher.subprocess.run.side_effect = [
        subprocess.CompletedProcess([], 0, "201", ""),
        subprocess.TimeoutExpired(["curl", "test-token"], 540),
    ]
    code, transport = run_publish()
    assert code == 1
    assert transport.call_count == 2
    assert not (workspace / "outputs").exists()
    captured = capsys.readouterr()
    assert "timed out" in captured.err
    assert "test-token" not in captured.out + captured.err


def test_workflow_wiring_and_secret_scope():
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    call = workflow["on"]["workflow_call"]
    assert set(workflow["on"]) == {"workflow_call"}
    assert set(call["inputs"]) == {
        "artifact_name", "template_repository", "template_ref", "repo", "commit", "branch", "tag", "extra_info",
    }
    for name in ("artifact_name", "template_ref", "repo", "commit", "branch", "tag"):
        assert call["inputs"][name]["required"] == "true"
        assert call["inputs"][name]["type"] == "string"
        assert "default" not in call["inputs"][name]
    assert call["inputs"]["extra_info"] == {
        "description": call["inputs"]["extra_info"]["description"],
        "required": "false", "type": "string", "default": "",
    }
    assert not {"log_url", "run_id", "run_attempt", "workflow"} & set(call["inputs"])
    job = workflow["jobs"]["push-wheel"]
    guard, checkout, setup, download, upload = job["steps"]
    assert workflow["permissions"] == {"contents": "read"}
    assert checkout["with"] == {
        "repository": "${{ inputs.template_repository }}", "ref": "${{ inputs.template_ref }}",
        "path": "ci-template", "token": "${{ secrets.template_token || github.token }}",
        "persist-credentials": "false",
    }
    assert setup["with"]["python-version"] == "3.12"
    assert not any("pip install" in step.get("run", "") for step in job["steps"])
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {"name": "${{ inputs.artifact_name }}",
                                "path": "${{ runner.temp }}/push-tmp-wheel-artifact"}
    assert upload["env"]["ARTIFACT_DIR"] == download["with"]["path"]
    source_env = {"repo": "SOURCE_REPO", "commit": "SOURCE_COMMIT", "branch": "SOURCE_BRANCH",
                  "tag": "SOURCE_TAG", "extra_info": "EXTRA_INFO"}
    assert set(upload["env"]) == {
        "ARTIFACT_DIR", "RELEASE_USERNAME", "RELEASE_TOKEN", "GITHUB_LOG_URL", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
        *source_env.values(),
    }
    for name, env_name in source_env.items():
        assert upload["env"][env_name] == "${{ inputs." + name + " }}"
    assert upload["env"]["GITHUB_LOG_URL"] == (
        "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
    )
    assert upload["env"]["GITHUB_RUN_ID"] == "${{ github.run_id }}"
    assert upload["env"]["GITHUB_RUN_ATTEMPT"] == "${{ github.run_attempt }}"
    assert upload["env"]["RELEASE_USERNAME"] == "${{ secrets.release_username }}"
    assert upload["env"]["RELEASE_TOKEN"] == "${{ secrets.release_token }}"
    assert upload["run"] == "python ci-template/ci-templates/push-tmp-wheel/scripts/push_tmp_wheel.py"
    assert job["outputs"]["upload_url"] == "${{ steps.upload.outputs.upload_url }}"
    assert call["outputs"]["upload_url"]["value"] == "${{ jobs.push-wheel.outputs.upload_url }}"
    assert "env" not in job
    for step in job["steps"]:
        assert "continue-on-error" not in step
        assert "${{" not in step.get("run", "")
        if step is not upload:
            assert "RELEASE_TOKEN" not in step.get("env", {})
    for name in ("", " \n\t", "wheel-artifact"):
        result = subprocess.run(["bash", "-c", guard["run"]], env=dict(os.environ, ARTIFACT_NAME=name),
                                capture_output=True, text=True, timeout=5)
        assert (result.returncode == 0) == bool(name.strip())


def test_demo_workflow_matches_template_contract():
    template = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    demo_path = ROOT / "workflows/demo-push-tmp-wheel.yml"
    demo = yaml.load(demo_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(demo["on"]) == {"workflow_dispatch"}
    dispatch_inputs = demo["on"]["workflow_dispatch"]["inputs"]
    assert set(dispatch_inputs) == {"source_repository", "source_ref"}
    assert all(value["required"] == "false" and value["default"] == "" for value in dispatch_inputs.values())
    assert demo["permissions"] == {"contents": "read"}
    build, publish, result = (demo["jobs"][name] for name in ("build", "publish", "result"))
    assert publish["needs"] == "build"
    assert "runs-on" not in publish and "steps" not in publish
    assert publish["uses"] == (
        publish["with"]["template_repository"] + "/.github/workflows/push-tmp-wheel.yml@"
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
    checkout = next(step for step in build["steps"] if step.get("uses") == "actions/checkout@v4")
    source = next(step for step in build["steps"] if step.get("id") == "source")
    assert checkout["with"]["repository"] == source["env"]["SOURCE_REPO"] == (
        "${{ inputs.source_repository || github.repository }}"
    )
    assert checkout["with"]["ref"] == "${{ inputs.source_ref || github.ref }}"
    assert checkout["with"]["fetch-depth"] == "0"
    assert build["steps"].index(checkout) < build["steps"].index(source)
    assert "git rev-parse HEAD" in source["run"]
    assert "git symbolic-ref --quiet --short HEAD" in source["run"]
    assert "git tag --points-at HEAD" in source["run"]
    for name in ("repo", "commit", "branch", "tag"):
        assert build["outputs"][name] == "${{ steps.source.outputs." + name + " }}"
        assert publish["with"][name] == "${{ needs.build.outputs." + name + " }}"
    assert publish["with"]["extra_info"] == '{"build_tool":"python-build"}'
    artifact = next(step for step in build["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert artifact["with"]["name"] == build["outputs"]["artifact_name"]
    assert "${{ github.run_id }}" in artifact["with"]["name"]
    assert "${{ github.run_attempt }}" in artifact["with"]["name"]
    assert publish["with"]["artifact_name"] == "${{ needs.build.outputs.artifact_name }}"
    assert artifact["with"]["path"] == "${{ runner.temp }}/push-tmp-wheel-dist/*.whl"
    assert artifact["with"]["if-no-files-found"] == "error"
    assert result["needs"] == "publish"
    assert result["steps"][0]["env"]["UPLOAD_URL"] == "${{ needs.publish.outputs.upload_url }}"
    for job in (build, result):
        assert "secrets" not in job
        for step in job["steps"]:
            if "run" in step:
                # Check syntax only; do not execute build or publishing commands.
                assert "${{" not in step["run"]
                checked = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True,
                                         text=True, timeout=5)
                assert checked.returncode == 0, checked.stderr


def test_readme_documents_interface_and_local_links():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    contract = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["on"]["workflow_call"]
    for section in ("inputs", "secrets", "outputs"):
        for name in contract[section]:
            assert f"| `{name}` |" in text
    for relative in ("../../.github/workflows/push-tmp-wheel.yml", "scripts/push_tmp_wheel.py",
                     "workflows/demo-push-tmp-wheel.yml"):
        assert f"]({relative})" in text
        assert (ROOT / relative).is_file()
    assert "Asia/Shanghai (UTC+08:00)" in text and "2026-11-02-1211" in text
    assert "https://pkg.flytiger-eco.com/artifactory/pypi_tmp/{timestamp}/{filename}" in text
    assert "No wheel validation" in text
    assert "| `package_name` |" not in text and "| `wheel_name` |" not in text


def test_cli_runs_without_third_party_packages_or_network(workspace):
    result = subprocess.run([sys.executable, "-S", str(SCRIPT)],
                            env=dict(os.environ, ARTIFACT_DIR=str(workspace / "missing")),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "::error::artifact" in result.stderr
