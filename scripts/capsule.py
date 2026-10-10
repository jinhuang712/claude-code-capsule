#!/usr/bin/env python3
"""claude-code-capsule: pack one Claude Code session into a zip (ship) and restore it on any machine (land).

Stdlib only, Python >= 3.9. Every subcommand prints one JSON object on stdout.
Exit codes: 0 ok · 1 error · 3 conflict (re-run with --force or --new-id) · 4 capsule damaged / verify failed

A capsule also runs by itself, without the plugin:  python3 capsule-<id>.zip land --dir <folder>
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
import zlib
from pathlib import Path

VERSION = "0.1.0"
FORMAT = "claude-code-capsule/1"
EXIT_OK, EXIT_ERR, EXIT_CONFLICT, EXIT_DAMAGED = 0, 1, 3, 4
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CHUNK = 1 << 20

# Per-session folders under the config dir (~/.claude) that are named after the session ID.
PER_SESSION_DIRS = {
    "file_history": "file-history",
    "session_env": "session-env",
    "tasks": "tasks",
    "uploads": "uploads",
    "image_cache": "image-cache",
    "dev_mods": "dev-mods",
}
# Parts whose text is Claude Code's own record of the session; paths inside them get remapped on land.
# Everything else (file-history snapshots, scratchpad, uploads, plan files, pastes) is content and lands
# byte for byte.
REWRITE_ROLES = {"transcript", "session_dir", "memory", "session_env", "tasks"}
# Parts shared with other sessions: an existing file that differs is kept, never overwritten.
ADD_ONLY_ROLES = {"memory", "config_ref"}
# Shared folders under the config dir whose files the transcript may point at by absolute path.
CONFIG_REF_DIRS = ("plans", "paste-cache")
# JSON keys whose values are opaque or signed and must never change.
FROZEN_KEYS = {"signature", "encrypted_content"}
FROZEN_BLOCKS = {"thinking", "redacted_thinking"}
PATH_KEYS = ("file_path", "path", "notebook_path", "cwd")
TEMP_SEGMENTS = {"tmp", "temp", "scratch"}


class CapsuleError(Exception):
    def __init__(self, msg: str, code: int = EXIT_ERR, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


# ---------------------------------------------------------------- environment


def home() -> str:
    return os.path.expanduser("~")


def config_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def tmp_root() -> str:
    """realpath(CLAUDE_CODE_TMPDIR or /tmp on macOS, os.tmpdir() elsewhere) + /claude-<uid>."""
    base = os.environ.get("CLAUDE_CODE_TMPDIR")
    if not base:
        if sys.platform == "darwin":
            base = "/tmp"
        else:
            base = next((os.environ[k] for k in ("TMPDIR", "TMP", "TEMP") if os.environ.get(k)), "/tmp")
    return os.path.join(os.path.realpath(base), f"claude-{os.getuid()}")


SLUG_MAX = 200


def slugify(path: str) -> str:
    """Project folder name Claude Code derives from a working directory.

    Mirrors the CLI: every non-alphanumeric UTF-16 code unit becomes "-"; past 200 characters the
    name is cut and gets "-" + base36(abs(java_hash(path))).
    """
    s = "".join(c if c.isascii() and c.isalnum() else "-" * (2 if ord(c) > 0xFFFF else 1) for c in path)
    if len(s) <= SLUG_MAX:
        return s
    h = 0
    units = path.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(units), 2):
        h = (h * 31 + int.from_bytes(units[i:i + 2], "little")) & 0xFFFFFFFF
    h = abs(h - (1 << 32) if h & 0x80000000 else h)
    digits = ""
    while True:
        h, r = divmod(h, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + digits
        if not h:
            break
    return f"{s[:SLUG_MAX]}-{digits}"


def project_dir_name(cwd: str) -> str:
    pinned = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    return pinned if pinned and os.environ.get("CLAUDE_CONFIG_DIR") else slugify(cwd)


def cleanup_days() -> int:
    """cleanupPeriodDays from user settings; Claude Code deletes older transcripts at startup."""
    try:
        v = json.loads((config_dir() / "settings.json").read_text()).get("cleanupPeriodDays")
        return int(v) if isinstance(v, (int, float)) and v >= 1 else 30
    except (OSError, ValueError, AttributeError):
        return 30


def is_under(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/") or root == "/"


def looks_temporary(path: str) -> bool:
    real = os.path.realpath(path) if os.path.exists(path) else path
    if any(is_under(real, t) for t in ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")):
        return True
    return any(seg.lower() in TEMP_SEGMENTS for seg in Path(path).parts)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_processes(sid: str) -> list[dict]:
    found = []
    for f in (config_dir() / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        pid = d.get("pid")
        if d.get("sessionId") == sid and isinstance(pid, int) and pid_alive(pid):
            found.append({"pid": pid, "status": d.get("status")})
    return found


def git(args: list[str], cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# ---------------------------------------------------------------- session discovery


def find_transcript(ref: str) -> Path:
    ref = ref.strip().lower()
    if not re.fullmatch(r"[0-9a-f-]{4,36}", ref):
        raise CapsuleError(f"not a session ID: {ref!r}")
    projects = config_dir() / "projects"
    hits = [p for p in projects.glob("*/*.jsonl") if UUID_RE.match(p.stem) and p.stem.startswith(ref)]
    ids = sorted({p.stem for p in hits})
    if not hits:
        raise CapsuleError(f"no session matches {ref!r} under {projects}")
    if len(ids) > 1:
        raise CapsuleError(f"{ref!r} matches {len(ids)} sessions; give more characters", matches=ids[:10])
    if len(hits) > 1:
        raise CapsuleError(
            f"session {ids[0]} exists in {len(hits)} project folders; remove the stale copy first",
            paths=[str(p) for p in hits],
        )
    return hits[0]


def read_jsonl(path: Path) -> list:
    out = []
    with open(path, "rb") as f:
        for raw in f:
            try:
                out.append(json.loads(raw))
            except ValueError:
                out.append(None)
    return out


def history_lines(sid: str) -> list[bytes]:
    hist = config_dir() / "history.jsonl"
    if not hist.exists():
        return []
    keep = []
    with open(hist, "rb") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("sessionId") == sid:
                keep.append(raw.rstrip(b"\r\n"))
    return keep


def first_prompts() -> dict[str, str]:
    hist = config_dir() / "history.jsonl"
    titles: dict[str, str] = {}
    if not hist.exists():
        return titles
    with open(hist, "rb") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            sid = d.get("sessionId") if isinstance(d, dict) else None
            if sid and sid not in titles and isinstance(d.get("display"), str):
                titles[sid] = d["display"].strip().replace("\n", " ")[:100]
    return titles


TITLE_KEYS = (("custom-title", "customTitle"), ("ai-title", "aiTitle"), ("summary", "summary"))


def session_title(lines: list, fallback: str | None) -> str | None:
    """Latest /rename title, else latest auto title, else summary, else the first prompt."""
    found: dict[str, str] = {}
    for d in lines:
        if isinstance(d, dict):
            for t, key in TITLE_KEYS:
                if d.get("type") == t and isinstance(d.get(key), str) and d[key].strip():
                    found[t] = d[key].strip()
    return next((found[t] for t, _ in TITLE_KEYS if t in found), fallback)


def tail_title(path: Path, size: int) -> str | None:
    with open(path, "rb") as f:
        f.seek(max(0, size - 256 * 1024))
        tail = f.read().split(b"\n")[1:] if size > 256 * 1024 else f.read().split(b"\n")
    lines = []
    for raw in tail:
        if b"title" in raw or b'"summary"' in raw:
            try:
                lines.append(json.loads(raw))
            except ValueError:
                pass
    return session_title(lines, None)


def session_cwd(lines: list, slug: str) -> str:
    """The folder the session is filed under: the last /cd relocation, else the head 'cwd' (as the CLI reads it)."""
    relocated = [d["relocatedCwd"] for d in lines if isinstance(d, dict) and d.get("type") == "relocated"
                 and isinstance(d.get("relocatedCwd"), str)]
    counts: dict[str, int] = {}
    for d in lines:
        c = d.get("cwd") if isinstance(d, dict) else None
        if isinstance(c, str) and c.startswith("/"):
            counts[c] = counts.get(c, 0) + 1
    candidates = relocated[::-1] + list(counts)
    exact = [c for c in candidates if slugify(c) == slug]
    if exact:
        return exact[0]
    if candidates:
        return relocated[-1] if relocated else max(counts, key=counts.get)
    raise CapsuleError("cannot tell the session's working folder: no 'cwd' in the transcript")


def referenced_paths(lines: list, home_dir: str) -> set[str]:
    found: set[str] = set()
    cmd_pat = re.compile(r"(?<![\w.~/-])" + re.escape(home_dir) + r"(?:/[\w.@+~-]+)+")
    for d in lines:
        if not isinstance(d, dict):
            continue
        c = d.get("cwd")
        if isinstance(c, str) and c.startswith("/"):
            found.add(c)
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("input"), dict)):
                continue
            inp = b["input"]
            for k in PATH_KEYS:
                v = inp.get(k)
                if isinstance(v, str) and v.startswith("/"):
                    found.add(v)
            cmd = inp.get("command")
            if isinstance(cmd, str):
                found.update(cmd_pat.findall(cmd))
    return found


def repo_state(top: str) -> dict:
    remotes = (git(["remote"], top) or "").split()
    name = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
    porcelain = git(["status", "--porcelain"], top)
    upstream = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], top)
    unpushed = git(["rev-list", "--count", "@{u}..HEAD"], top) if upstream else None
    return {
        "path": top,
        "remote": git(["remote", "get-url", name], top) if name else None,
        "branch": git(["branch", "--show-current"], top) or None,
        "head": git(["rev-parse", "HEAD"], top),
        "dirty": len(porcelain.splitlines()) if porcelain else 0,
        "upstream": upstream,
        "unpushed": int(unpushed) if unpushed and unpushed.isdigit() else (0 if upstream else None),
    }


def locate_repos(paths: set[str], cwd: str, home_dir: str, skip: list[str]) -> tuple[list[dict], list[str]]:
    """Git repos and other folders under home that the session touched, outside the config/temp dirs."""
    tops: list[str] = []
    roots: set[str] = set()
    checked: set[str] = set()
    for p in sorted(paths):
        if any(is_under(p, s) for s in skip):
            continue
        if not (is_under(p, home_dir) or is_under(p, cwd) or is_under(p, "/Volumes")):
            continue
        d = p
        while d not in ("/", "") and not os.path.isdir(d):
            d = os.path.dirname(d)
        if d in ("/", "", home_dir) or d in checked or any(is_under(d, t) for t in tops):
            continue
        checked.add(d)
        if len(checked) > 400:
            break
        top = git(["rev-parse", "--show-toplevel"], d)
        if top:
            # git prints the resolved path (/private/tmp/x for /tmp/x); keep the spelling the session used
            real_d = os.path.realpath(d)
            if is_under(real_d, top) and not is_under(d, top):
                top = os.path.normpath(os.path.join(d, os.path.relpath(top, real_d)))
            tops.append(top)
        elif is_under(d, home_dir) and not is_under(d, cwd):
            rel = os.path.relpath(d, home_dir).split(os.sep)
            roots.add(os.path.join(home_dir, *rel[:2]))
    repos = []
    for top in sorted(set(tops)):
        st = repo_state(top)
        st["role"] = "workdir" if is_under(cwd, top) else "referenced"
        repos.append(st)
    return repos, sorted(r for r in roots if not any(is_under(r, t["path"]) for t in repos))


# ---------------------------------------------------------------- ship


def arc_name(role: str, rel: str) -> str:
    if role == "transcript":
        return "capsule/transcript.jsonl"
    return f"capsule/{role}/{rel}" if rel else f"capsule/{role}"


def file_entry(role: str, rel: str, src: Path) -> dict | None:
    st = os.lstat(src)
    e = {"role": role, "rel": rel, "arc": arc_name(role, rel), "mode": stat.S_IMODE(st.st_mode), "mtime": st.st_mtime}
    if stat.S_ISLNK(st.st_mode):
        e.update(type="link", target=os.readlink(src))
    elif stat.S_ISDIR(st.st_mode):
        e["type"] = "dir"
    elif stat.S_ISREG(st.st_mode):
        e.update(type="file", size=st.st_size)
    else:
        return None
    return e


def collect(parts: list[tuple[str, Path, str]]) -> tuple[list[tuple[dict, Path]], list[str]]:
    """Entries to pack, plus what was left out. Links are kept only in the temp tree (land refuses others);
    elsewhere a link to a file is packed as that file and a link to a folder is skipped."""
    out: list[tuple[dict, Path]] = []
    skipped: list[str] = []

    def add(role: str, rel: str, p: Path):
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            skipped.append(f"{p} (name is not valid UTF-8)")
            return
        e = file_entry(role, rel, p)
        if e and e["type"] == "link" and (role != "tmp" or not rel):
            if p.is_file():
                e = file_entry(role, rel, p.resolve())
            else:
                skipped.append(f"{p} (link to a folder)")
                return
        if e:
            out.append((e, p))

    for role, src, base_rel in parts:
        add(role, base_rel, src)
        if src.is_symlink() or not src.is_dir():
            continue
        for root, dirs, files in os.walk(src, followlinks=False):
            dirs.sort()
            for name in dirs + sorted(files):
                p = Path(root) / name
                add(role, "/".join(filter(None, [base_rel, p.relative_to(src).as_posix()])), p)
    return out, skipped


def zip_time(ts: float) -> tuple:
    t = dt.datetime.fromtimestamp(max(ts, 315532800 + 86400))  # zip cannot store dates before 1980
    return t.timetuple()[:6]


def zip_add(zf: zipfile.ZipFile, e: dict, src: Path) -> None:
    if e["type"] == "dir":
        zi = zipfile.ZipInfo(e["arc"].rstrip("/") + "/", date_time=zip_time(e["mtime"]))
        zi.create_system = 3
        zi.external_attr = ((stat.S_IFDIR | e["mode"]) << 16) | 0x10
        zf.writestr(zi, b"")
        return
    zi = zipfile.ZipInfo(e["arc"], date_time=zip_time(e["mtime"]))
    zi.create_system = 3
    if e["type"] == "link":
        zi.external_attr = (stat.S_IFLNK | 0o777) << 16
        data = e["target"].encode("utf-8", "surrogateescape")
        e["sha256"] = sha256_bytes(data)
        zf.writestr(zi, data)
        return
    zi.external_attr = (stat.S_IFREG | e["mode"]) << 16
    zi.compress_type = zipfile.ZIP_DEFLATED
    h, n = hashlib.sha256(), 0
    with open(src, "rb") as f, zf.open(zi, "w", force_zip64=e["size"] > (1 << 31) - 1) as w:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            w.write(chunk)
            n += len(chunk)
    e["sha256"], e["size"] = h.hexdigest(), n


def own_source() -> bytes:
    try:
        return Path(__file__).read_bytes()
    except OSError:
        with zipfile.ZipFile(sys.argv[0]) as z:
            return z.read("__main__.py")


def readme_text(m: dict, zip_name: str) -> str:
    s, src = m["session"], m["source"]
    return f"""Claude Code session capsule ({FORMAT}, made by claude-code-capsule {VERSION})

