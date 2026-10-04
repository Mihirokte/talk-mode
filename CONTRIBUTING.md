# Contributing

Issues and pull requests are welcome.

## Before you open a pull request

1. Run `make setup` once after cloning. Besides installing dependencies, it turns on the secret-scanning git hooks in `.githooks/`.
2. Run `make check`. It runs lint, type checks, the tests and the secret scan, and CI runs the same steps. It costs nothing: no check calls OpenRouter or Alexa.
3. Don't skip the hooks with `--no-verify`. If the scanner flags something that isn't a secret, change the content instead.

## Ground rules

- **No real values in the repo.** Keys, skill IDs, note IDs and tunnel addresses belong in `.env`. Tests use made-up values, built up at runtime when they would otherwise look like a real key (see `tests/test_secret_scan.py`).
- **Pin dependencies exactly**, in `pyproject.toml` and with `uv lock`. Pin GitHub Actions to a commit SHA.
- **Keep the core pure.** Parsing, conversation building, speech and budget logic in `bridge/` stay free of I/O. Network, storage and framework code live in the shell modules (`skill`, `llm`, `store`, `server`, `lambda_handler`).
- **Keep the tool list small.** Every tool definition is sent with every question, so it costs tokens and time on each one. A new MCP server should be config only (see the README's "MCP tools" section). Write an adapter in `bridge/mcp/adapters/` only when a server's own tools are too broad to hand to a model that hears you through a microphone.
- **Commit email.** If you don't want your email address in a public history, commit with your GitHub noreply address (GitHub, then Settings, then Emails).
