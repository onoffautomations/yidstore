# YidStore (OnOff Integration Store)

YidStore is a Home Assistant integration that adds a full in-app “store” for installing and managing custom integrations, Lovelace cards, and blueprints. It connects to a curated list of repositories and lets you add your own custom repos, install/update packages, and manage them from a single dashboard.

## What it does
- In-Home Assistant store UI (sidebar panel)
- Install integrations, Lovelace cards, and blueprints directly from repos
- Custom repository support (add/remove your own repos)
- Update tracking and reinstall flow
- Local branding support for custom integrations (icon/logo files in the repo)

## Installation (HACS Custom Repository)
1) In Home Assistant, open HACS.
2) Go to **Integrations**.
3) Click the three dots in the top-right and choose **Custom repositories**.
4) Add this repository URL:
   - `https://github.com/onoffautomations/yidstore`
5) Select category **Integration** and click **Add**.
6) Find **YidStore** in HACS and install it.
7) Restart Home Assistant.

## Setup
1) Go to **Settings → Devices & Services → Add Integration**.
2) Search for **YidStore** and add it.
3) (Optional) Keep the sidebar panel enabled.

## Using the Store
- Open **YidStore** in the left sidebar.
- Install packages directly from the list.
