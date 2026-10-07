"""Real short video/thumbnail rendering for every new location, without ACE-Step."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP=Path(__file__).resolve().parents[1]/'lofi_auto'
sys.path.insert(0,str(APP))
import pipeline
from episode_package import prepare
from presets import AUDITION_SCENES

class ContentTests(unittest.TestCase):
    def test_new_locations_render_and_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);cache=root/'cache';cache.mkdir()
            audio=root/'tone.wav'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                'sine=frequency=220:sample_rate=48000:duration=4','-ac','2',str(audio)],check=True)
            for style,scene in AUDITION_SCENES.items():
                with self.subTest(scene=scene):
                    episode=root/scene;work=episode/'work';work.mkdir(parents=True)
                    video=pipeline.make_video(work,cache,4,640,4,audio,scene)
                    info=pipeline.probe(video)
                    self.assertAlmostEqual(float(info['format']['duration']),4,places=1)
                    self.assertEqual({s['codec_type'] for s in info['streams']},{'audio','video'})
                    (episode/'settings.json').write_text(json.dumps({'style':style,'time_of_day':scene}))
                    (episode/'status.json').write_text(json.dumps({'status':'done','duration_seconds':4}))
                    publication=prepare(APP,episode)
                    self.assertNotIn('Café',publication['title'])
                    self.assertTrue((episode/'thumbnail.jpg').is_file())

if __name__=='__main__':unittest.main()
