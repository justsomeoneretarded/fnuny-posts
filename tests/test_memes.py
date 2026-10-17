import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import shutil
from unittest.mock import Mock,patch
import app,automation,collectors,media,meme_worker
from actions_runner import GitState,StateError

class MemeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=app.DB
        app.DB=Path(self.tmp.name)/'memes.db'
        self.cfg=collectors.config();meme_worker.database().close()
        self.asset=Path(self.tmp.name)/'photo.jpg';self.asset.write_bytes(b'\xff\xd8\xfftest meme')
    def tearDown(self):app.DB=self.old;self.tmp.cleanup()
    def item(self,n=1,source='telegram:chosen',age=0):
        return dict(url='https://t.me/chosen/'+str(n),source=source,published=app.now()-age,caption='A funny caption',kind='image',media_url='https://cdn4.telesco.pe/photo.jpg',dash_url=None,engagement=100)
    def test_url_duplicate_is_only_enqueued_once(self):
        row=self.item();self.assertEqual(meme_worker.ingest([row,row],self.cfg),1)
    def test_old_future_and_untrusted_media_are_rejected(self):
        rows=[self.item(1,age=49*3600),self.item(2,age=-60),self.item(3)|{'media_url':'http://127.0.0.1/photo.jpg'}]
        self.assertEqual(meme_worker.ingest(rows,self.cfg),0)
    def test_preview_never_downloads_or_sends(self):
        meme_worker.ingest([self.item()],self.cfg)
        with patch('meme_worker.download') as download,patch('automation.send') as send:
            result=meme_worker.publish(Mock(),self.cfg)
        self.assertEqual(result['publishing'],'preview');download.assert_not_called();send.assert_not_called()
    def test_reserve_before_send_and_remove_queue_only_after_success(self):
        meme_worker.ingest([self.item()],self.cfg);events=[]
        state=Mock();state.checkpoint.side_effect=lambda:events.append('save')
        def send(*args,**kwargs):
            with meme_worker.database() as c:self.assertEqual(c.execute('SELECT state FROM publications').fetchone()[0],'uncertain')
            events.append('send');return '123'
        with patch('automation.authorization'),patch('meme_worker.download',return_value=self.asset),patch('automation.send',side_effect=send):
            report=meme_worker.publish(state,self.cfg,True)
        self.assertEqual(events,['save','send','save']);self.assertEqual(report['post_id'],'123')
        self.assertEqual(meme_worker.database().execute('SELECT COUNT(*) FROM meme_queue').fetchone()[0],0)
    def test_failed_checkpoint_prevents_post(self):
        meme_worker.ingest([self.item()],self.cfg)
        state=Mock();state.checkpoint.side_effect=RuntimeError
        with patch('automation.authorization'),patch('meme_worker.download',return_value=self.asset),patch('automation.send') as send:
            with self.assertRaises(RuntimeError):meme_worker.publish(state,self.cfg,True)
        send.assert_not_called()
    def test_uncertain_post_never_retries(self):
        meme_worker.ingest([self.item()],self.cfg)
        with patch('automation.authorization'),patch('meme_worker.download',return_value=self.asset),patch('automation.send',side_effect=TimeoutError) as send:
            self.assertFalse(meme_worker.publish(Mock(),self.cfg,True)['ok'])
            self.assertFalse(meme_worker.publish(Mock(),self.cfg,True)['ok'])
        self.assertEqual(send.call_count,1)
    def test_twenty_minute_gap_is_enforced(self):
        meme_worker.ingest([self.item(),self.item(2)],self.cfg)
        with patch('automation.authorization'),patch('meme_worker.download',return_value=self.asset),patch('automation.send',return_value='123') as send:
            meme_worker.publish(Mock(),self.cfg,True)
            self.assertIn('not due',meme_worker.publish(Mock(),self.cfg,True)['publishing'])
        self.assertEqual(send.call_count,1)
    def test_identical_media_from_different_sources_cannot_post_twice(self):
        meme_worker.ingest([self.item(),self.item(2,source='reddit:chosen')],self.cfg)
        with patch('automation.authorization'),patch('meme_worker.download',return_value=self.asset),patch('automation.send',return_value='123') as send:
            meme_worker.publish(Mock(),self.cfg,True)
            with meme_worker.database() as c:c.execute('UPDATE publications SET attempted=?',(app.now()-1201,))
            result=meme_worker.publish(Mock(),self.cfg,True)
        self.assertIn('identical media',result['publishing']);self.assertEqual(send.call_count,1)
    def test_media_failure_never_falls_back_to_text_only(self):
        meme_worker.ingest([self.item()],self.cfg)
        with patch('automation.authorization'),patch('meme_worker.download',side_effect=TimeoutError),patch('automation.send') as send:
            result=meme_worker.publish(Mock(),self.cfg,True)
        self.assertIn('media unavailable',result['publishing']);send.assert_not_called()
    def test_publish_tick_does_not_collect_or_sleep(self):
        cfg=self.cfg|{'telegram':['chosen']};meme_worker.save_setting('last_collection',app.now())
        with patch('collectors.config',return_value=cfg),patch('collectors.collect') as collect,patch('time.sleep') as sleep:
            meme_worker.run(Mock(),live=False)
        collect.assert_not_called();sleep.assert_not_called()
    def test_empty_source_config_is_inactive(self):
        with patch('collectors.collect') as collect,patch('automation.send') as send:
            result=meme_worker.run(Mock(),live=True)
        self.assertIn('not configured',result['publishing']);collect.assert_not_called();send.assert_not_called()
    def test_expired_queue_cleanup_keeps_url_history(self):
        meme_worker.ingest([self.item(age=47*3600)],self.cfg)
        with meme_worker.database() as c:c.execute('UPDATE stories SET published=?',(app.now()-49*3600,))
        meme_worker.cleanup(self.cfg)
        self.assertEqual(meme_worker.database().execute('SELECT COUNT(*) FROM meme_queue').fetchone()[0],0)
        self.assertEqual(meme_worker.database().execute('SELECT COUNT(*) FROM stories').fetchone()[0],1)
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg tools required')
    def test_reddit_separate_audio_is_merged_into_video(self):
        video=Path(self.tmp.name)/'video.mp4';audio=Path(self.tmp.name)/'audio.mp4'
        subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-f','lavfi','-i','color=c=blue:s=64x64:d=0.3','-an','-c:v','libx264',str(video)],check=True,capture_output=True)
        subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-f','lavfi','-i','sine=frequency=400:duration=0.3','-c:a','aac',str(audio)],check=True,capture_output=True)
        row=self.item()|{'kind':'video','dash_url':'https://v.redd.it/test/DASHPlaylist.mpd'}
        manifest=b'<MPD><Period><AdaptationSet mimeType="audio/mp4"><Representation><BaseURL>DASH_AUDIO.mp4</BaseURL></Representation></AdaptationSet></Period></MPD>'
        with patch('media.download_media',side_effect=[video,audio]),patch('media.read_allowed',return_value=manifest):
            merged=meme_worker.download(row,self.tmp.name)
        result=subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=codec_type','-of','json',str(merged)])
        self.assertEqual({s['codec_type'] for s in json.loads(result)['streams']},{'video','audio'})

