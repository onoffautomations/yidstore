"""Test bootstrap for the YidStore integration.

Puts the repository root on the import path so ``custom_components.yidstore``
resolves to the real integration package.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_config_dir(request, tmp_path):
    """Give each hass-using test a private config dir.

    pytest-homeassistant-custom-component shares one on-disk config dir across
    tests; without isolation, manifests/HACS storage one test writes leak into
    the next. We repoint the config dir at a fresh tmp_path per test.
    """
    if "hass" not in request.fixturenames:
        return
    hass = request.getfixturevalue("hass")
    hass.config.config_dir = str(tmp_path)
    (tmp_path / ".storage").mkdir(parents=True, exist_ok=True)
