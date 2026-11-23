"""Run the in-container probe for each task and turn its output into a verdict."""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PYTHONS = ("/opt/miniconda3/bin/python3", "python3")
PLATFORM = "linux/amd64"
# Windows CreateProcess caps a single env var / command line near 32k chars.
SPEC_LIMIT = 30_000 if sys.platform == "win32" else 100_000
TRUNCATE_STEPS = (200, 50, 10)


def _run(cmd, input_text=None, timeout=900, env=None):
    """Single choke point for subprocess calls. Returns (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except FileNotFoundError as e:
        return 127, "", "executable file not found: %s" % e
    return p.returncode, p.stdout, p.stderr


def probe_source():
    return (Path(__file__).parent / "probe_inside.py").read_text(encoding="utf-8")


def build_spec(task):
    from . import patchparse

    targets = []
    for role, key in (("fix", "patch"), ("tests", "test_patch")):
        for path, lines in patchparse.added_lines(task.get(key) or "").items():
            # Pre-filter with the same (or looser) rule the probe applies, so the
            # env-var spec stays small and truncation is rarely needed.
            keep = [l for l in dict.fromkeys(lines) if patchparse.is_distinctive(l)]
            targets.append({"path": path, "role": role, "lines": keep})
    return {
        "base_commit": task["base_commit"],
        "testbed": "/testbed",
        "targets": targets,
        "package_hint": task["repo"].split("/")[-1],
        "max_unreachable_blobs": 2000,
    }


def _encode_spec(spec, limit):
    """Return (json_text, truncated). Shrinks per-target line lists until under limit."""
    text = json.dumps(spec)
    if len(text) <= limit:
        return text, False
    for n in TRUNCATE_STEPS:
        small = dict(spec)
        small["targets"] = [dict(t, lines=t["lines"][:n]) for t in spec["targets"]]
        text = json.dumps(small)
        if len(text) <= limit:
            break
    return text, True


def _parse_last_json(stdout):
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _missing_exe(rc, err):
    e = (err or "").lower()
    return rc in (126, 127) or "executable file not found" in e


def compute_verdict(probe):
    """Return (verdict, leak_fix, leak_tests, leak_git)."""
    if not isinstance(probe, dict):
        return "ERROR", False, False, False
    hits = [h for h in (probe.get("fs_hits") or []) if h.get("leak")]
    git = probe.get("git") or {}
    ghits = [h for h in (git.get("unreachable_hits") or []) + (git.get("history_hits") or [])
             if h.get("leak")]
    leak_fix = any(h.get("role") == "fix" for h in hits + ghits)
    leak_tests = any(h.get("role") == "tests" for h in hits + ghits)
    # Evidence only: the task's own lines found in git objects. The mere existence of
    # commits/refs not below base_commit (release-branch tags, backports) is recorded
    # separately as future_history and is not, by itself, this task's answer.
    leak_git = bool(ghits)
    if leak_fix or leak_tests or leak_git:
        return "LEAK", leak_fix, leak_tests, leak_git
    if probe.get("errors"):
        # A partial scan (git or fs step failed) cannot certify CLEAN.
        return "ERROR", False, False, False
    if probe.get("other_copies"):
        # Weaker level (SWE-bench issue #640's definition): a different copy of a changed
        # file's module path is readable outside /testbed, but the exact fix/test lines were
        # not found in it (e.g. the code was refactored upstream after the fix).
        return "COPY", False, False, False
    summary = probe.get("targets_summary") or []
    if all((t.get("total") or 0) == 0 for t in summary):  # also: no targets at all
        return "UNDECIDABLE", False, False, False
    return "CLEAN", False, False, False


def _renamed_module_hit(probe):
    """True if enough fix/test lines were found, but only in a same-named file at a DIFFERENT
    module path (e.g. the module was moved upstream). Reported separately, never a LEAK."""
    hits = (probe or {}).get("fs_hits") or []
    return any(h.get("lines_ok") and not h.get("same_module") and not h.get("in_testbed")
               for h in hits)


def _set_verdict_fields(rec, probe):
    rec["verdict"], rec["leak_fix"], rec["leak_tests"], rec["leak_git"] = compute_verdict(probe)
    rec["leak_renamed_module"] = _renamed_module_hit(probe)


def rejudge(rec, overrides=None):
    """Apply the strict same-module rule to a record saved by an older tool version, using only
    the saved hit/copy paths (no Docker needed). Hits keep found/total; `leak` becomes
    lines_ok AND same_module; other_copies are re-filtered with the same module check.
    `overrides` maps instance_id -> {"leak_fix"/"leak_tests": False, "reason": ...}: a role
    a human reviewer rejected (e.g. matched "fix" lines that are only type annotations). It can
    only remove a role, never add one. Records without a probe (ERROR) are returned unchanged."""
    from . import probe_inside as pi

    probe = rec.get("probe")
    if not isinstance(probe, dict):
        return rec
    rec = json.loads(json.dumps(rec))  # deep copy
    probe = rec["probe"]
    hint = (rec.get("repo") or "").split("/")[-1].lower()
    for h in probe.get("fs_hits") or []:
        path = (h.get("path") or "").replace("\\", "/")
        own = "/testbed/" + (h.get("target") or "").lstrip("/")  # the task's own file
        same = path == own or pi.is_same_module(h.get("target") or "", path, hint)
        h["same_module"] = bool(same)
        h["lines_ok"] = pi.is_leak(h.get("found") or 0, h.get("total") or 0)
        h["leak"] = h["lines_ok"] and h["same_module"]
    if probe.get("other_copies"):
        probe["other_copies"] = [c for c in probe["other_copies"] if pi.is_same_module(
            c.get("target") or "", (c.get("path") or "").replace("\\", "/"), hint)]
    _set_verdict_fields(rec, probe)
    copies = probe.get("other_copies") or []
    rec["other_copy_fix"] = any(c.get("role") == "fix" for c in copies)
    rec["other_copy_tests"] = any(c.get("role") == "tests" for c in copies)
    ov = (overrides or {}).get(rec.get("instance_id"))
    if ov:
        for key in ("leak_fix", "leak_tests"):
            if ov.get(key) is False:
                rec[key] = False
        rec["manual_override"] = {"leak_fix": rec["leak_fix"], "leak_tests": rec["leak_tests"],
                                  "reason": ov.get("reason", "")}
        if rec["verdict"] == "LEAK" and not (rec["leak_fix"] or rec["leak_tests"] or rec["leak_git"]):
            rec["verdict"] = "COPY" if copies else "CLEAN"
    rec["rule"] = "strict-same-module"
    return rec


def scan_task(task, docker="docker", keep_image=False, timeout=900, spec_limit=None):
    t0 = time.time()
    image = task["image"]
    rec = {
        "instance_id": task["instance_id"], "repo": task["repo"], "image": image,
        "digest": None,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": "ERROR", "leak_fix": False, "leak_tests": False, "leak_git": False,
        "probe": None, "duration_s": 0.0, "error": None,
    }
    pulled = False
    try:
        rc, out, err = _run([docker, "pull", "--platform", PLATFORM, image], timeout=timeout)
        if rc != 0:
            rec["error"] = "docker pull failed (rc=%s): %s" % (rc, (err or out).strip()[-500:])
            return rec
        pulled = True
        rc, out, _ = _run(
            [docker, "image", "inspect", "--format", "{{index .RepoDigests 0}}", image],
            timeout=60)
        if rc == 0 and out.strip():
            rec["digest"] = out.strip()

        spec = build_spec(task)
        text, truncated = _encode_spec(spec, spec_limit or SPEC_LIMIT)
        if truncated:
            rec["spec_truncated"] = True
        env = dict(os.environ, ANSWERLEAK_SPEC=text)
        src = probe_source()

        rc = out = err = None
        name = "answerleak-%s-%d" % (re.sub(r"[^A-Za-z0-9_.-]", "_", task["instance_id"]), os.getpid())
        for py in PYTHONS:
            # value-less -e makes docker read ANSWERLEAK_SPEC from the client env,
            # avoiding Windows' command-line length limit.
            # -I: isolated mode, so a json.py/re.py in the image's WORKDIR (/testbed)
            # or a PYTHONPATH in the image cannot shadow the probe's stdlib imports.
            cmd = [docker, "run", "--rm", "-i", "--name", name, "--network", "none",
                   "--platform", PLATFORM, "-e", "ANSWERLEAK_SPEC", "--entrypoint", py,
                   image, "-I", "-"]
            rc, out, err = _run(cmd, input_text=src, timeout=timeout, env=env)
            if rc == 124:
                # killing the docker client does not stop the container; without this
                # the container keeps running and the later rmi fails.
                _run([docker, "rm", "-f", name], timeout=120)
            if not _missing_exe(rc, err):
                break
        probe = _parse_last_json(out)
        if probe is None:
            rec["error"] = "no JSON from probe (rc=%s): %s" % (rc, (err or out or "").strip()[-500:])
            return rec
        rec["probe"] = probe
        _set_verdict_fields(rec, probe)
        rec["rule"] = "strict-same-module"
        g = probe.get("git") or {}
        rec["future_history"] = bool((g.get("future_commits") or 0) > 0 or g.get("future_refs"))
        # Level 1 (issue #640's definition): a different copy of a changed file's module path
        # exists outside /testbed. Weaker than LEAK (exact fix lines), reported separately.
        copies = probe.get("other_copies") or []
        rec["other_copy_fix"] = any(c.get("role") == "fix" for c in copies)
        rec["other_copy_tests"] = any(c.get("role") == "tests" for c in copies)
        # CLEAN with fix_decidable=False means only test lines were checkable.
        rec["fix_decidable"] = any(t.get("role") == "fix" and (t.get("total") or 0) > 0
                                   for t in probe.get("targets_summary") or [])
        if probe.get("errors"):
            rec["error"] = "; ".join(str(e) for e in probe["errors"])[:500]
        return rec
    except Exception as e:  # keep the batch going
        rec["error"] = "%s: %s" % (type(e).__name__, e)
        return rec
    finally:
        if pulled and not keep_image:
            _run([docker, "rmi", image], timeout=300)
        rec["duration_s"] = round(time.time() - t0, 1)


def _done_ids(out_path):
    done = set()
    p = Path(out_path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("verdict") != "ERROR":
                done.add(r.get("instance_id"))
    return done


def scan_many(tasks, out_path, resume=True, docker="docker", keep_image=False,
              timeout=900, spec_limit=None):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = _done_ids(out) if resume else set()
    results = []
    for task in tasks:
        if task["instance_id"] in done:
            print("%s SKIP (already scanned)" % task["instance_id"], flush=True)
            continue
        rec = scan_task(task, docker=docker, keep_image=keep_image, timeout=timeout,
                        spec_limit=spec_limit)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        results.append(rec)
        print("%s %s %.1fs" % (rec["instance_id"], rec["verdict"], rec["duration_s"]), flush=True)
    return results
