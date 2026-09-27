# Changelog

## 0.3.0 — 2026-09-27

- **Memory router refactored**: no longer a rigid short-term → long-term → raw-history pipeline. The router now decides what to look up based on the information the task needs: current session context (already in Working Context), long-term memory (continuously maintained state), or historical sources (semantic recall + source verification). State and evidence can be queried in parallel.
- **Session archive auto-draft**: `tools/session_archive.py` now extracts the last few user messages and today's changed files, appending a draft state summary to the handoff file. Marked as "pending confirmation" so the agent can refine it next session.
- **End-of-session distillation rule**: every session end must check for new open loops, update the handoff draft, and verify precise facts against source files. No longer assumes an external editor's SessionEnd hook will run.
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
