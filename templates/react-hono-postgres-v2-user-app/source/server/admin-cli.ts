import * as readline from "node:readline/promises";
import { seedUser, resetPassword, revokeSessions } from "./admin.js";
import { validateRuntimeEnv } from "./env.js";

// Interactive admin CLI — run only inside the container or on the host
// (npm run admin:seed-user | admin:reset-password | admin:revoke-sessions <email>).
// Passwords are read via hidden stdin prompt; never passed as argv/env.

function hideInput(query: string): Promise<string> {
  return new Promise((resolve) => {
    const stdin = process.stdin;
    process.stdout.write(query);
    stdin.setRawMode(true);
    stdin.resume();
    stdin.setEncoding("utf8");
    let password = "";
    const onData = (char: string) => {
      if (char === "\n" || char === "\r" || char === "\u0004") {
        stdin.setRawMode(false);
        stdin.removeListener("data", onData);
        stdin.pause();
        process.stdout.write("\n");
        resolve(password);
      } else if (char === "\u0003") {
        stdin.setRawMode(false);
        process.exit(1);
      } else if (char === "\b" || char === "\x7f") {
        password = password.slice(0, -1);
      } else {
        password += char;
      }
    };
    stdin.on("data", onData);
  });
}

async function main() {
  // Admin operations use the auth context (argon2 hash, session revocation);
  // refuse to run without the required runtime secrets (fail-closed, M1).
  validateRuntimeEnv();
  const command = process.argv[2];
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (command === "seed-user") {
      const email = await rl.question("Email: ");
      const name = await rl.question("Name: ");
      const password = await hideInput("Password: ");
      if (!email || !password) throw new Error("Email and password required");
      const user = await seedUser(email, name || email.split("@")[0], password, { verified: true });
      console.log(`User ${email} created and verified (id: ${(user as { id: string }).id.slice(0, 8)}…).`);
    } else if (command === "reset-password") {
      const email = await rl.question("Email: ");
      const password = await hideInput("New password: ");
      if (!email || !password) throw new Error("Email and password required");
      await resetPassword(email, password);
      console.log(`Password reset for ${email}.`);
    } else if (command === "revoke-sessions") {
      const email = process.argv[3];
      if (!email) throw new Error("Usage: admin:revoke-sessions <email>");
      await revokeSessions(email);
      console.log(`All sessions revoked for ${email}.`);
    } else {
      console.error("Unknown command. Use: seed-user | reset-password | revoke-sessions <email>");
      process.exit(1);
    }
  } finally {
    rl.close();
    process.exit(0);
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});