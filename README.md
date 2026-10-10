# capsule — move a Claude Code session to another machine

Two skills and one script:

| | what it does |
|---|---|
| `/capsule:ship [session-id]` | Packs one session into a single `capsule-<id>-<time>.zip`: transcript, subagent transcripts, large tool outputs, `/rewind` checkpoints, scratchpad, prompt history. Warns about everything that is *not* inside (repos with uncommitted or unpushed work). |
| `/capsule:land <zip>` | Asks which folder the session should live in on this machine, remaps every path to match, verifies every file against its checksum, checks that the repos the session used are at the right commit, and gives you `cd <folder> && claude --resume <id>`. |

Why not copy the `.jsonl`? A session is spread over five places (`~/.claude/projects`, `file-history`, `session-env`, the temp scratchpad and `history.jsonl`) and is full of absolute paths. A bare copy loses subagents, tool outputs, checkpoints and scratchpad files, and stops working as soon as the other machine has a different username or folder layout.

## What a capsule holds

| Part | From (machine A) | Lands at (machine B) | Paths inside rewritten |
|---|---|---|---|
| Transcript | `~/.claude/projects/<project>/<id>.jsonl` | `~/.claude/projects/<new project>/<id>.jsonl` | yes |
| Subagents, tool outputs | `~/.claude/projects/<project>/<id>/` | same, under the new project | yes |
| Project memory | `~/.claude/projects/<project>/memory/` | same, under the new project; existing files are kept | yes |
| Checkpoints | `~/.claude/file-history/<id>/` | same | no, byte for byte |
| Session env, task lists, uploads, mods | `~/.claude/{session-env,tasks,uploads,image-cache,dev-mods}/<id>/` | same | session env and tasks only |
| Scratchpad, task outputs, images | `<tmp>/claude-<uid>/<project>/<id>/` | this machine's temp root | no, byte for byte; links are retargeted |
| Plan files, large pastes it refers to | `~/.claude/{plans,paste-cache}/…` | same; existing files are kept | no, byte for byte |
| Prompt history | its lines in `~/.claude/history.jsonl` | appended when missing | yes |

The zip also carries `capsule.py` as `__main__.py` and a `README.txt`, so it can restore itself.

## How paths are remapped

`land` builds one table and applies it in a single pass, longest match first, so nothing is rewritten twice:

| From machine A | To machine B |
|---|---|
| the session's working folder | the folder you chose |
| temp root, e.g. `/private/tmp/claude-501` | this machine's (`CLAUDE_CODE_TMPDIR`, else `/tmp` on macOS, `$TMPDIR` on Linux) + `/claude-<uid>` |
| config dir `~/.claude` | this machine's (`CLAUDE_CONFIG_DIR` honoured) |
| any `--map OLD=NEW` you give, e.g. a repo that lives elsewhere | `NEW` |
| home folder | this machine's home |
| project folder name and, with `--new-id`, the session ID | the new ones |

- Paths match only at path boundaries: `/Users/a` never touches `/Users/ab`, `/Users/a.txt` or `/backup/Users/a`, while a sentence-ending `/Users/a.` still matches. macOS `/tmp` ↔ `/private/tmp` spellings are both caught.
- A session started in the home folder, `~/.claude` or another folder that holds the home, config or temp root moves only that folder; paths below it follow those maps.
- A session started in a subfolder of a repo keeps the repo's layout: the folder you choose stands for that subfolder, and the repo root moves to the matching parent.
- `.jsonl` and `.json` files are parsed, so escaped newlines, truncated last lines and stray invalid UTF-8 bytes don't stop the rewrite.
- **Thinking blocks are never touched.** The API signs them; changing a single character makes the resumed conversation fail. `signature` and `encrypted_content` values and base64 image data are frozen too.
- The project folder name follows Claude Code's own rule, including the cut at 200 characters plus a hash for long paths, so `claude --resume` finds the session from the folder.
- File times are kept, so the `/resume` picker order stays the same, except for files older than `cleanupPeriodDays` (default 30): those get the current time, or Claude Code's startup cleanup would delete them.

