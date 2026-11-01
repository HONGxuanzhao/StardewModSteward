"""Build a Windows folder release and ZIP from a checkout. Never include user settings."""
from pathlib import Path
import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def collect_licenses(destination):
    destination.mkdir(parents=True, exist_ok=True)
    names = ["PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "requests", "urllib3",
             "certifi", "charset_normalizer", "idna", "cryptography", "cffi", "chardet", "PyInstaller"]
    for name in names:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for member in dist.files or []:
            if not any(s in str(member).lower() for s in ("license", "copying", "lgpl")):
                continue
            source = Path(dist.locate_file(member))
            if source.is_file():
                target = destination / f"{name}-{dist.version}" / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    # Conda packages can store their license texts outside Python's dist-info metadata.
    prefix = Path(sys.prefix)
    for record in (prefix / "conda-meta").glob("*.json"):
        package = json.loads(record.read_text(encoding="utf-8"))
        folder = f"{package['name']}-{package['version']}-{package['build']}"
        source = prefix / "pkgs" / folder / "info" / "licenses"
        relevant = {"python", "pyside6", "qtbase", "qtsvg", "qtdeclarative", "openssl", "zlib", "libzlib",
                    "libffi", "icu", "freetype", "harfbuzz", "graphite2", "glib", "libglib", "gettext",
                    "libiconv", "pcre2", "libpng", "jpeg", "bzip2", "xz", "libmpdec", "expat", "zstd"}
        if package.get("name") in relevant and source.is_dir():
            shutil.copytree(source, destination / folder, dirs_exist_ok=True)
    for source in (prefix / "LICENSE_PYTHON.txt", prefix / "LICENSE.txt"):
        if source.is_file():
            shutil.copy2(source, destination / ("Python-" + source.name))
    if os.environ.get("ISCC"):
        license_path = Path(os.environ["ISCC"]).parent / "license.txt"
        if license_path.is_file():
            folder = destination / "InnoSetup"
            folder.mkdir(exist_ok=True)
            shutil.copy2(license_path, folder / "license.txt")


def main():
    if os.name != "nt":
        raise SystemExit("请在 Windows x64 上构建此桌面程序。")
    sys.path.insert(0, str(ROOT))
    from core import APP_VERSION
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
               "--name", "星露谷MOD管家", "--icon", str(ROOT / "app.ico"),
               "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
               "--specpath", str(ROOT / "build")]
    for name in ("tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL", "PySide6.QtWebEngineCore",
                 "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick"):
        command += ["--exclude-module", name]
    command.append(str(ROOT / "app.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    package = ROOT / "dist" / "星露谷MOD管家"
    for filename in ("README.md", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md"):
        shutil.copy2(ROOT / filename, package / filename)
    shutil.copytree(ROOT / "docs", package / "docs", dirs_exist_ok=True)
    collect_licenses(package / "第三方许可")
    information = dict(version=APP_VERSION, sources={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                       for name in ("app.py", "core.py")},
                       exe_sha256=hashlib.sha256((package / "星露谷MOD管家.exe").read_bytes()).hexdigest())
    (package / "BUILD_INFO.json").write_text(json.dumps(information, indent=2), encoding="utf-8")
    archive = ROOT / "dist" / f"StardewModSteward-v{APP_VERSION}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for source in sorted(package.rglob("*")):
            if source.is_file():
                output.write(source, "StardewModSteward/" + source.relative_to(package).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (archive.parent / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"已生成：{archive}\nSHA256: {digest}")


if __name__ == "__main__":
    main()
