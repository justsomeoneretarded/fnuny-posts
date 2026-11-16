"""Short meme scheduler: collect every two hours, send at most once per live trigger."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import app
import automation
from actions_runner import GitState, StateError
import collectors
import judge
import media
import virality

# Quality over noise: a trigger only posts a meme that clears this bar.
MIN_SCORE = float(os.environ.get('MIN_SCORE', '70'))
DAILY_LIMIT = int(os.environ.get('DAILY_LIMIT', '0'))  # 0 = no daily cap (the scheduler controls pace)
AI_PER_TICK = int(os.environ.get('AI_PER_TICK', '4'))
AI_DAILY_CALLS = int(os.environ.get('AI_DAILY_CALLS', '250'))
JUDGE_TOP = 8  # only the best-scoring memes are worth an AI look

def database():
    c=automation.ledger()
    c.executescript('''CREATE TABLE IF NOT EXISTS meme_queue (
        story_id TEXT PRIMARY KEY,kind TEXT NOT NULL,media_url TEXT NOT NULL,dash_url TEXT);
        CREATE TABLE IF NOT EXISTS media_hashes (digest TEXT PRIMARY KEY,story_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS worker_state (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meme_signals (story_id TEXT PRIMARY KEY,views INTEGER NOT NULL DEFAULT 0,
            reactions INTEGER NOT NULL DEFAULT 0,preview_url TEXT,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS meme_verdicts (story_id TEXT PRIMARY KEY,value TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS post_meta (story_id TEXT PRIMARY KEY,post_id TEXT NOT NULL,source TEXT,kind TEXT,
            caption TEXT,score REAL,posted REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS post_metrics (post_id TEXT PRIMARY KEY,likes INTEGER,reposts INTEGER,replies INTEGER,
            checked REAL NOT NULL);''')
    return c

def setting(key,default=None):
    with database() as c:row=c.execute('SELECT value FROM worker_state WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default

def save_setting(key,value):
    with database() as c:c.execute('INSERT OR REPLACE INTO worker_state VALUES(?,?)',(key,json.dumps(value)))

def ingest(rows,cfg):
    """Queue new memes and refresh view/reaction counts of ones we already know."""
    added=0;end=app.now()
    with database() as c:
        for row in rows:
            if not end-cfg['source_max_age_hours']*3600<row['published']<=end:continue
            if row['kind'] not in ('video','image') or not media.media_allowed(row['media_url']):continue
            identity=hashlib.sha256(row['url'].encode()).hexdigest()[:24]
            age=max(0,(end-row['published'])/3600)
            score=math.log1p(max(0,row['engagement']))/(1+age/12)
            inserted=c.execute('INSERT OR IGNORE INTO stories VALUES(?,?,?,?,?,?,?,?)',
                (identity,row['caption'],row['url'],row['source'],row['published'],end,score,'new')).rowcount
            if inserted:
                c.execute('INSERT INTO meme_queue VALUES(?,?,?,?)',(identity,row['kind'],row['media_url'],row.get('dash_url')))
                added+=1
            # Growth since the last collection is the virality signal, so always refresh.
            c.execute('''INSERT INTO meme_signals VALUES(?,?,?,?,?) ON CONFLICT(story_id) DO UPDATE SET
                views=MAX(views,excluded.views),reactions=MAX(reactions,excluded.reactions),
                preview_url=COALESCE(excluded.preview_url,preview_url),updated=excluded.updated''',
                (identity,int(row.get('engagement') or 0),int(row.get('reactions') or 0),row.get('preview_url'),end))
    return added

def cleanup(cfg):
    with database() as c:
        ids=[r[0] for r in c.execute('''SELECT s.id FROM stories s LEFT JOIN publications p ON s.id=p.story_id
            WHERE (p.state="posted") OR (p.story_id IS NULL AND s.published<=?)''',(app.now()-cfg['queue_max_age_hours']*3600,))]
        for identity in ids:
            c.execute('DELETE FROM meme_queue WHERE story_id=?',(identity,))
            c.execute('DELETE FROM meme_verdicts WHERE story_id=?',(identity,))
            c.execute('UPDATE stories SET status="archived" WHERE id=?',(identity,))
    return len(ids)

def download(row,directory):
    path=media.download_media(row['media_url'],directory,video=row['kind']=='video')
    if row['dash_url']:
        if urllib.parse.urlsplit(row['dash_url']).hostname!='v.redd.it':raise ValueError('Unsupported audio manifest')
        # Reddit fallback MP4 often has no audio. Preserve an available DASH audio track.
        root=ET.fromstring(media.read_allowed(row['dash_url'],500_000))
        audio=[]
        for adaptation in root.iter():
            if adaptation.tag.split('}')[-1]=='AdaptationSet' and ('audio' in adaptation.get('mimeType','') or adaptation.get('contentType')=='audio'):
                audio.extend(n.text for n in adaptation.iter() if n.tag.split('}')[-1]=='BaseURL' and n.text)
        if audio:
            url=urllib.parse.urljoin(row['dash_url'],audio[0])
            if urllib.parse.urlsplit(url).hostname!='v.redd.it':raise ValueError('Unsupported audio host')
            audio_path=media.download_media(url,Path(directory)/'audio',video=True)
            merged=Path(directory)/'with-audio.mp4'
            result=subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-i',str(path),'-i',str(audio_path),
                '-c','copy','-shortest',str(merged)],capture_output=True,timeout=60)
            if result.returncode or not merged.exists():raise ValueError('Audio merge failed')
            if merged.stat().st_size>20_000_000:raise ValueError('Merged video too large')
            path=merged
    return path

def candidates(cfg):
    """Queued memes with their virality score parts, best first."""
    now=app.now()
    with database() as c:
        previous=c.execute('SELECT s.source FROM publications p JOIN stories s ON s.id=p.story_id WHERE p.state="posted" ORDER BY p.attempted DESC LIMIT 1').fetchone()
        rows=[dict(r) for r in c.execute('''SELECT s.*,q.kind,q.media_url,q.dash_url,COALESCE(g.views,0) AS views,
            COALESCE(g.reactions,0) AS reactions,g.preview_url,v.value AS verdict FROM stories s
            JOIN meme_queue q ON q.story_id=s.id LEFT JOIN publications p ON p.story_id=s.id
            LEFT JOIN meme_signals g ON g.story_id=s.id LEFT JOIN meme_verdicts v ON v.story_id=s.id
            WHERE p.story_id IS NULL AND s.status="new" AND s.published>?''',(now-cfg['queue_max_age_hours']*3600,))]
        recent=[dict(r) for r in c.execute('''SELECT s.source,s.published,COALESCE(g.views,0) AS views,
            COALESCE(g.reactions,0) AS reactions FROM stories s LEFT JOIN meme_signals g ON g.story_id=s.id
            WHERE s.published>?''',(now-48*3600,))]
    baseline=virality.baselines(recent,now)
    weights=setting('learned_weights',{})
    for row in rows:
        row['rule_score'],row['parts']=virality.score(row,now,baseline,weights,previous[0] if previous else None)
        row['verdict']=json.loads(row['verdict']) if row['verdict'] else None
        verdict=row['verdict'] if row['verdict'] and 'error' not in row['verdict'] else None
        row['total']=round(row['rule_score']+(virality.ai_adjustment(verdict) if verdict else 0),2)
        row['declined']=bool(verdict and not verdict['post'])
        row['text']=verdict['caption'] if verdict else row['title']
    return sorted(rows,key=lambda r:-r['total'])

def judge_top(cfg):
    """AI-judge the best unjudged memes (bounded per run and per day; verdicts cached)."""
    report={'enabled':judge.available(),'calls':0,'errors':[]}
    if not judge.available():return report
    usage=setting('ai_usage',{});day=int(app.now()//86400)
    used=usage.get('count',0) if usage.get('day')==day else 0
    saved=setting('ai_models',{})
    if app.now()-saved.get('saved',0)<86400:judge.remember(saved)
    rows=[r for r in candidates(cfg) if r['verdict'] is None or ('error' in r['verdict'] and app.now()-r['verdict'].get('at',0)>1800)]
    for row in rows[:JUDGE_TOP]:
        if report['calls']>=AI_PER_TICK or used+report['calls']>=AI_DAILY_CALLS:break
        preview=row['preview_url'] or (row['media_url'] if row['kind']=='image' else None)
        if not preview:continue
        try:
            image=media.read_allowed(preview,2_500_000,10)
        except Exception:
            continue  # no still frame available; the meme can still be ranked by its numbers
        report['calls']+=1
        try:
            verdict=judge.judge(image,row['title'],row['kind'])
        except judge.JudgeUnavailable as error:
            verdict={'error':str(error)[:300],'at':app.now()}
            report['errors'].append(verdict['error'])
        with database() as c:
            c.execute('INSERT OR REPLACE INTO meme_verdicts VALUES(?,?,?)',(row['id'],json.dumps(verdict),app.now()))
    if report['calls']:
        save_setting('ai_usage',{'day':day,'count':used+report['calls']})
        working=judge.remembered()
        if any(working.values()):save_setting('ai_models',dict(working,saved=app.now()))
    return report

def publish(state,cfg,live=False):
    with database() as c:
        if c.execute('SELECT 1 FROM publications WHERE state="uncertain"').fetchone():
            return {'ok':False,'publishing':'paused: prior posting outcome uncertain'}
        if DAILY_LIMIT and c.execute('SELECT COUNT(*) FROM publications WHERE attempted>?',(app.now()-86400,)).fetchone()[0]>=DAILY_LIMIT:
            return {'publishing':'daily limit reached'}
    rows=[r for r in candidates(cfg) if not r['declined']]
    if not rows:return {'publishing':'queue empty'}
    row=rows[0]
    summary={'score':row['total'],'score_bar':MIN_SCORE,'parts':row['parts'],
             'ai':{k:row['verdict'][k] for k in ('virality','reason','provider') if k in row['verdict']} if row['verdict'] else None}
    if row['total']<MIN_SCORE:
        return {'publishing':'nothing clears the quality bar yet','best_draft':row['text'],'kind':row['kind'],'source_url':row['url'],**summary}
    if not live:return {'publishing':'preview','draft':row['text'],'kind':row['kind'],'source_url':row['url'],**summary}
    automation.authorization()
    with tempfile.TemporaryDirectory() as d:
        try:path=download(row,d)
        except Exception:
            # No media means no post; don't block every subsequent slot on a broken URL.
            with database() as c:
                c.execute('DELETE FROM meme_queue WHERE story_id=?',(row['id'],))
                c.execute('UPDATE stories SET status="unavailable" WHERE id=?',(row['id'],))
            state.checkpoint()
            return {'publishing':'media unavailable; skipped without posting','source_url':row['url']}
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with database() as c:
            if c.execute('SELECT 1 FROM media_hashes WHERE digest=?',(digest,)).fetchone():
                c.execute('DELETE FROM meme_queue WHERE story_id=?',(row['id'],))
                c.execute('UPDATE stories SET status="duplicate" WHERE id=?',(row['id'],))
                duplicate=True
            else:
                duplicate=False
                c.execute('INSERT INTO publications VALUES(?,?,?,NULL)',(row['id'],app.now(),'uncertain'))
                c.execute('INSERT INTO media_hashes VALUES(?,?)',(digest,row['id']))
        state.checkpoint() # A failed save prevents public writes.
        if duplicate:return {'publishing':'identical media already used; skipped'}
        try:post_id=automation.send(row['text'],image_path=path)
        except Exception:return {'ok':False,'publishing':'paused: posting outcome uncertain'}
        with database() as c:
            c.execute('UPDATE publications SET state="posted",post_id=? WHERE story_id=?',(post_id,row['id']))
            c.execute('DELETE FROM meme_queue WHERE story_id=?',(row['id'],))
            c.execute('UPDATE stories SET status="posted" WHERE id=?',(row['id'],))
            c.execute('INSERT OR REPLACE INTO post_meta VALUES(?,?,?,?,?,?,?)',(row['id'],post_id,row['source'],row['kind'],row['text'],row['total'],app.now()))
        state.checkpoint()
    return {'publishing':'posted','post_id':post_id,'kind':row['kind'],'caption':row['text'],'source_url':row['url'],'queue_payload_removed':True,**summary}

def track_and_learn(limit=10,budget=30):
    """Hourly: read our recent posts' public metrics and update channel/kind weights."""
    if app.now()-setting('last_metrics',0)<3600:return None
    save_setting('last_metrics',app.now())
    with database() as c:
        posts=[r[0] for r in c.execute('''SELECT m.post_id FROM post_meta m LEFT JOIN post_metrics x ON x.post_id=m.post_id
            WHERE m.posted>? ORDER BY COALESCE(x.checked,0),m.posted DESC LIMIT ?''',(app.now()-3*86400,limit))]
    checked=errors=0
    if posts:
        client=automation.authorization();deadline=app.now()+budget
        for post_id in posts:
            if app.now()>=deadline:break
            try:
                tweet=client.get_tweet(post_id)
                if tweet.get('ok') is not True:raise RuntimeError('metrics unavailable')
            except Exception:
                errors+=1;continue
            with database() as c:
                c.execute('INSERT OR REPLACE INTO post_metrics VALUES(?,?,?,?,?)',(post_id,tweet.get('likes') or 0,tweet.get('retweets') or 0,tweet.get('replies') or 0,app.now()))
            checked+=1
    with database() as c:
        rows=[dict(r) for r in c.execute('''SELECT m.source,m.kind,m.caption,x.likes,x.reposts,x.replies FROM post_meta m
            JOIN post_metrics x ON x.post_id=m.post_id WHERE m.posted BETWEEN ? AND ?''',(app.now()-14*86400,app.now()-3*3600))]
    weights,report=virality.learn(rows)
    if weights is not None:save_setting('learned_weights',weights)
    return dict(report,checked=checked,errors=errors)

