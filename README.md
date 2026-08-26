# MediaVault

Keeps track of what shows and movies live on which drive, how much space each
drive has, and where to start deleting when you are running low. Works with
internal, external and network drives, and still shows you a drive's contents
while that drive is unplugged.

Drives are identified by a hidden `.mediavault_id` file at their root, not by
drive letter, so the same external HDD is recognised whatever letter Windows
gives it that day.

> Examples use `py`, the launcher that ships with Python on Windows. Use
> `python` if you do not have it.

## Setup

```
py -m pip install -r requirements.txt
py dashboard.py
```

Open <http://127.0.0.1:5151>. Press **Scan connected drives** to index them,
and again whenever you add, move or delete files.

## What you can do from the dashboard

- **Scan connected drives** — known drives plus any connected drive with a
  library folder, so a new drive is added by plugging it in and scanning.
- **See which drives are connected**, read live rather than from the last scan.
- **Set up drives** that do not have the folder layout yet.
- **Drill down** through any drive to individual files, even while unplugged.
- **Open anything** — folders in your file manager, files in their usual app.
- **Delete anything**, one item or a selection, to the Recycle Bin or
  permanently.
- **Rename or forget** a drive.
- **Tag titles**, one or many at once, and filter by tag.
- **Filter by backup state** — backed up, partly backed up, split across
  drives, or nowhere else.
- **Search** across every drive and jump to a result's place in the tree.
- **See what is backed up** — green shield for a complete copy elsewhere,
  amber for partial, purple when a title is merely *split* across drives and
  so protects nothing. Tag a title `partial-ok` if partial is intentional.
- **Find what to delete** from each drive's largest folders panel.
- **Move titles to another drive**, landing in the matching category.
- **Back up titles to another drive**, into the target's redundancy folder.
- **Set how each drive is treated** — SSD, HDD or USB, which sets its free
  space threshold (15% for an SSD, 10% for a mechanical disk). Tick **Cold
  storage** on a drive you have deliberately filled and only read from.
