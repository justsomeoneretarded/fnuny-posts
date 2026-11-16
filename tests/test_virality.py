import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
import collectors
import judge
import meme_worker
import virality


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = app.DB
        app.DB = Path(self.tmp.name)/'memes.db'
        self.cfg = collectors.config()
        meme_worker.database().close()
        self.asset = Path(self.tmp.name)/'photo.jpg'
        self.asset.write_bytes(b'\xff\xd8\xffmeme')
        for key, value in dict(MIN_SCORE=70.0, DAILY_LIMIT=0).items():
            p = patch.object(meme_worker, key, value)
            p.start()
            self.addCleanup(p.stop)
        keys = patch.dict('os.environ', {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''})
        keys.start()
        self.addCleanup(keys.stop)

    def tearDown(self):
        app.DB = self.old
        self.tmp.cleanup()

    def item(self, n, source='telegram:bestmemes', views=1000, reactions=20, age=3600, kind='image'):
        return dict(url='https://t.me/x/%d' % n, source=source, published=app.now()-age, caption='caption %d' % n,
                    kind=kind, media_url='https://cdn4.telesco.pe/%d.jpg' % n, dash_url=None,
                    engagement=views, reactions=reactions, preview_url='https://cdn4.telesco.pe/%d.jpg' % n)


class CollectorTests(Case):
    def test_reactions_and_video_still_are_collected(self):
        page = b'''<div class="tgme_widget_message" data-post="chosen/1"><time datetime="2026-10-17T12:00:00Z"></time>
        <i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn4.telesco.pe/thumb.jpg')"></i>
        <video src="https://cdn4.telesco.pe/video.mp4"></video>
        <span class="tgme_widget_message_views">12.5K</span>
        <div class="tgme_widget_message_reactions"><span class="tgme_reaction"><i class="emoji">x</i>1.2K</span>
        <span class="tgme_reaction"><i class="emoji">y</i>300</span></div></div>'''
        with patch('app.fetch', return_value=page):
            row = collectors.telegram('chosen')[0]
        self.assertEqual((row['kind'], row['engagement'], row['reactions']), ('video', 12500, 1500))
        self.assertEqual(row['preview_url'], 'https://cdn4.telesco.pe/thumb.jpg')
        self.assertEqual(row['media_url'], 'https://cdn4.telesco.pe/video.mp4')

    def test_repeat_collection_refreshes_counts(self):
        meme_worker.ingest([self.item(1, views=100, reactions=1)], self.cfg)
        meme_worker.ingest([self.item(1, views=900, reactions=40)], self.cfg)
        with meme_worker.database() as c:
            self.assertEqual(tuple(c.execute('SELECT views,reactions FROM meme_signals').fetchone()), (900, 40))


class ScoringTests(Case):
    def test_standout_meme_beats_average_post_from_bigger_channel(self):
        rows = [self.item(i, 'telegram:big', views=50000, reactions=500) for i in range(5)]
        rows += [self.item(10+i, 'telegram:small', views=1000, reactions=10) for i in range(4)]
        rows.append(self.item(99, 'telegram:small', views=5000, reactions=150))
        meme_worker.ingest(rows, self.cfg)
        best = meme_worker.candidates(self.cfg)[0]
        self.assertEqual(best['url'], 'https://t.me/x/99')

    def test_fresh_beats_stale_and_same_channel_twice_is_penalised(self):
        now = app.now()
        base = {'telegram:a': {'vph': 1000, 'rate': .02}}
        fresh = dict(source='telegram:a', kind='image', published=now-1800, views=500, reactions=10)
        stale = dict(fresh, published=now-30*3600, views=30000, reactions=600)
        self.assertGreater(virality.score(fresh, now, base)[0], virality.score(stale, now, base)[0])
        self.assertEqual(virality.score(fresh, now, base, last_source='telegram:a')[1]['fatigue'], -8)

    def test_ai_virality_is_the_only_ai_factor(self):
        self.assertEqual(virality.ai_adjustment({'virality': 9}), 32)
        self.assertEqual(virality.ai_adjustment({'virality': 2}), -24)


