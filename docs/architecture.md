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
