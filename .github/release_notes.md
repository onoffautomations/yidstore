After installing: restart Home Assistant, then refresh YidStore once (Ctrl+Shift+R on a computer). From then on it updates by itself.

### Install and update
- **New integrations load without a restart**, and their setup opens right in YidStore. Steps YidStore can't show itself open in Home Assistant's own setup dialog, on the same page.
- **Updating an integration asks for a restart**, with Home Assistant's own restart dialog. Also after "Install selected" and Auto-Install.
- **Configure** opens Home Assistant's own settings dialog for the integration.
- **The store updates right away** after you install or remove something, without reloading the page.

### Removing
- **Removing an integration uninstalls it from Home Assistant** (its setup, devices and entities), then deletes its files. Installing it again later needs no restart.
- **Removing a card removes its dashboard resource.**

### Security
- **Admin only.** YidStore's page, its store data and all its actions (install, check for updates, install app) need a logged-in Home Assistant admin. There is no public page anymore.
- **Safer installs.** Package and folder names are checked, files go to a temporary folder and are swapped in only when complete, and YidStore never deletes or overwrites anything outside its own folders (or Home Assistant, HACS or YidStore itself). Oversized and zip-bomb downloads are refused.
- **Store content is shown safely.** Names, descriptions, readmes and release notes can no longer run code in your browser.
- **No outside sites.** Fonts, icons and the markdown reader ship with YidStore.
- **YidStore Connector 1.3.1** never replaces an app folder it did not install.
- Removed unused endpoints that could write automations, dashboards and helpers.

### Page
- **Always current.** The page is never served from an old browser copy, and an open page reloads itself after YidStore is updated.
- **Loads about 5 times faster.**
- **Closing a popup keeps your place** instead of jumping to the top.
- Cards fit their buttons, keyboard access (Tab, Enter), zoom on phones, clearer errors, version in the header.

### Home Assistant
- **Cards register the official way**, through Home Assistant's dashboard resources.
- **Brand icons the built-in way** on newer Home Assistant.
- Much less log noise, and no blocking file reads during installs.
