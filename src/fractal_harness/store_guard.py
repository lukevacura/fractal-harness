"""Store guard: the rule store (`.fractal/`) is written only through the `fractal` CLI.

Rules are human-owned: their changes go through the CLI, where authority checks and history live.
The PreToolUse hook calls this module for two kinds of tool call:

  edit tools  Edit/Write/MultiEdit/NotebookEdit whose target path (symlinks and `..` resolved)
              lies inside a store dir are blocked. [sound for the path the tool names]
  Bash        the command is split (shlex; `;` `&&` `||` `|` `&` newlines, subshells, `$(...)` and
              backticks) into simple commands, and a command is blocked when it mentions a store
              path AND is a write-capable form: output redirection into the store; `rm`/`mv`/`shred`
              /`unlink` of a store path (or `rm`/`mv` of a dir that contains the store); `cp`/`rsync`
              /`ln`/`install` with a store target; `dd of=` a store path; `sed -i`; `git` other than
              read-only subcommands, and `git clean -x/-X` (it deletes ignored files: the store);
              `sqlite3` unless it is opened `-readonly` or by a `?mode=ro` URI and no other argument
              (SQL, dot-command) mentions the store; `find` with an action (`-delete`, `-exec`, ...);
              `xargs` fed by a store path; and every command not on a short read-only list
              (`cat`, `ls`, `grep`, `head`, ...), which covers interpreters (`python -c`, `node -e`,
              `sh -c`, ...) whose inline code mentions the store. `fractal ...` itself may write
              the store: it is the sanctioned door (but see human-only verbs below).
  fractal.toml  the repo config at the repo root and at each store's repo root is protected the
              same way (edit tools by resolved path; Bash by the same write-capable forms, with
              `fractal.toml` as a path component as the textual mention): its `[policy]` decides who
              may accept rules and how a root decision is confirmed, so changing it is a human
              decision. Reads are allowed. Created files count too (a store exists, no toml yet).
  human-only verbs  some CLI verbs are root decisions, and the hook refuses them in agent Bash
              whatever the CLI's own confirmation says (belt and braces: an agent can fake the
              terminal the CLI asks for). A fractal invocation is `fractal`, `<path>/fractal`,
              `python[3] [opts] -m fractal_harness[.cli]`, behind `uv run [opts]`, `uvx`, `env`,
              `sudo`, ... and global options (`--root X`, `--root=X`, `--json`). Blocked: `accept`
              without `--as NODE` (or `--as root`); `reject`; `override` without `--by NODE` (or
              `--by root`); `conflict arbitrate`; `conflict resolve` without `--as NODE`; `pin`/
              `unpin`; `lift`; and any fractal invocation with `--yes`. Options match argparse prefixes
              (`--ye`, `--b`). Also found: inside `sh -c`/`bash -c`/`eval`/`ssh`, piped or here-doc
              into a shell, run by `xargs`/`find -exec`/other launchers; interpreter code (`python
              -c`, `node -e`, `perl -e`, heredoc/here-string/pipe into one) naming `fractal_harness`
              with `main(`/`set_rule`/`human_set`/`record_human_accept`/`override(`/`arbitrate(`, or
              naming fractal with a human verb word; and pty wrappers around fractal (`script`,
              `expect`, `unbuffer`, `socat`, `ptyrun`, `tmux`, `screen`, ..., and code naming `pty`/
              `pexpect` with fractal), for any verb: an agent has no need of a terminal. Agent verbs
              (`invariants`, `check`, `propose`, `accept --as N`, `override --by N`, `conflict list`,
              `conflict escalate`, ...) are allowed.

With no store, everything is allowed (fractal.toml and the verbs included).

HEURISTIC. Shell is not parsed in full, and a determined agent can hide a write or a verb: a
script file written elsewhere and then run (`python x.py`, `bash x.sh`), a path or command
assembled at runtime (`"frac""tal"` is caught, `$(printf fra)ctal` is not) or held in a variable
the hook cannot see, an alias or shell function defined in an earlier call, an interpreter
reading the path or code from a file, a git alias or Makefile target. Textual checks on code err
toward blocking (`python -c` code that names fractal and the word `accept` is refused even when it
only prints). The goal is to close the accidental and
the obvious paths and to make a bypass a visible, deliberate act, not to be a sandbox. The parse
errs toward blocking: anything that mentions `.fractal` as a path component and is not a known
read-only form is blocked (a false block costs the agent one question to the user).
"""

