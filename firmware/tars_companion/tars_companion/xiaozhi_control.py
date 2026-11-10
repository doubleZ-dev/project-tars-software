"""Independent fail-closed motion lease; a control reconnect cannot close WSS."""
import asyncio
import contextlib
from .wifi_audio import LanAudioServer, NetworkJsonTransport
from .robot_runtime import RuntimeClient
from .motionpack_voice import CATALOG

class Unavailable:
    async def state(self, *a, **k): raise ConnectionError('motion_control_unavailable')
    arm = execute = estop = disarm = state

class ControlPool:
    def __init__(self, profile, allow_motion):
        self.profile = profile
        self.allow_motion = allow_motion
        self.server = LanAudioServer(profile['token'])
        self.current = None
        self.task = None
        self.reconnects = 0

    async def start(self):
        await self.server.start(self.profile['host'], self.profile['port'])
        self.task = asyncio.create_task(self.run())

    def snapshot(self):
        # Bind a turn to ONE verified boot/session, never follow reconnections mid-action.
        return self.current or Unavailable()

    async def run(self):
        while True:
            client = None
            try:
                link = await self.server.wait_connected(30)
                if link.device_id.lower() != '84:c7:bb:77:b5:f0':
                    await link.close()
                    raise RuntimeError('unexpected_paired_robot')
                client = RuntimeClient(NetworkJsonTransport(link), allow_motion=self.allow_motion)
                await client.start()
                if (client.hello['payload']['firmware_version'] not in {'0.17.0-xiaozhi', '0.18.0-director'}
                        and client.hello['payload'].get('director_api') != 1):
                    raise RuntimeError('requires_xiaozhi_firmware')
                if not (await client.disarm()).succeeded: raise RuntimeError('initial_disarm_failed')
                result = await client.command('motion.catalog', {}, retries=0)
                if not result.succeeded or result.message.get('payload', {}).get('result', {}).get('hash') != CATALOG['hash']:
                    raise RuntimeError('motion_catalog_mismatch')
                self.current = client
                print('Motion control verified; fresh commands only, independent 2s safety lease.', flush=True)
                await link.closed.wait()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print('Motion control unavailable: '+type(error).__name__+'; voice remains independent.', flush=True)
            finally:
                self.current = None
                if client: await client.close()
            self.reconnects += 1
            await asyncio.sleep(1)

    async def close(self):
        if self.current:
            with contextlib.suppress(Exception): await asyncio.wait_for(self.current.disarm(), 3)
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task, return_exceptions=True)
        await self.server.close()
