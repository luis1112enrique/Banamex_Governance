import type { MonthlyBalance } from '../services/api';
import { EmptyState, money } from './Shared';

export default function HistoryChart({ values, currency, hidden = false }: { values: MonthlyBalance[]; currency?: string | null; hidden?: boolean }) {
  const points = values.filter(item => item.deposit_balance !== null && Number.isFinite(Number(item.deposit_balance)));
  if (!points.length) return <EmptyState description="No hay cierres mensuales disponibles para esta identidad." />;
  if (hidden) return <div className="hidden-chart">Importes ocultos</div>;
  const width = 620, height = 200, left = 12, right = 16, top = 20, bottom = 38;
  const numbers = points.map(item => Number(item.deposit_balance));
  const min = Math.min(...numbers), max = Math.max(...numbers);
  const spread = max - min || Math.max(Math.abs(max) * .1, 1);
  const low = min - spread * .2, high = max + spread * .2;
  const x = (index: number) => left + (points.length === 1 ? (width - left - right) / 2 : index * (width - left - right) / (points.length - 1));
  const y = (value: number) => top + (high - value) / (high - low) * (height - top - bottom);
  const line = points.map((item, index) => `${index === 0 ? 'M' : 'L'}${x(index)},${y(Number(item.deposit_balance))}`).join(' ');
  const fill = `${line} L${x(points.length - 1)},${height - bottom} L${x(0)},${height - bottom} Z`;
  return <div className="history-chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Evolución de saldos registrados de captación por fecha de corte">
    {[0, 1, 2].map(index => <line key={index} x1={left} y1={top + index * (height - top - bottom) / 2} x2={width - right} y2={top + index * (height - top - bottom) / 2} stroke="#E6EDEE" strokeDasharray="3 5" />)}
    {points.length > 1 && <path d={fill} fill="#E5F4EE" />}
    <path d={line} fill="none" stroke="#003746" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
    {points.map((item, index) => <g key={item.cutoff_date}><circle cx={x(index)} cy={y(Number(item.deposit_balance))} r="4.5" fill="#003746" stroke="white" strokeWidth="2"><title>{`${item.cutoff_date}: ${money(item.deposit_balance, currency)}`}</title></circle><text x={x(index)} y={height - 12} textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'} fill="#5C7278" fontSize="12">{new Date(`${item.cutoff_date}T12:00:00`).toLocaleDateString('es-MX', { month: 'short' })}</text></g>)}
  </svg><p className="chart-note">Saldo registrado de captación por cierre mensual. Cada punto representa un corte independiente.</p></div>;
}
