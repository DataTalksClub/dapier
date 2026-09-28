"""Action-staple tests for the YouTube/S3/Zoom round:
youtube_add_to_playlist, youtube_update_video, s3_read_object,
s3_presign_url, s3_delete_object, zoom_update_meeting, zoom_add_registrant.

Unit tests drive each registered runner with a fake provider transport (or a
fake boto3 S3 client — s3.py's seam) and the connection/token/credential
seams patched (the test_round_sheetstelegram / test_dropbox_read_file
pattern), asserting method, URL, request body and the step-output shape. The
registry half checks the new types' field specs, save-time field typing, and
that validate_action_chain accepts a valid chain and rejects missing
required fields, unknown keys and mistyped literals.
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from src.dapier.engine.actions.s3 import (
    run_s3_delete_object,
    run_s3_presign_url,
    run_s3_read_object,
)
from src.dapier.engine.actions.youtube import (
    run_youtube_add_to_playlist,
    run_youtube_update_video,
)
from src.dapier.engine.actions.zoom import (
    run_zoom_add_registrant,
    run_zoom_update_meeting,
)

import src.dapier.connectors  # noqa: F401  (import = registration)


YOUTUBE_CONNECTION = {"connection_id": "google", "provider": "youtube",
                      "status": "connected", "credential_id": "oauth#google"}
ZOOM_CONNECTION = {"connection_id": "zoom-main", "provider": "zoom",
                   "status": "connected", "credential_id": "oauth#zoom-main"}
AWS_KEYS = {"access_key_id": "AKIAEXAMPLE0000", "secret_access_key": "b" * 40}

EVENT = {"id": "evt/1", "connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"video_id": "dQw4w9WgXcQ", "subject": "Standup"}}


class FakeTransport:
    """Records every call; routes by URL substring to (status, body bytes)."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, body) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body, "timeout": timeout})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, payload
        raise AssertionError(f"unexpected provider call: {method} {url}")


