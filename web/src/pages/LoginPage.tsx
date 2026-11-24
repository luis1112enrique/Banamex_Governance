import { useState, type FormEvent } from 'react';
import { Eye, EyeOff, ShieldCheck } from 'lucide-react';
import { api, type Portal, type Session } from '../services/api';
import '../styles/login.css';

export default function LoginPage({ onLogin }: { onLogin: (session: Session) => void }) {
  const [portal, setPortal] = useState<Portal>('cliente');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const institutional = portal === 'institucional';
  async function login(event: FormEvent) {
    event.preventDefault();
    setError(''); setLoading(true);
    try { onLogin(await api.login(username.trim(), password, portal)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'No fue posible iniciar sesión.'); }
    finally { setPassword(''); setLoading(false); }
  }
  function changePortal(value: Portal) { setPortal(value); setUsername(''); setPassword(''); setError(''); }
  return <div className="login-redesign">
    <header className="access-header"><span className="access-project">Data Steward <strong>AI</strong></span><span className="access-prototype">Prototipo de innovación — Entorno de pruebas</span></header>
    <main className="access-layout">
      <section className="access-form-area" aria-labelledby="login-heading">
        <div className="access-form-content">
          <p className="access-eyebrow">{institutional ? 'PORTAL INSTITUCIONAL' : 'TU BANCA PERSONAL'}</p>
          <h1 id="login-heading">Bienvenido<br />a tu banca</h1>
          <p className="access-intro">{institutional ? 'Un espacio para la gobernanza y el análisis. Ingresa con tu usuario Data Steward o Auditor de pruebas.' : 'Tu información financiera, con una nueva perspectiva. Ingresa con tu cuenta de pruebas.'}</p>
          <div className="access-portal-switch" role="group" aria-label="Selecciona tu portal" data-portal={portal}>
            <button type="button" className={!institutional ? 'active' : ''} aria-pressed={!institutional} disabled={loading} onClick={() => changePortal('cliente')}>Portal Cliente</button>
            <button type="button" className={institutional ? 'active' : ''} aria-pressed={institutional} disabled={loading} onClick={() => changePortal('institucional')}>Portal Institucional</button>
          </div>
          <form onSubmit={login} className="access-form" aria-busy={loading}>
            <label htmlFor="username">{institutional ? 'Usuario institucional' : 'Usuario de cliente'}</label>
            <input id="username" name="username" autoComplete="username" value={username} disabled={loading} onChange={event => setUsername(event.target.value)} placeholder="Tu usuario de pruebas" required maxLength={128} />
            <label htmlFor="password">Contraseña de pruebas</label>
            <div className="access-password-field"><input id="password" name="password" type={visible ? 'text' : 'password'} autoComplete="current-password" value={password} disabled={loading} onChange={event => setPassword(event.target.value)} placeholder="Ingresa tu contraseña" required maxLength={1024} /><button type="button" aria-label={visible ? 'Ocultar contraseña' : 'Mostrar contraseña'} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={20} /> : <Eye size={20} />}</button></div>
            {error && <p className="access-error" role="alert">{error}</p>}
            <button className="access-submit" disabled={loading || !username.trim() || !password}>{loading ? <><span className="spinner light" />Verificando acceso…</> : 'Ingresar'}</button>
          </form>
          <div className="access-safety"><ShieldCheck size={18} aria-hidden="true" /><p>Prototipo independiente. <strong>No ingreses credenciales bancarias reales.</strong> Utiliza únicamente las cuentas de pruebas asignadas.</p></div>
        </div>
      </section>
      <div className="access-symbol" aria-hidden="true"><span className="access-emblem"><img className="access-emblem-art" src="/assets/banamex-isotipo-depth.svg" alt="" width="950" height="955" /></span></div>
    </main>
  </div>;
}
