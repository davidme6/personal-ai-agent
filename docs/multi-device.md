# Multi-device guide

Each device may clone the framework into a different absolute directory. Shared files use relative paths, while `.device/device.json` records that machine's location.

Recommended order:

1. Keep the public framework in Git.
2. Initialize each device separately.
3. Compare existing private data before merging it.
4. Choose one private file-sync channel for the approved parts of `.personal/`.
5. Exclude `.git/`, `.device/`, credentials, caches, and high-sensitivity plaintext.
6. Test create, edit, offline catch-up, conflict preservation, and version recovery in a disposable folder.
7. Keep an independent backup outside the synced folder.

During initial adoption, use one writer for the same current-state file. Cloud-synced lock files do not provide reliable distributed locking.
