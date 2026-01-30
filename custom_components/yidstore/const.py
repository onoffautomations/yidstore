DOMAIN = "yidstore"

SERVICE_INSTALL = "install"
SERVICE_INSTALL_INTEGRATION = "install_integration"
SERVICE_INSTALL_LOVELACE = "install_lovelace"
SERVICE_INSTALL_BLUEPRINTS = "install_blueprints"
SERVICE_CHECK_UPDATES = "check_updates"

MODE_ASSET = "asset"
MODE_ZIPBALL = "zipball"

TYPE_INTEGRATION = "integration"
TYPE_LOVELACE = "lovelace"
TYPE_BLUEPRINTS = "blueprints"

# /config/www/community/onoff/<repo>  ->  /local/community/onoff/<repo>
LOVELACE_VENDOR_FOLDER = "onoff"

# Update check interval (2 hours)
UPDATE_CHECK_INTERVAL = 7200  # seconds

# Storage keys
STORAGE_VERSION = 1
STORAGE_KEY_PACKAGES = "yidstore_packages"

# Sensor attributes
ATTR_REPO_NAME = "repo_name"
ATTR_OWNER = "owner"
ATTR_PACKAGE_TYPE = "package_type"
ATTR_INSTALLED_VERSION = "installed_version"
ATTR_LATEST_VERSION = "latest_version"
ATTR_UPDATE_AVAILABLE = "update_available"
ATTR_INSTALL_DATE = "install_date"
ATTR_LAST_CHECK = "last_check"
ATTR_RELEASE_SUMMARY = "release_summary"
ATTR_RELEASE_NOTES = "release_notes"

CONF_SIDE_PANEL = "side_panel"
