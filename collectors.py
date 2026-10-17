"""Public Telegram media and Reddit-hosted media; no identity lookups."""
import concurrent.futures
import datetime as dt
import html
import json
import re
from bs4 import BeautifulSoup
import app
import media

PROMOTION=re.compile(r'\b(?:sponsored|paid promotion|promo code|casino|sportsbook|betting|referral|buy now|join our vip|guaranteed returns)\b',re.I)

def config():
    value=json.loads((app.ROOT/'sources.json').read_text())
    for name in value['telegram']+value['reddit']:
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_]{2,40}',name):
            raise ValueError('Invalid channel or subreddit')
    if value['posting_seconds']!=1200 or value['collection_seconds']<1200:
        raise ValueError('Use a 20-minute posting interval and a valid collection interval')
    return value

def caption(text):
    lines=[]
    for line in html.unescape(text).splitlines():
        line=re.sub(r'https?://\S+','',line).strip()
        if re.search(r'\b(?:subscribe|shared by|via @|follow us)\b',line,re.I):continue
        line=re.sub(r'(?<!\w)@[A-Za-z0-9_]+','',line).strip()
        if line:lines.append(line)
    text='\n\n'.join(lines)
    return text if len(text)<=120 else text[:117].rsplit(' ',1)[0]+'…'

def number(text):
    match=re.fullmatch(r'([\d.,]+)([KMB]?)',text.strip(),re.I)
    return int(float(match[1].replace(',',''))*{'':1,'K':1000,'M':1e6,'B':1e9}[match[2].upper()]) if match else 0

def telegram(channel):
    soup=BeautifulSoup(app.fetch('https://t.me/s/'+channel),'html.parser')
    messages=soup.select('.tgme_widget_message[data-post]')
    if not messages:raise ValueError('No public media preview')
    rows=[]
    for message in messages:
        stamp=message.select_one('time[datetime]')
        if not stamp:continue
        date=dt.datetime.fromisoformat(stamp['datetime'].replace('Z','+00:00'))
        if date.tzinfo is None:continue
        body=message.select_one('.tgme_widget_message_text')
        if body:
            for br in body.select('br'):br.replace_with('\n')
        text=body.get_text() if body else ''
        if PROMOTION.search(text):continue
        video=message.select_one('video, .tgme_widget_message_video_player, .tgme_widget_message_video_wrap')
        video_source=message.select_one('video[src], video source[src]')
        photo=message.select_one('.tgme_widget_message_photo_wrap')
        match=re.search(r'''background-image:\s*url\(['"]?(.*?)['"]?\)''',photo.get('style','')) if photo else None
        url=video_source.get('src') if video_source else (match[1] if match and not video else None)
        if not media.media_allowed(url):continue
        views=message.select_one('.tgme_widget_message_views')
        rows.append(dict(url='https://t.me/'+message['data-post'],source='telegram:'+channel,
                         published=date.timestamp(),caption=caption(text),kind='video' if video else 'image',
                         media_url=html.unescape(url),dash_url=None,engagement=number(views.get_text()) if views else 0))
    return rows

def reddit(subreddit):
    data=json.loads(app.fetch('https://www.reddit.com/r/'+subreddit+'/hot.json?limit=50&raw_json=1'))
    cfg=config();rows=[]
    for child in data.get('data',{}).get('children',[]):
        post=child['data']
        if post.get('over_18') or post.get('spoiler') or post.get('stickied') or post.get('removed_by_category') or PROMOTION.search(post.get('title','')):continue
        if post.get('score',0)<cfg['reddit_min_score'] or post.get('upvote_ratio',0)<cfg['reddit_min_upvote_ratio']:continue
        video=(post.get('media') or {}).get('reddit_video')
        url=video.get('fallback_url') if video else post.get('url_overridden_by_dest')
        if not media.media_allowed(url):continue
        rows.append(dict(url='https://www.reddit.com'+post['permalink'],source='reddit:'+subreddit,
                         published=post['created_utc'],caption=caption(post.get('title','')),
                         kind='video' if video else 'image',media_url=html.unescape(url),
                         dash_url=video.get('dash_url') if video and not video.get('is_gif') else None,
                         engagement=post.get('score',0)+2*post.get('num_comments',0)))
    return rows

def collect(cfg):
    jobs=[('telegram:'+n,telegram,n) for n in cfg['telegram']]+[('reddit:'+n,reddit,n) for n in cfg['reddit']]
    results={};rows=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures=[(name,pool.submit(function,arg)) for name,function,arg in jobs]
        for name,future in futures:
            try:
                items=future.result();rows.extend(items);results[name]={'usable_media':len(items)}
            except Exception as error:
                results[name]={'error':type(error).__name__,'detail':'Public source unavailable or format changed'}
    return rows,results
