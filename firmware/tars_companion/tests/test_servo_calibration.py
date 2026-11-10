import asyncio
import json
import time
import unittest
from tars_companion.servo_calibration import CalibrationBridge, LocalServer, WEB_LEASE

class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b=CalibrationBridge(True)
        self.owner=(await self.b.handle('claim',{}))['owner']
        await self.call('connect')
    async def asyncTearDown(self): await self.b.drop()
    async def call(self,action,**data): return await self.b.handle(action,{'owner':self.owner,**data})
    async def begin(self,servo=1): return await self.call('begin',servo=servo,confirm=True)

    async def test_default_off_confirmation_single_axis_and_no_old_offsets(self):
        d=self.b.device
        self.assertEqual(d.outputs,[False]*4)
        with self.assertRaises(ValueError): await self.call('begin',servo=1,confirm=False)
        for axis in range(1,5):
            s=await self.begin(axis)
            self.assertEqual(d.outputs,[i==axis-1 for i in range(4)])
            self.assertEqual(s['last_target']['pulse_us'],1500)
            self.assertEqual(s['last_target']['calibration_angle_deg'],0)
            self.assertFalse(d.armed)

    async def test_range_and_exact_integer_mapping(self):
        await self.begin(2);d=self.b.device
        for bad in (-21,21,90,1.5,True,'5',None):
            count=d.calibration_writes
            with self.assertRaises(ValueError): await self.call('set',servo=2,angle_deg=bad)
            self.assertEqual(d.calibration_writes,count)
        for angle in (-20,0,1,20):
            s=await self.call('set',servo=2,angle_deg=angle)
            self.assertEqual(s['last_target']['pulse_us'],500+(angle+90)*2000//180)
        with self.assertRaises(ValueError): await self.call('set',servo=3,angle_deg=0)

    async def test_protocol_rejects_range_stale_lease_and_other_modes(self):
        await self.begin()
        for name,payload in [('calibration.set',{'calibration_id':self.b.lease_id,'angle_deg':21}),
                             ('calibration.renew',{'calibration_id':'old'}),('safety.arm',{}),
                             ('motion.execute',{'name':'walk_forward','steps':1,'speed_pct':50})]:
            self.assertFalse((await self.b.client.command(name,payload)).succeeded)
        self.assertEqual(self.b.device.outputs,[True,False,False,False])

    async def test_dedup_begin_set_and_id_conflict(self):
        c=self.b.client;d=self.b.device
        p={'servo':3,'confirm':True,'calibration_id':'cal-test'}
        self.assertTrue((await c.command('calibration.begin',p,command_id='dedup-begin')).succeeded)
        count=d.calibration_writes
        self.assertTrue((await c.command('calibration.begin',p,command_id='dedup-begin')).succeeded)
        self.assertEqual(d.calibration_writes,count)
        p={'calibration_id':'cal-test','angle_deg':4}
        await c.command('calibration.set',p,command_id='dedup-set');count=d.calibration_writes
        await c.command('calibration.set',p,command_id='dedup-set')
        self.assertEqual(d.calibration_writes,count)
        self.assertFalse((await c.command('calibration.set',{**p,'angle_deg':5},command_id='dedup-set')).succeeded)
        self.assertEqual(d.calibration_writes,count)

    async def test_serial_heartbeat_cannot_renew_calibration(self):
        await self.begin();lease=self.b.lease_id
        await asyncio.sleep(1.32)
        self.assertEqual(self.b.device.outputs,[False]*4)
        self.assertTrue(self.b.device.calibration['timed_out'])
        self.assertFalse((await self.b.client.command('calibration.renew',{'calibration_id':lease})).succeeded)
        self.assertIsNotNone(self.b.client.boot_id)

    async def test_browser_watchdog_and_stale_page_rejected(self):
        await self.begin();watcher=asyncio.create_task(self.b.watchdog())
        self.b.last_ping=time.monotonic()-WEB_LEASE-.1
        await asyncio.sleep(.08);watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
        self.assertEqual(self.b.device.outputs,[False]*4)
        self.assertIsNone(self.b.owner)
        with self.assertRaises(ValueError): await self.begin()

    async def test_second_page_denied_and_release_closes_output(self):
        await self.begin()
        with self.assertRaises(ValueError): await self.b.handle('claim',{})
        await self.call('release');self.assertEqual(self.b.device.outputs,[False]*4)
        self.owner=(await self.b.handle('claim',{}))['owner']
        self.assertFalse((await self.call('ping'))['status']['active'])

    async def test_disconnect_reconnect_does_not_restore(self):
        await self.begin(4);await self.call('set',servo=4,angle_deg=-8)
        await self.call('disconnect');s=await self.call('connect')
        self.assertFalse(s['status']['active']);self.assertIsNone(s['last_target'])
        self.assertEqual(self.b.device.calibration_writes,0)

    async def test_reboot_invalidates_session(self):
        await self.begin();await self.b.device.reboot();await asyncio.sleep(.05)
        with self.assertRaises(ConnectionError): await self.call('set',servo=1,angle_deg=1)
        self.assertIsNone(self.b.client);self.assertIsNone(self.b.last_target)

    async def test_estop_clear_and_cancel(self):
        await self.begin();s=await self.call('estop')
        self.assertTrue(s['status']['estop']);self.assertEqual(self.b.device.outputs,[False]*4)
        with self.assertRaises(ValueError): await self.begin()
        self.assertFalse((await self.call('clear'))['status']['active'])
        await self.begin();await self.call('off');self.assertEqual(self.b.device.outputs,[False]*4)

    async def test_record_requires_user_confirmation_and_completed_target(self):
        with self.assertRaises(ValueError): await self.call('mark',servo=1,confirm=True)
        await self.begin();await self.call('set',servo=1,angle_deg=6)
        with self.assertRaises(ValueError): await self.call('mark',servo=1,confirm=False)
        await self.call('mark',servo=1,confirm=True)
        r=(await self.call('export'))['records'][0]
        self.assertEqual(r['user_confirmed_mechanical_zero_deg'],6)
        self.assertIsNone(r['actual_angle_deg']);self.assertEqual(r['evidence'],'mock')
        self.assertEqual(r['gpio'],38);self.assertIn('command_id',r)

    async def test_no_false_success_when_ack_lost(self):
        await self.begin()
        async def timeout(*a,**kw): raise TimeoutError('ACK lost')
        self.b.client.command=timeout
        with self.assertRaises(TimeoutError): await self.call('set',servo=1,angle_deg=5)
        self.assertIsNone(self.b.last_target);self.assertIsNone(self.b.client)
        self.assertTrue(self.b.status['connection_error'])

    async def test_disconnect_failure_preserves_unknown_output_status(self):
        await self.begin()
        async def timeout(*a,**kw): raise TimeoutError('lost stop ACK')
        self.b.client.command=timeout
        with self.assertRaises(TimeoutError): await self.call('disconnect')
        self.assertIsNone(self.b.client)
        self.assertTrue(self.b.status['connection_error'])
        self.assertIn('异常',self.b.connection)

    async def test_watchdog_failure_does_not_claim_output_off(self):
        await self.begin()
        async def timeout(*a,**kw): raise TimeoutError('lost stop ACK')
        self.b.client.command=timeout
        watcher=asyncio.create_task(self.b.watchdog())
        self.b.last_ping=time.monotonic()-WEB_LEASE-.1
        await asyncio.sleep(.08);watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
        self.assertTrue(self.b.status['connection_error'])
        self.assertNotIn('timed_out',self.b.status)

class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge=CalibrationBridge(True);self.server=LocalServer(self.bridge,0);await self.server.start()
    async def asyncTearDown(self):
        self.server.server.close();await self.server.server.wait_closed();await self.bridge.drop()
    async def request(self,origin=None,token=None,host=None,path='/api/claim',mime='application/json',length=2):
        r,w=await asyncio.open_connection('127.0.0.1',self.server.port)
        raw=(f'POST {path} HTTP/1.1\r\nHost: {host or "127.0.0.1:"+str(self.server.port)}\r\n'
             f'Origin: {origin or self.server.origin}\r\nX-Tars-Token: {token or self.server.secret}\r\n'
             f'Content-Type: {mime}\r\nContent-Length: {length}\r\n\r\n'+'{}').encode()
        w.write(raw);await w.drain();response=await r.read();w.close();await w.wait_closed();return response
    async def test_origin_token_host_loopback_and_no_cors(self):
        for kw in ({'origin':'https://evil.example'},{'token':'bad'},{'host':'evil.example'}):
            self.assertIn(b'403',(await self.request(**kw)).split(b'\r\n')[0]);self.assertIsNone(self.bridge.owner)
        response=await self.request();self.assertIn(b'200',response.split(b'\r\n')[0])
        self.assertNotIn(b'Access-Control-Allow-Origin',response)
        self.assertEqual(self.server.server.sockets[0].getsockname()[0],'127.0.0.1')
    async def test_bad_content_type_oversized_request_and_unknown_route(self):
        for kw in ({'mime':'text/plain'},{'length':5000},{'path':'/../../pyproject.toml'}):
            self.assertNotIn(b'200 Response',await self.request(**kw))

if __name__=='__main__':unittest.main()
