import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.generation import GenerationError, generate
from backend.huggingface_generation import generate_huggingface


class HuggingFaceTests(unittest.TestCase):
    def test_same_session_used_for_generation_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.png'
            source.write_bytes(b'input')
            output = Path(directory) / 'output.glb'
            downloaded = Path(directory) / 'download.glb'
            downloaded.write_bytes(b'glTF' + bytes(16))
            client = MagicMock()
            values = [None, str(source), '<preview>', (str(downloaded), str(downloaded))]
            client.submit.side_effect = [MagicMock(result=MagicMock(return_value=value)) for value in values]
            with patch('backend.huggingface_generation.Client', return_value=client) as factory:
                generate_huggingface(source, output)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual([call.kwargs['api_name'] for call in client.submit.call_args_list],
                             ['/start_session', '/preprocess_image', '/image_to_3d', '/extract_glb'])
            self.assertEqual(output.read_bytes(), downloaded.read_bytes())
            client.close.assert_called_once()

    def test_quota_error_does_not_fall_back_to_paid_provider(self):
        with patch.dict(os.environ, {'GENERATION_PROVIDER': 'huggingface', 'FAL_KEY': 'test-only', 'HF_TOKEN': ''}), \
             patch('backend.huggingface_generation.Client', side_effect=RuntimeError('GPU quota exceeded')), \
             patch('backend.generation.generate_fal') as paid:
            with self.assertRaisesRegex(GenerationError, 'quota is exhausted'):
                generate(Path('input.png'), Path('output.glb'))
            paid.assert_not_called()

    def test_second_token_used_when_first_hits_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.png'
            source.write_bytes(b'input')
            output = Path(directory) / 'output.glb'
            downloaded = Path(directory) / 'download.glb'
            downloaded.write_bytes(b'glTF' + bytes(16))

            def make_client(space, token, **kwargs):
                if token == 'bad-token':
                    raise RuntimeError('GPU quota exceeded')
                client = MagicMock()
                values = [None, str(source), '<preview>', (str(downloaded), str(downloaded))]
                client.submit.side_effect = [MagicMock(result=MagicMock(return_value=value)) for value in values]
                return client

            with patch.dict(os.environ, {'HF_TOKEN': 'bad-token,good-token'}), \
                 patch('backend.huggingface_generation.Client', side_effect=make_client) as factory:
                generate_huggingface(source, output)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(output.read_bytes(), downloaded.read_bytes())

    def test_non_quota_error_does_not_try_other_tokens(self):
        with patch.dict(os.environ, {'HF_TOKEN': 'token-a,token-b'}), \
             patch('backend.huggingface_generation.Client', side_effect=RuntimeError('boom')) as factory:
            with self.assertRaises(GenerationError):
                generate_huggingface(Path('input.png'), Path('output.glb'))
            self.assertEqual(factory.call_count, 1)
