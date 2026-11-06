# Changelog

## 0.4.0 — 2026-11-06

- **Private-repository mode** (`init_workspace.py --private-repo`): your `.personal/` text state can be committed to your own private repository so any device or web AI can pick it up; originals (`.personal/files/`) and local-only folders stay out of Git.
- **Daily automation** (`install_schedule.py`): Windows Task Scheduler or cron; archive refresh twice a day, change trace every 10 minutes, optional private-repo auto-sync and cloud-drive sync.
- **Verbatim chat archive** (`chat_archive.py`, `refresh.py`): export local Claude Code / Codex / WorkBuddy sessions to Markdown with attachments, idempotent, plus a generated index and a stale-memory report.
- **Git-style sync** (`sync.py`, `auto_sync.py`): pull before push, three-way merge, unresolved files kept locally and listed, no force-push, read-back verification.
- **Recommended storage stack** (all replaceable, all your own accounts): private GitHub repo for program and state + Feishu/Lark Drive as the main file library + Google Drive as secondary store / web-AI inbox + optional backup client.
- **Feishu sync** (`feishu_sync.py`): official Feishu CLI with your own login, baseline registry, three-way plan, keep-both conflicts, never deletes in the cloud, plan-and-approve before the first write.
- **Cloud drive sync** (`cloud_sync.py`): rclone bisync with keep-both conflicts, mass-delete stop, and private-folder filters.
- **Leak guards** (`upload_check.py`, `workflow_gate.py`, `install_git_hooks.py`): push only to the registered private remote, optional GitHub visibility check, secret / ID / private-folder / large-file scanning; optional task-card gate.
- **Change trace** (`trace_changes.py`): append-only change ledger per device.
- **Protocol**: `AGENTS.md` rewritten in Chinese with the seven working rules, the three archive commands, the git-style diff rule, storage roles, G0–G6, and the skill-pointer pattern; new templates for task cards, work logs, handoffs, and open loops.
- **Docs**: new README with diagrams, anywhere-access, automation, sync-and-diff, and skills guides. `README.md` is now Chinese; English moved to `README.en.md`.
- **Removed** `tools/session_archive.py` (the Claude-only SessionEnd draft script wrote to the old `logs/` and `handoff/` layout); `chat_archive.py` + `refresh.py` replace it and cover every supported AI.
- **Support page**: payment QR codes and the contact email were removed; history was squashed into a single clean commit.

## 0.3.0 — 2026-09-27

- **Memory router refactored**: no longer a rigid short-term → long-term → raw-history pipeline. The router now decides what to look up based on the information the task needs: current session context (already in Working Context), long-term memory (continuously maintained state), or historical sources (semantic recall + source verification). State and evidence can be queried in parallel.
- **Session archive auto-draft**: `tools/session_archive.py` now extracts the last few user messages and today's changed files, appending a draft state summary to the handoff file. Marked as "pending confirmation" so the agent can refine it next session.
- **End-of-session distillation rule**: every session end must check for new open loops, update the handoff draft, distill new long-term facts into memory files, and verify precise facts against source files. No longer assumes an external editor's SessionEnd hook will run.
- **Trigger-based closing**: the full closing flow triggers on end-of-session keywords ("done", "wrap up", "that's it"). A daily cron task serves as backup.
- **Text-first storage**: audio/video transcribed to text before archiving; images not stored by default (text extraction only).
- **fastembed dependency note**: `requirements-memory.txt` now requires fastembed; the archive script gracefully skips semantic indexing if the dependency is missing.

## 0.2.0 — 2026-09-19

- Added optional local semantic indexing and recall for the Git-ignored private workspace.
- Kept source files authoritative and the SQLite index disposable, local, and excluded from Git.
- Added tests, bilingual setup/privacy guidance, and visible PayPal/Alipay support options.

## 0.1.0 — 2026-09-15

- First public-template candidate.
- Added a vendor-neutral entry protocol, private workspace initializer, project registry, layered state model, and session workflows.
- Separated public framework, private user data, and device-local configuration.
- Added privacy, multi-device, community, security, and support documentation.
