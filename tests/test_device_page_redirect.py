"""The /device page bounces raw API hosts to the canonical console origin.

The frontend half of the DataOps parity (bba21c7f): a verification URI
can carry a raw Lambda or execute-api host, and the sign-in session that
approves a pairing lives on the canonical origin only. The page redirects
those arrivals, keeping the code; the canonical host, localhost, and any
custom domain stay put.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.dapier.api.router import _static

WEB = Path(__file__).resolve().parents[1] / "src" / "web"
DEVICE_JS = WEB / "js" / "device.js"


def _redirect_source():
    source = DEVICE_JS.read_text()
    return source.split("/* ---- canonical host ---- */", 1)[1].split(
        "/* ---- end canonical host ---- */", 1)[0]


def test_redirect_decisions():
    """Boot passes the formatted code; raw AWS hosts bounce, others stay."""
    if not shutil.which("node"):
        pytest.skip("node is needed to evaluate device page helpers")
    script = _redirect_source() + """
const cases = [
  ['a1b2c3d4e5.execute-api.eu-west-1.amazonaws.com', 'ABCD-EFGH'],
  ['pair.example.lambda-url.eu-west-1.on.aws', 'AB2C-3D4F'],
  ['shortname.on.aws', ''],
  ['dapier.dtcdev.click', 'ABCD-EFGH'],
  ['localhost', 'ABCD-EFGH'],
  ['127.0.0.1', ''],
  ['console.staging.example.test', 'AB2C-3D4F'],
];
console.log(JSON.stringify(cases.map(function (pair) {
  return canonicalDeviceRedirect(pair[0], pair[1]);
})));
"""
    out = subprocess.check_output(
        ["node", "--input-type=module", "-e", script], text=True)
    assert json.loads(out) == [
        "https://dapier.dtcdev.click/device?code=ABCD-EFGH",
        "https://dapier.dtcdev.click/device?code=AB2C-3D4F",
        "https://dapier.dtcdev.click/device",
        None,
        None,
        None,
        None,
    ]


def test_boot_redirects_before_anything_renders():
    source = DEVICE_JS.read_text()
    assert "canonicalDeviceRedirect(window.location.hostname, code)" in source
    assert "window.location.replace(bounce)" in source
    assert source.index("/* ---- canonical host ---- */") \
        < source.index("var params = new URLSearchParams")


def test_served_device_js_carries_the_redirect():
    served = _static("/assets/js/device.js")
    assert served is not None and served["statusCode"] == 200
    assert "canonicalDeviceRedirect" in served["body"]