Session:  {s['id']}
Title:    {s.get('title') or '-'}
From:     {src['user']}@{src['host']} ({src['platform']}), folder {src['cwd']}
Packed:   {m['created_at']}

Restore with the plugin:     /capsule:land {zip_name}
Restore without the plugin:  python3 {zip_name} land --dir <folder for the session>
Then resume:                 cd <folder> && claude --resume {s['id']}

Not inside: the files in the working folder and in any repo the session touched.
Move them yourself (for example git push / git pull) and check out the same commit.
The capsule is plain text and holds every tool output from the session. Keep it private.
"""


def cmd_ship(a) -> dict:
    cfg = config_dir()
    transcript = find_transcript(a.session)
    sid, slug = transcript.stem, transcript.parent.name
    lines = read_jsonl(transcript)
    text = transcript.read_text("utf-8", "surrogateescape")
    cwd = session_cwd(lines, slug)
    home_dir = home()

    # temp root as the session itself recorded it (e.g. /private/tmp/claude-501); the first candidate that holds
    # this session's folder wins, then this machine's own temp root
    found = re.findall(r"(?:(?<=[\s\"'`=(:,\[{>])|(?<=\\n)|(?<=\\t)|^)(/[^\s\"'`<>|\\]*?/claude-\d+)/"
                       + re.escape(slug) + "/" + re.escape(sid), text)
    candidates = list(dict.fromkeys(found + [tmp_root()]))
    old_tmp = next((c for c in candidates if (Path(c) / slug / sid).is_dir()), candidates[0])

    parts: list[tuple[str, Path, str]] = [("transcript", transcript, "")]
    sdir = transcript.parent / sid
    if sdir.is_dir():
        parts.append(("session_dir", sdir, ""))
    mem = transcript.parent / "memory"
    if mem.is_dir():
        parts.append(("memory", mem, ""))
    for role, folder in PER_SESSION_DIRS.items():
        p = cfg / folder / sid
        if os.path.lexists(p):
            parts.append((role, p, ""))
    tdir = Path(old_tmp) / slug / sid
    if tdir.is_dir() and not tdir.is_symlink():
        parts.append(("tmp", tdir, ""))

    # transcripts of the session and its subagents: for shared config files and repo detection
    all_lines, texts = list(lines), [text]
    if sdir.is_dir():
        for sub in sorted(sdir.rglob("*.jsonl")):
            all_lines += read_jsonl(sub)
            texts.append(sub.read_text("utf-8", "surrogateescape"))
    hist = history_lines(sid)
    texts += [h.decode("utf-8", "surrogateescape") for h in hist]
    ref_pat = re.compile(re.escape(str(cfg)) + r"/((?:" + "|".join(CONFIG_REF_DIRS) + r")/[^\s\"'`<>|\\]+)")
    refs = {mm.group(1).rstrip(".,;:)") for t in texts for mm in ref_pat.finditer(t)}
    for h in hist:  # large pastes in prompt history live in paste-cache/<contentHash>.txt
        try:
            pasted = json.loads(h).get("pastedContents") or {}
        except (ValueError, AttributeError):
            continue
        for v in pasted.values() if isinstance(pasted, dict) else []:
            if isinstance(v, dict) and re.fullmatch(r"[0-9a-f]{8,64}", str(v.get("contentHash", ""))):
                refs.add(f"paste-cache/{v['contentHash']}.txt")
    for rel in sorted(refs):
        p = cfg / rel
        if p.is_file() and not p.is_symlink():
            parts.append(("config_ref", p, rel))

    entries, skipped = collect(parts)
    repos, roots = locate_repos(
        referenced_paths(all_lines, home_dir), cwd, home_dir, [str(cfg), old_tmp, tmp_root()]
    )
    wrepo = next((r for r in repos if r["role"] == "workdir"), None)
    workdir = {"path": cwd, "exists": os.path.isdir(cwd), "git": wrepo is not None,
               "is_home": cwd.rstrip("/") == home_dir.rstrip("/")}
    if wrepo and wrepo["path"] != cwd and is_under(cwd, wrepo["path"]):  # session ran in a subfolder of the repo
        workdir.update(repo_top=wrepo["path"], rel=os.path.relpath(cwd, wrepo["path"]))
    if workdir["exists"] and not workdir["git"] and not workdir["is_home"]:  # nobody copies a whole home folder
        workdir["entries"] = sum(1 for _ in os.scandir(cwd))

    running = live_processes(sid)
    warnings = []
    if running:
        warnings.append(
            f"session is still running (pid {', '.join(str(r['pid']) for r in running)}); "
            "the capsule holds it up to now. Exit it before you continue on the other machine."
        )
    if sid == os.environ.get("CLAUDE_CODE_SESSION_ID"):
        warnings.append("this is the session you are in right now; the capsule stops at this point")
    warnings.append(
        "the working folder and other files the session touched are NOT in the capsule; "
        "move them yourself and keep the same layout"
    )
    if workdir.get("entries"):
        warnings.append(f"working folder {cwd} is not a git repo and has {workdir['entries']} entries; copy it yourself")
    for r in repos:
        if r["dirty"]:
            warnings.append(f"{r['path']}: {r['dirty']} uncommitted change(s); commit and push them first")
        if r["upstream"] is None:
            warnings.append(f"{r['path']}: branch {r['branch'] or '(detached)'} has no upstream; push it first")
        elif r["unpushed"]:
            warnings.append(f"{r['path']}: {r['unpushed']} unpushed commit(s) on {r['branch']}; push them first")
    if skipped:
        warnings.append(f"left out {len(skipped)} item(s): " + "; ".join(skipped[:5]))
    warnings.append("the capsule is plain text and holds every tool output of the session; send it privately")

    created = dt.datetime.now().astimezone()
    out = Path(os.path.expanduser(a.out)) if a.out else None
    if out is None:
        desktop = Path(home_dir) / "Desktop"
        out = desktop if desktop.is_dir() else Path.cwd()
    if out.suffix != ".zip":
        out = out / f"capsule-{sid[:8]}-{created.strftime('%Y%m%d-%H%M%S')}.zip"
    if os.path.lexists(out):
        raise CapsuleError(f"{out} already exists; pass another --out")
    out.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "format": FORMAT,
        "tool_version": VERSION,
        "created_at": created.isoformat(timespec="seconds"),
        "session": {
            "id": sid,
            "title": session_title(lines, first_prompts().get(sid)),
            "messages": sum(1 for d in lines if isinstance(d, dict) and d.get("type") in ("user", "assistant")),
            "claude_version": next(
                (d["version"] for d in reversed(lines) if isinstance(d, dict) and isinstance(d.get("version"), str)),
                None,
            ),
            "first_at": next((d["timestamp"] for d in lines if isinstance(d, dict) and d.get("timestamp")), None),
            "last_at": next((d["timestamp"] for d in reversed(lines) if isinstance(d, dict) and d.get("timestamp")), None),
        },
        "source": {
            "user": getpass.getuser(),
            "host": socket.gethostname(),
            "platform": f"{platform.system()} {platform.machine()}",
            "uid": os.getuid(),
            "home": home_dir,
            "config_dir": str(cfg),
            "tmp_root": old_tmp,
            "cwd": cwd,
            "slug": slug,
        },
        "running": running,
        "workdir": workdir,
        "repos": repos,
        "external_roots": roots,
        "history": {"arc": "capsule/history.jsonl", "lines": len(hist)},
        "files": [],
    }
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".capsule-", suffix=".zip.part")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
            zf.writestr("__main__.py", own_source())
            for e, src in entries:
                zip_add(zf, e, src)
                manifest["files"].append(e)
            hist_data = b"".join(h + b"\n" for h in hist)
            manifest["history"]["sha256"] = sha256_bytes(hist_data)
            zf.writestr(manifest["history"]["arc"], hist_data, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("capsule/manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False),
                        compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("README.txt", readme_text(manifest, out.name))
        os.replace(tmp, out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    files = [e for e in manifest["files"] if e["type"] == "file"]
    return {
        "zip": str(out),
        "zip_bytes": out.stat().st_size,
        "session": manifest["session"],
        "cwd": cwd,
        "parts": sorted({e["role"] for e in manifest["files"]}),
        "files": len(files),
        "links": sum(1 for e in manifest["files"] if e["type"] == "link"),
        "bytes": sum(e["size"] for e in files),
        "history_lines": len(hist),
        "workdir": workdir,
        "repos": repos,
        "external_roots": roots,
        "warnings": warnings,
        "land": {"plugin": f"/capsule:land {out}", "standalone": f"python3 {out} land --dir <folder>"},
    }


# ---------------------------------------------------------------- land


class Rewriter:
    """Single-pass, longest-match-first replacement of old paths and tokens.

    kind "path":  the path and everything below it, only at path boundaries, so /Users/a does not touch
                  /Users/ab, /Users/a.txt or /backup/Users/a (a sentence-ending "/Users/a." still matches).
    kind "exact": the path itself but nothing below it.
    kind "token": a whole dash-separated word (project folder name, session ID).
    """

    BEHIND = r"(?:(?<![\w.~/+-])|(?<=file://))"
    AHEAD = {"path": r"(?![\w~+-]|\.[\w~+-])", "exact": r"(?![\w~+/-]|\.[\w~+-])", "token": r"(?![A-Za-z0-9-])"}
    PRIORITY = {"exact": 0, "path": 1, "token": 2}

    def __init__(self, maps: list[tuple[str, str, str]]):
        items = [(o, n, k) for o, n, k in maps if o and o != n]
        items.sort(key=lambda x: (-len(x[0]), self.PRIORITY[x[2]]))
        self.new = [n for _, n, _ in items]
        alts = []
        for o, _, kind in items:
            behind = r"(?<![A-Za-z0-9-])" if kind == "token" else self.BEHIND
            alts.append("(" + behind + re.escape(o) + self.AHEAD[kind] + ")")
        self.rx = re.compile("|".join(alts)) if alts else None
        self.needles = sorted({o for o, _, _ in items})

    def hit(self, s: str) -> bool:
        return any(n in s for n in self.needles)

    def text(self, s: str, json_escape: bool = False) -> str:
        if not self.rx or not self.hit(s):
            return s
        if json_escape:
            return self.rx.sub(lambda m: json.dumps(self.new[m.lastindex - 1], ensure_ascii=False)[1:-1], s)
        return self.rx.sub(lambda m: self.new[m.lastindex - 1], s)

    def obj(self, o):
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, list):
            return [self.obj(x) for x in o]
        if isinstance(o, dict):
            kind = o.get("type")
            if not isinstance(kind, str):  # e.g. a JSON schema's "type": {...} inside a tool input
                kind = None
            if kind in FROZEN_BLOCKS:  # signed by the API; any change breaks the resumed conversation
                return o
            out = {}
            for k, v in o.items():
                if k in FROZEN_KEYS or (k == "data" and kind == "base64"):
                    out[k] = v
                else:
                    out[self.text(k)] = self.obj(v)
            return out
        return o

    @staticmethod
    def dump(o, **kw) -> bytes:
        try:  # surrogateescape gives back the exact bytes of any invalid UTF-8 the file held
            return json.dumps(o, ensure_ascii=False, **kw).encode("utf-8", "surrogateescape")
        except UnicodeEncodeError:
            return json.dumps(o, **kw).encode("ascii")

    def jsonl(self, data: bytes) -> bytes:
        out = []
        for raw in data.splitlines(keepends=True):
            body = raw.rstrip(b"\r\n")
            nl = raw[len(body):]
            s = body.decode("utf-8", "surrogateescape")
            if not self.hit(s):
                out.append(raw)
                continue
            try:
                o = json.loads(s)
            except ValueError:  # a truncated line, e.g. from a crash: rewrite it as text
                out.append(self.text(s, json_escape=True).encode("utf-8", "surrogateescape") + nl)
                continue
            new = self.obj(o)
            out.append(raw if new == o else self.dump(new, separators=(",", ":")) + nl)
        return b"".join(out)

    def file(self, data: bytes, name: str) -> bytes:
        if not self.rx or b"\0" in data:  # nothing to map, or binary (e.g. an image an MCP tool returned)
            return data
        if name.endswith(".jsonl"):
            return self.jsonl(data)
        s = data.decode("utf-8", "surrogateescape")
        if not self.hit(s):
            return data
        if name.endswith(".json"):
            try:
                o = json.loads(s)
            except ValueError:
                return self.text(s, json_escape=True).encode("utf-8", "surrogateescape")
            new = self.obj(o)
            if new == o:
                return data
            pretty = "\n" in s.strip()
            out = self.dump(new, indent=2) if pretty else self.dump(new, separators=(",", ":"))
            return out + (b"\n" if s.endswith("\n") else b"")
        return self.text(s).encode("utf-8", "surrogateescape")


def self_zip() -> str | None:
    p = sys.argv[0]
    return p if p.endswith(".zip") and zipfile.is_zipfile(p) else None


ROLES = {"transcript", "session_dir", "memory", "tmp", "config_ref", *PER_SESSION_DIRS}


def validate_manifest(m: dict) -> None:
    def need(cond, what: str):
        if not cond:
            raise CapsuleError(f"capsule manifest is malformed: {what}", EXIT_DAMAGED)

    try:
        need(isinstance(m["session"]["id"], str) and UUID_RE.match(m["session"]["id"]), "session.id")
        src = m["source"]
        for k in ("home", "config_dir", "tmp_root", "cwd"):
            need(isinstance(src[k], str) and src[k].startswith("/"), f"source.{k}")
        need(isinstance(src["slug"], str) and src["slug"], "source.slug")
        need(isinstance(m["history"]["arc"], str) and isinstance(m["history"]["sha256"], str), "history")
        need(isinstance(m["files"], list), "files")
        for e in m["files"]:
            need(e["role"] in ROLES and e["type"] in ("file", "dir", "link"), f"role/type of {e.get('arc')}")
            need(isinstance(e["rel"], str) and isinstance(e["arc"], str) and e["arc"].startswith("capsule/"),
                 f"rel/arc of {e.get('arc')}")
            need(isinstance(e["mode"], int) and isinstance(e["mtime"], (int, float)), f"mode/mtime of {e['arc']}")
            if e["type"] != "dir":
                need(isinstance(e["sha256"], str) and len(e["sha256"]) == 64, f"sha256 of {e['arc']}")
            if e["type"] == "file":
                need(isinstance(e["size"], int), f"size of {e['arc']}")
            if e["type"] == "link":
                need(isinstance(e["target"], str), f"target of {e['arc']}")
    except (KeyError, TypeError, AttributeError) as ex:
        raise CapsuleError(f"capsule manifest is malformed: missing {ex}", EXIT_DAMAGED)


def open_capsule(path: str | None) -> tuple[zipfile.ZipFile, dict, str]:
    path = path or self_zip()
    if not path:
        raise CapsuleError("give the capsule zip to use")
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise CapsuleError(f"no such file: {path}")
    try:
        zf = zipfile.ZipFile(path)
        m = json.loads(zf.read("capsule/manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise CapsuleError(f"not a readable capsule: {e}", EXIT_DAMAGED)
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise CapsuleError(f"unsupported capsule format {m.get('format') if isinstance(m, dict) else None!r}; "
                           "update claude-code-capsule")
    validate_manifest(m)
    return zf, m, os.path.abspath(path)


def verify_capsule(zf: zipfile.ZipFile, m: dict) -> None:
    bad = []
    checks = [(e["arc"], e["sha256"]) for e in m["files"] if e["type"] != "dir"]
    checks.append((m["history"]["arc"], m["history"]["sha256"]))
    for arc, want in checks:
        h = hashlib.sha256()
        try:
            with zf.open(arc) as f:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    h.update(chunk)
        except (KeyError, zipfile.BadZipFile, OSError, EOFError, zlib.error):
            bad.append(arc)
            continue
        if h.hexdigest() != want:
            bad.append(arc)
    if bad:
        raise CapsuleError(f"{len(bad)} file(s) in the capsule are damaged; copy it again", EXIT_DAMAGED, files=bad[:20])


def check_entries(m: dict) -> None:
    """Links only inside the temp tree, never as a part's own folder, and nothing stored beneath a link."""
    links = set()
    for e in m["files"]:
        rel = e["rel"]
        parts = Path(rel).parts
        if rel.startswith("/") or ".." in parts:
            raise CapsuleError(f"unsafe path in capsule: {e['role']}/{rel}", EXIT_DAMAGED)
        if e["role"] == "config_ref" and (len(parts) < 2 or parts[0] not in CONFIG_REF_DIRS or e["type"] != "file"):
            raise CapsuleError(f"unexpected shared file in capsule: {rel}", EXIT_DAMAGED)
        if e["role"] == "transcript" and (rel or e["type"] != "file"):
            raise CapsuleError("capsule transcript entry is not a plain file", EXIT_DAMAGED)
        if e["type"] == "link":
            if e["role"] != "tmp" or not rel:
                raise CapsuleError(f"unexpected link in capsule: {e['role']}/{rel}", EXIT_DAMAGED)
            links.add(rel)
    for e in m["files"]:
        parts = Path(e["rel"]).parts
        for i in range(1, len(parts)):
            if e["role"] == "tmp" and "/".join(parts[:i]) in links:
                raise CapsuleError(f"capsule stores {e['rel']} beneath a link", EXIT_DAMAGED)


