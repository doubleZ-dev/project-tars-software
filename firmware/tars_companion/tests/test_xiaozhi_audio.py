import asyncio
import json
import ssl
import unittest
from pathlib import Path
from websockets.asyncio.client import connect
from tars_companion.xiaozhi_audio import XiaozhiServer, XiaozhiOutput, PARAMS
from tars_companion.opus_codec import Opus
from tars_companion.xiaozhi_control import ControlPool, Unavailable

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = XiaozhiServer('a'*32)
        await self.server.start('127.0.0.1', 0)
        self.port = self.server._server.sockets[0].getsockname()[1]
        self.tls = ssl.create_default_context(cafile='data/tls/companion.pem')
        self.sockets = []

    async def asyncTearDown(self):
        for s in self.sockets: await s.close()
        await self.server.close()

    async def board(self):
        ws = await connect(f'wss://127.0.0.1:{self.port}/tars', ssl=self.tls,
            additional_headers={'Authorization':'Bearer '+'a'*32, 'Device-Id':'84:c7:bb:77:b5:f0'}, compression=None)
        self.sockets.append(ws)
        await ws.send(json.dumps(dict(type='hello', version=1, features={'tars_cached_audio':1}, audio_params=PARAMS)))
        hello = json.loads(await ws.recv())
        link = await self.server.wait_connected()
        return ws, hello, link

    async def ack(self, ws, command, status=0):
        await ws.send(json.dumps(dict(type='tars.ack', session_id=command['session_id'], id=command['id'], status=status)))

    async def test_real_opus_capture_and_grace_period(self):
        ws, hello, link = await self.board()
        task = asyncio.create_task(link.start_capture())
        command = json.loads(await ws.recv())
        await asyncio.sleep(2.8) # Old 2s application watchdog must not apply here.
        await self.ack(ws, command); await task
        codec = Opus()
        try:
            for i in range(5): await ws.send(codec.encode(bytes(1920)))
            self.assertEqual(len(await asyncio.wait_for(link.read_frame(),2)),3200)
            self.assertFalse(link.closed.is_set())
        finally: codec.close()

    async def test_cached_playback_completion_not_upload_ack(self):
        ws, _, link = await self.board()
        output = XiaozhiOutput(link,20)
        await output.write(bytes(3200))
        task=asyncio.create_task(output.finish())
        begin=json.loads(await ws.recv()); self.assertEqual(begin['samples'],1600)
        await self.ack(ws,begin)
        codec=Opus()
        try:
            for _ in range(2): self.assertEqual(len(codec.decode(await ws.recv())),1920)
        finally: codec.close()
        commit=json.loads(await ws.recv())
        self.assertEqual(commit['op'],'play_commit'); self.assertFalse(task.done())
        await asyncio.sleep(.1); await self.ack(ws,commit); await task
        self.assertEqual(output.bytes_sent,3200); self.assertEqual(link.mode,'idle')

    async def test_disconnect_no_old_command_replay(self):
        ws, hello, link=await self.board()
        task=asyncio.create_task(link.start_capture())
        await ws.recv(); await ws.close()
        with self.assertRaises(ConnectionError): await task
        await asyncio.sleep(.1)
        ws2, hello2, fresh=await self.board()
        self.assertNotEqual(hello['session_id'],hello2['session_id'])
        self.assertEqual(fresh.mode,'idle')
        with self.assertRaises(TimeoutError): await asyncio.wait_for(ws2.recv(),.15)

    async def test_wrong_pairing_is_rejected(self):
        with self.assertRaises(Exception):
            await connect(f'wss://127.0.0.1:{self.port}/tars',ssl=self.tls,
                additional_headers={'Authorization':'Bearer wrong','Device-Id':'84:c7:bb:77:b5:f0'})

    async def test_stale_session_ack_fails_closed(self):
        ws,_,link=await self.board()
        task=asyncio.create_task(link.start_capture()); command=json.loads(await ws.recv())
        command['session_id']='bad'; await self.ack(ws,command)
        with self.assertRaises(ConnectionError): await task
        self.assertTrue(link.closed.is_set())

    async def test_unavailable_control_never_moves(self):
        with self.assertRaises(ConnectionError): await Unavailable().execute('walk_forward')

if __name__=='__main__': unittest.main()
