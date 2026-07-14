import unittest

from src.engine import matches


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


if __name__ == "__main__":
    unittest.main()
