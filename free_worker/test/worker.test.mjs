import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { Miniflare, Response as MFResponse, Log, LogLevel } from 'miniflare';
import { generateKeyPair, exportJWK, SignJWT } from 'jose';
const { privateKey, publicKey } = await generateKeyPair('RS256');
const jwk = { ...await exportJWK(publicKey), kid: 'test-key', alg: 'RS256', use: 'sig' };
const schema = JSON.parse(await readFile(new URL('../src/schema.json', import.meta.url)));
const key = 'only-for-tests-not-a-real-secret-123456789';
async function token(sub='reader',scope='investment:read',extra={}) {
  return new SignJWT({ scope, ...extra }).setProtectedHeader({ alg:'RS256',kid:'test-key' }).setIssuer('https://issuer.example/').setAudience('https://mcp.example/mcp').setSubject(sub).setIssuedAt().setExpirationTime('10m').sign(privateKey);
}
async function fixture(fn) {
  let dispatches=0;
  const mf=new Miniflare({ modules:true,scriptPath:new URL('../dist/index.js',import.meta.url).pathname,compatibilityDate:'2026-07-30',d1Databases:['DB'],log:new Log(LogLevel.NONE),bindings:{ OAUTH_ISSUER:'https://issuer.example/',MCP_RESOURCE_URL:'https://mcp.example/mcp',INVESTMENT_OWNER_SUB:'owner',SYNC_KEY:key,GITHUB_REPOSITORY:'test/repo',GITHUB_DISPATCH_TOKEN:'test-only-token' },outboundService:async req=>{
    if(req.url==='https://issuer.example/.well-known/jwks.json')return new MFResponse(JSON.stringify({keys:[jwk]}),{headers:{'Content-Type':'application/json'}});
    if(req.url==='https://api.github.com/repos/test/repo/actions/workflows/sync-free.yml/dispatches'){dispatches++;return new MFResponse(null,{status:204});}
    throw Error('Unexpected outbound destination');
  }});
  try{
    const db=await mf.getD1Database('DB');await db.exec(await readFile(new URL('../migrations/0001_investment.sql',import.meta.url),'utf8'));
    const rpc=async(name,args={},bearer)=>{
      const r=await mf.dispatchFetch('https://mcp.example/mcp',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json, text/event-stream',...(bearer?{Authorization:'Bearer '+bearer}:{})},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}})});
      return {status:r.status,body:await r.json(),headers:r.headers};
    };
    const internal=async(path,body,lease,auth=key,method)=>mf.dispatchFetch('https://mcp.example'+path,{method:method||(body?'POST':'GET'),headers:{Authorization:'Bearer '+auth,'Content-Type':'application/json',...(lease?{'X-Sync-Lease':lease}:{})},body:body?JSON.stringify(body):undefined});
    await fn({mf,db,rpc,internal,dispatches:()=>dispatches});
  }finally{await mf.dispose();}
}
test('health, metadata and no authentication bypass',()=>fixture(async({mf,rpc})=>{
  assert.equal((await mf.dispatchFetch('https://mcp.example/healthz')).status,200);
  const meta=await mf.dispatchFetch('https://mcp.example/.well-known/oauth-protected-resource/mcp');
  assert.equal((await meta.json()).resource,'https://mcp.example/mcp');
  const r=await rpc('database_status');assert.equal(r.status,401);assert.match(r.headers.get('www-authenticate'),/resource_metadata/);
  assert.equal((await rpc('database_status',{},'SECRET')).status,401);
  assert.equal((await mf.dispatchFetch('http://mcp.example/mcp',{method:'POST',body:'{}'})).status,400);
}));
test('signed JWT audience, scope and owner separation',()=>fixture(async({rpc,dispatches})=>{
  assert.equal((await rpc('database_status',{},await token('reader','investment:update'))).status,403);
  const bad=await new SignJWT({scope:'investment:read'}).setProtectedHeader({alg:'RS256',kid:'test-key'}).setIssuer('https://issuer.example/').setAudience('wrong').setSubject('owner').setIssuedAt().setExpirationTime('10m').sign(privateKey);
  assert.equal((await rpc('database_status',{},bad)).status,401);
  const expired=await new SignJWT({scope:'investment:read'}).setProtectedHeader({alg:'RS256',kid:'test-key'}).setIssuer('https://issuer.example/').setAudience('https://mcp.example/mcp').setSubject('owner').setIssuedAt(1).setExpirationTime(2).sign(privateKey);
  assert.equal((await rpc('database_status',{},expired)).status,401);
  const read=await rpc('database_status',{},await token());assert.equal(read.status,200);assert.equal(read.body.result.isError,undefined);
  for(const bearer of [await token(),await token('reader','investment:read investment:update'),await token('owner')]){
    const r=await rpc('run_update',{start_date:'2024-01-04',end_date:'2024-01-04'},bearer);assert.equal(r.body.result.isError,true);
  }
  const result=await rpc('run_update',{start_date:'2024-01-04',end_date:'2024-01-04'},await token('owner','investment:read investment:update'));
  assert.equal(result.status,200);assert.notEqual(result.body.result.isError,true);assert.equal(dispatches(),1,JSON.stringify(result.body));
  const data=JSON.parse(result.body.result.content[0].text);assert.equal(data.dispatched,true);
}));
test('seven-day bound, secret-safe errors, literal SQL inputs',()=>fixture(async({rpc})=>{
  const bearer=await token('owner','investment:read investment:update');
  const bad=await rpc('run_update',{start_date:'2024-01-01',end_date:'2024-02-01'},bearer);assert.equal(bad.body.result.isError,true);
  const code=await rpc('lookup_price',{ticker:"SECRET' OR 1=1 --"},bearer);assert.equal(code.body.result.isError,true);assert.doesNotMatch(JSON.stringify(code.body),/test-only-token|only-for-tests/);
  const literal=await rpc('search_instruments',{query:"' OR 1=1 --"},bearer);assert.equal(JSON.parse(literal.body.result.content[0].text).length,0);
}));
test('sync lease excludes overlap; D1 batch rolls back all invalid rows',()=>fixture(async({db,internal})=>{
  assert.equal((await internal('/internal/lease',{},null,'bad')).status,401);
  const acquired=await internal('/internal/lease',{});assert.equal(acquired.status,200);const {lease}=await acquired.json();
  assert.equal((await internal('/internal/lease',{})).status,409);
  const make=(table,row)=>({table,action:'upsert',values:schema[table].columns.map(k=>row[k]??null)});
  const valid=make('instruments',{id:1,ticker:'7203',exchange:'TSE',country:'JP'});
  const invalid=make('prices',{instrument_id:999,traded_on:'2024-01-04',close:100});
  assert.equal((await internal('/internal/commit',{mutations:[valid,invalid]},lease)).status,503);
  assert.equal((await db.prepare('SELECT count(*) AS n FROM instruments').first()).n,0);
  assert.equal((await internal('/internal/commit',{mutations:[valid]},lease)).status,200);
  assert.equal((await internal('/internal/commit',{mutations:[valid]},lease)).status,200);
  assert.equal((await db.prepare('SELECT count(*) AS n FROM instruments').first()).n,1);
  const page=await internal('/internal/snapshot?table=instruments',undefined,lease);assert.equal((await page.json()).rows[0].ticker,'7203');
  const rejected=await internal('/internal/snapshot?table=investment_jobs',undefined,lease);assert.equal(rejected.status,400);
  await db.prepare('UPDATE investment_sync_lease SET expires_at=0').run();
  assert.equal((await internal('/internal/commit',{mutations:[valid]},lease)).status,503);
  await db.prepare('UPDATE investment_sync_lease SET expires_at=unixepoch()+60').run();
  await internal('/internal/lease',undefined,lease,key,'DELETE');
  assert.equal((await internal('/internal/lease',{})).status,200);
}));
test('batch caps prevent partial uploads and never accept arbitrary SQL',()=>fixture(async({internal})=>{
  const {lease}=await (await internal('/internal/lease',{})).json();
  assert.equal((await internal('/internal/commit',{mutations:Array(501).fill({})},lease)).status,400);
  assert.equal((await internal('/internal/commit',{mutations:[{table:'instruments; DROP TABLE prices',action:'upsert',values:[]}]},lease)).status,503);
  assert.equal((await internal('/internal/commit',{mutations:[{table:'investment_jobs',action:'upsert',values:[]}]},lease)).status,503);
}));
test('MCP initialize/list and stored-data tools through real Worker/D1',()=>fixture(async({mf,db,rpc})=>{
  await db.prepare("INSERT INTO instruments VALUES(1,'7203','TSE','JP')").run();
  await db.prepare("INSERT INTO sources(id,publisher,url) VALUES(1,'fixture','test:fixture')").run();
  await db.batch([1,2,3,4,5].map(i=>db.prepare('INSERT INTO prices(instrument_id,traded_on,open,high,low,close,volume,adj_close,available_at,provider_source_id) VALUES(1,?,100,110,90,105,1000,105,?,1)').bind(`2024-01-0${i}`,'2024-01-10T00:00:00Z')));
  await db.prepare('INSERT INTO jq_financial_disclosures(instrument_id,disclosure_number,disclosed_at,raw_json,sha256,fetched_at,source_id) VALUES(1,?,?,?,?,?,1)').bind('fixture-1','2024-01-04',JSON.stringify({Sales:100}),'fixture-hash','2024-01-10').run();
  const bearer=await token();
  const call=async(method,params)=>mf.dispatchFetch('https://mcp.example/mcp',{method:'POST',headers:{Authorization:'Bearer '+bearer,'Content-Type':'application/json',Accept:'application/json, text/event-stream'},body:JSON.stringify({jsonrpc:'2.0',id:1,method,params})});
  const init=await call('initialize',{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'offline-test',version:'1'}});assert.equal(init.status,200);assert.ok((await init.json()).result.protocolVersion);
  const list=await call('tools/list',{});assert.equal((await list.json()).result.tools.length,7);
  const price=await rpc('lookup_price',{ticker:'7203'},bearer);assert.equal(JSON.parse(price.body.result.content[0].text)[0].close,105);
  const financial=await rpc('financial_information',{ticker:'7203'},bearer);assert.equal(JSON.parse(financial.body.result.content[0].text)[0].raw_json.Sales,100);
  const technical=await rpc('technical_indicators',{ticker:'7203'},bearer);const values=JSON.parse(technical.body.result.content[0].text);assert.equal(values.sma_5,105);assert.equal(values.sma_25,null);
}));
