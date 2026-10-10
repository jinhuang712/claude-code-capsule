#!/usr/bin/env bash
# End-to-end test: build a session on "machine A", ship it, land it on "machine B" under another home,
# another folder and another temp root; then re-land, conflict, --force, --new-id, tamper and standalone runs.
set -euo pipefail
CAP="$(cd "$(dirname "$0")/.." && pwd)/scripts/capsule.py"
T="$(cd "$(mktemp -d)" && pwd -P)"; trap 'chmod -R u+w "$T" 2>/dev/null; rm -rf "$T"' EXIT
fail(){ echo "FAIL: $*"; exit 1; }
j(){ python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1],{'d':d}))" "$1"; }
unset CLAUDE_CONFIG_DIR CLAUDE_CODE_PROJECT_DIR_NAME CLAUDE_CODE_SESSION_ID
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

HA="$T/a/home"; TA="$T/a/tmp"; HB="$T/b/home"; TB="$T/b/tmp"
CWD="$HA/work/proj"; OTHER="$HA/other-repo"
SID=1b2c3d4e-0000-4000-8000-00000000c0de; SID2=2c3d4e5f-0000-4000-8000-0000000000aa
SID3=3d4e5f6a-0000-4000-8000-0000000000bb
mkdir -p "$CWD/sub" "$OTHER" "$TA" "$HB" "$TB" "$T/victim"; echo ORIGINAL > "$T/victim/target.txt"

# --- repos: the working folder (pushed, then one unpushed commit and one dirty file) and a referenced repo
git init -q --bare "$T/remote.git"
(cd "$CWD" && git init -q -b main && echo one > a.txt && git add -A && git commit -qm one \
  && git remote add origin "$T/remote.git" && git push -qu origin main \
  && echo two >> a.txt && git commit -qam two && echo dirty > b.txt)
(cd "$OTHER" && git init -q -b main && echo x > x.txt && git add -A && git commit -qm x)

# --- the session on machine A
HA="$HA" TA="$TA" CWD="$CWD" OTHER="$OTHER" SID="$SID" SID2="$SID2" SID3="$SID3" T="$T" python3 - <<'EOF'
import json, os, re, stat
E = os.environ; ha, cwd, sid, sid2, sid3 = E["HA"], E["CWD"], E["SID"], E["SID2"], E["SID3"]
tmp = os.path.join(os.path.realpath(E["TA"]), f"claude-{os.getuid()}")
slug = lambda p: re.sub(r"[^a-zA-Z0-9]", "-", p)
cfg = f"{ha}/.claude"; proj = f"{cfg}/projects/{slug(cwd)}"; tdir = f"{tmp}/{slug(cwd)}/{sid}"
def w(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)
    if mode: os.chmod(path, mode)
