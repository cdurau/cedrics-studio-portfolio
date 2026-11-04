import { createAuthClient } from "better-auth/react";

// Same-origin: the better-auth client defaults to window.location.origin.
// An explicit baseURL: "/" throws BetterAuthError: Invalid base URL.
export const authClient = createAuthClient();