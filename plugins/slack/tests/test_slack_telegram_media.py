"""The telegram crosspost mirrors a post's attachment instead of posting the
"no text in this post" placeholder (t.me/aidevtoolszoomcamp/81: a photo
with no caption reached Slack as an empty placeholder post)."""

import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from plugins.slack.runners import slack as slack_action

ACTION = {"type": "slack", "connection_id": "slack", "channel": "course-ai-dev-tools-zoomcamp",
          "telegram_format": True,
          "source_link": "https://t.me/aidevtoolszoomcamp/{message_id}"}

BOT_TOKEN = "5347157767:AAtelegram-secret-token-value"
PREVIEW_URL = "https://cdn4.telesco.pe/file/DFX1G19iEQ.jpg"
PREVIEW_HTML = (
    '<div class="tgme_widget_message_bubble"><a class="tgme_widget_message_photo_wrap '
    '5190850354319401177 1208589029_460005593" href="https://t.me/aidevtoolszoomcamp/81" '
    f"style=\"width:800px;background-image:url('{PREVIEW_URL}')\"></a></div>"
).encode()

# The photo sizes of the real post 81 (smallest first, as Telegram sends them).
PHOTO_SIZES = [
    {"file_id": "small", "file_size": 1147, "height": 51, "width": 90},
    {"file_id": "mid", "file_size": 81350, "height": 450, "width": 800},
    {"file_id": "largest", "file_size": 206676, "height": 810, "width": 1440},
]


def channel_post(**fields):
    chat = {"id": -1003188609266, "title": "AI Dev Tools Zoomcamp", "type": "channel",
            "username": "aidevtoolszoomcamp"}
    post = {"message_id": 81, "date": 1795208306, "chat": chat, **fields}
    return {"connector": "telegram", "event": "channel_post.received", "data": {
        "hook": "automator-telegram", "message_id": 81, "chat_id": chat["id"], "chat": chat,
        "text": post.get("caption") or post.get("text") or "",
        "entities": post.get("caption_entities") or post.get("entities") or [],
        "is_channel_post": True,
        "update": {"update_id": "relay--1003188609266-81", "channel_post": post},
    }}


class FakeNetwork:
    """Telegram, Slack upload and t.me preview endpoints, plus chat.postMessage."""

    def __init__(self, *, upload_error=None, preview=PREVIEW_HTML, post_errors=(),
                 getfile_status=200):
        self.requests = []
        self.posts = []
        self.upload_error = upload_error
        self.preview = preview
        self.post_errors = list(post_errors)
        self.getfile_status = getfile_status

    def transport(self, method, url, *, headers=None, body=None, timeout=10):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        if "/getFile?" in url:
            if self.getfile_status >= 300:
                return self.getfile_status, json.dumps(
                    {"ok": False, "description": "Bad Request: wrong file_id"}).encode()
            return 200, json.dumps({"ok": True, "result": {"file_path": "photos/file_7.jpg"}}).encode()
        if "/file/bot" in url:
            return 200, b"\xff\xd8jpeg-bytes"
        if "files.getUploadURLExternal" in url:
            if self.upload_error:
                return 200, json.dumps({"ok": False, "error": self.upload_error}).encode()
            return 200, json.dumps({"ok": True, "upload_url": "https://files.slack.com/upload/v1/abc",
                                    "file_id": "F0IMAGE"}).encode()
        if url.startswith("https://files.slack.com/upload/"):
            return 200, b"OK"
        if "files.completeUploadExternal" in url:
            return 200, json.dumps({"ok": True, "files": [{"id": "F0IMAGE"}]}).encode()
        if url.startswith("https://t.me/"):
            if self.preview is None:
                return 404, b""
            return 200, self.preview
        raise AssertionError(f"unexpected request {method} {url}")

    def json_request(self, url, payload, headers=None, timeout=10):
        self.posts.append(payload)
        if self.post_errors:
            error = self.post_errors.pop(0)
            if error:
                return {"ok": False, "error": error}
        return {"ok": True, "ts": f"ts-{len(self.posts)}", "channel": "C0AIDEV"}

    def urls(self, needle):
        return [request for request in self.requests if needle in request["url"]]


