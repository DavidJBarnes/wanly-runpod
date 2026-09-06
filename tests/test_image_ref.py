"""The image must be able to say which commit built it (wanly-gpu-docker#72).

start.sh has printed `image build: ${GIT_SHA:-unknown}` since the line was added, and it has
always said **unknown** -- the ARG existed and the build never passed it. So an image had no
identity at all, which is half of why two workers running different code went unnoticed for
14 hours: even reading the boot log told you nothing.

The daemon now reports WANLY_IMAGE_REF upstream, so this value stops being decorative.
"""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).parent.parent
DOCKERFILE = ROOT / "Dockerfile"
WORKFLOW = ROOT / ".github/workflows/build-push.yml"


def _build_step():
    d = yaml.safe_load(WORKFLOW.read_text())
    for job in d["jobs"].values():
        for step in job["steps"]:
            if "build-push-action" in str(step.get("uses", "")):
                return step
    raise AssertionError("no docker build step in the workflow")


def test_the_build_passes_the_commit():
    """THE fix. Without this the ARG defaults to 'unknown' and the whole chain is inert."""
    args = _build_step()["with"].get("build-args", "")
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
