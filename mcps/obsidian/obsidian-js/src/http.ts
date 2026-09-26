import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { timingSafeEqual, createHash } from "node:crypto";
import http from "node:http";
import type { HttpConfig } from "./config.js";
import { createServer } from "./server.js";
import type { VaultRegistry } from "./vault/vault.js";
import { VERSION } from "./version.js";

const LOOPBACK = new Set(["127.0.0.1", "::1", "localhost"]);

function tokenMatches(header: string | undefined, token: string): boolean {
  if (!header) return false;
  const m = /^Bearer\s+(.+)$/i.exec(header.trim());
  if (!m) return false;
  // Hashes vergleichen → konstante Länge für timingSafeEqual
  const a = createHash("sha256").update(m[1]!.trim()).digest();
  const b = createHash("sha256").update(token).digest();
  return timingSafeEqual(a, b);
}

function sendJson(res: http.ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { "content-type": "application/json", ...headers });
  res.end(JSON.stringify(body));
}

export interface StartedHttpServer {
  server: http.Server;
  url: string;
  close(): Promise<void>;
}

/**
 * Startet den MCP-Server im Streamable-HTTP-Modus (stateless: pro Request eine
 * frische Server-Instanz, dadurch horizontal skalierbar und ohne Session-Leaks).
 */
export async function startHttpServer(registry: VaultRegistry, cfg: HttpConfig, log: (msg: string) => void = console.error): Promise<StartedHttpServer> {
  const isLoopback = LOOPBACK.has(cfg.host);
  if (!cfg.authToken && !isLoopback && !cfg.allowNoAuth) {
    throw new Error(
      `HTTP auf ${cfg.host} ohne MCP_AUTH_TOKEN verweigert. Setze MCP_AUTH_TOKEN (empfohlen) ` +
        "oder – nur in einem geschützten Netz wie Tailscale – MCP_ALLOW_NO_AUTH=true.",
    );
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://localhost");

    if (url.pathname === "/healthz" || url.pathname === "/health") {
      if (req.method !== "GET" && req.method !== "HEAD") return sendJson(res, 405, { error: "Method not allowed" });
      return sendJson(res, 200, { status: "ok", name: "obsidian-vault-mcp", version: VERSION, vaults: registry.names().length });
    }

    if (url.pathname !== cfg.path) return sendJson(res, 404, { error: "Not found", mcpEndpoint: cfg.path });

    if (cfg.authToken && !tokenMatches(req.headers.authorization, cfg.authToken)) {
      return sendJson(res, 401, { jsonrpc: "2.0", error: { code: -32001, message: "Unauthorized" }, id: null }, { "www-authenticate": 'Bearer realm="obsidian-vault-mcp"' });
    }

    if (req.method !== "POST") {
      // Stateless-Modus: kein GET-SSE-Stream und keine Sessions zum Beenden
      return sendJson(res, 405, { jsonrpc: "2.0", error: { code: -32000, message: "Method not allowed" }, id: null }, { allow: "POST" });
    }

    const mcp = createServer(registry);
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
      // Schutz vor DNS-Rebinding, wenn ohne Token lokal gebunden
      ...(cfg.authToken
        ? {}
        : {
            enableDnsRebindingProtection: isLoopback,
            allowedHosts: [`localhost:${cfg.port}`, `127.0.0.1:${cfg.port}`, `[::1]:${cfg.port}`, "localhost", "127.0.0.1"],
          }),
    });
    res.on("close", () => {
      void transport.close();
      void mcp.close();
    });
    try {
      await mcp.connect(transport);
      await transport.handleRequest(req, res);
    } catch (e) {
      log(`[http] Fehler: ${(e as Error).stack ?? e}`);
      if (!res.headersSent) sendJson(res, 500, { jsonrpc: "2.0", error: { code: -32603, message: "Internal server error" }, id: null });
    }
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(cfg.port, cfg.host, () => {
      server.off("error", reject);
      resolve();
    });
  });
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : cfg.port;
  const hostForUrl = cfg.host.includes(":") ? `[${cfg.host}]` : cfg.host;
  const url = `http://${hostForUrl}:${port}${cfg.path}`;
  log(`[obsidian-vault-mcp] HTTP bereit auf ${url}${cfg.authToken ? " (Bearer-Token aktiv)" : " (ohne Auth)"}`);

  return {
    server,
    url,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections?.();
      }),
  };
}
