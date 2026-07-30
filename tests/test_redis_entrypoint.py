import os
import subprocess
from pathlib import Path


def test_redis_entrypoint_escapes_password_and_keeps_it_out_of_argv(tmp_path) -> None:
    project_root = Path(__file__).parents[1]
    entrypoint = project_root / "docker" / "redis-entrypoint.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_server = fake_bin / "redis-server"
    capture = tmp_path / "redis.conf.captured"
    argv_capture = tmp_path / "argv.txt"
    mode_capture = tmp_path / "mode.txt"

    fake_server.write_text(
        "#!/bin/sh\n"
        'cp "$1" "$CAPTURE_PATH"\n'
        'printf \'%s\\n\' "$@" > "$ARGV_PATH"\n'
        'stat -c %a "$1" > "$MODE_PATH"\n',
        encoding="utf-8",
    )
    fake_server.chmod(0o755)

    password = 'space quote" slash\\ hash# snowman☃'
    environment = os.environ.copy()
    environment.update(
        {
            "ARGV_PATH": str(argv_capture),
            "CAPTURE_PATH": str(capture),
            "MODE_PATH": str(mode_capture),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "REDIS_PASSWORD": password,
            "TMPDIR": str(tmp_path),
        }
    )

    subprocess.run(
        ["/bin/sh", str(entrypoint)],
        check=True,
        env=environment,
        text=True,
        capture_output=True,
    )

    escaped = "".join(f"\\x{byte:02x}" for byte in password.encode("utf-8"))
    assert capture.read_text(encoding="utf-8") == f'requirepass "{escaped}"\n'
    assert mode_capture.read_text(encoding="utf-8").strip() == "600"
    assert password not in argv_capture.read_text(encoding="utf-8")
