# FAQ

## Does this include an AI model?

No. It is a file protocol and starter workspace for assistants that can read local files.

## Does it upload my data?

The included Python tools only create and validate local files. Integrations added by a user may have different behavior and should be reviewed separately.

## Can I sync `.personal/`?

Yes, after choosing a private scope and testing exclusions. Keep credentials and high-sensitivity plaintext out of ordinary sync.

## Why not put everything in a private Git repository?

Git preserves history and makes accidental inclusion hard to retract. Personal records often need different retention, encryption, and deletion rules than source code.

## Is sync a backup?

No. Sync may reproduce deletion or corruption. Keep an independent recoverable copy.
