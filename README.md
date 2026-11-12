# YidStore (OnOff Integration Store)

YidStore is a Home Assistant integration that adds a full in-app “store” for installing and managing custom integrations, Lovelace cards, and blueprints. It connects to a curated list of repositories and lets you add your own custom repos, install/update packages, and manage them from a single dashboard.

## What it does
- In-Home Assistant store UI (sidebar panel)
- Install integrations, Lovelace cards, and blueprints directly from repos
- Custom repository support (add/remove your own repos)
- Update tracking and reinstall flow
- Local branding support for custom integrations (icon/logo files in the repo)

## Installation

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=onoffautomations&repository=yidstore&category=Integration)

The button works with both the Marketplace built into Home Assistant 2026.11+ and HACS.

### Home Assistant 2026.11 and newer (Marketplace)
1) Open **Marketplace** and tap the **add link** button next to the search bar.
2) Paste `https://github.com/onoffautomations/yidstore` and tap **Add**.
3) Install **YidStore**, then restart Home Assistant.

If YidStore is missing from the Marketplace after updating to Home Assistant 2026.11.1, add it back with the same three steps.

### Older versions (HACS)
1) Open **HACS**, click the three dots in the top-right and choose **Custom repositories**.
2) Add `https://github.com/onoffautomations/yidstore` with category **Integration**.
3) Install **YidStore**, then restart Home Assistant.

### Beta versions
To try a beta, open YidStore in the Marketplace (or HACS) and turn on beta versions for it.

## Setup
1) Go to **Settings → Devices & Services → Add Integration**.
2) Search for **YidStore** and add it.
3) (Optional) Keep the sidebar panel enabled.

## Using the Store
- Open **YidStore** in the left sidebar.
- Each item shows its stars and downloads.
- **Install** works like the Marketplace: a new integration loads without a restart and its setup opens right in YidStore. Steps YidStore can't show itself open in Home Assistant's own setup dialog, on the same page.
- **Updating** an integration needs a restart; **Restart** opens Home Assistant's own restart dialog.
- An installed integration that isn't set up yet has a **Set up** button, and **Configure** opens its settings.
- **Removing** an integration also removes it from Home Assistant (its setup, devices and entities). Removing a card also removes its dashboard resource.

## Security
- YidStore's page, its store data and all its actions need a logged-in Home Assistant **admin**.
- Packages only install into their own folders; YidStore never overwrites or deletes anything else, and never Home Assistant, HACS or itself.
- Text from the store (names, readmes, release notes) is shown safely, and the page loads nothing from other sites.

## Apps
The **Apps** tab installs add-ons on Home Assistant OS or Supervised installs.

- The first time, click **Install add-on**. YidStore installs and starts its helper add-on, the YidStore Connector, which ships inside this integration. There is no extra repository to add.
- **Install** downloads the app and has Home Assistant build and install it.
- Each app shows its installed and latest version. You can update, start, stop and open it, and set start on boot and watchdog.
- Some apps are only available to installations with a store account.
