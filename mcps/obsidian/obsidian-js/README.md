# obsidian-vault-mcp

TypeScript MCP server for Obsidian vaults. It works on the Markdown files directly and builds to a single file, `dist/obsidian-vault-mcp.mjs`.

## Run the tests

```bash
npm ci
npm test
```

Node.js 20 or newer is required. Pass vaults on the command line as `--vault name=/path/to/vault`. Do not commit a vault path.
