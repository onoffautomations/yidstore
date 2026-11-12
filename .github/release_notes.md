**Beta** — please test before 1.8.2 is released. Only offered to users who turn on beta versions for YidStore.

After installing: restart Home Assistant, then refresh YidStore once (Ctrl+Shift+R on a computer). From then on it updates by itself.

- **Updates always show.** Browsers used to keep an old copy of YidStore's page for up to a month, so fixes didn't appear and the panel could show stale "update available" or "restart required". Now the page is always current, and an open page reloads itself after YidStore is updated.
- **No public page anymore.** Opening YidStore's page address directly now goes to Home Assistant's login and the YidStore panel. The store's data (list, readmes, releases, status) needs a login too.
- **Safer.** Installing, removing, refreshing and other changes need a logged-in Home Assistant admin.
- **Configure opens Home Assistant's own settings dialog**, right on the page, like Restart opens Home Assistant's own restart dialog.
- **Cards register the official way**, through Home Assistant's dashboard resources. Old duplicate entries are cleaned up.
- **Brand icons the built-in way.** Newer Home Assistant shows each integration's own icons itself, so YidStore no longer loads its icon script on your dashboards there, and removes it where it was added before. Older Home Assistant keeps it.
- **Loads faster.** About 5 times less to download, and nothing from other sites holds up the first screen.
- **Version in the header**, for example "by: OnOff Automations · v1.8.2b4".
