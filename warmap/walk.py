"""A directory walk with a ceiling on the number of entries it looks at.

`Path.rglob` lists each directory in full before yielding anything, so a cap
on yielded paths cannot stop a flat folder of a million files from being read
end to end. This walks with `os.scandir`, which is lazy per directory, and
stops the moment the ceiling is reached, whether the entries sit in one folder
or a thousand. Directory symlinks are not followed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator


class BoundedWalk:
    """Iterate the files under `root`; `truncated` says whether the ceiling
    cut the walk short."""

    def __init__(self, root: Path, max_entries: int):
        self.root = Path(root)
        self.max_entries = max_entries
        self.scanned = 0
        self.truncated = False

    def __iter__(self) -> Iterator[Path]:
        pending = [str(self.root)]
        while pending:
            folder = pending.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        self.scanned += 1
                        if self.scanned > self.max_entries:
                            self.truncated = True
                            return
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(entry.path)
                                continue
                            if entry.is_file():
                                yield Path(entry.path)
                        except OSError:
                            continue
            except OSError:
                continue
