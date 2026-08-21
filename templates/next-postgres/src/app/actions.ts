'use server';

import { db } from '@/lib/db';
import { entries } from '@/lib/schema';
import { revalidatePath } from 'next/cache';
import { headers } from 'next/headers';

export async function addEntry(formData: FormData) {
  const content = formData.get('content') as string;
  if (!content) return;

  await db.insert(entries).values({ content });
  revalidatePath('/');
}

export async function getUserFromHeader() {
  const headersList = await headers();
  // Using the Authentik trusted header
  return headersList.get('X-authentik-email') || 'Anonymous';
}
