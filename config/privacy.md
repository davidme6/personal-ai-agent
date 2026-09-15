# Privacy model

The framework and the private workspace have different jobs.

| Data | Default location | Git | File sync / cloud backup |
|---|---|---|---|
| Framework, templates, tools | repository root | allowed | allowed |
| Profile, memory, project notes, conversations | `.personal/` | excluded | private scope chosen by the user |
| Device paths and credentials | `.device/` | excluded | do not sync in plaintext |
| High-sensitivity documents | user-selected encrypted vault | excluded | encrypt locally before upload |

Git history is durable and difficult to retract. A later `.gitignore` rule does not erase a secret from older commits.

File synchronization is not a backup: it may propagate deletion, corruption, or conflicts. Keep a separate recoverable copy on another device, offline media, or in a locally encrypted cloud archive.

Before publishing a fork, inspect both the current tree and its Git history. Never use a real personal workspace as the starting history for a public repository.
