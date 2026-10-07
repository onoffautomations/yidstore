# YidStore (OnOff Integration Store)

YidStore is a Home Assistant integration that adds a full in-app “store” for installing and managing custom integrations, Lovelace cards, and blueprints. It connects to a curated list of repositories and lets you add your own custom repos, install/update packages, and manage them from a single dashboard.

## What it does
- In-Home Assistant store UI (sidebar panel)
- Install integrations, Lovelace cards, and blueprints directly from repos
- Custom repository support (add/remove your own repos)
- Update tracking and reinstall flow
- Local branding support for custom integrations (icon/logo files in the repo)

## Installation

The link below works with both HACS and the Marketplace built into Home Assistant 2026.11+. Existing HACS addresses keep working there.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=onoffautomations&repository=yidstore&category=Integration)

### Home Assistant 2026.11 and newer (built-in Marketplace)
1) Go to **Settings → Marketplace**.
2) Open the menu (⋮) and choose **Custom repositories**. Custom repositories need a connected GitHub account.
3) Add `https://github.com/onoffautomations/yidstore` with type **Integration**.
4) Install **YidStore** and restart Home Assistant.

If you already installed YidStore through HACS, nothing changes. Home Assistant moves it into the Marketplace on its own.

### Older versions (HACS)
1) In Home Assistant, open HACS.
2) Click the three dots in the top-right and choose **Custom repositories**.
3) Add this repository URL:
   - `https://github.com/onoffautomations/yidstore`
4) Select category **Integration** and click **Add**.
5) Find **YidStore** in HACS and install it.
6) Restart Home Assistant.

## Setup
1) Go to **Settings → Devices & Services → Add Integration**.
2) Search for **YidStore** and add it.
3) (Optional) Keep the sidebar panel enabled.

## Using the Store
- Open **YidStore** in the left sidebar.
- Install packages directly from the list.

## Apps
The **Apps** tab installs add-ons on Home Assistant OS or Supervised installs.

- The first time, click **Install add-on**. YidStore installs and starts its helper add-on, the YidStore Connector, which ships inside this integration. There is no extra repository to add.
- **Install** downloads the app and has Home Assistant build and install it.
- Each app shows its installed and latest version. You can update, start, stop and open it, and set start on boot and watchdog.
- Some apps are only available to installations with a store account.
