---
name: ship
description: Pack one Claude Code session - transcript, subagent transcripts, large tool outputs, /rewind checkpoints, scratchpad, prompt history - into a single capsule zip that /capsule:land restores on another machine. Use when the user says /capsule:ship, "pack this session", "export session <id>", or wants to move a session to another laptop.
argument-hint: "[session-id or prefix] [--out <folder or .zip>]"
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capsule.py" *)
  - AskUserQuestion
---

# /capsule:ship — pack a session into a capsule zip

`CAP="${CLAUDE_PLUGIN_ROOT}/scripts/capsule.py"`. Arguments: `$ARGUMENTS`

## 1. Pick the session

If the arguments hold a session ID or a prefix of one (8+ hex characters is enough), use it and go to step 2.

Otherwise list recent sessions:

```bash
python3 "$CAP" list --limit 8
```

Ask with AskUserQuestion which one to pack. Offer up to 4 sessions, newest first. Label: the title, shortened to a few words. Description: ID prefix, working folder, modified time, plus `running` or `this session` when set. The user can type any other ID through "Other".

## 2. Ship

```bash
python3 "$CAP" ship <id>            # add --out <folder or .zip> if the user named a place; default ~/Desktop
```

Exit 0 prints JSON with `zip`, `warnings`, `repos`, `workdir`. Exit 1 prints `error`. If it lists `matches`, ask which session was meant and run again.

## 3. Report

Lead with the zip path and size, the session title and how many files it holds. Then:

- **Still running** (a warning says so): the capsule stops at this moment. Tell the user to exit that session before continuing on the other machine, or to ship again later.
- **Files are not inside.** The working folder and every repo the session touched stay behind. Show `repos` as a table: path, remote, branch @ short commit, uncommitted, unpushed. Tell the user to commit and push now, and that the other machine needs the same repos at the same commit. If `workdir.entries` is set (a non-git folder with files), they must copy that folder themselves.
- **Privacy.** The capsule is plain text with every tool output of the session. Suggest AirDrop or `scp`, not a shared drive. Never upload or send the zip yourself.
- **Next step** on the other machine, as a code block:

  ```text
  /capsule:land <zip>                          # with the plugin installed
  python3 <zip> land --dir <folder>            # without the plugin
  ```
