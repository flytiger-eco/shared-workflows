import os
import platform
import setuptools
import subprocess
import sys
import zipfile
import re
import json
import tempfile
import shutil
import importlib
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from packaging.version import parse
from packaging.version import Version
from packaging.requirements import Requirement, InvalidRequirement
from setuptools.command.install import install as _install
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel

PACKAGE_NAME = ""
build_version = ""
edition_type = "oe"
USE_MANYLINUX = False
RUNTIME_MARK = "cu"
framework = ""
BASE_WHEEL_URL = "https://pkg.flytiger-eco.com/artifactory/pypi_generic/"
JSON_URL = "https://pkg.flytiger-eco.com/artifactory/pypi_generic/"
STRATEGY_URL = "https://pkg.flytiger-eco.com/artifactory/pypi_generic/strategy.py"
STRATEGY_SIG_URL = "https://pkg.flytiger-eco.com/artifactory/pypi_generic/strategy.sig"
extra_info = {}
RED = "\033[91m"
RESET = "\033[0m"


@dataclass
class WheelResult:
    """
    Data class encapsulating the wheel lookup result.

    Throughout the build flow, WheelResult is the unified return structure, carrying
    the key information of the wheel matched from the remote repository or the local
    strategy, including the download URL, the file name, and the final build version.

    Attributes:
        url: Full download URL of the wheel. None if no matching package is found.
        filename: Wheel file name (e.g. "package-1.0.0+{RUNTIME_MARK}121ubuntu2204ce-cp310-cp310-linux_x86_64.whl").
                  None if no matching package is found.
        build_version: The finally matched build version string (e.g. "1.0.0+v0.1.0.ppu2.0.0").
                       None if no matching package is found.
    """
    url: Optional[str]
    filename: Optional[str]
    build_version: Optional[str]

    def __iter__(self):
        return iter((self.url, self.filename, self.build_version))


g_wheel_result: Optional[WheelResult] = None