def run(state,mode='auto',live=False):
    cfg=collectors.config();report={}
    if not cfg['telegram'] and not cfg['reddit']:
        return {'ok':True,'publishing':'not configured: choose sources before activation'}
    removed=cleanup(cfg)
    last=setting('last_collection',0)
    # Small tolerance: an external 20-minute trigger can arrive a little early.
    if mode=='collect' or (mode=='auto' and app.now()-last>=cfg['collection_seconds']-180 and app.now()-setting('last_collection_attempt',0)>=300):
        rows,results=collectors.collect(cfg)
        report={'sources':results,'added':ingest(rows,cfg)}
        if any('error' not in r for r in results.values()):save_setting('last_collection',app.now())
        else:report['ok']=False
        save_setting('last_collection_attempt',app.now())
        state.checkpoint()
    elif removed:state.checkpoint()
    if mode!='collect':
        report['ai']=judge_top(cfg)
        if report['ai']['calls']:state.checkpoint()
        report['publication']=publish(state,cfg,live)
        if live:
            try:
                learning=track_and_learn()
                if learning is not None:
                    report['learning']=learning;state.checkpoint()
            except Exception as error:
                report['learning']={'error':type(error).__name__}
    report['ok']=report.get('ok',True) and report.get('publication',{}).get('ok',True)
    return report

