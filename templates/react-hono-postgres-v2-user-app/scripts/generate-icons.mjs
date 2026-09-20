// Generates the PWA placeholder icons without an image dependency.
//
// Deliberately a *placeholder*: a neutral dark tile with a light rounded
// square. Every app generated from this profile is expected to replace the
// two PNGs with its own artwork — the profile only guarantees that a valid
// installable icon set exists from the first deploy.
//
// Usage: node scripts/generate-icons.mjs
import { deflateSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "source", "client", "public", "icons");

const BG = [15, 23, 42, 255]; // #0f172a
const FG = [56, 189, 248, 255]; // #38bdf8

function crc32(buf) {
  let c = ~0;
  for (let i = 0; i < buf.length; i++) {
    c ^= buf[i];
    for (let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ 0xedb88320 : c >>> 1;
  }
  return ~c >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

function png(size) {
  // Rounded square inset by 1/6 of the canvas, corner radius 1/5 of its side.
  const inset = Math.round(size / 6);
  const side = size - 2 * inset;
  const radius = Math.round(side / 5);

  const raw = Buffer.alloc(size * (size * 4 + 1));
  let p = 0;
  for (let y = 0; y < size; y++) {
    raw[p++] = 0; // filter: none
    for (let x = 0; x < size; x++) {
      const lx = x - inset;
      const ly = y - inset;
      let inside = lx >= 0 && ly >= 0 && lx < side && ly < side;
      if (inside) {
        // knock out the four corners outside the radius
        const cx = lx < radius ? radius : lx >= side - radius ? side - radius - 1 : lx;
        const cy = ly < radius ? radius : ly >= side - radius ? side - radius - 1 : ly;
        if (cx !== lx && cy !== ly) {
          inside = (lx - cx) ** 2 + (ly - cy) ** 2 <= radius ** 2;
        }
      }
      const c = inside ? FG : BG;
      raw[p++] = c[0];
      raw[p++] = c[1];
      raw[p++] = c[2];
      raw[p++] = c[3];
    }
  }

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // colour type RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

mkdirSync(OUT_DIR, { recursive: true });
for (const size of [192, 512]) {
  const file = join(OUT_DIR, `icon-${size}.png`);
  writeFileSync(file, png(size));
  console.log(`wrote ${file}`);
}
