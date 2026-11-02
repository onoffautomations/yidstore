**Beta** — please test before 1.8.0 is released. Only offered to users who turn on beta versions for YidStore in the Marketplace (HACS).

### New in beta 4: installs work like the Marketplace
- **Install dialog**: shows what gets installed and which version; GitHub (community) items show a "you install this at your own risk" note. A progress bar runs while it installs.
- **Setup right in the same dialog**: after a fresh integration installs, YidStore starts its configuration wizard in place, with the integration's own form, labels, errors and "Success" screen. Anything the dialog can't show offers "Continue in Home Assistant".
- **Restart the native way**: when an update needs a restart, YidStore offers **Restart**, which opens Home Assistant's own restart dialog. No more "go to Repairs".
- **Details page like the Marketplace**: a status card ("Not installed / Version X installed / Set it up to start using it / installed and set up") with Install, Update, Set up, Restart or Open integration, plus downloads, stars and last change.

### New in beta 3
- After installing an integration that's ready to use (no restart needed), YidStore opens its setup straight away: the same dialog as Settings → Devices & Services → Add integration.
- Installed integrations that aren't set up yet get a **Set up** button on their card, list row and details, so you can start the setup from YidStore at any time.

### Fixed in beta 2
- On phones, a tall app dialog no longer gets stuck off-screen. Popups now fit the screen and scroll inside, the buttons stay visible, and there's a close (X) button. Tapping outside a popup closes it.

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
