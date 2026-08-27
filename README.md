# MediaVault

Keeps track of what shows and movies live on which drive, how much space each
drive has, and where to start deleting when you are running low. Works with
internal, external and network drives, and still shows a drive's contents
while it is unplugged.

Drives are identified by a hidden `.mediavault_id` file at their root, not by
drive letter, so the same external HDD is recognised whatever letter Windows
gives it.

## Setup

```
py -m pip install -r requirements.txt
py dashboard.py
```

Open <http://127.0.0.1:5151>. Press **Scan connected drives** to index them,
and again whenever you add, move or delete files.

## What the dashboard does

- **Scan** known drives, plus any connected drive with a library folder.
- **Set up** a drive that has no library folder yet.
- **Drill down** to individual files, even while the drive is unplugged.
- **Open** folders in your file manager and files in their usual app.
- **Delete** to the Recycle Bin or permanently, one item or a selection.
- **Move** a title to another drive, landing in the matching category.
- **Back up** a title into another drive's redundancy folder.
- **Tag** titles, one or many, and filter by tag. Two tags change behaviour:
  **★ Star** marks a title worth protecting, **▶ Watching** marks one in use.
- **Search** across every drive and jump to a result in the tree.
- **See what is backed up**: green shield for a complete copy elsewhere,
  amber for partial, purple when a title is merely split across drives.
- **Filter and sort** drives by type, and filter titles by backup state.
- **Spot old folder layouts**, such as an anime with its seasons buried
  inside it, flagged with a wrench so you can tidy them up.
- **Get suggestions** for what to move or back up, each with its reasoning.
- **Set each drive's type**, which sets how full it is allowed to get.
- **Back up the database** to any rclone destination.
- **Rename or forget** a drive.

## Suggestions

The **Suggestions** button proposes what is worth moving or copying. Nothing
happens until you tick something, and ticking several destinations queues
them to run one after another.

| Kind      | Why it appears                                |
| --------- | --------------------------------------------- |
| `relief`  | A drive is past the free space it should keep |
| `protect` | A starred title exists in only one place      |
| `reclaim` | An SSD is holding bulk media                  |

Titles added in the last 21 days are left alone, and anything tagged
**Watching** is never proposed for a move.

Moves prefer mechanical disks, since only writes wear flash and a hard disk
does not wear from writes at all. Backups rank the other way, preferring
whichever drive holds fewest already, so one failure cannot take out most of
your protection. Cards and USB sticks are never suggested as a home for the
only copy of anything.

## Moving and backing up

A move copies, checks the copy arrived, and only then removes the original.
If anything goes wrong nothing is deleted. Verification compares file count
and total size. Tags follow the title.

A backup copies instead, into the target's redundancy folder under the same
category:

```
I:\99_Redundancy\Anime\Attack on Golem
```

Backing up to the drive a title is already on protects against deleting it by
accident, not against the drive failing, and the target list says so.

Set the copy program in **Settings**: `auto`, `robocopy`, `teracopy`,
`fastcopy`, or the built-in `python` copier. A missing tool falls back to the
built-in one and says so rather than failing.

## Folder convention

```
D:\
├── Videos                     any top-level folder starting with "Videos"
│   ├── Anime                      auto-tagged: Anime
│   ├── Movies                     auto-tagged: Movie
│   ├── TV Shows                   auto-tagged: TV
│   └── Anime Movies               auto-tagged: Movie + Anime
└── 99_Redundancy              any top-level folder containing "redundancy"
    └── Anime                      scanned, treated as backup copies
```

A **title** is the folder or file directly inside a category folder. That is
the unit everything is built around: largest folders, backup detection and
tagging all work at this level, however deeply the files are nested.

Inside a redundancy folder only recognisable media categories are scanned, so
`Desktop` or `Google Drive` stay out of the index. If a connected drive has
no such layout, the dashboard can create it.

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
in session 0, which has no desktop, so opening a folder does nothing and
playing a file gives audio from an invisible window you cannot close.

Running it unprivileged is deliberate: the dashboard can delete files, and an
unprivileged process cannot touch what the rest of Windows protects.

- A running dashboard serves the code it started with. **Restart the task
  after updating the code.**
- Look the process up by port, not command line, since Task Scheduler often
  reports an empty one:
  `powershell "Get-Process -Id (Get-NetTCPConnection -LocalPort 5151 -State Listen).OwningProcess"`
- Stop it with `powershell "Stop-ScheduledTask -TaskName 'MediaVault dashboard'"`.
- `py dashboard.py --dev` runs an auto-reloading copy on 5152 beside it.
- Idle cost is about 40 MB and no measurable CPU.

## Shortcuts

Optional Start Menu shortcuts for what you would otherwise do through Task
Scheduler or a terminal. Install both with:

```
powershell -File scripts\install-flow-shortcuts.ps1; powershell -File scripts\install-phone-shortcuts.ps1
```

Both take `-Uninstall`. They write to the per-user Start Menu, so no
administrator rights are needed. Windows search finds them, as does
[Flow Launcher](https://www.flowlauncher.com/), whose Program plugin indexes
the Start Menu already.

**Dashboard**: start, restart, stop and status, each reporting through a
message box rather than a console.

**Phones**: add, mount, unmount, remove and list. Mounts a phone's storage as
a drive letter so MediaVault can index it like any other drive. Needs
[rclone](https://rclone.org/) and [WinFsp](https://winfsp.dev/) on the PC and
an SSH server on the phone, usually [Termux](https://termux.dev/) with
`openssh`, since that is a real OpenSSH and can report how full the phone is.

Mounting is on demand by design: between mounts there is no process at all,
and the open window is the honest signal that rclone is running. **Add Phone**
tests the connection before saving and discards a remote it cannot reach.
Passwords go to rclone through a pipe rather than a command line, and
`scripts/phones.json` holds only the drive letter, volume name and path.

All of it works without the shortcuts:

```
powershell -File scripts\phone.ps1 add
powershell -File scripts\phone.ps1 mount -Name <name>
```
