from answerleak.patchparse import added_lines, distinctive, is_distinctive

ASTROPY = """diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py
--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -242,7 +242,7 @@ def _cstack(left, right):
         cright = _coord_matrix(right, 'right', noutp)
     else:
         cright = np.zeros((noutp, right.shape[1]))
-        cright[-right.shape[0]:, -right.shape[1]:] = 1
+        cright[-right.shape[0]:, -right.shape[1]:] = right

     return np.hstack([cleft, cright])
"""

MULTI = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 x = 1
+    value = compute_something(x)
+
@@ -10,2 +11,3 @@
 y = 2
+    other = compute_other(y)
\\ No newline at end of file
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone = 1
-also_gone = 2
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+brand new file content
"""


def test_astropy_hunk():
    r = added_lines(ASTROPY)
    assert r == {"astropy/modeling/separable.py": ["cright[-right.shape[0]:, -right.shape[1]:] = right"]}


def test_multi_file_hunks_deletions():
    r = added_lines(MULTI)
    assert r["a.py"] == ["value = compute_something(x)", "other = compute_other(y)"]
    assert "old.py" not in r
    assert r["new.txt"] == ["brand new file content"]


def test_crlf():
    r = added_lines(ASTROPY.replace("\n", "\r\n"))
    assert r["astropy/modeling/separable.py"] == ["cright[-right.shape[0]:, -right.shape[1]:] = right"]


def test_empty_diff():
    assert added_lines("") == {}


def test_is_distinctive():
    assert is_distinctive("cright[-right.shape[0]:, -right.shape[1]:] = right")
    assert not is_distinctive("x = 1")  # short
    assert not is_distinctive("# this is a long comment line here")
    assert not is_distinctive("// this is a long comment line here")
    assert not is_distinctive('""" long docstring opener text here')
    assert not is_distinctive("* javadoc style long comment line")
    assert not is_distinctive("}}]) ))) ((( ]]] 12345678901234567")  # no letters


def test_distinctive_filters_and_dedupes():
    original = "foo = some_existing_call(argument)\n   already_here_in_base_file = 1  \n"
    lines = [
        "short = 1",
        "# a long comment that is skipped",
        "foo = some_existing_call(argument)",
        "already_here_in_base_file = 1",
        "new_value = brand_new_function(arg)",
        "new_value = brand_new_function(arg)",
        "another_new_statement = compute(1)",
    ]
    assert distinctive(lines, original) == [
        "new_value = brand_new_function(arg)",
        "another_new_statement = compute(1)",
    ]


def test_distinctive_ignores_quote_and_spacing_variants():
    original = 'headers["Content-Length"] = "0"\n'
    assert distinctive(["headers['Content-Length']  =  '0'"], original) == []
    assert distinctive(["a = brand_new_function('x')", 'a = brand_new_function("x")'], "") == [
        "a = brand_new_function('x')"]


def test_rewrapped_existing_code_is_not_distinctive():
    # xarray-4687: the hidden test only collapsed a 4-line pytest.param(...) onto one line.
    original = 'params = [\n    pytest.param(\n        operator.eq,\n        id="equal",\n    ),\n]\n'
    added = ['pytest.param(operator.eq, id="equal"),', "really_new_function_call(alpha, beta)"]
    assert distinctive(added, original) == ["really_new_function_call(alpha, beta)"]
