"""Toolchain preflight for the cache performance framework.

Reports precisely which tools are missing and how to install them, so a broken
environment surfaces here rather than as an obscure failure deep inside a
Verilator invocation.

Standard library only: this has to run before pip exists.

    python3 py/cacheperf/preflight.py [--smoke]
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MIN_PYTHON = (3, 10)
MIN_VERILATOR = (5, 0)

CACHE_ROOT = Path.home() / ".cache" / "cache-perf"

APT_REQUIRED = ["verilator", "build-essential", "python3-pip", "python3-venv"]
APT_OPTIONAL = ["iverilog", "gtkwave"]

# Exercises parameters, always_ff and timing controls -- the constructs the real
# design leans on -- so a pass means more than "verilator is on PATH".
SMOKE_SV = """\
module smoke #(parameter int WIDTH = 8) ();
  logic             clk = 1'b0;
  logic [WIDTH-1:0] ctr = '0;

  always #5 clk = ~clk;

  always_ff @(posedge clk) ctr <= ctr + 1'b1;

  initial begin
    #100;
    if (ctr == 0) $fatal(1, "SMOKE_FAIL: counter did not advance");
    $display("SMOKE_OK width=%0d ctr=%0d", WIDTH, ctr);
    $finish;
  end
endmodule
"""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True
    fix: str = ""


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _parse_version(text: str) -> tuple[int, ...]:
    """Pull a dotted numeric version out of arbitrary tool banner text."""
    for token in text.split():
        parts = token.split(".")
        if parts and all(p.isdigit() for p in parts) and len(parts) >= 2:
            return tuple(int(p) for p in parts)
    return ()


def check_platform() -> Check:
    """The framework runs entirely inside WSL; the Windows Python is not used."""
    if platform.system() != "Linux":
        return Check(
            "platform",
            False,
            f"running on {platform.system()}, expected Linux",
            fix="Run this inside WSL: wsl -d Ubuntu",
        )
    release = platform.release()
    flavour = "WSL" if "microsoft" in release.lower() else "native Linux"
    return Check("platform", True, f"{flavour} ({release})")


def check_python() -> Check:
    v = sys.version_info[:2]
    return Check(
        "python3",
        v >= MIN_PYTHON,
        f"{platform.python_version()}",
        fix=f"Python >= {'.'.join(map(str, MIN_PYTHON))} required",
    )


def check_module(module: str, apt_pkg: str) -> Check:
    code, out = _run([sys.executable, "-c", f"import {module}"])
    return Check(
        module,
        code == 0,
        "importable" if code == 0 else out.splitlines()[-1] if out else "import failed",
        fix=f"sudo apt install -y {apt_pkg}",
    )


def check_tool(
    tool: str, version_flag: str = "--version", *, required: bool = True,
    minimum: tuple[int, ...] = (), apt_pkg: str = "",
) -> Check:
    path = shutil.which(tool)
    fix = f"sudo apt install -y {apt_pkg or tool}"
    if path is None:
        return Check(tool, False, "not found on PATH", required, fix)

    code, out = _run([tool, version_flag])
    banner = out.splitlines()[0] if out else ""
    if minimum:
        found = _parse_version(banner)
        if found and found < minimum:
            want = ".".join(map(str, minimum))
            return Check(
                tool, False, f"{banner} (need >= {want})", required,
                f"Installed {tool} is too old; need >= {want}",
            )
    return Check(tool, True, banner or path, required)


def check_cache_dir() -> Check:
    """Build output lives on the ext4 filesystem, not the slow 9p /mnt/c mount."""
    try:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        probe = CACHE_ROOT / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("cache dir", False, f"{CACHE_ROOT}: {exc}",
                     fix=f"Ensure {CACHE_ROOT} is writable")
    return Check("cache dir", True, str(CACHE_ROOT))


def run_smoke() -> Check:
    """Build and run a trivial model, proving the whole verilator -> g++ -> binary chain."""
    if shutil.which("verilator") is None:
        return Check("smoke test", False, "skipped, verilator not installed",
                     fix="Install the toolchain first")

    with tempfile.TemporaryDirectory(dir=CACHE_ROOT, prefix="smoke-") as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "smoke.sv").write_text(SMOKE_SV)
        code, out = _run([
            "verilator", "--binary", "-j", "0",
            "--Mdir", str(tmpdir / "obj_dir"),
            "-o", "smoke",
            str(tmpdir / "smoke.sv"),
        ])
        if code != 0:
            tail = "\n".join(out.splitlines()[-5:])
            return Check("smoke test", False, f"build failed:\n{tail}",
                         fix="Check that build-essential is installed")

        code, out = _run([str(tmpdir / "obj_dir" / "smoke")])
        if code != 0 or "SMOKE_OK" not in out:
            tail = "\n".join(out.splitlines()[-5:])
            return Check("smoke test", False, f"run failed:\n{tail}")

    return Check("smoke test", True, "verilator builds and runs a model")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--smoke", action="store_true",
                        help="also build and run a trivial Verilated model")
    args = parser.parse_args()

    checks = [
        check_platform(),
        check_python(),
        check_module("venv", "python3-venv"),
        check_module("pip", "python3-pip"),
        check_tool("verilator", minimum=MIN_VERILATOR),
        check_tool("g++", apt_pkg="build-essential"),
        check_tool("make", apt_pkg="build-essential"),
        check_cache_dir(),
        check_tool("iverilog", "-V", required=False),
        check_tool("gtkwave", required=False),
    ]
    if args.smoke:
        checks.append(run_smoke())

    width = max(len(c.name) for c in checks)
    for c in checks:
        if c.ok:
            mark = "ok  "
        elif c.required:
            mark = "MISS"
        else:
            mark = "--  "
        first, *rest = c.detail.splitlines() or [""]
        print(f"  [{mark}] {c.name.ljust(width)}  {first}")
        for line in rest:
            print(f"  {' ' * (width + 9)}{line}")

    failed = [c for c in checks if c.required and not c.ok]
    skipped = [c for c in checks if not c.required and not c.ok]

    if skipped:
        names = ", ".join(c.name for c in skipped)
        print(f"\nOptional tools not installed: {names}")
        print(f"  sudo apt install -y {' '.join(APT_OPTIONAL)}")

    if not failed:
        print("\nToolchain ready.")
        if not args.smoke:
            print("Re-run with --smoke to verify Verilator builds end to end.")
        return 0

    print(f"\n{len(failed)} required check(s) failed:")
    for c in failed:
        print(f"  {c.name}: {c.detail.splitlines()[0]}")

    apt_fixes = [c for c in failed if c.fix.startswith("sudo apt")]
    if apt_fixes:
        print("\nInstall the toolchain with:")
        print(f"  sudo apt install -y {' '.join(APT_REQUIRED)}")
        print("\nOr run: bash scripts/setup_wsl.sh")
    else:
        for c in failed:
            if c.fix:
                print(f"\n{c.name}: {c.fix}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