def check_ai():
    """Diagnostic only: judge one tiny generated image with each configured provider."""
    import struct,zlib
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    pixel=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',8,8,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(b'\x00'+b'\xff\xcc\x00'*8 for _ in range(8))))+chunk(b'IEND',b'')
    report={}
    for name in ('gemini','groq'):
        if not os.environ.get(name.upper()+'_API_KEY'):
            report[name]='no key';continue
        try:
            raw=judge._json({'gemini':judge.gemini,'groq':judge.groq}[name]('Media type: image\nOriginal caption: (none)',pixel))
            state=judge._gemini if name=='gemini' else judge._groq
            report[name]={'ok':True,'model':state['model'],'sample':raw}
        except judge.JudgeUnavailable as error:
            report[name]={'ok':False,'error':str(error)}
    return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['auto','collect','publish','check-ai'],default='auto')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--initialize',action='store_true')
    args=parser.parse_args()
    if args.mode=='check-ai':
        report={'ai_check':check_ai()}
        print(json.dumps(report,indent=2),flush=True)
        if not any(v.get('ok') for v in report['ai_check'].values() if isinstance(v,dict)):raise SystemExit(1)
        return
    report=run(GitState(app.ROOT,initialize=args.initialize),args.mode,args.live)
    Path('posting-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
    if not report.get('ok',True):raise SystemExit(1)

if __name__=='__main__':
    try:main()
    except Exception:
        print('Meme job stopped. Check the saved history, source access and account connection; no automatic post retry.',flush=True)
        raise SystemExit(1)