class PublishTests(Case):
    def test_bar_blocks_weak_memes_and_preview_shows_best(self):
        meme_worker.ingest([self.item(1, views=10, reactions=0, age=40*3600)], self.cfg)
        result = meme_worker.publish(Mock(), self.cfg)
        self.assertEqual(result['publishing'], 'nothing clears the quality bar yet')
        self.assertIn('best_draft', result)

    def test_ai_verdict_caption_is_posted_and_declined_memes_skipped(self):
        meme_worker.ingest([self.item(1), self.item(2, views=1100)], self.cfg)
        verdicts = {'caption 1': dict(post=False, virality=2, caption='', reason='not funny', provider='gemini'),
                    'caption 2': dict(post=True, virality=9, caption='me pretending to work', reason='relatable', provider='gemini')}
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'k'}), patch('media.read_allowed', return_value=b'\xff\xd8\xff'), \
                patch('judge.judge', side_effect=lambda image, caption, kind: verdicts[caption]):
            report = meme_worker.judge_top(self.cfg)
        self.assertEqual(report['calls'], 2)
        with patch('automation.authorization'), patch('meme_worker.download', return_value=self.asset), \
                patch('automation.send', return_value='77') as send:
            result = meme_worker.publish(Mock(), self.cfg, True)
        send.assert_called_once_with('me pretending to work', image_path=self.asset)
        self.assertEqual(result['ai']['virality'], 9)
        with meme_worker.database() as c:
            self.assertEqual(c.execute('SELECT caption,source FROM post_meta').fetchone()[0], 'me pretending to work')
        # Verdicts are cached: nothing is judged twice.
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'k'}), patch('judge.judge') as again:
            meme_worker.judge_top(self.cfg)
        again.assert_not_called()

    def test_ai_errors_are_retried_later_not_cached_forever(self):
        meme_worker.ingest([self.item(1)], self.cfg)
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'k'}), patch('media.read_allowed', return_value=b'\xff\xd8\xff'), \
                patch('judge.judge', side_effect=judge.JudgeUnavailable('HTTP 503 busy')):
            self.assertEqual(len(meme_worker.judge_top(self.cfg)['errors']), 1)
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'k'}), patch('media.read_allowed', return_value=b'\xff\xd8\xff'), \
                patch('judge.judge', return_value=dict(post=True, virality=7, caption='', reason='', provider='groq')) as again:
            meme_worker.judge_top(self.cfg)
            again.assert_not_called()
            with patch('app.now', return_value=app.now()+1900):
                meme_worker.judge_top(self.cfg)
            again.assert_called_once()

    def test_optional_daily_limit(self):
        with meme_worker.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)', ('x', app.now()-60, 'posted', '1'))
        meme_worker.ingest([self.item(1)], self.cfg)
        with patch.object(meme_worker, 'DAILY_LIMIT', 1):
            self.assertEqual(meme_worker.publish(Mock(), self.cfg)['publishing'], 'daily limit reached')


class JudgeTests(unittest.TestCase):
    def setUp(self):
        judge._gemini.update(model=None, thinking=None, down=False, discovered=False, bad=set())
        judge._groq.update(model=None, listed=None)
        self.addCleanup(lambda: (judge._gemini.update(model=None, thinking=None, down=False, discovered=False, bad=set()),
                                 judge._groq.update(model=None, listed=None)))

    def test_caption_rules_enforced_locally(self):
        self.assertEqual(judge.clean_caption('"me at 3am 😭😭 #relatable @someone https://x.co/a"'), 'me at 3am 😭')
        self.assertLessEqual(judge.weight(judge.clean_caption('word '*60)), judge.MAX_CAPTION+1)

    def test_busy_gemini_model_moves_to_next_and_sends_image(self):
        seen = []
        def request(url, headers, body=None, timeout=60):
            seen.append(url)
            if 'gemini-3.5-flash:' in url:
                raise judge.HTTPFailure(503, 'HTTP 503 high demand')
            self.assertIn('inline_data', body['contents'][0]['parts'][0])
            return {'candidates': [{'content': {'parts': [{'text': json.dumps({'post': True, 'virality': 8, 'caption': 'lol'})}]}}]}
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'k', 'GROQ_API_KEY': ''}), patch('judge._request', side_effect=request):
            result = judge.judge(b'\xff\xd8\xffimg', 'orig')
        self.assertEqual((result['virality'], result['caption'], result['provider']), (8, 'lol', 'gemini'))
        self.assertEqual(judge.remembered()['gemini_model'], 'gemini-3.6-flash')

    def test_groq_vision_fallback(self):
        def request(url, headers, body=None, timeout=60):
            if url.endswith('/models'):
                return {'data': [{'id': 'llama-3.3-70b-versatile'}, {'id': 'meta-llama/llama-4-maverick-17b-128e-instruct'}]}
            if body['model'] == judge.GROQ_VISION[0]:
                raise judge.HTTPFailure(404, 'HTTP 404 decommissioned')
            self.assertEqual(body['messages'][1]['content'][1]['type'], 'image_url')
            return {'choices': [{'message': {'content': '{"post": false, "virality": 3, "caption": ""}'}}]}
        with patch.dict('os.environ', {'GEMINI_API_KEY': '', 'GROQ_API_KEY': 'k'}), patch('judge._request', side_effect=request):
            result = judge.judge(b'\x89PNGimg', '')
        self.assertFalse(result['post'])
        self.assertEqual(judge._groq['model'], 'meta-llama/llama-4-maverick-17b-128e-instruct')


class LearningTests(Case):
    def test_weights_follow_engagement_within_bounds(self):
        rows = [dict(source='telegram:a', kind='video', likes=200, reposts=20, replies=10) for _ in range(6)]
        rows += [dict(source='telegram:b', kind='image', likes=3, reposts=0, replies=0) for _ in range(6)]
        weights, report = virality.learn(rows)
        self.assertGreater(weights['source']['telegram:a'], 1)
        self.assertLess(weights['source']['telegram:b'], 1)
        self.assertTrue(all(.7 <= w <= 1.4 for group in weights.values() for w in group.values()))

    def test_metrics_read_at_most_hourly(self):
        with meme_worker.database() as c:
            c.execute('INSERT INTO post_meta VALUES(?,?,?,?,?,?,?)', ('s', '99', 'telegram:a', 'image', 'cap', 80, app.now()-7200))
        client = Mock()
        client.get_tweet.return_value = {'ok': True, 'likes': 5, 'retweets': 1, 'replies': 2}
        with patch('automation.authorization', return_value=client):
            self.assertEqual(meme_worker.track_and_learn()['checked'], 1)
            self.assertIsNone(meme_worker.track_and_learn())
        with meme_worker.database() as c:
            self.assertEqual(tuple(c.execute('SELECT likes,reposts,replies FROM post_metrics').fetchone()), (5, 1, 2))


if __name__ == '__main__':
    unittest.main()
