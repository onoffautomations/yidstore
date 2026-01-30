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
- Add custom repositories via the **Custom Repos** modal (top-right menu).
- Use the list view to install without opening details.

## Branding (Icons)
If a repo includes icons, YidStore will install them automatically so Home Assistant can show local branding.

Supported paths:
- `icons/icon.png` (preferred)
- `custom_components/<domain>/icons/icon.png`
- `custom_components/<domain>/icon.png`

Optional:
- `icon@2x.png`
- `logo.png`
- SVG equivalents (`icon.svg`, `logo.svg`)

## Notes
- If you want private repos or more access, add your token in the integration’s **Settings → Configure** flow.
- After installing integrations, restart Home Assistant to load them.

---

Repo: `https://github.com/onoffautomations/yidstore`
