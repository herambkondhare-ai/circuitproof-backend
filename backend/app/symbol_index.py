"""
KiCad symbol index (symbol_index.py)

Searchable index of every symbol in the installed KiCad libraries (~20k symbols), built from each
symbol's name, description and keywords. Lets the planner ground Nemotron's choices in parts that
really exist, instead of a small fixed catalog.

The index is built once (in the background at server start) and cached in backend/data/.
"""

import bisect
import glob
import json
import math
import os
import re
import threading
import time
from collections import Counter

TOP_SYMBOL = re.compile(r'^(?:\t|  )\(symbol "((?:[^"\\]|\\.)*)"', re.M)
EXTENDS = re.compile(r'\(extends "((?:[^"\\]|\\.)*)"')
SUB_UNIT = re.compile(r"_\d+_\d+$")
SKIP_LIBS = {"power", "Graphic"}
TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def _prop(chunk, name):
    m = re.search(r'\(property "%s" "((?:[^"\\]|\\.)*)"' % re.escape(name), chunk)
    return m.group(1) if m else ""


def tokens(text):
    return TOKEN.findall(str(text).lower())


def _library_files(dirs):
    """Yield (library nickname, [files]) for .kicad_sym files and .kicad_symdir folders."""
    seen = set()
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.kicad_sym"))):
            nick = os.path.splitext(os.path.basename(path))[0]
            if nick not in seen:
                seen.add(nick)
                yield nick, [path]
        for folder in sorted(glob.glob(os.path.join(d, "*.kicad_symdir"))):
            nick = os.path.basename(folder)[: -len(".kicad_symdir")]
            if nick not in seen:
                seen.add(nick)
                yield nick, sorted(glob.glob(os.path.join(folder, "*.kicad_sym")))


def scan_library(nick, files):
    entries = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        starts = [(m.start(), m.group(1)) for m in TOP_SYMBOL.finditer(text)]
        for i, (pos, name) in enumerate(starts):
            if SUB_UNIT.search(name):
                continue
            chunk = text[pos: starts[i + 1][0] if i + 1 < len(starts) else len(text)]
            ext = EXTENDS.search(chunk[:400])
            entries.append({
                "lib_id": f"{nick}:{name}", "name": name, "lib": nick,
                "desc": _prop(chunk, "Description"), "kw": _prop(chunk, "ki_keywords"),
                "extends": ext.group(1) if ext else "",
            })
    # derived symbols without their own description inherit the parent's
    by_name = {e["name"]: e for e in entries}
    for e in entries:
        if e["extends"] and not e["desc"] and e["extends"] in by_name:
            e["desc"] = by_name[e["extends"]]["desc"]
            e["kw"] = e["kw"] or by_name[e["extends"]]["kw"]
    return entries


def _stamp(dirs):
    files = [p for d in dirs for p in glob.glob(os.path.join(d, "*.kicad_sym*"))]
    return {"dirs": dirs, "count": len(files), "mtime": max((os.path.getmtime(p) for p in files), default=0)}


def build_cache(dirs, cache_path, stamp, progress=None):
    """Parse every library and write the JSON cache. Runs in a child process (see SymbolIndex.load_or_build)
    so the ~20k-symbol parse never competes with the web server for the GIL."""
    libs = [(nick, files) for nick, files in _library_files(dirs) if nick not in SKIP_LIBS]
    entries = []
    for i, (nick, files) in enumerate(libs, 1):
        entries += scan_library(nick, files)
        if progress is not None:
            progress.value = i / max(1, len(libs))
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stamp": stamp, "entries": entries}, f)
    os.replace(tmp, cache_path)
    return len(entries)


def _build_worker(dirs, cache_path, stamp, progress):
    build_cache(dirs, cache_path, stamp, progress)


