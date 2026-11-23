"""Unified-diff parsing and the "distinctive added line" filter.

Stdlib only and Python 3.6 compatible on purpose: probe_inside.py copies
is_distinctive()/distinctive() to run inside old containers.
"""

_COMMENT_PREFIXES = ("#", "//", '"""', "'''", "*")


def added_lines(diff_text):
    """Map each changed file's new path -> list of stripped, non-empty added lines."""
    result = {}
    current = None
    for raw in diff_text.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            if path == "/dev/null":
                current = None
            else:
                if path.startswith("b/"):
                    path = path[2:]
                current = path
                result.setdefault(current, [])
            continue
        if line.startswith("--- ") or current is None:
            continue
        if line.startswith("+"):
            text = line[1:].strip()
            if text:
                result[current].append(text)
    return result


def is_distinctive(line):
    """True if a stripped line is long, has a letter, and is not comment-only."""
    line = line.strip()
    if len(line) < 20:
        return False
    if not any(c.isalpha() for c in line):
        return False
    if line.startswith(_COMMENT_PREFIXES):
        return False
    return True


def normalize(line):
    """Matching key (same as probe_inside.normalize): no whitespace, one quote style."""
    return "".join(line.split()).replace("'", '"')


def _no_trailing_commas(s):
    """Drop commas that directly precede a closing bracket: 'f(a,b,)' -> 'f(a,b)'."""
    out = s
    for close in ")]}":
        out = out.replace("," + close, close)
    return out


def distinctive(lines, original_text):
    """Distinctive lines not already present in original_text; de-duplicated, ordered.

    Presence is decided on normalize()d keys, like the in-container probe."""
    existing = set(normalize(l) for l in original_text.splitlines())
    # The whole original with all whitespace (incl. line breaks) removed: a line that only re-wraps
    # existing code, e.g. a 4-line call collapsed onto one line, is found here and is not "new".
    # Trailing commas before a closing bracket are dropped for this check (black adds them when it
    # splits a call over several lines).
    flat = _no_trailing_commas(normalize(original_text))
    out = []
    seen = set()
    for line in lines:
        s = line.strip()
        k = normalize(s)
        if (k in seen or k in existing or not is_distinctive(s)
                or (flat and _no_trailing_commas(k).rstrip(",") in flat)):
            continue
        seen.add(k)
        out.append(s)
    return out
