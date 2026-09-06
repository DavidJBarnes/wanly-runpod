"""The boot log's config dump must print values, not prose (wanly-gpu-daemon#175).

start.sh writes the daemon .env with a heredoc that includes explanatory comments, then
dumps the resolved file so a parity problem is visible early. The comments went with it --
including one that quoted the very log lines it was explaining:

    ERROR Resource rife49.pth: download failed ... HTTP 404
    ERROR Resource sync failed. Exiting.

So every healthy boot printed two lines that grep as ERROR. One was read as a live failure
during a routine restart, which is what filed the ticket.

The pipeline under test is EXTRACTED FROM start.sh rather than restated here. A copy would
pass while the real dump regressed, which is the failure mode this whole ticket is about.
"""
import pathlib
import re
import subprocess

START_SH = pathlib.Path(__file__).parent.parent / "start.sh"

FAKE_ENV = """QUEUE_URL=http://api.wanly22.com:8001
FRIENDLY_NAME=ltx-abc123
ENGINE=ltx
# EMPTY on purpose. The daemon would take ownership of ComfyUI.
#
#     ERROR Resource rife49.pth: download failed ... HTTP 404
#     ERROR Resource sync failed. Exiting.
#
COMFYUI_PATH=
LORA_CACHE_DIR=/workspace/models/loras
RUNPOD_API_KEY=rpa_supersecret
QUEUE_API_KEY=qk_supersecret
"""


def _dump_pipeline() -> str:
    """The real command from start.sh, between `echo "Daemon config:"` and the blank line."""
    text = START_SH.read_text()
    m = re.search(r'echo "Daemon config:"\n((?:.*\\\n)*.*\n)', text)
    assert m, "could not find the config dump in start.sh — has it been restructured?"
    return m.group(1)


def _run(env_text: str) -> str:
    tmp = pathlib.Path(subprocess.run(["mktemp", "-d"], capture_output=True, text=True)
                       .stdout.strip())
    envfile = tmp / ".env"
    envfile.write_text(env_text)
    script = f'DAEMON_DIR="{tmp}"\n' + _dump_pipeline()
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_no_line_greps_as_an_error():
    """The acceptance criterion: a healthy boot contains no line matching ERROR."""
    out = _run(FAKE_ENV)
    offenders = [ln for ln in out.splitlines() if "ERROR" in ln]
    assert offenders == [], f"config dump still prints ERROR-looking lines: {offenders}"


def test_comments_are_stripped_entirely():
    out = _run(FAKE_ENV)
    assert "#" not in out, f"comments leaked into the boot log:\n{out}"


def test_the_values_all_survive():
    """Stripping must not cost the thing the dump exists for. COMFYUI_PATH= is included
    deliberately: an EMPTY value is meaningful here and must still be visible."""
    out = _run(FAKE_ENV)
    for key in ("QUEUE_URL", "FRIENDLY_NAME", "ENGINE", "COMFYUI_PATH",
                "LORA_CACHE_DIR", "RUNPOD_API_KEY", "QUEUE_API_KEY"):
        assert re.search(rf"^\s*{key}=", out, re.M), f"{key} vanished from the dump"


def test_secrets_are_still_redacted():
    """The comment strip must not have disturbed the redaction it sits in front of."""
    out = _run(FAKE_ENV)
    assert "supersecret" not in out
    assert out.count("<redacted>") == 2
