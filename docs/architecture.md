# Architecture

Personal AI Agent treats continuity as a data-design problem.

## Control plane

The public control plane contains rules, templates, workflows, and checks. It can be forked and updated through Git without carrying a user's life into the repository.

## Data plane

The private data plane lives under `.personal/`. It contains the user's profile, current state, project records, knowledge, and logs. The repository ignores it by default.

## Device plane

The device plane lives under `.device/`. It stores paths, runtime adapters, and credential pointers that differ across machines. Shared documents refer to project-relative paths.

## One current state

`.personal/current-state.md` is the only global current-state summary. Dated handoffs preserve history; project state belongs to each project. This prevents multiple “latest” files from silently diverging.

## Retrieval path

The agent starts with profile and current state, selects a project through the registry, opens the project entry, and then loads evidence on demand. This keeps context small without discarding history.

## Optional semantic-memory cache

For questions that depend on older decisions, `tools/memory_index.py` can embed the Git-ignored `.personal/` text files into `.local/memory_index.db`. `tools/memory_recall.py` returns ranked excerpts with their source paths. The agent must reopen those source files before treating a recalled claim as current or verified.

The index is a local cache, not another memory authority. It can be deleted and rebuilt from the private files on each device. Because the database contains plaintext excerpts as well as vectors, it is excluded from Git and should be protected like the source material.
