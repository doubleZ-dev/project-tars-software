import asyncio
import copy
import unittest
from unittest.mock import patch
from tars_companion.gait_debug import GaitBridge,BASELINE,GAITS,validate_plan
from tars_companion.servo_calibration import WEB_LEASE

class GaitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b=GaitBridge(True)
        self.owner=(await self.b.handle('claim',{}))['owner']
        await self.call('connect')
    async def asyncTearDown(self): await self.b.drop()
    async def call(self,operation,**data):return await self.b.handle(operation,{'owner':self.owner,**data})
    async def execute(self,frames=None,selected=None):
        await self.call('run',frames=frames or copy.deepcopy(BASELINE),selected=selected or [1,2],confirm=True)
        await self.b.job

    async def test_authoritative_zero_mapping_default_off(self):
        self.assertEqual(self.b.metadata['zeros'],[10,0,-10,2])
        self.assertEqual(self.b.metadata['directions'],[1,-1,1,-1])
        self.assertFalse(self.b.device.armed)
        self.assertEqual(self.b.device.outputs,[False]*4)
        self.assertEqual(self.b.metadata['phase_ms'],300)
        self.assertEqual(self.b.metadata['max_phases'],12)

    async def test_drop_ramp_custom_angles_and_record(self):
        frames=copy.deepcopy(BASELINE)
        frames[2]=[5,40,35,5];frames[3]=[5,-20,-25,5]
        await self.call('run',frames=frames,selected=[1,2,3,4],confirm=True,drop_ramp=True)
        await self.b.job
        self.assertTrue(self.b.records[-1]['completed'])
        self.assertTrue(self.b.records[-1]['drop_ramp'])
        self.assertEqual(self.b.device.gait_test_history,frames[:4])
        self.assertFalse(self.b.device.armed)

    async def test_drop_ramp_rejects_side_motion_turns_and_old_firmware(self):
        frames=copy.deepcopy(BASELINE);frames[3][0]=5
        with self.assertRaises(ValueError):await self.call('run',frames=frames,selected=[1,2,3,4],confirm=True,drop_ramp=True)
        await self.call('select_action',action='turn_left')
        with self.assertRaises(ValueError):await self.call('run',frames=GAITS['turn_left'],selected=[1],confirm=True,drop_ramp=True)
        await self.call('select_action',action='walk_forward')
        self.b.metadata['drop_ramp']=False
        with self.assertRaises(ValueError):await self.call('run',frames=BASELINE,selected=[1],confirm=True,drop_ramp=True)
        self.assertEqual(self.b.device.execution_count,0)

    async def test_drop_ramp_firmware_guard_independent_of_web(self):
        await self.b.client.arm()
        frames=copy.deepcopy(BASELINE[:4]);frames[3][3]=5
        result=await self.b.client.command('motion.test_run',{'confirm':True,'frames':frames,'drop_ramp':True})
        self.assertFalse(result.succeeded)
        self.assertEqual(self.b.device.execution_count,0)

    async def test_left_and_right_selection_reads_plan_without_motion(self):
        for action in ('turn_left','turn_right','walk_forward'):
            await self.call('select_action',action=action)
            self.assertEqual(self.b.metadata['action'],action)
            self.assertEqual(self.b.metadata['baseline'],GAITS[action])
            self.assertFalse(self.b.device.armed)
            self.assertEqual(self.b.device.execution_count,0)

    async def test_left_right_prefix_and_smaller_turn_amplitude(self):
        for action,axis,value,number in [('turn_left',0,20,1),('turn_right',0,-20,2)]:
            await self.call('select_action',action=action)
            frames=copy.deepcopy(GAITS[action]);frames[2][axis]=value
            start=len(self.b.device.gait_test_history)
            await self.execute(frames,[1,2,3])
            self.assertEqual(self.b.device.gait_test_history[start:],frames[:3])
            self.assertEqual(self.b.diagnostic['current']['action'],number)
            self.assertEqual(self.b.records[-1]['action'],action)
            self.assertFalse(self.b.device.armed)

    async def test_turn_envelopes_wrong_sign_and_unknown_action_rejected(self):
        for action,axis,value in [('turn_left',3,1),('turn_right',0,1)]:
            await self.call('select_action',action=action)
            frames=copy.deepcopy(GAITS[action])+[[0,0,0,0]]
            frames[-1][axis]=value
            self.assertEqual(validate_plan(self.b.metadata,frames,list(range(1,8))),frames)
        with self.assertRaises(ValueError):await self.call('select_action',action='raw_gpio')
        self.assertEqual(self.b.device.execution_count,0)

    async def test_prefix_and_custom_angle_no_unselected_neutral(self):
        frames=copy.deepcopy(BASELINE);frames[1]=[0,30,30,0];frames[2]=[5,40,40,5]
        await self.execute(frames,[1,2,3])
        self.assertEqual(self.b.device.gait_test_history,frames[:3])
        self.assertEqual(self.b.device.gait_test_history[-1],[5,40,40,5])
        self.assertFalse(self.b.device.armed)
        self.assertEqual(self.b.device.outputs,[False]*4)
        self.assertTrue(self.b.records[-1]['completed'])

    async def test_manual_zero_button_one_zero_phase_then_off(self):
        await self.call('zero',confirm=True);await self.b.job
        self.assertEqual(self.b.device.gait_test_history,[[0,0,0,0]])
        self.assertFalse(self.b.device.armed)
        self.assertEqual(self.b.records[-1]['event'],'zero')

    async def test_append_custom_stages(self):
        frames=copy.deepcopy(BASELINE)+[[0,20,20,0],[5,-20,-20,5]]
        await self.execute(frames,list(range(1,9)))
        self.assertEqual(self.b.device.gait_test_history,frames)
        self.assertEqual(self.b.diagnostic['current']['phase'],8)

    async def test_plan_rejects_jumps_enlargement_reverse_float_bool(self):
        for selected in ([],[2],[1,3],[1,1],[True],[1,2,4],list(range(1,14))):
            with self.assertRaises(ValueError):validate_plan(self.b.metadata,copy.deepcopy(BASELINE),selected)
        for value in (-46,46,5.5,True,'5'):
            frames=copy.deepcopy(BASELINE);frames[2][0]=value
            with self.assertRaises(ValueError):validate_plan(self.b.metadata,frames,[1,2,3])
        frames=copy.deepcopy(BASELINE);frames[0][0]=1
        self.assertEqual(validate_plan(self.b.metadata,frames,[1]),[[1,0,0,0]])
        frames=copy.deepcopy(BASELINE);frames[2][0]=5
        self.assertEqual(validate_plan(self.b.metadata,frames,[1,2,3])[2][0],5)

    async def test_free_angles_every_cell_including_zero_and_appended(self):
        for action in GAITS:
            await self.call('select_action',action=action)
            frames=[[45,-45,45,-45] if i%2 else [-45,45,-45,45] for i in range(12)]
            self.assertTrue(self.b.metadata['free_angles'])
            self.assertEqual(validate_plan(self.b.metadata,frames,list(range(1,13))),frames)
        self.assertEqual(self.b.device.execution_count,0)

    async def test_free_angles_bounds_still_rejected_before_arm(self):
        for value in (-46,46,True,1.5):
            frames=copy.deepcopy(BASELINE);frames[0][0]=value
            with self.assertRaises(ValueError):await self.call('run',frames=frames,selected=[1],confirm=True)
        self.assertEqual(self.b.device.execution_count,0)
        self.assertFalse(self.b.device.armed)

    async def test_confirm_and_owner_required(self):
        with self.assertRaises(ValueError):await self.call('run',frames=BASELINE,selected=[1],confirm=False)
        with self.assertRaises(ValueError):await self.b.handle('zero',{'owner':'wrong','confirm':True})
        self.assertEqual(self.b.device.execution_count,0)

    async def test_firmware_sim_guard_and_dedup(self):
        client=self.b.client
        payload={'frames':BASELINE[:2],'confirm':True}
        self.assertFalse((await client.command('motion.test_run',payload)).succeeded)
        await client.arm()
        a=await client.command('motion.test_run',payload,command_id='one-prefix')
        b=await client.command('motion.test_run',payload,command_id='one-prefix')
        self.assertTrue(a.succeeded and b.succeeded)
        self.assertEqual(self.b.device.execution_count,1)
        self.assertEqual(self.b.device.gait_test_history,BASELINE[:2])

    async def test_stop_during_preparation_cannot_later_arm(self):
        ready=asyncio.Event();release=asyncio.Event()
        original=self.b.diagnostic_read
        async def blocked():ready.set();await release.wait();return await original()
        with patch.object(self.b,'diagnostic_read',blocked):
            await self.call('run',frames=BASELINE,selected=[1,2],confirm=True)
            await ready.wait();await self.call('off');release.set();await self.b.job
        self.assertEqual(self.b.device.execution_count,0)
        self.assertFalse(self.b.device.armed)

    async def test_page_lease_stops_running_test(self):
        self.b.device.motion_delay=.15
        await self.call('run',frames=BASELINE,selected=[1,2,3,4,5,6],confirm=True)
        await asyncio.sleep(.03)
        self.b.last_ping-=WEB_LEASE+1
        watcher=asyncio.create_task(self.b.watchdog())
        try:
            await asyncio.sleep(.12)
            self.assertIsNone(self.b.owner)
            self.assertFalse(self.b.device.armed)
            self.assertEqual(self.b.device.outputs,[False]*4)
        finally:
            watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)

    async def test_reboot_requires_manual_reconnect(self):
        await self.b.device.reboot();await asyncio.sleep(.03)
        watcher=asyncio.create_task(self.b.watchdog())
        try:
            await asyncio.sleep(.08)
            self.assertIsNone(self.b.client)
            self.assertFalse(self.b.connected())
        finally:
            watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
