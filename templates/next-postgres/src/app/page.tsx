import { db } from '@/lib/db';
import { entries } from '@/lib/schema';
import { addEntry, getUserFromHeader } from './actions';
import { desc } from 'drizzle-orm';

export const dynamic = 'force-dynamic';

export default async function Home() {
  const allEntries = await db.query.entries.findMany({
    orderBy: [desc(entries.createdAt)],
  });

  const user = await getUserFromHeader();

  return (
    <main className="max-w-2xl mx-auto p-6 bg-white shadow rounded mt-10">
      <h1 className="text-2xl font-bold mb-2">Next.js + Postgres App</h1>
      <p className="mb-6 text-gray-600">Logged in as: <strong>{user}</strong></p>

      <form action={addEntry} className="mb-8 flex gap-2">
        <input
          type="text"
          name="content"
          placeholder="Enter something..."
          className="border rounded p-2 flex-grow"
          required
        />
        <button type="submit" className="bg-blue-600 text-white px-4 py-2 rounded">
          Save
        </button>
      </form>

      <div>
        <h2 className="text-lg font-semibold mb-3">Saved Entries:</h2>
        {allEntries.length === 0 ? (
          <p className="text-gray-500 italic">No entries found.</p>
        ) : (
          <ul className="space-y-2">
            {allEntries.map((entry) => (
              <li key={entry.id} className="p-3 border rounded">
                <p>{entry.content}</p>
                <small className="text-gray-400">{new Date(entry.createdAt).toLocaleString()}</small>
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
