import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tars_companion.client import TarsClient
from tars_companion.robot_runtime import save_diagnostics

class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_view_validation_and_existing_json_command(self):
        client=TarsClient(None)
        client.command=AsyncMock(return_value='result')
        self.assertEqual(await client.diagnostics('previous'),'result')
        client.command.assert_awaited_once_with('diagnostic.get',{'view':'previous'})
        with self.assertRaises(ValueError): await client.diagnostics('raw_memory')

    async def test_save_three_views_without_arm_or_cloud(self):
        client=SimpleNamespace(boot_id='a'*16,diagnostics=AsyncMock(side_effect=[
            SimpleNamespace(succeeded=True,message={'payload':{'result':{'view':view,'valid':False}}})
            for view in ('boot','previous','current')]))
        with tempfile.TemporaryDirectory() as directory:
            report=await save_diagnostics(client,directory)
            self.assertEqual(list(report['records']),['boot','previous','current'])
            files=list(Path(directory).glob('*.json'))
            self.assertEqual(len(files),1)
            self.assertEqual(json.loads(files[0].read_text())['boot_id'],'a'*16)

    async def test_unavailable_or_disconnected_is_not_retried(self):
        client=SimpleNamespace(boot_id='a'*16,diagnostics=AsyncMock(side_effect=ConnectionError))
        self.assertIsNone(await save_diagnostics(client))
        client.diagnostics.assert_awaited_once()
        client.diagnostics=AsyncMock(return_value=SimpleNamespace(succeeded=False))
        self.assertIsNone(await save_diagnostics(client))
