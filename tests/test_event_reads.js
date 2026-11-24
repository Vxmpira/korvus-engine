// Checks the forex event card library (descriptions, read, verdict) straight from
// korvus_forex_calendar.html. Run: node tests/test_event_reads.js
const fs=require('fs'), path=require('path'); const m={};
const html=fs.readFileSync(path.join(__dirname,'..','korvus_forex_calendar.html'),'utf8');
const cut=(a,b)=>html.slice(html.indexOf(a), html.indexOf(b));
const lib=cut('const INVERSE_ACTUAL','// Clean, consistent number precision')+cut('const EV_DESC = [','function openEvent(eid){');
new Function('m', lib+';m.evDesc=evDesc;m.evRead=evRead;m.v=_verdict;m.ac=actualClass;')(m);
const fut=new Date(Date.now()+864e5).toISOString(), past=new Date(Date.now()-864e5).toISOString();
let bad=0; const ok=(c,msg)=>{ if(!c){bad++; console.log('FAIL',msg);} };
const E=(t,c,f,p,a,d)=>({title:t,country:c,forecast:f,previous:p,actual:a,date:d});
const R=e=>m.evRead(e,m.evDesc(e.title)).join(' ');
// direction and verdict agree
for(const [t,f,a,want] of [['Unemployment Rate','4.2%','4.0%','positive'],['Unemployment Claims','198K','203K','negative'],['Challenger Job Cuts y/y','10%','30%','negative'],['CPI m/m','0.3%','0.4%','positive'],['Retail Sales m/m','0.1%','-0.2%','negative'],['Flash Manufacturing PMI','52.0','49.5','negative']]){
  const e=E(t,'USD',f,'1',a,past), r=R(e), v=m.v(e);
  ok(r.includes('reads '+want), t+' read: '+r); ok(v.cls===(want==='positive'?'beat':'miss'), t+' verdict '+v.txt);
}
ok(R(E('Flash Manufacturing PMI','USD','52.0','51','49.5',past)).includes('below the 50 line'),'pmi 50 line uses actual');
// neutral kinds never claim a direction or beat/miss
for(const t of ['Crude Oil Inventories','10-y Bond Auction','Foreign Currency Reserves','Some Unknown Release']){
  const e=E(t,'USD','1.2M','3.4M','5.0M',past); ok(!/reads (positive|negative)/.test(R(e)), t+' neutral read'); ok(m.v(e).cls==='flat' && /Above forecast/.test(m.v(e).txt), t+' neutral verdict '+m.v(e).txt);
}
// talk / holiday
for(const t of ['FOMC Member Barr Speaks','Fed Chair Warsh Speaks','ECB Press Conference','FOMC Meeting Minutes','President Trump Speaks','OPEC Meetings','Autumn Forecast Statement','BOE Gov Bailey Speaks']){ const d=m.evDesc(t); ok(d.kind==='talk', t+' kind '+d.kind); ok(m.v(E(t,'USD','','','',past)).txt==='Event has taken place', t+' status'); }
ok(m.evDesc('Bank Holiday').kind==='none' && m.evDesc('Thanksgiving Day').kind==='none','holiday');
// rate decisions are not treated as talk
for(const t of ['Federal Funds Rate','Official Cash Rate','Main Refinancing Rate','Official Bank Rate','BOJ Policy Rate','RBNZ Rate Statement','Monetary Policy Statement','FOMC Statement']) ok(!m.evDesc(t).kind && m.evDesc(t).fut==='rates', t+' rates');
// specific beats general
ok(/autos/.test(m.evDesc('Core Retail Sales m/m').m),'core retail'); ok(/quarter over quarter/.test(m.evDesc('Core Retail Sales q/q').m),'period qoq');
ok(/volatile items/.test(m.evDesc('Core CPI y/y').m) && /year over year/.test(m.evDesc('Core CPI y/y').m),'core cpi yoy');
ok(/PCE/.test(m.evDesc('Core PCE Price Index m/m').m),'core pce'); ok(/expect inflation/.test(m.evDesc('Prelim UoM Inflation Expectations').m),'infl exp');
ok(/Germany/.test(m.evDesc('German Flash Manufacturing PMI').m) && /first estimate/.test(m.evDesc('German Flash Manufacturing PMI').vint),'german flash');
ok(/revision/.test(m.evDesc('Final Services PMI').vint),'final'); ok(!/\{p\}/.test(JSON.stringify(['GDP','CPI','Retail Sales','PPI','Average Hourly Earnings'].map(t=>m.evDesc(t).m))),'no raw {p}');
ok(!m.evDesc('Unemployment Rate').fut || m.evDesc('Unemployment Rate').better==='lower','unemp');
ok(m.evDesc('GDP q/q').fut==='growth' && !/tic/.test(m.evDesc('Gross Domestic Product q/q').m),'gdp');
// status wording + no-forecast + futures line only on USD
ok(m.v(E('CPI m/m','EUR','0.3%','0.2%','',past)).txt==='Released, result not in yet','past no actual'); ok(m.v(E('CPI m/m','EUR','0.3%','0.2%','',fut)).txt==='Not released yet','future');
ok(/No forecast is published/.test(R(E('Revised UoM Inflation Expectations','USD','','4.3%','3.7%',past))),'no forecast');
ok(/index futures/.test(R(E('CPI m/m','USD','0.3%','0.2%','',fut))) && !/index futures/.test(R(E('CPI m/m','EUR','0.3%','0.2%','',fut))),'futures line USD only');
ok(/in line with the forecast/.test(R(E('CPI m/m','USD','0.3%','0.2%','0.3%',past))),'in line');
ok(R(E('Trade Balance','USD','-60.5B','-70.1B','-58.0B',past)).includes('above the forecast'),'negatives');
ok(R(E('Tentative Thing','USD','abc','','',fut))==='' || true,'garbage safe');
// no em dashes, no undefined anywhere
const all=`Flash Manufacturing PMI|Flash Services PMI|German ifo Business Climate|CB Consumer Confidence|Prelim GDP q/q|Durable Goods Orders m/m|New Home Sales|Goods Trade Balance|Chicago PMI|Tokyo Core CPI y/y|Monthly CPI Indicator y/y|GfK Consumer Climate|M3 Money Supply y/y|KOF Economic Barometer|Housing Starts y/y|Current Account|ANZ Business Confidence|Nationwide HPI m/m|Mortgage Approvals|10-y Bond Auction|Natural Gas Storage|Revised UoM Consumer Sentiment|Employment Change q/q|PPI Input q/q|Wards Total Vehicle Sales|Consumer Credit m/m|JOLTS Job Openings|ADP Non-Farm Employment Change|Non-Farm Employment Change|Average Hourly Earnings m/m|ISM Manufacturing PMI|ISM Services PMI|Empire State Manufacturing Index|Philly Fed Manufacturing Index|TIC Long-Term Purchases|Federal Budget Balance|Building Permits|Existing Home Sales|Pending Home Sales m/m|Industrial Production m/m|Capacity Utilization Rate|Business Inventories m/m|Import Prices m/m|Core PPI m/m|Unit Labor Costs q/q|Prelim Nonfarm Productivity q/q|ZEW Economic Sentiment|Sentix Investor Confidence|German Factory Orders m/m|French Industrial Production m/m|Claimant Count Change|Average Earnings Index 3m/y|Tankan Manufacturing Index|Cash Rate|Trimmed Mean CPI q/q|Wage Price Index q/q|NAB Business Confidence|Westpac Consumer Sentiment|GDT Price Index|Ivey PMI|Overnight Rate`.split('|');
let gen=[]; for(const t of all){ for(const d of [fut,past]) for(const a of ['','1.0']){ const e=E(t,'USD','1.0','0.9',a,d), x=m.evDesc(t), txt=x.m+x.w+(x.vint||'')+R(e)+m.v(e).txt; ok(!/undefined|NaN|\{p\}|—/.test(txt), t+' clean: '+txt.slice(0,80)); } if(m.evDesc(t).generic) gen.push(t); }
ok(gen.length===0,'generic: '+gen.join(' | '));
console.log(bad ? 'FAILED '+bad : 'ALL EVENT READ TESTS PASSED'); process.exit(bad?1:0);
