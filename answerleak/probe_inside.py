"""answerleak probe: runs INSIDE a task container, piped to python via stdin.

STDLIB ONLY, Python 3.6+ compatible (no walrus, dataclasses, capture_output=, text=).
Spec comes from env var ANSWERLEAK_SPEC (JSON), because stdin is the script itself.
Prints exactly one JSON object to stdout and always exits 0.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVES = 200
MAX_FUTURE_REFS = 100
MAX_HISTORY_COMMITS = 200  # per target path, oldest future commits first
GIT_TIMEOUT = 300
ARCHIVE_EXTS = (".conda", ".tar.bz2", ".whl", ".tar.gz", ".zip")
MAX_COPIES = 200
MAX_MATCHED_SHOWN = 50  # every matched line is kept for manual review (evidence files)
COMMENT_PREFIXES = ("#", "//", '"""', "'''", "*")
_WS = re.compile(r"\s+")


def is_distinctive_line(line):
    if len(line) < 20:
        return False
    if not re.search(r"[A-Za-z]", line):
        return False
    if line.startswith(COMMENT_PREFIXES):
        return False
    return True


def normalize(line):
    """Matching key: drop all whitespace and unify quote style, so a later
    black/reformat pass (spacing, ' -> ") does not hide an otherwise identical line."""
    return _WS.sub("", line).replace("'", '"')


def stripped_lines(text):
    """Set of normalized lines of a text (name kept for compatibility)."""
    return set(normalize(l) for l in text.splitlines())


def no_trailing_commas(s):
    """Drop commas that directly precede a closing bracket: 'f(a,b,)' -> 'f(a,b)'."""
    for close in ")]}":
        s = s.replace("," + close, close)
    return s


def distinctive(lines, original_text):
    """Same semantics as patchparse.distinctive (part A). Returns normalized keys."""
    original = stripped_lines(original_text)
    # Whole original with all whitespace (incl. line breaks) removed: a line that only re-wraps
    # existing code (e.g. a multi-line call collapsed onto one line) is not new code.
    # Trailing commas before a closing bracket are ignored for this check (black adds them when it
    # splits a call over several lines).
    flat = no_trailing_commas(normalize(original_text))
    out = []
    seen = set()
    for raw in lines:
        line = raw.strip()
        if not is_distinctive_line(line):
            continue
        key = normalize(line)
        if key in seen or key in original or (flat and no_trailing_commas(key).rstrip(",") in flat):
            continue
        seen.add(key)
        out.append(key)
    return out


def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def norm(p):
    return os.path.normcase(os.path.realpath(p))


def readable_by_nonroot(path):
    """Approximation: other-read bit on the file and other-execute bit on every
    parent directory. Ignores ACLs, group membership and mount options."""
    try:
        if not (os.stat(path).st_mode & stat.S_IROTH):
            return False
        d = os.path.dirname(os.path.abspath(path))
        while True:
            if not (os.stat(d).st_mode & stat.S_IXOTH):
                return False
            parent = os.path.dirname(d)
            if parent == d:
                return True
            d = parent
    except OSError:
        return False


