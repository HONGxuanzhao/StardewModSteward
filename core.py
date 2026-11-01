"""Stardew Mod Steward: inventory, public update checks and reversible installs."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from collections import Counter
from contextlib import contextmanager

import requests

APP_VERSION = "1.3.1"
APP_MUTEX = r"Local\StardewModSteward.Desktop"
CACHE_SCHEMA = 2
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "StardewModSteward"
HTTP_HEADERS = {"User-Agent": f"StardewModSteward/{APP_VERSION}", "Accept": "application/json"}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ModError(Exception):
    pass


def parse_vdf(text):
    """Parse Valve's quoted KeyValues format, including nested modern Steam libraries."""
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[{}]|//[^\r\n]*', text)
    tokens = [t for t in tokens if not t.startswith("//")]
    position = 0
    def take():
        nonlocal position
        if position >= len(tokens):
            raise ValueError("Incomplete Valve KeyValues file")
        result = tokens[position]
        position += 1
        return result
    def unquote(token):
        if not token.startswith('"'):
            raise ValueError("Invalid Valve KeyValues token")
        return token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    def object_(nested=False):
        result = {}
        while position < len(tokens):
            token = take()
            if token == "}":
                if not nested:
                    raise ValueError("Unexpected closing brace")
                return result
            key = unquote(token)
            value = take()
            result[key.casefold()] = object_(True) if value == "{" else unquote(value)
        if nested:
            raise ValueError("Missing closing brace")
        return result
    return object_()


