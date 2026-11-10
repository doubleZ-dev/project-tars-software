import asyncio
import struct
import unittest
from tars_companion.cached_audio import DeviceBufferedOutput, BUFFER_START, BUFFER_PCM, BUFFER_COMMIT


class CachedAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_pcm_segments_and_acknowledged_physical_completion(self):
        class Link:
            mode='idle'
            def __init__(self):
                self.segments=[]; self.current=bytearray(); self.active=0; self.maximum=0
                self.commit_waiting=asyncio.Event(); self.finish=asyncio.Event()
            async def command(self,kind,payload=b'',timeout=2):
                if kind==BUFFER_START:
                    self.expected=struct.unpack('!I',payload[1:])[0]
                    self.assert_volume=payload[0]
                    self.current=bytearray()
                elif kind==BUFFER_PCM:
                    self.current.extend(payload)
                    self.active+=1; self.maximum=max(self.maximum,self.active)
                    await asyncio.sleep(.001); self.active-=1
                else:
                    assert kind==BUFFER_COMMIT and len(self.current)==self.expected
                    assert timeout==12
                    self.commit_waiting.set()
                    await self.finish.wait()
                    self.segments.append(bytes(self.current))
            async def close(self): self.mode='closed'
        link=Link(); output=DeviceBufferedOutput(link,20)
        pcm=b'\x01\x02'*150000
        await output.write(pcm)
        task=asyncio.create_task(output.finish())
        await link.commit_waiting.wait()
        self.assertFalse(task.done()); self.assertEqual(output.bytes_sent,0)
        link.finish.set(); await task
        self.assertEqual(b''.join(link.segments),pcm)
        self.assertEqual([len(x) for x in link.segments],[256000,44000])
        self.assertEqual(link.assert_volume,20)
        self.assertLessEqual(link.maximum,32)
        self.assertEqual(link.mode,'idle')

    async def test_cancel_waiting_for_playback_closes_link_without_replay(self):
        class Link:
            mode='idle'; starts=0
            def __init__(self): self.waiting=asyncio.Event()
            async def command(self,kind,payload=b'',timeout=2):
                if kind==BUFFER_START: self.starts+=1
                if kind==BUFFER_COMMIT:
                    self.waiting.set(); await asyncio.sleep(100)
            async def close(self): self.mode='closed'
        link=Link(); output=DeviceBufferedOutput(link,20)
        await output.write(bytes(3200))
        task=asyncio.create_task(output.finish()); await link.waiting.wait()
        task.cancel(); await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(link.mode,'closed'); self.assertEqual(link.starts,1)


if __name__=='__main__': unittest.main()