class Target:
    """Where a session lands on this machine."""

    def __init__(self, m: dict, workdir: str, maps: list[str], new_id: bool):
        src = m["source"]
        self.old_sid = m["session"]["id"]
        self.sid = str(uuid.uuid4()) if new_id else self.old_sid
        # Claude Code records the resolved working folder (/private/tmp/x for /tmp/x), so file it under that
        self.cwd = os.path.realpath(os.path.abspath(os.path.expanduser(workdir)))
        self.slug = project_dir_name(self.cwd)
        self.cfg = config_dir()
        self.tmp = tmp_root()
        self.home = home()
        user_maps = []
        for spec in maps:
            if "=" not in spec:
                raise CapsuleError(f"--map needs OLD=NEW, got {spec!r}")
            old, new = spec.split("=", 1)
            old, new = old.rstrip("/"), os.path.abspath(os.path.expanduser(new))
            if not old.startswith("/"):
                raise CapsuleError(f"--map OLD must be an absolute path from the source machine: {old!r}")
            user_maps.append((old, new, "path"))
        old_cwd = src["cwd"].rstrip("/") or "/"
        anchors = [src["home"], src["config_dir"], src["tmp_root"]]
        # A folder that holds the home, config or temp root moves only itself; what lies below follows those maps.
        cwd_kind = "exact" if any(is_under(a, old_cwd) for a in anchors) else "path"
        auto = [(old_cwd, self.cwd, cwd_kind)]
        # A session that ran in a subfolder of a repo keeps the repo's layout: the repo root moves with it.
        wd = m.get("workdir") or {}
        self.repo_map = None
        top, rel = wd.get("repo_top"), wd.get("rel")
        if isinstance(top, str) and isinstance(rel, str) and rel and not any(is_under(a, top) for a in anchors):
            new_top = self.cwd
            for _ in Path(rel).parts:
                new_top = os.path.dirname(new_top)
            self.repo_map = (top, new_top)
            auto.append((top, new_top, "path"))
        auto += [(src["tmp_root"], self.tmp, "path"), (src["config_dir"], str(self.cfg), "path"),
                 (src["home"], self.home, "path")]
        maps3 = user_maps + auto  # first wins below, so a --map overrides an automatic one
        # macOS reports /tmp and /var as /private/...; catch both spellings
        for old, new, kind in list(maps3):
            for a, b in (("/private/tmp/", "/tmp/"), ("/private/var/", "/var/")):
                if old.startswith(a):
                    maps3.append((b + old[len(a):], new, kind))
                elif old.startswith(b):
                    maps3.append((a + old[len(b):], new, kind))
        seen, uniq = set(), []
        for old, new, kind in maps3:
            if (old, kind) not in seen:
                seen.add((old, kind))
                uniq.append((old, new, kind))
        self.path_maps = uniq
        self.rw = Rewriter(uniq + [(src["slug"], self.slug, "token"), (self.old_sid, self.sid, "token")])

    def base(self, role: str) -> Path:
        project = self.cfg / "projects" / self.slug
        if role == "transcript":
            return project / f"{self.sid}.jsonl"
        if role == "session_dir":
            return project / self.sid
        if role == "memory":
            return project / "memory"
        if role in PER_SESSION_DIRS:
            return self.cfg / PER_SESSION_DIRS[role] / self.sid
        if role == "tmp":
            return Path(self.tmp) / self.slug / self.sid
        return self.cfg  # config_ref

    def trusted_root(self, e: dict) -> Path:
        """Deepest folder a user may have symlinked on purpose; below it nothing may be a symlink."""
        role = e["role"]
        if role == "tmp":
            return Path(self.tmp)
        if role == "config_ref":
            return self.cfg / Path(e["rel"]).parts[0]
        if role in ("transcript", "session_dir", "memory"):
            return self.cfg / "projects"
        return self.cfg / PER_SESSION_DIRS[role]

    def path(self, e: dict) -> Path:
        b = self.base(e["role"])
        return b / e["rel"] if e["rel"] else b

    def resume_command(self) -> str:
        return f"cd {shell_quote(self.cwd)} && claude --resume {self.sid}"


