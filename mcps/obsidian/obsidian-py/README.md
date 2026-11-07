# obsidian-mcp

Python MCP server for one or more Obsidian vaults. It reads and writes the Markdown files directly. Obsidian does not have to be running, and no plugin is required.

The server speaks stdio for local clients and streamable HTTP with a bearer token. Vault paths, the listen address, and any Cloudflare Access team or audience come from the environment or a config file. This package does not include a personal vault path or a deployment manifest.

## Run the tests

```bash
uv sync --extra dev
uv run pytest
```

Python 3.11 or newer is required.
