import { useEffect, useState } from 'react';
import { BookOpen, CircleHelp, CreditCard, Eye, EyeOff, Landmark, ListFilter, RefreshCw, Sparkles, Wallet } from 'lucide-react';
import { api, type Account, type Product, type Session, type Summary, type Transactions } from '../services/api';
import { Cutoff, EmptyState, ErrorNotice, LoadingState, SectionTitle, VerifiedPill, date, money } from '../components/Shared';
import HistoryChart from '../components/HistoryChart';

export type ClientView = 'home' | 'products' | 'transactions' | 'finances';

function AccountCard({ account, currency, hidden }: { account: Account; currency: string | null; hidden: boolean }) {
  const Icon = account.category === 'credit' ? CreditCard : account.category === 'savings' ? Landmark : Wallet;
  return <article className={`account-card ${account.category}`}><div className="account-card-top"><span className="account-icon"><Icon size={22} /></span><span className="account-category">{account.category === 'credit' ? 'Crédito' : account.category === 'savings' ? 'Ahorro' : account.category === 'debit' ? 'Débito' : 'Producto'}</span></div><h3>{account.product_name}</h3><p className="account-number">Cuenta {account.masked_number}</p><div className="account-card-bottom"><span>{account.category === 'credit' ? 'Saldo de crédito registrado' : 'Saldo registrado'}</span><strong>{money(account.registered_balance, currency, hidden)}</strong></div></article>;
}

function TransactionTable({ data, currency, hidden, compact = false }: { data: Transactions; currency: string | null; hidden: boolean; compact?: boolean }) {
  const rows = compact ? data.transactions.slice(0, 5) : data.transactions;
  if (!rows.length) return <EmptyState description="No se encontraron movimientos en el periodo disponible." />;
  return <div className="table-scroll"><table className="transaction-table"><thead><tr><th>Concepto</th><th>Producto</th><th>Fecha</th><th className="numeric">Importe registrado</th></tr></thead><tbody>{rows.map((item, index) => <tr key={`${item.id}-${index}`}><td><span className="movement-icon"><BookOpen size={17} /></span><span>{item.description}</span></td><td>{item.product_name}</td><td className="nowrap">{date(item.date)}</td><td className="numeric amount-cell">{money(item.amount, currency, hidden)}</td></tr>)}</tbody></table></div>;
}