def jl(*objs): return "".join(json.dumps(o, separators=(",", ":")) + "\n" for o in objs)
base = {"cwd": cwd, "sessionId": sid, "version": "2.1.296"}
w(f"{proj}/{sid}.jsonl", jl(
    {**base, "type": "user", "timestamp": "2026-10-01T00:00:00Z", "message": {"role": "user", "content": f"edit {cwd}/a.txt"}},
    {**base, "type": "assistant", "timestamp": "2026-10-01T00:00:01Z", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": f"look at {cwd}/a.txt and {ha}/notes", "signature": "SIG/abc+="},
        {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": f"{cwd}/a.txt"}},
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": f"cat {E['OTHER']}/x.txt {cwd}-other/y {E['T']}/a/home2/z /backup{ha}/w"}},
        {"type": "tool_use", "id": "t3", "name": "Tool", "input": {"schema": {"type": {"enum": ["a"]}}, "path": f"{ha}/Desktop/n.txt"}}]}},
    {**base, "type": "user", "timestamp": "2026-10-01T00:00:02Z", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": f"saved {cfg}/projects/{slug(cwd)}/{sid}/tool-results/r1.txt and {tdir}/scratchpad/n.md; plan {cfg}/plans/p.md"}]}},
    {"type": "file-history-snapshot", "messageId": "m1", "snapshot": {"messageId": "m1", "trackedFileBackups": {
        f"{cwd}/a.txt": {"backupFileName": "abc@v1", "version": 1, "realParentDir": cwd}}}},
    {"type": "ai-title", "aiTitle": "Test session", "sessionId": sid},
))
w(f"{proj}/{sid}/subagents/agent-a1.jsonl", jl({**base, "type": "user", "isSidechain": True, "message": {"role": "user", "content": f"read {cwd}/a.txt"}}))
w(f"{proj}/{sid}/subagents/agent-a1.meta.json", json.dumps({"agentType": "Explore", "cwd": cwd}))
w(f"{proj}/{sid}/tool-results/r1.txt", f"listing of {cwd}\n")
w(f"{proj}/memory/MEMORY.md", f"- project lives in {cwd}\n")
w(f"{cfg}/file-history/{sid}/abc@v1", f"original content mentioning {cwd}\n")
os.makedirs(f"{cfg}/session-env/{sid}", exist_ok=True)
w(f"{cfg}/plans/p.md", f"# plan for {cwd}\n")
w(f"{cfg}/paste-cache/0123456789abcdef.txt", "a long paste\n")
w(f"{tdir}/scratchpad/n.md", f"scratch note about {cwd}\n")
w(f"{tdir}/scratchpad/ro.bin", "read-only\n", 0o444)
os.makedirs(f"{tdir}/tasks", exist_ok=True)
os.symlink(f"{proj}/{sid}/subagents/agent-a1.jsonl", f"{tdir}/tasks/a1.output")
for d in (tmp, f"{tmp}/{slug(cwd)}", tdir): os.chmod(d, 0o700)
w(f"{cfg}/history.jsonl", jl(
    {"display": "edit a.txt", "pastedContents": {"1": {"id": 1, "type": "text", "contentHash": "0123456789abcdef"}}, "timestamp": 1, "project": cwd, "sessionId": sid},
    {"display": "other session", "pastedContents": {}, "timestamp": 2, "project": "/x", "sessionId": "ffffffff-0000-4000-8000-000000000000"}))
# a second session started in the home folder itself
w(f"{cfg}/projects/{slug(ha)}/{sid2}.jsonl", jl({"type": "user", "cwd": ha, "sessionId": sid2, "message": {"role": "user", "content": f"look at {ha} and {ha}/dev/x"}}))
old = 1_600_000_000  # 2020: older than any cleanupPeriodDays
os.utime(f"{cfg}/projects/{slug(ha)}/{sid2}.jsonl", (old, old))
# a third session started in a subfolder of the repo, with a bad UTF-8 byte and a truncated last line
sub = f"{cwd}/sub"; p3 = f"{cfg}/projects/{slug(sub)}"
os.makedirs(f"{p3}/{sid3}/subagents", exist_ok=True)
with open(f"{p3}/{sid3}.jsonl", "wb") as f:
    f.write(jl({"type": "user", "cwd": sub, "sessionId": sid3, "message": {"role": "user", "content": f"edit {sub}/f.py and {cwd}/lib/g.py. done in {sub}."}}).encode())
    f.write(b'{"type":"user","cwd":"' + sub.encode() + b'","sessionId":"' + sid3.encode() + b'","note":"bad \xff byte"}\n')
    f.write(b'{"type":"user","cwd":"' + sub.encode() + b'","message":{"role":"user","content":"cut')
w(f"{p3}/{sid3}/subagents/agent-b.meta.json", json.dumps({"d": f"files:\n{sub}/x"}))
EOF

A(){ HOME="$HA" CLAUDE_CODE_TMPDIR="$TA" python3 "$CAP" "$@"; }
B(){ HOME="$HB" CLAUDE_CODE_TMPDIR="$TB" python3 "$CAP" "$@"; }

