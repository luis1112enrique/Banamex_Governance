import { useEffect, useRef, useState } from 'react';
import { AlertCircle, CalendarDays, ChartColumn, CircleDollarSign, CreditCard, FileText, LayoutDashboard, MessageSquareText, Paperclip, PanelLeftClose, PanelLeftOpen, Plus, ReceiptText, Send, ShieldCheck, Sparkles, TrendingUp, Users, Wallet, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, type Session } from '../services/api';
import { Brand, date } from './Shared';
import '../styles/assistant.css';

interface Message { id: string; role: 'user' | 'assistant'; content: string; chartId?: string | null; error?: string; attachments?: { name: string; size: number }[]; }
interface RequestedQuestion { id: number; text: string; }
interface Conversation { id: string; title: string; messages: Message[]; cutoff: string | null; }

const MAX_FILES = 5;
const MAX_FILE_BYTES = 10 * 1024 * 1024;
const MAX_TOTAL_BYTES = 20 * 1024 * 1024;
function fileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  const unit = bytes >= 1024 * 1024 ? 'MiB' : 'KiB';
  return `${(bytes / (unit === 'MiB' ? 1024 * 1024 : 1024)).toLocaleString('es-MX', { maximumFractionDigits: 2 })} ${unit}`;
}

const CLIENT_QUESTIONS = [
  { text: '¿Cuál es el saldo de mis cuentas?', icon: Wallet, caption: 'Tus cuentas' },
  { text: '¿Cómo han evolucionado mis ahorros?', icon: TrendingUp, caption: 'Tu evolución' },
  { text: '¿Cuáles son mis principales gastos?', icon: ReceiptText, caption: 'Tus movimientos' },
  { text: '¿Qué productos tengo contratados?', icon: CreditCard, caption: 'Tus productos' },
  { text: '¿Qué comisiones tiene mi cuenta?', icon: CircleDollarSign, caption: 'Tus condiciones' },
  { text: 'Genera una gráfica de la evolución de mi saldo.', icon: ChartColumn, caption: 'Tus gráficas' },
];
const INSTITUTION_QUESTIONS = [
  { text: '¿Cuántos clientes tenemos registrados al cierre de junio de 2026?', icon: Users, caption: 'Indicadores' },
  { text: 'Genera una gráfica comparativa de captación por producto.', icon: ChartColumn, caption: 'Análisis financiero' },
  { text: '¿Cuáles son las condiciones de exención de comisiones para Cuenta Priority?', icon: FileText, caption: 'Consulta documental' },
];

function ChartImage({ chartId, onLoad }: { chartId: string; onLoad?: () => void }) {
  const [failed, setFailed] = useState(false);
  return failed ? <p className="ai-inline-error"><AlertCircle size={17} />La gráfica no está disponible para esta sesión.</p> : <figure className="ai-chart"><a href={api.chartUrl(chartId)} target="_blank" rel="noreferrer" aria-label="Abrir gráfica de la conversación"><img src={api.chartUrl(chartId)} alt="Gráfica generada por el asistente para esta conversación" onLoad={onLoad} onError={() => setFailed(true)} /></a><figcaption>Visualización generada para esta conversación</figcaption></figure>;
}

