import { useState, type ReactNode, type FormEvent } from 'react';
import { authClient } from './auth';

export function AuthShell({ children }: { children: ReactNode }) {
  const { data: session, isPending } = authClient.useSession();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function login(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('');
    try {
      const result = await authClient.signIn.email({ email, password });
      if (result.error) setError('Anmeldung fehlgeschlagen.');
      else { setPassword(''); window.location.reload(); }
    } catch { setError('Anmeldung derzeit nicht erreichbar.'); }
    finally { setBusy(false); }
  }
  if (isPending) return <p className="p-8" role="status">Lade…</p>;
  if (!session) return <main className="p-8 max-w-sm mx-auto space-y-4">
    <h1 className="text-2xl font-bold">Anmelden</h1>
    <form onSubmit={login} className="space-y-4">
      <label className="block">E-Mail<input className="border p-2 w-full" type="email" autoComplete="username" value={email} onChange={e => setEmail(e.target.value)} required /></label>
      <label className="block">Passwort<input className="border p-2 w-full" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></label>
      <button className="bg-blue-600 text-white p-2 rounded" disabled={busy}>{busy ? 'Anmelden…' : 'Anmelden'}</button>
      {error && <p role="alert">{error}</p>}
    </form>
  </main>;
  return <><header className="p-4 flex justify-between"><span>{session.user.email}</span><button onClick={async () => { await authClient.signOut(); window.location.reload(); }}>Abmelden</button></header>{children}</>;
}