class SymbolIndex:
    def __init__(self, dirs, cache_path):
        self.dirs = [d for d in dirs if d and os.path.isdir(d)]
        self.cache_path = cache_path
        self.entries, self.df, self.ready, self.error = [], Counter(), False, None
        self.postings, self.names = {}, []   # token -> entry indices; lower-case names (for substring matches)
        self._joined, self._offsets = "", []
        self.state, self.progress = "idle", 0.0   # idle | loading cache | building | indexing | ready | error
        self._shared_progress = None
        self._lock = threading.Lock()

    # ---------- build / load
    def _stamp(self):
        return _stamp(self.dirs)

    def _read_cache(self, stamp):
        if not os.path.isfile(self.cache_path):
            return None
        with open(self.cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        return cached["entries"] if cached.get("stamp") == stamp else None

    def load_or_build(self):
        try:
            started = time.time()
            stamp = self._stamp()
            self.state = "loading cache"
            entries = self._read_cache(stamp)
            if entries is None:
                self.state = "building"
                self._build_in_child(stamp)
                entries = self._read_cache(stamp)
                if entries is None:
                    raise RuntimeError("the index builder did not write a usable cache")
                print(f"[SymbolIndex] indexed {len(entries)} KiCad symbols in {time.time() - started:.1f}s")
            self.state = "indexing"
            self._set(entries)
            self.state, self.progress = "ready", 1.0
            print(f"[timing] symbol index ready: {time.time() - started:.2f}s ({len(entries)} symbols)", flush=True)
        except Exception as e:  # never take the server down because of the index
            self.state, self.error = "error", str(e)
            print(f"[SymbolIndex] failed: {e}")

    def _build_in_child(self, stamp):
        """Build in a separate process; fall back to this thread if processes can't be started."""
        try:
            import multiprocessing as mp
            ctx = mp.get_context("spawn")
            self._shared_progress = ctx.Value("d", 0.0, lock=False)
            proc = ctx.Process(target=_build_worker, daemon=True, name="symbol-index-builder",
                               args=(self.dirs, self.cache_path, stamp, self._shared_progress))
            proc.start()
        except Exception as e:
            print(f"[SymbolIndex] separate process unavailable ({e}); building in a thread")
            self._shared_progress = None
            build_cache(self.dirs, self.cache_path, stamp)
            return
        proc.join()
        if proc.exitcode != 0:
            raise RuntimeError(f"index builder process exited with code {proc.exitcode}")

    def build_in_background(self):
        threading.Thread(target=self.load_or_build, daemon=True, name="symbol-index").start()

    def _set(self, entries):
        df, postings = Counter(), {}
        for i, e in enumerate(entries):
            e["_name"] = e["name"].lower()
            e["_tok"] = set(tokens(e["name"] + " " + e["desc"] + " " + e["kw"]))
            df.update(e["_tok"])
            for t in e["_tok"]:
                postings.setdefault(t, []).append(i)
            if i % 500 == 0:
                time.sleep(0)  # let request threads run between chunks
        names = [e["_name"] for e in entries]
        # All names in one string (one per line) so substring matches are found by a single C-level regex scan.
        offsets, pos = [], 0
        for name in names:
            offsets.append(pos)
            pos += len(name) + 1
        with self._lock:
            self.entries, self.df, self.postings, self.names = entries, df, postings, names
            self._joined, self._offsets, self.ready = "\n".join(names), offsets, True

    def _name_hits(self, t):
        """Indices of symbols whose lower-case name contains t, in index order."""
        hits = []
        for m in re.finditer(re.escape(t), self._joined):
            i = bisect.bisect_right(self._offsets, m.start()) - 1
            if not hits or hits[-1] != i:
                hits.append(i)
        return hits

    # ---------- search
    def search(self, query, k=8):
        """Same scoring as a full scan, but only touches symbols that contain a query token
        (inverted index) or whose name contains it as a substring."""
        if not self.ready:
            return []
        q = [t for t in tokens(query) if len(t) > 1]
        if not q:
            return []
        n = len(self.entries) or 1
        scores = {}
        for t in q:
            idf = math.log(n / (1 + self.df.get(t, 0)))
            for i in self.postings.get(t, ()):
                scores[i] = scores.get(i, 0.0) + idf
            if len(t) >= 3:
                for i in self._name_hits(t):
                    scores[i] = scores.get(i, 0.0) + 2.0 * idf + (3.0 if self.names[i].startswith(t) else 0.0)
        scored = [(s, i) for i, s in scores.items() if s > 0]
        scored.sort(key=lambda x: (-x[0], len(self.entries[x[1]]["name"]), x[1]))
        return [{"lib_id": e["lib_id"], "name": e["name"], "lib": e["lib"], "desc": e["desc"],
                 "score": round(s, 2)} for s, e in ((s, self.entries[i]) for s, i in scored[:k])]

    def info(self):
        progress = self.progress
        if self.state == "building" and self._shared_progress is not None:
            progress = self._shared_progress.value
        return {"ready": self.ready, "symbols": len(self.entries), "error": self.error,
                "state": self.state, "progress": round(progress, 3)}
