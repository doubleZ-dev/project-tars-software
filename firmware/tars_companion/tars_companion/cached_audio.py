"""Send complete PCM to board PSRAM, then await actual local playback completion."""
import asyncio
import struct
from .seeduplex import BufferedRobotOutput

BUFFER_START, BUFFER_PCM, BUFFER_COMMIT = 23, 24, 25


class DeviceBufferedOutput(BufferedRobotOutput):
    async def finish(self):
        pending=set()
        try:
            for offset in range(0,len(self.buffer),256000):
                if self.link.mode!='idle': raise RuntimeError('half_duplex_busy')
                self.link.mode='buffered_playback'
                segment=self.buffer[offset:offset+256000]
                await self.link.command(BUFFER_START,bytes([self.volume])+struct.pack('!I',len(segment)))
                for i in range(0,len(segment),512):
                    pending.add(asyncio.create_task(self.link.command(BUFFER_PCM,bytes(segment[i:i+512]))))
                    if len(pending)>=32:
                        done,pending=await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
                        await asyncio.gather(*done)
                await asyncio.gather(*pending)
                pending.clear()
                # Firmware ACKs only after local I2S playback and amp shutdown.
                # Heartbeats continue to be serviced by its separate network task.
                await self.link.command(BUFFER_COMMIT,timeout=12)
                self.link.mode='idle'
                self.bytes_sent+=len(segment)
            self.buffer.clear()
        except BaseException:
            await self.link.close()
            raise
        finally:
            for task in pending: task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
