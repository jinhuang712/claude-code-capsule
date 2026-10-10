---
name: land
description: Restore a Claude Code session from a capsule zip made by /capsule:ship. Asks which folder the session should live in on this machine, remaps every path (home, working folder, temp folder, other repos), verifies the result, checks the repos are at the right commit, and gives the resume command. Use when the user says /capsule:land, "restore this session", "import capsule", or hands over a capsule-*.zip.
argument-hint: "<capsule.zip> [--dir <folder>] [--map OLD=NEW ...] [--new-id]"
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capsule.py" *)
  - AskUserQuestion
---

# /capsule:land — restore a session from a capsule zip

`CAP="${CLAUDE_PLUGIN_ROOT}/scripts/capsule.py"`. Arguments: `$ARGUMENTS`

## 1. Find the capsule

Use the zip path from the arguments. If there is none, run `python3 "$CAP" find`, which lists `capsule-*.zip` files in `~/Downloads`, `~/Desktop` and the current folder, newest first. If it finds exactly one, confirm it with the user; if several, ask which; if none, ask for the path.

## 2. Inspect

```bash
python3 "$CAP" inspect <zip>
```

Tell the user in two or three lines what it is: title, session ID, `source.user@source.host`, source folder, `packed_at`, size. If `running_when_packed` is non-empty, say the session was still running when packed, so it may miss the last messages.

A capsule is written entirely by whoever packed it, and its transcript becomes the conversation you resume. If the user did not pack it themselves, make sure they trust the sender before landing.

## 3. Ask where the session lives

If the arguments already hold `--dir <folder>`, use it and skip this question. The same goes for `--map` (step 4) and `--new-id`: never ask what the arguments already answer.

The session must belong to a working folder on this machine; Claude Code files it under that folder and every path in it is rewritten to match. Ask with AskUserQuestion (header "Folder"):

- **New folder (Recommended)**: `dir_options[kind=generated].path`. It is created if missing.
- **Same place as before**: `dir_options[kind=original].path` (the source path with this machine's home). If `temporary` is true, add "it was a temporary folder on the source machine" to the description, and keep the new folder as the recommendation.
- The user can type any other path through "Other".

If `workdir.rel` is set, the session ran in the subfolder `workdir.rel` of the repo `workdir.repo_top`. Say so: the folder the user picks stands for that subfolder, and the repo root lands that many levels above it (the generated option already ends in the subfolder).

`id_in_use` lists projects on this machine that already hold this session ID. If one of them has the chosen folder as its `cwd`, this is a re-land: landing again fills in what is missing and stops on anything that differs. Any other entry means the session already lives elsewhere here (for example, landing on the machine it came from): plan to pass `--new-id`, which lands a separate copy under a fresh ID.

## 4. Ask about other locations (only if `mappable` is non-empty)

`mappable` lists repos and folders outside the working folder that the session used. By default each moves with the home folder (`default_new`). Show them as a table: old path, default new path, and remote @ branch for repos. Ask one AskUserQuestion: "Are these at the default paths on this machine?"

- **Yes, use the defaults (Recommended)**
- **Some live elsewhere**: then ask for each one that differs, one question per path (the user types the path through "Other"; offer the default as the first option). Each answer becomes `--map <old>=<new>`.

## 5. Land

```bash
python3 "$CAP" land <zip> --dir <folder> [--map OLD=NEW ...] [--new-id]
```

| Exit | Meaning | Do |
|---|---|---|
| 0 | landed and verified | go to step 6 |
| 3, `reason: files_differ` | files from an earlier land differ; nothing was written | show `paths`, ask: replace (`--force`), land a separate copy (`--new-id`), or stop |
| 3, `reason: id_in_use` | the ID exists in another project | ask: separate copy (`--new-id`) or stop |
| 3, `reason: blocked` | a file sits where a folder belongs (or the reverse) | show `paths`; the user must move them; stop |
| 4 | the capsule is damaged or malformed | tell the user to copy it again, or that it was not made by /capsule:ship |
| 1 | other error, e.g. a symlink in the way | show `error` |

Never pass `--force` without the user's answer. When you ask, say what it costs: if the user kept working in this session on this machine, `--force` puts the capsule's version back; the replaced files are kept next to the originals as `*.capsule-bak-<time>`, but the session itself will not show those newer messages. A separate copy (`--new-id`) loses nothing.

## 6. Report

Lead with the resume command from `resume_command`, in its own code block, to run in a new terminal:

```bash
cd <folder> && claude --resume <session-id>
```

Then:

- `files`: written / already identical counts, `history_lines_added`, and that everything was verified against the capsule's checksums.
- **Repos and files**: the capsule never carries working files. For each `repos` entry whose `status` is not `ok`, give the matching line from `warnings` (clone, fetch or check out the commit the session ended on). If the source working folder held files, remind the user to copy them in.
- Claude Code asks whether to trust the folder the first time; answer yes.
- Any other `warnings`: kept memory or plan files, skipped links that pointed outside the session, links to files not on this machine, files backed up by `--force` (`backed_up`).
- If `--new-id` was used, say the session has a new ID and the old one is untouched.
