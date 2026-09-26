import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { formatDate, parseDateInput, renderTemplate } from "../vault/dates.js";
import { insertContent } from "../vault/edit.js";
import { VaultError, ensureNoteExtension, normalizeVaultPath } from "../vault/paths.js";
import type { Vault, VaultRegistry } from "../vault/vault.js";
import { json, safe, vaultParam } from "./util.js";

export interface DailyNoteRef {
  path: string;
  title: string;
  date: Date;
  exists: boolean;
}

export async function dailyNoteRef(v: Vault, dateInput: string | undefined, locale: string): Promise<DailyNoteRef> {
  const settings = await v.dailyNotesSettings();
  const date = parseDateInput(dateInput);
  const title = formatDate(date, settings.format, locale);
  const rel = normalizeVaultPath(ensureNoteExtension(settings.folder ? `${settings.folder}/${title}` : title));
  return { path: rel, title: title.split("/").pop()!, date, exists: await v.isFile(rel) };
}

/** Legt die Daily Note an (inkl. Template), falls sie fehlt. Gibt Inhalt und Warnungen zurück. */
export async function ensureDailyNote(
  v: Vault,
  ref: DailyNoteRef,
  locale: string,
): Promise<{ content: string; created: boolean; warnings: string[] }> {
  if (ref.exists) return { content: await v.readFile(ref.path), created: false, warnings: [] };
  v.assertWritable();
  const settings = await v.dailyNotesSettings();
  const warnings: string[] = [];
  let content = "";
  if (settings.template) {
    try {
      const tplPath = await v.resolveExisting(settings.template);
      content = renderTemplate(await v.readFile(tplPath), { date: ref.date, title: ref.title, dateFormat: settings.format, locale });
    } catch (e) {
      warnings.push(`Template "${settings.template}" konnte nicht geladen werden: ${(e as Error).message}`);
    }
  }
  await v.writeFile(ref.path, content, { overwrite: false });
  return { content, created: true, warnings };
}

export function registerDailyReadTools(server: McpServer, registry: VaultRegistry): void {
  const vault = vaultParam(registry);
  server.registerTool(
    "get_daily_note",
    {
      title: "Daily Note lesen",
      description:
        "Liest die Daily Note für ein Datum. Ordner, Dateinamensformat und Template kommen aus den Obsidian-Einstellungen " +
        "(Core-Plugin \"Tägliche Notizen\" bzw. Periodic Notes). date: today/heute, yesterday/gestern, tomorrow/morgen, +N/-N oder YYYY-MM-DD. " +
        "create=true legt sie (mit Template) an, falls sie fehlt.",
      inputSchema: {
        vault,
        date: z.string().default("today"),
        create: z.boolean().default(false),
      },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, date, create }) => {
      const v = registry.get(vName);
      const ref = await dailyNoteRef(v, date, registry.locale);
      if (!ref.exists && !create) {
        return json({ path: ref.path, exists: false, hint: "Noch nicht vorhanden – mit create=true oder append_to_daily_note anlegen." });
      }
      const { content, created, warnings } = await ensureDailyNote(v, ref, registry.locale);
      return {
        content: [
          { type: "text", text: JSON.stringify({ path: ref.path, exists: true, created, ...(warnings.length ? { warnings } : {}) }) },
          { type: "text", text: content },
        ],
      };
    }),
  );
}

export function registerDailyWriteTools(server: McpServer, registry: VaultRegistry): void {
  const vault = vaultParam(registry);
  server.registerTool(
    "append_to_daily_note",
    {
      title: "In Daily Note schreiben",
      description:
        "Hängt Text an die Daily Note an (legt sie bei Bedarf mit Template an). Optional unter einer Überschrift (wird erstellt, falls sie fehlt) " +
        "und mit Uhrzeit-Präfix – ideal für schnelles Journaling/Logging.",
      inputSchema: {
        vault,
        content: z.string().min(1),
        date: z.string().default("today"),
        heading: z.string().optional().describe("z. B. \"Log\" oder \"Journal\""),
        timestamp: z.boolean().default(false).describe("Aktuelle Uhrzeit (HH:mm) voranstellen"),
      },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
    },
    safe(async ({ vault: vName, content, date, heading, timestamp }) => {
      const v = registry.get(vName);
      v.assertWritable();
      const ref = await dailyNoteRef(v, date, registry.locale);
      const { content: existing, created, warnings } = await ensureDailyNote(v, ref, registry.locale);
      let addition = content;
      if (timestamp) {
        const hhmm = formatDate(new Date(), "HH:mm", registry.locale);
        const m = /^(\s*(?:[-*+]|\d+\.)\s+(?:\[.\]\s+)?)/.exec(addition);
        addition = m ? `${m[1]}${hhmm} ${addition.slice(m[1]!.length)}` : `${hhmm} ${addition}`;
      }
      const updated = insertContent(existing, addition, { heading, position: "end", createHeading: true });
      if (updated === existing) throw new VaultError("Nichts zu schreiben", "INVALID");
      await v.writeFile(ref.path, updated, { overwrite: true });
      return json({ path: ref.path, created, ...(warnings.length ? { warnings } : {}) });
    }),
  );
}