def shell_quote(s: str) -> str:
    return s if re.fullmatch(r"[A-Za-z0-9_./~@%+=:,-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def invalid_lines(data: bytes) -> int:
    n = 0
    for line in data.splitlines():
        if line.strip():
            try:
                json.loads(line)
            except ValueError:
                n += 1
    return n


def plan_land(zf: zipfile.ZipFile, m: dict, t: Target) -> list[dict]:
    """Decide every write before the first one happens; raise on anything unsafe."""
    check_entries(m)
    session_roots = [str(t.base("session_dir")), str(t.base("tmp"))]
    items = []
    for e in m["files"]:
        p = t.path(e)
        root = t.trusted_root(e)
        try:
            rel = p.relative_to(root)
        except ValueError:
            raise CapsuleError(f"capsule entry lands outside {root}: {p}", EXIT_DAMAGED)
        cur = root
        for part in rel.parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                raise CapsuleError(f"{cur} is a symlink; capsule refuses to write through it. Move it away first.")
        it = {"e": e, "path": p, "kind": e["type"]}
        if e["type"] == "link":
            target = t.rw.text(zf.read(e["arc"]).decode("utf-8", "surrogateescape"))
            resolved = os.path.normpath(target if target.startswith("/") else os.path.join(str(p.parent), target))
            it["target"] = target
            if not any(is_under(resolved, r) for r in session_roots):
                it["action"] = "unsafe"  # a link may only point inside the landed session
                items.append(it)
                continue
        elif e["type"] == "file":
            it["sha256"] = e["sha256"]
            if e["role"] in REWRITE_ROLES and t.rw.rx:
                data = zf.read(e["arc"])
                new = t.rw.file(data, p.name)
                if new != data:
                    if p.name.endswith(".jsonl") and invalid_lines(new) > invalid_lines(data):
                        raise CapsuleError(f"rewriting broke JSON in {e['arc']}; nothing written")
                    it["data"], it["sha256"] = new, sha256_bytes(new)
        items.append(it)

    for it in items:
        if "action" in it:
            continue
        p, e = it["path"], it["e"]
        if it["kind"] == "dir":
            it["action"] = "skip" if p.is_dir() and not p.is_symlink() else ("blocked" if os.path.lexists(p) else "write")
        elif it["kind"] == "link":
            if p.is_symlink() and os.readlink(p) == it["target"]:
                it["action"] = "skip"
            elif p.is_dir() and not p.is_symlink():
                it["action"] = "blocked"
            else:
                it["action"] = "conflict" if os.path.lexists(p) else "write"
        elif p.is_dir() and not p.is_symlink():
            it["action"] = "blocked"
        elif p.is_symlink() or (os.path.lexists(p) and not p.is_file()):
            it["action"] = "keep" if e["role"] in ADD_ONLY_ROLES else "conflict"
        elif p.is_file():
            size = len(it["data"]) if "data" in it else e["size"]
            same = p.stat().st_size == size and sha256_file(p) == it["sha256"]
            it["action"] = "skip" if same else ("keep" if e["role"] in ADD_ONLY_ROLES else "conflict")
        else:
            it["action"] = "write"
    return items


def backup(p: Path, stamp: str) -> str:
    dst = p.with_name(f"{p.name}.capsule-bak-{stamp}")
    os.rename(p, dst)
    return str(dst)


def write_item(zf: zipfile.ZipFile, it: dict, min_mtime: float) -> None:
    p, e = it["path"], it["e"]
    p.parent.mkdir(parents=True, exist_ok=True)
    if it["kind"] == "link":
        os.symlink(it["target"], p)
        return
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".capsule-")
    try:
        with os.fdopen(fd, "wb") as w:
            if "data" in it:
                w.write(it["data"])
            else:
                with zf.open(e["arc"]) as r:
                    shutil.copyfileobj(r, w, CHUNK)
        os.chmod(tmp, (e["mode"] & 0o777) | 0o400)  # no setuid bits from a capsule; always readable by you
        # keep the original time (the /resume picker sorts by it) unless the startup sweep would delete the file
        mtime = e["mtime"] if e["mtime"] >= min_mtime else dt.datetime.now().timestamp()
        os.utime(tmp, (mtime, mtime))
        os.replace(tmp, p)  # works over read-only files too
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def merge_history(zf: zipfile.ZipFile, m: dict, t: Target, dry: bool) -> int:
    lines = []
    for raw in zf.read(m["history"]["arc"]).splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("sessionId") == t.old_sid:  # only this session's own prompts
            lines.append(raw)
    if not lines:
        return 0
    hist = t.cfg / "history.jsonl"
    existing = set(hist.read_bytes().splitlines()) if hist.exists() else set()
    add = []
    for raw in lines:
        new = t.rw.jsonl(raw + b"\n").rstrip(b"\n")
        if new not in existing:
            existing.add(new)
            add.append(new)
    if add and not dry:
        hist.parent.mkdir(parents=True, exist_ok=True)
        lead = b""
        if hist.exists() and hist.stat().st_size:
            with open(hist, "rb") as f:
                f.seek(-1, os.SEEK_END)
                lead = b"" if f.read(1) == b"\n" else b"\n"
        with open(hist, "ab") as f:
            f.write(lead + b"\n".join(add) + b"\n")
    return len(add)


