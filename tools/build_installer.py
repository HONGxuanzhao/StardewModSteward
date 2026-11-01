"""Build the portable release and a per-user Windows EXE installer with Inno Setup 7."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import APP_VERSION


def compiler_path(override=None):
    candidates = [override, os.environ.get("ISCC"), shutil.which("ISCC.exe")]
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(variable):
            base = Path(os.environ[variable])
            candidates.extend([base / "Inno Setup 7" / "ISCC.exe", base / "Programs" / "Inno Setup 7" / "ISCC.exe"])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise SystemExit("未找到 Inno Setup 7。请先安装，或用 --iscc / ISCC 环境变量指定 ISCC.exe。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iscc", help="Path to the Inno Setup 7 compiler")
    parser.add_argument("--skip-build", action="store_true", help="Reuse a verified, current folder build")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("请在 Windows x64 上构建安装包。")
    compiler = compiler_path(args.iscc)
    environment = dict(os.environ, ISCC=str(compiler))
    if not args.skip_build:
        subprocess.run([sys.executable, str(ROOT / "tools" / "build_windows.py")], cwd=ROOT, env=environment, check=True)
    package = ROOT / "dist" / "星露谷MOD管家"
    info_path = package / "BUILD_INFO.json"
    if not info_path.is_file():
        raise SystemExit("缺少构建信息，请不带 --skip-build 重新构建。")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    expected = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in ("app.py", "core.py")}
    executable = package / "星露谷MOD管家.exe"
    if info.get("version") != APP_VERSION or info.get("sources") != expected or info.get("exe_sha256") != hashlib.sha256(executable.read_bytes()).hexdigest():
        raise SystemExit("已有程序与当前源码或版本不符，请不带 --skip-build 重新构建。")
    subprocess.run([str(compiler), "/DAppVersion=" + APP_VERSION, str(ROOT / "tools" / "windows_installer.iss")],
                   cwd=ROOT, check=True)
    installer = ROOT / "dist" / f"StardewModSteward-v{APP_VERSION}-windows-x64-Setup.exe"
    archive = ROOT / "dist" / f"StardewModSteward-v{APP_VERSION}-windows-x64.zip"
    sums = "".join(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}\n" for file in (archive, installer))
    (ROOT / "dist" / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
    print("已生成 EXE 安装包：" + str(installer))
    print(sums.strip())


if __name__ == "__main__":
    main()