class FakeS3:
    """A boto3 S3 client seam: records calls, hands back canned responses."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __getattr__(self, name):
        def _call(**kwargs):
            self.calls.append({"op": name, **kwargs})
            if name in self.responses:
                response = self.responses[name]
                return response() if callable(response) else response
            return {}
        return _call


def json_body(payload):
    return json.dumps(payload).encode()


def google_response(snippet):
    return json_body({"id": "vid-9", "snippet": snippet})


# --- youtube_add_to_playlist ----------------------------------------------------


def run_playlist_add(transport, action, event=None, steps=None):
    action = {"type": "youtube_add_to_playlist", "connection_id": "google", **action}
    with patch("src.dapier.engine.actions.youtube._youtube_connection",
               return_value=dict(YOUTUBE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_youtube_add_to_playlist(action, event or EVENT, steps=steps,
                                           transport=transport)


class YoutubeAddToPlaylistTests(unittest.TestCase):
    def test_posts_the_snippet_resource_and_returns_the_listing(self):
        transport = FakeTransport(
            ("playlistItems", 200, json_body({
                "id": "PI-1",
                "snippet": {"playlistId": "PL-1", "position": 3,
                            "title": "Deploying dapier",
                            "resourceId": {"kind": "youtube#video",
                                           "videoId": "vid-9"}},
            })))

        output = run_playlist_add(transport, {"playlist_id": "PL-1",
                                              "video_id": "vid-9"})

        self.assertEqual(output, {
            "playlist_id": "PL-1", "video_id": "vid-9",
            "playlist_item_id": "PI-1", "title": "Deploying dapier",
            "position": 3})
        call, = transport.calls
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"],
            "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(call["body"]), {"snippet": {
            "playlistId": "PL-1",
            "resourceId": {"kind": "youtube#video", "videoId": "vid-9"},
        }})

    def test_ids_take_templates(self):
        transport = FakeTransport(("playlistItems", 200, json_body({"id": "PI-2"})))

        output = run_playlist_add(
            transport,
            {"playlist_id": "{steps.find.output.playlist_id}",
             "video_id": "{trigger.video_id}"},
            steps={"find": {"output": {"playlist_id": "PL-7"}}})

        self.assertEqual(output["playlist_id"], "PL-7")
        self.assertEqual(output["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(json.loads(transport.calls[0]["body"])["snippet"],
                         {"playlistId": "PL-7",
                          "resourceId": {"kind": "youtube#video",
                                         "videoId": "dQw4w9WgXcQ"}})

    def test_errors_surface_the_api_message(self):
        transport = FakeTransport(
            ("playlistItems", 403,
             json_body({"error": {"message": "The user is not allowed"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_playlist_add(transport, {"playlist_id": "PL-1",
                                         "video_id": "vid-9"})
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("The user is not allowed", str(caught.exception))

    def test_requires_playlist_and_video(self):
        transport = FakeTransport()

        for action in ({"video_id": "vid-9"}, {"playlist_id": "PL-1"}):
            with self.assertRaises(ValueError):
                run_playlist_add(transport, action)
        self.assertEqual(transport.calls, [])


# --- youtube_update_video -------------------------------------------------------


def run_video_update(transport, action, event=None, steps=None):
    action = {"type": "youtube_update_video", "connection_id": "google", **action}
    with patch("src.dapier.engine.actions.youtube._youtube_connection",
               return_value=dict(YOUTUBE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_youtube_update_video(action, event or EVENT, steps=steps,
                                        transport=transport)


class YoutubeUpdateVideoTests(unittest.TestCase):
    def test_puts_the_snippet_with_every_given_field(self):
        transport = FakeTransport(
            ("v3/videos", 200, google_response({
                "title": "Deploying dapier (2026)", "description": "Updated",
                "categoryId": "28"})))

        output = run_video_update(transport, {"video_id": "vid-9",
                                              "title": "Deploying dapier (2026)",
                                              "description": "Updated",
                                              "category_id": "28"})

        self.assertEqual(output, {"updated": True, "video_id": "vid-9",
                                  "title": "Deploying dapier (2026)",
                                  "description": "Updated", "category_id": "28"})
        call, = transport.calls
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(
            call["url"], "https://www.googleapis.com/youtube/v3/videos?part=snippet")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(json.loads(call["body"]), {
            "id": "vid-9",
            "snippet": {"title": "Deploying dapier (2026)",
                        "description": "Updated", "categoryId": "28"},
        })

    def test_only_title_is_sent_when_nothing_else_given(self):
        transport = FakeTransport(
            ("v3/videos", 200, google_response({"title": "New"})))

        output = run_video_update(transport, {"video_id": "vid-9",
                                              "title": "New"})

        self.assertEqual(json.loads(transport.calls[0]["body"])["snippet"],
                         {"title": "New"})
        self.assertIsNone(output["description"])
        self.assertIsNone(output["category_id"])

    def test_title_takes_a_template(self):
        transport = FakeTransport(
            ("v3/videos", 200, google_response({"title": "Standup"})))

        run_video_update(transport, {"video_id": "{trigger.video_id}",
                                     "title": "{subject}"})

        self.assertEqual(json.loads(transport.calls[0]["body"]),
                         {"id": "dQw4w9WgXcQ", "snippet": {"title": "Standup"}})

    def test_requires_video_and_title(self):
        transport = FakeTransport()

        for action in ({"title": "x"}, {"video_id": "vid-9"}):
            with self.assertRaises(ValueError):
                run_video_update(transport, action)
        self.assertEqual(transport.calls, [])

    def test_errors_surface_the_api_message(self):
        transport = FakeTransport(
            ("v3/videos", 403,
             json_body({"error": {"message": "forbidden"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_video_update(transport, {"video_id": "vid-9", "title": "New"})
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("forbidden", str(caught.exception))


# --- s3_read_object -------------------------------------------------------------


class FakeBody:
    """boto3's StreamingBody seam: the bytes behind ``.read()``."""

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload


def s3_response(body, content_type=None):
    return lambda: {"Body": FakeBody(body), "ContentType": content_type}


def run_read(action, event=None, *, s3_client=None, steps=None):
    action = {"type": "s3_read_object", "credential_id": "aws", **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value=dict(AWS_KEYS)), \
         patch.dict("os.environ", {"RENDER_ARTIFACTS_BUCKET": "artifacts"}):
        return run_s3_read_object(action, event or EVENT, steps=steps,
                                  s3_client=s3_client)