from __future__ import annotations

import glob
import os
import re
import shlex
from pathlib import Path

from .cache import STORE_DIR, store_root
from .config import CONFIG_FILE

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
GUARDED_TOOLS = EDIT_TOOLS | {"Bash"}

# `.fractal` as a whole path component (not `.fractal-notes`, `.fractal.md`, `x.fractal`).
_MENTION = re.compile(r"(?i)(?<![\w.-])" + re.escape(STORE_DIR) + r"(?![\w.-])")   # case: macOS file systems fold it
_SEPARATORS = {";", "&&", "||", "|", "|&", "&", ";;", "(", ")", "{", "}", "!"}
_WRAPPERS = {"sudo", "doas", "env", "command", "builtin", "exec", "nohup", "time", "nice", "stdbuf",
             "timeout", "caffeinate"}
READ_ONLY = {"cat", "less", "more", "head", "tail", "ls", "stat", "file", "wc", "grep", "egrep", "fgrep",
             "rg", "ag", "du", "diff", "cmp", "md5", "md5sum", "shasum", "sha1sum", "sha256sum", "readlink",
             "realpath", "test", "[", "echo", "printf", "basename", "dirname", "strings", "xxd", "hexdump",
             "od", "uniq", "jq", "tree", "find", "sort", "cd", "pushd", "popd", "true", "false", "which",
             "type", "pwd", "column", "cut", "tr", "nl", "bat"}
GIT_READ = {"status", "log", "diff", "show", "ls-files", "check-ignore", "blame", "grep", "rev-parse",
            "ls-tree", "cat-file", "describe", "shortlog", "whatchanged"}
FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
SQLITE_VALUE_FLAGS = {"-cmd", "-init", "-separator", "-newline", "-nullvalue", "-vfs", "-maxsize", "-mmap",
                      "-heap", "-lookaside", "-pagecache"}

MESSAGE = ("Rules and everything under {store}/ change only through the fractal CLI (`fractal ...`), where "
           "authority checks and history live. Do not write the store directly or work around this guard. "
           "If a rule should change, stop and ask the user.")


def store_dirs(root: Path, cwd: Path) -> list[Path]:
    """Resolved store dirs that govern a tool call: the nearest `.fractal/` holding an `edges.db`
    above `cwd` or `root`, and the shared store of a linked worktree. Empty: no store, guard off."""
    found: list[Path] = []
    for base in (cwd, root):
        for d in (base, *base.parents):
            if (d / STORE_DIR / "edges.db").exists():
                found.append((d / STORE_DIR).resolve())
                break
    try:
        shared = store_root(root) / STORE_DIR
        if (shared / "edges.db").exists():
            found.append(shared.resolve())
    except Exception:
        pass
    return list(dict.fromkeys(found))


def _fold(p: Path) -> Path:
    """Paths compared case-insensitively: the default macOS file system treats .FRACTAL as .fractal. On a
    case-sensitive one this over-protects, never under-protects."""
    return Path(str(p).casefold())


def _inside(p: Path, dirs: list[Path]) -> bool:
    fp = _fold(p)
    return any(fp == _fold(d) or _fold(d) in fp.parents for d in dirs)


def _resolve(raw: str, cwd: Path) -> list[Path]:
    s = os.path.expanduser(os.path.expandvars(raw))
    if s.startswith("file:"):                       # sqlite URI filename
        s = s[5:].split("?", 1)[0]
    if not s:
        return []
    paths = [s]
    if any(c in s for c in "*?["):
        paths += glob.glob(s, root_dir=cwd) if not os.path.isabs(s) else glob.glob(s)
    out = []
    for q in paths:
        try:
            out.append((cwd / q).resolve())
        except (OSError, RuntimeError, ValueError):
            pass
    return out


def edit_target(path: str | None, cwd: Path, dirs: list[Path]) -> Path | None:
    """The resolved target of an edit tool when it lies inside a store dir."""
    if not path or not dirs:
        return None
    p = (cwd / os.path.expanduser(path)).resolve()
    return p if _inside(p, dirs) else None


# --- bash ---------------------------------------------------------------------------------