def check_repos(m: dict, t: Target) -> list[dict]:
    out = []
    for r in m.get("repos", []):
        here = t.rw.text(r["path"])
        st = {"role": r["role"], "source_path": r["path"], "path": here, "remote": r["remote"],
              "branch": r["branch"], "head": r["head"]}
        if not os.path.isdir(here):
            st["status"] = "missing"
        elif not git(["rev-parse", "--show-toplevel"], here):
            st["status"] = "not_git"
        else:
            head = git(["rev-parse", "HEAD"], here)
            st["have"] = head
            if r["head"] and head != r["head"]:
                st["status"] = "different_commit"
                st["commit_present"] = git(["cat-file", "-e", r["head"] + "^{commit}"], here) is not None
            else:
                st["status"] = "ok"
            porcelain = git(["status", "--porcelain"], here)
            st["dirty"] = len(porcelain.splitlines()) if porcelain else 0
        out.append(st)
    return out


def repo_advice(st: dict) -> str | None:
    want = f"{st['branch'] or 'detached'} @ {(st['head'] or '?')[:12]}"
    if st["status"] == "missing":
        return f"{st['path']} does not exist: git clone {st['remote'] or '<remote>'} there and check out {want}"
    if st["status"] == "not_git":
        return f"{st['path']} is not a git repo; the session expects {st['remote'] or 'a repo'} at {want}"
    if st["status"] == "different_commit":
        how = "git checkout " + st["head"][:12] if st.get("commit_present") else "git fetch, then check out the commit"
        return f"{st['path']} is at {(st['have'] or '?')[:12]}, the session ended at {want}: {how}"
    return None


