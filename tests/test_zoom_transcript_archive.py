from pathlib import Path

import yaml

from src.dapier.api import designer_store
from src.dapier.engine import logic


ROOT = Path(__file__).resolve().parents[1]


def test_archive_selects_only_transcripts_and_keeps_recordings_separate():
    workflow = designer_store.parse_workflow(
        (ROOT / "workflows/aisl-recording-agent.yaml").read_text())
    assert workflow["trigger"] == {"connector": "zoom", "event": "recording.transcript_completed"}
    event = {"data": {"meeting_id": "81517877578", "start_time": "2026-10-16T15:00:52Z",
                      "video_files": [
                          {"id": "video", "file_type": "MP4", "download_url": "https://example.test/video"},
                          {"id": "transcript", "file_type": "TRANSCRIPT", "download_url": "https://example.test/transcript"},
                      ]}}
    uploads = []

    def runner(action, event, workflow_id, **kwargs):
        uploads.append(action)
        return {}

    logic.run_chain(workflow["id"], workflow["actions"][:1], event, runner)
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["type"] == "s3_upload"
    assert upload["runtime_identity"] is True
    assert upload["source_connection_id"] == "zoom-api"
    assert upload["source_url"] == "https://example.test/transcript"
    assert upload["key"] == "81517877578/2026-10-16T15:00:52Z/transcript.vtt"


def test_archive_bucket_is_private_retained_and_persistent():
    template = yaml.load((ROOT / "template.yaml").read_text(), Loader=yaml.BaseLoader)
    bucket = template["Resources"]["ZoomTranscriptsBucket"]
    assert bucket["DeletionPolicy"] == bucket["UpdateReplacePolicy"] == "Retain"
    props = bucket["Properties"]
    assert set(props["PublicAccessBlockConfiguration"].values()) == {"true"}
    assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert props["VersioningConfiguration"]["Status"] == "Enabled"
    assert "LifecycleConfiguration" not in props
