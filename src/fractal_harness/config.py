"""Repo configuration: `fractal.toml` at the repo root. Optional; with no file everything is as before.

    [roots]                      # named roots: specs/decisions may live in another repo or directory
    docs = "../project-docs"     # relative to the repo root, or absolute; `~` is expanded

    [policy]                     # reserved: parsed and exposed, no behavior yet
    accept_policy = "human_only" # human_only | local | local_with_audit
    friction_threshold = 5       # a local rule blocked this many times within the window raises a `conflict` event
    friction_window_days = 14    # (heuristic signal; permissive defaults; see conflict.py)
    human_nodes = ["core"]       # nodes whose conflicts only a human resolves (a conflict at root always does)
    root_signal = "tty"          # how a root decision is confirmed: tty (default) | flag (see root_signal.py)

Pinned policy (`fractal pin`, a root decision): the keys that decide who may change rules -- accept_policy,
human_nodes, root_signal -- are pinned in the store (.fractal/policy.pin). Once pinned, an edit to fractal.toml (or
removing it) does not take effect for those keys until a human pins again: `load` returns the pinned values and
lists the difference in `policy_drift` (reported by `fractal check`). Without a pin, the file is the policy, as
before. SOUND for the CLI's own reads; a writer who also rewrites the pin file is not detected (the hooks' store
guard blocks the obvious paths: HEURISTIC).

A path written `<root>:<path>` (e.g. `docs:specs/foo.md`) names a file under a named root. A plain
path is relative to the repo root, exactly as before. Unknown tables are ignored (forward compatible);
unknown keys in tables this module owns are reported by `Config.warnings`.

Resolution is pure path arithmetic plus a containment check, so it is SOUND (deterministic, may gate).
`doctor_checks` reports whether each root exists; that is a presence check and also sound.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

CONFIG_FILE = "fractal.toml"
ACCEPT_POLICIES = ("human_only", "local", "local_with_audit")
ROOT_SIGNALS = ("tty", "flag")       # root_signal.SIGNALS (kept here so config has no import of it)
PINNED_KEYS = ("accept_policy", "human_nodes", "root_signal")
PIN_FILE = "policy.pin"
OWNED = {"roots": None, "policy": {"accept_policy", "friction_threshold", "friction_window_days", "human_nodes",
                                   "root_signal"}, "jobs": None, "brief": None}   # table -> allowed keys (None: any name is a key)
JOBS_KEYS = {"backend", "ssh"}
SSH_KEYS = {"host", "dir", "prefix", "ssh", "options"}
BRIEF_SECTIONS = ("task", "reporting", "heavy")
BRIEF_KEYS = {"report_dir", "report_to", *BRIEF_SECTIONS, *(f"{s}_extra" for s in BRIEF_SECTIONS)}
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
    friction_threshold: int = 5          # [policy]: blocks of one local rule within the window that raise a conflict
    friction_window_days: float = 14     # [policy]: the window; a heuristic signal, it only raises events
    human_nodes: tuple = ()              # [policy]: nodes whose conflicts only a human resolves
    root_signal: str | None = None       # [policy]: tty | flag; None = root_signal.DEFAULT
    policy_drift: dict = field(default_factory=dict)   # pinned key -> {"file": value in fractal.toml, "pinned": value used}
    jobs: dict = field(default_factory=dict)       # [jobs]: backend = "local" | "ssh"; [jobs.ssh]: host, dir, prefix, ssh, options
    brief: dict = field(default_factory=dict)      # [brief]: report_dir, report_to, task/reporting/heavy (+ _extra)
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
    """The repo's config; defaults when the file is absent. Raises ConfigError (not a traceback) when malformed.
    The pinned policy keys come from the pin when one exists (see the module docstring)."""
    repo = Path(repo)
    cfg = _load_file(repo)
    main = _store_dir(str(repo.resolve())).parent
    if main != repo.resolve():
        # a linked worktree or a subdirectory shares the main checkout's store: that checkout's policy governs it,
        # not a fractal.toml an agent can write next to its own working copy
        m = _load_file(main)
        cfg = replace(cfg, accept_policy=m.accept_policy, human_nodes=m.human_nodes, root_signal=m.root_signal)
    return _apply_pin(repo, cfg)


def policy_values(cfg: Config) -> dict:
    """The pinnable policy keys as fractal.toml states them (before any pin is applied)."""
    return {"accept_policy": cfg.accept_policy, "human_nodes": list(cfg.human_nodes), "root_signal": cfg.root_signal}


@lru_cache(maxsize=256)
def _store_dir(repo: str) -> Path:
    from .cache import STORE_DIR, store_root
    return store_root(Path(repo)) / STORE_DIR


def pin_path(repo: Path | str) -> Path:
    return _store_dir(str(Path(repo).resolve())) / PIN_FILE


def read_pin(repo: Path | str) -> dict | None:
    f = pin_path(repo)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError) as e:
        raise ConfigFileError(f"{f} is unreadable ({e}): restore it, or a human re-pins with `fractal pin`") from e
    return data if isinstance(data, dict) else None


def write_pin(repo: Path | str) -> dict:
    """Pin fractal.toml's current policy keys (the caller has the root's confirmation)."""
    values = {**policy_values(_load_file(Path(repo))), "ts": time.time()}
    f = pin_path(repo)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(values, indent=1) + "\n")
    return values


def _apply_pin(repo: Path, cfg: Config) -> Config:
    pin = read_pin(repo)
    if pin is None:
        return cfg
    file_vals, drift, out = policy_values(cfg), {}, {}
    for k in PINNED_KEYS:
        pv = pin[k] if k in pin else file_vals[k]
        if pv != file_vals[k]:
            drift[k] = {"file": file_vals[k], "pinned": pv}
        out[k] = tuple(pv or ()) if k == "human_nodes" else pv
    if not drift:
        return cfg
    warnings = [*cfg.warnings, *(f"{CONFIG_FILE}: [policy] {k} = {v['file']!r} is not pinned; using the pinned "
                                 f"{v['pinned']!r} until a human runs `fractal pin`" for k, v in drift.items())]
    return replace(cfg, **out, policy_drift=drift, warnings=warnings)


def _load_file(repo: Path) -> Config:
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
    if "root_signal" in pol:
        if pol["root_signal"] not in ROOT_SIGNALS:
            raise ConfigFileError(f"{CONFIG_FILE}: root_signal must be one of {', '.join(ROOT_SIGNALS)}, "
                                  f"got {pol['root_signal']!r}")
        cfg.root_signal = pol["root_signal"]
    _parse_conflict_policy(pol, cfg)
    for k in sorted(set(pol) - OWNED["policy"]):
        cfg.warnings.append(f"{CONFIG_FILE}: unknown key [policy] {k} (ignored)")
    _parse_jobs(data, cfg)
    _parse_brief(data, cfg)
    _cache[key] = cfg
    return cfg


def _parse_conflict_policy(pol: dict, cfg: Config) -> None:
    """[policy] friction_threshold (int >= 1), friction_window_days (number > 0), human_nodes (list of node ids)."""
    for k, num in (("friction_threshold", int), ("friction_window_days", float)):
        if k in pol:
            v = pol[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 or (num is int and v != int(v)):
                raise ConfigFileError(f"{CONFIG_FILE}: [policy] {k} must be a positive "
                                      f"{'integer' if num is int else 'number'}, got {v!r}")
            setattr(cfg, k, num(v))
    if "human_nodes" in pol:
        v = pol["human_nodes"]
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise ConfigFileError(f"{CONFIG_FILE}: [policy] human_nodes must be a list of node ids")
        cfg.human_nodes = tuple(v)


def _parse_jobs(data: dict, cfg: Config) -> None:
    """[jobs] backend = "local" (default) | "ssh"; [jobs.ssh] host, dir, prefix, ssh, options. Types only: whether a
    backend is complete or reachable is `jobs.doctor_checks`'s business."""
    j = data.get("jobs", {})
    for k in sorted(set(j) - JOBS_KEYS):
        cfg.warnings.append(f"{CONFIG_FILE}: unknown key [jobs] {k} (ignored)")
    if "backend" in j:
        if not isinstance(j["backend"], str) or not j["backend"].strip():
            raise ConfigFileError(f"{CONFIG_FILE}: [jobs] backend must be a backend name (local, ssh)")
        cfg.jobs["backend"] = j["backend"]
    if "ssh" in j:
        ssh = j["ssh"]
        if not isinstance(ssh, dict):
            raise ConfigFileError(f"{CONFIG_FILE}: [jobs.ssh] must be a table")
        for k, v in ssh.items():
            if k == "options":
                ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
            else:
                ok = isinstance(v, str) and v.strip()
            if k in SSH_KEYS and not ok:
                raise ConfigFileError(f"{CONFIG_FILE}: [jobs.ssh] {k} must be "
                                      + ("a list of strings" if k == "options" else "a non-empty string"))
            if k not in SSH_KEYS:
                cfg.warnings.append(f"{CONFIG_FILE}: unknown key [jobs.ssh] {k} (ignored)")
        cfg.jobs["ssh"] = {k: v for k, v in ssh.items() if k in SSH_KEYS}


def _parse_brief(data: dict, cfg: Config) -> None:
    b = data.get("brief", {})
    for k, v in b.items():
        if k not in BRIEF_KEYS:
            cfg.warnings.append(f"{CONFIG_FILE}: unknown key [brief] {k} (ignored)")
        elif not isinstance(v, str):
            raise ConfigFileError(f"{CONFIG_FILE}: [brief] {k} must be a string")
        else:
            cfg.brief[k] = v


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
