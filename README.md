# MediaVault

A small local tool that keeps track of what shows and movies live on which
drive, how much space each drive has, and where to start deleting when you
are running low. It works for internal and external drives, and it still
shows you a drive's contents while that drive is unplugged.

Drives are identified by a small hidden file written to their root
(`.mediavault_id`), not by drive letter, so the same external HDD is
recognised correctly no matter what letter Windows gives it that day.

> Examples use `py`, the launcher that ships with Python on Windows. If `py`
> is not on your machine, use `python` instead.

## Setup

```
py -m pip install -r requirements.txt
```

## Running it

```
py dashboard.py
```

Then open <http://127.0.0.1:5151>. Everything happens from there. Press
**Scan connected drives** to index them. Run it again whenever you add, move,
or delete files.

## What you can do from the dashboard

- **Scan connected drives.** Indexes every connected drive in one go, showing
  progress per drive. It picks up drives MediaVault already knows about, plus
  any connected drive with a library folder on it, so a new drive is added
  just by plugging it in and scanning. Only one scan runs at a time, and the
  page stays usable while it works.
- **See which drives are connected**, with their current drive letter, read
  live rather than from the last scan.
- **Set up drives** that do not have the folder layout yet. Pick the drives
  from a list and it creates the folders, then scans them.
- **Drill down** through any drive to individual files, even while the drive
  is unplugged.
- **Open anything.** Folders open in your file manager, files in whatever
  application normally plays them.
- **Delete anything**, one item or a whole selection, either to the Recycle
  Bin or permanently. You choose which in the confirmation, since the Recycle
  Bin is undoable but does not free space until emptied. The delete control
  sits at the far right of a row, apart from the open button, so the
  destructive action is nowhere near the everyday one.
- **Rename or forget** a drive.
- **Tag titles**, one at a time or many at once, and filter by tag.
- **Filter by backup state.** Chips at the top narrow the tree to titles that
  are backed up, only partly backed up, split across drives, or exist nowhere
  else, so you can review one category at a time. Combines with the tag
  filters. Drives and folders with no matches drop out, leaving only what you
  filtered for.
- **Search** across every drive and jump from a result to its place in the
  tree. Use the up and down arrows to move through results, enter to jump,
  escape to close.
- **See what is backed up.** Titles that exist in more than one place get a
  shield: green when a complete copy exists somewhere, amber when only part
  of it does, showing counts such as "3 of 30 items". A purple icon means the
  same title is *split* across drives with no overlap, which looks like a
  copy but protects nothing. Click any entry to jump to that copy. Tag a
  title `partial-ok` if it is only partly backed up on purpose.
- **Find what to delete** from each drive's largest folders panel. Click any
  entry to jump to it in the tree, where the open, tag and delete controls
  are.

## Folder convention

MediaVault expects a structure like this on each drive:

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
├── TV Shows                   scanned
│   └── Better Call Paul       same title also under Videos, so it is flagged
├── Desktop                    skipped, not a media category
└── Google Drive               skipped, not a media category
```

- **Library root**: any top-level folder whose name starts with "Videos".
- **Category folders** (`Anime`, `Movies`, `TV Shows`, `Anime Movies`) sit one
  level below the library root. They are never listed as "largest folders"
  themselves, only the titles inside them are.
- **Titles** are the folder or file directly inside a category folder. This is
  the unit everything is built around: the largest folders panel, backup
  detection, and tagging all work at this level, however deeply the actual
  files are nested.
- **Redundancy folders**: any top-level folder whose name contains
  "redundancy" is scanned the same way but treated as a backup copy rather
  than a primary library. Inside one, only subfolders that look like media
  categories are scanned, which keeps folders like `Desktop` or `Google Drive`
  out of the index.

  Note that a redundancy copy on the *same* physical drive protects against
  deleting something by accident, not against the drive failing. For that,
  the copy has to be on a different drive.

If a connected drive does not have this layout yet, the dashboard can create
it for you.

## Running it all the time

Have Windows start the dashboard at login, so it is always at
<http://127.0.0.1:5151> with no terminal to keep open. Bookmark that address;
it never changes.

In **Task Scheduler**, choose **Create Task** (not "Create Basic Task"):

| Tab | Setting |
| --- | --- |
| General | Name it `MediaVault dashboard`. Select **Run only when user is logged on**. Leave **Run with highest privileges** unticked. |
| Triggers | New, **At log on**. |
| Actions | New. Program `pythonw.exe`, arguments `dashboard.py`, **Start in** set to this folder. |
| Settings | Tick **If the task fails, restart every** 1 minute. |

**"Run only when user is logged on" is not optional.** The other choice,
"Run whether user is logged on or not", runs the task in Windows session 0,
which has no desktop. The dashboard itself works, but anything it launches
has nowhere to draw: opening a folder does nothing, and playing a file gives
you audio from an invisible window you cannot close without Task Manager.

`pythonw.exe` runs without a console window. Find its path with:

```
py -c "import sys, os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))"
```

Running it unprivileged is deliberate: the dashboard can delete files, and an
unprivileged process cannot touch anything the rest of Windows protects.

### Managing it

Check whether it is up:

```
py -c "import socket; s=socket.socket(); print('running' if s.connect_ex(('127.0.0.1',5151))==0 else 'not running')"
```

Stop it:

```
powershell "Stop-ScheduledTask -TaskName 'MediaVault dashboard'"
```

Start it again from Task Scheduler ("Run" on the task), or `py dashboard.py`.

A few things worth knowing:

- A running dashboard keeps serving the code it started with. **Restart the
  task after updating the code.**
- Look the process up by port, not command line. Task Scheduler often reports
  an empty command line, so filtering on `dashboard.py` misses it:
  `powershell "Get-Process -Id (Get-NetTCPConnection -LocalPort 5151 -State Listen).OwningProcess"`
- To run a second copy without disturbing the first, set a different port:
  `powershell "$env:MEDIAVAULT_PORT=5152; py dashboard.py"`
- It costs nothing while idle, roughly 40 MB of memory and no measurable CPU,
  because it waits on a socket rather than polling. Scans are heavy while
  they run, which is why they are something you trigger rather than schedule.
