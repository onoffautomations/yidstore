"""Dashboard ownership-label helpers."""
from custom_components.yidstore.dashboard import (
    _install_source_for_package,
    _package_is_yidstore_managed,
    _package_update_available,
)

from helpers import yidstore_package


def test_managed_helper():
    assert _package_is_yidstore_managed(yidstore_package()) is True
    assert _package_is_yidstore_managed(
        yidstore_package(managed_by="hacs", installed_by_yidstore=False)) is False
    assert _package_is_yidstore_managed(
        yidstore_package(managed_by="manual", installed_by_yidstore=False)) is False
    assert _package_is_yidstore_managed(None) is False
    # Legacy record without the field.
    assert _package_is_yidstore_managed({"installed_by_yidstore": True}) is True


def test_install_source_resolution():
    assert _install_source_for_package(yidstore_package(), "manual") == "yidstore"
    assert _install_source_for_package(
        yidstore_package(managed_by="hacs", installed_by_yidstore=False), "hacs") == "hacs"
    assert _install_source_for_package(
        yidstore_package(managed_by="manual", installed_by_yidstore=False), "manual") == "manual"
    # 14. No tracked package -> fall back to disk detection, never "yidstore".
    assert _install_source_for_package(None, "manual") == "manual"
    assert _install_source_for_package(None, "hacs") == "hacs"
    assert _install_source_for_package(None, None) is None


def test_update_available_gated_by_ownership():
    # YidStore-managed with a real update.
    assert _package_update_available(
        yidstore_package(update_available=True)) is True
    # HACS/manual never expose an actionable YidStore update, even if the flag
    # is somehow set.
    assert _package_update_available(
        yidstore_package(managed_by="hacs", installed_by_yidstore=False,
                         update_available=True)) is False
    assert _package_update_available(
        yidstore_package(managed_by="manual", installed_by_yidstore=False,
                         update_available=True)) is False
    assert _package_update_available(None) is False
