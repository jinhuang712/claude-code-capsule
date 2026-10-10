# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.0] - 2026-10-11

### Added
- `/capsule:ship [session-id]` skill: pack one session into a zip with its transcript, subagent transcripts, tool outputs, checkpoints, session env, task lists, scratchpad, referenced plan and paste files, project memory and prompt history; record and warn about the git state of every repo the session touched.
- `/capsule:land <zip>` skill: ask which folder the session lives in, remap home, working folder, temp root, config dir, project folder name and any `--map` paths in one pass, verify every file, check repos are at the right commit, and print the resume command.
- `scripts/capsule.py` (Python 3.9+, stdlib only) with subcommands `list ship find inspect land`; exit codes 0 / 1 / 3 (conflict) / 4 (damaged).
- Capsules run on their own: `python3 capsule-<id>.zip land --dir <folder>`.
- Hardened `land` for untrusted capsules: manifest schema check, symlinks only inside the scratchpad tree and only pointing into the session, no writes through existing symlinks, masked mode bits, all checks before the first write; `--force` keeps replaced files as `*.capsule-bak-<time>`.
- End-to-end test `tests/e2e.sh`, including hostile-capsule cases.
