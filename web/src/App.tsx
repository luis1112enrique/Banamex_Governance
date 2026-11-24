import { useEffect, useState } from 'react';
import { BarChart3, CreditCard, Home, LayoutDashboard, List, MessageSquareText, ShieldCheck, Wallet } from 'lucide-react';
import { api, type Session } from './services/api';
import { Header, LoadingState } from './components/Shared';
import AssistantPanel from './components/AssistantPanel';
import LoginPage from './pages/LoginPage';
import BankingDashboard, { type ClientView } from './pages/BankingDashboard';
import InstitutionalDashboard, { type InstitutionView } from './pages/InstitutionalDashboard';

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [booting, setBooting] = useState(true);
  const [clientView, setClientView] = useState<ClientView>('home');
  const [institutionView, setInstitutionView] = useState<InstitutionView>('home');
  const [assistantOpen, setAssistantOpen] = useState(() => window.location.pathname === '/assistant');
  const [question, setQuestion] = useState<{ id: number; text: string } | null>(null);
  const [logoutError, setLogoutError] = useState('');
  function navigateAssistant(open: boolean, replace = false) {
    const path = open ? '/assistant' : '/';
    if (window.location.pathname !== path) {
      window.history[replace ? 'replaceState' : 'pushState'](null, '', path);
    }
    setAssistantOpen(open);
  }
  useEffect(() => {
    api.session().then(setSession).catch(() => setSession(null)).finally(() => setBooting(false));
    const expire = () => { setSession(null); navigateAssistant(false, true); setQuestion(null); };
    const navigate = () => setAssistantOpen(window.location.pathname === '/assistant');
    window.addEventListener('banamex:session-expired', expire);
    window.addEventListener('popstate', navigate);
    return () => {
      window.removeEventListener('banamex:session-expired', expire);
      window.removeEventListener('popstate', navigate);
    };
  }, []);
  function login(value: Session) { setSession(value); setClientView('home'); setInstitutionView('home'); navigateAssistant(false, true); setQuestion(null); setLogoutError(''); }
  async function logout() {
    try { await api.logout(); setSession(null); navigateAssistant(false, true); setQuestion(null); }
    catch (reason) { setLogoutError(reason instanceof Error ? reason.message : 'No fue posible cerrar sesión.'); }
  }
  function ask(text?: string) { navigateAssistant(true); if (text) setQuestion({ id: Date.now(), text }); }
  useEffect(() => {
    const main = document.querySelector<HTMLElement>('.dashboard-route > .bank-main');
    if (main) { main.scrollTop = 0; main.focus({ preventScroll: true }); }
  }, [clientView, institutionView, assistantOpen]);
  if (booting) return <><Header /><main className="boot-screen"><LoadingState /></main></>;
  if (!session) return <LoginPage onLogin={login} />;
  const customer = session.profile === 'cliente';
  const clientNav = [{ id: 'home', label: 'Inicio', icon: Home }, { id: 'products', label: 'Mis productos', icon: CreditCard }, { id: 'transactions', label: 'Movimientos', icon: List }, { id: 'finances', label: 'Mis finanzas', icon: Wallet }] as const;
  const institutionNav = [{ id: 'home', label: 'Resumen', icon: LayoutDashboard }, { id: 'analysis', label: 'Análisis financiero', icon: BarChart3 }, { id: 'governance', label: 'Gobernanza', icon: ShieldCheck }] as const;
  return <div className={`banking-app ${assistantOpen ? 'assistant-route' : 'dashboard-route'}`}>{!assistantOpen && <Header session={session} onLogout={logout}>
    <nav className="main-nav" aria-label={customer ? 'Banca personal' : 'Portal institucional'}><div className="main-nav-inner">{customer ? clientNav.map(item => <button key={item.id} className={clientView === item.id ? 'active' : ''} aria-current={clientView === item.id ? 'page' : undefined} onClick={() => setClientView(item.id)}><item.icon size={17} />{item.label}</button>) : institutionNav.map(item => <button key={item.id} className={institutionView === item.id ? 'active' : ''} aria-current={institutionView === item.id ? 'page' : undefined} onClick={() => setInstitutionView(item.id)}><item.icon size={17} />{item.label}</button>)}<button className="nav-assistant" onClick={() => ask()}><MessageSquareText size={17} />Asistente Banamex AI</button></div></nav>
  </Header>}
    {!assistantOpen && logoutError && <div className="logout-error" role="alert">{logoutError}</div>}
    {!assistantOpen && (customer ? <BankingDashboard session={session} view={clientView} setView={setClientView} onAsk={ask} /> : <InstitutionalDashboard session={session} view={institutionView} onAsk={ask} />)}
    <AssistantPanel key={session.csrf_token} session={session} open={assistantOpen} onOpen={() => ask()} onClose={() => navigateAssistant(false)} requestedQuestion={question} />
  </div>;
}
