import { readFileSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { z } from "zod";

/**
 * Konfiguration des Servers.
 *
 * Quellen (höchste Priorität zuerst):
 *   1. CLI-Flags            (--vault, --http, --port, --host, --read-only, --config, --locale)
 *   2. Umgebungsvariablen   (OBSIDIAN_VAULTS, MCP_TRANSPORT, MCP_PORT, MCP_AUTH_TOKEN, …)
 *   3. JSON-Datei           (--config oder OBSIDIAN_MCP_CONFIG)
 */

const DailyNotesSchema = z
  .object({
    folder: z.string().optional(),
    format: z.string().optional(),
    template: z.string().optional(),
  })
  .strict();

const VaultEntrySchema = z
  .object({
    name: z.string().min(1).optional(),
    path: z.string().min(1),
    readOnly: z.boolean().optional(),
    dailyNotes: DailyNotesSchema.optional(),
  })
  .strict();

const FileConfigSchema = z
  .object({
    vaults: z.array(VaultEntrySchema).min(1),
    defaultVault: z.string().optional(),
    readOnly: z.boolean().optional(),
    locale: z.string().optional(),
    trash: z.enum(["obsidian", "none"]).optional(),
    http: z
      .object({
        host: z.string().optional(),
        port: z.number().int().positive().optional(),
        path: z.string().optional(),
      })
      .strict()
      .optional(),
  })
  .strict();

export type DailyNotesOverride = z.infer<typeof DailyNotesSchema>;

export interface VaultConfig {
  name: string;
  path: string;
  readOnly: boolean;
  dailyNotes?: DailyNotesOverride;
}

export interface HttpConfig {
  host: string;
  port: number;
  path: string;
  authToken?: string;
  allowNoAuth: boolean;
}

export interface AppConfig {
  vaults: VaultConfig[];
  defaultVault: string;
  locale: string;
  /** "obsidian" = gelöschte Notizen landen in <vault>/.trash, "none" = endgültig löschen erlaubt nur mit permanent=true */
  trash: "obsidian" | "none";
  transport: "stdio" | "http";
  http: HttpConfig;
}

export class ConfigError extends Error {}

export function expandHome(p: string): string {
  if (p === "~") return os.homedir();
  if (p.startsWith("~/")) return path.join(os.homedir(), p.slice(2));
  return p;
}

export function slugifyVaultName(name: string): string {
  const slug = name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "vault";
}

function parseBool(v: string | undefined): boolean | undefined {
  if (v === undefined || v === "") return undefined;
  return ["1", "true", "yes", "on", "ja"].includes(v.toLowerCase());
}

/** "name=/pfad" oder nur "/pfad" (Name = Ordnername). */
function parseVaultSpec(spec: string): { name?: string; path: string } {
  const trimmed = spec.trim();
  const eq = trimmed.indexOf("=");
  // Ein "=" nur als Trenner werten, wenn davor kein Pfadtrenner steht.
  if (eq > 0 && !trimmed.slice(0, eq).includes("/") && !trimmed.slice(0, eq).includes("\\")) {
    return { name: trimmed.slice(0, eq).trim(), path: trimmed.slice(eq + 1).trim() };
  }
  return { path: trimmed };
}

interface CliArgs {
  vaults: string[];
  config?: string;
  http?: boolean;
  host?: string;
  port?: number;
  readOnly?: boolean;
  locale?: string;
  help?: boolean;
  version?: boolean;
}

export function parseArgs(argv: string[]): CliArgs {
  const out: CliArgs = { vaults: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]!;
    const next = (): string => {
      const v = argv[++i];
      if (v === undefined) throw new ConfigError(`Fehlender Wert für ${arg}`);
      return v;
    };
    const [flag, inline] = arg.startsWith("--") && arg.includes("=") ? [arg.slice(0, arg.indexOf("=")), arg.slice(arg.indexOf("=") + 1)] : [arg, undefined];
    const value = (): string => inline ?? next();
    switch (flag) {
      case "--vault":
      case "-v":
        out.vaults.push(value());
        break;
      case "--config":
      case "-c":
        out.config = value();
        break;
      case "--http":
        out.http = true;
        break;
      case "--stdio":
        out.http = false;
        break;
      case "--host":
        out.host = value();
        break;
      case "--port":
      case "-p": {
        const n = Number(value());
        if (!Number.isInteger(n) || n <= 0) throw new ConfigError(`Ungültiger Port: ${arg}`);
        out.port = n;
        break;
      }
      case "--read-only":
      case "--readonly":
        out.readOnly = true;
        break;
      case "--locale":
        out.locale = value();
        break;
      case "--help":
      case "-h":
        out.help = true;
        break;
      case "--version":
        out.version = true;
        break;
      default:
        if (arg.startsWith("-")) throw new ConfigError(`Unbekannte Option: ${arg}`);
        out.vaults.push(arg);
    }
  }
  return out;
}

