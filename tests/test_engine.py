import unittest
from unittest.mock import patch

from src.engine import matches, run_slack


class MatchTests(unittest.TestCase):
    def test_matches_nested_string_rules(self):
        workflow = {
            "enabled": True,
            "trigger": {
                "connector": "dropbox",
                "event": "file.created",
                "filters": {"path": {"prefix": "/incoming/", "suffix": ".pdf"}},
            },
        }
        event = {"connector": "dropbox", "event": "file.created", "data": {"path": "/incoming/a.pdf"}}
        self.assertTrue(matches(workflow, event))

    def test_rejects_wrong_event(self):
        workflow = {"enabled": True, "trigger": {"connector": "youtube", "event": "video.published"}}
        event = {"connector": "dropbox", "event": "video.published", "data": {}}
        self.assertFalse(matches(workflow, event))


class SlackTests(unittest.TestCase):
    @patch("src.engine._json_request")
    @patch("src.engine.get_credential")
    def test_reads_bot_token_from_dynamodb_credential(self, get_credential, json_request):
        get_credential.return_value = {"token": "xoxb-private"}
        json_request.return_value = {"ok": True}

        run_slack(
            {"credential_id": "slack", "channel": "C123", "text": "{title}: {url}"},
            {"data": {"title": "Published", "url": "https://example.test/video"}},
        )

        get_credential.assert_called_once_with("slack")
        self.assertEqual(json_request.call_args.args[1]["channel"], "C123")
        self.assertEqual(json_request.call_args.args[1]["text"], "Published: https://example.test/video")
        self.assertEqual(json_request.call_args.kwargs["headers"], {"authorization": "Bearer xoxb-private"})


if __name__ == "__main__":
    unittest.main()