class S3ReadObjectTests(unittest.TestCase):
    def test_stages_the_object_and_names_the_ref(self):
        s3 = FakeS3({"get_object": s3_response(b"%PDF fake bytes",
                                               "application/pdf")})

        output = run_read({"bucket": "backups", "key": "reports/report.pdf"},
                          s3_client=s3)

        self.assertEqual(output, {
            "filename": "report.pdf", "size": 15,
            "content_type": "application/pdf",
            "bucket": "artifacts", "key": "s3/evt_1/read/report.pdf",
            "source_bucket": "backups", "source_key": "reports/report.pdf"})
        get_call, put_call = s3.calls
        self.assertEqual(get_call["op"], "get_object")
        self.assertEqual(get_call["Bucket"], "backups")
        self.assertEqual(get_call["Key"], "reports/report.pdf")
        self.assertEqual(put_call["op"], "put_object")
        self.assertEqual(put_call["Bucket"], "artifacts")
        self.assertEqual(put_call["Body"], b"%PDF fake bytes")
        self.assertEqual(put_call["ContentType"], "application/pdf")
        self.assertEqual(put_call["ServerSideEncryption"], "AES256")

    def test_key_defaults_to_the_event_object(self):
        s3 = FakeS3({"get_object": s3_response(b"a,b\n1,2\n", "text/csv")})
        event = {"id": "e2", "connector": "s3", "event": "file.created",
                 "data": {"key": "in/2026/exports.csv"}}

        output = run_read({"bucket": "backups"}, event=event, s3_client=s3)

        self.assertEqual(s3.calls[0]["Key"], "in/2026/exports.csv")
        self.assertEqual(output["filename"], "exports.csv")
        self.assertEqual(output["content_type"], "text/csv")
        self.assertEqual(output["source_key"], "in/2026/exports.csv")

    def test_content_type_falls_back_to_the_filename_guess(self):
        s3 = FakeS3({"get_object": s3_response(b"bytes")})

        output = run_read({"bucket": "backups", "key": "x/notes.txt"},
                          s3_client=s3)

        self.assertEqual(output["content_type"], "text/plain")

    def test_requires_bucket_then_key(self):
        s3 = FakeS3()

        with self.assertRaises(ValueError):
            run_read({"key": "x"}, s3_client=s3)
        with self.assertRaises(ValueError):
            run_read({"bucket": "backups"}, event={"data": {}}, s3_client=s3)
        self.assertEqual(s3.calls, [])

    def test_registry_dispatch_stages_with_the_default_client(self):
        s3 = FakeS3({"get_object": s3_response(b"bytes", "application/pdf")})
        with patch("boto3.client", return_value=s3), \
             patch("src.dapier.connections.credentials.get_credential",
                   return_value=dict(AWS_KEYS)), \
             patch.dict("os.environ", {"RENDER_ARTIFACTS_BUCKET": "artifacts"}):
            output = registry.run_action(
                {"type": "s3_read_object", "bucket": "backups",
                 "key": "a/report.pdf", "id": "fetch"},
                {"id": "evt/9", "data": {}}, "wf-1", steps={})

        self.assertEqual(output["key"], "s3/evt_9/fetch/report.pdf")
        self.assertEqual(len(s3.calls), 2)  # one get, one staged put


# --- s3_presign_url -------------------------------------------------------------


def run_presign(action, event=None, *, s3_client=None, steps=None):
    action = {"type": "s3_presign_url", "credential_id": "aws", **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value=dict(AWS_KEYS)):
        return run_s3_presign_url(action, event or EVENT, steps=steps,
                                  s3_client=s3_client)


class FakePresigner(FakeS3):
    """generate_presigned_url records its arguments, mints a fake link."""

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
        self.calls.append({"op": "generate_presigned_url", "operation": operation,
                           "Params": Params, "ExpiresIn": ExpiresIn})
        return (f"https://s3.test/{Params['Bucket']}/{Params['Key']}"
                f"?X-Amz-Expires={ExpiresIn}")


