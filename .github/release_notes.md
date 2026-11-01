**Beta** — please test before 1.8.0 is released. Only offered to users who turn on beta versions for YidStore in the Marketplace (HACS).

### Stars and downloads
- Every integration, card and app now shows its star count, and its download count when there is one. YidStore reads them from a public stats file that this repository refreshes every 6 hours, so no token is needed.

### Integrations: install without a restart
- New config-flow integrations load right away, like Home Assistant's Marketplace. No restart and no "Restart required" repair.
- Updating an integration that is already running (or a YAML-only one) still needs a restart. The fixable **Restart required** repair restarts Home Assistant for you.

### Apps (add-ons)
- The Apps tab loads much faster (cached store reads, pre-loaded at startup).
- Install older versions: the app dialog has a version picker. Switching to an older version rebuilds the app with it.
- Release notes for the selected version are shown in the app dialog.

### Home Assistant 2026.11.1+
- A notification and a one-time popup explain how to re-add YidStore as a custom repository in the Marketplace if it goes missing after the update.
- Installs from the community store are labelled **Marketplace** on 2026.11.1+ and **HACS** on older versions.
