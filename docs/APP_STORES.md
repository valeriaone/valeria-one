# Valeria One in the app stores

Valeria One is already an installable app (v0.18): open it in Chrome, Edge or Samsung Internet and tap **Install**;
on an iPhone, Safari › Share › **Add to Home Screen**. It has a web app manifest (`/manifest.webmanifest`), icons
(`static/icons/`), a service worker (`/sw.js`: an offline page and Valeria One's own files — never a signed-in page,
an API answer or anyone's data) and notifications by the standard Web Push protocol (this server's own VAPID keys;
no Firebase).

The store versions wrap that same app. **The store accounts, the signing keys, the store listings and the reviews
are yours (Tad's): this repository can't create or submit them.** What is ready in code, and the exact steps:

## Google Play (Android) — a Trusted Web Activity

Cost: a Google Play developer account, **US$25 once** (play.google.com/console). Review usually takes a few days;
new personal accounts must first run a closed test with at least 12 testers for 14 days.

What the repository has:

* `deploy/android/twa-manifest.json` — the Bubblewrap project (package `mx.valai.one`, host `one.valai.mx`; change
  both to your own before the first build: the package name can never change after the first upload).
* `/.well-known/assetlinks.json` — served from two settings in the server's `.env`:
  * `VO_ANDROID_PACKAGE` — the package name, e.g. `mx.valai.one`
  * `VO_ANDROID_SHA256` — the app's signing certificate SHA-256 fingerprint(s), `AB:CD:…` (comma between several:
    your upload key and Google Play's app-signing key)

  Not set = 404 (the site works as always). They are optional.

Steps (on your own computer, not on the server — the Android tools need ~3 GB and Java):

1. Install Node.js 18+ and Bubblewrap: `npm i -g @bubblewrap/cli`.
2. `mkdir valeria-one-android && cd valeria-one-android`
3. Copy `deploy/android/twa-manifest.json` into it (edit `packageId`, `host`, the icon URLs if your address differs).
4. `bubblewrap update` (it offers to download the JDK and Android SDK the first time — say yes), then
   `bubblewrap build`. It makes `android.keystore` (**keep it and its passwords safe forever: losing it means you can
   never update the app**), `app-release-signed.apk` and `app-release-bundle.aab`.
5. `keytool -list -v -keystore android.keystore` → copy the **SHA-256** line.
6. On the server: add `VO_ANDROID_PACKAGE=mx.valai.one` and `VO_ANDROID_SHA256=<that fingerprint>` to `.env`, restart.
   Check `https://<your host>/.well-known/assetlinks.json` shows them.
7. Play Console › Create app › upload `app-release-bundle.aab` to **Internal testing** first. In Play Console ›
   Setup › App signing, copy Google's **App signing key** SHA-256 and add it to `VO_ANDROID_SHA256` (comma
   separated) — without it the installed app shows a browser bar instead of full screen.
8. Fill in the store listing (name, short and full description, the 512×512 icon `static/icons/icon-512.png`,
   a 1024×500 feature graphic, at least 2 phone screenshots — `docs/screenshots/` has some), the content rating
   questionnaire, the data-safety form (the app is the website; data is the business's own, kept on its server),
   a privacy policy URL (`/privacy` on your server) and the target audience (adults, business).
9. Promote from internal → closed test → production when Google approves.

Updating the app is rarely needed: it shows the live website, so every server update reaches it at once. Rebuild
only to change the name, icon or package settings (bump `appVersionCode`).

## Apple App Store (iPhone, iPad)

Cost: the Apple Developer Program, **US$99 per year** (developer.apple.com). Needs a Mac with Xcode. Review takes
1–3 days; Apple rejects apps that are "just a website" (guideline 4.2), so the wrapper must use native features —
push notifications, the share sheet, offline — and the listing should explain the business tools.

The route: **Capacitor** (capacitorjs.com) with the server URL, which gives a native WKWebView shell:

1. On a Mac: `npm i @capacitor/core @capacitor/cli @capacitor/ios @capacitor/push-notifications`
2. `npx cap init "Valeria One" mx.valai.one --web-dir www` (a `www/index.html` that just redirects is enough).
3. In `capacitor.config.json` set `"server": {"url": "https://one.valai.mx/app", "cleartext": false}` and
   `"ios": {"contentInset": "always"}`.
4. `npx cap add ios && npx cap open ios` → in Xcode: the team (your Apple account), the bundle id, the icons
   (`static/icons/icon-512.png` → Xcode's 1024×1024 App Icon; scale up from the SVG with the same ffmpeg command
   used here), the "Push Notifications" capability.
5. Web Push also works in an installed Home-Screen web app on iOS 16.4+, but inside a Capacitor app native push
   (APNs) is the reliable path: it needs an APNs key from your Apple account; wiring it to Valeria One is a small
   follow-up (a device-token endpoint next to `/app/push/subscribe`). Until then the iOS app works without push.
6. Product › Archive → Distribute → App Store Connect; fill in the listing, privacy "nutrition labels", screenshots
   (6.7" and 5.5" iPhone), the review notes (a demo login: use a sandbox business), and submit.

## What is not verified here

No store build was made on this server (no Android SDK or Xcode on purpose: the disk is small and they belong on a
developer machine). The Bubblewrap manifest follows Bubblewrap's documented format; the first real `bubblewrap
build`, the Play review and the App Store review are yours to run.
