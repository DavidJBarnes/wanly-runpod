"""A worker that cannot possibly work must say so in one second (wanly-gpu-docker#77).

A container started by hand -- `docker run <image>` with no `-v` and no `--gpus` -- began
staging 10Eros from Hugging Face at 3 MB/s over the 3090's own complete copy of it, onto a
30-40 GB overlay that could never have held the 43 GB file. Nothing in that boot log said
anything was wrong, because on a pod the very same lines are correct behaviour.

Three things were knowable at second zero and none of them stopped it: no GPU, no models
mount, and nowhere to put 58 GiB. Each is now a refusal, and each is tested here by RUNNING
the real code out of the real script rather than by restating it -- a restated copy passes
while the shipped script regresses, which is the whole shape of this bug.
"""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
START_SH = ROOT / "start.sh"
DOWNLOAD_SH = ROOT / "download_models.sh"


def _extract(path, start_pat, end_pat):
    """Pull a block out of a shell script so the test runs the shipped lines themselves."""
    text = path.read_text()
    m = re.search(f"{start_pat}.*?{end_pat}", text, re.S | re.M)
    assert m, f"could not find {start_pat!r}..{end_pat!r} in {path.name} -- did it move?"
    return m.group(0)


def _run(script, env=None, cwd=None):
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {})}, cwd=cwd,
    )


# --------------------------------------------------------------------------- no GPU

GPU_BLOCK = _extract(START_SH, r"^if ! GPU_LINE=", r"^fi$")


def _fake_nvidia_smi(tmp_path, *, exit_code, stdout=""):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    f = d / "nvidia-smi"
    f.write_text(f'#!/bin/bash\nprintf "%s" {stdout!r}\nexit {exit_code}\n')
    f.chmod(0o755)
    return str(d)


def test_no_gpu_aborts_the_boot(tmp_path):
    """nvidia-smi failing used to print WARNING and carry straight on into a 58 GiB fetch."""
    bindir = _fake_nvidia_smi(tmp_path, exit_code=1)
    r = _run(GPU_BLOCK, env={"PATH": f"{bindir}:{os.environ['PATH']}", "ALLOW_NO_GPU": "0"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FATAL: no GPU visible" in r.stdout
    # The message has to name the actual mistake, or it sends you looking at the GPU.
    assert "--gpus all" in r.stdout


def test_a_silent_nvidia_smi_also_aborts(tmp_path):
    """Exit 0 with no output is not a GPU. Only the non-empty line counts."""
    bindir = _fake_nvidia_smi(tmp_path, exit_code=0, stdout="")
    r = _run(GPU_BLOCK, env={"PATH": f"{bindir}:{os.environ['PATH']}", "ALLOW_NO_GPU": "0"})
    assert r.returncode == 1, r.stdout + r.stderr


def test_a_real_gpu_passes_and_is_printed(tmp_path):
    bindir = _fake_nvidia_smi(tmp_path, exit_code=0, stdout="NVIDIA GeForce RTX 3090, 24576 MiB")
    r = _run(GPU_BLOCK, env={"PATH": f"{bindir}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RTX 3090" in r.stdout


def test_allow_no_gpu_continues_but_says_it_cannot_render(tmp_path):
    """The debug escape hatch must never leave a log that reads like a healthy worker."""
    bindir = _fake_nvidia_smi(tmp_path, exit_code=1)
    r = _run(GPU_BLOCK, env={"PATH": f"{bindir}:{os.environ['PATH']}", "ALLOW_NO_GPU": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CANNOT render" in r.stdout


# ------------------------------------------------------------------- mountpoint parsing

MOUNTPOINT_FN = _extract(DOWNLOAD_SH, r"^_is_mountpoint\(\) \{", r"^\}$")


def _real_mountpoints():
    out = set()
    for line in pathlib.Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 5:
            out.add(fields[4])
    return out


@pytest.mark.parametrize("path", ["/", "/proc"])
def test_known_mountpoints_are_recognised(path):
    if path not in _real_mountpoints():
        pytest.skip(f"{path} is not a mountpoint on this host")
    r = _run(f"{MOUNTPOINT_FN}\n_is_mountpoint {path!r}")
    assert r.returncode == 0, r.stdout + r.stderr


def test_an_ordinary_directory_is_not_a_mountpoint(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    r = _run(f"{MOUNTPOINT_FN}\n_is_mountpoint {str(d)!r}")
    assert r.returncode == 1, r.stdout + r.stderr


def test_a_path_appearing_elsewhere_on_the_line_does_not_match():
    """Field 5 only. `grep " $path "` would match the in-filesystem root or an option string."""
    fake = "36 35 98:0 /workspace/models /somewhere/else rw,noatime - ext4 /dev/root rw\n"
    script = f"""
{MOUNTPOINT_FN.replace('< /proc/self/mountinfo', '< "$FAKE_MOUNTINFO"')}
_is_mountpoint /workspace/models
"""
    with tempfile.NamedTemporaryFile("w", suffix=".mountinfo", delete=False) as fh:
        fh.write(fake)
        name = fh.name
    try:
        r = _run(script, env={"FAKE_MOUNTINFO": name})
        assert r.returncode == 1, "matched a path that is not the mount point"
    finally:
        os.unlink(name)


# ------------------------------------------------ an incomplete bind mount never downloads


def test_incomplete_bind_mount_refuses_instead_of_fetching():
    """End to end, against a real mountpoint: /proc is mounted and holds no models.

    The assertion that matters is the absence of "fetching" -- exiting non-zero would also be
    satisfied by failing *after* starting a download, which is exactly what #77 did.
    """
    if "/proc" not in _real_mountpoints():
        pytest.skip("no /proc mountpoint")
    r = subprocess.run(
        ["bash", str(DOWNLOAD_SH)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "MODELS_DIR": "/proc"},
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "is a bind mount from the host" in r.stdout
    assert "fetching" not in r.stdout, "started a download from a bind-mounted model root"


# --------------------------------------------------------------------- free-space parsing

DF_LINES = _extract(DOWNLOAD_SH, r"^    avail_bytes=0$", r"^    avail_gib=.*?$")


def test_free_space_is_read_as_a_plain_integer(tmp_path):
    """mawk renders a large integer as 1.5e+11 through CONVFMT, which bash cannot subtract.

    That trap already cost this file three bugs, so the free-space read uses no awk at all.
    This asserts the value both parses as an integer and equals what df reports.
    """
    script = f'MODELS={str(tmp_path)!r}\n{DF_LINES}\necho "$avail_gib"'
    r = _run(script)
    assert r.returncode == 0, r.stdout + r.stderr
    got = r.stdout.strip()
    assert re.fullmatch(r"\d+", got), f"not a plain integer: {got!r}"
    expected = shutil.disk_usage(tmp_path).free // (1024 ** 3)
    assert abs(int(got) - expected) <= 1, f"{got} vs df/statvfs {expected}"


# ------------------------------------------------------------------------ the wanted rows


def test_every_wanted_row_declares_a_size():
    """A row missing its size column contributes 0 GiB and silently weakens the precheck."""
    block = _extract(DOWNLOAD_SH, r"^_WANTED=\(", r"^\)$")
    rows = re.findall(r'^\s*"(.+)"\s*$', block, re.M)
    assert len(rows) >= 4, f"expected the four staged models, found {len(rows)}"
    for row in rows:
        fields = row.split("|")
        assert len(fields) == 5, f"{row!r} has {len(fields)} fields, want 5"
        assert fields[4].isdigit() and int(fields[4]) > 0, f"{row!r} has no usable size"
