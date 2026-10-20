import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LaunchProfileTests(unittest.TestCase):
    def launch(self, profile, preflight_exit=0):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(ROOT / 'lofi_auto/run_ace.sh', root / 'run_ace.sh')
            ace = root / 'ace'
            (ace / '.venv/bin').mkdir(parents=True)
            patch = ace / 'acestep/core/generation/handler/generate_music_decode.py'
            patch.parent.mkdir(parents=True)
            patch.write_text('_decode_generate_music_vae_cuda_fp32')
            # Substitute executables only: exercise the real launcher and config loading.
            for name, body in [('python', f'exit {preflight_exit}'), ('acestep',
                    'printf "VAE_GPU=%s VAE_CPU=%s DTYPE=%s\\n" "$ACESTEP_VAE_ON_GPU" "$ACESTEP_VAE_ON_CPU" "${ACESTEP_DTYPE:-auto}"; printf "%s\\n" "$@"')]:
                p = ace / '.venv/bin' / name
                p.write_text('#!/bin/bash\n' + body + '\n'); p.chmod(0o755)
            (root / 'config.env').write_text(f'ACE_ROOT="{ace}"\nLOFI_ACE_PROFILE={profile}\n')
            return subprocess.run(['bash', str(root / 'run_ace.sh')],
                                  capture_output=True, text=True, env=os.environ.copy())

    def test_cpu_patch_never_enabled_with_cuda(self):
        for profile, dit in [('rtx3060', 'true'), ('rtx3060-resident', 'false')]:
            result = self.launch(profile)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('VAE_GPU=0 VAE_CPU=0 DTYPE=auto', result.stdout)
            self.assertIn('--device\ncuda\n', result.stdout)
            self.assertIn('--offload_dit_to_cpu\n' + dit + '\n', result.stdout)

    def test_stable_keeps_workaround(self):
        result = self.launch('stable')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('VAE_GPU=1 VAE_CPU=1 DTYPE=float32', result.stdout)
        self.assertIn('--device\ncpu\n', result.stdout)

    def test_typo_fails_before_start(self):
        self.assertNotEqual(self.launch('rtx3600').returncode, 0)

    def test_failed_preflight_does_not_start_server(self):
        result = self.launch('rtx3060', preflight_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('--device', result.stdout)