# --- list + ship on A
[ "$(A list | j "[s['title'] for s in d['sessions'] if s['id']=='$SID']")" = "['Test session']" ] || fail "list title"
OUT=$(A ship "${SID:0:8}" --out "$T/out") || fail "ship exit $?: $OUT"
ZIP=$(echo "$OUT" | j "d['zip']"); [ -f "$ZIP" ] || fail "zip missing"
[ "$(echo "$OUT" | j "sorted(d['parts'])")" = "['config_ref', 'file_history', 'memory', 'session_dir', 'session_env', 'tmp', 'transcript']" ] || fail "parts: $(echo "$OUT" | j "d['parts']")"
echo "$OUT" | j "d['warnings']" | grep -q "uncommitted" || fail "dirty warning"
echo "$OUT" | j "d['warnings']" | grep -q "unpushed" || fail "unpushed warning"
echo "$OUT" | j "d['warnings']" | grep -q "no upstream" || fail "no-upstream warning"
[ "$(echo "$OUT" | j "sorted(r['role'] for r in d['repos'])")" = "['referenced', 'workdir']" ] || fail "repos"
[ "$(echo "$OUT" | j "d['history_lines']")" = 1 ] || fail "history lines"
echo "✓ ship (7 parts, repo warnings, 1 history line)"

# --- inspect on B
INS=$(B inspect "$ZIP")
[ "$(echo "$INS" | j "d['dir_options'][0]['path']")" = "$HB/claude-capsules/proj-${SID:0:8}" ] || fail "generated dir"
[ "$(echo "$INS" | j "[m['old'] for m in d['mappable'] if m['kind']=='repo']")" = "['$OTHER']" ] || fail "mappable repo"
echo "✓ inspect"

# --- land on B: new home, new folder, other repo mapped elsewhere
NEW="$HB/sessions/proj"
LAND=$(B land "$ZIP" --dir "$NEW" --map "$OTHER=$HB/code/other") || fail "land exit $?: $LAND"
[ "$(echo "$LAND" | j "d['verified']")" = True ] || fail "not verified"
[ "$(echo "$LAND" | j "d['resume_command']")" = "cd $NEW && claude --resume $SID" ] || fail "resume command"
[ "$(echo "$LAND" | j "sorted((r['role'], r['status']) for r in d['repos'])")" = "[('referenced', 'missing'), ('workdir', 'not_git')]" ] || fail "repo checks"
HA="$HA" HB="$HB" TB="$TB" NEW="$NEW" SID="$SID" T="$T" python3 - <<'EOF'
import json, os, re, stat
E = os.environ; ha, hb, new, sid = E["HA"], E["HB"], E["NEW"], E["SID"]
slug = re.sub(r"[^a-zA-Z0-9]", "-", new); cfg = f"{hb}/.claude"; proj = f"{cfg}/projects/{slug}"
tmp = os.path.join(os.path.realpath(E["TB"]), f"claude-{os.getuid()}"); tdir = f"{tmp}/{slug}/{sid}"
def check(cond, msg):
    if not cond: raise SystemExit("FAIL: " + msg)