def run_cmd(cmd: str, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    ret = subprocess.run(args=cmd, shell=True, stdout=stdout, stderr=stderr, encoding="utf-8")
    return ret


def get_os_name():
    """
    Detect the current operating system and return a normalized OS name string.

    The system type is determined by reading /etc/os-release. Supported systems:
      - Ubuntu 20.04 -> "ubuntu2004"
      - Ubuntu 22.04 -> "ubuntu2204"
      - Ubuntu 24.04 -> "ubuntu2404"
      - AliOS -> "alios7u2"
    When the USE_MANYLINUX global flag is enabled, unsupported systems fall back to "alios7u2".
    Other unsupported systems return "any".

    The name is used to compose the wheel file name so the installed package matches the target OS.

    Returns:
        str: Normalized OS name, e.g. "ubuntu2204", "alios7u2", or "any".
    """
    cmd = "cat /etc/os-release"
    ret = run_cmd(cmd)
    if ret.returncode != 0:
        print("Fail to get OS info, use alios7u2")
        return "alios7u2"
    else:
        output = ret.stdout
        if USE_MANYLINUX:
            print("This package is build in manylinux, use os alios7u2.")
            return "alios7u2"
        if "Ubuntu" in output and "20.04" in output:
            return "ubuntu2004"
        if "Ubuntu" in output and "22.04" in output:
            return "ubuntu2204"
        if "Ubuntu" in output and "24.04" in output:
            return "ubuntu2404"
        if "alios" in output:
            return "alios7u2"
        print("No exact match for the OS, use alios7u2")
        return "alios7u2"


def get_platform():
    if sys.platform.startswith("linux"):
        return "linux_x86_64"
    elif sys.platform == "darwin":
        mac_version = ".".join(platform.mac_ver()[0].split(".")[:2])
        return f"macosx_{mac_version}_x86_64"
    elif sys.platform == "win32":
        return "win_amd64"
    else:
        raise ValueError("Unsupported platform: {}".format(sys.platform))


def get_framework_version():
    """
    Get the version of the deep learning framework installed in the current environment.

    The framework to detect is determined by the global variable framework:
      - If framework is empty or None, return an empty string (no framework dependency).
      - If framework is "torch", import PyTorch and extract its version. The local version
        identifier (e.g. "+{RUNTIME_MARK}121") is stripped, keeping only major.minor.micro.
      - For any other value, try to import the module dynamically via importlib and read its __version__ attribute.

    The framework version is used to compose the wheel file name so the installed
    package is compatible with the current framework version.

    Returns:
        str: Framework version string (e.g. "2.1.0"), or an empty string when no framework is specified.

    Raises:
        SystemExit: When framework is "torch" but importing PyTorch fails.
    """
    if framework == '' or framework is None:
        return ""
    if framework == 'torch':
        try:
            import torch
            torch_version_raw = parse(torch.__version__)
            try:
                torch_version = f"{torch_version_raw.major}.{torch_version_raw.minor}.{torch_version_raw.micro}"
            except AttributeError:
                torch_version = str(torch_version_raw).split("+")[0]
            return torch_version
        except Exception as e:
            print(f"Import torch failed: {e}")
            exit(1)
    else:
        try:
           import importlib
           __sail_framework = importlib.import_module(framework)
           return __sail_framework.__version__
        except Exception as e:
           print(f"Import framework {framework} failed: {e}")
           return ""


def get_cuda_version():
    """
    Get the CUDA version of the current environment (dot-free format).

    The CUDA_SDK_VER environment variable is read first; when no valid version is
    obtained, detection depends on framework:
      - If framework is "torch", extract the CUDA major/minor version from torch.version.cuda.
        The program exits when importing PyTorch fails.
      - Otherwise get the version via nvcc --version.
    Dot separators are removed from the returned version.

    The returned dot-free version is used directly in wheel file name composition (e.g. "cu121").

    Returns:
        str or None: CUDA version without dot separators (e.g. "121", "128"),
                     or None when it cannot be obtained.

    Raises:
        SystemExit: When framework is "torch" but the import fails.
    """
    cuda_version = get_cuda_version_by_env()
    if cuda_version is not None:
        return cuda_version.replace(".", "")
    if framework == 'torch':
        try:
            import torch
            torch_cuda_version = parse(torch.version.cuda)
            try:
                cuda_version = f"{torch_cuda_version.major}{torch_cuda_version.minor}"
                print("Get cuda version from torch.version.cuda major + minor")
            except AttributeError:
                cuda_version = str(torch_cuda_version).replace(".", "")
                print("Get cuda version from torch.version.cuda")
            return cuda_version
        except Exception as e:
            print(f"Import torch failed: {e}")
            exit(1)
    else:
        cuda_version = get_cuda_version_by_nvcc()
        if cuda_version is None:
            return None
        return cuda_version.replace(".", "")


def get_cuda_version_by_nvcc():
    """
    Get the CUDA version installed on the current system by running nvcc --version.

    The nvcc output is parsed with a regex matching the "release X.Y" version format.
    For example, from "Cuda compilation tools, release 12.2, V12.2.140" the version "12.2" is extracted.

    This method is used when no version is given via environment variables and PyTorch is not the dependency.

    Returns:
        str or None: CUDA version string (e.g. "12.2"), or None when it cannot be obtained.
    """
    try:
        result = subprocess.run(['nvcc', '--version'],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                check=True)
        output = result.stdout
        # example: Cuda compilation tools, release 12.2, V12.2.140
        match = re.search(r'release (\d+\.\d+)', output)
        if match:
            version = match.group(1)  # 12.2
            print(f"Get cuda version by nvcc --version: {output}")
            return version
        else:
            print(f"Miss match cuda version by nvcc --version: {output}")
            return None
    except Exception as e:
        print(f"Unexpected error for nvcc --version: {e}")
        return None


def get_cuda_version_by_env():
    """
    Get the CUDA version from the CUDA_SDK_VER environment variable.

    The expected format of CUDA_SDK_VER is "cuda-X.Y" (e.g. "cuda-12.8");
    the "X.Y" part is extracted as the version. The extracted version is also
    validated with packaging.version.Version.

    This method takes precedence over PyTorch and nvcc detection and is used to explicitly specify the CUDA version.

    Returns:
        str or None: CUDA version string (e.g. "12.8"), or None when the variable is unset or malformed.
    """
    try:
        # CUDA_SDK_VER=cuda-12.8
        output = os.getenv('CUDA_SDK_VER')
        if output is not None and 'cuda-' in output:
            print(f"Get cuda version by env 'CUDA_SDK_VER': {output}")
            cuda_version = output.split('-')[1]
            Version(cuda_version)
            return cuda_version
        else:
            print(f"Can not get env 'CUDA_SDK_VER': {output}. Example: CUDA_SDK_VER=cuda-12.8")
            return None
    except Exception as e:
        print(f"Unexpected error for get env CUDA_SDK_VER: {e}")
        return None


def get_runtime_version():
    """
    Get the runtime version of the current environment.

    HGGCRT_VERSION is read first; when it is unset or malformed, detection falls back to hgcc.

    Returns:
        str or None: Runtime version string, or None when it cannot be obtained.
    """
    runtime_version = get_runtime_version_by_env()
    if runtime_version is not None:
        return runtime_version
    return get_runtime_version_by_hgcc()


def get_runtime_version_by_hgcc():
    """
    Get the runtime version installed on the current system by running hgcc --version.

    The hgcc output is parsed with a regex matching the "Runtime API version v2" format.
    For example, from "Runtime API version v2" the version "2" is extracted.

    This method is used when the environment variable provides no valid version; only integer API versions are accepted.

    Returns:
        str or None: Runtime version string (e.g. "2"), or None when it cannot be obtained.
    """
    try:
        result = subprocess.run(['hgcc', '--version'],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                check=True)
        output = os.linesep.join((result.stdout or "", result.stderr or ""))
        # Example: Runtime API version v2, only the numeric part 2 is returned.
        for line in output.splitlines():
            match = re.fullmatch(r'\s*Runtime\s+API\s+version\s+v([0-9]+)\s*', line)
            if match:
                version = match.group(1)
                print(f"Get runtime version by hgcc --version: {output}")
                return version
        print(f"Miss match runtime version by hgcc --version: {output}")
        return None
    except Exception as e:
        print(f"Unexpected error for hgcc --version: {e}")
        return None


def get_runtime_version_by_env():
    """
    Get the hggcrt runtime version from the HGGCRT_VERSION environment variable.
    HGGCRT_VERSION must be the hggcrt prefix plus an integer API version, e.g. "hggcrt3".
    Returns:
        str or None: hggcrt version string (e.g. "3"), or None when the variable is unset or malformed.
    """
    try:
        # HGGCRT_VERSION=hggcrt3
        output = os.getenv('HGGCRT_VERSION')
        match = re.fullmatch(r'hggcrt([0-9]+)', output or "")
        if match:
            print(f"Get hggcrt version by env 'HGGCRT_VERSION': {output}")
            return match.group(1)
        else:
            print(f"Can not get env 'HGGCRT_VERSION': {output}. Example: HGGCRT_VERSION=hggcrt3")
            return None
    except Exception as e:
        print(f"Unexpected error for get env HGGCRT_VERSION: {e}")
        return None


def get_sdk_version_by_env():
    """
    Get the SDK version from the SAIL_PYPI_SDK_VERSION environment variable.

    This variable lets the user specify the SDK version manually when it cannot be
    detected automatically through other means. The expected format is a plain
    version string such as "1.7.0".

    Returns:
        str or None: SDK version string, or None when the variable is unset.
    """
    try:
        # SDK_VESION_FOR_PYPI=1.7.0
        output = os.getenv('SAIL_PYPI_SDK_VERSION')
        if output is not None:
            print(f"Get sdk version by env 'SAIL_PYPI_SDK_VERSION': {output}")
            return output
        else:
            print(f"Can not get env 'SAIL_PYPI_SDK_VERSION': {output}. Example: SAIL_PYPI_SDK_VERSION=1.7.0")
            return None
    except Exception as e:
        print(f"Unexpected error for get env SAIL_PYPI_SDK_VERSION: {e}")
        return None


def load_plugin(file_path):
    """
    Dynamically load a Python module as a plugin from the given file path.

    Uses the low-level importlib API to load a .py file at any path into memory as a
    module named "dynamic_plugin". This mechanism loads the matching logic from the
    strategy file (strategy.py) downloaded from the remote at runtime, allowing the
    strategy to be updated dynamically without modifying this script.

    Args:
        file_path: Absolute path of the Python file to load.

    Returns:
        module or None: The loaded module object, whose functions can be called via
                        module.run() etc. Returns None when loading fails.
    """
    try:
        spec = importlib.util.spec_from_file_location("dynamic_plugin", file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"Failed to load plugin from {file_path}: {e}")
        return None
    return module


def get_wheel_url():
    """
    Determine the download URL and file name of the target wheel based on the
    current environment (Python version, platform, OS, RUNTIME version, framework version, etc.).

    This function is the core entry of the whole build flow, coordinating environment
    detection and version matching. The process is:

    1. Collect current environment info: Python version (cpXY), platform tag, OS name,
       package version, framework version, and RUNTIME version.
    2. Check the SAIL_SDK_MATCH_STRATEGY environment variable:
       - If "off", use the legacy local matching logic (get_wheel_url_old)
         and compose the wheel URL directly from the environment parameters.
       - Otherwise (default), download the strategy script strategy.py from the remote,
         load it dynamically and run its run() method for intelligent version matching.
         The strategy script downloads the index.json version index file and picks the
         best match considering SDK version compatibility.
    3. Store the match result in the global variable g_wheel_result.
    4. If no matching package is found, print detailed environment info and exit.

    Returns:
        tuple: (wheel_url, wheel_filename), the full download URL and file name of the wheel.

    Raises:
        SystemExit: When no installation package compatible with the current environment is found.

    Side Effects:
        Modifies the global variable g_wheel_result to store the full match result.
    """
    python_version = None
    platform_name = None
    os_name = None
    package_version = None
    framework_version = None
    runtime_version = None
    strategy = None
    global g_wheel_result
    try:
        python_version = f"cp{sys.version_info.major}{sys.version_info.minor}"
        platform_name = get_platform()
        os_name = get_os_name()
        package_version = build_version.split("+")[0]
        framework_version = get_framework_version()
        runtime_version = get_cuda_version()
        strategy = os.getenv('SAIL_SDK_MATCH_STRATEGY')
        if strategy and strategy.lower() == "off":
            wheel_url, wheel_filename = get_wheel_url_old(
                python_version=python_version,
                platform_name=platform_name,
                os_name=os_name,
                package_version=package_version,
                framework_version=framework_version,
                runtime_version=runtime_version
                )
            g_wheel_result = WheelResult(url=wheel_url, filename=wheel_filename, build_version=build_version)
        else:
            with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
                strategy_file_path = os.path.join(temp_dir, "strategy.py")
                download_file(STRATEGY_URL, "strategy.py", temp_file_path=temp_dir)
                strategy_module = load_plugin(strategy_file_path)
                data = {
                    "base_wheel_url": BASE_WHEEL_URL,
                    "base_json_url": JSON_URL,
                    "build_version": build_version,
                    "edition_type": edition_type,
                    "framework": framework,
                    "package_name": PACKAGE_NAME,
                    "runtime_mark": RUNTIME_MARK,
                    "runtime_version": runtime_version
                }
                if strategy_module is None or not hasattr(strategy_module, "run"):
                    print(f"Can not access strategy.py")
                    g_wheel_result = WheelResult(url=None, filename=None, build_version=None)
                else:
                    wheel_url, wheel_filename, best_matching_build_version = strategy_module.run(data, globals())
                    g_wheel_result = WheelResult(url=wheel_url, filename=wheel_filename, build_version=best_matching_build_version)
                    print(f"Strategy return: {wheel_url} {wheel_filename} {best_matching_build_version}")
    except Exception as e:
        g_wheel_result = WheelResult(url=None, filename=None, build_version=None)
        print(f"{e}")
        print(f"{RED}Strategy:{strategy}. No installation package compatible with the current environment{RESET}")
        print(f"{RED}python:{python_version}, platform:{platform_name}, os:{os_name}, runtime_version:{runtime_version}{RESET}")
        print(f"{RED}version:{package_version}, framework:{framework}{framework_version}, build version:{build_version}{RESET}")
        print(f"{RED}Contact FlyTiger to release the current configuration package if required.{RESET}")
        exit(1)
    if g_wheel_result.url is None or g_wheel_result.filename is None or g_wheel_result.url == "" or g_wheel_result.filename == "":
        print(f"{RED}No installation package compatible with the current environment{RESET}")
        print(f"{RED}python:{python_version}, platform:{platform_name}, os:{os_name}, runtime_version:{runtime_version}{RESET}")
        print(f"{RED}version:{package_version}, framework:{framework}{framework_version}, build version:{build_version}{RESET}")
        print(f"{RED}Contact FlyTiger to release the current configuration package if required.{RESET}")
        exit(1)
    return g_wheel_result.url, g_wheel_result.filename


def get_wheel_url_old(python_version, platform_name, os_name, package_version, framework_version, runtime_version):
    """
    Compose the wheel download URL and file name from local configuration (no remote strategy dependency).

    When SAIL_SDK_MATCH_STRATEGY is set to "off", remote strategy matching is skipped
    and the wheel file name and URL are composed from the given environment parameters
    by fixed rules.

    The file name format follows these rules:
      - With a framework dependency: {package}-{version}+{RUNTIME_MARK}{RUNTIME}{framework}{framework_version}{OS}{edition_type}-{Python}-{Python}-{platform}.whl
      - Without a framework dependency: {package}-{version}+{RUNTIME_MARK}{RUNTIME}{OS}{edition_type}-{Python}-{Python}-{platform}.whl

    Args:
        python_version: Python version tag (e.g. "cp310").
        platform_name: Platform tag (e.g. "linux_x86_64").
        os_name: Operating system name (e.g. "ubuntu2204").
        package_version: Package version without the build suffix (e.g. "1.0.0").
        framework_version: Framework version (e.g. "2.1.0"); empty means no framework dependency.
        runtime_version: RUNTIME version (e.g. CUDA "121" or HGGCRT "3").

    Returns:
        tuple: (wheel_url, wheel_filename) tuple.

    Side Effects:
        Modifies the global variable g_wheel_result.
    """
    global g_wheel_result
    print("Get whl url with local config.")
    if framework != "" and framework_version is not None:
        wheel_filename = f"{PACKAGE_NAME}-{package_version}+{RUNTIME_MARK}{runtime_version}{framework}{framework_version}{os_name}{edition_type}-{python_version}-{python_version}-{platform_name}.whl"
        wheel_url = f"{BASE_WHEEL_URL}/{PACKAGE_NAME}/{build_version}/{wheel_filename}"
        print(f"Whl with framework is wheel_url: {wheel_url}")
        g_wheel_result = WheelResult(url=wheel_url, filename=wheel_filename, build_version=build_version)
        return wheel_url, wheel_filename
    else:
        wheel_filename = f"{PACKAGE_NAME}-{package_version}+{RUNTIME_MARK}{runtime_version}{os_name}{edition_type}-{python_version}-{python_version}-{platform_name}.whl"
        wheel_url = f"{BASE_WHEEL_URL}/{PACKAGE_NAME}/{build_version}/{wheel_filename}"
        print(f"Whl is wheel_url: {wheel_url}")
        g_wheel_result = WheelResult(url=wheel_url, filename=wheel_filename, build_version=build_version)
        return wheel_url, wheel_filename


def download_file(file_url, filename, temp_file_path=None):
    """
    Download a file via wget using preset credentials. Mainly used to fetch wheel
    packages or other configuration files from the artifact repository (Artifactory).

    Args:
        file_url: Full URL of the file to download.
        filename: File name to save after the download.
        temp_file_path: Optional temporary directory path. When provided, the file
                        is saved under that directory.

    Returns:
        str: Local absolute path of the downloaded file.

    Raises:
        SystemExit: The program exits when the download fails and it is not a
                    temporary file download.
    """
    file_path = os.path.abspath(filename)
    if temp_file_path is not None:
        file_path = os.path.join(temp_file_path, filename)
    else:
        print(f"Guessing wheel URL: {file_url}")
    output = []
    try:
        cmd = f"wget -O {file_path} {file_url}"
        ret = run_cmd(cmd)
        output = ret.stdout.splitlines() + ret.stderr.splitlines()
        if ret.returncode == 0:
            if os.path.exists(file_path):
                return file_path
            else:
                print("File not found: " + str(file_path))
                print("Unknown Error! Please check environment or contact FlyTiger IT Team.")
                for line in output:
                    print(line)
                exit(1)
        else:
            print(f"Attempt to download the file: {file_url} failed.")
            for line in output:
                print(line)
            if temp_file_path is not None:
                return file_path
            exit(1)
    except Exception as e:
        print(f"An error occurred in download_file: {e}")
        exit(-1)


def extract_metadata_from_wheel(wheel_file):
    """
    Extract the content of the METADATA file from a wheel package.

    A wheel package is essentially a ZIP archive containing a METADATA file
    that records package metadata such as name, version, and dependencies.
    This function opens the wheel package, locates the METADATA file, and
    reads its content.

    Args:
        wheel_file: Local path of the wheel package file.

    Returns:
        str: Full content of the METADATA file. Returns an empty string when
             the METADATA file is not found or an error occurs.
    """
    try:
        with zipfile.ZipFile(wheel_file, 'r') as zip_ref:
            metadata_filename = next((name for name in zip_ref.namelist() if name.endswith('METADATA')), None)
            if not metadata_filename:
                print("METADATA file not found in the wheel package.")
                return ""
            with zip_ref.open(metadata_filename) as metadata_file:
                metadata_content = metadata_file.read().decode('utf-8')
        return metadata_content
    except Exception as e:
        print(f"Error for extract wheel metadata: {e}")
        return ""


def marker_operand_to_str(node):
    """
    Restore a single operand (Variable/Op/Value) of a Marker triple into a PEP 508 fragment.

    Whether quoting is needed is determined by the node type, not by position:
    PEP 508 allows a literal on the left side (e.g. "arm" not in platform_machine),
    in which case the triple is (Value, Op, Variable).
    packaging nodes provide serialize(): Variable outputs the variable name and
    Value outputs a quoted string literal; reusing them directly avoids restoring
    an invalid marker.

    Args:
        node: A Variable, Op, or Value node from the Marker._markers triple.

    Returns:
        str: The restored PEP 508 fragment.
    """
    serialize = getattr(node, "serialize", None)
    if callable(serialize):
        return serialize()
    return str(node)


def marker_node_to_str(node):
    """
    Restore a single node of the Marker expression tree into a PEP 508 condition string.

    A node can be a (Variable, Op, Value) triple (e.g. python_version < "3.10")
    or a nested list representing a parenthesized group; nested lists are
    restored recursively and wrapped in parentheses.

    Args:
        node: A single node from Marker._markers.

    Returns:
        str: The restored condition string.
    """
    if isinstance(node, tuple) and len(node) == 3:
        return " ".join(marker_operand_to_str(item) for item in node)
    if isinstance(node, list):
        return "(" + " ".join(
            item if isinstance(item, str) else marker_node_to_str(item)
            for item in node
        ) + ")"
    return str(node)


def split_extra_marker(marker):
    """
    Separate the extra name from the residual environment condition in a PEP 508 marker.

    Marker._markers is an expression tree composed of (Variable, Op, Value)
    triples, "and"/"or" connectors, and nested lists for parenthesized groups;
    this structure has been stable since packaging 16.1.

    In METADATA produced by standard packaging tools, the extra clause is always
    at the top level and joined by "and" (e.g. python_version < "3.11" and extra == 'dev'),
    so it can be split safely; complex combinations containing a top-level "or"
    are conservatively treated as a whole as non-extra conditions and not split.

    Args:
        marker: A packaging.markers.Marker object.

    Returns:
        tuple: (extra_name, residual_condition).
               extra_name is the extra name (e.g. "dev"), or None when there is
               no extra clause; residual_condition is the remaining condition
               string, or None when nothing remains.
    """
    nodes = marker._markers
    if any(node == "or" for node in nodes):
        return None, str(marker)
    extra_name = None
    residual_parts = []
    for node in nodes:
        if (isinstance(node, tuple) and len(node) == 3
                and str(node[0]) == "extra" and str(node[1]) == "=="):
            extra_name = str(node[2])
        elif node == "and":
            continue
        else:
            residual_parts.append(marker_node_to_str(node))
    residual_condition = " and ".join(residual_parts) if residual_parts else None
    return extra_name, residual_condition


def parse_requires_dist(metadata_content):
    """
    Parse the Requires-Dist fields of the METADATA file and extract dependencies.

    The Requires-Dist fields in METADATA list all dependencies of the package.
    Each line is first validated against PEP 508 with packaging.requirements.Requirement
    (invalid lines are warned about and skipped, so dirty data cannot reach setup()
    and break the build), then classified by whether the environment marker
    contains an extra clause:
      - No marker or only plain environment markers (e.g. platform_machine, python_version):
        added to the install_requires list with the condition kept as-is for pip to
        evaluate at install time.
      - Contains an extra clause: added to the extras_require dict grouped by extra
        name; conditions combined with the extra (e.g. python_version < "3.11") are
        kept in the dependency entry.

    Dependency text keeps the original METADATA form (not rebuilt from Requirement)
    to avoid SpecifierSet normalization reordering version constraints.

    METADATA is RFC 822 format: header fields come before the first blank line,
    followed by the package description body (long_description). Only the header
    field area is parsed and field names must start at the beginning of a line,
    so text like "Requires-Dist: xxx" inside the body (e.g. sample code in the
    README) is not mistaken for a real dependency.

    Args:
        metadata_content: Full content string of the METADATA file.

    Returns:
        tuple: (install_requires, extras_require).
               install_requires is a list of dependencies (possibly with environment
               markers); extras_require is a dict of conditional dependencies
               grouped by extra name.
    """
    try:
        install_requires = []
        extras_require = {}
        field_prefix = "Requires-Dist:"
        for raw_line in metadata_content.splitlines():
            # Everything after the first blank line is the description body;
            # stop parsing fields at a blank line.
            # In legacy METADATA, a body folded into the Description field encodes
            # its blank lines as 8 spaces instead of truly empty lines, so header
            # fields are never truncated early here.
            if raw_line == "":
                break
            # Match must start at the beginning of the line: occurrences inside a
            # line are not fields, and indented lines are continuations of the
            # previous field in RFC 822, so they are not new Requires-Dist fields.
            if not raw_line.startswith(field_prefix):
                continue
            line = raw_line[len(field_prefix):].strip()
            if not line:
                continue
            # Validate the whole line against PEP 508 and parse the marker
            # with packaging.requirements
            try:
                requirement = Requirement(line)
            except InvalidRequirement as req_err:
                print(f"Skip invalid Requires-Dist line '{line}': {req_err}")
                continue
            dependency = line.split(';', 1)[0].strip()
            if requirement.marker is None:
                install_requires.append(dependency)
                continue
            extra_name, residual_condition = split_extra_marker(requirement.marker)
            if extra_name is not None:
                if extra_name not in extras_require:
                    extras_require[extra_name] = []
                # extras_require values also support environment markers; keep the
                # residual conditions combined with the extra (e.g. python_version < "3.11")
                if residual_condition:
                    extras_require[extra_name].append(f"{dependency}; {residual_condition}")
                else:
                    extras_require[extra_name].append(dependency)
            else:
                # Non-extra environment markers (e.g. platform_machine, python_version):
                # keep the condition as-is for pip to evaluate against the current
                # environment at install time.
                condition = line.split(';', 1)[1].strip()
                install_requires.append(f"{dependency}; {condition}")
        return install_requires, extras_require
    except Exception as e:
        print(f"Error for parse requires dist: {e}")
        return [], {}


class DisableInstallCommand(_install):
    """
    Custom install command class that disables direct installation from this PyPI source.

    Inherits from the setuptools install command and overrides run().
    When a user tries to install this package directly via pip install, this
    command is triggered, printing a hint and exiting. This design prevents
    users from installing incompatible prebuilt packages from PyPI and ensures
    the correct installation method is used.
    """
    def run(self):
        print("Custom installation from this pypi source is disabled.")
        print("PLEASE try cloning from github and run python setup.py install.")
        exit(-1)


class CloneInstallCommand(_install):
    """
    Custom install command class that disables the clone-based installation method.

    Inherits from the setuptools install command and overrides run().
    The current implementation exits directly, indicating that clone-based
    installation has been disabled.
    """
    def run(self):
        print("Clone from github is disabled.")
        exit(-1)


class CachedWheelsCommand(_bdist_wheel):
    """
    Custom bdist_wheel command class that uses a prebuilt wheel package.

    Inherits from wheel's bdist_wheel command and overrides run().
    This command is triggered when pip cannot find an existing wheel package.
    Instead of performing an actual compilation build, this implementation
    renames the pre-downloaded wheel to the file name pip expects, thereby
    bypassing the build process.

    Using prebuilt binaries avoids time-consuming compilation on the user's
    machine while ensuring the package is fully compatible with the current environment.
    """
    def run(self):
        """
        Called when the wheel build command runs.

        Rename the pre-downloaded wheel stored in the global variable g_wheel_result
        to the standard file name pip expects and save it to the dist directory.

        Raises:
            SystemExit: The program exits when the rename operation fails.
        """
        try:
            # Make the archive
            # Lifted from the root wheel processing command
            # https://github.com/pypa/wheel/blob/cf71108ff9f6ffc36978069acb28824b44ae028e/src/wheel/bdist_wheel.py#LL381C9-L381C85
            if not os.path.exists(self.dist_dir):
                os.makedirs(self.dist_dir)

            impl_tag, abi_tag, plat_tag = self.get_tag()
            archive_basename = f"{self.wheel_dist_name}-{impl_tag}-{abi_tag}-{plat_tag}"

            wheel_path = os.path.join(self.dist_dir, archive_basename + ".whl")
            print("Raw wheel path", wheel_path)
            if g_wheel_result and g_wheel_result.filename:
                os.rename(g_wheel_result.filename, wheel_path)
        except Exception as e:
            print(f"An error occurred: {e}")
            exit(-1)


if __name__ == "__main__":
    install_requires, extras_require = [], {}
    extra_args = {}
    if os.getenv("__FAKE_BUILD") != "FAKE_WHEEL":
        wheel_url, wheel_filename = get_wheel_url()
        best_matching_build_version = g_wheel_result.build_version if g_wheel_result else None
        if best_matching_build_version == build_version:
            file_path = download_file(wheel_url, wheel_filename)
            metadata = extract_metadata_from_wheel(file_path)
            install_requires, extras_require = parse_requires_dist(metadata)
        else:
            if best_matching_build_version != "" and best_matching_build_version is not None:
                extra_args["python_requires"] = ">9.9"
    else:
        extra_args["python_requires"] = ">=3.8"
    install_requires += ['numpy', 'packaging']

    setuptools.setup(
        name=PACKAGE_NAME,
        description=PACKAGE_NAME,
        version=build_version,
        install_requires=install_requires,
        extras_require=extras_require,
        **extra_args,
        packages=setuptools.find_packages(exclude=("tests*", "benchmarks*")),
        cmdclass={
            "bdist_wheel": CachedWheelsCommand,
            "install": DisableInstallCommand if "FlyTiger_Internal" not in os.environ.keys() else CloneInstallCommand
        },
        long_description_content_type="text/markdown",
        classifiers=[
            "Programming Language :: Python :: 3.8",
            "Programming Language :: Python :: 3.9",
            "Programming Language :: Python :: 3.10",
            "Programming Language :: Python :: 3.11",
            "License :: OSI Approved :: Apache Software License",
            "Topic :: Scientific/Engineering :: Artificial Intelligence",
        ],
        zip_safe=False,
    )
