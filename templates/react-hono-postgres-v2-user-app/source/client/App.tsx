import React, { useEffect, useState } from 'react';
import { authClient } from './auth';

type Entry = {
  id: number;
  content: string;
  createdAt: string;
};

export default function App() {
  const { data: session, isPending } = authClient.useSession();
  const [entries, setEntries] = useState<Entry[]>([]);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loginError, setLoginError] = useState('');

  useEffect(() => {
    if (session) {
      fetch('/api/entries')
        .then((res) => res.json())
        .then((data) => {
          if (data.entries) setEntries(data.entries);
        })
        .finally(() => setLoading(false));
    }
  }, [session]);

  if (isPending) return <div className="p-8">Lade...</div>;

  if (!session) {
    const handleLogin = async (e: React.FormEvent) => {
      e.preventDefault();
      setLoginError('');
      const { error } = await authClient.signIn.email({ email, password });
      if (error) {
        setLoginError(error.message || 'Login fehlgeschlagen');
      } else {
        window.location.reload();
      }
    };

    const handleGoogleLogin = async () => {
      const { error } = await authClient.signIn.social({ provider: 'google', callbackURL: '/' });
      if (error) setLoginError(error.message || 'Google Login fehlgeschlagen');
    };

    return (
      <div className="p-8 max-w-sm mx-auto space-y-6">
        <h1 className="text-2xl font-bold text-center">Login</h1>
        <form onSubmit={handleLogin} className="space-y-4">
          <input type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="Email" className="w-full border p-2 rounded" required />
          <input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Password" className="w-full border p-2 rounded" required />
          <button type="submit" className="w-full bg-blue-500 text-white p-2 rounded">Login</button>
        </form>
        {loginError && <p className="text-red-500 text-sm text-center">{loginError}</p>}
        <div className="relative flex py-5 items-center">
          <div className="flex-grow border-t border-gray-400"></div>
          <span className="flex-shrink-0 mx-4 text-gray-400">oder</span>
          <div className="flex-grow border-t border-gray-400"></div>
        </div>
        <button onClick={handleGoogleLogin} className="w-full bg-red-500 text-white p-2 rounded">
          Mit Google anmelden
        </button>
      </div>
    );
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!content.trim()) return;
    const res = await fetch('/api/entries', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    if (res.ok) {
      const data = await res.json();
      setEntries([data.entry, ...entries]);
      setContent('');
    }
  };

  const handleLinkGoogle = async () => {
    await authClient.linkSocial({ provider: 'google', callbackURL: '/' });
  };

  const handleLogout = async () => {
    await authClient.signOut();
    window.location.reload();
  };

  return (
    <div className="p-8 max-w-xl mx-auto space-y-6">
      <div className="flex justify-between items-center bg-gray-100 p-4 rounded">
        <div>
          <p className="font-bold">{session.user.name}</p>
          <p className="text-sm text-gray-600">{session.user.email}</p>
        </div>
        <div className="space-x-2">
          <button onClick={handleLinkGoogle} className="bg-red-500 text-white px-3 py-1 rounded text-sm">
            Google verknüpfen
          </button>
          <button onClick={handleLogout} className="bg-gray-500 text-white px-3 py-1 rounded text-sm">
            Logout
          </button>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          type="text"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="New entry..."
          className="border p-2 flex-1 rounded"
        />
        <button type="submit" className="bg-blue-500 text-white px-4 py-2 rounded">
          Add
        </button>
      </form>
      
      {loading ? (
        <p>Loading...</p>
      ) : (
        <ul className="space-y-2">
          {entries.map((entry) => (
            <li key={entry.id} className="border p-3 rounded">
              <p>{entry.content}</p>
              <span className="text-xs text-gray-500">
                {new Date(entry.createdAt).toLocaleString()}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