class S3PresignUrlTests(unittest.TestCase):
    def test_presigns_a_get_for_one_hour_by_default(self):
        s3 = FakePresigner()

        output = run_presign({"bucket": "backups", "key": "reports/a.pdf"},
                             s3_client=s3)

        self.assertEqual(output, {
            "link": "https://s3.test/backups/reports/a.pdf?X-Amz-Expires=3600",
            "bucket": "backups", "key": "reports/a.pdf", "expires_in": 3600})
        call, = s3.calls
        self.assertEqual(call["operation"], "get_object")
        self.assertEqual(call["Params"], {"Bucket": "backups",
                                          "Key": "reports/a.pdf"})
        self.assertEqual(call["ExpiresIn"], 3600)
        self.assertEqual(len(s3.calls), 1)  # computed, never sent

    def test_custom_expiry_clamps_at_the_seven_day_ceiling(self):
        for raw, expected in (("90", 90), ("999999999", 604800)):
            s3 = FakePresigner()
            output = run_presign({"bucket": "b", "key": "k", "expires_in": raw},
                                 s3_client=s3)
            self.assertEqual(output["expires_in"], expected)

    def test_invalid_expiry_raises_before_any_call(self):
        for raw in ("soon", "0", "-5"):
            s3 = FakePresigner()
            with self.assertRaises(ValueError):
                run_presign({"bucket": "b", "key": "k", "expires_in": raw},
                            s3_client=s3)
            self.assertEqual(s3.calls, [])

    def test_bucket_and_key_take_templates(self):
        s3 = FakePresigner()

        output = run_presign(
            {"bucket": "{steps.find.output.bucket}",
             "key": "{steps.find.output.key}"},
            steps={"find": {"output": {"bucket": "staging",
                                       "key": "x/invoice.pdf"}}},
            s3_client=s3)

        self.assertEqual(output["link"],
                         "https://s3.test/staging/x/invoice.pdf?X-Amz-Expires=3600")

    def test_requires_bucket_and_key(self):
        s3 = FakePresigner()
        with self.assertRaises(ValueError):
            run_presign({"key": "k"}, s3_client=s3)
        with self.assertRaises(ValueError):
            run_presign({"bucket": "b"}, s3_client=s3)
        self.assertEqual(s3.calls, [])


# --- s3_delete_object -----------------------------------------------------------


def run_delete(action, event=None, *, s3_client=None, steps=None):
    action = {"type": "s3_delete_object", "credential_id": "aws", **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value=dict(AWS_KEYS)):
        return run_s3_delete_object(action, event or EVENT, steps=steps,
                                    s3_client=s3_client)


class S3DeleteObjectTests(unittest.TestCase):
    def test_deletes_the_rendered_key(self):
        s3 = FakeS3()

        output = run_delete({"bucket": "backups", "key": "tmp/{subject}.pdf"},
                            s3_client=s3)

        self.assertEqual(output, {"deleted": True, "bucket": "backups",
                                  "key": "tmp/Standup.pdf"})
        call, = s3.calls
        self.assertEqual(call["op"], "delete_object")
        self.assertEqual(call["Bucket"], "backups")
        self.assertEqual(call["Key"], "tmp/Standup.pdf")

    def test_requires_bucket_and_key(self):
        s3 = FakeS3()
        for action in ({"key": "k"}, {"bucket": "b"}):
            with self.assertRaises(ValueError):
                run_delete(action, s3_client=s3)
        self.assertEqual(s3.calls, [])

    def test_registry_dispatch_deletes(self):
        s3 = FakeS3()
        with patch("boto3.client", return_value=s3), \
             patch("src.dapier.connections.credentials.get_credential",
                   return_value=dict(AWS_KEYS)):
            output = registry.run_action(
                {"type": "s3_delete_object", "bucket": "backups",
                 "key": "tmp/old.pdf"}, EVENT, "wf-1", steps={})

        self.assertEqual(output, {"deleted": True, "bucket": "backups",
                                  "key": "tmp/old.pdf"})


# --- zoom_update_meeting --------------------------------------------------------


