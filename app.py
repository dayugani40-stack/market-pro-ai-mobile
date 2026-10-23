import truststore
truststore.inject_into_ssl()
import os,zipfile,xml.etree.ElementTree as ET,math,re,ast
from dotenv import load_dotenv
load_dotenv()
from flask import Flask,render_template,request,jsonify,send_file,redirect,session,url_for
app=Flask(__name__)
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json, secrets, gzip, time
app.secret_key=os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
UPSTOX_REDIRECT=os.environ.get('UPSTOX_REDIRECT','http://127.0.0.1:5000/callback').strip()
TOKEN=None
INSTRUMENTS=None

def api_json(url, token=None, post=None):
 headers={'Accept':'application/json','User-Agent':'MarketProAI/1.0'}
 if token:headers['Authorization']='Bearer '+token
 if post is not None:
  headers['Content-Type']='application/x-www-form-urlencoded'
  post=urlencode(post).encode()
 with urlopen(Request(url,headers=headers,data=post),timeout=20) as r:return json.load(r)

@app.get('/connect/upstox')
def connect_upstox():
 key=os.environ.get('UPSTOX_API_KEY','').strip()
 if not key:return jsonify(error='Set UPSTOX_API_KEY environment variable'),400
 state=secrets.token_urlsafe(24);session['upstox_state']=state
 return redirect('https://api.upstox.com/v2/login/authorization/dialog?'+urlencode({'response_type':'code','client_id':key,'redirect_uri':UPSTOX_REDIRECT,'state':state}))

@app.get('/callback')
def upstox_callback():
 global TOKEN
 expected=session.pop('upstox_state',None)
 if not expected or not secrets.compare_digest(expected,request.args.get('state','')):
  return 'Invalid OAuth state; restart login',400
 code=request.args.get('code')
 if not code:return 'Upstox authorization was not completed',400
 try:
  result=api_json('https://api.upstox.com/v2/login/authorization/token',post={
   'code':code,'client_id':os.environ['UPSTOX_API_KEY'],
   'client_secret':os.environ['UPSTOX_API_SECRET'],
   'redirect_uri':UPSTOX_REDIRECT,'grant_type':'authorization_code'})
  TOKEN=result['access_token']
  return redirect('/?upstox=connected')
 except Exception as e:return 'Upstox login failed: '+str(e)[:150],502

@app.get('/api/upstox/status')
def upstox_status():return jsonify(connected=bool(TOKEN),source='Upstox',login_url='/connect/upstox')

def get_instruments():
 global INSTRUMENTS
 if INSTRUMENTS is not None:return INSTRUMENTS
 url='https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz'
 with urlopen(Request(url,headers={'User-Agent':'MarketProAI/1.0'}),timeout=30) as r:
  raw=r.read()
 INSTRUMENTS=json.loads(gzip.decompress(raw) if raw[:2]==b'\x1f\x8b' else raw)
 return INSTRUMENTS

def instrument_key(market,symbol):
 # Official Upstox index instrument key; avoids downloading the full instrument list for this index.
 if market=='Indices' and symbol=='NIFTY 50':return 'NSE_INDEX|Nifty 50'
 wanted='BSE' if market=='BSE Stocks' or symbol in ('SENSEX','BANKEX') else 'NSE'
 for x in get_instruments():
  seg=str(x.get('segment',''));typ=str(x.get('instrument_type',''))
  name=str(x.get('trading_symbol',x.get('tradingsymbol',''))).upper()
  short=str(x.get('name','')).upper()
  if market=='Indices':
   if 'INDEX' in seg.upper() and (short==symbol or name==symbol):return x['instrument_key']
  elif seg.startswith(wanted+'_') and typ=='EQ' and name==symbol:return x['instrument_key']
 raise ValueError('Symbol not found in Upstox instruments: '+symbol)

