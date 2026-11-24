export type Portal = 'cliente' | 'institucional';
export type Profile = 'cliente' | 'data_steward' | 'auditor';

export interface Session {
  identity: string;
  display_name?: string;
  profile: Profile;
  portal: Portal;
  cliente_id?: number | null;
  csrf_token: string;
  permissions: string[];
}

export interface MonthlyBalance {
  cutoff_date: string;
  deposit_balance: number | null;
  credit_balance: number | null;
}

export interface Summary {
  cliente_id: number;
  cutoff_date: string | null;
  currency: string | null;
  registered_balance: number | null;
  credit_balance: number | null;
  available_balance: number | null;
  account_count: number;
  product_count: number;
  client?: { segment?: string; person_type?: string };
  monthly_balances: MonthlyBalance[];
  transaction_concepts: { description: string; amount: number; count: number }[];
  limitations: string[];
}

export interface Account {
  account_id: number | string;
  account_number?: string;
  masked_number: string;
  product_code: string;
  product_name: string;
  domain?: string;
  group: string;
  category: 'debit' | 'savings' | 'credit' | 'other';
  registered_balance: number | null;
  available_balance: number | null;
  opened_on?: string;
  cutoff_date: string;
}

export interface Product {
  product_code: string;
  product_name: string;
  group: string;
  category: string;
  account_count: number;
  registered_balance: number | null;
}

export interface Transaction {
  id: string | number;
  account_id: string | number;
  product_code: string;
  product_name: string;
  date: string;
  description: string;
  amount: number | null;
  direction: string | null;
}

export interface Transactions {
  cutoff_date: string | null;
  transaction_date_from?: string;
  transaction_date_to?: string;
  transactions: Transaction[];
  total: number;
  limit: number;
  offset: number;
}

export interface InstitutionSummary {
  cutoff_date: string | null;
  currency?: string | null;
  registered_clients: number;
  active_clients: number;
  account_count: number;
  deposit_balance: number | null;
  credit_balance: number | null;
  product_breakdown: { product_code: string; product_name: string; group: string; registered_balance: number; account_count: number }[];
  segment_breakdown: { segment: string; client_count: number; deposit_balance: number; credit_balance: number }[];
  monthly_balances: MonthlyBalance[];
  limitations?: string[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

let csrf = '';

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = init.method ?? 'GET';
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (method !== 'GET' && csrf) headers.set('X-CSRF-Token', csrf);
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { ...init, method, headers, credentials: 'include' });
  } catch {
    throw new ApiError(0, 'No fue posible conectar con el servicio local. Comprueba que la API esté iniciada.');
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail;
    const message = typeof detail === 'string' ? detail : detail?.message ?? body.error?.message ?? body.message ?? 'No fue posible completar la solicitud.';
    if (response.status === 401 && path !== '/auth/login' && path !== '/session') {
      window.dispatchEvent(new Event('banamex:session-expired'));
    }
    throw new ApiError(response.status, message);
  }
  return body as T;
}

export const api = {
  async session(): Promise<Session> {
    const session = await request<Session>('/session');
    csrf = session.csrf_token;
    return session;
  },
  async login(username: string, password: string, portal: Portal): Promise<Session> {
    await request('/auth/login', { method: 'POST', body: JSON.stringify({ username, password, portal }) });
    return this.session();
  },
  async logout(): Promise<void> {
    await request('/auth/logout', { method: 'POST' });
    csrf = '';
  },
  summary: () => request<Summary>('/me/summary'),
  accounts: () => request<{ cutoff_date: string | null; currency: string | null; accounts: Account[] }>('/me/accounts'),
  products: () => request<{ cutoff_date: string | null; currency: string | null; products: Product[] }>('/me/products'),
  transactions: (offset = 0) => request<Transactions>(`/me/transactions?limit=20&offset=${offset}`),
  institutionSummary: () => request<InstitutionSummary>('/institution/summary'),
  conversation: () => request<{ conversation_id: string }>('/conversations', { method: 'POST' }),
  chat: (conversation_id: string, question: string, files: File[] = []) => {
    if (files.length) {
      const body = new FormData();
      body.append('conversation_id', conversation_id);
      body.append('question', question);
      files.forEach(file => body.append('files', file, file.name));
      return request<{ final_answer: string; chart_id?: string | null; cutoff_date?: string | null }>('/assistant/documents', { method: 'POST', body });
    }
    return request<{ final_answer: string; chart_id?: string | null; cutoff_date?: string | null }>('/assistant/chat', { method: 'POST', body: JSON.stringify({ conversation_id, question }) });
  },
  chartUrl: (id: string) => `/api/charts/${encodeURIComponent(id)}`,
};