def _hits(tok: str, cwd: Path, dirs: list[Path]) -> bool:
    """Does `tok` mention a path inside the store (textually, or once resolved)?"""
    if getattr(dirs, "mention", _MENTION).search(tok):
        return True
    return any(_inside(p, dirs) for part in {tok, *tok.split("=")[1:]} for p in _resolve(part, cwd))


def _contains_store(tok: str, cwd: Path, dirs: list[Path]) -> bool:
    """Does `tok` resolve to a strict ancestor of a store dir (so removing it removes the store)?"""
    return any(_fold(p) in _fold(d).parents for p in _resolve(tok, cwd) for d in dirs)


def _normalize(command: str) -> tuple[str, list[str]]:
    """Make unquoted newlines and backticks into `;`, and pull out command substitutions inside
    double quotes (they run too) as extra commands to scan."""
    out, extra = [], []
    i, quote = 0, None
    while i < len(command):
        c = command[i]
        if c == "\\" and quote != "'" and i + 1 < len(command):
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote is None:
            if c in "'\"":
                quote = c
            elif c in "\n`":
                c = " ; "
        elif c == quote:
            quote = None
        elif quote == '"' and c == "`":
            j = command.find("`", i + 1)
            j = len(command) if j < 0 else j
            extra.append(command[i + 1:j])
        elif quote == '"' and command.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(command) and depth:
                depth += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            extra.append(command[i + 2:j - 1])
        out.append(c)
        i += 1
    return "".join(out), extra