@app.get('/api/upstox/quote')
def upstox_quote():
 if not TOKEN:return jsonify(error='Connect Upstox first'),401
 market=request.args.get('market','');symbol=request.args.get('symbol','').strip().upper()
 if market not in INDIAN_MARKETS or symbol not in INDIAN_MARKETS[market]:return jsonify(error='Unsupported symbol'),400
 stage='instrument lookup'
 try:
  key=instrument_key(market,symbol)
  stage='Upstox quote API'
  data=api_json('https://api.upstox.com/v2/market-quote/quotes?'+urlencode({'instrument_key':key}),TOKEN)
  values=list(data.get('data',{}).values())
  if not values:raise ValueError('No market quote')
  q=values[0];ohlc=q.get('ohlc',{});op=float(ohlc.get('open',0));ltp=float(q.get('last_price',0))
  if op<=0 or ltp<=0:raise ValueError('Invalid quote values')
  return jsonify(market=market,symbol=symbol,open=op,ltp=ltp,source='Upstox API quote (snapshot; not streaming)',instrument_key=key,timestamp=q.get('last_trade_time'),delayed=False)
 except Exception as e:return jsonify(error='Upstox quote failed at '+stage+': '+str(e)[:200]),503

@app.get('/api/upstox/candles')
def upstox_candles():
 if not TOKEN:return jsonify(error='Connect Upstox first'),401
 market=request.args.get('market','');symbol=request.args.get('symbol','').strip().upper()
 if market not in INDIAN_MARKETS or symbol not in INDIAN_MARKETS[market]:return jsonify(error='Unsupported symbol'),400
 interval=request.args.get('interval','1minute')
 if interval not in ('1minute','30minute'):return jsonify(error='Unsupported interval'),400
 from urllib.parse import quote as urlquote
 from datetime import datetime,timedelta,timezone
 stage='instrument lookup'
 try:
  key=instrument_key(market,symbol)
  encoded=urlquote(key,safe='')
  minutes='1' if interval=='1minute' else '30'
  stage='Upstox V3 intraday candle API'
  url='https://api.upstox.com/v3/historical-candle/intraday/'+encoded+'/minutes/'+minutes
  result=api_json(url,TOKEN)
  raw=result.get('data',{}).get('candles',[])
  source='Upstox V3 intraday candles (current session)'
  if not raw:
   stage='Upstox V3 historical candle API'
   today=datetime.now(timezone(timedelta(hours=5,minutes=30))).date()
   from_day=today-timedelta(days=10)
   url=('https://api.upstox.com/v3/historical-candle/'+encoded+'/minutes/'+minutes+'/'+today.isoformat()+'/'+from_day.isoformat())
   result=api_json(url,TOKEN)
   raw=result.get('data',{}).get('candles',[])
   source='Upstox V3 historical candles (not live)'
  candles=[]
  for x in reversed(raw):
   if len(x)>=6:
    candles.append({'time':x[0],'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])})
  return jsonify(candles=candles[-350:],source=source,interval=interval,instrument_key=key,warning=('No candles returned by Upstox for selected instrument/date range' if not candles else None))
 except Exception as e:return jsonify(error='Candles unavailable at '+stage+': '+str(e)[:180]),503

BASE=os.path.dirname(__file__); BOOK=os.path.join(BASE,'INTRDAY AND POSITION for STOCKS and INDICES.xlsx')
NS={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
def load_book():
 with zipfile.ZipFile(BOOK) as z:
  wb=ET.fromstring(z.read('xl/workbook.xml')); rel=ET.fromstring(z.read('xl/_rels/workbook.xml.rels')); links={e.attrib['Id']:e.attrib['Target'] for e in rel}; out={}
  for sh in wb.findall('m:sheets/m:sheet',NS):
   name=sh.attrib['name']; target=links[sh.attrib['{%s}id'%NS['r']]]; target=target.lstrip('/') if target.startswith('/') else 'xl/'+target; target=os.path.normpath(target).replace('\\','/')
   root=ET.fromstring(z.read(target)); formulas={}; cells={}
   for c in root.findall('.//m:sheetData/m:row/m:c',NS):
    addr=c.attrib['r'];f=c.find('m:f',NS);v=c.find('m:v',NS);val=v.text if v is not None else '';cells[addr]=val
    if f is not None: formulas[addr]=(f.text or ('[Shared formula]' if f.attrib.get('t')=='shared' else ''),val)
   out[name]=(formulas,cells)
  return out
DATA=load_book()
@app.get('/download/excel')
def download_excel():return send_file(BOOK,as_attachment=True,download_name=os.path.basename(BOOK))
@app.get('/api/summary')
def summary():
 return jsonify(sheets=[{'name':name,'formulas':len(fs),'cells':len(cs)} for name,(fs,cs) in DATA.items()],total_formulas=sum(len(fs) for fs,_ in DATA.values()),limitations=['GOOGLEFINANCE and external feeds not evaluated','Shared formulas may be stored without an expanded expression','Complex Excel functions may show cached results'])
@app.get('/')
def home():return render_template('index.html',sheets=list(DATA),count=sum(len(x[0]) for x in DATA.values()))
@app.get('/api/formulas')
def formulas():
 sheet=request.args.get('sheet','All');term=request.args.get('q','').lower().strip();page=max(1,int(request.args.get('page','1')));limit=100;rows=[]
 for s,(fs,_) in DATA.items():
  if sheet!='All' and sheet!=s:continue
  for cell,(f,cached) in fs.items():
   if term and term not in (s+' '+cell+' '+f+' '+str(cached)).lower():continue
   rows.append({'sheet':s,'cell':cell,'formula':'='+f,'saved':cached})
 return jsonify(total=len(rows),page=page,pages=max(1,math.ceil(len(rows)/limit)),rows=rows[(page-1)*limit:page*limit])
INDIAN_MARKETS={
 'Indices':['NIFTY 50','NIFTY BANK','NIFTY FIN SERVICE','NIFTY MIDCAP SELECT','SENSEX','BANKEX'],
 'NSE Stocks':['RELIANCE','HDFCBANK','ICICIBANK','SBIN','INFY','TCS','ITC','BHARTIARTL','LT','AXISBANK','KOTAKBANK','BAJFINANCE','TATAMOTORS','MARUTI','SUNPHARMA','WIPRO','ADANIENT','HINDUNILVR','NTPC','POWERGRID'],
 'BSE Stocks':['RELIANCE','HDFCBANK','TCS','INFY','SBIN','ITC']
}
@app.get('/api/indian-markets')
def indian_markets():return jsonify(markets=INDIAN_MARKETS,price_source='manual',currency='INR')
# Indicative Yahoo Finance quote adapter; not an exchange-authorized realtime feed.
SYMBOL_MAP={'NIFTY 50':'^NSEI','NIFTY BANK':'^NSEBANK','NIFTY FIN SERVICE':'NIFTY_FIN_SERVICE.NS','NIFTY MIDCAP SELECT':'NIFTY_MID_SELECT.NS','SENSEX':'^BSESN','BANKEX':'^BSEBANK'}
@app.get('/api/quote')
def quote():
 try:
  import yfinance as yf
  market=request.args.get('market','');symbol=request.args.get('symbol','').strip().upper()
  if market not in INDIAN_MARKETS or symbol not in INDIAN_MARKETS[market]:return jsonify(error='Unsupported market/symbol'),400
  ticker=SYMBOL_MAP.get(symbol) if market=='Indices' else symbol+('.BO' if market=='BSE Stocks' else '.NS')
  history=yf.Ticker(ticker).history(period='5d',interval='1m',auto_adjust=False)
  if history.empty:return jsonify(error='Quote unavailable. Use manual prices; do not assume market is open.'),503
  last=history.iloc[-1]; day=history[history.index.date==history.index[-1].date()]
  op=float(day.iloc[0]['Open']);ltp=float(last['Close']);stamp=history.index[-1].isoformat()
  if not all(map(math.isfinite,[op,ltp])) or min(op,ltp)<=0:raise ValueError('Invalid quote')
  return jsonify(market=market,symbol=symbol,open=op,ltp=ltp,timestamp=stamp,ticker=ticker,source='Yahoo Finance via yfinance (indicative, potentially delayed/stale; NOT exchange realtime)',delayed=True)
 except Exception as exc:return jsonify(error='Quote fetch failed: '+str(exc)[:180]+'. Enter prices manually.'),503
@app.post('/api/levels')
def levels():
 try:
  body=request.get_json(force=True);op=float(body['open']);ltp=float(body['ltp']);market=body.get('market','Indices');symbol=str(body.get('symbol','NIFTY 50')).strip().upper();assert market in INDIAN_MARKETS and (symbol in INDIAN_MARKETS[market] or (market!='Indices' and re.fullmatch(r'[A-Z0-9&_.-]{1,25}',symbol)));assert math.isfinite(op) and math.isfinite(ltp) and op>0 and ltp>0
  root=math.sqrt(op);offsets=[.0834,.111,.222,.333,.444,.555,.666,.777,.888,.999];up=[(root+x)**2 for x in offsets];down=[(root-x)**2 for x in offsets]
  status='BUY LEVEL CROSSED — UNCONFIRMED' if ltp>up[0] else 'SELL LEVEL CROSSED — UNCONFIRMED' if ltp<down[0] else 'WAIT'
  return jsonify(status=status,market=market,symbol=symbol,currency='INR',source='Manual input (not live NSE/BSE data)',buy=up[0],sell=down[0],targets=[{'n':i,'up':u,'down':d} for i,(u,d) in enumerate(zip(up[1:],down[1:]),1)])
 except (ValueError,TypeError,KeyError,AssertionError):return jsonify(error='Enter valid positive prices'),400
@app.post('/api/calculate')
def calculate():
 try:
  b=request.get_json(force=True);sh=b['sheet'];cell=b['cell'].upper().replace('$','');assert sh in DATA and re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]*',cell)
  overrides={};inp=b.get('inputCell','').upper().replace('$','');raw=b.get('inputValue','')
  if inp and raw!='':overrides[(sh,inp)]=float(raw)
  cache={}
  def value(s,c,trail=frozenset()):
   key=(s,c)
   if key in overrides:return overrides[key]
   if key in cache:return cache[key]
   if key in trail:raise ValueError('Circular reference')
   if s not in DATA:raise ValueError('Missing sheet')
   fs,cs=DATA[s];formula,saved=fs.get(c,('',cs.get(c,'')))
   if not formula:return float(saved) if saved and _numeric(saved) else saved
   if any(t in formula.upper() for t in ['GOOGLEFINANCE','DUMMYFUNCTION','#REF!','[SHARED','INDIRECT(','VLOOKUP(','SUMPRODUCT(','OFFSET(']):raise ValueError('Unsupported formula')
   expr=formula.replace('^','**');refs={}
   pat=r"(?<![A-Za-z0-9_])(?:(?:'([^']+)'|([A-Za-z][A-Za-z0-9 ]*))!)?(\$?[A-Za-z]{1,3}\$?\d+)(?![A-Za-z0-9_])"
   def sub(m):
    name='ref'+str(len(refs));refs[name]=value(m.group(1) or m.group(2) or s,m.group(3).replace('$',''),trail|{key});return name
   expr=re.sub(pat,sub,expr);node=ast.parse(expr,mode='eval')
   funcs={'SQRT':math.sqrt,'ABS':abs,'ROUND':round,'MAX':max,'MIN':min,'POWER':pow,'INT':math.floor}
   def ev(n):
    if isinstance(n,ast.Expression):return ev(n.body)
    if isinstance(n,ast.Constant) and isinstance(n.value,(float,int)):return n.value
    if isinstance(n,ast.Name) and n.id in refs:return refs[n.id]
    if isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.UAdd,ast.USub)):return ev(n.operand)*(1 if isinstance(n.op,ast.UAdd) else -1)
    if isinstance(n,ast.BinOp) and isinstance(n.op,(ast.Add,ast.Sub,ast.Mult,ast.Div,ast.Pow)):
     a=ev(n.left);b=ev(n.right)
     if isinstance(n.op,ast.Add):return a+b
     if isinstance(n.op,ast.Sub):return a-b
     if isinstance(n.op,ast.Mult):return a*b
     if isinstance(n.op,ast.Div):return a/b
     return a**b
    if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id.upper() in funcs and not n.keywords:return funcs[n.func.id.upper()](*[ev(a) for a in n.args])
    raise ValueError('Unsupported syntax')
   result=ev(node);cache[key]=result;return result
  f,saved=DATA[sh][0].get(cell,('',DATA[sh][1].get(cell,'')))
  try:result=value(sh,cell);mode='calculated'
  except Exception:result=saved;mode='saved Excel value (not recalculated)'
  return jsonify(sheet=sh,cell=cell,formula=('='+f if f else 'No formula'),saved=saved,result=result,mode=mode)
 except Exception as e:return jsonify(error=str(e)),400
def _numeric(s):
 try:float(s);return True
 except: return False
if __name__=='__main__':app.run(debug=False,port=int(os.environ.get('PORT',5000)),host='0.0.0.0')
