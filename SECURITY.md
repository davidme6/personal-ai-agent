# Security policy

## Supported version

Security fixes target the latest release on the default branch.

## Reporting

Use the repository's Security tab → "Report a vulnerability" (GitHub private vulnerability reporting). Do not open a public issue containing a vulnerability exploit, personal data, credentials, or payment information.

## Scope

Most tools only read and write local files. These tools talk to the network, and only when you configure and run them: `sync.py` / `auto_sync.py` (push to the private remote you registered), `feishu_sync.py` (your own Feishu/Lark account through its official CLI), `cloud_sync.py` (your own cloud drive through rclone), `call_llm.py` (sends your prompt to the model provider you choose), `doubao_export.py` (downloads image links found in your own sessions) and `upload_check.py` (optionally asks `gh` whether the remote is private). Third-party AI tools, sync clients, and integrations have their own data behavior and are outside this repository's guarantees.

If a secret was committed, revoke or rotate it first. Removing it from the latest file or adding `.gitignore` does not remove it from Git history.
