import { describe, it, expect } from "vitest";
import { getAuthentikIdentity } from "../source/server/auth.js";
import { Context } from "hono";

// Identity extraction (Studio App Contract v2, G2 + review S2): a missing
// uid must never authenticate — getAuthentikIdentity returns null without
// X-authentik-uid, regardless of other headers.

describe("getAuthentikIdentity", () => {
  const makeContext = (headers: Record<string, string>) =>
    ({
      req: {
        header: (name: string) => headers[name.toLowerCase()],
      },
    }) as unknown as Context;

  it("returns null if X-authentik-uid is missing (uid is the identity key)", () => {
    const headers = {
      "x-authentik-username": "admin",
      "x-authentik-uid": "",
    };
    expect(getAuthentikIdentity(makeContext(headers))).toBeNull();
  });

  it("returns null without any identity headers", () => {
    expect(getAuthentikIdentity(makeContext({}))).toBeNull();
  });

  it("returns null when username exists but uid header is absent", () => {
    const headers = { "x-authentik-username": "admin" };
    expect(getAuthentikIdentity(makeContext(headers))).toBeNull();
  });

  it("extracts identity headers correctly", () => {
    const headers = {
      "x-authentik-username": "admin",
      "x-authentik-email": "admin@example.com",
      "x-authentik-name": "Admin User",
      "x-authentik-uid": "12345",
      "x-authentik-groups": "Admins|Users",
    };

    expect(getAuthentikIdentity(makeContext(headers))).toEqual({
      username: "admin",
      email: "admin@example.com",
      name: "Admin User",
      uid: "12345",
      groups: ["Admins", "Users"],
    });
  });

  it("handles comma separated groups", () => {
    const headers = {
      "x-authentik-username": "user",
      "x-authentik-uid": "67890",
      "x-authentik-groups": "Admins, Users",
    };

    expect(getAuthentikIdentity(makeContext(headers))?.groups).toEqual([
      "Admins",
      "Users",
    ]);
  });
});