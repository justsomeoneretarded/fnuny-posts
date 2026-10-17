"""Scheduled collection and X publishing with durable duplicate protection."""
import argparse
import json
import os
import time
import app

def authorization():
    """Validate a session locally; never print or save cookie values."""
    cookie = os.environ.get('TWEETKIT_COOKIE')
    if not cookie:
        raise ValueError('Missing credentials: TWEETKIT_COOKIE GitHub Actions secret')
    try:
        from tweetkit_x import TweetKit
        return TweetKit(cookie=cookie, timeout=30)
    except ImportError:
        raise ValueError('Install requirements.txt before using TweetKit-X') from None
    except Exception:
        raise ValueError('Session cookie is incomplete; update TWEETKIT_COOKIE securely') from None


def send(text,image_path=None):
    client=authorization()
    result = client.post(text,image_path=str(image_path)) if image_path else client.post(text)
    if result.get('ok') is not True or not str(result.get('id', '')).isdigit():
        # Upstream response bodies may contain sensitive information. Do not log them.
        raise RuntimeError('TweetKit-X did not confirm a successful post')
    return str(result['id'])


def ledger():
    c = app.connect()
    c.execute('CREATE TABLE IF NOT EXISTS publications (story_id TEXT PRIMARY KEY, attempted REAL NOT NULL, state TEXT NOT NULL, post_id TEXT)')
    c.commit()
    return c
