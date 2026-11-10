"""Two targeted release checks; no device, microphone or cloud use."""
import unittest
from pathlib import Path
from tars_companion.gait_debug import GAITS
from tars_companion.intent import parse_intent,MotionIntent

class VoiceGaitRelease(unittest.TestCase):
    def test_screenshot_defaults_and_voice_intents(self):
        self.assertEqual(GAITS['walk_forward'][4],[0,-30,-30,0])
        self.assertEqual(GAITS['turn_right'][3:5],[[-45,25,35,45],[0,25,35,0]])
        self.assertEqual(GAITS['turn_left'][3:5],[[40,20,20,-40],[0,20,20,0]])
        for text,action in [('向前走一步','walk_forward'),('向左转一次','turn_left'),('向右转一次','turn_right')]:
            intent=parse_intent(text);self.assertIsInstance(intent,MotionIntent)
            self.assertEqual((intent.action,intent.steps),(action,1))

    def test_normal_all_phases_and_start_snapshot(self):
        root=Path(__file__).resolve().parents[2]/'tars_firmware'
        source=(root/'components/tars_motion/tars_motion.c').read_text()
        self.assertIn('63u;',source)
        self.assertIn('if(f<TARS_GAIT_PHASES && (smooth & (1u<<f)))',source)
        self.assertIn('memcpy(from,previous,sizeof(from));',source)
        self.assertIn('if(s_cancel || s_estop)',source)
        self.assertIn('pdMS_TO_TICKS(MOTION_TICK_MS)',source)
