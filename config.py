"""
config.py - settings that live outside the database.

A small JSON file next to the code, created with defaults on first run. It
holds the things you might want to change by hand, chiefly which program
copies files when moving a title between drives.
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
    #   auto      use robocopy if it is on the system, otherwise python
    #   robocopy  ships with Windows, resumable, good with long paths
    #   teracopy  needs copy_tool_path set below
    #   fastcopy  needs copy_tool_path set below
    #   custom    any copier taking a source and a destination
    #   python    shutil.copytree, works everywhere, no frills
    "copy_tool": "auto",
    "teracopy_path": r"C:\Program Files\TeraCopy\TeraCopy.exe",
    # Full path to whichever program copy_tool names, when it needs one.
    "copy_tool_path": "",
    # After copying, check the destination matches before deleting the source.
    # Turning this off makes a move faster and considerably less safe.
    "verify_after_copy": True,
    # Where the Backup button sends the database, as an rclone path such as
    # "gdrive:Backups/PC/mediavault". Empty means the feature is off. rclone
    # has to be installed and configured separately; see backup.py.
    "backup_target": "",
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


def save(settings):
    """Write settings back, keeping only keys we recognise."""
    clean = {k: v for k, v in settings.items() if k in DEFAULTS}
    merged = dict(DEFAULTS)
    merged.update(clean)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    return merged


# Copy programs worth offering if they are already installed. Each entry is
# (key, display name, exe name, [places to look]). robocopy and the built-in
# copier need no path, so they are always available.
#
# The listed folders are only the common cases. Both of these installers also
# happily drop themselves somewhere personal - FastCopy in particular defaults
# to a folder under the user profile - so find_installed() falls back to the
# uninstall registry, which records wherever it actually went.
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
    Where an installed program put its executable, according to the registry.

    Windows records every installer under an uninstall key holding, among
    other things, InstallLocation and DisplayIcon. That is the only reliable
    way to find a program that was installed somewhere unusual - a per-user
    folder, a second drive - without walking the whole disk.

    Returns a path, or None on any other platform or if nothing matches.
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
    """The path to a copy program, looked for in the three plausible places."""
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    on_path = shutil.which(exe_name)
    if on_path:
        return on_path
    return _registry_exe(display_name, exe_name)


def detect_copy_tools():
    """
    The copy programs available on this machine.

    Saves hunting through Program Files: anything found in its usual place is
    offered directly. Returns a list of dicts for the settings picker.
    """
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
    Which copier will actually be used, and whether it is available.

    Returns (name, path_or_None, note). 'auto' is resolved here so the UI can
    show what will really run rather than the word "auto".
    """
    settings = settings or load()
    choice = (settings.get("copy_tool") or "auto").lower()

    if choice in ("teracopy", "fastcopy", "custom"):
        path = settings.get("copy_tool_path") or settings.get("teracopy_path")
        if path and os.path.isfile(path):
            # A custom executable is driven the same way as TeraCopy: launch
            # it, then wait for the target to settle.
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
