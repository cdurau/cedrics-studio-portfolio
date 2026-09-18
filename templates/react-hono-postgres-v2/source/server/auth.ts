import type { Context } from "hono";

export type AuthentikIdentity = {
  username: string;
  email: string;
  name: string;
  uid: string;
  groups: string[];
};

export function getAuthentikIdentity(c: Context): AuthentikIdentity | null {
  // G2: a missing uid must never authenticate (review S2). The uid — not the
  // username — is the stable identity key that the injector also enforces,
  // so it is required here too.
  const uid = c.req.header("x-authentik-uid");
  if (!uid) {
    return null;
  }

  const username = c.req.header("x-authentik-username") || "";
  const email = c.req.header("x-authentik-email") || "";
  const name = c.req.header("x-authentik-name") || "";

  const groupsHeader = c.req.header("x-authentik-groups") || "";
  const groups = groupsHeader
    .split(/[|,]/)
    .map((g) => g.trim())
    .filter(Boolean);

  return { username, email, name, uid, groups };
}