def steam_registry_roots():
    result = []
    if os.name == "nt":
        import winreg
        for hive, subkey, name in [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")]:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    result.append(Path(winreg.QueryValueEx(key, name)[0]))
            except OSError:
                pass
    return result


def registered_game_paths():
    """Read install locations registered by Steam/GOG; do not walk whole disks."""
    result = []
    if os.name != "nt":
        return result
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for subkey in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                       r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(hive, subkey) as parent:
                    for i in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as child:
                                name = winreg.QueryValueEx(child, "DisplayName")[0]
                                if "stardew valley" in str(name).casefold():
                                    result.append(Path(winreg.QueryValueEx(child, "InstallLocation")[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    return result


def normalize_game_path(value):
    path = Path(value).expanduser().absolute()
    if path.name.casefold() == "mods" or path.suffix.casefold() == ".exe":
        path = path.parent
    return path


def discover_games(extra_paths=(), steam_roots=None, include_system=True):
    """Return validated SMAPI game + Mods paths, ordered by explicit choice then evidence."""
    found = {}
    def add(path, source, priority, played=0):
        try:
            if not str(path).strip():
                return
            path = normalize_game_path(path)
            if not (path / "StardewModdingAPI.exe").is_file() or not (path / "Mods").is_dir():
                return
            key = str(path).casefold()
            row = found.setdefault(key, dict(game=str(path), mods=str(path / "Mods"), sources=[], priority=0,
                                             last_played=0))
            if source not in row["sources"]:
                row["sources"].append(source)
            row["priority"] = max(priority, row["priority"])
            row["last_played"] = max(int(played), row["last_played"])
        except (OSError, ValueError, TypeError):
            pass
    for path in extra_paths:
        if path:
            add(path, "已选目录", 1000)
    roots = list(steam_roots if steam_roots is not None else steam_registry_roots())
    if include_system:
        cwd = Path.cwd()
        for parent in [cwd, *list(cwd.parents)[:5]]:
            add(parent, "程序附近目录", 850)
        logfile = Path(os.environ.get("APPDATA", str(Path.home()))) / "StardewValley" / "ErrorLogs" / "SMAPI-latest.txt"
        try:
            if time.time() - logfile.stat().st_mtime < 180 * 86400:
                for line in logfile.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:50]:
                    match = re.search(r"Mods go here:\s*(.+)", line)
                    if match:
                        add(match[1].strip(), "最近 SMAPI 日志", 750)
        except OSError:
            pass
        for path in registered_game_paths():
            add(path, "安装注册表", 300)
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                roots.append(Path(os.environ[env]) / "Steam")
                add(Path(os.environ[env]) / "GOG Galaxy" / "Games" / "Stardew Valley", "常见 GOG 目录", 100)
        if os.name == "nt":
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                drive = f"{chr(65 + i)}:\\"
                if mask & (1 << i) and ctypes.windll.kernel32.GetDriveTypeW(drive) == 3:
                    roots.extend(Path(drive) / name for name in ("Steam", "SteamLibrary", "Games/Steam"))
                    add(Path(drive) / "GOG Games" / "Stardew Valley", "常见 GOG 目录", 100)
                    add(Path(drive) / "XboxGames" / "Stardew Valley" / "Content", "常见 Xbox 目录", 100)
    libraries = {}
    for root in roots:
        root = Path(root).absolute()
        libraries[str(root).casefold()] = root
        for filename in (root / "steamapps" / "libraryfolders.vdf", root / "config" / "libraryfolders.vdf"):
            try:
                data = parse_vdf(filename.read_text(encoding="utf-8-sig"))
                for index, value in data.get("libraryfolders", {}).items():
                    if not index.isdigit():
                        continue
                    path = value.get("path") if isinstance(value, dict) else value
                    if path:
                        library = Path(path).absolute()
                        libraries[str(library).casefold()] = library
            except (OSError, ValueError):
                pass
    for library in libraries.values():
        steamapps = library / "steamapps"
        try:
            data = parse_vdf((steamapps / "appmanifest_413150.acf").read_text(encoding="utf-8-sig"))
            app = data.get("appstate", {})
            directory = app.get("installdir", "Stardew Valley")
            # App manifests are data: don't accept an absolute/parent path from them.
            if Path(directory).name == directory and directory not in (".", ".."):
                add(steamapps / "common" / directory, "Steam 库 / 游戏清单", 400, app.get("lastplayed", 0))
        except (OSError, ValueError):
            pass
        add(steamapps / "common" / "Stardew Valley", "Steam 库", 350)
    return sorted(found.values(), key=lambda r: (-r["priority"], -r["last_played"], r["game"].casefold()))


# Backward-compatible default for scripts; discovered from this machine on every launch.
DEFAULT_GAME = next((row["game"] for row in discover_games()), "")


def validated_update_keys(keys):
    """Keep only actual update sources, preserving subkeys and tolerating surrounding spaces."""
    valid, rejected = [], []
    sites = {"nexus": "Nexus", "moddrop": "ModDrop", "curseforge": "CurseForge", "chucklefish": "Chucklefish"}
    for raw in keys:
        raw = str(raw).strip()
        site, colon, ident = raw.partition(":")
        site, ident = site.strip(), ident.strip()
        base, at, subkey = ident.partition("@")
        base = base.strip()
        good = None
        if colon and site.casefold() in sites and re.fullmatch(r"[1-9]\d*", base) and (not at or subkey.strip()):
            good = sites[site.casefold()] + ":" + base + ("@" + subkey.strip() if at else "")
        elif colon and site.casefold() == "github" and re.fullmatch(r"[\w.-]+/[\w.-]+", base) and (not at or subkey.strip()):
            good = "GitHub:" + base + ("@" + subkey.strip() if at else "")
        if good:
            if good.casefold() not in {v.casefold() for v in valid}:
                valid.append(good)
        elif raw:
            rejected.append(raw)
    return valid, rejected


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_jsonc(path: Path):
    """Strip comments and trailing commas without changing string literals."""
    s = path.read_text(encoding="utf-8-sig")
    token = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
    s = token.sub(lambda m: m[0] if m[0].startswith('"') else " ", s)
    s = re.sub(r'("(?:\\.|[^"\\])*")|,(\s*[}\]])',
               lambda m: m[1] if m[1] is not None else m[2], s)
    return json.loads(s)


def linked(path: Path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def checked_path(root: Path, path: Path, allow_root=False):
    root = root.absolute()
    path = path.absolute()
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise ModError(f"路径越界：{path}")
    if ".." in rel.parts or (not rel.parts and not allow_root):
        raise ModError(f"不允许操作此路径：{path}")
    for p in [root, *[root.joinpath(*rel.parts[:i]) for i in range(1, len(rel.parts) + 1)]]:
        if p.exists() and linked(p):
            raise ModError(f"为防止修改目录外文件，已跳过链接或联接目录：{p}")
    return path


def safe_remove(root: Path, target: Path):
    checked_path(root, target)
    if target.exists():
        shutil.rmtree(target)


def protect_key(value: str, decrypt=False):
    if not value:
        return ""
    if os.name != "nt":
        raise ModError("密钥保护仅支持 Windows。")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_char))]

    raw = base64.b64decode(value) if decrypt else value.encode("utf-8")
    buf = ctypes.create_string_buffer(raw)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    # CRYPTPROTECT_UI_FORBIDDEN; DPAPI is scoped to the current Windows user.
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise ModError("Windows 无法保护或读取 Nexus 密钥。请重新输入密钥。")
    try:
        result = ctypes.string_at(dst.data, dst.size)
        return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(dst.data)


class Settings:
    def __init__(self, data=DATA):
        self.data = Path(data)
        self.path = self.data / "settings.json"
        self.values = dict(game="", auto_check=True, auto_update=False, preserve_config=True)
        if self.path.exists():
            self.values.update(json.loads(self.path.read_text(encoding="utf-8")))
        candidates = discover_games([self.values.get("game", "")])
        if candidates:
            self.values["game"] = candidates[0]["game"]
            self.values["game_source"] = " / ".join(candidates[0]["sources"])
        else:
            self.values["game_source"] = "未找到安装，请手动选择"

    def save(self):
        write_json(self.path, self.values)

    def key(self):
        return protect_key(self.values.get("nexus_key", ""), decrypt=True)

    def set_key(self, key):
        self.values["nexus_key"] = protect_key(key.strip())


class ModGroups:
    """Per-installation, UID-based collections. Saving a group never moves a MOD."""
    def __init__(self, game, data=DATA):
        self.game = str(validate_game(game))
        self.path = Path(data) / "groups.json"
        self.key = os.path.normcase(os.path.abspath(self.game))
        self.document = {"schema": 1, "games": {}}
        try:
            checked_path(self.path.parent, self.path)
            if self.path.exists():
                self.document = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(self.document, dict) or self.document.get("schema") != 1 or not isinstance(self.document.get("games"), dict):
                raise ValueError()
            self.record = self.document["games"].setdefault(self.key, {"game": self.game, "groups": []})
            if not isinstance(self.record, dict) or not isinstance(self.record.get("groups"), list):
                raise ValueError()
            names, identifiers = set(), set()
            for group in self.record["groups"]:
                if not isinstance(group, dict) or not isinstance(group.get("id"), str) or not group["id"]:
                    raise ValueError()
                name = self.valid_name(group.get("name"))
                members = group.get("members")
                if group["id"] in identifiers or name.casefold() in names or not isinstance(members, list):
                    raise ValueError()
                self.valid_members(members)
                names.add(name.casefold())
                identifiers.add(group["id"])
        except (OSError, ValueError, KeyError, TypeError, ModError) as exc:
            raise ModError("无法读取自定义编组，原文件未改动。请核对或从备份恢复：" + str(self.path)) from exc

    @staticmethod
    def valid_name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
            raise ModError("编组名称需为 1–60 个字符。")
        return name.strip()

    @staticmethod
    def valid_members(members):
        if isinstance(members, (str, bytes)) or not isinstance(members, (list, tuple, set)):
            raise ModError("编组成员格式无效。")
        result, seen = [], set()
        for uid in members:
            if not isinstance(uid, str) or not uid.strip() or len(uid) > 256:
                raise ModError("编组包含无效的 MOD ID。")
            uid = uid.strip()
            if uid.casefold() not in seen:
                result.append(uid)
                seen.add(uid.casefold())
        return result

    @property
    def groups(self):
        return self.record["groups"]

    def get(self, identifier):
        return next((group for group in self.groups if group["id"] == identifier), None)

    def save(self, name, members, identifier=None):
        name, members = self.valid_name(name), self.valid_members(members)
        if any(g["name"].casefold() == name.casefold() and g["id"] != identifier for g in self.groups):
            raise ModError("已有同名编组，请使用不同名称。")
        if identifier is not None and self.get(identifier) is None:
            raise ModError("此编组已不存在，请刷新后重试。")
        group = dict(id=identifier or uuid.uuid4().hex, name=name, members=members)
        previous = list(self.groups)
        self.record["groups"] = [group if g["id"] == identifier else g for g in previous]
        if identifier is None:
            self.record["groups"].append(group)
        try:
            self._write()
        except BaseException:
            self.record["groups"] = previous
            raise
        return group

    def delete(self, identifier):
        if self.get(identifier) is None:
            raise ModError("此编组已不存在。")
        previous = list(self.groups)
        self.record["groups"] = [g for g in previous if g["id"] != identifier]
        try:
            self._write()
        except BaseException:
            self.record["groups"] = previous
            raise

    def _write(self):
        # Preserve the preceding group document for recoverable metadata edits.
        checked_path(self.path.parent, self.path)
        if self.path.exists():
            backup = self.path.with_name("groups.previous.json")
            checked_path(self.path.parent, backup)
            checked_path(self.path.parent, self.path)
            current = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(current, dict) or current.get("schema") != 1 or not isinstance(current.get("games"), dict):
                raise ModError("编组文件在编辑期间发生变化，请重新打开。")
            write_json(backup, current)
            current["games"][self.key] = self.record
            self.document = current
        write_json(self.path, self.document)


@dataclass
class Mod:
    name: str
    uid: str
    version: str
    author: str
    description: str
    path: Path
    manifest: dict
    disabled: bool = False
    issues: list[str] = field(default_factory=list)
    update: dict = field(default_factory=dict)

    @property
    def keys(self):
        return [x for x in self.manifest.get("UpdateKeys", []) if isinstance(x, str) and x.strip()]

    @property
    def valid_keys(self):
        return validated_update_keys(self.keys)[0]

    @property
    def requires_attention(self):
        return bool(self.issues or self.update.get("errors") or self.compatibility in ("Abandoned", "Obsolete", "Broken")
                    or (self.compatibility == "Unofficial" and not self.has_unofficial_fix)
                    or self.update.get("bundle", {}).get("mode") == "web")

    @property
    def compatibility(self):
        return (self.update.get("metadata") or {}).get("compatibilityStatus", "")

    @property
    def has_unofficial_fix(self):
        version = ((self.update.get("metadata") or {}).get("unofficial") or {}).get("version")
        return bool(version and self.compatibility == "Unofficial" and compare_versions(self.version, version) == 0)

    @property
    def nexus_id(self):
        suggested = self.update.get("suggestedUpdate") or {}
        match = re.search(r"nexusmods\.com/stardewvalley/mods/(\d+)", suggested.get("url", ""), re.I)
        if match:
            return int(match[1])
        mid = (self.update.get("metadata") or {}).get("nexusID")
        if mid:
            return int(mid)
        for key in self.valid_keys:
            match = re.fullmatch(r"Nexus:(\d+)(?:@.*)?", key, re.I)
            if match:
                return int(match[1])
        return None

    @property
    def suggestion(self):
        return (self.update.get("suggestedUpdate") or {}).get("version", "")

    @property
    def url(self):
        url = (self.update.get("suggestedUpdate") or {}).get("url", "")
        if url:
            return url
        if self.update.get("bundle"):
            return self.update["bundle"].get("url", "")
        meta = self.update.get("metadata") or {}
        for field in ("main", "unofficial"):
            if isinstance(meta.get(field), dict) and meta[field].get("url"):
                return meta[field]["url"]
        return f"https://www.nexusmods.com/stardewvalley/mods/{self.nexus_id}" if self.nexus_id else ""

    @property
    def source_label(self):
        if self.update.get("managedBySmapi"):
            return "SMAPI"
        from urllib.parse import urlparse
        host = urlparse(self.url).hostname or ""
        for domain, name in [("nexusmods.com", "Nexus"), ("moddrop.com", "ModDrop"),
                             ("github.com", "GitHub"), ("curseforge.com", "CurseForge"), ("smapi.io", "SMAPI")]:
            if host == domain or host.endswith("." + domain):
                return name
        return self.valid_keys[0].split(":", 1)[0] if self.valid_keys else "—"

    @property
    def status(self):
        if self.disabled:
            return "已禁用"
        if self.issues:
            return "需要检查"
        if self.compatibility in ("Abandoned", "Obsolete"):
            return "停止维护" if self.compatibility == "Abandoned" else "已被替代"
        if self.compatibility in ("Broken", "Unofficial"):
            return "已装修复版" if self.has_unofficial_fix else "兼容性提示"
        if self.suggestion:
            return "可更新"
        if self.update.get("errors"):
            return "检查失败"
        if self.update.get("bundle"):
            return "需网页核对" if self.update["bundle"].get("mode") == "web" else "随主包检查"
        if self.update.get("managedBySmapi"):
            return "随 SMAPI 更新"
        if self.update:
            meta = self.update.get("metadata") or {}
            if meta.get("main"):
                return "无推荐更新"
            return "无独立更新源"
        return "待检查" if self.valid_keys else "无独立更新源"


def mod_from_manifest(path, disabled=False):
    m = read_jsonc(path)
    # SMAPI's JSON fields are case insensitive (its bundled mods use UniqueId).
    canonical = {x.casefold(): x for x in ("Name", "Author", "Version", "Description", "UniqueID",
                 "EntryDll", "MinimumApiVersion", "UpdateKeys", "Dependencies", "ContentPackFor",
                 "IsRequired", "MinimumVersion")}
    def normalize(value):
        if isinstance(value, dict):
            return {canonical.get(k.casefold(), k): normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value
    m = normalize(m)
    if not isinstance(m, dict) or not m.get("UniqueID") or not m.get("Version") or not m.get("Name"):
        raise ModError("清单缺少 Name / UniqueID / Version。")
    if not isinstance(m["UniqueID"], str) or not isinstance(m["Version"], (str, int, float)):
        raise ModError("清单 ID 或版本格式无效。")
    return Mod(str(m["Name"]), m["UniqueID"], str(m["Version"]), str(m.get("Author", "")),
               str(m.get("Description", "")), path.parent, m, disabled)


def walk_manifests(root, errors=None):
    if not root.exists():
        return
    if linked(root):
        raise ModError(f"目录是链接或联接：{root}")
    def onerror(e):
        if errors is not None:
            errors.append(str(e))
        else:
            raise e
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        clean = []
        for d in dirs:
            p = Path(folder) / d
            if linked(p):
                if errors is not None:
                    errors.append(f"已跳过链接：{p}")
            elif d not in (".git", "__MACOSX"):
                clean.append(d)
        dirs[:] = clean
        for name in files:
            if name.lower() == "manifest.json":
                p = Path(folder) / name
                if linked(p):
                    raise ModError(f"清单是链接：{p}")
                yield p


def validate_game(game):
    if not str(game).strip():
        raise ModError("未找到星露谷，请在设置中自动探测或选择游戏 / Mods 目录。")
    game = normalize_game_path(game)
    if not (game / "StardewModdingAPI.exe").is_file() or not (game / "Mods").is_dir():
        raise ModError("请选择含 StardewModdingAPI.exe 和 Mods 文件夹的游戏目录。")
    checked_path(game, game / "Mods")
    return game


def scan(game, with_disabled=True):
    game = validate_game(game)
    mods, errors = [], []
    roots = [(game / "Mods", False)]
    if with_disabled:
        roots.append((game / ".stardew-mod-steward" / "disabled", True))
    for root, disabled in roots:
        for p in walk_manifests(root, errors):
            try:
                mod = mod_from_manifest(p, disabled)
                # SMAPI ignores dot-prefixed directories, but retain them in the inventory.
                if not disabled and any(x.startswith(".") for x in p.relative_to(root).parts[:-1]):
                    mod.disabled = True
                    mod.issues.append("目录名以点开头，被 SMAPI 忽略；请在资源管理器中调整。")
                mods.append(mod)
            except Exception as e:
                errors.append(f"{p}: {e}")
    counts = Counter(m.uid.casefold() for m in mods if not m.disabled)
    active = {m.uid.casefold(): m for m in mods if not m.disabled}
    api, _ = installed_versions(game)
    for m in mods:
        if not m.disabled and counts[m.uid.casefold()] > 1:
            m.issues.append("重复 UniqueID；请先人工确认保留哪一份。")
        if m.disabled:
            continue
        deps = list(m.manifest.get("Dependencies", []))
        if m.manifest.get("ContentPackFor"):
            deps.append(m.manifest["ContentPackFor"])
        for dep in deps:
            if dep.get("IsRequired", True) and dep.get("UniqueID", "").casefold() not in active:
                m.issues.append("缺少前置：" + dep.get("UniqueID", "?"))
            elif dep.get("IsRequired", True) and dep.get("MinimumVersion"):
                other = active.get(dep.get("UniqueID", "").casefold())
                if other and compare_versions(other.version, str(dep["MinimumVersion"])) == -1:
                    m.issues.append("前置版本过低：" + dep["UniqueID"])
        minimum = m.manifest.get("MinimumApiVersion", m.manifest.get("MinimumAPIVersion", ""))
        if minimum and compare_versions(api, str(minimum)) == -1:
            m.issues.append("需要 SMAPI " + str(minimum))
    return sorted(mods, key=lambda m: m.name.casefold()), errors


def installed_versions(game):
    """Use installed assemblies, never a version from a previous game's log."""
    game = Path(game)
    api, gv = "", ""
    for filename, is_api in [("StardewModdingAPI.deps.json", True), ("Stardew Valley.deps.json", False)]:
        try:
            data = json.loads((game / filename).read_text(encoding="utf-8-sig"))
            prefix = "StardewModdingAPI/" if is_api else "Stardew Valley/"
            version = next(x.split("/", 1)[1] for x in data["libraries"] if x.startswith(prefix))
            if is_api:
                api = version
            else:
                gv = version
        except (OSError, ValueError, KeyError, StopIteration):
            pass
    if os.name == "nt":
        def file_version(path):
            ver = ctypes.windll.version
            size = ver.GetFileVersionInfoSizeW(str(path), None)
            if not size:
                return ""
            buf = ctypes.create_string_buffer(size)
            ptr, length = ctypes.c_void_p(), ctypes.c_uint()
            if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
                return ""
            if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
                return ""
            values = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32))
            parts = [values[2] >> 16, values[2] & 65535, values[3] >> 16, values[3] & 65535]
            while len(parts) > 3 and parts[-1] == 0:
                parts.pop()
            return ".".join(map(str, parts))
        api = file_version(game / "StardewModdingAPI.dll") or api
        gv = file_version(game / "Stardew Valley.dll") or gv
    if not api:
        raise ModError("无法读取已安装的 SMAPI 版本，请检查游戏目录。")
    return api, gv


