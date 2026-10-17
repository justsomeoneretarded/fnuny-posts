"""Short meme scheduler: collect every two hours, send at most once per live trigger."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import app
import automation
from actions_runner import GitState, StateError
import collectors
import media

def database():
    c=automation.ledger()
    c.executescript('''CREATE TABLE IF NOT EXISTS meme_queue (
        story_id TEXT PRIMARY KEY,kind TEXT NOT NULL,media_url TEXT NOT NULL,dash_url TEXT);
        CREATE TABLE IF NOT EXISTS media_hashes (digest TEXT PRIMARY KEY,story_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS worker_state (key TEXT PRIMARY KEY,value TEXT NOT NULL);''')
    return c

def setting(key,default=None):
    with database() as c:row=c.execute('SELECT value FROM worker_state WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default

def save_setting(key,value):
    with database() as c:c.execute('INSERT OR REPLACE INTO worker_state VALUES(?,?)',(key,json.dumps(value)))

def ingest(rows,cfg):
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
    return added

def cleanup(cfg):
    with database() as c:
        ids=[r[0] for r in c.execute('''SELECT s.id FROM stories s LEFT JOIN publications p ON s.id=p.story_id
            WHERE (p.state="posted") OR (p.story_id IS NULL AND s.published<=?)''',(app.now()-cfg['queue_max_age_hours']*3600,))]
        for identity in ids:
            c.execute('DELETE FROM meme_queue WHERE story_id=?',(identity,))
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

def publish(state,cfg,live=False):
    with database() as c:
        if c.execute('SELECT 1 FROM publications WHERE state="uncertain"').fetchone():
            return {'ok':False,'publishing':'paused: prior posting outcome uncertain'}
        if c.execute('SELECT COUNT(*) FROM publications WHERE attempted>?',(app.now()-86400,)).fetchone()[0]>=cfg['daily_limit']:
            return {'publishing':'daily limit reached'}
        previous=c.execute('SELECT s.source FROM publications p JOIN stories s ON s.id=p.story_id WHERE p.state="posted" ORDER BY p.attempted DESC LIMIT 1').fetchone()
        rows=[dict(r) for r in c.execute('''SELECT s.*,q.kind,q.media_url,q.dash_url FROM stories s
            JOIN meme_queue q ON q.story_id=s.id LEFT JOIN publications p ON p.story_id=s.id
            WHERE p.story_id IS NULL AND s.status="new" AND s.published>? ORDER BY s.score DESC''',(app.now()-cfg['queue_max_age_hours']*3600,))]
    if not rows:return {'publishing':'queue empty'}
    row=next((r for r in rows if not previous or r['source']!=previous[0]),rows[0])
    if not live:return {'publishing':'preview','draft':row['title'],'kind':row['kind'],'source_url':row['url']}
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
        try:post_id=automation.send(row['title'],image_path=path)
        except Exception:return {'ok':False,'publishing':'paused: posting outcome uncertain'}
        with database() as c:
            c.execute('UPDATE publications SET state="posted",post_id=? WHERE story_id=?',(post_id,row['id']))
            c.execute('DELETE FROM meme_queue WHERE story_id=?',(row['id'],))
            c.execute('UPDATE stories SET status="posted" WHERE id=?',(row['id'],))
        state.checkpoint()
    return {'publishing':'posted','post_id':post_id,'kind':row['kind'],'source_url':row['url'],'queue_payload_removed':True}

def run(state,mode='auto',live=False):
    cfg=collectors.config();report={}
    if not cfg['telegram'] and not cfg['reddit']:
        return {'ok':True,'publishing':'not configured: choose sources before activation'}
    removed=cleanup(cfg)
    last=setting('last_collection',0)
    if mode=='collect' or (mode=='auto' and app.now()-last>=cfg['collection_seconds'] and app.now()-setting('last_collection_attempt',0)>=1800):
        rows,results=collectors.collect(cfg)
        report={'sources':results,'added':ingest(rows,cfg)}
        if any('error' not in r for r in results.values()):save_setting('last_collection',app.now())
        else:report['ok']=False
        save_setting('last_collection_attempt',app.now())
        state.checkpoint()
    elif removed:state.checkpoint()
    if mode!='collect':report['publication']=publish(state,cfg,live)
    report['ok']=report.get('ok',True) and report.get('publication',{}).get('ok',True)
    return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['auto','collect','publish'],default='auto')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--initialize',action='store_true')
    args=parser.parse_args()
    report=run(GitState(app.ROOT,initialize=args.initialize),args.mode,args.live)
    Path('posting-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
    if not report.get('ok',True):raise SystemExit(1)

if __name__=='__main__':
    try:main()
    except Exception:
        print('Meme job stopped. Check the saved history, source access and account connection; no automatic post retry.',flush=True)
        raise SystemExit(1)
