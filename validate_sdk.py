#!/usr/bin/env python3
"""Re-run the pinned patched SDK gate in a fresh temporary source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def file_map(root: Path, paths: list[str]) -> dict[str, str | None]:
    return {name: sha(root / name) if (root / name).is_file() else None for name in paths}


def source_identity(root: Path) -> str:
    files = {
        str(path.relative_to(root)): sha(path)
        for path in sorted((root / "src/agents").rglob("*.py"))
    }
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def captured(command: list[str], cwd: Path, env: dict[str, str], out: Path, name: str) -> dict:
    started = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    log_path = out / f"{name}.log"
    with log_path.open("xb") as log:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
    receipt = {
        "command": command,
        "cwd": str(cwd),
        "started_utc": started_utc,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.monotonic() - started,
        "exit_code": result.returncode,
        "log": log_path.name,
        "log_sha256": sha(log_path),
    }
    save(out / f"{name}-command.json", receipt)
    print(f"{name}: exit {result.returncode} ({receipt['elapsed_seconds']:.1f}s)", flush=True)
    return receipt


def parse_tests(log: str) -> dict:
    summaries = []
    for line in log.splitlines():
        if line.startswith("=") and re.search(r"\b\d+ passed\b", line):
            numbers = {
                key: int(number)
                for number, key in re.findall(
                    r"(\d+) (passed|skipped|failed|deselected|errors?)\b", line
                )
            }
            numbers["raw_summary"] = line
            summaries.append(numbers)
    if len(summaries) != 2:
        return {"parsed": False, "summaries": summaries}
    return {
        "parsed": True,
        "parallel": summaries[0],
        "serial": summaries[1],
        "total_passed": sum(item.get("passed", 0) for item in summaries),
        "total_skipped": sum(item.get("skipped", 0) for item in summaries),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv", help="uv executable compatible with bundled uv.lock")
    parser.add_argument("--python", default="3.13", help="Interpreter path or uv Python selector")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/sdk-verification")
    args = parser.parse_args()
    uv = shutil.which(args.uv)
    if uv is None:
        raise SystemExit(f"uv executable not found: {args.uv}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fresh = Path(tempfile.mkdtemp(prefix="agents-terminal-sdk-gate-")).resolve()
    sdk = fresh / "sdk"
    sdk.mkdir()
    archive = ROOT / "vendor/sdk-upstream.tar.gz"
    patch = ROOT / "artifacts/upstream.patch"
    implementation = ROOT / "artifacts/implementation.json"
    pins = json.loads(implementation.read_text())
    input_names = [str(path.relative_to(ROOT)) for path in (archive, patch, implementation)]
    input_before = file_map(ROOT, input_names)
    save(output / "inputs-before.json", input_before)
    if sha(archive) != pins["source_archive_sha256"] or sha(patch) != pins["patch_sha256"]:
        raise SystemExit("Pinned archive or patch SHA-256 mismatch; no gate was run.")
    with tarfile.open(archive, "r:gz") as handle:
        source_paths = sorted(member.name for member in handle.getmembers() if member.isfile())
        handle.extractall(sdk, filter="data")
    upstream_identity = source_identity(sdk)
    if upstream_identity != pins["upstream_source_identity_sha256"]:
        raise SystemExit("Extracted upstream source identity mismatch; no patch was applied.")

    # Pass only explicitly selected host variables. No ambient credential variables are inherited.
    allowed = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "SHELL", "SYSTEMROOT")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(
        PATH=str(Path(uv).parent) + os.pathsep + "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        PYTHONPATH=str(sdk / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX="1",
        PYTEST_XDIST_AUTO_NUM_WORKERS="4",
        PYRIGHT_THREADS="2",
        UV_FROZEN="1",
        UV_OFFLINE="1",
        UV_DEFAULT_INDEX="https://pypi.org/simple",
        UV_PROJECT_ENVIRONMENT=str(sdk / ".venv"),
        UV_PYTHON=args.python,
    )
    save(output / "environment.json", {
        "platform": platform.platform(),
        "host_environment_allowlist": list(allowed),
        "credentials_inherited": False,
        "settings": {key: value for key, value in env.items() if key not in allowed},
        "fresh_source_root": str(sdk),
        "uv": subprocess.check_output([uv, "--version"], text=True, env=env).strip(),
        "verification_runner_sha256": sha(Path(__file__)),
    })
    for name, command in (
        ("patch-check", ["git", "apply", "--check", str(patch)]),
        ("patch-apply", ["git", "apply", str(patch)]),
    ):
        if captured(command, sdk, env, output, name)["exit_code"]:
            raise SystemExit(f"{name} failed; see its immutable log.")
    patched_identity = source_identity(sdk)
    if patched_identity != pins["patched_source_identity_sha256"]:
        raise SystemExit("Patched source identity mismatch; no gate was run.")
    added_paths = re.findall(r"^\+\+\+ b/(.+)$", patch.read_text(), flags=re.MULTILINE)
    source_paths = sorted(set(source_paths + added_paths))
    source_before = file_map(sdk, source_paths)
    save(output / "source-files-before.json", source_before)
    save(output / "provenance.json", {
        "upstream_commit": pins["upstream_commit"],
        "archive_sha256": sha(archive),
        "patch_sha256": sha(patch),
        "upstream_source_identity_sha256": upstream_identity,
        "patched_source_identity_sha256": patched_identity,
        "source_file_count": len(source_before),
        "fresh_source_root": str(sdk),
        "extraction": "Python tarfile extraction with data filter",
        "patch_applied_unchanged": True,
        "live_provider_integration_requested": False,
    })
    setup = captured(
        [uv, "sync", "--frozen", "--offline", "--all-extras", "--all-packages", "--group", "dev"],
        sdk, env, output, "environment-setup",
    )
    gate = None
    log = ""
    if setup["exit_code"] == 0:
        inventory_code = (
            "import importlib.metadata as m,json,platform,sys; "
            "print(json.dumps({'python':sys.version,'executable':sys.executable,"
            "'packages':sorted([{'name':d.metadata['Name'],'version':d.version} "
            "for d in m.distributions()],key=lambda d:d['name'].lower())},indent=2))"
        )
        inventory = subprocess.check_output(
            [str(sdk / ".venv/bin/python"), "-c", inventory_code], cwd=sdk, env=env, text=True
        )
        save(output / "installed-environment.json", json.loads(inventory))
        capacity = subprocess.check_output(["ps", "-axo", "pid,ppid,comm"], text=True)
        observed = [line for line in capacity.splitlines() if re.search(r"pytest|mypy|pyright", line)]
        save(output / "capacity-check.json", {
            "checked_utc": datetime.now(timezone.utc).isoformat(),
            "matching_processes": observed,
        })
        if observed:
            raise SystemExit("Another broad test or type process is active; gate not started.")
        gate = captured(
            ["bash", ".agents/skills/code-change-verification/scripts/run.sh"],
            sdk, env, output, "repository-verification",
        )
        log = (output / "repository-verification.log").read_text()
    source_after = file_map(sdk, source_paths)
    input_after = file_map(ROOT, input_names)
    save(output / "source-files-after.json", source_after)
    save(output / "inputs-after.json", input_after)
    counts = parse_tests(log)
    checks = {
        name: f"make {name} passed in " in log for name in ("format", "lint", "typecheck", "tests")
    }
    receipt = {
        "schema": "fresh-patched-sdk-verification/v1",
        "status": "PASS" if (
            gate is not None and gate["exit_code"] == 0 and all(checks.values())
            and counts.get("parsed") and source_before == source_after and input_before == input_after
        ) else "FAIL",
        "checks": checks,
        "tests": counts,
        "mypy_checked_files": int(re.search(r"Success: no issues found in (\d+) source files", log).group(1))
        if re.search(r"Success: no issues found in (\d+) source files", log) else None,
        "pyright_zero_diagnostics": "0 errors, 0 warnings, 0 informations" in log,
        "input_files_unchanged": input_before == input_after,
        "source_files_unchanged": source_before == source_after,
        "changed_source_paths": [key for key in source_before if source_before[key] != source_after[key]],
        "source_identity_after": source_identity(sdk),
        "setup": setup,
        "gate": gate,
        "coverage_limits": [
            "Repository-designated native macOS nested-sandbox tests use the required Codex skip flag.",
            "Live provider integration and other platform or container matrices were not invoked.",
            "The SDK suite includes mocked transports and local subprocess/network fixtures.",
        ],
    }
    save(output / "verification.json", receipt)
    print(json.dumps({key: receipt[key] for key in ("status", "tests", "source_files_unchanged", "input_files_unchanged")}))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