def cmd_find(a) -> dict:
    h = Path(home())
    seen, rows = set(), []
    for d in (h / "Downloads", h / "Desktop", Path.cwd()):
        if not d.is_dir():
            continue
        for p in d.glob("capsule-*.zip"):
            if p.resolve() in seen:
                continue
            seen.add(p.resolve())
            st = p.stat()
            row = {"zip": str(p), "modified": iso(st.st_mtime), "bytes": st.st_size}
            try:
                with zipfile.ZipFile(p) as z:
                    mm = json.loads(z.read("capsule/manifest.json"))
                row.update(session=mm["session"]["id"], title=mm["session"].get("title"),
                           packed_at=mm.get("created_at"), source=f"{mm['source']['user']}@{mm['source']['host']}")
            except (OSError, KeyError, ValueError, TypeError, zipfile.BadZipFile):
                row["error"] = "not a readable capsule"
            rows.append(row)
    rows.sort(key=lambda r: r["modified"], reverse=True)
    return {"capsules": rows}


def cmd_inspect(a) -> dict:
    zf, m, path = open_capsule(a.zip)
    src, s = m["source"], m["session"]
    h = home()
    t0 = Rewriter([(src["home"], h, "path")])
    wd = m.get("workdir") or {}
    name_from = wd.get("repo_top") if wd.get("rel") else src["cwd"]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(name_from.rstrip("/")) or "session")
    generated = os.path.join(h, "claude-capsules", f"{base}-{s['id'][:8]}", *(Path(wd["rel"]).parts if wd.get("rel") else ()))
    original = t0.text(src["cwd"])
    in_use = []
    for p in sorted((config_dir() / "projects").glob(f"*/{s['id']}.jsonl")):
        in_use.append({"project_folder": p.parent.name, "cwd": first_cwd(p)})
    files = [e for e in m["files"] if e["type"] == "file"]
    return {
        "capsule": path,
        "packed_at": m.get("created_at"),
        "session": s,
        "source": src,
        "running_when_packed": m.get("running") or [],
        "files": len(files),
        "bytes": sum(e["size"] for e in files),
        "parts": sorted({e["role"] for e in m["files"]}),
        "this_machine": {"home": h, "config_dir": str(config_dir()), "tmp_root": tmp_root()},
        "dir_options": [
            {"kind": "generated", "path": generated, "exists": os.path.exists(generated)},
            {"kind": "original", "path": original, "exists": os.path.exists(original),
             "temporary": looks_temporary(src["cwd"]), "same_as_source": original == src["cwd"]},
        ],
        "workdir": wd,
        "automatic_maps": [
            {"what": "home", "old": src["home"], "new": h},
            {"what": "temp root", "old": src["tmp_root"], "new": tmp_root()},
            {"what": "config dir", "old": src["config_dir"], "new": str(config_dir())},
        ],
        "mappable": [
            {"kind": "repo", "old": r["path"], "default_new": t0.text(r["path"]), "remote": r["remote"],
             "branch": r["branch"], "head": r["head"], "role": r["role"]}
            for r in m.get("repos", []) if r["role"] != "workdir"
        ] + [{"kind": "folder", "old": r, "default_new": t0.text(r)} for r in m.get("external_roots", [])],
        "id_in_use": in_use,
    }