def is_leak(found, total):
    # At least half of the distinctive lines AND at least 2 of them. Exception: when a file's
    # change has only ONE distinctive line, that single line is enough, so a file with a
    # one-line fix CAN become a LEAK on one line (manual review must judge how specific it is).
    # For files with 2+ distinctive lines, one generic line such as
    # `assert actual.attrs == self.attrs` that also occurs elsewhere can never make a LEAK alone.
    return total >= 1 and found >= max(min(total, 2), -(-total // 2))


LAYOUT_PREFIXES = ("src", "lib")


def module_parts(target_path):
    """Path components of a repo file as it would appear when installed: a leading src/ or lib/
    (src-layout repos) is dropped, e.g. lib/matplotlib/axes.py -> matplotlib/axes.py."""
    parts = [c for c in target_path.replace("\\", "/").split("/") if c]
    while len(parts) > 1 and parts[0] in LAYOUT_PREFIXES:
        parts = parts[1:]
    return parts


GENERIC_TOP_DIRS = ("tests", "test", "testing", "doc", "docs", "examples", "example",
                    "scripts", "benchmarks", "bench", "tools", "ci")


def is_same_module(target_path, full_path, hint):
    """True if full_path ends with the target's whole module path (e.g. .../xarray/tests/
    test_dataset.py), so h5py/tests/test_dataset.py is not taken as a copy of xarray's file.
    Paths that do not start with a package name -- repo-root files like test_requests.py, or
    files under a generic top-level folder like tests/test_utils.py -- also need the package
    hint in the directories ABOVE the matched suffix, so another package's tests/test_utils.py
    is not taken as a copy of requests' tests/test_utils.py."""
    parts = module_parts(target_path)
    if suffix_depth("/".join(parts), full_path) < len(parts):
        return False
    if len(parts) == 1 or parts[0].lower() in GENERIC_TOP_DIRS:
        above = [c for c in full_path.replace("\\", "/").split("/") if c][:-len(parts)]
        return bool(hint) and hint in "/".join(above).lower()
    return True


def suffix_depth(target_path, full_path):
    """Number of trailing path components shared by the target and a candidate."""
    a = [c for c in target_path.replace("\\", "/").split("/") if c]
    b = [c for c in full_path.replace("\\", "/").split("/") if c]
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def run_git(testbed, args, binary=False, input_bytes=None):
    cmd = ["git", "-c", "safe.directory=*"] + args
    kw = {"cwd": testbed, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
          "timeout": GIT_TIMEOUT}
    if input_bytes is None:
        kw["stdin"] = subprocess.DEVNULL  # never let git read the probe's stdin
    else:
        kw["input"] = input_bytes
    p = subprocess.run(cmd, **kw)
    out = p.stdout if binary else p.stdout.decode("utf-8", errors="replace")
    return p.returncode, out, p.stderr.decode("utf-8", errors="replace")


def git_ready(spec, errors):
    """True when /testbed is a git repo, git runs, and base_commit resolves."""
    testbed = spec.get("testbed", "/testbed")
    base = spec.get("base_commit", "")
    if not os.path.isdir(os.path.join(testbed, ".git")):
        errors.append("git: %s/.git not found" % testbed)
        return False
    if shutil.which("git") is None:
        errors.append("git: executable not on PATH")
        return False
    if not base:
        errors.append("git: base_commit missing")
        return False
    try:
        rc, _, err = run_git(testbed, ["cat-file", "-e", base + "^{commit}"])
    except Exception as e:
        errors.append("git: %r" % (e,))
        return False
    if rc != 0:
        errors.append("git: base_commit %s not in repo: %s" % (base, err.strip()[:200]))
        return False
    return True


def build_targets(spec, errors, use_git=False):
    """The 'original' of each target is the file at base_commit (from git) when
    possible, so a dirty or already-patched working tree cannot hide the fix."""
    testbed = spec.get("testbed", "/testbed")
    base = spec.get("base_commit", "")
    targets = []
    for t in spec.get("targets", []):
        path = t["path"]
        original = None
        source = "missing"
        if use_git:
            try:
                rc, text, _ = run_git(testbed, ["cat-file", "-p", "%s:%s" % (base, path)])
                if rc == 0:
                    original, source = text, "git"
                else:
                    original, source = "", "absent_at_base"  # file added by the patch
            except Exception as e:
                errors.append("git cat-file %s: %r" % (path, e))
        if original is None:
            try:
                original = read_text(os.path.join(testbed, path))
                source = "worktree"
            except (IOError, OSError):
                original = ""
        lines = distinctive(t.get("lines", []), original)
        targets.append({
            "path": path,
            "role": t.get("role", "fix"),
            "lines": lines,
            "total": len(lines),
            "lineset": set(lines),
            "own": norm(os.path.join(testbed, path)),
            "original": source,
            "orig_lines": stripped_lines(original) if original else set(),
        })
    return targets


def testbed_base_files(spec, errors):
    """Real paths of tracked /testbed files whose content equals base_commit.
    They are base-version source by construction, so a line match there is not a
    leak (e.g. a Django test line that also occurs in another tests/*/tests.py).
    Returns None if git cannot tell; the caller then only skips each target's own file."""
    testbed = spec.get("testbed", "/testbed")
    base = spec.get("base_commit", "")
    try:
        rc, out, err = run_git(testbed, ["ls-files", "-z"])
        if rc != 0:
            errors.append("git ls-files failed: %s" % err.strip()[:200])
            return None
        tracked = [p for p in out.split("\0") if p]
        # core.fileMode=false: images often chmod -R 777 /testbed; mode-only changes are not content.
        rc, out, err = run_git(testbed, ["-c", "core.fileMode=false", "diff", "--name-only",
                                         "--no-renames", "-z", base, "--"])
        if rc != 0:
            errors.append("git diff failed: %s" % err.strip()[:200])
            return None
        modified = set(p for p in out.split("\0") if p)
    except Exception as e:
        errors.append("git testbed files: %r" % (e,))
        return None
    return set(norm(os.path.join(testbed, p)) for p in tracked if p not in modified)


def count_found(target, text_lines):
    return len(target["lineset"] & text_lines)


def scan_fs(spec, targets, errors, skip_real=None):
    roots = spec.get("roots") or ["/"]
    exclude = [os.path.normcase(os.path.abspath(e)) for e in spec.get("exclude", ["/proc", "/sys", "/dev"])]
    hint = (spec.get("package_hint") or "").lower()
    testbed_real = norm(spec.get("testbed", "/testbed"))
    by_base = {}
    for t in targets:
        # every target, even with no distinctive lines: we still report other copies of it
        by_base.setdefault(os.path.basename(t["path"]), []).append(t)
    hits = []
    archives = []
    copies = []  # level 1: another copy of the same module path (issue #640's definition)

    def excluded(p):
        n = os.path.normcase(os.path.abspath(p))
        for e in exclude:
            if n == e or n.startswith(e.rstrip("\\/") + os.sep):
                return True
        return False

    def onerr(e):
        pass

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerr):
            dirnames[:] = [d for d in dirnames if not excluded(os.path.join(dirpath, d))]
            for fn in filenames:
                cands = by_base.get(fn)
                is_arch = (hint and len(archives) < MAX_ARCHIVES
                           and fn.lower().startswith(hint) and fn.lower().endswith(ARCHIVE_EXTS))
                if not cands and not is_arch:
                    continue
                full = os.path.join(dirpath, fn)
                if is_arch:
                    archives.append(full)
                if not cands:
                    continue
                try:
                    st = os.lstat(full)
                    # symlinks are reached via their target; FIFOs/devices would block or lie
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    size = st.st_size
                    if size > MAX_FILE_BYTES:
                        continue
                    real = norm(full)
                    if skip_real is not None:
                        if real in skip_real:
                            continue
                        todo = list(cands)
                    else:
                        todo = [t for t in cands if t["own"] != real]
                    if not todo:
                        continue
                    lines = stripped_lines(read_text(full))
                except (IOError, OSError):
                    continue
                in_testbed = real == testbed_real or real.startswith(testbed_real.rstrip("\\/") + os.sep)
                for t in todo:
                    depth = suffix_depth(t["path"], full)
                    if (not in_testbed and len(copies) < MAX_COPIES and t["orig_lines"]
                            and lines != t["orig_lines"] and is_same_module(t["path"], full, hint)):
                        copies.append({
                            "path": full, "target": t["path"], "role": t["role"],
                            "suffix_depth": depth, "size": size,
                            "readable_by_nonroot": readable_by_nonroot(full),
                        })
                    if t["total"] < 1:
                        continue
                    found = count_found(t, lines)
                    if found >= 1:
                        # Strict rule: a LEAK must be in a copy of the SAME module path (e.g.
                        # .../xarray/core/combine.py), or the task's own file. A same-named file at
                        # another path (e.g. a module moved upstream to xarray/structure/combine.py)
                        # is recorded with same_module=False and never counts as a LEAK.
                        same = real == t["own"] or is_same_module(t["path"], full, hint)
                        lines_ok = is_leak(found, t["total"])
                        hits.append({
                            "path": full, "target": t["path"], "role": t["role"],
                            "found": found, "total": t["total"], "size": size,
                            "readable_by_nonroot": readable_by_nonroot(full),
                            "in_testbed": in_testbed,
                            "suffix_depth": depth,
                            "same_module": same,
                            "lines_ok": lines_ok,
                            "leak": lines_ok and same,
                            # the matched (normalized) lines, for manual review
                            "matched": [l for l in t["lines"] if l in lines][:MAX_MATCHED_SHOWN],
                        })
    return hits, archives, copies


def _batch_blob_lines(testbed, names):
    """Map each '<rev>:<path>' name to the normalized line set of that blob (missing -> absent)."""
    if not names:
        return {}
    rc, out, _ = run_git(testbed, ["cat-file", "--batch-check"],
                         input_bytes=("\n".join(names) + "\n").encode("utf-8"))
    if rc != 0:
        raise RuntimeError("cat-file --batch-check rc=%s" % rc)
    name_to_sha = {}
    for name, line in zip(names, out.splitlines()):
        p = line.split()
        if len(p) == 3 and p[1] == "blob":
            name_to_sha[name] = p[0]
    shas = sorted(set(name_to_sha.values()))
    if not shas:
        return {}
    rc, raw, _ = run_git(testbed, ["cat-file", "--batch"], binary=True,
                         input_bytes=("\n".join(shas) + "\n").encode("ascii"))
    if rc != 0:
        raise RuntimeError("cat-file --batch rc=%s" % rc)
    blob_lines = {}
    pos = 0
    while pos < len(raw):
        nl = raw.index(b"\n", pos)
        head = raw[pos:nl].decode("ascii", "replace").split()
        pos = nl + 1
        if len(head) == 3:
            size = int(head[2])
            blob_lines[head[0]] = stripped_lines(raw[pos:pos + size].decode("utf-8", "replace"))
            pos += size + 1
    return dict((n, blob_lines[s]) for n, s in name_to_sha.items() if s in blob_lines)


def git_checks(spec, targets, errors):
    """Git channel. 'Future' = commits that descend from base_commit, or that are not
    its ancestors and were committed at/after it; reachable from refs, reflogs, or
    unreachable commit objects. leak evidence = the target's distinctive lines found
    in a future version of the target path (history_hits) or in an unreachable blob."""
    testbed = spec.get("testbed", "/testbed")
    base = spec.get("base_commit", "")
    res = {"future_commits": 0, "nonancestor_commits": 0, "future_refs": [],
           "unreachable_objects": 0, "unreachable_hits": [], "history_hits": []}
    active = [t for t in targets if t["total"] > 0]

    # 1. unreachable objects (reflog-held objects count as unreachable here)
    blobs, ucommits = [], []
    try:
        args = ["fsck", "--unreachable", "--no-reflogs", "--connectivity-only"]
        rc, out, err = run_git(testbed, args)
        if rc == 129:  # very old git without --connectivity-only
            rc, out, err = run_git(testbed, args[:-1])
        count = 0
        for line in out.splitlines():
            if line.startswith("unreachable"):
                count += 1
                p = line.split()
                if len(p) >= 3 and p[1] == "blob":
                    blobs.append(p[2])
                elif len(p) >= 3 and p[1] == "commit":
                    ucommits.append(p[2])
        res["unreachable_objects"] = count
        if rc != 0:
            # A ref pointing at the null sha (e.g. a dangling refs/remotes/origin/HEAD left by
            # image builds) makes fsck exit non-zero, but fsck still completes its object walk.
            # Treat that specific case as a warning; anything else stays a hard error.
            err_lines = [l.strip() for l in err.splitlines() if l.strip().startswith("error:")]
            null_ref = re.compile(r"^error: \S+: invalid sha1 pointer 0{40}$")
            if err_lines and all(null_ref.match(l) for l in err_lines):
                res["fsck_warnings"] = err_lines[:20]
            else:
                errors.append("git fsck rc=%s: %s" % (rc, err.strip()[:200]))
    except Exception as e:
        errors.append("git fsck: %r" % (e,))
    # Tips = unreachable commits + every ref that points at an existing object. We pass them
    # explicitly instead of `--all`, because images can contain a ref pointing at the null sha
    # (refs/remotes/origin/HEAD -> 0000...), which makes `rev-list --all` die with "bad object".
    tips = list(ucommits)
    try:
        rc, out, err = run_git(testbed, ["for-each-ref", "--format=%(objectname)"])
        shas = sorted(set(s for s in out.split() if s and set(s) != {"0"})) if rc == 0 else []
        if shas:
            rc2, out2, err2 = run_git(testbed, ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
                                      input_bytes=("\n".join(shas) + "\n").encode("ascii"))
            for line in out2.splitlines():
                p = line.split()
                if len(p) == 2 and p[1] != "missing":
                    tips.append(p[0])
        if rc != 0:
            errors.append("git for-each-ref (tips) failed: %s" % err.strip()[:200])
    except Exception as e:
        errors.append("git tips: %r" % (e,))
    tips_in = ("\n".join(tips) + "\n").encode("ascii") if tips else b""
    reflog_flag = ["--reflog"]

    def rev_list(extra, tail=()):
        # Try with reflogs; if a broken reflog entry breaks rev-list, retry without and say so.
        rc, out, err = run_git(testbed, ["rev-list"] + extra + reflog_flag + ["--stdin", "^" + base] + list(tail),
                               input_bytes=tips_in)
        if rc != 0 and reflog_flag:
            rc, out, err = run_git(testbed, ["rev-list"] + extra + ["--stdin", "^" + base] + list(tail),
                                   input_bytes=tips_in)
            if rc == 0:
                del reflog_flag[:]
                res.setdefault("git_warnings", []).append("reflogs skipped: rev-list --reflog failed")
        return rc, out, err

    # 2. future commits
    future = set()
    try:
        rc, out, err = run_git(testbed, ["log", "-1", "--format=%ct", base])
        if rc != 0:
            raise RuntimeError("git log base failed: %s" % err.strip()[:200])
        base_ct = int(out.strip())
        rc, out, err = rev_list(["--timestamp"])
        if rc != 0:
            raise RuntimeError("git rev-list failed: %s" % err.strip()[:200])
        nonanc = 0
        for line in out.splitlines():
            p = line.split()
            if len(p) == 2:
                nonanc += 1
                if int(p[0]) >= base_ct:
                    future.add(p[1])
        res["nonancestor_commits"] = nonanc
        rc, out, err = rev_list(["--ancestry-path"])
        if rc != 0:
            raise RuntimeError("git rev-list --ancestry-path failed: %s" % err.strip()[:200])
        future.update(out.split())  # descendants of base, whatever their dates
        res["future_commits"] = len(future)
    except Exception as e:
        errors.append("git future commits: %r" % (e,))

    # 3. refs pointing at future commits (annotated tags peeled one level)
    try:
        rc, out, err = run_git(testbed, ["for-each-ref", "--format=%(refname) %(objectname) %(*objectname)"])
        if rc != 0:
            errors.append("git for-each-ref failed: %s" % err.strip()[:200])
        else:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[-1] in future:
                    res["future_refs"].append(parts[0])
                    if len(res["future_refs"]) >= MAX_FUTURE_REFS:
                        break
    except Exception as e:
        errors.append("git for-each-ref: %r" % (e,))

    # 4. target lines in future versions of the target paths
    if future and active:
        by_path = {}
        for t in active:
            by_path.setdefault(t["path"], []).append(t)
        for path, ts in by_path.items():
            try:
                rc, out, err = rev_list([], tail=("--", path))
                if rc != 0:
                    errors.append("git rev-list %s: %s" % (path, err.strip()[:200]))
                    continue
                commits = [c for c in out.split() if c in future][::-1][:MAX_HISTORY_COMMITS]
                names = ["%s:%s" % (c, path) for c in commits]
                versions = _batch_blob_lines(testbed, names)
                for t in ts:
                    best = None
                    for c, name in zip(commits, names):
                        lines = versions.get(name)
                        if lines is None:
                            continue
                        found = count_found(t, lines)
                        if found >= 1 and (best is None or found > best["found"]):
                            best = {"commit": c, "target": t["path"], "role": t["role"],
                                    "found": found, "total": t["total"],
                                    "leak": is_leak(found, t["total"])}
                            if found == t["total"]:
                                break
                    if best:
                        best["commits_checked"] = len(commits)
                        res["history_hits"].append(best)
            except Exception as e:
                errors.append("git history %s: %r" % (path, e))

    # 5. stray unreachable blobs (capped)
    try:
        cap = int(spec.get("max_unreachable_blobs", 2000))
        for sha in blobs[:cap]:
            if not active:
                break
            try:
                rc, text, _ = run_git(testbed, ["cat-file", "-p", sha])
            except Exception:
                continue
            if rc != 0:
                continue
            lines = stripped_lines(text)
            for t in active:
                found = count_found(t, lines)
                if found >= 1:
                    res["unreachable_hits"].append({
                        "sha": sha, "target": t["path"], "role": t["role"],
                        "found": found, "total": t["total"],
                        "leak": is_leak(found, t["total"]),
                    })
        res["unreachable_blobs_checked"] = min(len(blobs), cap)
    except Exception as e:
        errors.append("git unreachable blobs: %r" % (e,))
    return res


def main():
    errors = []
    out = {
        "git": {"future_commits": 0, "future_refs": [], "unreachable_objects": 0, "unreachable_hits": [],
                "history_hits": []},
        "fs_hits": [], "archives": [], "other_copies": [], "targets_summary": [], "errors": errors,
        "python_version": sys.version.split()[0],
    }
    try:
        raw = os.environ.get("ANSWERLEAK_SPEC")
        if not raw:
            raise ValueError("ANSWERLEAK_SPEC not set")
        spec = json.loads(raw)
        use_git = git_ready(spec, errors)
        targets = build_targets(spec, errors, use_git=use_git)
        out["targets_summary"] = [{"path": t["path"], "role": t["role"], "total": t["total"],
                                   "original": t["original"]} for t in targets]
        skip_real = testbed_base_files(spec, errors) if use_git else None
        try:
            out["fs_hits"], out["archives"], out["other_copies"] = scan_fs(spec, targets, errors,
                                                                           skip_real=skip_real)
        except Exception as e:
            errors.append("fs scan: %r" % (e,))
        if use_git:
            try:
                out["git"] = git_checks(spec, targets, errors)
            except Exception as e:
                errors.append("git: %r" % (e,))
    except Exception as e:
        errors.append("fatal: %r" % (e,))
    try:
        sys.stdout.write(json.dumps(out) + "\n")
    except Exception as e:
        sys.stdout.write(json.dumps({"errors": ["output: %r" % (e,)]}) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
