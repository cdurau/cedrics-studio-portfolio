import { createServer, type Server } from "node:http";

// Minimal local OIDC provider mock for CI (executor brief §2.11):
// discovery + authorize + token + userinfo. User selection via login_hint;
// email_verified=false fixture supported (contract test).

export type OidcUser = {
  sub: string;
  email: string;
  email_verified: boolean;
  name?: string;
  picture?: string;
};

export type MockOidc = {
  issuer: string;
  server: Server;
  port: number;
  close: () => Promise<void>;
};

export function startMockOidc(users: Record<string, OidcUser>): Promise<MockOidc> {
  return new Promise((resolve) => {
    let issuer = "";

    const server = createServer(async (req, res) => {
      const url = new URL(req.url ?? "/", "http://127.0.0.1");
      res.setHeader("content-type", "application/json");

      if (url.pathname === "/.well-known/openid-configuration") {
        res.end(JSON.stringify({
          issuer,
          authorization_endpoint: `${issuer}/authorize`,
          token_endpoint: `${issuer}/token`,
          userinfo_endpoint: `${issuer}/userinfo`,
          id_token_signing_alg_values_supported: ["HS256"],
          // NOTE: no jwks_uri — advertising one would enable ID-token JWKS
          // verification in better-auth against a non-existent key set.
        }));
        return;
      }

      // Auto-approve authorization: capture login_hint, redirect back with code.
      if (url.pathname === "/authorize") {
        const state = url.searchParams.get("state");
        const callback = url.searchParams.get("redirect_uri") ?? "";
        const loginHint = url.searchParams.get("login_hint") ?? "";
        const code = Buffer.from(JSON.stringify({ hint: loginHint })).toString("base64url");
        const sep = callback.includes("?") ? "&" : "?";
        res.writeHead(302, { location: `${callback}${sep}code=${code}&state=${state}` });
        res.end();
        return;
      }

      if (url.pathname === "/token") {
        let body = "";
        for await (const chunk of req) body += chunk;
        const params = new URLSearchParams(body);
        let hint = "";
        try {
          hint = JSON.parse(Buffer.from(params.get("code") ?? "", "base64url").toString()).hint ?? "";
        } catch { /* invalid code -> default user */ }
        const key = hint || Object.keys(users)[0];
        const u = users[key] ?? users[Object.keys(users)[0]];
        const now = Math.floor(Date.now() / 1000);
        const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
        const payload = b64({
          iss: issuer, sub: u.sub, aud: "test-client", iat: now, exp: now + 3600,
          email: u.email, email_verified: u.email_verified, name: u.name ?? "",
        });
        // id_token signature is not verified against JWKS in this mock path
        // (provider.idToken verification is not enabled in the generic config).
        res.end(JSON.stringify({
          access_token: `at_${u.sub}`,
          token_type: "Bearer",
          id_token: `eyJhbGciOiJIUzI1NiJ9.${payload}.mock`,
          expires_in: 3600,
        }));
        return;
      }

      if (url.pathname === "/userinfo") {
        const auth = req.headers.authorization ?? "";
        const sub = auth.includes("at_") ? auth.replace("Bearer at_", "") : Object.keys(users)[0];
        const u = users[sub] ?? Object.values(users)[0];
        res.end(JSON.stringify(u));
        return;
      }

      res.writeHead(404).end("{}");
    });

    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      issuer = `http://127.0.0.1:${port}`;
      resolve({ issuer, server, port, close: () => new Promise((r) => server.close(() => r())) });
    });
  });
}