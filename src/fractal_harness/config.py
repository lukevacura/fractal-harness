"""Repo configuration: `fractal.toml` at the repo root. Optional; with no file everything is as before.

    [roots]                      # named roots: specs/decisions may live in another repo or directory
    docs = "../project-docs"     # relative to the repo root, or absolute; `~` is expanded

    [policy]                     # reserved: parsed and exposed, no behavior yet
    accept_policy = "human_only" # human_only | local | local_with_audit

A path written `<root>:<path>` (e.g. `docs:specs/foo.md`) names a file under a named root. A plain
path is relative to the repo root, exactly as before. Unknown tables are ignored (forward compatible);
unknown keys in tables this module owns are reported by `Config.warnings`.

Resolution is pure path arithmetic plus a containment check, so it is SOUND (deterministic, may gate).
`doctor_checks` reports whether each root exists; that is a presence check and also sound.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "fractal.toml"
ACCEPT_POLICIES = ("human_only", "local", "local_with_audit")
OWNED = {"roots": None, "policy": {"accept_policy"}}      # table -> allowed keys (None: any name is a key)
_REF = re.compile(r"^([A-Za-z_][\w.-]+):(?!:)(.*)$", re.S)  # 2+ chars: `C:` is a drive, not a root


class ConfigError(ValueError):
    pass


class ConfigFileError(ConfigError):
    """fractal.toml itself is unusable (as opposed to one ref failing to resolve): never swallowed."""


@dataclass
class Config:
    repo: Path
    roots: dict[str, str] = field(default_factory=dict)       # name -> path as written
    accept_policy: str = "human_only"
    warnings: list[str] = field(default_factory=list)
    present: bool = False

    def root_dir(self, name: str) -> Path:
        if name not in self.roots:
            known = ", ".join(sorted(self.roots)) or "none defined"
            raise ConfigError(f"unknown root {name!r} (define it under [roots] in {CONFIG_FILE}; known: {known})")
        p = Path(self.roots[name]).expanduser()
        return (p if p.is_absolute() else self.repo / p).resolve()

    def resolve(self, ref: str, must_exist: bool = False) -> Path:
        """`<root>:<path>` -> absolute path; a plain path -> repo root / path (as always)."""
        m = _REF.match(ref)
        if not m:
            f = self.repo / ref
        else:
            name, rel = m.groups()
            base = self.root_dir(name)
            if not base.is_dir():
                raise ConfigError(f"root {name!r} -> {base} does not exist")
            if not rel or Path(rel).is_absolute():
                raise ConfigError(f"{ref!r}: expected {name}:<path relative to the root>")
            f = (base / rel).resolve()
            if f != base and base not in f.parents:
                raise ConfigError(f"{ref!r} escapes root {name!r} ({base})")
        if must_exist and not f.is_file():
            raise ConfigError(f"no such file: {ref}")
        return f


_cache: dict[tuple[str, int, int], Config] = {}


def load(repo: Path | str) -> Config:
    """The repo's config; defaults when the file is absent. Raises ConfigError (not a traceback) when malformed."""
    repo = Path(repo)
    f = repo / CONFIG_FILE
    if not f.is_file():
        return Config(repo=repo)
    st = f.stat()
    key = (str(f), st.st_mtime_ns, st.st_size)
    if key in _cache:
        return _cache[key]
    try:
        data = tomllib.loads(f.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigFileError(f"{CONFIG_FILE} is not valid TOML: {e}") from e
    cfg = Config(repo=repo, present=True)
    for table, keys in OWNED.items():
        if table in data and not isinstance(data[table], dict):
            raise ConfigFileError(f"{CONFIG_FILE}: [{table}] must be a table")
    for name, path in data.get("roots", {}).items():
        if not isinstance(path, str) or not path.strip():
            raise ConfigFileError(f"{CONFIG_FILE}: [roots] {name} must be a non-empty path string")
        if not re.fullmatch(r"[A-Za-z_][\w.-]+", name):
            raise ConfigFileError(f"{CONFIG_FILE}: root name {name!r} must be 2+ characters of letters, digits, _ . -")
        cfg.roots[name] = path
    pol = data.get("policy", {})
    if "accept_policy" in pol:
        if pol["accept_policy"] not in ACCEPT_POLICIES:
            raise ConfigFileError(f"{CONFIG_FILE}: accept_policy must be one of {', '.join(ACCEPT_POLICIES)}, "
                              f"got {pol['accept_policy']!r}")
        cfg.accept_policy = pol["accept_policy"]
    for k in sorted(set(pol) - OWNED["policy"]):
        cfg.warnings.append(f"{CONFIG_FILE}: unknown key [policy] {k} (ignored)")
    _cache[key] = cfg
    return cfg


def resolve_path(repo: Path, ref: str) -> Path:
    """`repo / ref`, or the file under a named root for `<root>:<path>`. Raises ConfigError."""
    if _REF.match(ref):
        return load(repo).resolve(ref)
    return repo / ref


def doctor_checks(repo: Path) -> list[tuple[bool, str]]:
    """(ok, message) per named root: does it resolve to an existing directory. SOUND (presence check)."""
    try:
        cfg = load(repo)
    except ConfigError as e:
        return [(False, str(e))]
    out = [(False, w) for w in cfg.warnings]
    for name in sorted(cfg.roots):
        try:
            d = cfg.root_dir(name)
            out.append((d.is_dir(), f"root {name!r} -> {d}" + ("" if d.is_dir() else " (does not exist)")))
        except ConfigError as e:
            out.append((False, str(e)))
    return out