def version_tuple(v):
    v = str(v).strip().lstrip("vV")
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", v)
    if not match:
        return None
    nums = tuple(map(int, match[1].split(".")))
    nums += (0,) * max(0, 4 - len(nums))
    pre = match[2]
    prekey = tuple((0, int(t)) if t.isdigit() else (1, t.lower()) for t in pre.split(".")) if pre else ()
    return nums, 0 if pre else 1, prekey


def compare_versions(a, b):
    x, y = version_tuple(a), version_tuple(b)
    return None if x is None or y is None else (x > y) - (x < y)


def request_json(method, url, **kwargs):
    try:
        r = requests.request(method, url, timeout=(12, 60), **kwargs)
        if r.status_code == 429:
            raise ModError("接口限流，请稍后重试；本次不继续请求。")
        if r.status_code in (401, 403):
            raise ModError("接口拒绝访问：请检查 Nexus 密钥及账号下载权限。")
        if not r.ok:
            raise ModError(f"服务器返回 HTTP {r.status_code}。")
        return r.json()
    except (requests.RequestException, ValueError) as e:
        # Do not include request URLs or headers: temporary download tokens are secrets.
        raise ModError(f"网络请求失败（{type(e).__name__}），请检查网络后重试。") from None


# Exact identities of components shipped with these packages. These are associations,
# never version substitutions: a component's version is not compared with its main package.
# Verified against installed package manifests + the primary ID returned by SMAPI's API.
BUNDLE_COMPONENTS = {
    "flashshifter.stardewvalleyexpandedcp": ("FlashShifter.SVECode", "package"),
    "flashshifter.sve-ftm": ("FlashShifter.SVECode", "package"),
    "rafseazz.rsvcc": ("Rafseazz.RidgesideVillage", "package"),
    "rafseazz.rsvftm": ("Rafseazz.RidgesideVillage", "package"),
    "lemurkat.eastscarp": ("atravita.EastScarp", "package"),
    "lemurkat.eastscarpnpcs": ("atravita.EastScarp", "package"),
    "lemurkat.eastscarpe.ftm": ("atravita.EastScarp", "package"),
    "lemurkat.eastscarpe.cp": ("atravita.EastScarp", "web"),
}