export function loadConfig(argv: string[], env: NodeJS.ProcessEnv = process.env): AppConfig {
  const args = parseArgs(argv);

  const configPath = args.config ?? env.OBSIDIAN_MCP_CONFIG;
  let fileCfg: z.infer<typeof FileConfigSchema> | undefined;
  if (configPath) {
    const resolved = path.resolve(expandHome(configPath));
    let raw: string;
    try {
      raw = readFileSync(resolved, "utf8");
    } catch (e) {
      throw new ConfigError(`Config-Datei nicht lesbar: ${resolved} (${(e as Error).message})`);
    }
    const parsed = FileConfigSchema.safeParse(JSON.parse(raw));
    if (!parsed.success) {
      throw new ConfigError(`Ungültige Config-Datei ${resolved}:\n${z.prettifyError(parsed.error)}`);
    }
    fileCfg = parsed.data;
  }

  const globalReadOnly = args.readOnly ?? parseBool(env.OBSIDIAN_READONLY) ?? fileCfg?.readOnly ?? false;

  // Vault-Liste: CLI > ENV > Datei
  let specs: { name?: string; path: string; readOnly?: boolean; dailyNotes?: DailyNotesOverride }[];
  if (args.vaults.length > 0) {
    specs = args.vaults.map(parseVaultSpec);
  } else if (env.OBSIDIAN_VAULTS?.trim()) {
    specs = env.OBSIDIAN_VAULTS.split(/[;\n]/).filter((s) => s.trim()).map(parseVaultSpec);
  } else if (env.OBSIDIAN_VAULT?.trim()) {
    specs = [parseVaultSpec(env.OBSIDIAN_VAULT)];
  } else if (fileCfg) {
    specs = fileCfg.vaults;
  } else {
    throw new ConfigError(
      "Kein Vault konfiguriert. Nutze z. B. `obsidian-vault-mcp ~/Obsidian/MeinVault`, " +
        "`--vault name=/pfad`, OBSIDIAN_VAULTS=\"name=/pfad;name2=/pfad2\" oder --config vaults.json.",
    );
  }

  const vaults: VaultConfig[] = [];
  const seen = new Set<string>();
  for (const spec of specs) {
    const abs = path.resolve(expandHome(spec.path));
    let st;
    try {
      st = statSync(abs);
    } catch {
      throw new ConfigError(`Vault-Pfad existiert nicht: ${abs}`);
    }
    if (!st.isDirectory()) throw new ConfigError(`Vault-Pfad ist kein Ordner: ${abs}`);
    let name = slugifyVaultName(spec.name ?? path.basename(abs));
    if (seen.has(name)) {
      let i = 2;
      while (seen.has(`${name}-${i}`)) i++;
      name = `${name}-${i}`;
    }
    seen.add(name);
    vaults.push({
      name,
      path: abs,
      readOnly: globalReadOnly || (spec.readOnly ?? false),
      dailyNotes: spec.dailyNotes,
    });
  }

  const requestedDefault = env.OBSIDIAN_DEFAULT_VAULT ?? fileCfg?.defaultVault;
  const defaultVault = requestedDefault ? slugifyVaultName(requestedDefault) : vaults[0]!.name;
  if (!vaults.some((v) => v.name === defaultVault)) {
    throw new ConfigError(`Default-Vault "${defaultVault}" ist nicht konfiguriert (vorhanden: ${vaults.map((v) => v.name).join(", ")})`);
  }

  const transportEnv = env.MCP_TRANSPORT?.toLowerCase();
  if (transportEnv && transportEnv !== "stdio" && transportEnv !== "http") {
    throw new ConfigError(`MCP_TRANSPORT muss "stdio" oder "http" sein, nicht "${env.MCP_TRANSPORT}"`);
  }
  const transport: "stdio" | "http" = args.http === true ? "http" : args.http === false ? "stdio" : ((transportEnv as "stdio" | "http" | undefined) ?? "stdio");

  const portEnv = env.MCP_PORT ?? env.PORT;
  const port = args.port ?? (portEnv ? Number(portEnv) : undefined) ?? fileCfg?.http?.port ?? 3333;
  if (!Number.isInteger(port) || port <= 0) throw new ConfigError(`Ungültiger Port: ${portEnv}`);

  let httpPath = env.MCP_PATH ?? fileCfg?.http?.path ?? "/mcp";
  if (!httpPath.startsWith("/")) httpPath = `/${httpPath}`;

  return {
    vaults,
    defaultVault,
    locale: args.locale ?? env.OBSIDIAN_LOCALE ?? fileCfg?.locale ?? "de",
    trash: env.OBSIDIAN_TRASH === "none" || env.OBSIDIAN_TRASH === "obsidian" ? env.OBSIDIAN_TRASH : (fileCfg?.trash ?? "obsidian"),
    transport,
    http: {
      host: args.host ?? env.MCP_HOST ?? fileCfg?.http?.host ?? "127.0.0.1",
      port,
      path: httpPath,
      authToken: env.MCP_AUTH_TOKEN?.trim() || undefined,
      allowNoAuth: parseBool(env.MCP_ALLOW_NO_AUTH) ?? false,
    },
  };
}

