import { promises as fs } from "node:fs";
import path from "node:path";

export class VaultError extends Error {
  constructor(
    message: string,
    readonly code: "NOT_FOUND" | "EXISTS" | "INVALID_PATH" | "READ_ONLY" | "AMBIGUOUS" | "INVALID" = "INVALID",
  ) {
    super(message);
  }
}

/** Dateiendungen, die als Notiz (Markdown) behandelt werden. */
export const NOTE_EXTENSIONS = new Set([".md"]);
/** Weitere Textdateien, die gelesen werden dürfen. */
export const TEXT_EXTENSIONS = new Set([".md", ".markdown", ".txt", ".canvas", ".base", ".csv"]);
/** Ordner, die nie durchsucht oder angefasst werden. */
export const IGNORED_DIRS = new Set(["node_modules"]);

/**
 * Normalisiert einen vom Client übergebenen, vault-relativen Pfad zu POSIX-Form
 * ("Ordner/Notiz.md") und lehnt alles ab, was aus dem Vault herausführen könnte
 * oder versteckte Ordner (.obsidian, .git, .trash, …) betrifft.
 */
export function normalizeVaultPath(input: string): string {
  if (typeof input !== "string") throw new VaultError("Pfad fehlt", "INVALID_PATH");
  let p = input.trim().replace(/\\/g, "/");
  if (p.includes("\0")) throw new VaultError("Ungültiges Zeichen im Pfad", "INVALID_PATH");
  p = p.replace(/^\.\/+/, "").replace(/^\/+/, "");
  const normalized = path.posix.normalize(p);
  if (normalized === "." || normalized === "") return "";
  if (normalized === ".." || normalized.startsWith("../")) {
    throw new VaultError(`Pfad liegt außerhalb des Vaults: ${input}`, "INVALID_PATH");
  }
  for (const seg of normalized.split("/")) {
    if (seg.startsWith(".")) {
      throw new VaultError(`Versteckte Pfade (z. B. .obsidian, .git, .trash) sind gesperrt: ${input}`, "INVALID_PATH");
    }
  }
  return normalized.replace(/\/+$/, "");
}

/**
 * Löst einen normalisierten Pfad absolut auf und prüft zusätzlich per realpath,
 * dass kein Symlink aus dem Vault herausführt.
 */
export async function toAbsoluteSafe(rootReal: string, relPosix: string): Promise<string> {
  const abs = path.resolve(rootReal, ...relPosix.split("/").filter(Boolean));
  if (!isInside(rootReal, abs)) {
    throw new VaultError(`Pfad liegt außerhalb des Vaults: ${relPosix}`, "INVALID_PATH");
  }
  // Nächsten existierenden Vorfahren per realpath prüfen (Symlink-Schutz).
  let probe = abs;
  for (;;) {
    try {
      const real = await fs.realpath(probe);
      if (!isInside(rootReal, real)) {
        throw new VaultError(`Symlink zeigt aus dem Vault heraus: ${relPosix}`, "INVALID_PATH");
      }
      break;
    } catch (e) {
      if (e instanceof VaultError) throw e;
      const parent = path.dirname(probe);
      if (parent === probe) break;
      probe = parent;
    }
  }
  return abs;
}

export function isInside(root: string, candidate: string): boolean {
  const rel = path.relative(root, candidate);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

/** Hängt ".md" an, falls keine bekannte Textendung vorhanden ist. */
export function ensureNoteExtension(relPosix: string): string {
  const ext = path.posix.extname(relPosix).toLowerCase();
  if (TEXT_EXTENSIONS.has(ext)) return relPosix;
  return `${relPosix}.md`;
}

export function isNotePath(relPosix: string): boolean {
  return NOTE_EXTENSIONS.has(path.posix.extname(relPosix).toLowerCase());
}

export function noteBasename(relPosix: string): string {
  const base = path.posix.basename(relPosix);
  const ext = path.posix.extname(base);
  return NOTE_EXTENSIONS.has(ext.toLowerCase()) ? base.slice(0, -ext.length) : base;
}

export function stripNoteExt(relPosix: string): string {
  const ext = path.posix.extname(relPosix);
  return NOTE_EXTENSIONS.has(ext.toLowerCase()) ? relPosix.slice(0, -ext.length) : relPosix;
}
