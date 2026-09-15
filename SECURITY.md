# Security policy

## Supported version

Security fixes target the latest release on the default branch.

## Reporting

Use GitHub's private vulnerability reporting feature when it is enabled. Do not open a public issue containing a vulnerability exploit, personal data, credentials, or payment information.

## Scope

The included tools create and validate local files. They do not upload workspace data. Third-party AI tools, sync clients, and integrations have their own data behavior and are outside this repository's guarantees.

If a secret was committed, revoke or rotate it first. Removing it from the latest file or adding `.gitignore` does not remove it from Git history.
