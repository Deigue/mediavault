"""
config.py - settings that live outside the database.

A small JSON file next to the code, created with defaults on first run.
Chiefly which program copies files when moving a title between drives.
"""

import json
import os
import shutil

CONFIG_PATH = os.environ.get(
    "MEDIAVAULT_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
)

DEFAULTS = {
    # auto | robocopy | teracopy | fastcopy | custom | python
    "copy_tool": "auto",
    "teracopy_path": r"C:\Program Files\TeraCopy\TeraCopy.exe",
    # Full path to whichever program copy_tool names, when it needs one.
    "copy_tool_path": "",
    # Off makes a move faster and considerably less safe.
    "verify_after_copy": True,
    # rclone path for the Backup button, e.g. "gdrive:Backups/PC". Empty is off.
    "backup_target": "",
    # Where mediavault.db lives. Empty means next to the code. MEDIAVAULT_DB
    # overrides both.
    "db_path": "",
}


def load():
    """Current settings, with any missing keys filled in from the defaults."""
    settings = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        # Missing or unreadable file just means defaults.
        pass
    return settings


# Folder names the desktop sync clients use, matched anywhere in the path
# because the synced folder is usually an ancestor of the one picked.
CLOUD_FOLDERS = ("onedrive", "dropbox", "google drive", "googledrive", "gdrive",
                 "icloud", "box sync", "creative cloud files", "pcloud",
                 "megasync", "nextcloud", "owncloud", "yandexdisk", "tresorit",
                 "sync.com", "koofr", "jottacloud")


CLOUD_WARNING = (
    "That looks like a cloud-synced folder. The database is three files that "
    "only make sense as a set, and a scan rewrites them constantly. A sync "
    "client uploads each one whenever it happens to notice it, so the copy in "
    "the cloud can be a set that never existed, and writing its version back "
    "while the dashboard has the file open can corrupt it.\n\n"
    "Back up the database instead: that takes one consistent copy and uploads "
    "only that."
)

NETWORK_WARNING = (
    "{what} SQLite guards the file with byte-range locks, which shares do not "
    "reliably honour, so two writers can damage it and so can a connection "
    "dropping mid-write. Keep the database on a local disk and back it up to "
    "the network."
)


def db_path_warning(path):
    """
    Why a folder is a poor home for the live database, or None if it is fine.

    A warning, never a refusal. Both cases are the same problem: something
    other than SQLite is free to copy or rewrite the file behind its back.
    """
    if not path:
        return None
    lowered = path.replace("/", "\\").lower()

    if any(marker in lowered for marker in CLOUD_FOLDERS):
        return CLOUD_WARNING
    if lowered.startswith("\\\\"):
        return NETWORK_WARNING.format(what="That is a network path.")

    # A mapped letter is the same share under another name. Imported here so
    # this module keeps its one job and gains no import cycle.
    import scanner
    root = os.path.splitdrive(path)[0]
    if root and scanner.get_drive_type(root + "\\") == scanner.DRIVE_REMOTE:
        return NETWORK_WARNING.format(what=f"{root} is a network drive.")
    return None


def save(settings):
    """Write settings back, keeping only keys we recognise."""
    clean = {k: v for k, v in settings.items() if k in DEFAULTS}
    merged = dict(DEFAULTS)
    merged.update(clean)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    return merged


# (key, display name, exe name, [places to look]). The listed folders are the
# common cases only; both installers also drop themselves under the user
# profile, so find_installed() falls back to the uninstall registry.
KNOWN_COPY_TOOLS = [
    ("teracopy", "TeraCopy", "TeraCopy.exe", [
        r"C:\Program Files\TeraCopy\TeraCopy.exe",
        r"C:\Program Files (x86)\TeraCopy\TeraCopy.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\TeraCopy\TeraCopy.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\TeraCopy\TeraCopy.exe"),
        os.path.expandvars(r"%USERPROFILE%\TeraCopy\TeraCopy.exe"),
        r"C:\TeraCopy\TeraCopy.exe",
    ]),
    ("fastcopy", "FastCopy", "FastCopy.exe", [
        r"C:\Program Files\FastCopy\FastCopy.exe",
        r"C:\Program Files (x86)\FastCopy\FastCopy.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\FastCopy\FastCopy.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\FastCopy\FastCopy.exe"),
        # FastCopy's own installer offers this by default.
        os.path.expandvars(r"%USERPROFILE%\FastCopy\FastCopy.exe"),
        os.path.expandvars(r"%APPDATA%\FastCopy\FastCopy.exe"),
        r"C:\FastCopy\FastCopy.exe",
    ]),
]

