# Security

## Reporting a vulnerability

Report it privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Please don't put details in a public issue. You should get a reply within a week.

Things worth reporting include a way to:

- get the backend to accept a request that Alexa didn't sign, or one for a different skill ID;
- make paid model calls after the spend cap is reached;
- make an MCP adapter change or reveal more than it is meant to (for the Simplenote adapter: anything beyond reading the one task note and adding a single line to it);
- get a key or personal value past `scripts/secret_scan.py` into a commit.

Only the latest `main` is supported.

## If you leaked a key of your own

The scanner blocks the usual mistakes, but if a key ever reaches a remote, assume it's compromised. Revoke it with its provider first (for OpenRouter, at [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys)), and only then clean the git history. Removing the commit doesn't help anyone who already fetched it.
