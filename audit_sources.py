"""Read-only source survey. Errors and missing engagement stay explicit."""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import statistics
import re
from bs4 import BeautifulSoup
import app
from collectors import number

TELEGRAM=['bestmemes','meme','memee','memes','funnyvideos','funny_videos','epicfails','comedy','notmemes','videomemes',
          'funnyshit','funnyvids','funnyanimals','instantregret','unexpected','perfectlycutscreams','funnyasf',
          'shitpost','shitposting','memestime','memetube','funnymemesdaily','MemeHub','DailyMemes','FunnyChannel',
          'funnyclips','Funnys','funnymeme','funnyfail','funnyvideoss','Memes_Hub','humor','humour','jokes',
          'FunnyCats','funnydogs','AnimalMemes','animals','WholesomeMemes','MemeWorld','englishmemes',
          'redditmemes','r_memes','memesreddit','funnypictures','funny_pics','funnyvideo','funnyvines','vines','bestfunny',
          'laugh','laughing','lol','lolmemes','memesonly','memesfunny','memes_english','memesenglish',
          'thedankestmemes','shitposts','MDK','lepragram','yaplakal']
REDDIT=['funny','memes','Unexpected','therewasanattempt','AnimalsBeingDerps','ContagiousLaughter',
        'PerfectlyCutScreams','WatchPeopleDieInside','instant_regret','funnyvideos']

def telegram(name):
    soup=BeautifulSoup(app.fetch('https://t.me/s/'+name),'html.parser')
    title=soup.select_one('.tgme_channel_info_header_title')
    counter=soup.select_one('.tgme_channel_info_counter')
    messages=soup.select('.tgme_widget_message[data-post]');samples=[]
    for message in messages:
        stamp=message.select_one('time[datetime]')
        if not stamp:continue
        date=dt.datetime.fromisoformat(stamp['datetime'].replace('Z','+00:00'))
        text=message.select_one('.tgme_widget_message_text');views=message.select_one('.tgme_widget_message_views')
        reactions=0
        for reaction in message.select('.tgme_reaction'):
            values=re.findall(r'[\d.,]+[KMB]?',reaction.get_text(' ',strip=True),re.I)
            if values:reactions+=number(values[-1])
        samples.append(dict(source_post=message['data-post'],published=date.timestamp(),reactions=reactions,
            views=number(views.get_text()) if views else None,
            caption=text.get_text(' ',strip=True)[:200] if text else '',
            video=bool(message.select_one('video, .tgme_widget_message_video_player')),
            image=bool(message.select_one('.tgme_widget_message_photo_wrap'))))
    timezone=dt.timezone(dt.timedelta(hours=3));today=dt.datetime.fromtimestamp(app.now(),timezone).date()
    counts=sum(dt.datetime.fromtimestamp(r['published'],timezone).date()==today for r in samples)
    mature=[r for r in samples if r['views'] is not None and 7200<app.now()-r['published']<48*3600]
    return dict(platform='telegram',name=name,url='https://t.me/'+name,
                title=title.get_text(' ',strip=True) if title else None,
                subscribers_label=counter.get_text(' ',strip=True) if counter else None,
                sampled_posts_today=counts,latest_post=max((r['published'] for r in samples),default=None),
                median_views_2_to_48h=statistics.median(r['views'] for r in mature) if mature else None,
                median_reactions_2_to_48h=statistics.median(r['reactions'] for r in mature) if mature else None,
                interaction_verified=any(r['reactions'] for r in samples),samples=samples[-5:],
                status='public preview verified' if messages else 'no public channel preview')

def reddit(name):
    data=json.loads(app.fetch('https://www.reddit.com/r/'+name+'/new.json?limit=100&raw_json=1'))
    posts=[r['data'] for r in data.get('data',{}).get('children',[]) if not r['data'].get('stickied')]
    recent=[r for r in posts if app.now()-r['created_utc']<86400]
    return dict(platform='reddit',name=name,url='https://www.reddit.com/r/'+name+'/',
                sampled_posts_last24h=len(recent),median_score=statistics.median(r['score'] for r in recent) if recent else None,
                median_comments=statistics.median(r['num_comments'] for r in recent) if recent else None,
                status='public feed verified',humor_quality='not manually reviewed')

def survey():
    def check(platform,name):
        try:return globals()[platform](name)
        except Exception as error:return dict(platform=platform,name=name,status='unverified',error=type(error).__name__)
    jobs=[('telegram',n) for n in TELEGRAM]+[('reddit',n) for n in REDDIT]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda pair:check(*pair),jobs))
    return {'checked_at_utc':dt.datetime.fromtimestamp(app.now(),app.UTC).isoformat(),'display_timezone':'Africa/Nairobi',
            'note':'Sample counts are lower bounds; views are reach, not reactions/comments. Humour quality is unreviewed.', 'sources':results}

if __name__=='__main__':
    report=survey()
    Path('docs/source-audit.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    for row in report['sources']:
        print(json.dumps({k:v for k,v in row.items() if k!='samples'},ensure_ascii=False),flush=True)
