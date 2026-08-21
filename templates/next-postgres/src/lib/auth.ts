import { headers } from 'next/headers';

export interface User {
  username: string;
  email: string;
  name: string;
  uid: string;
  groups: string[];
}

export async function getUser(): Promise<User | null> {
  const headersList = await headers();

  const username = headersList.get('x-authentik-username');
  if (!username) {
    return null;
  }

  const email = headersList.get('x-authentik-email') || '';
  const name = headersList.get('x-authentik-name') || '';
  const uid = headersList.get('x-authentik-uid') || '';
  const groupsStr = headersList.get('x-authentik-groups') || '';

  let groups: string[] = [];
  try {
    if (groupsStr) {
      groups = groupsStr.split('|').map(g => g.trim()).filter(Boolean);
    }
  } catch (e) {
    // Ignore parse errors for groups
  }

  return {
    username,
    email,
    name,
    uid,
    groups,
  };
}
