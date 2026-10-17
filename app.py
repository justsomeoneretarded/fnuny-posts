"""Shared paths, HTTP reads and isolated SQLite history for the meme account."""
import datetime as dt
import os
from pathlib import Path
import sqlite3
import urllib.request

ROOT=Path(__file__).resolve().parent
DB=Path(os.environ.get('TRENDING_DB',ROOT/'.local/memes.db'))
UTC=dt.timezone.utc

def now():
    return dt.datetime.now(UTC).timestamp()

def fetch(url):
    request=urllib.request.Request(url,headers={'User-Agent':'fnuny-posts/1.0 (public source collection)'})
    with urllib.request.urlopen(request,timeout=15) as response:
        data=response.read(4_000_001)
    if len(data)>4_000_000:
        raise ValueError('Source response too large')
    return data

def connect():
    DB.parent.mkdir(parents=True,exist_ok=True)
    connection=sqlite3.connect(DB,timeout=30)
    connection.row_factory=sqlite3.Row
    connection.execute('''CREATE TABLE IF NOT EXISTS stories (
        id TEXT PRIMARY KEY,title TEXT NOT NULL,url TEXT UNIQUE NOT NULL,source TEXT NOT NULL,
        published REAL NOT NULL,collected REAL NOT NULL,score REAL NOT NULL,status TEXT NOT NULL DEFAULT 'new')''')
    connection.commit()
    return connection
