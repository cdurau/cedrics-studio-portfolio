import { NextResponse } from 'next/server';
import { db } from '@/lib/db';
import { entries } from '@/lib/schema';
import { desc } from 'drizzle-orm';

export async function GET() {
  const allEntries = await db.query.entries.findMany({
    orderBy: [desc(entries.createdAt)],
  });
  return NextResponse.json(allEntries);
}

export async function POST(req: Request) {
  const body = await req.json();
  if (!body.content) return NextResponse.json({ error: 'Missing content' }, { status: 400 });
  await db.insert(entries).values({ content: body.content });
  return NextResponse.json({ ok: true });
}
