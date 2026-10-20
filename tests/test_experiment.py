import importlib.util,json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'lofi_auto'))
from presets import templates
spec=importlib.util.spec_from_file_location('checkpoint',ROOT/'scripts/checkpoint.py')
checkpoint=importlib.util.module_from_spec(spec);spec.loader.exec_module(checkpoint)

class ExperimentTests(unittest.TestCase):
    def test_original_morning_prompts_unchanged(self):
        original=json.loads((ROOT/'lofi_auto/prompt_templates.json').read_text())
        self.assertEqual([j['prompt'] for j in templates(original)], [j['prompt'] for j in original[:6]])
        for profile in ('melodic','dialogue'):
            for job in templates(original,style='ambient',profile=profile):
                self.assertIn('No drums or percussion',job['prompt'])
                self.assertEqual(job['steps'],8)
    def test_checkpoint_restores_code_not_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);runtime=root/'runtime';runtime.mkdir();backup=root/'backup'
            (runtime/'pipeline.py').write_text('working')
            (runtime/'config.env').write_text('LOCAL_CONFIG=1')
            (runtime/'web_data').mkdir();history=runtime/'web_data/history.db';history.write_text('old')
            checkpoint.save(runtime,backup)
            (runtime/'pipeline.py').write_text('experimental');history.write_text('new')
            checkpoint.restore(runtime,backup)
            self.assertEqual((runtime/'pipeline.py').read_text(),'working')
            self.assertEqual(history.read_text(),'new')
            self.assertEqual((backup.stat().st_mode&0o777),0o700)
            (backup/'files/pipeline.py').write_text('corrupted')
            with self.assertRaises(ValueError):checkpoint.restore(runtime,backup)

    def test_experiments_have_distinct_ensembles_and_harmony(self):
        from presets import ENSEMBLES, HARMONIC_COLORS
        original=json.loads((ROOT/'lofi_auto/prompt_templates.json').read_text())
        for style in ENSEMBLES:
            for profile in ('melodic','dialogue'):
                jobs=templates(original,style=style,profile=profile)
                self.assertEqual(len({j['prompt'] for j in jobs}),6)
                for i,j in enumerate(jobs):
                    self.assertIn(ENSEMBLES[style][i%3],j['prompt'])
                    self.assertIn(HARMONIC_COLORS[i],j['prompt'])
