import tomllib
from pathlib import Path


BACKEND_ROOT = Path(__file__).parents[1]


def test_rl_railway_config_selects_rl_dockerfile():
    config = tomllib.loads((BACKEND_ROOT / "railway.rl.toml").read_text(encoding="utf-8"))

    assert config["build"]["builder"] == "DOCKERFILE"
    assert config["build"]["dockerfilePath"] == "Dockerfile.rl"


def test_rl_image_installs_rl_requirements():
    dockerfile = (BACKEND_ROOT / "Dockerfile.rl").read_text(encoding="utf-8")
    requirements = (BACKEND_ROOT / "requirements-rl.txt").read_text(encoding="utf-8")

    assert "requirements-rl.txt" in dockerfile
    assert "stable-baselines3" in requirements
    assert "torch" in requirements


def test_standard_image_has_rl_runtime_fallback_for_railway_worker():
    requirements = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "stable-baselines3==2.9.0" in requirements
    assert "torch==2.8.0+cpu" in requirements


def test_entrypoint_supports_every_documented_process():
    entrypoint = (BACKEND_ROOT / "entrypoint.sh").read_text(encoding="utf-8")

    for process in ("web", "telegram", "trader", "candles", "optimizer", "rl"):
        assert process in entrypoint
    assert "Unknown APP_PROCESS" in entrypoint