export default function BankingDashboard({ session, view, setView, onAsk }: { session: Session; view: ClientView; setView: (view: ClientView) => void; onAsk: (question?: string) => void }) {
  const [data, setData] = useState<{ summary: Summary; accounts: Account[]; products: Product[]; transactions: Transactions } | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [hidden, setHidden] = useState(false);
  const [reload, setReload] = useState(0);
  const [pageLoading, setPageLoading] = useState(false);
  useEffect(() => {
    let live = true;
    setLoading(true); setError('');
    Promise.all([api.summary(), api.accounts(), api.products(), api.transactions()]).then(([summary, accounts, products, transactions]) => {
      if (live) setData({ summary, accounts: accounts.accounts, products: products.products, transactions });
    }).catch(reason => { if (live) setError(reason instanceof Error ? reason.message : 'No fue posible cargar la información.'); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [reload, session.identity]);

  async function changePage(offset: number) {
    setPageLoading(true);
    try { const result = await api.transactions(offset); setData(current => current ? { ...current, transactions: result } : current); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'No fue posible cargar los movimientos.'); }
    finally { setPageLoading(false); }
  }

  const summary = data?.summary;
  const currency = summary?.currency ?? null;
  const name = session.display_name || `Cliente ${session.cliente_id ?? ''}`;
  const pageTitle = view === 'products' ? 'Tus productos, en un solo lugar.' : view === 'transactions' ? 'Cada movimiento, más claro.' : view === 'finances' ? 'Una mirada a tus finanzas.' : `Hola, ${name}.`;
  return <main className="bank-main" tabIndex={-1}>
    <div className="page-heading"><div><p className="eyebrow">{view === 'home' ? 'TU BANCA PERSONAL' : view === 'products' ? 'MIS PRODUCTOS' : view === 'transactions' ? 'MOVIMIENTOS' : 'MIS FINANZAS'}</p><h1>{pageTitle}</h1><p>{view === 'home' ? 'Tu información financiera y la inteligencia para comprenderla.' : 'Información del entorno autorizado para tu identidad de pruebas.'}</p></div><div className="page-heading-actions"><button className="icon-button" title="Actualizar información" aria-label="Actualizar información" disabled={loading} onClick={() => setReload(value => value + 1)}><RefreshCw size={18} /></button><button className="secondary-button compact-button" onClick={() => setHidden(!hidden)}>{hidden ? <Eye size={17} /> : <EyeOff size={17} />}{hidden ? 'Mostrar importes' : 'Ocultar importes'}</button></div></div>
    <div className="data-context"><div className="data-context-details"><Cutoff value={summary?.cutoff_date} /><VerifiedPill /></div>{data && !currency && <p className="source-note"><CircleHelp size={14} />La fuente no especifica moneda. Los importes se muestran sin símbolo monetario.</p>}</div>
    {loading && !data ? <LoadingState /> : error && !data ? <ErrorNotice message={error} onRetry={() => setReload(value => value + 1)} /> : data && <>
      {error && <ErrorNotice message={error} onRetry={() => setReload(value => value + 1)} />}
      {view === 'home' && <>
        <div className="overview-grid"><section className="balance-card"><div className="balance-card-top"><span className="balance-label"><Wallet size={18} />Tu saldo registrado</span><span className="subtle-pill">Captación</span></div><strong className="main-balance">{money(data.summary.registered_balance, currency, hidden)}</strong><p>Débito y ahorro · Último cierre disponible</p><div className="balance-card-bottom"><span>{data.summary.account_count} cuentas autorizadas</span><span>{data.summary.product_count} productos</span></div></section>
          <section className="ai-intro-card"><span className="ai-card-icon"><Sparkles size={24} /></span><p className="eyebrow">ASISTENTE BANAMEX AI</p><h2>Entender tus datos<br />también es avanzar.</h2><p>Pregunta por tus cuentas, explora su evolución o consulta un producto.</p><button className="text-button" onClick={() => onAsk()}>Abrir mi asistente<MessageIcon /></button></section></div>
        <section className="content-section"><SectionTitle title="Tus productos" action="Ver todos" onAction={() => setView('products')} /><div className="accounts-grid">{data.accounts.length ? data.accounts.map(account => <AccountCard key={account.account_id} account={account} currency={currency} hidden={hidden} />) : <EmptyState description="No hay cuentas asociadas al corte disponible." />}</div></section>
        <section className="panel content-section"><SectionTitle title="Últimos movimientos" action="Ver movimientos" onAction={() => setView('transactions')} /><TransactionTable data={data.transactions} currency={currency} hidden={hidden} compact /><p className="panel-footnote">Importes registrados en la fuente. No se identifica una clasificación de cargo o abono.</p></section>
      </>}
      {view === 'products' && <>
        <section className="content-section"><SectionTitle title="Cuentas y saldos registrados" /><div className="accounts-grid">{data.accounts.map(account => <AccountCard key={account.account_id} account={account} currency={currency} hidden={hidden} />)}</div></section>
        <section className="panel content-section"><SectionTitle title="Detalle de productos" />{data.products.length ? <div className="product-list">{data.products.map(product => <div className="product-row" key={product.product_code}><span className={`product-mark ${product.category}`}><Wallet size={21} /></span><div><h3>{product.product_name}</h3><p>{product.account_count} {product.account_count === 1 ? 'cuenta asociada' : 'cuentas asociadas'} · {product.group === 'CAPTACION' ? 'Captación' : product.group === 'COLOCACION' ? 'Crédito' : product.group}</p></div><strong>{money(product.registered_balance, currency, hidden)}</strong></div>)}</div> : <EmptyState />}</section>
        <section className="unavailable-product"><Landmark size={21} /><div><h3>Inversiones</h3><p>Información no disponible en el modelo de datos autorizado.</p></div></section>
      </>}
      {view === 'transactions' && <section className="panel content-section"><SectionTitle title="Movimientos de tus cuentas" /><div className="transactions-meta"><span><ListFilter size={16} />{data.transactions.total} movimientos disponibles</span><span>{data.transactions.transaction_date_from && data.transactions.transaction_date_to ? `${date(data.transactions.transaction_date_from)} — ${date(data.transactions.transaction_date_to)}` : 'Periodo disponible en la fuente'}</span></div>{pageLoading ? <LoadingState /> : <TransactionTable data={data.transactions} currency={currency} hidden={hidden} />}<div className="pagination"><span>Mostrando {data.transactions.total ? data.transactions.offset + 1 : 0}–{Math.min(data.transactions.offset + data.transactions.transactions.length, data.transactions.total)} de {data.transactions.total}</span><div><button className="secondary-button compact-button" disabled={data.transactions.offset === 0 || pageLoading} onClick={() => changePage(Math.max(0, data.transactions.offset - data.transactions.limit))}>Anterior</button><button className="secondary-button compact-button" disabled={data.transactions.offset + data.transactions.limit >= data.transactions.total || pageLoading} onClick={() => changePage(data.transactions.offset + data.transactions.limit)}>Siguiente</button></div></div><p className="panel-footnote">La fuente no distingue cargos y abonos ni saldo disponible. No se deduce una clasificación.</p></section>}
      {view === 'finances' && <div className="finances-grid content-section"><section className="panel"><SectionTitle title="La evolución de tu ahorro" /><p className="section-subtitle">Saldos registrados de captación en cada cierre mensual.</p><HistoryChart values={data.summary.monthly_balances} currency={currency} hidden={hidden} /></section><section className="panel"><SectionTitle title="Movimientos por concepto" /><p className="section-subtitle">Importes agrupados del periodo disponible; no equivalen a un análisis de gastos.</p>{data.summary.transaction_concepts?.length ? <div className="concept-list">{data.summary.transaction_concepts.map(item => <div key={item.description}><span>{item.description}<small>{item.count} movimientos</small></span><strong>{money(item.amount, currency, hidden)}</strong></div>)}</div> : <EmptyState />}<button className="secondary-button" onClick={() => onAsk('¿Cómo han evolucionado mis ahorros?')}><Sparkles size={17} />Explorar con mi asistente</button></section></div>}
      <footer className="data-footer"><ShieldNote />Esta vista muestra exclusivamente información asociada a tu identidad autorizada.</footer>
    </>}
  </main>;
}

function MessageIcon() { return <Sparkles size={15} />; }
function ShieldNote() { return <span className="footer-shield"><CircleHelp size={15} /></span>; }