def run(event, network, action=None):
    sleeps = []
    with patch.object(slack_action.base, "_json_request", side_effect=network.json_request), \
         patch("src.dapier.triggers.hook_triggers.get_item",
               return_value={"hook_id": "automator-telegram",
                             "connection_id": "automator-telegram"}) as get_item, \
         patch.object(slack_action.base, "_connected_connection",
                      return_value={"connection_id": "automator-telegram",
                                    "credential_id": "cred-tg"}) as connected, \
         patch.object(slack_action.credentials, "get_credential",
                      return_value={"token": BOT_TOKEN}):
        output = slack_action._post_telegram_format(
            action or dict(ACTION), event, "xoxb-test",
            transport=network.transport, sleep=sleeps.append)
    return output, sleeps, get_item, connected


def all_text(posts):
    return json.dumps(posts)


class PhotoPostTests(unittest.TestCase):
    def test_post_81_photo_without_caption_is_the_uploaded_image(self):
        network = FakeNetwork()
        output, _, get_item, connected = run(channel_post(photo=PHOTO_SIZES), network)

        # The trigger's own bot downloads the largest size.
        get_item.assert_called_once_with("automator-telegram")
        connected.assert_called_once_with("automator-telegram")
        getfile = network.urls("/getFile?")
        self.assertEqual(len(getfile), 1)
        self.assertIn("file_id=largest", getfile[0]["url"])
        self.assertIn(f"/file/bot{BOT_TOKEN}/photos/file_7.jpg", network.urls("/file/bot")[0]["url"])

        # The bytes go to Slack unshared, then an image block references them.
        upload = network.urls("files.slack.com/upload/")[0]
        self.assertEqual(upload["body"], b"\xff\xd8jpeg-bytes")
        completion = parse_qs(network.urls("completeUploadExternal")[0]["body"].decode())
        self.assertEqual(json.loads(completion["files"][0])[0]["id"], "F0IMAGE")
        self.assertNotIn("channel_id", completion)

        first, link = network.posts
        self.assertEqual(first["channel"], "course-ai-dev-tools-zoomcamp")
        self.assertEqual(first["blocks"], [{"type": "image", "slack_file": {"id": "F0IMAGE"},
                                            "alt_text": "Image from the Telegram post"}])
        self.assertEqual(link["thread_ts"], "ts-1")
        self.assertIn("https://t.me/aidevtoolszoomcamp/81", all_text([link]))
        self.assertNotIn("no text in this post", all_text(network.posts))
        self.assertEqual(output["media"], {"kind": "photo", "via": "upload", "file_id": "F0IMAGE"})
        self.assertEqual(output["messages"], 2)

    def test_caption_is_posted_above_the_image(self):
        network = FakeNetwork()
        run(channel_post(photo=PHOTO_SIZES, caption="Certificates are ready",
                         caption_entities=[{"offset": 0, "length": 12, "type": "bold"}]), network)
        blocks = network.posts[0]["blocks"]
        self.assertEqual(blocks[0], {"type": "section", "text": {
            "type": "mrkdwn", "text": ">*Certificates* are ready"}})
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["alt_text"], "Certificates are ready")
        self.assertNotIn("no text in this post", all_text(network.posts))

    def test_missing_files_write_scope_falls_back_to_the_public_preview_image(self):
        network = FakeNetwork(upload_error="missing_scope")
        output, _, _, _ = run(channel_post(photo=PHOTO_SIZES), network)

        preview = network.urls("https://t.me/")[0]
        self.assertEqual(preview["url"], "https://t.me/aidevtoolszoomcamp/81?embed=1&mode=tme")
        self.assertEqual(network.posts[0]["blocks"], [{
            "type": "image", "image_url": PREVIEW_URL,
            "alt_text": "Image from the Telegram post"}])
        self.assertEqual(output["media"], {"kind": "photo", "via": "telegram_preview"})
        self.assertIn("missing_scope", output["media_error"])
        self.assertEqual(network.posts[1]["thread_ts"], "ts-1")

    def test_image_block_is_retried_while_slack_processes_the_file(self):
        network = FakeNetwork(post_errors=["invalid_blocks", "invalid_blocks"])
        output, sleeps, _, _ = run(channel_post(photo=PHOTO_SIZES), network)
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(network.posts[2]["blocks"][0]["slack_file"], {"id": "F0IMAGE"})
        self.assertEqual(output["media"]["via"], "upload")

    def test_photo_nothing_can_show_and_no_caption_is_skipped(self):
        network = FakeNetwork(upload_error="missing_scope", preview=None)
        output, _, _, _ = run(channel_post(photo=PHOTO_SIZES), network)
        self.assertEqual(network.posts, [])
        self.assertTrue(output["skipped"])
        self.assertIn("missing_scope", output["media_error"])

    def test_photo_nothing_can_show_but_caption_still_posts(self):
        network = FakeNetwork(upload_error="missing_scope", preview=None)
        run(channel_post(photo=PHOTO_SIZES, caption="Read this"), network)
        self.assertEqual(network.posts[0]["blocks"], [{"type": "section", "text": {
            "type": "mrkdwn", "text": ">Read this"}}])

    def test_download_errors_never_leak_the_bot_token(self):
        network = FakeNetwork(getfile_status=400)
        output, _, _, _ = run(channel_post(photo=PHOTO_SIZES), network)
        self.assertNotIn(BOT_TOKEN, json.dumps(output))
        self.assertIn("wrong file_id", output["media_error"])
        # The download failed, so the public preview carries the image.
        self.assertEqual(output["media"]["via"], "telegram_preview")

    def test_explicit_telegram_connection_wins_over_the_hook(self):
        network = FakeNetwork()
        action = dict(ACTION, telegram_connection_id="other-bot")
        _, _, get_item, connected = run(channel_post(photo=PHOTO_SIZES), network, action)
        get_item.assert_not_called()
        connected.assert_called_once_with("other-bot")