def cmd_land(a) -> dict:
    zf, m, path = open_capsule(a.zip)
    verify_capsule(zf, m)
    t = Target(m, a.dir, a.map, a.new_id)
    if os.path.lexists(t.cwd) and not os.path.isdir(t.cwd):
        raise CapsuleError(f"{t.cwd} exists and is not a folder")

    elsewhere = sorted(str(p) for p in (t.cfg / "projects").glob(f"*/{t.sid}.jsonl") if p.parent.name != t.slug)
    items = plan_land(zf, m, t)
    pick = lambda action: [it for it in items if it["action"] == action]  # noqa: E731
    if pick("blocked"):
        raise CapsuleError("these paths are in the way (a file where a folder belongs, or the reverse); "
                           "move them away first", EXIT_CONFLICT, reason="blocked",
                           paths=[str(it["path"]) for it in pick("blocked")][:30])
    if elsewhere:
        raise CapsuleError(
            "this session ID already exists in another project on this machine; land with --new-id",
            EXIT_CONFLICT, reason="id_in_use", paths=elsewhere,
        )
    if pick("conflict") and not a.force:
        raise CapsuleError(
            f"{len(pick('conflict'))} file(s) already exist with different content; nothing written. "
            "--force replaces them (the old ones are kept as *.capsule-bak-<time>); --new-id lands a separate copy",
            EXIT_CONFLICT, reason="files_differ", paths=[str(it["path"]) for it in pick("conflict")][:30],
        )

    counts = {k: len(pick(k)) for k in ("write", "skip", "conflict", "keep", "unsafe")}
    result = {
        "session_id": t.sid,
        "source_session_id": t.old_sid,
        "dir": t.cwd,
        "project_folder": str(t.cfg / "projects" / t.slug),
        "maps": [{"old": o, "new": n, "kind": k} for o, n, k in t.path_maps if o != n],
        "files": counts,
        "kept_existing": [str(it["path"]) for it in pick("keep")],
    }
    if a.dry_run:
        result.update(dry_run=True, history_lines_to_add=merge_history(zf, m, t, dry=True),
                      repos=check_repos(m, t), resume_command=t.resume_command())
        return result

    os.makedirs(t.cwd, exist_ok=True)
    for d in (Path(t.tmp), Path(t.tmp) / t.slug):  # Claude Code keeps its temp folders private
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backed_up = []
    min_mtime = dt.datetime.now().timestamp() - (cleanup_days() - 1) * 86400
    refreshed = sum(1 for it in items if it["kind"] == "file" and it["action"] in ("write", "conflict")
                    and it["e"]["mtime"] < min_mtime)
    created_dirs = []
    for it in items:
        if it["kind"] == "dir" and it["action"] == "write":
            it["path"].mkdir(parents=True, exist_ok=True)
            created_dirs.append(it)
    for kind in ("file", "link"):  # links last, so no file is ever written through one
        for it in items:
            if it["kind"] == kind and it["action"] in ("write", "conflict"):
                if it["action"] == "conflict":
                    backed_up.append(backup(it["path"], stamp))
                write_item(zf, it, min_mtime)
    for it in reversed(created_dirs):
        os.chmod(it["path"], (it["e"]["mode"] & 0o777) | 0o700)
    result["history_lines_added"] = merge_history(zf, m, t, dry=False)

    mismatched, unresolved = [], []
    for it in items:
        p = it["path"]
        if it["kind"] == "file" and it["action"] != "keep":
            if not p.is_file() or sha256_file(p) != it["sha256"]:
                mismatched.append(str(p))
        elif it["kind"] == "link" and it["action"] != "unsafe":
            if not p.is_symlink() or os.readlink(p) != it["target"]:
                mismatched.append(str(p))
            elif not os.path.exists(p):
                unresolved.append(f"{p} -> {it['target']}")
    if mismatched:
        raise CapsuleError("restored files do not match the capsule", EXIT_DAMAGED, paths=mismatched[:30])

    repos = check_repos(m, t)
    warnings = [w for w in (repo_advice(r) for r in repos) if w]
    wd = m.get("workdir") or {}
    if wd.get("entries") and not any(os.scandir(t.cwd)):
        warnings.append(f"the source working folder had {wd['entries']} entries and {t.cwd} is empty; copy them over")
    if t.repo_map:
        warnings.append(f"the session ran in a subfolder of {t.repo_map[0]}; that repo now maps to {t.repo_map[1]}")
    if pick("unsafe"):
        warnings.append(f"skipped {len(pick('unsafe'))} link(s) that point outside the session: "
                        + ", ".join(f"{it['path']} -> {it['target']}" for it in pick("unsafe")[:3]))
    if unresolved:
        warnings.append(f"{len(unresolved)} link(s) point at files that are not on this machine")
    if result["kept_existing"]:
        warnings.append(f"kept {len(result['kept_existing'])} existing memory, plan or paste file(s) that differ "
                        "from the capsule's copy: " + ", ".join(result["kept_existing"][:5]))
    if backed_up:
        warnings.append(f"--force replaced {len(backed_up)} file(s); the old versions are kept next to them as "
                        f"*.capsule-bak-{stamp}")
    if refreshed:
        warnings.append(f"{refreshed} file(s) were older than cleanupPeriodDays ({cleanup_days()}); their modified "
                        "time was set to now so Claude Code's startup cleanup does not delete them")
    result.update(
        verified=True,
        backed_up=backed_up[:30],
        links_unresolved=unresolved[:20],
        repos=repos,
        warnings=warnings,
        resume_command=t.resume_command(),
    )
    return result


