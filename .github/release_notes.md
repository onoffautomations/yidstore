Beta. Turn on beta versions for YidStore in the Marketplace (or HACS) to get it. After installing: restart Home Assistant, then refresh YidStore once (Ctrl+Shift+R on a computer).

### New
- **Update All** in the menu (the three dots). It shows how many items have an update, lists them with their versions, and updates them one by one. If an integration needs it, it offers Home Assistant's restart at the end. Items installed by HACS / the Marketplace are left to it.

### Changed
- **No more Marketplace "re-add YidStore" popup or notification.** The popup is gone from the store, and the Home Assistant notification is cleared on startup.
- **Multi-select install bar stays in place.** When you select several items, the Install / Cancel bar floats at the bottom of the screen and no longer scrolls away with the page.
- **Fits screens better.**
  - Phones: the header is now three tidy rows: YidStore, section, select and menu on top; a full-width search bar; then the filters.
  - Wide screens use more of the width, and the search bar grows on desktop.
  - Very small phones get tighter spacing.
- **Audio section hidden.** The Audio tab, the Audio filter, the "Audio Download" menu item and audio packs in the store are hidden for now. The audio code is still there and can be turned back on.