def _segments(command: str) -> list[tuple[list[str], str]]:
    """Simple commands as (tokens, the separator that follows)."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    segs, cur = [], []
    for tok in lex:
        if tok in _SEPARATORS:
            segs.append((cur, tok))
            cur = []
        else:
            cur.append(tok)
    segs.append((cur, ""))
    return [(t, sep) for t, sep in segs if t]


def _strip_wrappers(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        w = words[i]
        if w == "$" or re.match(r"^[A-Za-z_]\w*=", w):
            i += 1
        elif os.path.basename(w) in ("uv", "uvx", "pipx") and words[i + 1:i + 2] == ["run"] or (
                os.path.basename(w) == "uvx"):              # `uv run fractal ...` is still the CLI
            i += 2 if os.path.basename(w) != "uvx" else 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        elif os.path.basename(w) in _WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or re.match(r"^\d+[smhd]?$", words[i])):
                i += 1
        else:
            break
    return words[i:]


def _target(args: list[str]) -> str | None:
    """The destination of cp/rsync/ln/install: `-t DIR`, `--target-directory=DIR`, else the last operand."""
    for k, a in enumerate(args):
        if a == "-t" and k + 1 < len(args):
            return args[k + 1]
        if a.startswith("--target-directory="):
            return a.split("=", 1)[1]
    operands = [a for a in args if not a.startswith("-")]
    return operands[-1] if len(operands) > 1 else None


def _git_clean_ignored(args: list[str]) -> bool:
    flags = [a for a in args if a.startswith("-") and not a.startswith("--")]
    dry = "--dry-run" in args or any("n" in f for f in flags)
    return not dry and any("x" in f or "X" in f for f in flags)


def _sqlite_read_only(args: list[str], cwd: Path, dirs: list[Path]) -> bool:
    db, k = None, 0
    while k < len(args):
        a = args[k]
        if a in SQLITE_VALUE_FLAGS:
            k += 2
            continue
        if not a.startswith("-"):
            db = k
            break
        k += 1
    readonly = "-readonly" in args or "--readonly" in args or (
        db is not None and re.search(r"[?&]mode=ro(&|$)", args[db]) is not None)
    others = [a for j, a in enumerate(args) if j != db and _hits(a, cwd, dirs)]
    return readonly and not others


def _simple_write(words: list[str], redirects: list[str], cwd: Path, dirs: list[Path],
                  piped_store: bool) -> str | None:
    """Why this simple command writes the store, or None."""
    if any(_hits(t, cwd, dirs) for t in redirects):
        return "output redirection into the store"
    words = _strip_wrappers(words)
    if not words:
        return None
    cmd, args = os.path.basename(words[0]), words[1:]
    if cmd == "fractal":
        return None
    if cmd == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub == "clean" and _git_clean_ignored(args):
            return "`git clean -x` deletes ignored files, which include the store"
    hit = [a for a in args if _hits(a, cwd, dirs)]
    operands = [a for a in args if not a.startswith("-")]
    removed = operands if cmd == "rm" else operands[:-1] if cmd == "mv" else []
    if any(_contains_store(a, cwd, dirs) for a in removed):
        return f"`{cmd}` of a directory that contains the store"
    if cmd == "xargs" and piped_store:
        inner = next((a for a in args if not a.startswith("-")), "echo")
        if os.path.basename(inner) not in READ_ONLY:
            return "`xargs` fed store paths"
    if _hits(cmd, cwd, dirs):
        return "runs a program from the store"
    if not hit:
        return None
    if cmd == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        return None if sub in GIT_READ else f"`git {sub}` on a store path"
    if cmd == "sqlite3":
        return None if _sqlite_read_only(args, cwd, dirs) else (
            "`sqlite3` on the store that is not provably read-only: open it with -readonly or a ?mode=ro URI "
            "and keep store paths out of the SQL")
    if cmd in ("cp", "rsync", "ln", "install", "scp", "ditto"):
        t = _target(args)
        if (t is not None and _hits(t, cwd, dirs)) or "--remove-source-files" in args:
            return f"`{cmd}` into the store"
        return None
    if cmd == "dd":
        return "`dd of=` the store" if any(a.startswith("of=") and _hits(a, cwd, dirs) for a in args) else None
    if cmd == "sed":
        return "`sed -i` on a store path" if any(a.startswith("-i") or a.startswith("--in-place") or
                                                 (a.startswith("-") and not a.startswith("--") and "i" in a)
                                                 for a in args) else None
    if cmd == "find":
        return "`find` with an action on the store" if any(a in FIND_ACTIONS for a in args) else None
    if cmd == "sort":
        return "`sort -o` into the store" if any(a.startswith("-o") or a.startswith("--output") for a in args) else None
    if cmd in READ_ONLY:
        return None
    return f"`{cmd}` with a store path (not a known read-only form)"


def bash_write(command: str, cwd: Path, dirs: list[Path], _depth: int = 0) -> str | None:
    """Why `command` would write to a store dir, or None. Errs toward blocking."""
    if not dirs or not command or not command.strip():
        return None
    try:
        text, extra = _normalize(command)
        segments = _segments(text)
    except ValueError:                      # unbalanced quotes etc.: fall back to a textual mention
        return ("unparseable command that mentions the store"
                if getattr(dirs, "mention", _MENTION).search(command) else None)
    if _depth < 4:
        for sub in extra:
            why = bash_write(sub, cwd, dirs, _depth + 1)
            if why:
                return why
    piped_store = False
    shell_vars: dict[str, str] = {}         # `D=.fractal; rm $D/x`: literal assignments seen earlier
    for tokens, sep in segments:
        if shell_vars:
            tokens = [re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: shell_vars.get(m[1], m[0]), t) for t in tokens]
        assigns = [t for t in tokens if t != "export" and re.match(r"^[A-Za-z_]\w*=", t)]
        if assigns and all(t == "export" or t in assigns for t in tokens):
            shell_vars.update(a.split("=", 1) for a in assigns)
        words, redirects, k = [], [], 0
        while k < len(tokens):
            t = tokens[k]
            if set(t) <= set("<>&|") and ">" in t:          # >, >>, >|, &>, &>>, >&, <>
                if k + 1 < len(tokens):
                    redirects.append(tokens[k + 1])
                k += 2
                continue
            if set(t) <= set("<&") and "<" in t:            # input redirection / heredoc: reads
                k += 2
                continue
            words.append(t)
            k += 1
        why = _simple_write(words, redirects, cwd, dirs, piped_store)
        if why:
            return why
        mentions = any(_hits(w, cwd, dirs) for w in words[1:])
        piped_store = sep in ("|", "|&") and (mentions or piped_store)
        stripped = _strip_wrappers(words)
        if stripped and stripped[0] in ("cd", "pushd"):         # later relative paths resolve from there
            target = next((a for a in stripped[1:] if not a.startswith("-")), os.path.expanduser("~"))
            resolved = _resolve(target, cwd)
            if resolved:
                cwd = resolved[0]
    return None


# --- human-only CLI verbs ------------------------------------------------------------------

class Reason(str):
    """A block reason that knows what it protects: "store", "config" (fractal.toml) or "human" (a root verb)."""
    kind = "store"

    def __new__(cls, text: str, kind: str = "store") -> "Reason":
        r = super().__new__(cls, text)
        r.kind = kind
        return r


class _Targets(list):
    """Protected paths plus the textual mention that counts as naming one (see `_hits`)."""

    def __init__(self, paths, mention: re.Pattern):
        super().__init__(paths)
        self.mention = mention


_TOML_MENTION = re.compile(r"(?i)(?<![\w.-])" + re.escape(CONFIG_FILE) + r"(?![\w.-])")
_FRACTAL_MODULES = {"fractal_harness", "fractal_harness.cli"}
_PYTHON = re.compile(r"^(python[\d.]*|pypy[\d.]*)$")
INTERPRETERS = {"node", "nodejs", "deno", "bun", "perl", "ruby", "php", "lua", "tclsh", "osascript", "Rscript"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "su"}
PTY_WRAPPERS = {"script", "expect", "unbuffer", "socat", "ptyrun", "empty", "pty", "winpty", "dtach", "abduco",
                "tmux", "screen", "zpty", "faketty"}
_FRACTAL_WORD = re.compile(r"(?<![\w-])fractal(?![\w-])|fractal_harness")
_CODE_VERB = re.compile(r"\b(accept|reject|override|change|arbitrate|resolve|pin|unpin|lift)\b|(?<![\w-])--yes\b")
_CODE_API = re.compile(r"\bmain\s*\(|set_rule|human_set|record_human_accept|\boverride\s*\(|\barbitrate\s*\(|change_probe")
_CODE_PTY = re.compile(r"(?i)\bpty\b|openpty|forkpty|pexpect|ptyprocess")
_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([\w.-]+)\2[^\n]*\n")

HUMAN_MESSAGE = ("Accepting or rejecting rules (without --as NODE), overriding at root, resolving a conflict as root, "
                 "arbitrating, pinning, lifting a rule's probe, and confirming with --yes are decisions for the human (root), not for an "
                 "agent. Do not run them, fake a terminal for them, or work around this guard: stop and ask the "
                 "user to run the command in their own terminal.")
CONFIG_MESSAGE = ("fractal.toml is the repo's policy: its [policy] decides who may accept rules and how a root "
                  "decision is confirmed, so changing it is a human decision. Do not edit it or work around this "
                  "guard: stop and ask the user to make the change.")


def _is_opt(arg: str, long: str) -> bool:
    """`arg` names option `long`, in full or as an argparse prefix (`--b` for `--by`), with or without `=value`."""
    name = arg.split("=", 1)[0]
    return len(name) > 2 and name.startswith("--") and long.startswith(name)


def _options(args: list[str]) -> list[str]:
    """`args` up to a `--` terminator (after it nothing is an option)."""
    return args[:args.index("--")] if "--" in args else args


_ROOTISH = (None, "", "root")


def _maybe_root(args: list[str], long: str) -> bool:
    """Is option `long` absent, empty or root in ANY occurrence (argparse keeps the last; a reader may take the
    first), or computed by the shell (`$(...)`, `$X`, backticks: its value is unknown here)?"""
    args = _options(args)
    vals = [a.split("=", 1)[1] if "=" in a else (args[k + 1] if k + 1 < len(args) else "")
            for k, a in enumerate(args) if _is_opt(a, long)]
    return not vals or any(v in _ROOTISH or "$" in v or "`" in v for v in vals)


def _opt_value(args: list[str], long: str) -> str | None:
    """The value of option `long` (`--x V` or `--x=V`; "" when it has none), None when absent."""
    args = _options(args)
    for k, a in enumerate(args):
        if _is_opt(a, long):
            return a.split("=", 1)[1] if "=" in a else (args[k + 1] if k + 1 < len(args) else "")
    return None


def _fractal_args(words: list[str]) -> list[str] | None:
    """The CLI arguments when `words` (wrappers stripped) runs the fractal CLI, else None:
    `fractal`, `<path>/fractal`, `python[3] [opts] -m fractal_harness[.cli]`."""
    if not words:
        return None
    cmd = os.path.basename(words[0])
    if cmd == "fractal":
        return words[1:]
    if _PYTHON.match(cmd):
        i = 1
        while i < len(words) and words[i].startswith("-") and words[i] != "-":
            w = words[i]
            if w == "-m":
                return words[i + 2:] if words[i + 1:i + 2] and words[i + 1] in _FRACTAL_MODULES else None
            if w.startswith("-m"):
                return words[i + 1:] if w[2:] in _FRACTAL_MODULES else None
            if w == "-c":
                return None
            i += 2 if w in ("-X", "-W", "-Q") else 1
    return None


def _human_verb(args: list[str]) -> str | None:
    """Why these fractal CLI arguments are a human (root) decision, or None."""
    if any(_is_opt(a, "--yes") for a in _options(args)):
        return "`--yes` confirms a human decision non-interactively"
    i = 0
    while i < len(args) and args[i].startswith("-"):        # global options: --root X, --root=X, --json
        i += 2 if _is_opt(args[i], "--root") and "=" not in args[i] else 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    if sub == "accept":
        return "`fractal accept` without `--as NODE`" if _maybe_root(rest, "--as") else None
    if sub == "reject":
        return "`fractal reject`"
    if sub in ("override", "change"):
        return f"`fractal {sub}` at root (no `--by NODE`)" if _maybe_root(rest, "--by") else None
    if sub in ("pin", "unpin", "lift"):
        return f"`fractal {sub}`"
    if sub == "conflict":
        sub2 = next((a for a in rest if not a.startswith("-")), "")
        if sub2 == "arbitrate":
            return "`fractal conflict arbitrate`"
        if sub2 == "resolve" and _maybe_root(rest, "--as"):
            return "`fractal conflict resolve` as root (no `--as NODE`)"
    return None


def _code_human(code: str) -> str | None:
    """Why interpreter code reaches a human decision, or None. Textual."""
    if not _FRACTAL_WORD.search(code):
        return None
    if _CODE_PTY.search(code):
        return "code that drives fractal through a pseudo-terminal"
    if "fractal_harness" in code and _CODE_API.search(code):
        return "code that calls the fractal library behind the CLI's human checks"
    if _CODE_VERB.search(code):
        return "code that runs a human-only fractal verb"
    return None


def _human_simple(words: list[str], stdin_code: str, depth: int) -> str | None:
    w = _strip_wrappers(words)
    if not w:
        return None
    cmd, args = os.path.basename(w[0]), w[1:]
    fargs = _fractal_args(w)
    if fargs is not None:
        return _human_verb(fargs)
    if cmd in PTY_WRAPPERS:
        mentioned = any(_FRACTAL_WORD.search(a) for a in args)
        return f"`{cmd}` gives fractal a (pseudo-)terminal" if mentioned else None
    if cmd in SHELLS or cmd in ("eval", "ssh"):
        if depth < 4:
            for code in ([" ".join(args)] if cmd in ("eval", "ssh") else args) + [stdin_code]:
                why = human_decision(code, depth + 1)
                if why:
                    return why
        return _code_human(stdin_code)       # `echo 'fractal accept r1' | sh`: the piped text is the script
    if _PYTHON.match(cmd) or cmd in INTERPRETERS:
        return _code_human(" ".join(args) + "\n" + stdin_code)
    if cmd in READ_ONLY and cmd != "find":
        return None
    for j in range(1, len(w)):                # `xargs fractal accept`, `find -exec fractal ...`, `watch ...`
        fargs = _fractal_args(w[j:])
        why = _human_verb(fargs) if fargs is not None else None
        if why:
            return why
    return None


def _heredocs(command: str) -> list[tuple[str, str]]:
    """(the command text before `<<`, the body) for each heredoc."""
    out = []
    for m in _HEREDOC.finditer(command):
        prefix = command[command.rfind("\n", 0, m.start()) + 1:m.start()]
        body = []
        for ln in command[m.end():].split("\n"):
            if (ln.strip() if m[1] else ln) == m[3]:
                break
            body.append(ln)
        out.append((prefix, "\n".join(body)))
    return out


def human_decision(command: str, _depth: int = 0) -> str | None:
    """Why `command` would take a human-only (root) decision through the CLI, or None. HEURISTIC."""
    if not command or not command.strip() or not _FRACTAL_WORD.search(re.sub(r"[\"'\\]", "", command)):
        return None                         # fast path: every form below names fractal (quotes aside)
    for prefix, body in _heredocs(command):
        words = _strip_wrappers(prefix.split())
        if words and (_PYTHON.match(os.path.basename(words[0])) or os.path.basename(words[0]) in INTERPRETERS):
            why = _code_human(body)
            if why:
                return why
    try:
        text, extra = _normalize(command)
        segments = _segments(text)
    except ValueError:
        return "unparseable command that names a human-only fractal verb" if _code_human(command) else None
    if _depth < 4:
        for sub in extra:
            why = human_decision(sub, _depth + 1)
            if why:
                return why
    shell_vars: dict[str, str] = {}
    piped = ""
    for tokens, sep in segments:
        if shell_vars:
            tokens = [re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: shell_vars.get(m[1], m[0]), t) for t in tokens]
        assigns = [t for t in tokens if t != "export" and re.match(r"^[A-Za-z_]\w*=", t)]
        if assigns and all(t == "export" or t in assigns for t in tokens):
            shell_vars.update(a.split("=", 1) for a in assigns)
        words, stdin_code, k = [], piped, 0
        while k < len(tokens):
            t = tokens[k]
            if set(t) <= set("<>&|") and ("<" in t or ">" in t):     # redirections; `<<<` feeds code on stdin
                if t == "<<<" and k + 1 < len(tokens):
                    stdin_code += "\n" + tokens[k + 1]
                k += 2
                continue
            words.append(t)
            k += 1
        why = _human_simple(words, stdin_code, _depth)
        if why:
            return why
        piped = " ".join(words) if sep in ("|", "|&") else ""
    return None


# --- fractal.toml --------------------------------------------------------------------------

def config_files(root: Path, dirs: list[Path]) -> _Targets:
    """The fractal.toml files that govern a store: at the repo root and at each store's repo root."""
    paths = []
    for base in (root, *(d.parent for d in dirs)):
        try:
            paths.append((base / CONFIG_FILE).resolve())
        except (OSError, RuntimeError, ValueError):
            pass
    return _Targets(dict.fromkeys(paths), _TOML_MENTION)


