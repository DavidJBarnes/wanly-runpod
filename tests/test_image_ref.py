"""The image must be able to say which commit built it (wanly-gpu-docker#72).

start.sh has printed `image build: ${GIT_SHA:-unknown}` since the line was added, and it has
always said **unknown** -- the ARG existed and the build never passed it. So an image had no
identity at all, which is half of why two workers running different code went unnoticed for
14 hours: even reading the boot log told you nothing.

The daemon now reports WANLY_IMAGE_REF upstream, so this value stops being decorative.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).parent.parent
DOCKERFILE = ROOT / "Dockerfile"
WORKFLOW = ROOT / ".github/workflows/build-push.yml"


def _build_args_block() -> str:
    """The build-args block from the docker build step.

    Read with a regex rather than a YAML parser: every other test in this repo is
    stdlib-only, and PyYAML is not installed in CI -- importing it turned a passing suite
    into a collection error, which is a worse outcome than a slightly cruder parse.
    """
    text = WORKFLOW.read_text()
    assert "build-push-action" in text, "no docker build step in the workflow"
    m = re.search(r"^\s*build-args:\s*\|?\s*\n((?:\s+\S.*\n)+)", text, re.M)
    return m.group(1) if m else ""


def test_the_build_passes_the_commit():
    """THE fix. Without this the ARG defaults to 'unknown' and the whole chain is inert."""
    args = _build_args_block()
    assert "GIT_SHA=" in args, "the build does not pass GIT_SHA; the image cannot identify itself"
    assert "github.sha" in args, "GIT_SHA must come from the commit, not a literal"


def test_the_dockerfile_exposes_it_under_the_name_the_daemon_reads():
    """daemon/build_identity.py reads WANLY_IMAGE_REF. A rename on either side silently
    returns the field to null, which from the API is indistinguishable from an older daemon
    that cannot report at all."""
    s = DOCKERFILE.read_text()
    assert re.search(r"^ENV WANLY_IMAGE_REF=\$GIT_SHA", s, re.M), \
        "WANLY_IMAGE_REF is not set from GIT_SHA"


def test_it_still_defaults_rather_than_failing_the_build():
    """A local `docker build` with no --build-arg must still work. An image that refuses to
    build without CI is worse than one that says 'unknown'."""
    assert re.search(r"^ARG GIT_SHA=unknown", DOCKERFILE.read_text(), re.M)


def test_the_boot_log_still_prints_it():
    """The line existed before this change and was always 'unknown'. It is the first place
    anyone looks when two workers disagree, so it must survive."""
    assert "image build:" in (ROOT / "start.sh").read_text()
