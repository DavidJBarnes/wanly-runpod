"""The long-lived worker must converge on :latest, and never mid-render (#72).

The 3090 ran a 37-hour-old image and a 14-hour-old daemon while a RunPod pod ran current
code, and the two produced different results from the same queue. Nothing detected it; it
surfaced as a 422 that looked random.

These tests guard the two halves of the fix: that the container spec is complete enough to
recreate the worker correctly, and that the updater's decision logic is right -- because
every wrong decision it can make is expensive. Recreating mid-render loses 10-13 minutes and
leaves a segment to be reclaimed; never recreating leaves the drift in place.
"""
import os
import pathlib
import shutil
import stat
import subprocess
import textwrap

DEPLOY = pathlib.Path(__file__).parent.parent / "deploy"
RUN = DEPLOY / "run-worker.sh"
UPDATE = DEPLOY / "update-worker.sh"


class TestTheContainerSpecIsComplete:
    """Captured from the running 3090 container on 2026-09-06. A missing mount or port does
    not fail loudly -- the worker boots and then cannot see its models, or the console cannot
    reach ComfyUI, both of which are diagnosed the slow way."""

    def test_every_captured_mount_is_present(self):
        s = RUN.read_text()
        for mount in ("/jobs", "/opt/engine/recipes:ro", "/workspace/models:ro",
                      "/workspace/models/loras"):
            assert mount in s, f"run-worker.sh no longer mounts {mount}"

    def test_the_models_tree_stays_read_only(self):
        """This box is the source of truth for 217 GB of weights. Nothing in the container
        should be able to modify or delete them."""
        assert '"$MODELS_DIR:/workspace/models:ro"' in RUN.read_text()

    def test_both_published_ports(self):
        s = RUN.read_text()
        assert ":8188" in s and ":8190" in s

    def test_it_survives_a_reboot(self):
        assert "--restart unless-stopped" in RUN.read_text()

    def test_it_gets_the_gpu(self):
        assert "--gpus all" in RUN.read_text()

    def test_comfyui_path_is_explicitly_empty(self):
        """Not merely absent. With a path set the daemon takes ownership of ComfyUI's custom
        nodes, which breaks an LTX worker -- and an unset variable in the container would let
        an image default win."""
        assert '-e "COMFYUI_PATH="' in RUN.read_text()

    def test_the_queue_key_is_required_not_defaulted(self):
        """A worker that boots without it registers and then claims nothing, which reads as
        an empty queue."""
        assert 'QUEUE_API_KEY:?' in RUN.read_text()


class TestTheIdleCheck:
    def test_it_asks_inside_the_container(self):
        """The engine binds 127.0.0.1 INSIDE the container, so the -p 8190:8190 mapping
        resolves to nothing. Verified on the 3090: a host-side curl returns empty, which
        parsed naively reads as "idle" and would recreate the container mid-render. This
        very nearly shipped."""
        s = UPDATE.read_text()
        assert 'docker exec "$NAME" curl' in s, \
            "the idle check must run inside the container, not against the published port"


def _fake_docker(tmp, *, running_image, latest_image, busy):
    """A `docker` shim covering exactly the calls update-worker.sh makes."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        case "$1 $2" in
          "inspect wanly-ltx")            echo present; exit 0 ;;
        esac
        case "$*" in
          "inspect -f {{{{.Image}}}} wanly-ltx")  echo "{running_image}"; exit 0 ;;
          "pull -q "*)                            exit 0 ;;
          "image inspect "*)                      echo "{latest_image}"; exit 0 ;;
          *"curl"*)                               echo '{{"queue_depth":0,"running":{busy}}}'; exit 0 ;;
        esac
        exit 0
        """))
    (bin_dir / "docker").chmod(0o755)
    # run-worker.sh is replaced: this test is about the DECISION, not the docker run.
    (bin_dir / "run-worker.sh").write_text("#!/usr/bin/env bash\necho RECREATED\n")
    (bin_dir / "run-worker.sh").chmod(0o755)
    return bin_dir


def _run(tmp_path, *, running_image, latest_image, busy):
    bin_dir = _fake_docker(tmp_path, running_image=running_image,
                           latest_image=latest_image, busy=busy)
    stage = tmp_path / "deploy"
    stage.mkdir(exist_ok=True)
    shutil.copy(UPDATE, stage / "update-worker.sh")
    shutil.copy(bin_dir / "run-worker.sh", stage / "run-worker.sh")
    (stage / "run-worker.sh").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(stage / "update-worker.sh")],
                          capture_output=True, text=True, env=env)


class TestTheDecision:
    SAME = "sha256:aaa"
    NEW = "sha256:bbb"

    def test_it_does_nothing_when_the_image_has_not_changed(self, tmp_path):
        """It runs on a timer against an image that is almost always unchanged, so the
        no-op path is the common one and must be silent and cheap."""
        r = _run(tmp_path, running_image=self.SAME, latest_image=self.SAME, busy=0)
        assert r.returncode == 0
        assert "nothing to do" in r.stdout
        assert "RECREATED" not in r.stdout

    def test_it_recreates_when_the_image_changed_and_the_worker_is_idle(self, tmp_path):
        r = _run(tmp_path, running_image=self.SAME, latest_image=self.NEW, busy=0)
        assert r.returncode == 0
        assert "RECREATED" in r.stdout, r.stdout

    def test_it_leaves_a_busy_worker_alone(self, tmp_path):
        """THE one that matters. A render is 10-13 minutes; interrupting it loses the work
        AND leaves the segment to be reclaimed, costing more than the drift it fixes."""
        r = _run(tmp_path, running_image=self.SAME, latest_image=self.NEW, busy=1)
        assert r.returncode == 0
        assert "RECREATED" not in r.stdout, "recreated the container mid-render"
        assert "in flight" in r.stdout

    def test_an_unreadable_engine_counts_as_busy(self, tmp_path):
        """Fail SAFE. An unreachable engine is not evidence of idleness -- it may be
        mid-boot, and recreating then interrupts model staging."""
        bin_dir = _fake_docker(tmp_path, running_image=self.SAME,
                               latest_image=self.NEW, busy=0)
        (bin_dir / "docker").write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            case "$*" in
              "inspect -f {{{{.Image}}}} wanly-ltx")  echo "{self.SAME}"; exit 0 ;;
              "pull -q "*)                            exit 0 ;;
              "image inspect "*)                      echo "{self.NEW}"; exit 0 ;;
              *"curl"*)                               exit 7 ;;
            esac
            exit 0
            """))
        (bin_dir / "docker").chmod(0o755)
        stage = tmp_path / "deploy"; stage.mkdir(exist_ok=True)
        shutil.copy(UPDATE, stage / "update-worker.sh")
        shutil.copy(bin_dir / "run-worker.sh", stage / "run-worker.sh")
        (stage / "run-worker.sh").chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        r = subprocess.run(["bash", str(stage / "update-worker.sh")],
                           capture_output=True, text=True, env=env)
        assert "RECREATED" not in r.stdout
        assert "assuming busy" in r.stdout


def test_the_scripts_are_executable():
    for p in (RUN, UPDATE):
        assert os.stat(p).st_mode & stat.S_IXUSR, f"{p.name} is not executable"


def test_no_secret_is_committed():
    """worker.env carries the queue key and must never be in the repo."""
    assert not (DEPLOY / "worker.env").exists()
    assert "QUEUE_API_KEY=" in (DEPLOY / "worker.env.example").read_text()
    assert (DEPLOY / "worker.env.example").read_text().count("QUEUE_API_KEY=\n") == 1