def prepare_update_result(mod, raw):
    result = json.loads(json.dumps(raw))
    valid, rejected = validated_update_keys(mod.keys)
    result["effectiveUpdateKeys"] = valid
    result["ignoredUpdateKeys"] = rejected
    result["sourceNotes"] = []
    if rejected:
        result["sourceNotes"].append("清单含无效更新键，已在本软件查询时忽略：" + "、".join(rejected))
    meta = result.get("metadata") or {}
    usable = bool((result.get("suggestedUpdate") or {}).get("version") or
                  any(isinstance(meta.get(f), dict) and meta[f].get("version")
                      for f in ("main", "optional", "unofficial")))
    errors = result.get("errors") or []
    result["queryState"] = "failed" if errors else "complete" if usable else "unknown"
    if errors and usable:
        # API errors describe individual sources. A deleted Nexus page doesn't invalidate
        # version data that the same response successfully retrieved from another source.
        result["sourceWarnings"] = errors
        result["errors"] = []
        result["queryState"] = "partial"
        result["sourceNotes"].append("部分来源不可用，已取得其他来源的版本；原始来源错误仍保留在详情中。")
    if mod.uid.casefold() in ("smapi.consolecommands", "smapi.savebackup") and not usable and not errors:
        result["managedBySmapi"] = True
        result["queryState"] = "managed"
    return result


def associate_bundles(rows, result):
    by_id = {m.uid.casefold(): m for m in rows}
    for mod in rows:
        relation = BUNDLE_COMPONENTS.get(mod.uid.casefold())
        # A valid, explicit update key takes precedence over a bundle association.
        if not relation or mod.valid_keys:
            continue
        uid, mode = relation
        owner = by_id.get(uid.casefold())
        own = result.get(mod.uid.casefold(), {})
        parent = result.get(uid.casefold(), {})
        if not owner or owner.path.parent != mod.path.parent or own.get("errors"):
            continue
        meta = parent.get("metadata") or {}
        main = meta.get("main") or {}
        primary_ids = [str(x).casefold() for x in meta.get("id", [])]
        # Require the authoritative API to recognize the exact primary ID and a real source.
        if owner.uid.casefold() not in primary_ids or not main.get("version") or not main.get("url"):
            continue
        own["bundle"] = dict(uid=owner.uid, name=owner.name, path=str(owner.path), url=main["url"],
                             installedVersion=owner.version, remoteVersion=main["version"],
                             suggestedVersion=(parent.get("suggestedUpdate") or {}).get("version", ""),
                             mode=mode)
        own["queryState"] = "bundled"
        own["sourceNotes"].append("此组件随同目录的主包管理，不将组件版本与主包版本直接比较。" if mode == "package" else
                                   "此兼容补丁来自主包下载页的可选文件，缺少独立可比版本，请在网页核对。")


def check_updates(game, mods, log=lambda _: None):
    unique = {}
    # Only one inventory row per ID is sent. Ambiguous duplicate installs aren't updated.
    for m in mods:
        if not m.disabled:
            unique.setdefault(m.uid.casefold(), m)
    rows, result = list(unique.values()), {}
    api, gv = installed_versions(game)
    for i in range(0, len(rows), 35):
        chunk = rows[i:i + 35]
        log(f"查询更新 {i + 1}–{i + len(chunk)} / {len(rows)} …")
        body = dict(mods=[dict(id=m.uid, updateKeys=m.valid_keys, installedVersion=m.version, isBroken=False)
                          for m in chunk], apiVersion=api, platform="Windows", includeExtendedMetadata=True)
        if gv:
            body["gameVersion"] = gv
        data = request_json("POST", "https://smapi.io/api/v4.0.0/mods", headers=HTTP_HEADERS, json=body)
        if not isinstance(data, list):
            raise ModError("SMAPI 更新接口返回格式发生变化。")
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                uid = item["id"].casefold()
                if uid in unique:
                    result[uid] = prepare_update_result(unique[uid], item)
    missing = [m.uid for m in rows if m.uid.casefold() not in result]
    if missing:
        for uid in missing:
            result[uid.casefold()] = prepare_update_result(unique[uid.casefold()],
                {"id": uid, "errors": ["接口未返回此 MOD 的结果。"]})
    associate_bundles(rows, result)
    return {"schema": CACHE_SCHEMA, "time": time.time(), "game": str(validate_game(game)),
            "versions": {m.uid.casefold(): m.version for m in rows},
            "keys": {m.uid.casefold(): m.keys for m in rows}, "results": result}


def apply_cache(mods, cache, game):
    if cache.get("schema") != CACHE_SCHEMA or cache.get("game", "").casefold() != str(normalize_game_path(game)).casefold():
        return
    for m in mods:
        if not m.disabled and cache.get("versions", {}).get(m.uid.casefold()) == m.version and cache.get("keys", {}).get(m.uid.casefold()) == m.keys:
            row = cache.get("results", {}).get(m.uid.casefold(), {})
            bundle = row.get("bundle") or {}
            if bundle and not any(other.uid == bundle["uid"] and str(other.path) == bundle["path"] and
                                  other.version == bundle["installedVersion"] for other in mods):
                continue
            m.update = row


class Nexus:
    def __init__(self, key):
        if not key:
            raise ModError("请先在设置中填写 Nexus 个人 API Key。")
        self.headers = {**HTTP_HEADERS, "apikey": key, "Application-Name": "StardewModSteward",
                        "Application-Version": APP_VERSION}

    def get(self, endpoint):
        return request_json("GET", "https://api.nexusmods.com/v1/" + endpoint,
                            headers=self.headers, allow_redirects=False)

    def validate(self):
        return self.get("users/validate.json")

    def files(self, mid):
        return self.get(f"games/stardewvalley/mods/{int(mid)}/files.json").get("files", [])

    def candidates(self, mod):
        if not mod.nexus_id:
            return []
        subkeys = [k.split("@", 1)[1] for k in mod.keys
                   if re.match(rf"Nexus:{mod.nexus_id}@", k, re.I)]
        files = [f for f in self.files(mod.nexus_id) if f.get("category_id") == 1
                 and compare_versions(str(f.get("version", "")), mod.suggestion) == 0]
        if subkeys:
            files = [f for f in files if any("@" + sub.lower() in
                     (f.get("name", "") + " " + f.get("description", "")).lower() for sub in subkeys)]
        return files

    def download(self, mid, file, log=lambda _: None, data=DATA):
        links = self.get(f"games/stardewvalley/mods/{int(mid)}/files/{int(file['file_id'])}/download_link.json")
        if not links or not links[0].get("URI"):
            raise ModError("Nexus 未返回可用的下载链接。")
        url = links[0]["URI"]
        from urllib.parse import urlparse
        if urlparse(url).scheme != "https":
            raise ModError("拒绝非 HTTPS 下载链接。")
        name = Path(file.get("file_name", "mod.zip")).name
        ext = Path(name).suffix.lower()
        if ext not in (".zip", ".7z", ".rar"):
            raise ModError("当前仅支持 ZIP / 7Z / RAR 更新包。")
        dest = Path(data) / "downloads" / f"{mid}-{file['file_id']}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(ext + ".part")
        try:
            # Deliberately use a new request without the API key on the CDN.
            with requests.get(url, headers={"User-Agent": HTTP_HEADERS["User-Agent"]},
                              stream=True, timeout=(12, 60)) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length", 0))
                count, last = 0, 0.0
                with part.open("wb") as out:
                    for block in response.iter_content(512 * 1024):
                        count += len(block)
                        if count > 3 * 1024 ** 3:
                            raise ModError("下载包超过 3 GB，已停止。")
                        out.write(block)
                        if time.monotonic() - last > 1:
                            log(f"下载 {name}：{count / 1024**2:.1f} MB" +
                                (f" / {total / 1024**2:.1f} MB" if total else ""))
                            last = time.monotonic()
                if total and count != total:
                    raise ModError("下载不完整，请重试。")
            os.replace(part, dest)
            return dest
        except requests.RequestException as e:
            raise ModError(f"下载失败（{type(e).__name__}），可稍后重试。") from None
        finally:
            part.unlink(missing_ok=True)


