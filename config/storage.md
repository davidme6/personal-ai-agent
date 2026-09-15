# Storage map

## Public framework

- `AGENTS.md`: universal entry protocol
- `config/`: architecture, privacy, and example schemas
- `workflows/`: repeatable session procedures
- `templates/`: starters copied into the private workspace
- `tools/` and `tests/`: initialization and validation

## Private runtime

- `.personal/profile.md`: stable user preferences and boundaries
- `.personal/current-state.md`: one authoritative global status
- `.personal/project-registry.json`: routes requests to project entries
- `.personal/projects/`: project-specific state and evidence
- `.personal/knowledge/`: user-curated knowledge
- `.personal/logs/`: work and conversation records

## Device-local runtime

- `.device/device.json`: paths and device identity
- `.device/secrets/`: credentials or encrypted-vault pointers

Do not duplicate current state across platform-specific runtime folders. Platform adapters should point to this workspace.
