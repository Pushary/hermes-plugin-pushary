import json
import unittest
from unittest import mock
from pathlib import Path
import pushary_plugin as plugin
from pushary_plugin import api, tools


class ReminderTests(unittest.TestCase):
    def test_manifest_advertises_every_registered_tool(self):
        manifest = (Path(plugin.__file__).parent / 'plugin.yaml').read_text()
        tools_block = manifest.split('provides_tools:\n', 1)[1].split('provides_hooks:', 1)[0]
        advertised = {line.strip()[2:] for line in tools_block.splitlines() if line.strip().startswith('- ')}
        self.assertEqual(advertised, {registration[0] for registration in plugin.TOOL_REGISTRATIONS})

    def test_reminder_uses_server_scheduler_and_preserves_identity(self):
        with mock.patch.object(api, '_mcp_call', return_value={'pending': []}) as call:
            result = json.loads(tools.pushary_remind({'body': 'Check deploy', 'in_minutes': 30, 'session_id': 'session'}))
        self.assertEqual(result, {'pending': []})
        self.assertEqual(call.call_args.args[0], 'schedule_reminder')
        self.assertEqual(call.call_args.args[1]['inMinutes'], 30)
        self.assertEqual(call.call_args.args[1]['sessionId'], 'session')

    def test_list_and_cancel_do_not_invent_a_schedule(self):
        with mock.patch.object(api, '_mcp_call', return_value={'cancelled': True}) as call:
            tools.pushary_remind({'cancel_reminder_id': 'id'})
        self.assertEqual(call.call_args.args[1]['cancelReminderId'], 'id')
        self.assertNotIn('inMinutes', call.call_args.args[1])
        self.assertNotIn('body', call.call_args.args[1])