def hold_desktop_mutex():
    """Let Setup detect a running GUI without closing an in-progress MOD job.

    Keep the handle alive for the process lifetime; Windows releases it on exit.
    The existing QLockFile remains responsible for single-instance behavior.
    """
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, APP_MUTEX)
    if not handle:
        raise ModError("无法创建安装器运行状态标记，请关闭管家后重试。")
    return handle


def game_running():
    if os.name != "nt":
        return False
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                         creationflags=CREATE_NO_WINDOW, timeout=15)
    if out.returncode:
        raise ModError("无法确认游戏进程状态，已停止修改。")
    text = out.stdout.decode("utf-8", errors="replace").lower()
    return '"stardewmoddingapi.exe"' in text or '"stardew valley.exe"' in text


def ensure_stopped():
    if game_running():
        raise ModError("请先退出星露谷和 SMAPI，再安装、启用、禁用或恢复 MOD。")


def validate_member(name):
    name = name.replace("\\", "/")
    p = PurePosixPath(name)
    if not name or name.startswith("/") or any(x in ("..", "") for x in p.parts):
        raise ModError(f"压缩包包含不安全路径：{name}")
    for part in p.parts:
        if ":" in part or part.endswith((" ", ".")) or re.search(r'[<>"|?*\x00-\x1f]', part):
            raise ModError(f"压缩包包含非法 Windows 文件名：{name}")
        if part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
            raise ModError(f"压缩包包含保留文件名：{name}")
    return p


