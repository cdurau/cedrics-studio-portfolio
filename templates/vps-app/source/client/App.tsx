import { useEffect, useState, type FormEvent } from 'react';
type Entry = { id: number; content: string; createdAt: string };
export default function App() {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [content, setContent] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    fetch('/api/entries').then(async res => {
      if (!res.ok) throw new Error();
      setEntries((await res.json()).entries);
    }).catch(() => setError('Einträge konnten nicht geladen werden.'));
  }, []);
  async function submit(e: FormEvent) {
    e.preventDefault(); setError('');
    try {
      const res = await fetch('/api/entries', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }) });
      if (!res.ok) throw new Error();
      const data = await res.json(); setEntries(old => [data.entry, ...old]); setContent('');
    } catch { setError('Eintrag konnte nicht gespeichert werden.'); }
  }
  return <main className="p-8 max-w-xl mx-auto space-y-4">
    <h1 className="text-2xl font-bold">Meine Einträge</h1>
    <form onSubmit={submit} className="flex gap-2"><label className="flex-1">Neuer Eintrag<input className="border p-2 w-full" value={content} maxLength={500} required onChange={e => setContent(e.target.value)} /></label><button className="bg-blue-600 text-white p-2 rounded">Speichern</button></form>
    {error && <p role="alert">{error}</p>}
    <ul className="space-y-2">{entries.map(entry => <li className="border p-3 rounded" key={entry.id}>{entry.content}</li>)}</ul>
  </main>;
}
