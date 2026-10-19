"""Built-in file search fallback for when Everything isn't available.

Mirrors the public surface of ``core.everything.EverythingSDK`` so the rest of
the app can keep calling the same methods. The walker is lazy: nothing is
indexed until the first query, and the index is cached for the process lifetime
keyed by absolute folder path. Switching to a different folder rebuilds.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Mesh export formats exposed by find_mesh_files. Mirrored from
# core.everything.MESH_EXTS — keep the two in sync (duplicated rather than
# imported so each backend module stays standalone).
_MESH_EXTS = (".psk", ".pskx", ".glb", ".fbx")


class WalkSearcher:
    """Drop-in replacement for ``EverythingSDK`` backed by ``os.walk``.

    Significantly slower than Everything on first query (a typical unpacked
    UE5 game folder has 50k–500k files; the initial walk runs in a few
    seconds to half a minute). Subsequent queries hit the in-memory index.
    """

    def __init__(self) -> None:
        self._index_lock = threading.Lock()
        self._indexed_folder: str = ""
        # Lower-cased extension (incl. leading dot) → list of Paths
        self._by_ext: dict[str, list[Path]] = {}
        # Lower-cased file basename → list of Paths
        self._by_name: dict[str, list[Path]] = {}
        # Special index for ``.props.txt`` files (compound extension)
        self._props_files: list[Path] = []

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _ensure_indexed(self, folder: str) -> None:
        """Walk *folder* once. Re-walks on folder change or if not yet built."""
        target = os.path.abspath(folder) if folder else ""
        with self._index_lock:
            if target == self._indexed_folder and self._by_ext:
                return
            self._by_ext = {}
            self._by_name = {}
            self._props_files = []
            self._indexed_folder = target
            if not target or not os.path.isdir(target):
                return
            log.info("WalkSearcher: indexing %s (this may take a moment)…", target)
            count = 0
            for root, _dirs, files in os.walk(target):
                for fname in files:
                    full = Path(root) / fname
                    lower = fname.lower()
                    self._by_name.setdefault(lower, []).append(full)
                    if lower.endswith(".props.txt"):
                        self._props_files.append(full)
                        # Also record under .txt so generic ext lookup still works.
                        self._by_ext.setdefault(".txt", []).append(full)
                    else:
                        ext = os.path.splitext(lower)[1]
                        if ext:
                            self._by_ext.setdefault(ext, []).append(full)
                    count += 1
            log.info("WalkSearcher: indexed %d files under %s", count, target)

    # ------------------------------------------------------------------
    # EverythingSDK-shaped API
    # ------------------------------------------------------------------

    def find_psk_files(self, folder: str = "") -> list[Path]:
        self._ensure_indexed(folder)
        return list(self._by_ext.get(".psk", [])) + list(self._by_ext.get(".pskx", []))

    def find_mesh_files(self, folder: str = "") -> list[Path]:
        """Find psk/pskx/glb/fbx files from the cached index (no re-walk)."""
        self._ensure_indexed(folder)
        out: list[Path] = []
        for ext in _MESH_EXTS:
            out.extend(self._by_ext.get(ext, []))
        return out

    def find_texture(self, texture_name: str, folder: str = "") -> list[Path]:
        return self.search_file(texture_name, extension="tga", folder=folder)

    def find_textures_in_folder(
        self,
        folder: str,
        ext: str = "tga",
        max_results: int = 5_000,
    ) -> list[Path]:
        """List every texture file under *folder* (recursive substring match).

        Mirrors :meth:`EverythingSDK.find_textures_in_folder`. Filters the
        cached index by path-prefix instead of re-walking; this still walks
        from scratch when called against a folder above the currently indexed
        root.
        """
        if not folder:
            return []
        # Index against the indexed root (or this folder, whichever covers it).
        target = os.path.abspath(folder)
        if (
            not self._indexed_folder
            or not target.lower().startswith(self._indexed_folder.lower())
        ):
            self._ensure_indexed(folder)
        ext_key = "." + ext.lstrip(".").lower()
        target_lower = target.lower().rstrip("\\/")
        out: list[Path] = []
        for p in self._by_ext.get(ext_key, []):
            sp = str(p).lower()
            if sp.startswith(target_lower + os.sep) or sp.startswith(target_lower + "/"):
                out.append(p)
                if len(out) >= max_results:
                    break
        return out

    def find_props_file(
        self, name: str, folder: str = "", max_results: int = 10_000
    ) -> list[Path]:
        self._ensure_indexed(folder)
        target_name = f"{name}.props.txt".lower()
        matches = [p for p in self._props_files if p.name.lower() == target_name]
        if len(matches) >= max_results:
            log.warning(
                "find_props_file (walker) hit max_results=%d for %r",
                max_results, name,
            )
            return matches[:max_results]
        return matches

    def search_file(
        self,
        filename: str,
        extension: str = "",
        folder: str = "",
        max_results: int = 50,
    ) -> list[Path]:
        self._ensure_indexed(folder)
        target_stem = filename.lower()
        if extension:
            ext = "." + extension.lstrip(".").lower()
            candidates = self._by_ext.get(ext, [])
            target = f"{target_stem}{ext}"
            return [p for p in candidates if p.name.lower() == target][:max_results]
        # No extension specified — match by stem across any extension.
        out: list[Path] = []
        for fname, paths in self._by_name.items():
            stem = os.path.splitext(fname)[0]
            # Strip the second extension segment for ``.props.txt`` files
            if fname.endswith(".props.txt"):
                stem = fname[: -len(".props.txt")]
            if stem == target_stem:
                out.extend(paths)
                if len(out) >= max_results:
                    return out[:max_results]
        return out

    def search(self, query: str, max_results: int = 100, sort: int = 0) -> list[str]:
        """Compatibility shim — accepts an Everything-style query string.

        We don't try to parse the full Everything syntax. Anything that calls
        this directly (instead of the typed helpers) won't be accelerated by
        the walker — return an empty list so callers degrade gracefully.
        """
        log.debug("WalkSearcher.search(%r) is unsupported — returning []", query)
        return []

    def test_connection(self) -> tuple[bool, str]:
        return True, "Built-in walker (Everything not detected)"

    def test_folder_search(self, folder: str) -> tuple[int, str]:
        self._ensure_indexed(folder)
        total = sum(len(v) for v in self._by_ext.values())
        if total <= 0:
            return 0, f"No files indexed under: {folder}"
        # Pick any sample for the message
        sample = ""
        for paths in self._by_ext.values():
            if paths:
                sample = str(paths[0])
                break
        return total, f"Indexed {total:,} files, e.g.: {sample}"


class PlocateSearcher:
    """Linux backend backed by ``plocate`` (or ``mlocate``/``locate``).

    plocate is the standard Linux equivalent of Everything: an indexed
    filename database kept fresh by ``updatedb`` (systemd timer / cron).
    Queries run through ``subprocess`` with NUL-delimited output — no shell,
    no pip dependencies, no retained buffers between calls.

    plocate matches *substrings anywhere in the path*, so helpers narrow
    results client-side (filename equality / extension / folder subtree) to
    keep semantics close to the Everything queries this replaces.

    The public surface is duck-typed with ``EverythingSDK`` and
    ``WalkSearcher`` so callers can treat backends interchangeably.
    """

    # Generous pre-filter limit — results are narrowed client-side afterwards.
    _PREFILTER_LIMIT = 50_000
    _QUERY_TIMEOUT = 20.0  # seconds

    def __init__(self) -> None:
        # Prefer plocate; plain locate/mlocate are slower but compatible.
        self._bin: str = shutil.which("plocate") or shutil.which("locate") or ""

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _run_raw(self, pattern: str, limit: int) -> tuple[int, str, str]:
        """Run one locate query. Returns (returncode, stdout, stderr)."""
        if not self._bin:
            return 127, "", "no locate binary found"
        try:
            proc = subprocess.run(
                [self._bin, "-0", "-i", "-l", str(limit), pattern],
                capture_output=True,
                timeout=self._QUERY_TIMEOUT,
                check=False,
            )
            return proc.returncode, proc.stdout.decode("utf-8", "replace"), \
                proc.stderr.decode("utf-8", "replace")
        except FileNotFoundError as e:
            return 127, "", str(e)
        except PermissionError as e:
            return 126, "", str(e)
        except subprocess.TimeoutExpired:
            log.warning("plocate: query timed out for pattern %r", pattern)
            return 124, "", "timeout"

    def _run(self, pattern: str, limit: int) -> list[Path]:
        """Run one locate query and return matching paths (best-effort).

        Errors are logged, never raised — a broken locate database must not
        crash any caller.
        """
        rc, out, err = self._run_raw(pattern, limit)
        if rc == 1:
            # plocate: exit 1 = no match. stderr may hint at a stale db.
            if err.strip():
                log.debug("plocate: no match for %r (%s)", pattern, err.strip())
            return []
        if rc not in (0, 1):
            log.warning(
                "plocate: exit %d for %r — %s", rc, pattern, err.strip()
            )
            return []
        return [Path(x) for x in out.split("\0") if x]

    @staticmethod
    def _in_folder(folder: str, p: Path) -> bool:
        """True when *p* is inside (subtree of) *folder*. Empty folder = any."""
        if not folder:
            return True
        target = os.path.abspath(folder)
        sp = os.path.abspath(str(p))
        return sp.startswith(target + os.sep)

    @staticmethod
    def _walk_folder(folder: str, exts: tuple[str, ...]) -> list[Path]:
        """Scoped ``os.walk`` for entries the plocate db doesn't know yet.

        ``updatedb`` can lag behind files that were just unpacked, otherwise
        a freshly-opened project could show zero results. Only walks this
        folder, so the cost is bounded.
        """
        target = os.path.abspath(folder)
        if not target or not os.path.isdir(target):
            return []
        log.info(
            "plocate: no indexed hits under %s — walking the folder directly "
            "(run 'sudo updatedb' to refresh the index)", target,
        )
        out: list[Path] = []
        for root, _dirs, files in os.walk(target):
            for fname in files:
                if not exts or fname.lower().endswith(exts):
                    out.append(Path(root) / fname)
        return out

    # ------------------------------------------------------------------
    # EverythingSDK-shaped API
    # ------------------------------------------------------------------

    def find_psk_files(self, folder: str = "") -> list[Path]:
        # '.psk' as a substring also hits '.pskx' names; post-filter splits them.
        paths = self._run(".psk", self._PREFILTER_LIMIT)
        out: list[Path] = [
            p for p in paths
            if p.name.lower().endswith((".psk", ".pskx"))
            and self._in_folder(folder, p)
        ]
        if folder and not out:
            # Stale db → try a direct, folder-scoped walk before giving up.
            out = self._walk_folder(folder, (".psk", ".pskx"))
        return out

    def find_mesh_files(self, folder: str = "") -> list[Path]:
        """Find psk/pskx/glb/fbx files via plocate runs, folder-scoped.

        Each extension gets its own locate query because locate matches
        substrings anywhere in the path (a ''.psk'' pattern also returns
        ''.pskx'' names). The exact ``p.suffix`` filter below splits them
        apart so every file is emitted exactly once.
        """
        out: list[Path] = []
        for ext in _MESH_EXTS:
            paths = self._run(ext, self._PREFILTER_LIMIT)
            out.extend(
                p for p in paths
                if p.suffix.lower() == ext and self._in_folder(folder, p)
            )
        if folder and not out:
            # Stale db → try a direct, folder-scoped walk before giving up.
            out = self._walk_folder(folder, _MESH_EXTS)
        return out

    def find_texture(self, texture_name: str, folder: str = "") -> list[Path]:
        return self.search_file(texture_name, extension="tga", folder=folder)

    def find_textures_in_folder(
        self,
        folder: str,
        ext: str = "tga",
        max_results: int = 5_000,
    ) -> list[Path]:
        """List every texture file under *folder* (recursive match)."""
        if not folder:
            return []
        ext = ext.lstrip(".").lower()
        paths = self._run("." + ext, self._PREFILTER_LIMIT)
        out: list[Path] = [
            p for p in paths
            if p.suffix.lower() == "." + ext and self._in_folder(folder, p)
        ][:max_results]
        if not out:
            out = self._walk_folder(folder, ("." + ext,))[:max_results]
        return out

    def find_props_file(
        self, name: str, folder: str = "", max_results: int = 10_000
    ) -> list[Path]:
        """Find ``<name>.props.txt`` files by stem, optionally folder-scoped."""
        paths = self._run(f"{name}.props.txt", self._PREFILTER_LIMIT)
        target = f"{name}.props.txt".lower()
        matches = [
            p for p in paths
            if p.name.lower() == target and self._in_folder(folder, p)
        ]
        if len(matches) >= max_results:
            log.warning(
                "find_props_file (plocate) hit max_results=%d for %r",
                max_results, name,
            )
            return matches[:max_results]
        return matches

    def search_file(
        self,
        filename: str,
        extension: str = "",
        folder: str = "",
        max_results: int = 50,
    ) -> list[Path]:
        """Search for a file by stem, optional extension, optional folder."""
        stem = filename.lower()
        if extension:
            ext = extension.lstrip(".").lower()
            pattern = f"{filename}.{ext}"  # argv to subprocess — no shell
            paths = self._run(pattern, self._PREFILTER_LIMIT)
            target_name = f"{stem}.{ext}"

            def matches(p: Path) -> bool:
                return p.name.lower() == target_name and self._in_folder(folder, p)

            walk_exts: tuple[str, ...] = ("." + ext,)
        else:
            # No extension specified — match by stem across any extension.
            paths = self._run(filename, self._PREFILTER_LIMIT)

            def matches(p: Path) -> bool:
                name = p.name.lower()
                base = (
                    name[: -len(".props.txt")]
                    if name.endswith(".props.txt")
                    else os.path.splitext(name)[0]
                )
                return base == stem and self._in_folder(folder, p)

            walk_exts = ()

        out: list[Path] = []
        for p in paths:
            if not matches(p):
                continue
            out.append(p)
            if len(out) >= max_results:
                break
        if folder and not out:
            # Stale db → direct folder walk with the same matcher.
            for w in self._walk_folder(folder, walk_exts):
                if matches(w):
                    out.append(w)
                    if len(out) >= max_results:
                        break
        return out

    def search(self, query: str, max_results: int = 100, sort: int = 0) -> list[str]:
        """Compatibility shim — accepts an Everything-style query string.

        Same policy as ``WalkSearcher.search``: the full Everything query
        syntax isn't translated here. Typed helpers above are accelerated.
        """
        log.debug("PlocateSearcher.search(%r) is unsupported — returning []", query)
        return []

    def test_connection(self) -> tuple[bool, str]:
        """Check the locate binary exists and its database answers."""
        if not self._bin:
            return False, (
                "plocate/locate not found. Install plocate ('sudo apt install "
                "plocate') for indexed search, or the built-in walker will be used."
            )
        # '/' as a substring occurs in every path — a working db matches it.
        rc, out, err = self._run_raw("/", 1)
        matches = [x for x in out.split("\0") if x]
        if matches:
            return True, f"{self._bin} backend connected"
        if "No such file" in err or "not exist" in err or "Permission denied" in err:
            return False, (
                f"{self._bin} is installed but its database is missing or "
                f"unreadable — run 'sudo updatedb' to build it. ({err.strip()})"
            )
        return False, f"{self._bin} query failed: {err.strip()}"

    def test_folder_search(self, folder: str) -> tuple[int, str]:
        """Count indexed hits inside *folder*. Returns (count, message)."""
        if not folder:
            return 0, "No folder given"
        paths = [
            p for p in self._run(os.path.abspath(folder), self._PREFILTER_LIMIT)
            if self._in_folder(folder, p)
        ]
        if not paths:
            found = self._walk_folder(folder, ())  # diagnostic-only walk
            if found:
                return len(found), (
                    f"Not yet in the locate index, but {len(found):,} files exist "
                    f"under: {folder} (run 'sudo updatedb')"
                )
            return 0, f"No files found under: {folder}"
        return len(paths), f"Found files, e.g.: {paths[0]}"
