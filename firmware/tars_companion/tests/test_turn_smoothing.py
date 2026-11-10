"""Focused Mock/parameter checks only; no real hardware or cloud."""
import asyncio,copy,json,unittest
from tars_companion.gait_debug import GaitBridge,GAITS,validate_turn_profile

class TurnSmoothingTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_and_profiles(self):
        self.assertEqual(GAITS['walk_forward'][2:4],[[5,45,45,5],[5,-30,-30,5]])
        self.assertEqual(GAITS['turn_left'][2],[40,-30,-30,-40])
        self.assertEqual(GAITS['turn_right'][2],[-45,-30,-30,45])
        b=GaitBridge(True)
        try:
            owner=(await b.handle('claim',{}))['owner']
            async def call(op,**kw):return await b.handle(op,{'owner':owner,**kw})
            await call('connect')
            for action in ('turn_left','turn_right'):
                await call('select_action',action=action)
                for mask in (8,28,62):
                    b.last_ping=__import__('time').monotonic()
                    await call('run',frames=copy.deepcopy(GAITS[action]),selected=list(range(1,7)),smooth=mask,settle=60 if mask==28 else 0,confirm=True)
                    await b.job
                    self.assertTrue(b.records[-1]['completed'])
                    self.assertEqual(b.records[-1]['smooth'],mask)
                    self.assertFalse(b.device.armed)
        finally:await b.drop()

    async def test_support_order_and_options_rejected(self):
        for action in ('turn_left','turn_right'):
            for f,j in ((2,1),(3,0),(4,2)):
                frames=copy.deepcopy(GAITS[action]);frames[f][j]+=1
                for mask in (8,28,62):
                    with self.assertRaises(ValueError):validate_turn_profile(action,frames,mask,0)
                validate_turn_profile(action,frames,0,0) # direct remains freely editable
        for mask,settle in ((True,0),(16,0),(8,20),(0,60)):
            with self.assertRaises(ValueError):validate_turn_profile('turn_left',GAITS['turn_left'],mask,settle)
        with self.assertRaises(ValueError):validate_turn_profile('walk_forward',GAITS['walk_forward'],8,0)

    async def test_trajectory_arithmetic_and_wire_budget(self):
        for start,end in ((-30,20),(0,40),(40,0),(0,-45),(-45,0)):
            values=[]
            for ms in range(0,301,20):
                u=ms/300;s=u*u/.32 if u<.2 else (u-.1)/.8 if u<=.8 else 1-(1-u)**2/.32
                values.append(start+(end-start)*s)
            self.assertAlmostEqual(values[0],start);self.assertAlmostEqual(values[-1],end)
            self.assertTrue(all(min(start,end)-1e-6<=v<=max(start,end)+1e-6 for v in values))
        wire={'v':2,'type':'motion.test_run','target_boot_id':'b'*16,'command_id':'c'*39,'session_id':'s'*39,
              'payload':{'confirm':True,'action':'turn_right','frames':[[-45,-45,-45,45]]*12,'smooth':62,'settle':60}}
        self.assertLessEqual(len(json.dumps(wire,separators=(',',':')).encode()),512)