# ---------------------------------------------------------------- list


def first_cwd(path: Path) -> str | None:
    with open(path, "rb") as f:
        for n, raw in enumerate(f):
            if n > 200:
                break
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("cwd"), str):
                return d["cwd"]
    return None


def cmd_list(a) -> dict:
    projects = config_dir() / "projects"
    files = [p for p in projects.glob("*/*.jsonl") if UUID_RE.match(p.stem)]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    titles = first_prompts()
    current = os.environ.get("CLAUDE_CODE_SESSION_ID")
    rows = []
    for p in files[: a.limit]:
        st = p.stat()
        rows.append({
            "id": p.stem,
            "cwd": first_cwd(p),
            "modified": iso(st.st_mtime),
            "bytes": st.st_size,
            "title": tail_title(p, st.st_size) or titles.get(p.stem),
            "running": bool(live_processes(p.stem)),
            "current": p.stem == current,
        })
    return {"sessions": rows, "total": len(files)}


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="capsule", description="Move a Claude Code session between machines.")
    ap.add_argument("--version", action="version", version=f"claude-code-capsule {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list", help="recent sessions, newest first")
    s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("ship", help="pack a session into a capsule zip")
    s.add_argument("session", help="session ID or a unique prefix of it")
    s.add_argument("--out", help="output folder or .zip path (default: ~/Desktop)")
    sub.add_parser("find", help="capsule zips in ~/Downloads, ~/Desktop and the current folder")
    s = sub.add_parser("inspect", help="describe a capsule and the choices for landing it")
    s.add_argument("zip", nargs="?")
    s = sub.add_parser("land", help="restore a capsule on this machine")
    s.add_argument("zip", nargs="?")
    s.add_argument("--dir", required=True, help="working folder the session belongs to on this machine")
    s.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                   help="also move an absolute path from the source machine (repeatable)")
    s.add_argument("--new-id", action="store_true", help="land under a new session ID")
    s.add_argument("--force", action="store_true", help="replace files that differ (old ones kept as *.capsule-bak-*)")
    s.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    a = ap.parse_args(argv)
    cmds = {"list": cmd_list, "ship": cmd_ship, "find": cmd_find, "inspect": cmd_inspect, "land": cmd_land}
    try:
        out, code = cmds[a.cmd](a), EXIT_OK
    except CapsuleError as e:
        out, code = {"error": str(e), **e.extra}, e.code
    except OSError as e:
        out, code = {"error": f"{type(e).__name__}: {e}"}, EXIT_ERR
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
