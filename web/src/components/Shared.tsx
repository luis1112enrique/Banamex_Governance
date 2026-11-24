import { AlertCircle, CalendarDays, Check, LogOut, ShieldCheck } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Session } from '../services/api';

export function Brand() {
  return <div className="brand"><img className="brand-logo" src="/assets/banamex-logo.svg" alt="Banamex" width="161" height="53" /><span className="brand-divider" /><span className="brand-project">Data Steward <strong>AI</strong></span></div>;
}

export function PrototypeBadge() {
  return <span className="prototype-badge"><ShieldCheck size={15} />Prototipo de innovación — entorno de pruebas</span>;
}

export function Header({ session, children, onLogout }: { session?: Session; children?: ReactNode; onLogout?: () => void }) {
  const name = session?.display_name || session?.identity || '';
  const initials = name.split(/[ ._-]+/).filter(Boolean).slice(0, 2).map(part => part[0]).join('').toUpperCase();
  return <>
    <header className="site-header"><div className="header-inner"><Brand />
      {session ? <div className="header-user"><span className="header-greeting">{name}</span><span className="avatar">{initials}</span><button className="icon-button logout-button" title="Cerrar sesión" aria-label="Cerrar sesión" onClick={onLogout}><LogOut size={19} /></button></div> : <PrototypeBadge />}
    </div></header>
    {children}
    {session && <div className="environment-row"><div className="environment-inner"><PrototypeBadge /><span>Fuentes autorizadas · Sin operaciones bancarias</span></div></div>}
  </>;
}

export function Cutoff({ value }: { value?: string | null }) {
  return <span className="cutoff"><CalendarDays size={15} />{value ? `Corte: ${date(value)}` : 'Fecha de corte no disponible'}</span>;
}

export function date(value: string) {
  const result = new Date(`${value.slice(0, 10)}T12:00:00`);
  return Number.isNaN(result.getTime()) ? value : result.toLocaleDateString('es-MX', { day: 'numeric', month: 'short', year: 'numeric' });
}

export function money(value: number | null | undefined, currency?: string | null, hidden = false): string {
  if (hidden) return '••••••';
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return 'Información no disponible';
  return new Intl.NumberFormat('es-MX', currency ? { style: 'currency', currency, minimumFractionDigits: 2 } : { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value));
}

export function EmptyState({ title = 'Información no disponible', description }: { title?: string; description?: string }) {
  return <div className="empty-state"><span className="empty-icon"><AlertCircle size={22} /></span><strong>{title}</strong>{description && <p>{description}</p>}</div>;
}

export function ErrorNotice({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return <div className="error-notice" role="alert"><AlertCircle size={20} /><div><strong>No pudimos cargar la información</strong><p>{message}</p></div>{onRetry && <button className="text-button" onClick={onRetry}>Reintentar</button>}</div>;
}

export function LoadingState() {
  return <div className="loading-state" role="status"><span className="spinner" /><span>Consultando información autorizada…</span></div>;
}

export function SectionTitle({ eyebrow, title, action, onAction }: { eyebrow?: string; title: string; action?: string; onAction?: () => void }) {
  return <div className="section-heading"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2>{title}</h2></div>{action && <button className="text-button" onClick={onAction}>{action}</button>}</div>;
}

export function VerifiedPill() {
  return <span className="verified-pill"><Check size={14} />Sesión autorizada</span>;
}
