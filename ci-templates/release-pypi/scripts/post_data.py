import argparse
import base64
import hashlib
import json
import os
import re
import requests
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REQUEST_TIMEOUT = (30, 300)
OPEN_API_BASE_URL = "https://www.flytiger-eco.com"
PATH_SAVE = "/open/flyingTiger/synchronize/artifact/save"
OPEN_API_CLIENT_ID = "sync-artifact-client-prod"
SERVER_PUBLIC_KEY_B64 = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApzI2dQiGSUT9bJoqg1Ek0yMaCAiC4zvMWsryh+weplsQy1dQ2c3IopjDXDEQZ0LkU8j2AhUwU5oQmSP2OISiC5uf3z9siQFgCTxIwuWy/KW6BMBGHfY5+yHY3QnsooqtoSVk8hDGNtHb+iwJZ+DTwYWLpqLU2XzCF1P87eG5CLpAvkdZ/i+Rc1fjkEfnTWXsjtNQ4RGXqNeDzWGBICJ6agEQItwJP09W3yRVdBDb0qME4OP/yWHQYTQ/srdm9bkRVf388OgZeIgQb02f0nLUX4+w2mXcYgtZSqD5t4/oDIDQ6iqlnuAi0EL1SBD6aFJ0VPZLhP487v57rD5y7wbDawIDAQAB"


@dataclass
class OpenApiConfig:
    base_url: str
    client_id: str
    server_public_key_b64: str = field(repr=False)
    client_private_key_b64: str = field(repr=False)
    timeout: tuple[int, int] = REQUEST_TIMEOUT

    @classmethod
    def from_env(cls):
        """Shared configuration comes from script constants; only the client private key is read from the environment."""
        return cls(
            base_url=OPEN_API_BASE_URL,
            client_id=OPEN_API_CLIENT_ID,
            server_public_key_b64=SERVER_PUBLIC_KEY_B64,
            client_private_key_b64=os.environ.get("CLIENT_PRIVATE_KEY_B64", ""),
        )

    def validate(self):
        for name, value in (
            ("OPEN_API_BASE_URL", self.base_url),
            ("OPEN_API_CLIENT_ID", self.client_id),
            ("SERVER_PUBLIC_KEY_B64", self.server_public_key_b64),
            ("CLIENT_PRIVATE_KEY_B64", self.client_private_key_b64),
        ):
            if (
                not isinstance(value, str) or not value or value != value.strip()
                or any(character.isspace() for character in value) or value.startswith("<")
            ):
                raise ValueError(f"{name} must not be empty, contain whitespace, or use a placeholder value")
        if not self.client_id.isascii():
            raise ValueError("OPEN_API_CLIENT_ID must use ASCII characters")
        try:
            parts = urlsplit(self.base_url)
            valid_url = (
                parts.scheme in {"http", "https"} and parts.hostname
                and parts.username is None and parts.password is None
                and parts.path in {"", "/"} and not parts.query and not parts.fragment
            )
            parts.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("OPEN_API_BASE_URL must be an HTTP(S) address containing only the protocol, host, and optional port")


