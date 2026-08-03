"""Install the official Fubon Neo 2.2.8 binary for this platform."""

from __future__ import annotations

import io
import platform
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional


VERSION = "2.2.8"
BASE_URL = "https://www.fbs.com.tw/TradeAPI_SDK/fubon_binary"
PACKAGES = {
    ("Darwin", "arm64"): f"fubon_neo-{VERSION}-cp37-abi3-macosx_11_0_arm64.zip",
    ("Darwin", "x86_64"): f"fubon_neo-{VERSION}-cp37-abi3-macosx_10_12_x86_64.zip",
    ("Windows", "AMD64"): f"fubon_neo-{VERSION}-cp37-abi3-win_amd64.zip",
    ("Linux", "x86_64"): (
        f"fubon_neo-{VERSION}-cp37-abi3-manylinux_2_17_x86_64."
        "manylinux2014_x86_64.zip"
    ),
}


def installed_version() -> Optional[str]:
    try:
        import importlib.metadata

        return importlib.metadata.version("fubon-neo")
    except Exception:
        return None


def main() -> int:
    current = installed_version()
    if current == VERSION:
        print(f"[富邦 SDK] 已安裝 {VERSION}")
        return 0

    key = (platform.system(), platform.machine())
    archive_name = PACKAGES.get(key)
    if not archive_name:
        print(f"[富邦 SDK] 不支援的平台：{key[0]} {key[1]}", file=sys.stderr)
        return 1

    url = f"{BASE_URL}/{archive_name}"
    print(f"[富邦 SDK] 下載官方 SDK {VERSION}：{key[0]} {key[1]}")
    request = urllib.request.Request(url, headers={"User-Agent": "TXF-Pro-Viewer/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            archive = response.read()
    except Exception as exc:
        print(f"[富邦 SDK] 下載失敗：{type(exc).__name__}", file=sys.stderr)
        return 1

    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            wheels = [name for name in bundle.namelist() if name.endswith(".whl")]
            if len(wheels) != 1 or Path(wheels[0]).name != wheels[0]:
                raise ValueError("官方壓縮檔中的 wheel 結構不符預期")
            wheel_bytes = bundle.read(wheels[0])
            wheel_name = wheels[0]
    except Exception as exc:
        print(f"[富邦 SDK] 壓縮檔驗證失敗：{exc}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="fubon-sdk-") as tmp:
        wheel_path = Path(tmp) / wheel_name
        wheel_path.write_bytes(wheel_bytes)
        command = [sys.executable, "-m", "pip", "install"]
        if sys.prefix == sys.base_prefix:
            command.append("--user")
        command.append(str(wheel_path))
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            return result.returncode

    installed = installed_version()
    if installed != VERSION:
        print(
            f"[富邦 SDK] 安裝後版本不符：{installed or '未偵測到'}",
            file=sys.stderr,
        )
        return 1
    print(f"[富邦 SDK] 安裝完成：{installed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