L = [json.loads(x) for x in open(f"{proj}/{sid}.jsonl")]
check(all(d["cwd"] == new for d in L if "cwd" in d), "cwd fields")
c = L[1]["message"]["content"]
check(c[0] == {"type": "thinking", "thinking": f"look at {ha}/work/proj/a.txt and {ha}/notes", "signature": "SIG/abc+="}, "thinking block changed")
check(c[1]["input"]["file_path"] == f"{new}/a.txt", "Edit path")
cmd = c[2]["input"]["command"]
check(f"{hb}/code/other/x.txt" in cmd, "--map path")
check(f"{hb}/work/proj-other/y" in cmd, "sibling folder must follow home, not the working folder")
check(f"{E['T']}/a/home2/z" in cmd, "home2 must not match home")
check(f"/backup{ha}/w" in cmd, "path inside another path must not match")
check(c[3]["input"]["schema"] == {"type": {"enum": ["a"]}} and c[3]["input"]["path"] == f"{hb}/Desktop/n.txt", "schema-typed input")
r = L[2]["message"]["content"][0]["content"]
check(f"{cfg}/projects/{slug}/{sid}/tool-results/r1.txt" in r and f"{tdir}/scratchpad/n.md" in r and f"{cfg}/plans/p.md" in r, "tool result paths: " + r)
check(list(L[3]["snapshot"]["trackedFileBackups"]) == [f"{new}/a.txt"], "file-history key")
check(L[3]["snapshot"]["trackedFileBackups"][f"{new}/a.txt"]["realParentDir"] == new, "realParentDir")
check(ha not in open(f"{proj}/{sid}/subagents/agent-a1.jsonl").read(), "subagent transcript")
check(json.load(open(f"{proj}/{sid}/subagents/agent-a1.meta.json"))["cwd"] == new, "subagent meta")
check(open(f"{proj}/memory/MEMORY.md").read() == f"- project lives in {new}\n", "memory")
check(ha in open(f"{cfg}/file-history/{sid}/abc@v1").read(), "file-history snapshot must stay byte for byte")
check(ha in open(f"{tdir}/scratchpad/n.md").read(), "scratchpad must stay byte for byte")
check(stat.S_IMODE(os.stat(f"{tdir}/scratchpad/ro.bin").st_mode) == 0o444, "read-only mode")
link = f"{tdir}/tasks/a1.output"
check(os.readlink(link) == f"{proj}/{sid}/subagents/agent-a1.jsonl" and os.path.exists(link), "symlink")
for d in (tmp, f"{tmp}/{slug}", tdir):
    check(stat.S_IMODE(os.stat(d).st_mode) == 0o700, "temp mode " + d)
check(os.path.isdir(f"{cfg}/session-env/{sid}"), "empty session-env folder")
check(open(f"{cfg}/plans/p.md").read() == f"# plan for {ha}/work/proj\n", "plan file must stay byte for byte")
check(os.path.isfile(f"{cfg}/paste-cache/0123456789abcdef.txt"), "paste-cache")
H = [json.loads(x) for x in open(f"{cfg}/history.jsonl")]
check(len(H) == 1 and H[0]["project"] == new and H[0]["sessionId"] == sid, "history")
check(abs(os.stat(f"{proj}/{sid}.jsonl").st_mtime - os.stat(f"{ha}/.claude/projects/{re.sub(r'[^a-zA-Z0-9]', '-', ha + '/work/proj')}/{sid}.jsonl").st_mtime) < 1e-3, "mtime kept")
EOF
echo "✓ land (paths remapped, thinking block untouched, content files byte for byte, links, modes, history)"

# --- land again: nothing to do
[ "$(B land "$ZIP" --dir "$NEW" --map "$OTHER=$HB/code/other" | j "(d['files']['write'], d['history_lines_added'])")" = "(0, 0)" ] || fail "re-land not idempotent"
echo "✓ re-land is a no-op"

# --- a landed file changed: stop, then --force
P="$HB/.claude/projects/$(echo "$NEW" | sed 's/[^a-zA-Z0-9]/-/g')/$SID.jsonl"
echo '{"type":"user","note":"local edit"}' >> "$P"
set +e; R=$(B land "$ZIP" --dir "$NEW" --map "$OTHER=$HB/code/other"); rc=$?; set -e
[ $rc = 3 ] && [ "$(echo "$R" | j "d['reason']")" = files_differ ] || fail "conflict not detected (rc=$rc)"
[ "$(B land "$ZIP" --dir "$NEW" --map "$OTHER=$HB/code/other" --force | j "d['files']['conflict']")" = 1 ] || fail "--force"
grep -q "local edit" "$P" && fail "--force did not restore the transcript"
grep -q "local edit" "$P".capsule-bak-* || fail "--force did not keep the replaced file"
echo "✓ conflict stops, --force replaces and keeps a backup"

