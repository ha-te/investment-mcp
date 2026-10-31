import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js';
import { createRemoteJWKSet, jwtVerify, type JWTPayload } from 'jose';
import { z } from 'zod';
import schemaJSON from './schema.json';

interface Env {
  DB: D1Database;
  OAUTH_ISSUER: string;
  MCP_RESOURCE_URL: string;
  INVESTMENT_OWNER_SUB: string;
  SYNC_KEY?: string;
  GITHUB_REPOSITORY?: string;
  GITHUB_DISPATCH_TOKEN?: string;
  GITHUB_REF?: string;
}
const schema: Record<string, { columns: string[]; primary: string[] }> = schemaJSON;
const tables = Object.keys(schema).filter(t => t !== 'investment_jobs');
const code = z.string().regex(/^[0-9A-Z]{4}$/);
const date = z.string().regex(/^\d{4}-\d{2}-\d{2}$/).refine(v => {
  const d = new Date(v + 'T00:00:00Z');
  return !Number.isNaN(+d) && d.toISOString().slice(0, 10) === v;
});
const jobId = z.string().regex(/^[0-9a-f]{32}$/);
const readHint = { readOnlyHint: true, destructiveHint: false, openWorldHint: false };
const jwks = new Map<string, ReturnType<typeof createRemoteJWKSet>>();
const budget: number[] = [];
const json = (body: unknown, status = 200, headers = {}) => Response.json(body, { status, headers });
const uid = () => crypto.randomUUID().replaceAll('-', '');
function https(value: string) {
  const u = new URL(value);
  if (u.protocol !== 'https:' || u.username || u.password || u.search || u.hash) throw Error('configuration');
  return u;
}
function range(start: string, end: string) {
  date.parse(start); date.parse(end);
  const days = (+new Date(end) - +new Date(start)) / 86400000;
  if (days < 0 || days > 6) throw Error('Free updates are limited to seven calendar days');
}
async function authorize(req: Request, env: Env): Promise<JWTPayload> {
  https(env.OAUTH_ISSUER); https(env.MCP_RESOURCE_URL);
  const bearer = req.headers.get('authorization') ?? '';
  if (!bearer.startsWith('Bearer ') || bearer.length > 16391) throw Error('unauthorized');
  let keys = jwks.get(env.OAUTH_ISSUER);
  if (!keys) {
    keys = createRemoteJWKSet(new URL(env.OAUTH_ISSUER.replace(/\/$/, '') + '/.well-known/jwks.json'), { timeoutDuration: 5000, cacheMaxAge: 300000 });
    jwks.set(env.OAUTH_ISSUER, keys);
  }
  const { payload } = await jwtVerify(bearer.slice(7), keys, {
    algorithms: ['RS256'], issuer: env.OAUTH_ISSUER, audience: env.MCP_RESOURCE_URL,
    requiredClaims: ['iss', 'aud', 'sub', 'iat', 'exp'], clockTolerance: 10,
  });
  if (!payload.sub || typeof payload.scope !== 'string') throw Error('unauthorized');
  return payload;
}
function owner(claims: JWTPayload, env: Env) {
  if (!env.INVESTMENT_OWNER_SUB || claims.sub !== env.INVESTMENT_OWNER_SUB || !(claims.scope as string).split(' ').includes('investment:update')) throw Error('Owner authorization required');
}
async function rows(env: Env, sql: string, args: (string | number | null)[] = []) {
  return (await env.DB.prepare(sql).bind(...args).all()).results;
}
async function prices(env: Env, ticker: string, limit: number) {
  return rows(env, `SELECT p.traded_on,p.open,p.high,p.low,p.close,p.volume,p.adj_close,p.available_at FROM prices p JOIN instruments i ON i.id=p.instrument_id WHERE i.ticker=? AND i.country='JP' ORDER BY p.traded_on DESC LIMIT ?`, [ticker, limit]);
}
async function indicators(env: Env, ticker: string) {
  const points = await prices(env, ticker, 250);
  const values = points.slice().reverse().map(r => r.adj_close);
  if (!values.length || values.some(v => typeof v !== 'number' || !Number.isFinite(v) || v <= 0)) return { ticker, status: 'insufficient_adjusted_prices', observations: values.length };
  const numbers = values as number[];
  const result: Record<string, unknown> = { ticker, date: points[0].traded_on, basis: 'adjusted_close', observations: values.length };
  for (const n of [5, 25, 75]) result[`sma_${n}`] = numbers.length >= n ? numbers.slice(-n).reduce((a, b) => a + b, 0) / n : null;
  result.cutler_rsi_14 = null;
  if (numbers.length >= 15) {
    const tail = numbers.slice(-15); let gains = 0, losses = 0;
    for (let i = 1; i < tail.length; i++) { const d = tail[i] - tail[i - 1]; gains += Math.max(0, d); losses += Math.max(0, -d); }
    result.cutler_rsi_14 = gains + losses ? 100 * gains / (gains + losses) : 50;
  }
  return result;
}
function mcp(env: Env, claims: JWTPayload) {
  const server = new McpServer({ name: 'investment-mcp-free', version: '1.0.0' });
  function tool(name: string, description: string, input: z.ZodRawShape, fn: (args: any) => Promise<unknown>, write = false) {
    server.registerTool(name, { description, inputSchema: input, annotations: write ? { readOnlyHint: false, destructiveHint: false, openWorldHint: true } : readHint }, async args => {
      try { const data = await fn(args); return { content: [{ type: 'text' as const, text: JSON.stringify(data) }] }; }
      catch { return { isError: true, content: [{ type: 'text' as const, text: 'Operation rejected or unavailable; verify permissions, input and free quotas' }] }; }
    });
  }
  tool('search_instruments', 'Search stored watchlist codes and company names.', { query: z.string().min(1).max(80), limit: z.number().int().min(1).max(50).default(20) }, async a => {
    const pattern = '%' + a.query.replace(/[\\%_]/g, (c: string) => '\\' + c) + '%';
    return rows(env, `SELECT i.ticker,i.exchange,s.name,s.sector,s.market FROM instruments i LEFT JOIN jq_instrument_snapshots s ON s.rowid=(SELECT s2.rowid FROM jq_instrument_snapshots s2 WHERE s2.instrument_id=i.id ORDER BY effective_on DESC,fetched_at DESC,rowid DESC LIMIT 1) WHERE i.country='JP' AND (i.ticker LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\') ORDER BY i.ticker LIMIT ?`, [pattern, pattern, a.limit]);
  });
  tool('lookup_price', 'Read stored prices, newest first.', { ticker: code, limit: z.number().int().min(1).max(250).default(30) }, a => prices(env, a.ticker, a.limit));
  tool('financial_information', 'Read original v2 financial fields and correction history.', { ticker: code, limit: z.number().int().min(1).max(50).default(20) }, async a => (await rows(env, `SELECT d.* FROM jq_financial_disclosures d JOIN instruments i ON i.id=d.instrument_id WHERE i.ticker=? AND i.country='JP' ORDER BY d.disclosed_at DESC,d.id DESC LIMIT ?`, [a.ticker, a.limit])).map(r => ({ ...r, raw_json: JSON.parse(r.raw_json as string) })));
  tool('technical_indicators', 'Adjusted-close SMA 5/25/75 and Cutler RSI 14; missing values are not fabricated.', { ticker: code }, a => indicators(env, a.ticker));
  tool('database_status', 'Stored watchlist statistics, latest price date and update history.', {}, async () => {
    const counts: Record<string, unknown> = {};
    for (const t of ['instruments', 'prices', 'jq_financial_disclosures']) counts[t] = (await env.DB.prepare(`SELECT COUNT(*) AS n FROM ${t}`).first())?.n;
    return { counts, latest_price_date: (await env.DB.prepare('SELECT MAX(traded_on) AS d FROM prices').first())?.d, recent_updates: await rows(env, 'SELECT * FROM investment_sync_units ORDER BY finished_at DESC LIMIT 20'), hosting: 'Workers Free + D1 Free' };
  });
  tool('run_update', 'OWNER ONLY: queue up to seven calendar days, within the configured free watchlist.', { start_date: date, end_date: date, refresh_completed: z.boolean().default(false) }, async a => {
    owner(claims, env); range(a.start_date, a.end_date);
    const id = uid();
    await env.DB.prepare(`INSERT INTO investment_jobs(id,requested_by,start_date,end_date,status,result_json) VALUES(?,?,?,?,'queued',?)`).bind(id, claims.sub, a.start_date, a.end_date, JSON.stringify({ refresh: a.refresh_completed })).run();
    let dispatched = false;
    if (env.GITHUB_DISPATCH_TOKEN && /^[\w.-]+\/[\w.-]+$/.test(env.GITHUB_REPOSITORY ?? '')) {
      try {
        const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/sync-free.yml/dispatches`, { method: 'POST', headers: { Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`, 'Content-Type': 'application/json', 'User-Agent': 'investment-mcp-free', 'X-GitHub-Api-Version': '2022-11-28' }, body: JSON.stringify({ ref: env.GITHUB_REF || 'main', inputs: { job_id: id } }), signal: AbortSignal.timeout(10000), redirect: 'manual' });
        dispatched = r.status === 204;
      } catch { /* Keep the durable queue for the scheduled workflow. */ }
    }
    return { job_id: id, status: 'queued', dispatched, note: dispatched ? 'Queued in GitHub Actions' : 'Waiting for the next scheduled/manual free sync workflow' };
  }, true);
  tool('update_job_status', 'OWNER ONLY: read durable update status.', { job_id: jobId }, async a => {
    owner(claims, env);
    const r = await env.DB.prepare('SELECT id,start_date,end_date,status,result_json,created_at,finished_at FROM investment_jobs WHERE id=?').bind(a.job_id).first();
    if (!r) throw Error('unknown job');
    return { ...r, result_json: r.result_json ? JSON.parse(r.result_json as string) : null };
  });
  return server;
}
async function internalAuth(req: Request, env: Env) {
  const key = env.SYNC_KEY;
  if (!key || key.length < 32) return false;
  const header = req.headers.get('authorization') ?? '';
  if (!header.startsWith('Bearer ')) return false;
  const received = header.slice(7);
  if (received.length > 128) return false;
  const hash = async (v: string) => new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(v)));
  const [a, b] = await Promise.all([hash(key), hash(received)]);
  let different = 0; for (let i = 0; i < a.length; i++) different |= a[i] ^ b[i];
  return different === 0;
}
async function lease(req: Request, env: Env) {
  const token = req.headers.get('x-sync-lease');
  if (!token || !/^[0-9a-f]{32}$/.test(token)) throw Error('lease');
  const valid = await env.DB.prepare('SELECT 1 FROM investment_sync_lease WHERE id=1 AND token=? AND expires_at>unixepoch()').bind(token).first();
  if (!valid) throw Error('lease');
  return token;
}
async function internal(req: Request, env: Env) {
  if (!await internalAuth(req, env)) return json({ error: 'unauthorized' }, 401);
  const url = new URL(req.url);
  if (url.pathname === '/internal/lease' && req.method === 'POST') {
    const token = uid();
    const result = await env.DB.prepare(`INSERT INTO investment_sync_lease(id,token,expires_at) VALUES(1,?,unixepoch()+1800) ON CONFLICT(id) DO UPDATE SET token=excluded.token,expires_at=excluded.expires_at WHERE investment_sync_lease.expires_at<=unixepoch()`).bind(token).run();
    if (!result.meta.changes) return json({ error: 'update_already_running' }, 409);
    await env.DB.prepare(`UPDATE investment_jobs SET status='interrupted',finished_at=CURRENT_TIMESTAMP WHERE status='running'`).run();
    return json({ lease: token, tables, schema });
  }
  const token = await lease(req, env);
  if (url.pathname === '/internal/lease' && req.method === 'DELETE') {
    await env.DB.prepare('DELETE FROM investment_sync_lease WHERE id=1 AND token=?').bind(token).run();
    return json({ released: true });
  }
  if (url.pathname === '/internal/snapshot' && req.method === 'GET') {
    const table = url.searchParams.get('table') ?? '';
    const after = Number(url.searchParams.get('after') ?? '0');
    if (!tables.includes(table) || !Number.isSafeInteger(after) || after < 0) return json({ error: 'invalid_request' }, 400);
    return json({ rows: await rows(env, `SELECT rowid AS _sync_rowid,* FROM ${table} WHERE rowid>? ORDER BY rowid LIMIT 250`, [after]) });
  }
  if (url.pathname === '/internal/commit' && req.method === 'POST') {
    const body = await req.json() as { mutations: { table: string; action: string; values: (string | number | null)[] }[] };
    if (!Array.isArray(body.mutations) || body.mutations.length > 500) return json({ error: 'batch_limit' }, 400);
    const batch: D1PreparedStatement[] = [];
    for (const mutation of body.mutations) {
      if (!tables.includes(mutation.table)) throw Error('table');
      const { columns, primary } = schema[mutation.table];
      if (!Array.isArray(mutation.values) || mutation.values.some(v => v !== null && (typeof v !== 'string' && typeof v !== 'number' || typeof v === 'number' && !Number.isFinite(v)))) throw Error('values');
      if (mutation.action === 'upsert' && mutation.values.length === columns.length) {
        const update = columns.filter(c => !primary.includes(c)).map(c => `${c}=excluded.${c}`).join(',');
        batch.push(env.DB.prepare(`INSERT INTO ${mutation.table}(${columns.join(',')}) SELECT ${columns.map(() => '?').join(',')} WHERE EXISTS(SELECT 1 FROM investment_sync_lease WHERE id=1 AND token=? AND expires_at>unixepoch()) ON CONFLICT(${primary.join(',')}) ${update ? 'DO UPDATE SET ' + update : 'DO NOTHING'}`).bind(...mutation.values,token));
      } else if (mutation.action === 'delete' && mutation.values.length === primary.length) {
        batch.push(env.DB.prepare(`DELETE FROM ${mutation.table} WHERE ${primary.map(c => `${c}=?`).join(' AND ')} AND EXISTS(SELECT 1 FROM investment_sync_lease WHERE id=1 AND token=? AND expires_at>unixepoch())`).bind(...mutation.values,token));
      } else throw Error('mutation');
    }
    // A whole endpoint/day delta is one D1 transaction, never several uploads.
    batch.unshift(env.DB.prepare('UPDATE investment_sync_lease SET expires_at=unixepoch()+1800 WHERE id=1 AND token=? AND expires_at>unixepoch()').bind(token));
    const applied = await env.DB.batch(batch);
    if (!applied[0].meta.changes) return json({ error: 'lease_expired' },409);
    return json({ committed: body.mutations.length });
  }
  if (url.pathname === '/internal/jobs' && req.method === 'GET') {
    const id = url.searchParams.get('id');
    if (id) return json({ jobs: await rows(env, 'SELECT * FROM investment_jobs WHERE id=?', [jobId.parse(id)]) });
    return json({ jobs: await rows(env, "SELECT * FROM investment_jobs WHERE status IN ('queued','interrupted') ORDER BY created_at LIMIT 1") });
  }
  if (url.pathname === '/internal/jobs' && req.method === 'POST') {
    const body = await req.json() as any;
    if (body.action === 'schedule') {
      range(body.start_date, body.end_date);
      const existing = await env.DB.prepare("SELECT id FROM investment_jobs WHERE requested_by='scheduler' AND start_date=? AND end_date=? AND status IN ('queued','running','complete','partial')").bind(body.start_date, body.end_date).first();
      if (existing) return json({ job_id: existing.id });
      const id = uid();
      await env.DB.prepare("INSERT INTO investment_jobs(id,requested_by,start_date,end_date,status,result_json) VALUES(?,'scheduler',?,?,'queued',?)").bind(id, body.start_date, body.end_date, JSON.stringify({ refresh: true })).run();
      return json({ job_id: id });
    }
    const id = jobId.parse(body.job_id);
    if (body.action === 'start') await env.DB.prepare("UPDATE investment_jobs SET status='running',finished_at=NULL WHERE id=? AND status IN ('queued','interrupted','failed')").bind(id).run();
    else if (body.action === 'finish' && ['complete', 'partial', 'failed'].includes(body.status)) {
      const summary = { status: body.status, reason: body.reason === 'configuration_missing' ? 'configuration_missing' : body.reason === 'update_failed' ? 'update_failed' : undefined };
      await env.DB.prepare('UPDATE investment_jobs SET status=?,result_json=?,finished_at=CURRENT_TIMESTAMP WHERE id=?').bind(body.status, JSON.stringify(summary), id).run();
    } else throw Error('job');
    return json({ updated: true });
  }
  return json({ error: 'not_found' }, 404);
}
async function handle(req: Request, env: Env) {
  const url = new URL(req.url);
  if (url.pathname === '/healthz') {
    try {
      https(env.OAUTH_ISSUER);
      if (https(env.MCP_RESOURCE_URL).pathname !== '/mcp' || !env.INVESTMENT_OWNER_SUB) throw Error('configuration');
      const version = await env.DB.prepare('SELECT version FROM investment_schema_versions WHERE version=1').first();
      if (!version) throw Error('migration');
      return json({ status: 'ok' });
    }
    catch { return json({ status: 'unavailable' }, 503); }
  }
  if (url.pathname.startsWith('/internal/')) return internal(req, env);
  if (url.pathname === '/.well-known/oauth-protected-resource/mcp') {
    return json({ resource: env.MCP_RESOURCE_URL, authorization_servers: [env.OAUTH_ISSUER], scopes_supported: ['investment:read'], bearer_methods_supported: ['header'] });
  }
  if (url.pathname !== '/mcp') return json({ error: 'not_found' }, 404);
  if (req.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);
  let claims: JWTPayload;
  try { claims = await authorize(req, env); }
  catch { return json({ error: 'unauthorized' }, 401, { 'WWW-Authenticate': `Bearer resource_metadata="${new URL('/.well-known/oauth-protected-resource/mcp', env.MCP_RESOURCE_URL)}", scope="investment:read"` }); }
  if (!(claims.scope as string).split(' ').includes('investment:read')) return json({ error: 'insufficient_scope' }, 403);
  const server = mcp(env, claims);
  const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
  await server.connect(transport);
  return transport.handleRequest(req);
}
export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    try {
      if (new URL(req.url).protocol !== 'https:') return json({ error: 'https_required' }, 400);
      const now = Date.now(); while (budget.length && budget[0] < now - 60000) budget.shift();
      if (new URL(req.url).pathname !== '/healthz') {
        if (budget.length >= 120) return json({ error: 'rate_limited' }, 429, { 'Retry-After': '60' });
        budget.push(now);
      }
      const limit = new URL(req.url).pathname === '/internal/commit' ? 2097152 : 65536;
      if (req.body) {
        const reader = req.body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
        while (true) {
          let timer: ReturnType<typeof setTimeout> | undefined;
          const chunk = await Promise.race([reader.read(), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(Error('timeout')), 15000); })]).finally(() => clearTimeout(timer));
          if (chunk.done) break;
          size += chunk.value.byteLength;
          if (size > limit) { await reader.cancel(); return json({ error: 'body_limit' }, 413); }
          chunks.push(chunk.value);
        }
        const body = new Uint8Array(size); let offset = 0;
        for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
        req = new Request(req.url, { method: req.method, headers: req.headers, body: size ? body : undefined });
      }
      return await handle(req, env);
    } catch { return json({ error: 'operation_unavailable' }, 503); }
  },
};
