"""Virality scoring for memes, plus learning from our own posts' engagement.

score = spread + reactions + freshness + video - fatigue (+ AI virality, added by the worker)

- spread: views per hour compared with the same channel's normal (median) pace. A meme
  doing 3x its channel's usual numbers is a real signal; raw views would just favour
  big channels.
- reactions: reactions per view compared with the channel's normal rate.
- freshness: memes age slower than news (6-hour half-life).
- fatigue: small penalty for posting the same channel twice in a row.
Learned per-channel and per-kind weights (0.7-1.4) come from our own posts' metrics.
"""
import math
import statistics


def pace(row, now):
    age = max(0.25, (now-row['published'])/3600)
    views = row.get('views') or 0
    reactions = row.get('reactions') or 0
    return views/age, (reactions/views if views else 0.0)


def baselines(rows, now):
    """Median views/hour and reactions/view per source."""
    by_source = {}
    for row in rows:
        by_source.setdefault(row['source'], []).append(pace(row, now))
    result = {}
    for source, values in by_source.items():
        vph = [v for v, _ in values if v > 0]
        rate = [r for _, r in values if r > 0]
        result[source] = dict(vph=statistics.median(vph) if vph else 0.0,
                              rate=statistics.median(rate) if rate else 0.0)
    return result


def score(row, now, baseline, weights=None, last_source=None):
    weights = weights or {}
    vph, rate = pace(row, now)
    base = baseline.get(row['source'], {'vph': 0.0, 'rate': 0.0})
    rel_views = vph/base['vph'] if base['vph'] > 0 else 1.0
    rel_react = rate/base['rate'] if base['rate'] > 0 and rate > 0 else (1.0 if base['rate'] == 0 else 0.5)
    multiplier = weights.get('source', {}).get(row['source'], 1.0)*weights.get('kind', {}).get(row['kind'], 1.0)
    age = max(0.0, (now-row['published'])/3600)
    parts = dict(
        spread=round(min(45.0, 30*math.log2(1+rel_views))*multiplier, 2),
        reactions=round(min(30.0, 20*math.log2(1+rel_react))*multiplier, 2),
        freshness=round(20*0.5**(age/6), 2),
        video=5 if row['kind'] == 'video' else 0,
        fatigue=-8 if last_source and row['source'] == last_source else 0,
    )
    return round(sum(parts.values()), 2), parts


def ai_adjustment(verdict):
    """Only predicted virality counts: 8 points per step away from a neutral 5."""
    return 8*(verdict['virality']-5)


def engagement(row):
    """Replies and reposts spread a post; weight them above likes."""
    return (row.get('likes') or 0)+3*(row.get('reposts') or 0)+4*(row.get('replies') or 0)


def learn(rows, min_posts=8):
    """Nudge channel and kind weights toward what our audience engages with."""
    report = dict(posts=len(rows))
    if len(rows) < min_posts:
        report['learning'] = 'waiting for more measured posts'
        return None, report
    for row in rows:
        row['score'] = engagement(row)
    overall = sum(r['score'] for r in rows)/len(rows) or 1
    weights = {'source': {}, 'kind': {}}
    for field, target in (('source', 'source'), ('kind', 'kind')):
        for value in sorted({r[field] for r in rows if r.get(field)}):
            scores = [r['score'] for r in rows if r.get(field) == value]
            if len(scores) < 3:
                continue
            ratio = (sum(scores)/len(scores))/overall
            shrink = len(scores)/(len(scores)+10)
            weights[target][value] = round(max(.7, min(1.4, 1+(ratio-1)*shrink)), 3)
    report.update(weights=weights, best=sorted(({'score': r['score'], 'caption': r.get('caption') or ''} for r in rows),
                                                key=lambda r: -r['score'])[:3])
    return weights, report