def _as_config(why: str) -> Reason:
    return Reason(why.replace("a store path", CONFIG_FILE).replace("the store", CONFIG_FILE), "config")


def check(tool: str | None, inp: dict, cwd: Path, root: Path) -> tuple[str, str, Path] | None:
    """(what, why, store dir) when this tool call would write the store or fractal.toml, or take a
    human-only decision, else None. `why` is a `Reason`; its `kind` picks the message."""
    if tool not in GUARDED_TOOLS:
        return None
    dirs = store_dirs(root, cwd)
    if not dirs:
        return None
    configs = config_files(root, dirs)
    if tool == "Bash":
        command = inp.get("command")
        if not isinstance(command, str):
            return None
        why = human_decision(command)
        if why:
            return command, Reason(why, "human"), dirs[0]
        why = bash_write(command, cwd, dirs)
        if why:
            return command, Reason(why, "store"), dirs[0]
        why = bash_write(command, cwd, configs)
        return (command, _as_config(why), dirs[0]) if why else None
    path = inp.get("file_path") or inp.get("notebook_path")
    target = edit_target(path, cwd, dirs)
    if target:
        return str(target), Reason(f"{tool} of a file inside the store", "store"), dirs[0]
    target = edit_target(path, cwd, configs)
    return (str(target), Reason(f"{tool} of {CONFIG_FILE}", "config"), dirs[0]) if target else None


def block_message(tool: str, what: str, why: str) -> str:
    action = "command was NOT run" if tool == "Bash" else "edit was NOT applied"
    shown = what if len(what) <= 200 else what[:200] + "..."
    kind = getattr(why, "kind", "store")
    if kind == "human":
        return "\n".join([f"fractal: this {action}: it is a human decision (root) ({why}): {shown}", HUMAN_MESSAGE])
    if kind == "config":
        return "\n".join([f"fractal: this {action}: it would change {CONFIG_FILE} ({why}): {shown}",
                          CONFIG_MESSAGE])
    return "\n".join([f"fractal: this {action}: it would write the rule store ({why}): {shown}",
                      MESSAGE.format(store=STORE_DIR)])


def log_block(store: Path, tool: str, what: str, why: str) -> None:
    """Record the block in the store's event log. Never raises."""
    try:
        from .store import Store
        s = Store(store / "edges.db")
        try:
            s.log("store_write_blocked", None, tool=tool, command=what, reason=why)
        finally:
            s.close()
    except Exception:
        pass
