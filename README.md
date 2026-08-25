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
- **Move titles to another drive.** Use the arrow icon on a title, or select
  several and use the bar at the bottom. Each lands in the matching category
  on the target (Anime to Anime, Movies to Movies). Targets that cannot fit
  the selection are marked. See "Moving things around" below.
- **Back up titles to another drive.** The shield icon copies a title into
  the target's redundancy folder and leaves the original alone.
- **Set how each drive is treated.** Each drive is labelled SSD, HDD or USB,
  read from its volume label, and you can override it. The type decides how
  much free space it should keep: 15 percent for an SSD, which needs the
  headroom to write efficiently and wear evenly, and 10 percent for a
  mechanical disk, which is fine to fill but gets slow and fragmented near
  the end. Tick **Cold storage** on a drive you have deliberately filled and
  only read from, and it stops warning you.

## Moving things around

Selecting titles and choosing **Move to drive** copies them to the target,
checks the copy arrived, and only then removes the originals. A move between
drives is never a rename, so:

- If the copy fails or comes up short, **nothing is deleted**. The original
  stays exactly where it is and the partial copy is left on the target for
  you to look at or remove.
- If the copy succeeds but the original cannot be removed, for example
  because something has the folder open, you are told, and the title is left
  in both places rather than silently half moved.
- Tags follow the title to its new home.

Verification compares file count and total size. That catches the failures
that actually happen, a copy that stopped partway or ran out of room, without
the cost of hashing hundreds of gigabytes.

### Backing up to another drive

**Back up to drive** copies instead of moving. The copy lands in the target
drive's redundancy folder, under the same category, and the original stays
where it is:

```
I:\99_Redundancy\Anime\Attack on Golem
```

Both the redundancy folder and the category are created if the target does
not have them. Once the copy is verified the title shows a green shield,
because a complete copy now exists elsewhere.

You can back up to the same drive the title is already on. That protects
against deleting it by accident, but not against the drive failing, and the
target list says so.

The drive list is tagged to help you choose:

| Tag | Meaning |
| --- | --- |
| `external` | On the USB bus, so it can be unplugged and kept away from the machine |
| `backups (n)` | Already holds n backed up titles, so it is where backups live |
| `SSD` / `HDD` / `USB` | The drive type, which sets its free space threshold |

`backups` counts titles actually sitting in the redundancy folder, not
whether the folder exists. A drive with an empty `99_Redundancy` is not
holding anything, so it does not claim to.

### Choosing the copy tool

Settings live in `config.json`, created next to the code on first run:

```json
{
  "copy_tool": "teracopy",
  "teracopy_path": "C:\\Program Files\\TeraCopy\\TeraCopy.exe",
  "verify_after_copy": true
}
```

`copy_tool` accepts:

| Value | What it uses |
| --- | --- |
| `auto` | robocopy if present, otherwise the built-in copier |
| `robocopy` | ships with Windows, resumable, handles long paths well |
| `teracopy` | TeraCopy's own window shows the transfer |
| `python` | the built-in copier, works anywhere, no frills |

Restart the dashboard after editing `config.json`.

The easiest way to set this is the **Settings** button on the dashboard. It
lists the copy programs found on your machine, so TeraCopy or FastCopy in
their usual place can just be picked. Anything else is chosen with
**Browse**, which opens a normal Windows file picker.

#### Using TeraCopy

Choose TeraCopy in Settings, or set `copy_tool` to `teracopy` and point
`teracopy_path` at the executable, usually
`C:\Program Files\TeraCopy\TeraCopy.exe`. TeraCopy opens its own
window with its own progress, and the dashboard shows progress at the same
time.

TeraCopy often hands work to an instance that is already running and exits
straight away, so the program exiting is not treated as the copy being
finished. MediaVault watches the target until it matches the source and
stops growing, then verifies as usual.

#### Using another copier

Any copier that takes a source and a destination on the command line can be
added. Copy the shape of `_copy_with_teracopy` in `moveops.py`, for example
FastCopy:

```python
def _copy_with_fastcopy(source, target, is_dir, tool_path, log, expected_bytes):
    args = [tool_path, "/cmd=diff", "/auto_close", f"/to={os.path.dirname(target)}", source]
    subprocess.Popen(args).wait()
    wait_until_settled(target, expected_bytes, log)
```

then add it to `copy_tree` in `moveops.py` and to `resolve_copy_tool` in
`config.py`. The rules to follow:

- **Block until the copy is really finished.** If the program returns early,
  call `wait_until_settled` the way the TeraCopy path does.
- **Never delete anything yourself.** MediaVault verifies the copy first and
  handles removing the source.
- Progress needs nothing from the tool. It is measured by watching the
  target folder fill.

If a chosen tool is missing, MediaVault falls back to the built-in copier and
says so in the log rather than failing. Setting `verify_after_copy` to false
makes transfers faster and considerably less safe.

### Watching progress

Move and copy jobs show a panel with overall progress, progress within the
title currently being copied, and a log. The panel stays in view as you
scroll. **Minimise** shrinks it to a pill in the bottom corner that keeps
showing progress, and clicking the pill brings the panel back. A running job
is never hidden completely.

When a job finishes the page refreshes on its own after a few seconds, since
sizes, free space and backup shields are all out of date until it does.
**Stay on this page** stops the countdown if you want to read the log first.
Whichever drives were expanded, and where you were scrolled to, are restored
after the refresh.

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
