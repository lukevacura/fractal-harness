from pathlib import Path

from fractal_harness.audit import EXCLUDED, _violating_line, audit_edge
from fractal_harness.cache import ClaimCache

SRC = """from db import Pool

POOL_SIZE = 10   # tuned for prod
TIMEOUT_S = 30

def make_pool():
    return Pool(size=POOL_SIZE, timeout=TIMEOUT_S)
"""


def _cache(tmp_path: Path) -> ClaimCache:
    (tmp_path / "app.py").write_text(SRC)
    (tmp_path / "legacy.py").write_text("# OldPool was removed\nx = 1\n")
    return ClaimCache(tmp_path)


def g(pattern, expect="present", paths=("*.py",), **kw):
    return {"type": "grep", "pattern": pattern, "paths": list(paths), "expect": expect, **kw}


def test_ok_when_probe_checks_everything_claimed(tmp_path):
    c = _cache(tmp_path)
    e = c.put("POOL_SIZE is 10", ["app.py"], probe=g(r"^POOL_SIZE = 10\b"))
    a = audit_edge(e, tmp_path)
    assert a.verdict == "ok", a.findings


def test_partial_when_value_or_identifier_unchecked(tmp_path):
    c = _cache(tmp_path)
    e = c.put("POOL_SIZE is 10 and make_pool passes TIMEOUT_S", ["app.py"], probe=g(r"^POOL_SIZE ="))
    a = audit_edge(e, tmp_path)
    assert a.verdict == "partial"
    assert any("value 10" in f for f in a.findings)
    assert any("`make_pool`" in f for f in a.findings) and any("`TIMEOUT_S`" in f for f in a.findings)


def test_weak_when_probe_does_not_depend_on_matched_code(tmp_path):
    c = _cache(tmp_path)
    e = c.put("the pool is configured", ["app.py"], probe=g(r"Pool", expect={"max": 50}))
    assert audit_edge(e, tmp_path).verdict == "weak"


def test_absent_rule_checked_by_injection(tmp_path):
    c = _cache(tmp_path)
    good = c.put("OldPool never appears in code", ["*.py"], probe=g(r"\bOldPool\b", "absent", exclude=r"^\s*#"))
    assert audit_edge(good, tmp_path).verdict == "ok"
    # the exclude swallows every line, so the probe can never fail
    bad = c.put("OldPool never appears", ["*.py"], probe=g(r"\bOldPool\b", "absent", exclude=r"."))
    assert audit_edge(bad, tmp_path).verdict == "weak"
    # claim forbids OldPool anywhere; probe only forbids `class OldPool` definitions
    narrow = c.put("OldPool never appears in code", ["*.py"], probe=g(r"^class OldPool\b", "absent"))
    a = audit_edge(narrow, tmp_path)
    assert a.verdict == "partial" and any("`OldPool`" in f for f in a.findings)


def test_violating_line_synthesis():
    assert _violating_line(r"\bOldPool\b", None) is not None
    assert _violating_line(r"^\s*(class|typedef)\s+(A|B)\b", None) is not None
    assert _violating_line(r"x", r".") == EXCLUDED
    assert _violating_line(r"(?<=a)b{3}", None) is None


def test_file_names_are_not_identifiers(tmp_path):
    c = _cache(tmp_path)
    e = c.put("app.py defines POOL_SIZE = 10", ["app.py"], probe=g(r"^POOL_SIZE = 10\b"))
    assert audit_edge(e, tmp_path).verdict == "ok"


def test_audit_never_touches_files(tmp_path):
    c = _cache(tmp_path)
    e = c.put("POOL_SIZE is 10 and make_pool passes TIMEOUT_S", ["app.py"], probe=g(r"^POOL_SIZE ="))
    audit_edge(e, tmp_path)
    assert (tmp_path / "app.py").read_text() == SRC
