#!/usr/bin/env python
"""One clean verification pass for the current paper_trade.py surfaces.

These are the surfaces I actually changed since the last state sync:

* --list-skills is a readonly disk probe of the synced account-skills tree.
* --skill-read is a readonly file reader for repo paths.
* --health is the scheduler probe that still drives the exit code.
* --status is the human mirror of the same health state.
* --health-force-mode is a placeholder refusal path for now.

This script does not try to be clever about combined output shape. It checks
each behavior through the subprocess boundary, because that is the contract.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SAMPLE_CSV = Path("data/sample/BTCUSDT_1d.csv")
PYTHON = [sys.executable, "paper_trade.py"]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        PYTHON + list(args),
        capture_output=True,
        text=True,
    )


def health_from_stdout(stdout: str) -> dict:
    body = stdout.strip()
    first = body.find("{")
    if first == -1:
        raise AssertionError(f"no JSON object on stdout: {stdout[:200]!r}")
    return json.loads(body[first:])


def assert_contains(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} missing; stdout:\n{text}")


def _fresh_state() -> None:
    RESULTS_DIR = Path(__file__).resolve().parent.parent / "results" / "paper"
    for path in (RESULTS_DIR / "last_run.json", RESULTS_DIR / "last_failure.json"):
        if path.exists():
            path.unlink()


def main() -> int:
    SKILL_DIR = Path.home() / ".config" / "freebuff-desktop" / "account-skills"

    def _find_skill_dirs() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not SKILL_DIR.is_dir():
            return out
        try:
            for account_dir in sorted(SKILL_DIR.iterdir()):
                if not account_dir.is_dir():
                    continue
                try:
                    for skill_dir in sorted(account_dir.iterdir()):
                        if not skill_dir.is_dir():
                            continue
                        leaves = sorted(p.name for p in skill_dir.iterdir())
                        out.append(
                            {
                                "account_key": account_dir.name,
                                "skill_dir_name": skill_dir.name,
                                "skill_dir": str(skill_dir),
                                "has_skill_md": (skill_dir / "SKILL.md").is_file(),
                                "leaves": leaves,
                            }
                        )
                except OSError:
                    continue
        except OSError:
            return out
        return out

    # --- skill probe: list ---
    skills_out = run("--list-skills")
    assert skills_out.returncode == 0, skills_out.returncode
    assert_contains(skills_out.stdout, "account skills on disk", "list-skills header")
    leaves_nested = []
    for r in _find_skill_dirs():
        leaves_nested.append(r["skill_dir_name"])
        for lf in sorted(Path(r["skill_dir"]).glob("*")):
            if lf.is_dir():
                leaves_nested.append(lf.name)
    assert "frontend-design" in leaves_nested, leaves_nested
    assert "web-design-guidelines" in leaves_nested, leaves_nested
    assert "webapp-testing" in leaves_nested, leaves_nested
    assert_contains(skills_out.stdout, "SKILL.md: no", "list-skills SKILL.md absence")
    print("list-skills ok")

    # --- skill probe: read existing file ---
    r_skill_read = run("--skill-read", "quant/config.py")
    assert r_skill_read.returncode == 0, r_skill_read.returncode
    assert_contains(r_skill_read.stdout, "fee_bps", "skill-read config.py")
    print("skill-read existing ok")


    # --- skill probe: missing file ---
    r_skill_missing = run("--skill-read", "quant/not_real_now")
    assert r_skill_missing.returncode == 2, r_skill_missing.returncode
    assert r_skill_missing.stdout.strip() == "", r_skill_missing.stdout
    assert "not found" in r_skill_missing.stderr.lower() or "cannot read" in r_skill_missing.stderr.lower(), r_skill_missing.stderr
    print("skill-read missing ok")

    # --- scheduler probe: cold health ---
    _fresh_state()
    r_cold_health = run("--health")
    assert r_cold_health.returncode == 1, r_cold_health.returncode
    Health = health_from_stdout(r_cold_health.stdout)
    assert Health["healthy"] is False
    assert Health["reason"] == "no successful run yet"
    assert Health["last_run"] is None
    assert Health["last_failure"] is None, Health
    print("cold health ok")

    # --- scheduler probe: cold health stays honest even with a leftover stale failure ---
    r_cold_health_after_stale = run("--health")
    assert r_cold_health_after_stale.returncode == 1, r_cold_health_after_stale.returncode
    Health2 = health_from_stdout(r_cold_health_after_stale.stdout)
    assert Health2["healthy"] is False, Health2
    assert Health2["reason"].startswith("no successful run"), Health2
    print("cold health after stale failure ok")

    # --- scheduler probe: --status serves the human mirror of the cold state ---
    status = run("--status").stdout
    assert_contains(status, "no successful run", "status reason")
    print("status mirrors cold state ok")

    # --- scheduler probe: a real run writes hard-failure state when it is stale ---
    if not SAMPLE_CSV.exists():
        raise SystemExit("expected a sample CSV for the stale-run case")
    r = run("--no-save", "--csv", str(SAMPLE_CSV))
    assert r.returncode == 5, r.returncode
    Health = health_from_stdout(run("--health").stdout)
    assert Health["last_run"] is None, Health
    lf = Health["last_failure"]
    assert lf["kind"] == "stale_data", lf
    cfg = lf["config_hash"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    assert isinstance(cfg, dict) and cfg["symbol"] == "BTC/USDT", cfg
    print("stale run hard-failure state ok")

    # --- placeholder refusal path ---
    r = run("--health-force-mode", "yes-please")
    assert r.returncode == 3, r.returncode
    assert "health-force-mode is not wired" in r.stderr.lower(), r.stderr
    print("health-force-mode refusal ok")

    # --- final state check: after the whole script, health is still cold ---
    HealthFinal = health_from_stdout(run("--health").stdout)
    assert HealthFinal["healthy"] is False, HealthFinal
    assert HealthFinal["reason"].startswith("no successful run"), HealthFinal

    print("final verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
