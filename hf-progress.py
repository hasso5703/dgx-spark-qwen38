"""Byte progress of a checkpoint download, for the three fetches of switch-model.sh.

snapshot_download counts FILES ("Fetching 422 files: 35%"), a 53 GB safetensors the same
as a 2 KB config, and through a 22-minute switch to a 135 GB checkpoint started from the
cockpit the Load window said "Step 1 of 2", which read as frozen (2026-10-05). While a
fetch runs, this says every few seconds how many bytes of the revision's files are in the cache,
against their total as the Hub lists them: whole blobs plus the .incomplete files
snapshot_download writes into, so a fetch that resumes is counted from the files it
already holds whole (the releases pinned here start a cut-off file over).
Bytes and rates are decimal (GB, MB/s), the way the Hub states repository sizes.

The part that counts and words needs nothing. reporting() needs huggingface_hub, and runs
wherever snapshot_download runs: the image and video lanes' venvs, the text lanes'
pinned SGLang image. It never raises and never holds a download up: without the sizes it
says so once and the download goes on as before. A file fetched over Xet (the one repo
switch-model.sh lets use it) lands whole at its end, so the count holds still while it
comes.
"""
import os
import threading
import time
from contextlib import contextmanager


def _field(obj, name):
    """An attribute of the Hub's dataclasses, or a key of the dicts older releases gave."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def blob_sizes(siblings):
    """{name of the file's blob in the cache: size} for the files listed. A file kept in LFS
    is stored under its sha256, any other under its git blob id, and files with the same
    content share one blob, counted once. Also returns how many files had no size."""
    out, unsized = {}, 0
    for s in siblings:
        lfs = _field(s, "lfs")
        name, size = (_field(lfs, "sha256"), _field(lfs, "size")) if lfs else (_field(s, "blob_id"), _field(s, "size"))
        if name and isinstance(size, int) and size >= 0:
            out[name] = size
        else:
            unsized += 1
    return out, unsized


def bytes_here(blob_dir, sizes):
    """Bytes of those blobs in the cache: the whole ones, and what has come of the others.
    A blob on its way is written beside its final name, as <name>.incomplete by older
    huggingface_hub releases and as <name>.<8 hex>.incomplete, one per process, by recent
    ones (1.33 on this box). Several can be there, one left by a killed fetch: the one
    written last is the live one."""
    try:
        entries = os.listdir(blob_dir)
    except OSError:
        return 0
    partial = {}
    for entry in entries:
        if not entry.endswith(".incomplete"):
            continue
        name = entry.split(".", 1)[0]
        try:
            st = os.stat(os.path.join(blob_dir, entry))
        except OSError:
            continue
        if name not in partial or st.st_mtime > partial[name][0]:
            partial[name] = (st.st_mtime, st.st_size)
    n = 0
    for name, size in sizes.items():
        try:
            n += min(os.path.getsize(os.path.join(blob_dir, name)), size)
        except OSError:
            if name in partial:
                n += min(partial[name][1], size)
    return n


def say_gb(n):
    return f"{n / 1e9:.1f} GB"


def say_left(seconds):
    s = max(1, int(round(seconds)))
    if s < 90:
        return f"about {s} s"
    if s < 3600:
        return f"about {round(s / 60)} min"
    return f"about {s // 3600} h {round((s % 3600) / 60):02d}"


def progress_line(here, total, rate):
    """One line: what is here, of what, and at the rate since this fetch started, how long
    the rest takes. The percentage is floored: 100 % is said only when it is all here."""
    pct = int(100 * here / total) if total else 100
    line = f"downloading: {here / 1e9:.1f} of {say_gb(total)} ({pct} %)"
    if rate > 0 and here < total:
        line += f", {rate / 1e6:.0f} MB/s, {say_left((total - here) / rate)} left"
    return line


def _watch(blob_dir, sizes, unsized, every, stop, say):
    try:
        total = sum(sizes.values())
        t0, h0 = time.monotonic(), bytes_here(blob_dir, sizes)
        say(f"checkpoint: {len(sizes)} files, {say_gb(total)} at this revision, {say_gb(h0)} already here"
            + (f" ({unsized} files the Hub gave no size for are not counted)" if unsized else ""))
        while not stop.wait(every):
            h = bytes_here(blob_dir, sizes)
            say(progress_line(h, total, (h - h0) / max(1e-9, time.monotonic() - t0)))
        say(f"checkpoint: {bytes_here(blob_dir, sizes) / 1e9:.1f} of {say_gb(total)} here")
    except Exception as e:  # noqa: BLE001 (a progress line is never worth a download)
        say(f"progress stopped ({type(e).__name__}); the download goes on")


def _say(line):
    print(line, flush=True)


@contextmanager
def reporting(repo, revision=None, allow_patterns=None, every=10.0, say=_say):
    """Say the byte progress of the block's fetch of `repo` every `every` seconds."""
    stop, thread = threading.Event(), None
    try:
        from huggingface_hub import HfApi, constants
        from huggingface_hub.file_download import repo_folder_name
        from huggingface_hub.utils import filter_repo_objects
        info = HfApi().model_info(repo, revision=revision, files_metadata=True)
        files = list(filter_repo_objects(info.siblings or [], allow_patterns=allow_patterns,
                                         key=lambda s: _field(s, "rfilename")))
        sizes, unsized = blob_sizes(files)
        blob_dir = os.path.join(constants.HF_HUB_CACHE, repo_folder_name(repo_id=repo, repo_type="model"), "blobs")
        if sizes:
            thread = threading.Thread(target=_watch, args=(blob_dir, sizes, unsized, every, stop, say), daemon=True)
            thread.start()
        else:
            say("the Hub listed no file sizes for this checkpoint: the download goes on without a byte count")
    except Exception as e:  # noqa: BLE001 (no sizes, no count: never a failed download)
        say(f"the Hub did not give this checkpoint's file sizes ({type(e).__name__}): the download goes on without a byte count")
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=5)