## What is not inside

**Your working files.** The capsule never carries the working folder or any repo the session touched. `ship` records each repo's remote, branch, commit, uncommitted and unpushed changes and tells you to push first. `land` checks the mapped location and tells you to clone, fetch or check out the commit the session ended on. A non-git working folder with files must be copied by hand.

Also not inside: plugins, MCP servers, settings and your login. Install the same plugins on the other machine or the restored session will miss their tools.

## Safety

- Every file is checked against the SHA-256 recorded at ship time, before and after writing. A damaged or malformed capsule exits 4 and writes nothing.
- Every check runs before the first write. A file that exists with different content stops the land (exit 3); `--force` replaces only those files and keeps each old one next to it as `*.capsule-bak-<time>`. Memory, plan and paste files are shared with other sessions and are never replaced.
- If the session ID already exists in another project here (for example, landing on the machine it came from), `land` refuses: Claude Code cannot resume an ID that lives in two projects. `--new-id` lands a separate copy.
- A capsule is untrusted input. Paths from the zip are validated (no `..`, no absolute paths, nothing outside its part's folder). Symlinks are allowed only inside the scratchpad tree, are created after every file, and may only point inside the landed session; others are skipped with a warning. Nothing is written through an existing symlink (exit 1). Mode bits are masked. Writes go to a temp file and are renamed into place.
- Landing the same capsule again is a no-op.
- The capsule is plain text and holds every tool output of the session. Send it privately, and only land capsules you packed yourself or got from someone you trust: the transcript becomes the conversation you resume.

## Install

Requires Claude Code and Python ≥ 3.9 (stdlib only).

```bash
# 1. register this repo as a plugin marketplace (one-time)
claude plugin marketplace add jinhuang712/claude-code-capsule

# 2. install the plugin (user scope by default)
claude plugin install capsule@claude-code-capsule
```

Start a new Claude Code session; `/capsule:ship` and `/capsule:land` are now available. Install it on both machines, or use the zip on its own (below).

```bash
claude plugin list                                  # verify: capsule@claude-code-capsule · enabled
claude plugin update capsule@claude-code-capsule    # pull a newer version
claude plugin uninstall capsule@claude-code-capsule
```

Developing locally: `claude plugin marketplace add /path/to/clone` instead of the GitHub slug, then `claude plugin update` after every source change (installs are copies, not symlinks).

### Without the plugin

```bash
python3 capsule-1b2c3d4e-20261011-0130.zip inspect                  # what is inside, folder options
python3 capsule-1b2c3d4e-20261011-0130.zip land --dir ~/work/proj   # restore
cd ~/work/proj && claude --resume <session-id>
```

## Script

`scripts/capsule.py` prints one JSON object per command.

```text
capsule.py list    [--limit N]                      recent sessions, newest first
capsule.py ship    <id or prefix> [--out PATH]      write the capsule (default ~/Desktop; never overwrites)
capsule.py find                                     capsule zips in ~/Downloads, ~/Desktop and the current folder
capsule.py inspect [ZIP]                            describe a capsule and the landing choices
capsule.py land    [ZIP] --dir FOLDER [--map OLD=NEW]... [--new-id] [--force] [--dry-run]
```

Exit codes: `0` ok · `1` error (for example a symlink in the way) · `3` conflict, nothing written (`reason`: `files_differ`, `id_in_use`, `blocked`) · `4` capsule damaged, malformed or unsafe.

Tests: `bash tests/e2e.sh` builds a session on a fake machine A and lands it on a fake machine B with a different home, folder and temp root.

## Limitations

- macOS and Linux. Tested on macOS with Claude Code 2.1.296; the Linux temp-root rule follows the docs and is untested. No Windows.
- The transcript format is internal to Claude Code and changes between versions. Land on the same or a newer Claude Code.
- On the other machine the scratchpad also lives under the temp root, which macOS clears on reboot. Keep the zip until you no longer need those files; landing again restores them.
- Continue the session on one machine only. Writing to both copies splits the history.

## License

MIT. See [CHANGELOG.md](CHANGELOG.md) for release notes.