class OpenApiClient:
    def __init__(self, config: OpenApiConfig):
        self.config = config
        self.config.validate()

        try:
            self._server_pub_key = serialization.load_der_public_key(
                base64.b64decode(self.config.server_public_key_b64, validate=True)
            )
            self._client_priv_key = serialization.load_der_private_key(
                base64.b64decode(self.config.client_private_key_b64, validate=True),
                password=None,
            )
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ValueError("Key loading failed; provide a Base64-encoded DER public key and an unencrypted DER private key") from None
        if not isinstance(self._server_pub_key, rsa.RSAPublicKey) or not isinstance(
            self._client_priv_key, rsa.RSAPrivateKey
        ):
            raise ValueError("The server public key and client private key must be RSA keys")

    def _build_url(self, path: str) -> str:
        return self.config.base_url.rstrip("/") + path

    def _encrypt_payload(self, plain_bytes: bytes) -> dict[str, str]:
        aes_key = os.urandom(32)
        iv = os.urandom(12)

        cipher_blob = AESGCM(aes_key).encrypt(iv, plain_bytes, None)

        enc_key = self._server_pub_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        return {
            "encKey": base64.b64encode(enc_key).decode('utf-8'),
            "iv": base64.b64encode(iv).decode('utf-8'),
            "cipher": base64.b64encode(cipher_blob).decode('utf-8'),
        }

    def _sign_request(self, body_bytes: bytes, timestamp: str) -> str:
        digest_hex = hashlib.sha256(body_bytes).hexdigest()
        string_to_sign = (
            f"{self.config.client_id}\nPOST\n{PATH_SAVE}\n{timestamp}\n{digest_hex}"
        ).encode("utf-8")

        signature = self._client_priv_key.sign(
            string_to_sign,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def save_artifact(self, business_data: dict[str, Any]) -> requests.Response:
        """Encrypt the business data and sign the exact request body that is sent; failures are not retried automatically."""
        plain_bytes = json.dumps(
            business_data, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        envelope = self._encrypt_payload(plain_bytes)
        body_bytes = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

        timestamp_ms = str(int(time.time() * 1000))
        signature = self._sign_request(body_bytes, timestamp_ms)

        headers = {
            "Content-Type": "application/json",
            "X-Open-Client": self.config.client_id,
            "X-Open-Timestamp": timestamp_ms,
            "X-Open-Sign": signature,
        }
        url = self._build_url(PATH_SAVE)
        try:
            response = requests.post(
                url,
                data=body_bytes,
                headers=headers,
                timeout=self.config.timeout,
                # The signature is bound to POST and the fixed path; redirects must not alter the request or forward signature headers.
                allow_redirects=False,
            )
        except requests.RequestException as error:
            # Request exceptions may contain credentials, so only the exception type is kept.
            raise ValueError(f"release data request failed: {type(error).__name__}") from None
        print(f"Release data API response: HTTP {response.status_code}", flush=True)
        check_response(response)
        return response


@dataclass
class PypiData:
    artifactName: str = ""
    businessType: str = "PYPI"
    idempotentKey: str = ""
    attributes: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def normalize_target(target):
    if not isinstance(target, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", target
    ):
        raise ValueError("package_name must be a valid Python package name")
    return target.replace("-", "_")


def check_response(response):
    # requests' raise_for_status does not reject 3xx; block redirects from counting as success here too.
    if not 200 <= response.status_code < 300:
        raise ValueError(f"Release data API request failed with HTTP {response.status_code}")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type in {"text/html", "application/xhtml+xml"}:
        raise ValueError("The API returned HTML, possibly a login page; release success cannot be confirmed")


def build_payload(record, target):
    """Assemble the data per the API fields; keep the original matrix values and leave unprovided string fields empty."""
    if not isinstance(record, dict):
        raise ValueError("The release data JSON must be an object")
    payload = PypiData().to_dict()
    if record["artifactName"] != target or record["item"] != target:
        raise ValueError("The release data package name does not match package_name")

    record_package_name = record["artifactName"]
    record_sdk_version = record["sdkVersion"]
    record_item = record["item"]
    record_os_type_list = record["osTypeList"]
    record_runtime_version = record["runtimeVersion"]
    record_framework = record["framework"]
    record_python = record["python"]
    record_tag = record["tag"]

    payload["artifactName"] = record_package_name
    payload["businessType"] = "PIP"

    attributes = dict()
    attributes["item"] = [record_item]
    attributes["type"] = [""]
    attributes["sdk_version"] = [record_sdk_version.lower().replace("v", "").replace("sail", "").replace("ppu", "")]

    # Handle osTypeList
    _record_os = record_os_type_list.lower().replace(".", "")
    record_os = ""
    if "ubuntu24" in _record_os:
        record_os = "ubuntu24"
    elif "ubuntu22" in _record_os:
        record_os = "ubuntu22"
    elif "ubuntu20" in _record_os:
        record_os = "ubuntu20"
    elif "alios7u2" in _record_os:
        record_os = "alios7u2"
    else:
        raise ValueError("osTypeList is not in the supported list")
    attributes["os"] = [record_os]

    # Derive version and sub_version from the tag
    tag_list = record_tag.split("+")
    version = ""
    sub_version = ""
    if len(tag_list) == 2:
        version, sub_version = tag_list
    elif len(tag_list) == 1:
        version = tag_list[0]
    sub_version = sub_version.lower().replace("v", "")
    attributes["sub_version"] = [sub_version]
    attributes["version"] = [version]

    # Handle cudaVersion
    _cuda_version = record_runtime_version.lower().replace("cuda", "").replace("cu", "")
    _cuda_version_list = _cuda_version.split(".")
    if len(_cuda_version_list) == 3:
        attributes["cuda"] = [f"{_cuda_version_list[0]}.{_cuda_version_list[1]}"]
    else:
        raise ValueError(f"Unsupported cudaVersion: {record_runtime_version}")

    # Handle framework
    framework_version = record_framework.lower().replace("torch", "")
    if framework_version:
        attributes["framework"] = [f"torch{framework_version}"]
    else:
        attributes["framework"] = [""]

    # Handle the python version
    _python_version = record_python.lower().replace("python", "").replace("py", "").replace("cp", "").replace(".", "")
    python_version_list = _python_version.split("3", 1)
    python_version = f"3.{python_version_list[1]}"
    attributes["python"] = [python_version]

    # Handle cmdLine and specificLine
    attributes["cmd_line"] = [f"pip install {attributes["item"][0]}=={attributes["version"][0]} -i https://pkg.flytiger-eco.com/artifactory/api/pypi/pypi_index/simple"]
    if attributes["sub_version"][0]:
        attributes["specific_line"] = [f"pip install {attributes["item"][0]}=={attributes["version"][0]}+v{attributes["sub_version"][0]}.ppu{attributes["sdk_version"][0]} -i https://pkg.flytiger-eco.com/artifactory/api/pypi/pypi_index/simple"]
    else:
        attributes["specific_line"] = [f"pip install {attributes["item"][0]}=={attributes["version"][0]}+ppu{attributes["sdk_version"][0]} -i https://pkg.flytiger-eco.com/artifactory/api/pypi/pypi_index/simple"]

    payload["attributes"] = attributes

    # Handle idempotentKey
    pypi_key = f"{attributes['item'][0]}-{attributes['version'][0]}-{attributes['cuda'][0]}-{attributes['python'][0]}-{attributes["os"][0]}-{attributes['framework'][0]}-{attributes['sdk_version'][0]}-{attributes['sub_version'][0]}-{attributes['type'][0]}"
    payload['idempotentKey'] = hashlib.md5(pypi_key.encode('utf-8')).hexdigest()

    return payload


def read_payload(path, target):
    if not path.is_file() or path.is_symlink():
        raise ValueError("Release data must be a regular JSON file, not a symlink")
    return build_payload(json.loads(path.read_text(encoding="utf-8")), target)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Post release data for successful matrix jobs one by one")
    parser.add_argument("--input-dir", type=Path, required=True, help="Directory containing the release JSON files")
    args = parser.parse_args(argv)
    target = normalize_target(os.environ.get("PACKAGE_NAME", ""))
    client = OpenApiClient(OpenApiConfig.from_env())
    if not args.input_dir.is_dir() or args.input_dir.is_symlink():
        raise ValueError("The release data directory does not exist or is not a regular directory")
    files = sorted(args.input_dir.glob("*.json"))
    if not files:
        raise ValueError("No release data JSON found to post")
    # Validate every file first, then send one request per file, so a bad file cannot cause an avoidable partial push.
    records = [(path, read_payload(path, target)) for path in files]
    for path, payload in records:
        client.save_artifact(payload)
        print("------------------------")
        print(f"Pushed release data: {json.dumps(path.name, ensure_ascii=True)}", flush=True)
        print(payload)
    print("------------------------")
    print(f"Pushed {len(records)} release data records", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(f"::error::failed to post release data: {error}") from None