class FilePostTests(unittest.TestCase):
    DOCUMENT = {"file_id": "doc1", "file_name": "syllabus.pdf", "mime_type": "application/pdf",
                "file_size": 5000}

    def test_document_is_shared_into_the_thread(self):
        network = FakeNetwork()
        output, _, _, _ = run(channel_post(document=self.DOCUMENT), network)
        self.assertEqual(network.posts[0]["blocks"], [{"type": "section", "text": {
            "type": "mrkdwn", "text": "\U0001f4ce *syllabus.pdf*"}}])
        completion = parse_qs(network.urls("completeUploadExternal")[0]["body"].decode())
        self.assertEqual(completion["channel_id"], ["C0AIDEV"])
        self.assertEqual(completion["thread_ts"], ["ts-1"])
        self.assertEqual(output["media"]["kind"], "document")
        self.assertNotIn("no text in this post", all_text(network.posts))

    def test_image_sent_as_a_file_is_shown_as_an_image(self):
        network = FakeNetwork()
        run(channel_post(document={"file_id": "doc2", "file_name": "slide.png",
                                   "mime_type": "image/png"}), network)
        self.assertEqual(network.posts[0]["blocks"][0]["type"], "image")

    def test_text_post_is_unchanged(self):
        network = FakeNetwork()
        event = channel_post(text="hello")
        output, _, get_item, _ = run(event, network)
        self.assertEqual(network.requests, [])
        get_item.assert_not_called()
        self.assertEqual(network.posts[0]["blocks"], [{"type": "section", "text": {
            "type": "mrkdwn", "text": ">hello"}}])
        self.assertNotIn("media", output)


class MediaDetectionTests(unittest.TestCase):
    def test_largest_photo_size_is_chosen_whatever_the_order(self):
        media = slack_action._telegram_media({"message_id": 9, "photo": list(reversed(PHOTO_SIZES))})
        self.assertEqual(media["file_id"], "largest")
        self.assertTrue(media["image"])

    def test_oversized_files_are_not_downloaded(self):
        with self.assertRaises(RuntimeError):
            slack_action._telegram_download("t", {"file_id": "x", "size": 30 * 1024 * 1024},
                                            transport=None)


if __name__ == "__main__":
    unittest.main()