# --- same ID into another folder: refuse, then --new-id
set +e; R=$(B land "$ZIP" --dir "$HB/elsewhere"); rc=$?; set -e
[ $rc = 3 ] && [ "$(echo "$R" | j "d['reason']")" = id_in_use ] || fail "id_in_use not detected (rc=$rc)"
R=$(B land "$ZIP" --dir "$HB/elsewhere" --new-id) || fail "--new-id exit $?"
NID=$(echo "$R" | j "d['session_id']"); [ "$NID" != "$SID" ] || fail "new id"
P2="$HB/.claude/projects/$(echo "$HB/elsewhere" | sed 's/[^a-zA-Z0-9]/-/g')/$NID.jsonl"
grep -q "$SID" "$P2" && fail "old session ID left in the new copy"
[ "$(B land "$ZIP" --dir "$HB/elsewhere" --new-id --dry-run | j "d['dry_run']")" = True ] || fail "dry run"
echo "✓ id in use is refused, --new-id lands a separate copy"

# --- a damaged capsule is refused
python3 - "$ZIP" "$T/bad.zip" <<'EOF'
import sys, zipfile
src, dst = sys.argv[1:]
with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst, "w") as b:
    for i in a.infolist():
        data = a.read(i)
        b.writestr(i, data + b"x" if i.filename.endswith("r1.txt") else data)
EOF
set +e; B land "$T/bad.zip" --dir "$HB/bad" >/dev/null; rc=$?; set -e
[ $rc = 4 ] || fail "damaged capsule exit $rc"
echo "✓ damaged capsule is refused"

# --- the zip runs by itself, without the plugin
HC="$T/c/home"; mkdir -p "$HC"
R=$(HOME="$HC" CLAUDE_CODE_TMPDIR="$T/c/tmp" python3 "$ZIP" land --dir "$HC/p") || fail "standalone exit $?"
[ "$(echo "$R" | j "d['verified']")" = True ] || fail "standalone verify"
echo "✓ python3 <zip> land works without the plugin"

# --- a session started in the home folder: only that folder moves, the rest follows the home
Z2=$(A ship "$SID2" --out "$T/out2" | j "d['zip']")
R=$(B land "$Z2" --dir "$HB/claude-capsules/home") || fail "home-session land"
echo "$R" | j "d['warnings']" | grep -q "cleanupPeriodDays" || fail "old-mtime warning"
L=$(cat "$HB/.claude/projects/$(echo "$HB/claude-capsules/home" | sed 's/[^a-zA-Z0-9]/-/g')/$SID2.jsonl")
echo "$L" | grep -q "look at $HB/claude-capsules/home and $HB/dev/x" || fail "home-session mapping: $L"
echo "✓ home-folder session maps exactly, old files survive the startup cleanup"

# --- a session in a repo subfolder keeps the repo layout; bad bytes, truncated lines and escaped \n still map
Z3=$(A ship "$SID3" --out "$T/out3" | j "d['zip']")
R=$(B land "$Z3" --dir "$HB/r/sub") || fail "subfolder land: $R"
echo "$R" | j "d['warnings']" | grep -q "subfolder of $CWD" || fail "subfolder warning"
[ "$(echo "$R" | j "[r['path'] for r in d['repos'] if r['role']=='workdir']")" = "['$HB/r']" ] || fail "repo checked at the wrong place"
HB="$HB" SID3="$SID3" python3 - <<'EOF'
import json, os, re
hb, sid3 = os.environ["HB"], os.environ["SID3"]; new = f"{hb}/r/sub"
raw = open(f"{hb}/.claude/projects/{re.sub(r'[^a-zA-Z0-9]', '-', new)}/{sid3}.jsonl", "rb").read().split(b"\n")
first = json.loads(raw[0])
assert first["message"]["content"] == f"edit {new}/f.py and {hb}/r/lib/g.py. done in {new}.", first["message"]["content"]
assert json.loads(raw[1].decode("utf-8", "surrogateescape"))["cwd"] == new and b"\xff" in raw[1], "bad-byte line"
assert new.encode() in raw[2] and raw[2].endswith(b"cut"), "truncated line"
meta = json.load(open(f"{hb}/.claude/projects/{re.sub(r'[^a-zA-Z0-9]', '-', new)}/{sid3}/subagents/agent-b.meta.json"))
assert meta == {"d": f"files:\n{new}/x"}, meta
EOF
echo "✓ repo subfolder keeps its layout; bad UTF-8, truncated line, escaped newline handled"

