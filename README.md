# MediaVault

A tiny local system to track metadata for what shows/movies live on which drive, how much
space each drive has, and where to start deleting when you're running low —
without maintaining a manual list by hand.

Drives are identified by a small hidden ID file written to their root
(`.mediavault_id`), not by drive letter — so the same external HDD is always
recognized correctly no matter what letter Windows assigns it that day.

## Folder convention

MediaVault expects (and auto-detects) a structure like this on each drive:

```
D:\
└── Videos                     <- any folder name STARTING WITH "Videos/00_Videos"
    ├── 00_Anime                   auto-tagged: Anime
    │   └── Attack on Golem
    ├── 01_Movies                  auto-tagged: Movie
    │   └── Reception
    ├── 02_TV Shows                auto-tagged: TV
    │   └── Better Call Paul
    │       └── Season 01
    └── 03_Anime Movies            auto-tagged: Movie + Anime
        └── My Name

99_Redundancy                  <- any folder name CONTAINING "redundancy"
├── 00_Anime                   included (numeric prefix XX_)
│   └── Attack on Golem
├── 02_TV Shows                included (numeric prefix XX_)
│   └── Better Call Paul       <- same title also under Videos = flagged ⧉
├── Desktop                    skipped by default
└── Google Drive               skipped by default
```

- **Library root**: any top-level folder whose name *starts with* "Videos"
  or "00_Videos". Override with `--root-prefix "your_prefix"` or
  use multiple prefixes: `--root-prefix "your_prefix,another_prefix"`.
- **Category folders** (`00_Anime`, `01_Movies`, `02_TV Shows`,
  `03_Anime Movies`, or your own names) are one level below the library root.
  They're never shown as "largest folders" candidates themselves — only the
  titles inside them are.
- **Titles** — the folder/file directly inside a category folder — are the unit
  everything is built around: the "largest folders" panel, duplicate
  detection, and tagging all operate at this level, regardless of how deep
  the actual files are nested
- **Redundancy/backup folders**: any top-level folder whose name *contains*
  "redundancy" (`99_Redundancy`, `Redundancy_Backup`, ...) is scanned the
  same way but treated as a backup copy, not a primary library. Override
  with `--redundancy-prefix`, or pass `--redundancy-prefix ""` to disable.
  **Inside a redundancy folder, only subfolders whose names start with a
  numeric prefix (`00_Anime`, `01_Movies`, `02_TV Shows`, …) are scanned.**
  This prevents non-media folders that happen to live at the same level from being
  indexed, which would otherwise bloat the database and skew redundancy
  reporting. To include a non-standard folder name, pass
  `--redundancy-include "FolderA,FolderB"` (exact names, case-insensitive).

## Setup

Open a terminal in this folder and run:

```
py -m pip install -r requirements.txt
```


> Examples below use the `py` launcher, standard on Windows Python installs.
> Swap in `python` or `python3` if that's what you use instead.

## Usage

**1. Scan each drive** (first time, give it a name; after that it's remembered):

```
py scanner.py D:\ --label "Seagate 4TB Blue"
py scanner.py E:\ --label "WD Passport 2TB"
py scanner.py F:\ --label "SanDisk 128GB microSD"
```

To also include a non-standard subfolder inside your redundancy root:

```
py scanner.py D:\ --redundancy-include "SomeOtherFolder"
```

Re-run the same command (label optional after the first time) any time you
add, move, or delete files on that drive

```
py scanner.py D:\
```

**2. View the dashboard:**

```
py dashboard.py
```

Open **http://127.0.0.1:5151** and leave that terminal window running in
the background. From here you can:

- **Drill down** — click any folder to expand it, all the way to individual files.
- **Tag titles** — click the `+` next to a title to add a tag (e.g. `K-Drama`,
  `Documentary`), click the `×` on a tag to remove it. Changes save
  instantly to the database.
- **Filter by tag**
- **Search** 
- **Spot duplicates** — the ⧉ icon marks a title that also exists somewhere
  else (typically your primary copy vs. its `99_Redundancy` backup, but it
  also catches accidental duplicates between two separate library drives).
  Hover it to see where the other copy is. If you delete one copy and
  rescan, the icon disappears from the copy that remains.
- **Find what to delete** — each drive's "largest folders" panel lists its
  biggest titles
