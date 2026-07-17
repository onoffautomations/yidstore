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

## Update entities & release notes
YidStore exposes a Home Assistant **update entity** for every integration it
manages, and those entities now show full release notes below the available
update — the same experience as HACS. For each managed package the entity
provides the installed version, the latest version, a release title/summary,
the full release body, and a link to the release when Home Assistant can
display one.

- **GitHub packages** fetch the actual latest *stable* GitHub release
  (tag, name, body, HTML URL, published date). Draft releases are ignored, and
  prereleases are not treated as a stable update unless the package explicitly
  opts in. Release data is cached and only refreshed on the normal update-check
  interval, and API rate limits/repos-without-releases are handled without
  inventing a false update.
- **Gitea packages** fetch the latest release through the configured Gitea
  client and store the same shared fields (`release_summary`, `release_notes`,
  `release_url`, `release_published_at`), so the update entity reads one
  consistent shape regardless of source. GitHub packages never query the Gitea
  server, and Gitea packages never query GitHub.
- When a release has no notes, the entity returns a clear
  “No release notes were published for this version.” and does not falsely
  advertise the release-notes feature.

## Installation ownership (YidStore vs HACS vs manual)
YidStore now tracks *who installed* each integration instead of assuming it
owns anything found in `custom_components`:

- **Installed by YidStore** — tracked as YidStore-managed with durable metadata
  (`managed_by`, `installed_by_yidstore`, `installation_id`, `source`,
  `repo_url`). YidStore creates its device/entities, checks the correct
  repository for updates, and offers the update with release notes.
- **Installed by HACS** — shown as “Installed by HACS”. YidStore does **not**
  advertise its own update or create a competing update entity; HACS remains
  responsible for updating it. YidStore reads the HACS-installed version and
  keeps its displayed version in sync when HACS updates the integration.
- **Installed manually** — shown as “Manually installed”. YidStore does not
  claim ownership or offer an update; you must explicitly (re)install through
  YidStore to have it manage the update lifecycle.

Ownership is deterministic and durable: a matching folder on disk or a HACS
catalog entry is **not** proof that YidStore installed something. A package
YidStore installed stays YidStore-managed even if HACS later recognizes the
same domain. The installed version is reconciled from the on-disk
`manifest.json` (and HACS metadata for HACS-owned integrations) using proper
version ordering, so `v1.2.3`/`1.2.3` don't show a phantom update and branch
installs (`main`/`master`/`dev`) are never reported as release updates.

Existing installations are migrated automatically on upgrade: old package
records gain the new ownership fields based on available evidence, no tracked
data is lost, and ambiguous cases fall back to the safest non-actionable state.

## Development / tests
The automated tests live in `tests/` and use
`pytest-homeassistant-custom-component`:

```bash
pip install homeassistant pytest pytest-homeassistant-custom-component aioresponses tzdata
python -m pytest tests/
```
