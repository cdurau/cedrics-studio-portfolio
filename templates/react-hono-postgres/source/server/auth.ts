import type { Context } from "hono";

export type AuthentikIdentity = {
  username: string;
  email: string;
  name: string;
  uid: string;
  groups: string[];
};

export function getAuthentikIdentity(c: Context): AuthentikIdentity | null {
  const username = c.req.header("x-authentik-username");
  if (!username) {
    return null;
  }

  const email = c.req.header("x-authentik-email") || "";
  const name = c.req.header("x-authentik-name") || "";
  const uid = c.req.header("x-authentik-uid") || "";

  const groupsHeader = c.req.header("x-authentik-groups") || "";
  const groups = groupsHeader
    .split(/[|,]/)
    .map((g) => g.trim())
    .filter(Boolean);

  return { username, email, name, uid, groups };
}
