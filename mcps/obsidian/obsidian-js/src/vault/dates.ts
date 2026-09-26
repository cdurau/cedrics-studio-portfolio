/**
 * Minimaler, abhängigkeitsfreier Formatter für die Moment.js-Formate,
 * die Obsidian für Daily Notes nutzt (z. B. "YYYY-MM-DD", "YYYY/MM/DD dddd", "gggg-[W]ww").
 */

const TOKEN_RE =
  /\[([^\]]*)\]|YYYY|YY|Q|MMMM|MMM|MM|M|DDDD|DDD|Do|DD|D|dddd|ddd|dd|d|E|e|GGGG|gggg|WW|W|ww|w|HH|H|hh|h|mm|m|ss|s|A|a|X|x/g;

function pad(n: number, len = 2): string {
  return String(n).padStart(len, "0");
}

function isoWeekInfo(date: Date): { week: number; year: number } {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const day = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((d.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
  return { week, year: d.getUTCFullYear() };
}

function dayOfYear(date: Date): number {
  const start = new Date(date.getFullYear(), 0, 0);
  const diff = date.getTime() - start.getTime() + (start.getTimezoneOffset() - date.getTimezoneOffset()) * 60000;
  return Math.floor(diff / 86400000);
}

function ordinal(n: number, locale: string): string {
  if (locale.toLowerCase().startsWith("de")) return `${n}.`;
  const s = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return n + (s[(v - 20) % 10] ?? s[v] ?? s[0]!);
}

export function formatDate(date: Date, format: string, locale = "de"): string {
  const name = (opts: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat(locale, opts).format(date);
  // Lokalisierte Wochen richten sich hier nach ISO (Mo–So), wie es in DE üblich ist.
  const iso = isoWeekInfo(date);
  return format.replace(TOKEN_RE, (tok, literal: string | undefined) => {
    if (literal !== undefined) return literal;
    switch (tok) {
      case "YYYY":
        return String(date.getFullYear()).padStart(4, "0");
      case "YY":
        return pad(date.getFullYear() % 100);
      case "Q":
        return String(Math.floor(date.getMonth() / 3) + 1);
      case "MMMM":
        return name({ month: "long" });
      case "MMM":
        return name({ month: "short" }).replace(/\.$/, "");
      case "MM":
        return pad(date.getMonth() + 1);
      case "M":
        return String(date.getMonth() + 1);
      case "DDDD":
        return pad(dayOfYear(date), 3);
      case "DDD":
        return String(dayOfYear(date));
      case "Do":
        return ordinal(date.getDate(), locale);
      case "DD":
        return pad(date.getDate());
      case "D":
        return String(date.getDate());
      case "dddd":
        return name({ weekday: "long" });
      case "ddd":
        return name({ weekday: "short" }).replace(/\.$/, "");
      case "dd":
        return name({ weekday: "short" }).replace(/\.$/, "").slice(0, 2);
      case "d":
        return String(date.getDay());
      case "E":
      case "e":
        return String(date.getDay() || 7);
      case "GGGG":
      case "gggg":
        return String(iso.year);
      case "WW":
      case "ww":
        return pad(iso.week);
      case "W":
      case "w":
        return String(iso.week);
      case "HH":
        return pad(date.getHours());
      case "H":
        return String(date.getHours());
      case "hh":
        return pad(date.getHours() % 12 || 12);
      case "h":
        return String(date.getHours() % 12 || 12);
      case "mm":
        return pad(date.getMinutes());
      case "m":
        return String(date.getMinutes());
      case "ss":
        return pad(date.getSeconds());
      case "s":
        return String(date.getSeconds());
      case "A":
        return date.getHours() < 12 ? "AM" : "PM";
      case "a":
        return date.getHours() < 12 ? "am" : "pm";
      case "X":
        return String(Math.floor(date.getTime() / 1000));
      case "x":
        return String(date.getTime());
      default:
        return tok;
    }
  });
}

/**
 * Wandelt Eingaben wie "today", "heute", "yesterday", "gestern", "tomorrow", "morgen",
 * "+2", "-7" oder "2026-09-26" in ein lokales Datum (00:00 Uhr) um.
 */
export function parseDateInput(input: string | undefined, now = new Date()): Date {
  const base = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const v = (input ?? "today").trim().toLowerCase();
  const offset = (days: number) => new Date(base.getFullYear(), base.getMonth(), base.getDate() + days);
  if (v === "" || v === "today" || v === "heute") return base;
  if (v === "yesterday" || v === "gestern") return offset(-1);
  if (v === "tomorrow" || v === "morgen") return offset(1);
  const rel = /^([+-]\d{1,4})d?$/.exec(v);
  if (rel) return offset(Number(rel[1]));
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
  if (iso) {
    const d = new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]));
    if (d.getMonth() !== Number(iso[2]) - 1) throw new Error(`Ungültiges Datum: ${input}`);
    return d;
  }
  throw new Error(`Datum nicht erkannt: "${input}". Erlaubt: today/heute, yesterday/gestern, tomorrow/morgen, +N/-N oder YYYY-MM-DD.`);
}

/** Ersetzt Obsidian-Template-Variablen {{date}}, {{time}}, {{title}}, {{date:FORMAT}}, {{time:FORMAT}}. */
export function renderTemplate(
  template: string,
  opts: { date: Date; title: string; dateFormat: string; locale: string; now?: Date },
): string {
  const now = opts.now ?? new Date();
  // Datum der Notiz, aber mit aktueller Uhrzeit (wie Obsidian)
  const dt = new Date(opts.date.getFullYear(), opts.date.getMonth(), opts.date.getDate(), now.getHours(), now.getMinutes(), now.getSeconds());
  return template.replace(/\{\{\s*(date|time|title)\s*(?::\s*([^}]*?))?\s*\}\}/gi, (_m, kind: string, fmt: string | undefined) => {
    switch (kind.toLowerCase()) {
      case "title":
        return opts.title;
      case "date":
        return formatDate(dt, fmt || opts.dateFormat, opts.locale);
      case "time":
        return formatDate(dt, fmt || "HH:mm", opts.locale);
      default:
        return _m;
    }
  });
}
