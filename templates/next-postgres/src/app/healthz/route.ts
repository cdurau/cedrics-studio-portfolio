import { NextResponse } from 'next/server';
import { db } from '@/lib/db';
import { sql } from 'drizzle-orm';

export async function GET() {
  try {
    // Check DB connection
    await db.execute(sql`SELECT 1`);
    return NextResponse.json({ ok: true });
  } catch (error) {
    console.error("Health check failed:", error);
    return NextResponse.json({ ok: false }, { status: 503 });
  }
}