export const HELP_TEXT = `obsidian-vault-mcp – MCP-Server für Obsidian Vaults

Aufruf:
  obsidian-vault-mcp [optionen] [vault-pfad | name=vault-pfad ...]

Optionen:
  -v, --vault <name=pfad>   Vault hinzufügen (mehrfach möglich)
  -c, --config <datei>      JSON-Konfiguration laden
      --http                Streamable-HTTP statt stdio starten
      --host <host>         HTTP-Host (Standard 127.0.0.1)
  -p, --port <port>         HTTP-Port (Standard 3333)
      --read-only           Alle Vaults nur lesend
      --locale <de|en|…>    Sprache für Datumsformate der Daily Notes (Standard de)
  -h, --help                Hilfe anzeigen
      --version             Version anzeigen

Umgebungsvariablen:
  OBSIDIAN_VAULTS           "name=/pfad;name2=/pfad2"
  OBSIDIAN_VAULT            einzelner Vault-Pfad
  OBSIDIAN_DEFAULT_VAULT    Name des Standard-Vaults
  OBSIDIAN_READONLY         true/false
  OBSIDIAN_LOCALE           Locale für Datumsformate
  OBSIDIAN_MCP_CONFIG       Pfad zur JSON-Konfiguration
  MCP_TRANSPORT             stdio | http
  MCP_HOST / MCP_PORT       HTTP-Bindung
  MCP_PATH                  HTTP-Endpunkt (Standard /mcp)
  MCP_AUTH_TOKEN            Bearer-Token für HTTP (dringend empfohlen)
  MCP_ALLOW_NO_AUTH         true = HTTP ohne Token auch auf nicht-lokalen Hosts erlauben
`;
