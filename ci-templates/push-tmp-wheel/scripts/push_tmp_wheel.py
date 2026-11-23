"""Upload all artifact .whl files unchanged into one timestamped temporary directory."""

from datetime import datetime
from zoneinfo import ZoneInfo
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote


ARTIFACTORY_URL = "https://pkg.flytiger-eco.com/artifactory/pypi_tmp/"
ARTIFACTORY_STORAGE_URL = "https://pkg.flytiger-eco.com/artifactory/api/storage/pypi_tmp/"
PROPERTY_ENV = {
    "repo": "SOURCE_REPO",
    "commit": "SOURCE_COMMIT",
    "branch": "SOURCE_BRANCH",
    "tag": "SOURCE_TAG",
    "log_url": "GITHUB_LOG_URL",
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
}


def build_property_query(env):
    for key in ("repo", "commit", "branch", "tag"):
        if PROPERTY_ENV[key] not in env:
            raise ValueError(f"{key} must be provided")
    properties = {key: env.get(name, "") for key, name in PROPERTY_ENV.items()}
    repo = properties["repo"]
    if repo and (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+", repo)
        or repo.split("/")[-1] in (".", "..")
    ):
        raise ValueError("repo must be in owner/repo format when non-empty")
    if properties["commit"] and not re.fullmatch(r"[0-9a-fA-F]{40}", properties["commit"]):
        raise ValueError("commit must be a full 40-character hexadecimal SHA when non-empty")
    if not properties["log_url"].strip():
        # Older workflow wrappers do not inject GITHUB_LOG_URL; use the runner's native context.
        server_url = env.get("GITHUB_SERVER_URL", "").strip().rstrip("/")
        repository = env.get("GITHUB_REPOSITORY", "").strip()
        run_id = properties["run_id"]
        if server_url and repository and run_id:
            properties["log_url"] = f"{server_url}/{repository}/actions/runs/{run_id}"
    if not properties["log_url"].strip():
        raise ValueError(
            "GITHUB_LOG_URL must not be empty unless GITHUB_SERVER_URL, GITHUB_REPOSITORY, and GITHUB_RUN_ID are available"
        )
    for key in ("run_id", "run_attempt"):
        if not re.fullmatch(r"[1-9][0-9]*", properties[key]):
            raise ValueError(f"{PROPERTY_ENV[key]} must be a positive integer")
    pairs = []
    for key, value in properties.items():
        if value == "":
            continue
        escaped_value = re.sub(r"([\\,|=])", r"\\\1", value)
        pairs.append(f"{key}={quote(escaped_value, safe='')}")
    return "properties=" + ";".join(pairs)


def find_wheels(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("artifact directory does not exist or is not a regular directory")

    def scan_error(error):
        raise error

    matches = []
    # Select regular files by suffix only; do not inspect wheel names, metadata, or contents.
    # Do not follow symlinks outside the downloaded artifact.
    for directory, directories, filenames in os.walk(root, onerror=scan_error, followlinks=False):
        directories[:] = [name for name in directories if not (Path(directory) / name).is_symlink()]
        for name in filenames:
            path = Path(directory) / name
            if name.endswith(".whl") and not path.is_symlink() and path.is_file():
                matches.append(path)
    if not matches:
        raise ValueError("No .whl files found in the artifact")
    return sorted(matches)


def upload_wheel(path, batch_url, username, token):
    if not username.strip() or not token.strip():
        raise ValueError("Publishing username and token must not be empty")
    if ":" in username or any(char in username + token for char in "\r\n\x00"):
        raise ValueError("Invalid publishing credential format")
    url = batch_url + quote(path.name, safe="")
    # Disable default curl configuration and redirects; never log commands containing credentials or response bodies.
    result = subprocess.run(
        [
            "curl", "--disable", "--fail", "--silent", "--show-error", "--retry", "3",
            "--connect-timeout", "30", "--max-time", "600", "--proto", "=https",
            "--user", f"{username}:{token}", "--upload-file", str(path.resolve()),
            "--output", os.devnull, "--write-out", "%{http_code}", "--", url,
        ],
        capture_output=True,
        text=True,
        timeout=1500,
        check=False,
    )
    status = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"2[0-9]{2}", status):
        safe_status = status if re.fullmatch(r"[0-9]{3}", status) else "unknown"
        raise ValueError(f"Artifactory upload failed: curl exit code {result.returncode}, HTTP {safe_status}")
    return url


def set_properties(storage_url, query, username, token):
    result = subprocess.run(
        [
            "curl", "--disable", "--fail", "--silent", "--show-error", "--retry", "3",
            "--connect-timeout", "30", "--max-time", "120", "--retry-max-time", "120",
            "--proto", "=https", "--user", f"{username}:{token}",
            "--upload-file", os.devnull, "--output", os.devnull,
            "--write-out", "%{http_code}", "--", f"{storage_url}?{query}&recursive=0",
        ],
        capture_output=True,
        text=True,
        timeout=540,
        check=False,
    )
    status = result.stdout.strip()
    if result.returncode or status != "204":
        safe_status = status if re.fullmatch(r"[0-9]{3}", status) else "unknown"
        raise ValueError(
            f"Artifactory property update failed: curl exit code {result.returncode}, HTTP {safe_status}; expected HTTP 204"
        )


def main():
    try:
        artifact_dir = os.environ.get("ARTIFACT_DIR", "")
        if not artifact_dir:
            raise ValueError("ARTIFACT_DIR must not be empty")
        paths = find_wheels(Path(artifact_dir))
        property_query = build_property_query(os.environ)
        # Read the Shanghai time only once so all files uploaded across a minute boundary still share the same directory.
        timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d-%H%M")
        batch_url = ARTIFACTORY_URL + timestamp + "/"
        username = os.environ.get("RELEASE_USERNAME", "")
        token = os.environ.get("RELEASE_TOKEN", "")
        for path in paths:
            url = upload_wheel(path, batch_url, username, token)
            storage_url = ARTIFACTORY_STORAGE_URL + timestamp + "/" + quote(path.name, safe="")
            set_properties(storage_url, property_query, username, token)
            print(f"Uploaded: {url}", flush=True)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"upload_url={batch_url}\n")
        print(f"Uploaded {len(paths)} files to: {batch_url}")
    except subprocess.TimeoutExpired:
        print("::error::Artifactory upload or property update timed out", file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError) as error:
        message = str(error).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
