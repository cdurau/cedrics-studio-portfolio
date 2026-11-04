import * as readline from "node:readline/promises";
import { seedUser, resetPassword, revokeSessions } from "./admin.js";
import { validateRuntimeEnv } from "./env.js";

// Interactive admin CLI — run only inside the container or on the host
// (npm run admin:seed-user | admin:reset-password | admin:revoke-sessions <email>).
// Passwords are read via hidden stdin prompt; never passed as argv/env.

// Hidden password prompt (claude gesamtreview 2026-09-20, M1).
// The previous version read the password with a raw-mode listener while the
// readline interface used for the email/name prompts was still active:
// readline kept echoing every keystroke, so the password appeared in clear
// text on the terminal, and a pasted password (one data chunk) never matched
// the single-character "\r" check and hung the CLI. Now:
//   - the caller closes its readline interface BEFORE calling this function,
//   - input is processed character by character inside each chunk (paste-safe),
//   - nothing is echoed,
//   - a non-TTY stdin is refused (no silent plain-text fallback).
function hideInput(query: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const stdin = process.stdin;
    if (!stdin.isTTY || typeof stdin.setRawMode !== "function") {
      reject(new Error("Password prompt requires an interactive TTY (use `docker exec -it`)"));
      return;
    }
    process.stdout.write(query);
    stdin.setRawMode(true);
    stdin.resume();
    stdin.setEncoding("utf8");
    let password = "";
    const finish = (err?: Error) => {
      stdin.setRawMode(false);
      stdin.removeListener("data", onData);
      stdin.pause();
      process.stdout.write("\n");
      if (err) reject(err);
      else resolve(password);
    };
    const onData = (chunk: string) => {
      for (const char of chunk) {
        if (char === "\n" || char === "\r" || char === "\u0004") {
          finish();
          return;
        } else if (char === "\u0003") {
          finish(new Error("Aborted"));
          return;
        } else if (char === "\b" || char === "\x7f") {
          password = password.slice(0, -1);
        } else {
          password += char;
        }
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

  // Plain (visible) prompts only; the interface is closed before any password
  // is read so it can never echo hidden input (gesamtreview 2026-09-20, M1).
  async function ask(questions: string[]): Promise<string[]> {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    try {
      const answers: string[] = [];
      for (const q of questions) answers.push((await rl.question(q)).trim());
      return answers;
    } finally {
      rl.close();
    }
  }

  // No process.exit on the success path (claude review 2026-09-19, HIGH-2):
  // Node exits 0 naturally; the .catch handler below exits 1 on failure.
  // Interactive commands need a TTY up front: with piped stdin readline can
  // hit EOF mid-prompt, leave its promise pending and let Node exit 0 — a
  // silent "success" without any user being created.
  if ((command === "seed-user" || command === "reset-password") && !process.stdin.isTTY) {
    throw new Error("This command requires an interactive TTY (use `docker exec -it`)");
  }

  if (command === "seed-user") {
    const [email, name] = await ask(["Email: ", "Name: "]);
    const password = await hideInput("Password: ");
    if (!email || !password) throw new Error("Email and password required");
    const user = await seedUser(email, name || email.split("@")[0], password, { verified: true });
    console.log(`User ${email} created and verified (id: ${(user as { id: string }).id.slice(0, 8)}…).`);
  } else if (command === "reset-password") {
    const [email] = await ask(["Email: "]);
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
    throw new Error("Unknown command. Use: seed-user | reset-password | revoke-sessions <email>");
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});