_UNINSTALL_KEYS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]


def _registry_exe(display_name, exe_name):
    """
    Where an installed program put its executable, per the uninstall registry
    keys. The only way to find one installed somewhere unusual without
    walking the disk. None off Windows or if nothing matches.
    """
    try:
        import winreg
    except ImportError:
        return None

    wanted = display_name.lower()
    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}

    for hive_name, subkey in _UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hives[hive_name], subkey)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    entry = winreg.OpenKey(root, winreg.EnumKey(root, i))
                except OSError:
                    continue
                with entry:
                    def value(name):
                        try:
                            return winreg.QueryValueEx(entry, name)[0]
                        except OSError:
                            return None

                    name = (value("DisplayName") or "").lower()
                    if wanted not in name:
                        continue

                    # DisplayIcon is usually the exe itself, sometimes with a
                    # ",0" icon index tacked on the end.
                    icon = (value("DisplayIcon") or "").split(",")[0].strip('" ')
                    if icon and os.path.basename(icon).lower() == exe_name.lower() \
                            and os.path.isfile(icon):
                        return icon

                    folder = (value("InstallLocation") or "").strip('" ')
                    if folder:
                        candidate = os.path.join(folder, exe_name)
                        if os.path.isfile(candidate):
                            return candidate
    return None


def find_installed(display_name, exe_name, candidates):
    """A copy program's path: usual folders, then PATH, then the registry."""
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    on_path = shutil.which(exe_name)
    if on_path:
        return on_path
    return _registry_exe(display_name, exe_name)


def detect_copy_tools():
    """The copy programs available on this machine, as dicts for the settings
    picker."""
    found = [
        {"key": "auto", "name": "Automatic (robocopy if present)",
         "path": None, "available": True,
         "note": "Uses robocopy when it exists, otherwise the built-in copier."},
        {"key": "python", "name": "Built-in copier",
         "path": None, "available": True,
         "note": "Works everywhere. No progress window of its own."},
    ]

    robocopy = shutil.which("robocopy")
    found.append({
        "key": "robocopy", "name": "Robocopy (ships with Windows)",
        "path": robocopy, "available": bool(robocopy),
        "note": "Resumable and good with long paths. No window of its own.",
    })

    for key, name, exe, candidates in KNOWN_COPY_TOOLS:
        path = find_installed(name, exe, candidates)
        found.append({
            "key": key, "name": name, "path": path, "available": bool(path),
            "note": (f"Found at {path}" if path
                     else "Not installed, or not in its usual place."),
        })
    return found


def resolve_copy_tool(settings=None):
    """
    Which copier will actually run: (name, path_or_None, note). 'auto' and a
    missing executable are resolved here, so the UI can show what will really
    happen rather than the word "auto".
    """
    settings = settings or load()
    choice = (settings.get("copy_tool") or "auto").lower()

    if choice in ("teracopy", "fastcopy", "custom"):
        path = settings.get("copy_tool_path") or settings.get("teracopy_path")
        if path and os.path.isfile(path):
            return ("teracopy" if choice == "teracopy" else choice), path, ""
        return "python", None, (f"{choice} is selected but no executable was "
                                f"found at {path}. Falling back to the "
                                f"built-in copier.")

    if choice == "robocopy":
        path = shutil.which("robocopy")
        if path:
            return "robocopy", path, ""
        return "python", None, ("robocopy is selected but was not found. "
                                "Falling back to the built-in copier.")

    if choice == "python":
        return "python", None, ""

    # auto
    path = shutil.which("robocopy")
    if path:
        return "robocopy", path, ""
    return "python", None, ""
