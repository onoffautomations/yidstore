**Beta** — please test before 1.8.2 is released. Only offered to users who turn on beta versions for YidStore.

After installing: restart Home Assistant, then refresh YidStore once (Ctrl+Shift+R on a computer). From then on it updates by itself.

### New in 1.8.2b5 — security review fixes

- **Removing an integration uninstalls it.** Deleting an integration in YidStore now also removes it from Home Assistant (its setup, devices and entities), then deletes its files and offers a restart.
- **Removing a card removes its dashboard resource**, so nothing is left behind under Settings → Dashboards → Resources.
- **Closing a popup keeps your place.** The page no longer jumps back to the top.
- **Safer installs and removals.** Package and folder names are checked, files are installed into a temporary folder and swapped in only when complete, and YidStore can never delete or overwrite anything outside its own folders (or Home Assistant, HACS or YidStore itself). Oversized or zip-bomb downloads are refused.
- **Admin only.** YidStore's actions (install, check for updates, install app) and all store data need a Home Assistant admin.
- **Store content is shown safely.** Names, descriptions, readmes and release notes from the store can no longer run code in your browser.
- **No outside sites.** Fonts, icons and the markdown reader now ship with YidStore.
- **YidStore Connector 1.3.1** never replaces an app folder it did not install.
- Removed unused endpoints that could write automations, dashboards and helpers.
- Keyboard access (Tab, Enter) for store cards, zoom allowed on phones, clearer readme errors, much less log noise.

### Earlier in 1.8.2 betas

- **Updates always show.** The page is always current, and an open page reloads itself after YidStore is updated.
- **No public page.** Opening YidStore's page address directly goes to Home Assistant's login and the YidStore panel.
- **Configure and Restart open Home Assistant's own dialogs.**
- **Cards register the official way**, through Home Assistant's dashboard resources.
- **Brand icons the built-in way** on newer Home Assistant.
- **Loads faster**, about 5 times less to download.
- **Version in the header**, for example "by: OnOff Automations · v1.8.2b5".
