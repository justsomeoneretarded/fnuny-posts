"""Bounded media downloads with allowlisted redirects and content signatures."""
from pathlib import Path
import urllib.parse
import urllib.request

def media_allowed(url):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ''
        return p.scheme=='https' and not p.username and not p.password and p.port in (None,443) and (
            host in ('cdn-telegram.org','telesco.pe','i.redd.it','preview.redd.it','v.redd.it') or
            host.endswith('.telesco.pe') or
            host.endswith('.cdn-telegram.org'))
    except (ValueError, TypeError):
        return False


def read_allowed(url,limit,timeout=15):
    if not media_allowed(url):
        raise ValueError('Unsupported media host')
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not media_allowed(newurl):
                raise ValueError('Unsupported media redirect')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    req = urllib.request.Request(url,headers={'User-Agent':'fnuny-posts/1.0'})
    with urllib.request.build_opener(Redirect).open(req,timeout=timeout) as r:
        data = r.read(limit+1)
    if len(data)>limit:
        raise ValueError('Media too large')
    return data


def download_media(url, directory, video=False):
    data=read_allowed(url,20_000_000 if video else 2_500_000,30 if video else 10)
    if video:
        if len(data)<12 or data[4:8]!=b'ftyp':
            raise ValueError('Unsupported video content')
        suffix='.mp4'
    elif data.startswith(b'\xff\xd8\xff'):
        suffix='.jpg'
    elif data.startswith(b'\x89PNG\r\n\x1a\n'):
        suffix='.png'
    elif data[:6] in (b'GIF87a',b'GIF89a'):
        suffix='.gif'
    elif data[:4]==b'RIFF' and data[8:12]==b'WEBP':
        suffix='.webp'
    else:
        raise ValueError('Unsupported image content')
    path=Path(directory)/('image'+suffix)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(data)
    return path
