# Changelog

## 1.2.0

- App updates show up in Home Assistant (add-on page and Settings → Updates): new versions are put in place so Home Assistant can update them itself.

## 1.1.0

- Now ships inside the YidStore integration. YidStore installs and updates it for you.

## 1.0.2

- Show the real reason when an app download fails.
- Report the app's own slug so YidStore can confirm it appeared.

## 1.0.1

- Fix the build on newer Home Assistant versions (no base image was passed to the build).

## 1.0.0

- First release: installs and removes YidStore apps in the local add-ons folder.
- Uses the YidStore integration's settings. Nothing to configure in the add-on.
