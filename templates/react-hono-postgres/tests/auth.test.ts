import { describe, it, expect } from "vitest";
import { getAuthentikIdentity } from "../source/server/auth.js";
import { Context } from "hono";

describe("getAuthentikIdentity", () => {
  it("should return null if X-authentik-username is missing", () => {
    const mockContext = {
      req: {
        header: (name: string) => undefined,
      },
    } as unknown as Context;

    expect(getAuthentikIdentity(mockContext)).toBeNull();
  });

  it("should extract identity headers correctly", () => {
    const headers: Record<string, string> = {
      "x-authentik-username": "admin",
      "x-authentik-email": "admin@example.com",
      "x-authentik-name": "Admin User",
      "x-authentik-uid": "12345",
      "x-authentik-groups": "Admins|Users",
    };

    const mockContext = {
      req: {
        header: (name: string) => headers[name.toLowerCase()],
      },
    } as unknown as Context;

    const identity = getAuthentikIdentity(mockContext);
    expect(identity).toEqual({
      username: "admin",
      email: "admin@example.com",
      name: "Admin User",
      uid: "12345",
      groups: ["Admins", "Users"],
    });
  });

  it("should handle comma separated groups", () => {
    const headers: Record<string, string> = {
      "x-authentik-username": "user",
      "x-authentik-groups": "Admins, Users",
    };

    const mockContext = {
      req: {
        header: (name: string) => headers[name.toLowerCase()],
      },
    } as unknown as Context;

    const identity = getAuthentikIdentity(mockContext);
    expect(identity?.groups).toEqual(["Admins", "Users"]);
  });
});