def extract_archive(archive: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            if len(entries) > 100000 or sum(x.file_size for x in entries) > 6 * 1024**3:
                raise ModError("压缩包展开过大。")
            seen = set()
            for x in entries:
                p = validate_member(x.filename.rstrip("/\\"))
                key = str(p).casefold()
                if key in seen:
                    raise ModError("压缩包中有重名路径，已停止。")
                seen.add(key)
                if stat.S_ISLNK(x.external_attr >> 16):
                    raise ModError("压缩包内不允许符号链接。")
            # Extract by normalized path; zipfile.extract mishandles backslashes on some platforms.
            for x in entries:
                target = dest.joinpath(*PurePosixPath(x.filename.replace("\\", "/")).parts)
                checked_path(dest, target)
                if x.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(x) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
    else:
        exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe"
        if not exe.exists():
            raise ModError("7Z / RAR 需要安装官方 7-Zip；ZIP 可以直接安装。")
        listing = subprocess.run([str(exe), "l", "-slt", "-ba", "-sccUTF-8", "--", str(archive)],
                                 capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=90)
        if listing.returncode:
            raise ModError("7-Zip 无法读取此压缩包。")
        text = listing.stdout.decode("utf-8", errors="replace")
        entries = re.split(r"\r?\n\r?\n", text.strip())
        total = 0
        for entry in entries:
            values = dict(line.split(" = ", 1) for line in entry.splitlines() if " = " in line)
            if "Path" in values:
                validate_member(values["Path"].rstrip("/\\"))
            if any("link" in k.lower() for k in values) or " L" in values.get("Attributes", ""):
                raise ModError("压缩包包含链接，已停止。")
            total += int(values.get("Size", "0"))
        if not entries or len(entries) > 100000 or total > 6 * 1024**3:
            raise ModError("压缩包展开过大或为空。")
        run = subprocess.run([str(exe), "x", "-y", "-p", "-sccUTF-8", f"-o{dest}", "--", str(archive)],
                             capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=300)
        if run.returncode:
            raise ModError("7-Zip 解压失败（可能有密码或文件损坏）。")
    assert_plain_tree(dest)


def assert_plain_tree(root):
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            checked_path(root, Path(folder) / name)


@dataclass
class PlanItem:
    mod: Mod
    target: Path
    old: Mod | None


class InstallPlan:
    def __init__(self, game, archive, expected=None, log=lambda _: None):
        self.game = validate_game(game)
        ensure_no_pending(self.game)
        self.archive = Path(archive)
        self.state = self.game / ".stardew-mod-steward"
        checked_path(self.game, self.state)
        self.state.mkdir(exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(prefix="stage-", dir=self.state))
        self.items = []
        self.expected = expected or {}
        self.log = log
        try:
            log("正在校验并解压更新包 …")
            extract_archive(self.archive, self.temp / "extract")
            available = [mod_from_manifest(p) for p in walk_manifests(self.temp / "extract")]
            if not available:
                raise ModError("压缩包内没有 SMAPI manifest.json；不支持游戏原文件覆盖包。")
            if len({m.uid.casefold() for m in available}) != len(available):
                raise ModError("压缩包内有重复 UniqueID，无法安全选择安装版本。")
            # Nested source mods have shared payload boundaries. Require a clean package.
            for a in available:
                for b in available:
                    if a is not b and a.path in b.path.parents:
                        raise ModError(f"压缩包中 MOD 互相嵌套：{a.name} / {b.name}；请整理为独立目录后安装。")
            existing, errors = scan(self.game)
            if errors:
                raise ModError("当前目录存在无法读取的清单或链接，请先处理：" + errors[0])
            by_id = {}
            for m in existing:
                by_id.setdefault(m.uid.casefold(), []).append(m)
            for mod in available:
                matches = by_id.get(mod.uid.casefold(), [])
                if len(matches) > 1:
                    raise ModError(f"{mod.name} 在本机存在多份相同 ID，已阻止覆盖。")
                old = matches[0] if matches else None
                if old:
                    if old.disabled:
                        raise ModError(f"{mod.name} 已停用；请先启用后再更新。")
                    if compare_versions(mod.version, old.version) == -1:
                        raise ModError(f"{mod.name} 的包版本 {mod.version} 低于已装 {old.version}，已阻止降级。")
                    target = old.path
                    if any(other is not old and (target in other.path.parents or other.path in target.parents)
                           for other in existing if not other.disabled):
                        raise ModError(f"{mod.name} 的本机目录与其他 MOD 嵌套，请先整理为独立目录再更新。")
                else:
                    folder = re.sub(r'[^\w.\-\[\] ]', "_", mod.uid).strip(" .")[:120]
                    target = self.game / "Mods" / folder
                    if target.exists():
                        raise ModError(f"新 MOD 目标目录已存在：{target}；为防误覆盖已停止。")
                checked_path(self.game / "Mods", target)
                self.items.append(PlanItem(mod, target, old))
            for uid, wanted in self.expected.items():
                found = next((m for m in available if m.uid.casefold() == uid.casefold()), None)
                if not found or compare_versions(found.version, wanted) != 0:
                    raise ModError(f"下载包清单与推荐更新不一致：{uid}，预期版本 {wanted}。")
            for a in self.items:
                for b in self.items:
                    if a is not b and (a.target == b.target or a.target in b.target.parents):
                        raise ModError("安装目标重叠，已停止。")
            # Verify dependencies against the combined future inventory.
            future = {m.uid.casefold(): m for m in existing if not m.disabled}
            future.update({m.uid.casefold(): m for m in available})
            api, _ = installed_versions(self.game)
            for m in available:
                minimum = m.manifest.get("MinimumApiVersion", m.manifest.get("MinimumAPIVersion", ""))
                if minimum and compare_versions(api, str(minimum)) == -1:
                    raise ModError(f"{m.name} 需要 SMAPI {minimum}；请先更新 SMAPI。")
                deps = list(m.manifest.get("Dependencies", []))
                if m.manifest.get("ContentPackFor"):
                    deps.append(m.manifest["ContentPackFor"])
                for dep in deps:
                    if not dep.get("IsRequired", True):
                        continue
                    target = future.get(dep.get("UniqueID", "").casefold())
                    if not target:
                        raise ModError(f"{m.name} 缺少前置 {dep.get('UniqueID', '?')}，请先安装前置或一起导入。")
                    if dep.get("MinimumVersion") and compare_versions(target.version, str(dep["MinimumVersion"])) == -1:
                        raise ModError(f"{m.name} 的前置 {target.name} 需要 {dep['MinimumVersion']}。")
        except BaseException:
            self.close()
            raise

    def summary(self):
        return "\n".join(f"{'更新' if i.old else '安装'}  {i.mod.name}  " +
                         (f"{i.old.version} → " if i.old else "") + i.mod.version for i in self.items)

    def close(self):
        if self.temp.exists():
            safe_remove(self.state, self.temp)

    def execute(self, preserve=True):
        ensure_stopped()
        ensure_no_pending(self.game)
        backup = self.state / "backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        checked_path(self.state, backup)
        backup.mkdir(parents=True)
        entries = []
        for index, item in enumerate(self.items):
            checked_path(self.game / "Mods", item.target)
            stage = self.temp / f"ready-{index}"
            assert_plain_tree(item.mod.path)
            shutil.copytree(item.mod.path, stage)
            if item.old:
                assert_plain_tree(item.target)
                # Preserve user configuration; old code/assets are never merged into the new version.
                if preserve:
                    for old_config in item.target.rglob("config.json"):
                        dest = stage / old_config.relative_to(item.target)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(old_config, dest)
            entries.append(dict(target=str(item.target.relative_to(self.game)), old=item.old is not None,
                                backup=f"old-{index}", stage=str(stage), uid=item.mod.uid,
                                name=item.mod.name, version=item.mod.version, moved=False, installed=False))
        journal = dict(kind="install", state="prepared", game=str(self.game), archive=self.archive.name,
                       time=time.time(), entries=entries)
        path = backup / "transaction.json"
        write_json(path, journal)
        try:
            for entry in entries:
                ensure_stopped()
                target = checked_path(self.game / "Mods", self.game / entry["target"])
                # Re-read live metadata immediately before mutation.
                if entry["old"]:
                    current = mod_from_manifest(target / "manifest.json")
                    original = next(i.old for i in self.items if i.mod.uid == entry["uid"])
                    if current.uid.casefold() != original.uid.casefold() or current.version != original.version:
                        raise ModError("MOD 在准备期间发生变化，请刷新后重试。")
                    os.replace(target, backup / entry["backup"])
                    entry["moved"] = True
                    write_json(path, journal)
                elif target.exists():
                    raise ModError("目标目录在准备期间出现，请刷新后重试。")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(entry["stage"], target)
                entry["installed"] = True
                write_json(path, journal)
                self.log("已安装 " + entry["name"] + " " + entry["version"])
            journal["state"] = "complete"
            write_json(path, journal)
            self.log("原版目录备份：" + str(backup))
            return backup
        except BaseException:
            rollback_errors = []
            for entry in reversed(entries):
                target = self.game / entry["target"]
                try:
                    if entry["installed"] and target.exists():
                        safe_remove(self.game / "Mods", target)
                    saved = backup / entry["backup"]
                    if entry["moved"] and saved.exists():
                        os.replace(saved, target)
                except Exception as e:
                    rollback_errors.append(str(e))
            journal["state"] = "recovery_required" if rollback_errors else "rolled_back"
            journal["errors"] = rollback_errors
            write_json(path, journal)
            if rollback_errors:
                raise ModError(f"自动回滚未完全完成，请打开备份目录恢复：{backup}")
            raise
        finally:
            self.close()


def backup_list(game):
    root = Path(game) / ".stardew-mod-steward" / "backups"
    checked_path(Path(game), root)
    result = []
    if root.exists():
        for p in sorted(root.glob("*/transaction.json"), reverse=True):
            checked_path(root, p)
            try:
                row = json.loads(p.read_text(encoding="utf-8"))
                row["folder"] = str(p.parent)
                result.append(row)
            except (OSError, ValueError):
                pass
    return sorted(result, key=lambda row: row.get("time", 0), reverse=True)


def ensure_no_pending(game):
    if any(row.get("state") in ("prepared", "recovery_required") for row in backup_list(game)):
        raise ModError("发现中断的文件操作（安装或启停），请先在「备份与恢复」中恢复该批次，再进行新的修改。")


def restore_backup(game, folder, log=lambda _: None):
    with mutation_lock(game):
        return _restore_backup_locked(game, folder, log)


def _restore_backup_locked(game, folder, log=lambda _: None):
    game = validate_game(game)
    ensure_stopped()
    root = game / ".stardew-mod-steward" / "backups"
    folder = checked_path(root, Path(folder))
    path = folder / "transaction.json"
    journal = json.loads(path.read_text(encoding="utf-8"))
    if journal.get("state") not in ("complete", "prepared", "recovery_required"):
        raise ModError("这个备份已经恢复或回滚。")
    if journal.get("kind") not in ("install", "mod_state") or journal.get("game", "").casefold() != str(game).casefold():
        raise ModError("备份不属于当前游戏。")
    # Restore newest active transaction first, so earlier backups remain meaningful.
    later = [b for b in backup_list(game) if b.get("state") == "complete" and b.get("time", 0) > journal["time"]]
    if later:
        raise ModError("请按时间从新到旧恢复备份。")
    if journal["kind"] == "mod_state":
        return _restore_mod_state(game, folder, journal, log)
    actions = []
    for entry in journal["entries"]:
        target = checked_path(game / "Mods", game / entry["target"])
        saved = checked_path(folder, folder / entry["backup"])
        if target.exists():
            assert_plain_tree(target)
        if saved.exists():
            assert_plain_tree(saved)
        # A prepared transaction can have crashed between a rename and journal update.
        if entry["old"] and not saved.exists():
            continue
        if not entry["old"] and not target.exists():
            continue
        actions.append((entry, target, saved))
    current = folder / ("before-restore-" + uuid.uuid4().hex[:8])
    current.mkdir()
    done = []
    try:
        for index, (entry, target, saved) in enumerate(reversed(actions)):
            ensure_stopped()
            held = current / str(index)
            if target.exists():
                os.replace(target, held)
            done.append((entry, target, saved, held))
            if entry["old"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(saved, target)
            log("已恢复 " + entry["name"])
    except BaseException:
        for entry, target, saved, held in reversed(done):
            if entry["old"] and target.exists():
                os.replace(target, saved)
            if held.exists():
                os.replace(held, target)
        raise
    journal["state"] = "restored"
    journal["restore_time"] = time.time()
    journal["previous_files"] = str(current)
    write_json(path, journal)
    return current


def required_dependencies(mod):
    result = [dep for dep in mod.manifest.get("Dependencies", []) if dep.get("IsRequired", True)]
    if mod.manifest.get("ContentPackFor"):
        result.append({**mod.manifest["ContentPackFor"], "IsRequired": True})
    return result


def state_companions(game, requested, mods):
    """Suggest connected components within an original container, never shared frameworks.

    Folder names alone aren't evidence of a package. Require an explicit required
    dependency and matching nonempty author; disabled rows use their recorded path.
    Suggestions remain separate from ModStatePlan and require UI confirmation.
    """
    game = validate_game(game)
    roots = {}
    for mod in mods:
        try:
            if mod.disabled:
                checked_path(game / ".stardew-mod-steward" / "disabled", mod.path)
                marker = mod.path / ".steward-disabled.json"
                checked_path(mod.path, marker)
                if marker.stat().st_size > 65536:
                    continue
                record = json.loads(marker.read_text(encoding="utf-8-sig"))
                relative = record["relative"]
                if not isinstance(relative, str) or not relative:
                    continue
                original = checked_path(game / "Mods", game / "Mods" / relative)
            else:
                original = checked_path(game / "Mods", mod.path)
            parts = original.relative_to(game / "Mods").parts
            # A MOD directly under Mods has no containing package folder.
            if len(parts) > 1 and mod.author.strip():
                roots[str(mod.path).casefold()] = (parts[0].casefold(), mod.author.strip().casefold())
        except (OSError, ValueError, KeyError, TypeError, ModError):
            continue
    chosen = {str(mod.path).casefold() for mod in requested}
    connected = set(chosen)
    dependencies = {str(mod.path).casefold(): {dep.get("UniqueID", "").casefold()
                    for dep in required_dependencies(mod)} for mod in mods}
    while True:
        added = set()
        for mod in mods:
            key = str(mod.path).casefold()
            if key in connected or key not in roots:
                continue
            for other in mods:
                other_key = str(other.path).casefold()
                if other_key not in connected or roots.get(other_key) != roots[key]:
                    continue
                if other.uid.casefold() in dependencies[key] or mod.uid.casefold() in dependencies[other_key]:
                    added.add(key)
                    break
        if not added:
            break
        connected.update(added)
    return [mod for mod in mods if str(mod.path).casefold() in connected - chosen]


def _state_paths(game, entry):
    source = checked_path(game, game / entry["source"])
    destination = checked_path(game, game / entry["destination"])
    disabled = game / ".stardew-mod-steward" / "disabled"
    checked_path(disabled if entry["enabled"] else game / "Mods", source)
    checked_path(game / "Mods" if entry["enabled"] else disabled, destination)
    return source, destination


def _state_marker(folder, encoded):
    marker = folder / ".steward-disabled.json"
    checked_path(folder, marker)
    if encoded is None:
        marker.unlink(missing_ok=True)
        return
    data = base64.b64decode(encoded, validate=True)
    if len(data) > 65536:
        raise ModError("启停记录过大，已停止。")
    temporary = marker.with_name(marker.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def _check_state_identity(path, entry):
    actual = mod_from_manifest(path / "manifest.json")
    if actual.uid.casefold() != entry["uid"].casefold() or actual.version != entry["version"]:
        raise ModError("MOD 在操作期间发生变化，请刷新列表后重试：" + entry["name"])
    assert_plain_tree(path)


def _rollback_state_change(game, journal, log=lambda _: None):
    errors = []
    for entry in reversed(journal["entries"]):
        try:
            source, destination = _state_paths(game, entry)
            if source.exists() and destination.exists():
                raise ModError("原目录和目标同时存在，请人工核对，未覆盖任何文件。")
            if destination.exists():
                _check_state_identity(destination, entry)
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
            elif not source.exists():
                raise ModError("找不到该 MOD 的原目录或目标目录。")
            _check_state_identity(source, entry)
            _state_marker(source, entry["marker_before"])
            log("已恢复原启用状态：" + entry["name"])
        except Exception as exc:
            errors.append(entry["name"] + "：" + str(exc))
    return errors


class ModStatePlan:
    """Validate a single/batch state change against the final dependency graph."""
    def __init__(self, game, requested, enabled):
        self.game = validate_game(game)
        ensure_stopped()
        ensure_no_pending(self.game)
        # None means each requested item is a (Mod, desired_enabled) pair.
        self.enabled = bool(enabled) if enabled is not None else None
        self.requested = list(requested)
        targets = [(mod, self.enabled) for mod in self.requested] if enabled is not None else self.requested
        self.items = []
        mods, errors = scan(self.game)
        if errors:
            raise ModError("有清单读取错误，请先修复后操作：" + errors[0])
        disabled = self.game / ".stardew-mod-steward" / "disabled"
        checked_path(self.game, disabled)
        seen = {}
        for old, wanted in targets:
            if not isinstance(wanted, bool):
                raise ModError("启用状态必须是布尔值。")
            key = str(old.path).casefold()
            if key in seen:
                if seen[key] != wanted:
                    raise ModError("同一个 MOD 不能同时启用和禁用。")
                continue
            seen[key] = wanted
            mod = next((m for m in mods if m.path == old.path and m.uid.casefold() == old.uid.casefold()
                        and m.version == old.version), None)
            if mod is None or mod.disabled != old.disabled:
                raise ModError("列表状态已改变，请刷新后重试：" + old.name)
            if (not mod.disabled) == wanted:
                continue
            assert_plain_tree(mod.path)
            if any(other.path != mod.path and (mod.path in other.path.parents or other.path in mod.path.parents)
                   for other in mods):
                raise ModError("此 MOD 与其他 MOD 目录嵌套，请先整理独立目录：" + mod.name)
            marker = mod.path / ".steward-disabled.json"
            if wanted:
                try:
                    checked_path(disabled, mod.path)
                except ModError:
                    raise ModError("此目录由外部方式隐藏，未保存原路径；请在资源管理器中调整点开头的目录名：" + mod.name)
                if not marker.is_file() or marker.stat().st_size > 65536:
                    raise ModError("缺少有效的原目录记录，无法自动启用：" + mod.name)
                before = marker.read_bytes()
                try:
                    record = json.loads(before.decode("utf-8-sig"))
                    relative = record["relative"]
                    if not isinstance(relative, str) or not relative:
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise ModError("原目录记录无效，无法自动启用：" + mod.name)
                target = checked_path(self.game / "Mods", self.game / "Mods" / relative)
                after = None
            else:
                checked_path(self.game / "Mods", mod.path)
                if marker.exists():
                    raise ModError("目录中有遗留的启停记录，请先核对后再操作：" + mod.name)
                before = None
                target = disabled / (re.sub(r"[^\w.-]", "_", mod.uid)[:100] + "-" + uuid.uuid4().hex[:8])
                record = dict(relative=str(mod.path.relative_to(self.game / "Mods")), uid=mod.uid, version=mod.version)
                after = base64.b64encode(json.dumps(record, ensure_ascii=False).encode("utf-8")).decode("ascii")
            if target.exists():
                raise ModError("目标目录已存在，未覆盖任何文件：" + str(target))
            entry = dict(name=mod.name, uid=mod.uid, version=mod.version, source=str(mod.path.relative_to(self.game)),
                         destination=str(target.relative_to(self.game)), enabled=wanted,
                         marker_before=base64.b64encode(before).decode("ascii") if before is not None else None,
                         marker_after=after)
            _state_paths(self.game, entry)
            self.items.append(entry)
        destinations = [str((self.game / e["destination"]).absolute()).casefold() for e in self.items]
        if len(set(destinations)) != len(destinations):
            raise ModError("多个 MOD 将写入同一个目录，已停止。")
        changed_paths = {str(self.game / e["source"]).casefold() for e in self.items}
        changed_ids = {e["uid"].casefold() for e in self.items}
        changing = [m for m in mods if str(m.path).casefold() in changed_paths]
        enabling_paths = {str(self.game / e["source"]).casefold() for e in self.items if e["enabled"]}
        enabling = [m for m in changing if str(m.path).casefold() in enabling_paths]
        future = [m for m in mods if not m.disabled and str(m.path).casefold() not in changed_paths]
        future.extend(enabling)
        by_id = {}
        for mod in future:
            by_id.setdefault(mod.uid.casefold(), []).append(mod)
        problems = []
        api, _ = installed_versions(self.game)
        for mod in enabling:
            if len(by_id.get(mod.uid.casefold(), [])) > 1:
                problems.append(mod.name + "：已有相同 UniqueID 的启用 MOD。")
            minimum = mod.manifest.get("MinimumApiVersion")
            if minimum and compare_versions(api, str(minimum)) == -1:
                problems.append(f"{mod.name}：需要 SMAPI {minimum}。")
        for mod in future:
            for dep in required_dependencies(mod):
                uid = dep.get("UniqueID", "").casefold()
                # Existing unrelated issues don't block an independent state change.
                if str(mod.path).casefold() not in changed_paths and uid not in changed_ids:
                    continue
                candidates = by_id.get(uid, [])
                if not candidates:
                    problems.append(f"{mod.name}：需要启用前置 {dep.get('UniqueID', '?')}。")
                elif dep.get("MinimumVersion") and not any(compare_versions(m.version, str(dep["MinimumVersion"])) in (0, 1)
                                                          for m in candidates):
                    problems.append(f"{mod.name}：前置 {dep['UniqueID']} 需要版本 {dep['MinimumVersion']}。")
        if problems:
            action = "切换方案" if self.enabled is None else "启用" if self.enabled else "禁用"
            raise ModError("无法" + action + "所选 MOD，请将需要同时调整的前置和依赖者一起选择：\n" + "\n".join(dict.fromkeys(problems)))

    def summary(self):
        return "\n".join(f"{'启用' if e['enabled'] else '禁用'}  {e['name']}  ({e['uid']})" for e in self.items)

    def execute(self, log=lambda _: None):
        with mutation_lock(self.game):
            return ModStatePlan(self.game, self.requested, self.enabled)._execute_locked(log)

    def _execute_locked(self, log):
        ensure_stopped()
        ensure_no_pending(self.game)
        if not self.items:
            return None
        state = self.game / ".stardew-mod-steward"
        folder = checked_path(state, state / "backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]))
        folder.mkdir(parents=True)
        journal = dict(kind="mod_state", action="切换方案" if self.enabled is None else "启用" if self.enabled else "禁用", state="prepared",
                       game=str(self.game), time=time.time(), enabled=self.enabled, entries=self.items)
        path = folder / "transaction.json"
        write_json(path, journal)
        try:
            for entry in self.items:
                ensure_stopped()
                source, destination = _state_paths(self.game, entry)
                _check_state_identity(source, entry)
                if destination.exists():
                    raise ModError("目标目录在准备期间出现，请刷新后重试。")
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                _state_marker(destination, entry["marker_after"])
                write_json(path, journal)
                log(("已启用：" if entry["enabled"] else "已禁用：") + entry["name"])
            journal["state"] = "complete"
            write_json(path, journal)
            return folder
        except BaseException:
            failures = _rollback_state_change(self.game, journal, log)
            journal["state"] = "recovery_required" if failures else "rolled_back"
            journal["errors"] = failures
            write_json(path, journal)
            if failures:
                raise ModError("启停操作未完全回滚，请打开「备份与恢复」处理：" + str(folder))
            raise


def _restore_mod_state(game, folder, journal, log):
    if journal["state"] in ("prepared", "recovery_required"):
        failures = _rollback_state_change(game, journal, log)
        journal["errors"] = failures
        journal["state"] = "recovery_required" if failures else "restored"
        write_json(folder / "transaction.json", journal)
        if failures:
            raise ModError("仍有启停记录需要人工恢复：\n" + "\n".join(failures))
        return folder
    mods, errors = scan(game)
    if errors:
        raise ModError("有清单读取错误，请先核对后再恢复。")
    selected = []
    for entry in journal["entries"]:
        _, current = _state_paths(game, entry)
        _check_state_identity(current, entry)
        mod = next((m for m in mods if m.path == current and m.uid.casefold() == entry["uid"].casefold()), None)
        if mod is None:
            raise ModError("找不到该启停批次对应的 MOD：" + entry["name"])
        selected.append((mod, not entry["enabled"]))
    if journal["enabled"] is None:
        plan = ModStatePlan(game, selected, None)
    else:
        plan = ModStatePlan(game, [mod for mod, _ in selected], not journal["enabled"])
    result = plan._execute_locked(log)
    journal["state"] = "restored"
    journal["restore_time"] = time.time()
    write_json(folder / "transaction.json", journal)
    return result or folder


def resolve_group_members(members, mods):
    """Expand required dependencies without guessing between duplicate IDs."""
    wanted = ModGroups.valid_members(members)
    by_id = {}
    for mod in mods:
        by_id.setdefault(mod.uid.casefold(), []).append(mod)
    resolved, queue = {}, list(wanted)
    while queue:
        uid = queue.pop(0)
        if uid.casefold() in resolved:
            continue
        candidates = by_id.get(uid.casefold(), [])
        if not candidates:
            raise ModError("编组需要的 MOD 未安装，请安装或编辑编组移除此项：" + uid)
        if len(candidates) != 1:
            raise ModError("编组中有重复 UniqueID，无法确定使用哪份，请先整理：" + uid)
        mod = candidates[0]
        resolved[uid.casefold()] = mod
        queue.extend(dep.get("UniqueID", "") for dep in required_dependencies(mod))
    return sorted(resolved.values(), key=lambda mod: mod.name.casefold())


class ModGroupPlan:
    """An exact enabled set plus required dependencies, applied in one transaction."""
    def __init__(self, game, members):
        self.game = validate_game(game)
        self.members = ModGroups.valid_members(members)
        ensure_stopped()
        mods, errors = scan(self.game)
        if errors:
            raise ModError("有清单读取错误，请先修复后应用编组：" + errors[0])
        self.resolved = resolve_group_members(self.members, mods)
        api, _ = installed_versions(self.game)
        for mod in self.resolved:
            minimum = mod.manifest.get("MinimumApiVersion")
            if minimum and compare_versions(api, str(minimum)) == -1:
                raise ModError(f"{mod.name}：需要 SMAPI {minimum}。")
            for dep in required_dependencies(mod):
                host = next(other for other in self.resolved if other.uid.casefold() == dep.get("UniqueID", "").casefold())
                if dep.get("MinimumVersion") and compare_versions(host.version, str(dep["MinimumVersion"])) == -1:
                    raise ModError(f"{mod.name}：前置 {host.uid} 需要版本 {dep['MinimumVersion']}。")
        original = {uid.casefold() for uid in self.members}
        self.dependencies = [mod for mod in self.resolved if mod.uid.casefold() not in original]
        desired = {mod.uid.casefold() for mod in self.resolved}
        self.plan = ModStatePlan(self.game, [(mod, mod.uid.casefold() in desired) for mod in mods], None)
        self.items = self.plan.items
        self.snapshot = sorted((str(mod.path).casefold(), mod.uid.casefold(), mod.version, mod.disabled,
                                json.dumps(mod.manifest, sort_keys=True, ensure_ascii=False)) for mod in mods)

    def summary(self):
        return self.plan.summary()

    def execute(self, log=lambda _: None):
        with mutation_lock(self.game):
            current = ModGroupPlan(self.game, self.members)
            if current.snapshot != self.snapshot:
                raise ModError("MOD 列表在预览后发生变化，请刷新并重新确认编组方案。")
            return current.plan._execute_locked(log)


def toggle_mod(game, mod):
    """Compatibility wrapper; all dependency and safety checks now live in the core."""
    plan = ModStatePlan(game, [mod], mod.disabled)
    folder = plan.execute()
    if folder is None:
        return mod.path
    journal = json.loads((folder / "transaction.json").read_text(encoding="utf-8"))
    return plan.game / journal["entries"][0]["destination"]


@contextmanager
def mutation_lock(game):
    state = validate_game(game) / ".stardew-mod-steward"
    checked_path(Path(game), state)
    state.mkdir(exist_ok=True)
    path = state / "operation.lock"
    with path.open("a+b") as f:
        f.write(b"0")
        f.flush()
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ModError("另一个管理器正在修改 MOD，请稍后重试。")
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