def run_meeting_update(transport, action, event=None, steps=None):
    action = {"type": "zoom_update_meeting", "connection_id": "zoom-main", **action}
    with patch("src.dapier.engine.actions.zoom._zoom_connection",
               return_value=dict(ZOOM_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_zoom_update_meeting(action, event or EVENT, steps=steps,
                                       transport=transport)


class ZoomUpdateMeetingTests(unittest.TestCase):
    def test_patches_only_the_given_fields(self):
        transport = FakeTransport(("/meetings/9100", 204, b""))

        output = run_meeting_update(
            transport, {"meeting_id": "9100",
                        "start_time": "2026-10-02T09:00:00Z", "duration": "45"})

        self.assertEqual(output, {"updated": True, "meeting_id": "9100",
                                  "updated_fields": ["duration", "start_time"]})
        call, = transport.calls
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(call["url"], "https://api.zoom.us/v2/meetings/9100")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(call["body"]),
                         {"start_time": "2026-10-02T09:00:00Z", "duration": 45})

    def test_topic_timezone_agenda_settings_ride_along(self):
        transport = FakeTransport(("/meetings/9100", 204, b""))

        output = run_meeting_update(
            transport, {"meeting_id": "9100", "topic": "Renamed",
                        "timezone": "Europe/Berlin", "agenda": "Agenda",
                        "settings": '{{"join_before_host": true}}',
                        "duration": "{steps.form.output.minutes}"},
            steps={"form": {"output": {"minutes": "30"}}})

        self.assertEqual(output["updated_fields"],
                         ["agenda", "duration", "settings", "timezone", "topic"])
        self.assertEqual(json.loads(transport.calls[0]["body"]), {
            "topic": "Renamed", "duration": 30, "timezone": "Europe/Berlin",
            "agenda": "Agenda", "settings": {"join_before_host": True}})

    def test_updating_nothing_is_an_error_before_any_call(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_meeting_update(transport, {"meeting_id": "9100"})
        self.assertIn("at least one", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_bad_start_time_and_duration_are_rejected_before_any_call(self):
        transport = FakeTransport()

        for action in ({"meeting_id": "9100", "start_time": "tuesday"},
                       {"meeting_id": "9100", "duration": "forty"},
                       {"meeting_id": "9100", "duration": "0"}):
            with self.assertRaises(ValueError):
                run_meeting_update(transport, action)
        self.assertEqual(transport.calls, [])

    def test_zoom_error_surfaces_status_and_message(self):
        transport = FakeTransport(
            ("/meetings/9100", 404, json_body({"message": "Meeting not found"})))

        with self.assertRaises(RuntimeError) as caught:
            run_meeting_update(transport, {"meeting_id": "9100",
                                           "topic": "New"})
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertIn("Meeting not found", str(caught.exception))


# --- zoom_add_registrant --------------------------------------------------------


def run_registrant_add(transport, action, event=None, steps=None):
    action = {"type": "zoom_add_registrant", "connection_id": "zoom-main", **action}
    with patch("src.dapier.engine.actions.zoom._zoom_connection",
               return_value=dict(ZOOM_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_zoom_add_registrant(action, event or EVENT, steps=steps,
                                       transport=transport)


class ZoomAddRegistrantTests(unittest.TestCase):
    def test_posts_the_registrant_and_returns_the_join_url(self):
        transport = FakeTransport(
            ("registrants", 201, json_body({
                "registrant_id": "reg-1", "id": 9100,
                "join_url": "https://zoom.us/j/9100?pwd=reg-1"})))

        output = run_registrant_add(
            transport, {"meeting_id": "9100", "email": "ada@example.test",
                        "first_name": "Ada", "last_name": "Lovelace"})

        self.assertEqual(output, {
            "registered": True, "meeting_id": "9100",
            "registrant_id": "reg-1",
            "join_url": "https://zoom.us/j/9100?pwd=reg-1"})
        call, = transport.calls
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"],
                         "https://api.zoom.us/v2/meetings/9100/registrants")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(json.loads(call["body"]), {
            "email": "ada@example.test", "first_name": "Ada",
            "last_name": "Lovelace"})

    def test_names_are_optional(self):
        transport = FakeTransport(
            ("registrants", 201, json_body({"registrant_id": "reg-2"})))

        output = run_registrant_add(
            transport, {"meeting_id": "9100", "email": "{trigger.email}"},
            event={"data": {"email": "grace@example.test"}})

        self.assertEqual(output["registrant_id"], "reg-2")
        self.assertEqual(json.loads(transport.calls[0]["body"]),
                         {"email": "grace@example.test"})

    def test_meeting_and_email_are_required(self):
        transport = FakeTransport()
        for action in ({"email": "ada@example.test"},
                       {"meeting_id": "9100"}):
            with self.assertRaises(ValueError):
                run_registrant_add(transport, action)
        self.assertEqual(transport.calls, [])

    def test_zoom_error_surfaces_status_and_message(self):
        transport = FakeTransport(
            ("registrants", 400,
             json_body({"message": "Meeting does not require registration"})))

        with self.assertRaises(RuntimeError) as caught:
            run_registrant_add(transport, {"meeting_id": "9100",
                                           "email": "ada@example.test"})
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("does not require registration", str(caught.exception))


# --- registry wiring ------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_new_actions_are_registered_with_their_specs(self):
        specs = registry.action_specs()
        self.assertEqual(specs["youtube_add_to_playlist"],
                         ({"connection_id", "playlist_id", "video_id"},
                          frozenset()))
        self.assertEqual(specs["youtube_update_video"],
                         ({"connection_id", "video_id", "title"},
                          {"description", "category_id"}))
        self.assertEqual(specs["s3_read_object"],
                         ({"bucket"}, {"key", "credential_id", "connection_id"}))
        self.assertEqual(specs["s3_presign_url"],
                         ({"bucket", "key"},
                          {"expires_in", "credential_id", "connection_id"}))
        self.assertEqual(specs["s3_delete_object"],
                         ({"bucket", "key"},
                          {"credential_id", "connection_id"}))
        self.assertEqual(specs["zoom_update_meeting"],
                         ({"connection_id", "meeting_id"},
                          {"topic", "start_time", "duration", "timezone",
                           "agenda", "settings"}))
        self.assertEqual(specs["zoom_add_registrant"],
                         ({"connection_id", "meeting_id", "email"},
                          {"first_name", "last_name"}))

    def test_save_time_field_typing(self):
        def field(type_, key):
            return next(field for field in registry.ACTIONS[type_].fields
                        if field["key"] == key)
        self.assertEqual(field("zoom_update_meeting", "duration")["type"], "number")
        self.assertEqual(field("zoom_add_registrant", "email")["type"], "email")
        self.assertEqual(field("s3_presign_url", "expires_in")["type"], "number")
        self.assertEqual(
            field("youtube_add_to_playlist", "playlist_id")["discover"],
            {"resource": "youtube.playlists"})
        self.assertEqual(
            field("zoom_update_meeting", "meeting_id")["discover"],
            {"resource": "zoom.meetings"})

    def test_a_chain_using_the_new_actions_validates(self):
        registry.validate_action_chain([
            {"type": "youtube_find_playlist_items", "connection_id": "google",
             "playlist_id": "PL-1"},
            {"type": "youtube_add_to_playlist", "connection_id": "google",
             "playlist_id": "{steps.p.output.playlist_id}",
             "video_id": "{trigger.video_id}"},
            {"type": "youtube_update_video", "connection_id": "google",
             "video_id": "{trigger.video_id}", "title": "{subject}"},
            {"type": "s3_presign_url", "credential_id": "aws",
             "bucket": "backups", "key": "reports/a.pdf", "expires_in": 3600},
            {"type": "s3_read_object", "credential_id": "aws",
             "bucket": "backups"},
            {"type": "zoom_update_meeting", "connection_id": "zoom-main",
             "meeting_id": "9100", "duration": 45},
            {"type": "zoom_add_registrant", "connection_id": "zoom-main",
             "meeting_id": "9100", "email": "{trigger.email}"},
        ])

    def test_validation_rejects_missing_and_unknown_and_mistyped(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "youtube_add_to_playlist", "connection_id": "google",
                 "playlist_id": "PL-1"}])
        self.assertIn("missing: video_id", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "s3_delete_object", "bucket": "b", "key": "k",
                 "prefix": "x"}])
        self.assertIn("unknown keys: prefix", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "zoom_update_meeting", "connection_id": "z",
                 "meeting_id": "9100", "duration": "forty"}])
        self.assertIn("duration", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "zoom_add_registrant", "connection_id": "z",
                 "meeting_id": "9100", "email": "not-an-email"}])
        self.assertIn("email", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