- **Back up the database** to any rclone destination. Set the target in
  Settings; needs [rclone](https://rclone.org) installed and configured.

## Moving and backing up

**Move to drive** copies to the target, checks the copy arrived, and only then
removes the originals:

- If the copy fails or comes up short, **nothing is deleted**.
- If the copy succeeds but the original cannot be removed, you are told, and
  the title is left in both places rather than silently half moved.
- Tags follow the title.

Verification compares file count and total size, which catches the failures
that actually happen without hashing hundreds of gigabytes.

**Back up to drive** copies instead of moving, into the target's redundancy
folder under the same category, creating either if missing:

```
I:\99_Redundancy\Anime\Attack on Golem
```

You can back up to the drive the title is already on. That protects against
deleting it by accident, not against the drive failing, and the target list
says so. Drives are tagged to help you choose:

| Tag                   | Meaning                                                 |
| --------------------- | ------------------------------------------------------- |
| `external`            | On the USB bus, so it can be kept away from the machine |
| `backups (n)`         | Already holds n backed up titles                        |
| `SSD` / `HDD` / `USB` | The drive type, which sets its free space threshold     |

`backups` counts titles actually in the redundancy folder, not whether the
folder exists.

### Copy tool

Set this with the **Settings** button, which lists what it found on your
machine. Anything else is picked with **Browse**.

| Value      | What it uses                                           |
| ---------- | ------------------------------------------------------ |
| `auto`     | robocopy if present, otherwise the built-in copier     |
| `robocopy` | ships with Windows, resumable, handles long paths well |
| `teracopy` | TeraCopy's own window shows the transfer               |
| `fastcopy` | FastCopy's own window shows the transfer               |
| `python`   | the built-in copier, works anywhere, no frills         |

A missing tool falls back to the built-in copier and says so in the log rather
than failing. Turning off **verify** makes transfers faster and considerably
less safe.

## Folder convention

```
D:\
└── Videos                     any top-level folder whose name starts with "Videos"
    ├── Anime                      auto-tagged: Anime
    │   └── Attack on Golem
    ├── Movies                     auto-tagged: Movie
    │   └── Reception
    ├── TV Shows                   auto-tagged: TV
    │   └── Better Call Paul
    │       └── Season 01
    └── Anime Movies               auto-tagged: Movie + Anime
        └── My Name

99_Redundancy                  any top-level folder whose name contains "redundancy"
├── Anime                      scanned, treated as a backup copy
│   └── Attack on Golem
├── Desktop                    skipped, not a media category
└── Google Drive               skipped, not a media category
```

- **Library root**: any top-level folder whose name starts with "Videos".
- **Category folders** sit one level below it and are never listed as largest
  folders themselves, only the titles inside them.
- **Titles** are the folder or file directly inside a category folder. This is
  the unit everything is built around — largest folders, backup detection and
  tagging all work at this level, however deeply the files are nested.
- **Redundancy folders**: scanned the same way but treated as backup copies.
  Only subfolders that look like media categories are scanned, keeping
  `Desktop` or `Google Drive` out of the index.

A redundancy copy on the *same* drive protects against deleting something by
accident, not against the drive failing.

If a connected drive does not have this layout, the dashboard can create it.

## Running it all the time

Have Windows start the dashboard at login. In **Task Scheduler**, choose
**Create Task** (not "Create Basic Task"):

| Tab      | Setting                                                                                                                     |
| -------- | --------------------------------------------------------------------------------------------------------------------------- |
| General  | Name it `MediaVault dashboard`. Select **Run only when user is logged on**. Leave **Run with highest privileges** unticked. |
| Triggers | New, **At log on**.                                                                                                         |
| Actions  | New. Program `pythonw.exe`, arguments `dashboard.py`, **Start in** set to this folder.                                      |
| Settings | Tick **If the task fails, restart every** 1 minute.                                                                         |

**"Run only when user is logged on" is not optional.** The alternative runs
the task in Windows session 0, which has no desktop. The dashboard works, but
anything it launches has nowhere to draw: opening a folder does nothing, and
playing a file gives you audio from an invisible window you cannot close
without Task Manager.

`pythonw.exe` runs without a console window; find it with:

```
py -c "import sys, os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))"
```

Running it unprivileged is deliberate: the dashboard can delete files, and an
unprivileged process cannot touch anything the rest of Windows protects.

A few things worth knowing:

- A running dashboard keeps serving the code it started with. **Restart the
  task after updating the code.**
- Look the process up by port, not command line — Task Scheduler often reports
  an empty command line, so filtering on `dashboard.py` misses it:
  `powershell "Get-Process -Id (Get-NetTCPConnection -LocalPort 5151 -State Listen).OwningProcess"`
- Stop it with
  `powershell "Stop-ScheduledTask -TaskName 'MediaVault dashboard'"`.
- Run a second copy on another port with
  `powershell "$env:MEDIAVAULT_PORT=5152; py dashboard.py"`.
- Idle cost is roughly 40 MB and no measurable CPU, because it waits on a
  socket rather than polling.

## Shortcuts

Optional. Two scripts put shortcuts in your Start Menu for the things you
would otherwise do through Task Scheduler or a terminal. Install both with:

```
powershell -File scripts\install-flow-shortcuts.ps1; powershell -File scripts\install-phone-shortcuts.ps1
```

Both take `-Uninstall` to remove what they added. They write to the per-user
Start Menu, so no administrator rights are needed and nothing outside your own
profile is touched.

Windows Start Menu search finds them, and so does
[Flow Launcher](https://www.flowlauncher.com/), whose Program plugin indexes
the Start Menu without any plugin to install. Flow only rescans periodically,
so press **Reload Plugin Data** in its settings to see them immediately.

### Dashboard

| Shortcut                          | What it does                                     |
| --------------------------------- | ------------------------------------------------ |
| **MediaVault Dashboard**          | Start the dashboard and open it in your browser  |
| **MediaVault Restart Dashboard**  | Restart it, so Python changes take effect        |
| **MediaVault Stop Dashboard**     | Stop it                                          |
| **MediaVault Dashboard Status**   | Is it running, for how long, and on which port   |

These share a prefix because they are four views of one thing and read better
listed together. They report through a message box rather than a console, so
nothing flashes on screen.

### Phones

Mount a phone's storage as a drive letter, so MediaVault can index it like any
other drive. Needs [rclone](https://rclone.org/) and
[WinFsp](https://winfsp.dev/) on the PC, and an SSH server on the phone -
[Termux](https://termux.dev/) with `openssh` is the usual way, since it is a
real OpenSSH and so can report how full the phone is.

| Shortcut          | What it does                                                        |
| ----------------- | ------------------------------------------------------------------- |
| **Add Phone**     | Ask for address, login, drive letter and volume name, then set it up |
| **Mount Phone**   | Mount it. The window stays open; closing it unmounts                 |
| **Unmount Phone** | Unmount it and stop rclone                                          |
| **Remove Phone**  | Forget a phone and delete its rclone remote                         |
| **List Phones**   | Which phones are set up, and which are mounted right now            |

These are named verb first, so typing `mount` reaches the thing that mounts
without a prefix in the way.

Mounting is deliberately on demand. Nothing runs in the background and there is
no scheduled task: between mounts there is no process at all, which is the only
way to be certain a mount is never competing for CPU while you are doing
something that cares. The open window is the honest signal that rclone is
running, and closing it is the plainest way to stop.

**Add Phone** tests the connection before saving, and discards the half-made
remote if it cannot reach the phone. Passwords are handed to rclone through a
pipe rather than a command line, and kept obscured in rclone's own config;
`scripts/phones.json` holds only the drive letter, volume name and path, and is
git-ignored either way.

All of this is equally usable without the shortcuts:

```
powershell -File scripts\phone.ps1 add
powershell -File scripts\phone.ps1 mount -Name <name>
powershell -File scripts\phone.ps1 list
```