# --- hostile capsules: nothing is ever written outside the session
forge(){ python3 - "$ZIP" "$T/$1.zip" "$1" "$T/victim" <<'EOF'
import hashlib, json, sys, zipfile
src, dst, mode, victim = sys.argv[1:]
a = zipfile.ZipFile(src); m = json.loads(a.read("capsule/manifest.json")); add = {}
def ent(role, rel, typ, data=b"", target=None):
    arc = f"capsule/{role}/{rel}" if rel else f"capsule/{role}"
    e = {"role": role, "rel": rel, "arc": arc, "mode": 0o4777, "mtime": 1.7e9, "type": typ}
    if typ == "link":
        data, e["target"] = target.encode(), target
    if typ == "file":
        e["size"] = len(data)
    e["sha256"] = hashlib.sha256(data).hexdigest(); add[arc] = data
    return e
if mode == "rootlink":
    m["files"] = [e for e in m["files"] if e["role"] != "memory"] + [ent("memory", "", "link", target=victim), ent("memory", "target.txt", "file", b"PWNED")]
elif mode == "underlink":
    m["files"] += [ent("tmp", "x", "link", target=victim), ent("tmp", "x/target.txt", "file", b"PWNED")]
elif mode == "outlink":
    m["files"] += [ent("tmp", "tasks/evil", "link", target=victim + "/target.txt")]
elif mode == "traversal":
    m["files"] += [ent("session_dir", "../../escape.txt", "file", b"PWNED")]
with zipfile.ZipFile(dst, "w") as b:
    for i in a.infolist():
        if i.filename != "capsule/manifest.json":
            b.writestr(i, a.read(i))
    for arc, data in add.items():
        b.writestr(arc, data)
    b.writestr("capsule/manifest.json", json.dumps(m))
EOF
}
D(){ HOME="$T/d/$1" CLAUDE_CODE_TMPDIR="$T/d/$1-tmp" python3 "$CAP" land "$T/$1.zip" --dir "$T/d/$1/p"; }
for mode in rootlink underlink traversal; do
  forge $mode; set +e; D $mode >/dev/null; rc=$?; set -e
  [ $rc = 4 ] || fail "hostile capsule '$mode' exit $rc"
done
forge outlink; R=$(D outlink) || fail "outlink land"
echo "$R" | j "d['warnings']" | grep -q "point outside the session" || fail "outside link not reported"
[ -e "$T/d/outlink-tmp/claude-$(id -u)" ] && find "$T/d/outlink-tmp" -name evil | grep -q . && fail "outside link was created"
mkdir -p "$T/d/pre/.claude/projects" "$T/victim2"
ln -s "$T/victim2" "$T/d/pre/.claude/projects/$(echo "$T/d/pre/p" | sed 's/[^a-zA-Z0-9]/-/g')"
set +e; R=$(HOME="$T/d/pre" CLAUDE_CODE_TMPDIR="$T/d/pre-tmp" python3 "$CAP" land "$ZIP" --dir "$T/d/pre/p"); rc=$?; set -e
[ $rc = 1 ] && echo "$R" | grep -q "symlink" || fail "pre-existing symlink not refused (rc=$rc)"
[ -z "$(ls -A "$T/victim2")" ] || fail "wrote through a pre-existing symlink"
[ "$(cat "$T/victim/target.txt")" = ORIGINAL ] || fail "victim file was overwritten"
echo "✓ hostile capsules refused: root link, file under a link, path traversal, outside link, pre-existing symlink"

# --- find, and ship never overwrites a zip
mkdir -p "$HB/Downloads"; cp "$ZIP" "$HB/Downloads/"
[ "$(B find | j "[c.get('session') for c in d['capsules']]")" = "['$SID']" ] || fail "find"
set +e; A ship "$SID" --out "$ZIP" >/dev/null; rc=$?; set -e
[ $rc = 1 ] || fail "ship overwrote an existing zip"
echo "✓ find lists capsules; ship refuses to overwrite"

echo "ALL PASS"