class CollectorTests(unittest.TestCase):
    def test_captionless_video_is_collected_without_using_poster(self):
        page=b'''<div class="tgme_widget_message" data-post="chosen/1"><time datetime="2026-10-17T12:00:00Z"></time><video src="https://cdn4.telesco.pe/video.mp4" poster="https://cdn4.telesco.pe/photo.jpg"></video></div>'''
        with patch('app.fetch',return_value=page):rows=collectors.telegram('chosen')
        self.assertEqual(rows[0]['kind'],'video');self.assertEqual(rows[0]['caption'],'')
    def test_gambling_ad_is_rejected_even_with_funny_media(self):
        page=b'''<div class="tgme_widget_message" data-post="chosen/1"><time datetime="2026-10-17T12:00:00Z"></time><video src="https://cdn4.telesco.pe/video.mp4"></video><div class="tgme_widget_message_text">Funny clip! Crypto casino.</div></div>'''
        with patch('app.fetch',return_value=page):self.assertEqual(collectors.telegram('chosen'),[])
    def test_caption_removes_footer_and_keeps_paragraphs(self):
        self.assertEqual(collectors.caption('First line\nSecond line\nSubscribe to @channel'),'First line\n\nSecond line')
    def test_reddit_filters_nsfw_and_keeps_dash_audio_manifest(self):
        post=dict(title='Funny moment',score=100,upvote_ratio=.95,created_utc=app.now(),permalink='/r/test/1',
                  media={'reddit_video':{'fallback_url':'https://v.redd.it/abc/DASH_720.mp4','dash_url':'https://v.redd.it/abc/DASHPlaylist.mpd'}})
        data={'data':{'children':[{'data':post},{'data':post|{'over_18':True}}]}}
        with patch('app.fetch',return_value=json.dumps(data).encode()):rows=collectors.reddit('test')
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['dash_url'].endswith('.mpd'))
    def test_media_host_rules_reject_credentials_and_host_suffix_tricks(self):
        for url in ['https://i.redd.it.evil.test/x','http://v.redd.it/x','https://user:pass@v.redd.it/x']:
            self.assertFalse(media.media_allowed(url))
        self.assertTrue(media.media_allowed('https://i.redd.it/photo.jpg'))

class DurableHistoryTests(unittest.TestCase):
    def test_separate_repositories_have_separate_history_and_stale_save_fails(self):
        old=app.DB
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);remote=root/'remote.git'
            def git(*args):subprocess.run(['git',*map(str,args)],check=True,capture_output=True)
            git('init','--bare',remote)
            for name in ['first','second']:
                git('init',root/name);git('-C',root/name,'remote','add','origin',remote)
            try:
                app.DB=root/'one.db';first=GitState(root/'first',initialize=True)
                app.DB=root/'two.db';second=GitState(root/'second')
                meme_worker.save_setting('changed',True)
                second.checkpoint()
                app.DB=root/'one.db'
                with self.assertRaises(StateError):first.checkpoint()
            finally:app.DB=old

if __name__=='__main__':unittest.main()