export default function AssistantPanel({ session, open, onOpen, onClose, requestedQuestion }: { session: Session; open: boolean; onOpen: () => void; onClose: () => void; requestedQuestion?: RequestedQuestion | null }) {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [attachmentError, setAttachmentError] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const conversation = useRef<string | null>(null);
  const lastRequested = useRef<number | null>(null);
  const busyRef = useRef(false);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const retrySubmission = useRef<{ conversationId: string; text: string; files: File[] } | null>(null);
  const end = useRef<HTMLDivElement | null>(null);
  const mounted = useRef(true);
  const customer = session.profile === 'cliente';
  const canUse = session.permissions.includes('assistant:use');
  const suggestions = customer ? CLIENT_QUESTIONS : INSTITUTION_QUESTIONS;
  const active = conversations.find(item => item.id === activeId);
  const messages = active?.messages ?? [];
  const name = session.display_name || session.identity;
  const initials = name.split(/[ ._-]+/).filter(Boolean).slice(0, 2).map(part => part[0]).join('').toUpperCase();
  const profile = customer ? 'Cliente' : session.profile === 'auditor' ? 'Auditor' : 'Data Steward';

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  function scrollToLatest() {
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    end.current?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'nearest' });
  }
  useEffect(() => { if (open) scrollToLatest(); }, [messages.length, busy, open, activeId]);
  function pending(value: boolean) { busyRef.current = value; setBusy(value); }
  function clearAttachments() { setPendingFiles([]); setAttachmentError(''); if (fileInput.current) fileInput.current.value = ''; }
  function selectAttachments(selected: FileList | null) {
    if (!canUse || busyRef.current || !selected) return;
    const files = [...pendingFiles];
    for (const file of Array.from(selected)) {
      if (!files.some(item => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) files.push(file);
    }
    if (fileInput.current) fileInput.current.value = '';
    if (files.length > MAX_FILES) { setAttachmentError('Puedes adjuntar como máximo 5 archivos.'); return; }
    const invalid = files.find(file => !/\.(pdf|png|jpe?g)$/i.test(file.name));
    if (invalid) { setAttachmentError(`Formato no admitido: ${invalid.name}. Usa PDF, PNG, JPG o JPEG.`); return; }
    const empty = files.find(file => file.size === 0);
    if (empty) { setAttachmentError(`El archivo ${empty.name} está vacío.`); return; }
    const oversized = files.find(file => file.size > MAX_FILE_BYTES);
    if (oversized) { setAttachmentError(`${oversized.name} supera el límite de 10 MiB por archivo.`); return; }
    if (files.reduce((total, file) => total + file.size, 0) > MAX_TOTAL_BYTES) { setAttachmentError('Los archivos superan el límite total de 20 MiB.'); return; }
    setPendingFiles(files); setAttachmentError('');
  }
  function append(id: string, message: Message) { setConversations(current => current.map(item => item.id === id ? { ...item, messages: [...item.messages, message] } : item)); }
  async function createConversation(title = 'Conversación nueva') {
    const result = await api.conversation();
    if (mounted.current) {
      conversation.current = result.conversation_id;
      setActiveId(result.conversation_id);
      setConversations(current => [{ id: result.conversation_id, title, messages: [], cutoff: null }, ...current]);
    }
    return result.conversation_id;
  }
  async function send(question: string, files: File[] = []) {
    const text = question.trim();
    if (!canUse || (!text && !files.length) || busyRef.current) return;
    setDraft(''); setError(''); pending(true);
    let id = conversation.current;
    try {
      const title = text || `Documentos: ${files[0].name}`;
      if (!id) id = await createConversation(title);
      if (!mounted.current) return;
      const target = id;
      setConversations(current => current.map(item => item.id === target && !item.messages.length ? { ...item, title } : item));
      const previous = retrySubmission.current;
      const retry = previous?.conversationId === target && previous.text === text && previous.files.length === files.length && previous.files.every((file, index) => file === files[index]);
      if (!retry) append(target, { id: crypto.randomUUID(), role: 'user', content: text || 'Analiza los documentos adjuntos.', attachments: files.map(file => ({ name: file.name, size: file.size })) });
      retrySubmission.current = null;
      const response = await api.chat(target, text, files);
      if (mounted.current) {
        append(target, { id: crypto.randomUUID(), role: 'assistant', content: response.final_answer, chartId: response.chart_id });
        if (response.cutoff_date) setConversations(current => current.map(item => item.id === target ? { ...item, cutoff: response.cutoff_date! } : item));
        if (files.length) clearAttachments();
      }
    } catch (reason) {
      if (mounted.current) {
        const message = reason instanceof Error ? reason.message : 'No fue posible completar la consulta.';
        setDraft(text);
        if (id) { retrySubmission.current = { conversationId: id, text, files }; append(id, { id: crypto.randomUUID(), role: 'assistant', content: '', error: message }); }
        else setError(message);
      }
    } finally { if (mounted.current) pending(false); }
  }
  useEffect(() => {
    if (canUse && requestedQuestion && requestedQuestion.id !== lastRequested.current && !busy) { lastRequested.current = requestedQuestion.id; void send(requestedQuestion.text); }
  }, [requestedQuestion, busy, canUse]);
  async function newConversation() {
    if (!canUse || busyRef.current) return;
    pending(true); setError('');
    try { await createConversation(); if (mounted.current) { setDraft(''); clearAttachments(); retrySubmission.current = null; } }
    catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : 'No fue posible iniciar una conversación.'); }
    finally { if (mounted.current) pending(false); }
  }
  function switchConversation(id: string) {
    if (busyRef.current || !conversations.some(item => item.id === id)) return;
    conversation.current = id; setActiveId(id); setDraft(''); setError(''); clearAttachments(); retrySubmission.current = null;
  }

  if (!open) return <div className="assistant-dock"><button className="assistant-launcher" onClick={onOpen}><span><MessageSquareText size={20} /></span><span>Asistente Banamex <strong>AI</strong></span></button></div>;
  return <div className="ai-workspace">
    <header className="ai-workspace-header"><div className="ai-header-left"><button className="ai-icon-button" aria-label={sidebarOpen ? 'Contraer barra lateral' : 'Mostrar barra lateral'} aria-expanded={sidebarOpen} aria-controls="ai-sidebar" onClick={() => setSidebarOpen(!sidebarOpen)}>{sidebarOpen ? <PanelLeftClose size={21} /> : <PanelLeftOpen size={21} />}</button><div className="ai-header-brand"><Brand /></div><span className="ai-header-separator" /><div className="ai-header-title"><strong>Asistente Banamex AI</strong><span>Tu información, en perspectiva</span></div></div><div className="ai-header-right"><span className="ai-prototype"><ShieldCheck size={15} />Entorno de pruebas</span><button className="ai-back-button" onClick={onClose}><LayoutDashboard size={17} />Volver a mi banca</button><span className="ai-user-avatar" title={`${name} · ${profile}`}>{initials}</span></div></header>
    <div className={`ai-workspace-body ${sidebarOpen ? '' : 'ai-sidebar-collapsed'}`}>
      <aside className="ai-sidebar" id="ai-sidebar" aria-hidden={!sidebarOpen}><button className="ai-new-conversation" onClick={newConversation} disabled={busy || !canUse}><Plus size={19} />Nueva conversación</button><div className="ai-history-heading">TUS CONVERSACIONES</div><div className="ai-history-list">{conversations.length ? conversations.map(item => <button key={item.id} className={`ai-history-item ${activeId === item.id ? 'is-active' : ''}`} onClick={() => switchConversation(item.id)} disabled={busy} aria-label={item.title} aria-current={activeId === item.id ? 'page' : undefined} data-conversation-id={item.id} title={item.title}><MessageSquareText size={16} /><span>{item.title}</span></button>) : <p className="ai-history-empty">Aquí aparecerán las conversaciones que inicies en esta sesión.</p>}</div><div className="ai-sidebar-bottom"><div className="ai-session-profile"><span className="ai-user-avatar">{initials}</span><div><strong>{name}</strong><span>{profile} · Sesión autorizada</span></div></div><button className="ai-dashboard-link" onClick={onClose}><LayoutDashboard size={17} />Regresar al dashboard</button></div></aside>
      <main className="ai-main" data-conversation-id={activeId ?? undefined}><div className="ai-scroll-area" role={messages.length ? 'log' : undefined} aria-live="polite" aria-relevant="additions"><div className={`ai-reading-area ${messages.length ? 'has-messages' : ''}`}>
        {!canUse && <p className="ai-permission-notice" role="note"><ShieldCheck size={20} />Tu perfil tiene acceso a indicadores institucionales. El asistente no está habilitado para esta cuenta.</p>}
        {!messages.length && <section className="ai-welcome"><span className="ai-welcome-icon"><Sparkles size={31} /></span><p className="ai-welcome-eyebrow">ASISTENTE BANAMEX AI</p><h1>{canUse ? 'Tus finanzas, desde una nueva perspectiva' : 'Indicadores a tu alcance'}</h1><p className="ai-welcome-subtitle">{canUse ? 'Consulta, comprende y analiza tu información financiera' : 'Tu identidad conserva el acceso a los indicadores institucionales autorizados.'}</p><div className="ai-suggestion-grid">{suggestions.map(({ text, icon: Icon, caption }) => <button className="ai-suggestion-card" key={text} disabled={busy || !canUse} onClick={() => send(text)}><span className="ai-suggestion-icon"><Icon size={21} /></span><span className="ai-suggestion-caption">{caption}</span><span className="ai-suggestion-text">{text}</span></button>)}</div></section>}
        {messages.length > 0 && <div className="ai-thread-heading"><span>CONVERSACIÓN</span>{active?.cutoff && <span><CalendarDays size={14} />Datos financieros al {date(active.cutoff)}</span>}</div>}
        {messages.map(message => <article className={`ai-message ai-message-${message.role}`} key={message.id}><div className="ai-message-label">{message.role === 'assistant' ? <span className="ai-message-avatar"><Sparkles size={18} /></span> : <span className="ai-message-user-avatar">{initials}</span>}<strong>{message.role === 'user' ? 'Tú' : 'Asistente Banamex AI'}</strong></div>{message.error ? <p className="ai-inline-error"><AlertCircle size={19} />{message.error}</p> : <div className="ai-message-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{ img: () => null, a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />, code: ({ className, children, node: _node, ...props }) => customer && className?.includes('language-sql') ? null : <code className={className} {...props}>{children}</code> }}>{message.content}</ReactMarkdown></div>}{Boolean(message.attachments?.length) && <ul className="ai-message-attachments" aria-label="Documentos enviados">{message.attachments!.map((file, index) => <li key={`${file.name}-${index}`}><FileText size={16} /><span>{file.name}</span><small>{fileSize(file.size)}</small></li>)}</ul>}{message.chartId && <ChartImage chartId={message.chartId} onLoad={message.id === messages.at(-1)?.id ? scrollToLatest : undefined} />}</article>)}
        {busy && <div className="ai-thinking" role="status"><span className="ai-thinking-dots"><i /><i /><i /></span>Consultando información autorizada…</div>}{error && <p className="ai-inline-error" role="alert"><AlertCircle size={19} />{error}</p>}<div ref={end} />
      </div></div>
      <div className="ai-composer-region">
        <input ref={fileInput} id="assistant-documents" className="ai-file-input" type="file" aria-label="Adjuntar documentos" multiple accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg" disabled={busy || !canUse} onChange={event => selectAttachments(event.target.files)} />
        {pendingFiles.length > 0 && <ul className="ai-pending-attachments" aria-label="Documentos pendientes">{pendingFiles.map((file, index) => <li key={`${file.name}-${file.size}-${file.lastModified}`}><FileText size={17} /><span className="ai-pending-file-name" title={file.name}>{file.name}<small>{fileSize(file.size)}</small></span><button type="button" aria-label={`Quitar ${file.name}`} title="Quitar documento" disabled={busy} onClick={() => { if (!busyRef.current) { setPendingFiles(current => current.filter((_, position) => position !== index)); setAttachmentError(''); } }}><X size={15} /></button></li>)}</ul>}
        {attachmentError && <p className="ai-attachment-error" role="alert"><AlertCircle size={16} />{attachmentError}</p>}
        <form className="ai-composer" onSubmit={event => { event.preventDefault(); void send(draft, pendingFiles); }}>
          <label className="sr-only" htmlFor="assistant-question">Escribe tu pregunta</label>
          <button type="button" className="ai-attach-button" aria-label="Seleccionar documentos" title="Adjuntar documentos" disabled={busy || !canUse} onClick={() => fileInput.current?.click()}><Paperclip size={21} /></button>
          <textarea id="assistant-question" aria-label="Escribe tu pregunta" value={draft} onChange={event => setDraft(event.target.value)} placeholder={canUse ? '¿Qué quieres comprender hoy?' : 'Asistente no habilitado para esta cuenta'} rows={2} maxLength={4000} disabled={busy || !canUse} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(draft, pendingFiles); } }} />
          <button className="ai-send-button" aria-label="Enviar pregunta" disabled={busy || !canUse || (!draft.trim() && !pendingFiles.length)}><Send size={20} /></button>
        </form>
        <p className="ai-upload-limits">PDF, PNG o JPG · Máximo 5 archivos · 10 MiB por archivo · 20 MiB en total</p>
        <p className="ai-composer-note">Prototipo de innovación — Entorno de pruebas. Verifica la información antes de tomar decisiones.</p>
      </div></main>
    </div>
  </div>;
}
