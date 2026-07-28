# TOM Driver — native iOS + Android (Capacitor)

This folder packages the **exact same** web app in `../frontend` into real
native apps you can ship to the **App Store** and **Google Play**. Nothing in
the web app is rewritten — Capacitor loads the PWA in a native WebView and adds
OS capabilities (background GPS, push, hardware barcode, Face/Touch ID) through
`../frontend/native-bridge.js`, which stays a harmless no-op in a plain browser.

```
native/
├── capacitor.config.json   app id, name, webDir → ../frontend, plugin config
├── package.json            Capacitor + plugin dependencies
│   (API base lives in ../frontend/config.js — protocol-guarded so it only
│    activates inside the packaged app, and ships with the copied assets)
├── .gitignore              ios/ android/ node_modules are generated, not committed
└── (generated) ios/  android/  node_modules/
```

## Prerequisites

| Platform | Needs |
|----------|-------|
| iOS      | macOS + **Xcode 15+**, CocoaPods (`sudo gem install cocoapods`), an Apple Developer account |
| Android  | **Android Studio** (Giraffe+) + JDK 17 |
| Both     | **Node 18+** (you have Node 20) |

## One-time setup

> **Status (10 Jul 2026):** `npm install`, `cap add android` and `cap add ios`
> have been run — `android/` and `ios/` exist with the permission strings from
> §"Required OS permission strings" **already injected** into
> `AndroidManifest.xml` and `Info.plist`, and web assets copied. Remaining:
> install Xcode + CocoaPods then `cd ios/App && pod install` (iOS), and
> Android Studio + JDK 17 (Android), then build as below.

```bash
cd Driver/app/native
npm install
# Point the packaged app at the deployed TOM backend (NOT localhost):
#   edit ../frontend/config.js → NATIVE_API_BASE = 'https://<your-tom-host>'
npx cap add ios      # done — regenerating overwrites the injected permissions
npx cap add android  # done — regenerating overwrites the injected permissions
npx cap sync         # copies ../frontend + installs native plugin code
```

## Run on a device / simulator

```bash
npx cap run ios        # or: npx cap open ios     → run from Xcode
npx cap run android    # or: npx cap open android → run from Android Studio
```

### iOS Simulator from the command line (no Xcode GUI)

Proven working 10 Jul 2026 (Xcode 26.6, iOS 26.5 simulator, Apple Silicon):

```bash
cd ios/App
SKIP_MLKIT=1 pod install     # ML Kit 4.x has no arm64-simulator slice — see Podfile note
DEVELOPER_DIR=/Applications/Xcode.app xcodebuild -workspace App.xcworkspace \
  -scheme App -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath build ARCHS=arm64 ONLY_ACTIVE_ARCH=NO CODE_SIGN_IDENTITY=- \
  LM_SKIP_METADATA_EXTRACTION=YES build
xcrun simctl install <device-udid> build/Build/Products/Debug-iphonesimulator/App.app
xcrun simctl launch <device-udid> uk.co.xtramile.tomdriver
pod install                  # restore ML Kit before any device/store build
```

Notes: `LM_SKIP_METADATA_EXTRACTION=YES` works around an Xcode 26 CLI bug in
App Intents extraction; `CODE_SIGN_IDENTITY=-` ad-hoc signs (Apple-Silicon
simulators refuse unsigned arm64). If `xcodebuild` can't see simulator
destinations, either use `generic/platform=iOS Simulator` as above or point
xcode-select at the full Xcode once: `sudo xcode-select -s /Applications/Xcode.app`.
Physical-device and App Store builds are untouched by all of this — they keep
ML Kit and need your Apple Developer signing in Xcode.

## After any web change

The native apps are a snapshot of `../frontend`. Re-sync after editing the PWA:

```bash
npx cap copy          # fast: just re-copies web assets
# or
npx cap sync          # also re-installs/updates native plugins
```

## Required OS permission strings

These are **store-review blockers** if missing. Add them when you first build.

### iOS — `ios/App/App/Info.plist`

```xml
<key>NSLocationWhenInUseUsageDescription</key>
<string>TOM shares your location with dispatch while you are on shift so jobs are routed to you.</string>
<key>NSLocationAlwaysAndWhenInUseUsageDescription</key>
<string>TOM keeps sharing your location in the background while on shift so dispatch can route jobs even when the screen is off.</string>
<key>NSCameraUsageDescription</key>
<string>TOM uses the camera to scan parcel barcodes and capture proof-of-delivery photos.</string>
<key>NSFaceIDUsageDescription</key>
<string>TOM uses Face ID to unlock the app.</string>
<key>UIBackgroundModes</key>
<array><string>location</string><string>remote-notification</string></array>
```
Enable **Push Notifications** and **Background Modes → Location updates** under
*Signing & Capabilities*, and upload the **APNs key** to Firebase if using FCM.

### Android — `android/app/src/main/AndroidManifest.xml`

```xml
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_BACKGROUND_LOCATION" />
<uses-permission android:name="android.permission.CAMERA" />
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_LOCATION" />
<uses-permission android:name="android.permission.USE_BIOMETRIC" />
```
Add the Firebase `google-services.json` to `android/app/` for push.

## Native capabilities wired through `native-bridge.js`

| Feature | Web PWA (foreground only) | Native build |
|---------|---------------------------|--------------|
| Location | `navigator.geolocation.watchPosition` | `@capacitor-community/background-geolocation` — **background** GPS |
| Push | — | `@capacitor/push-notifications` (FCM/APNs); token POSTed to TOM |
| Barcode | browser `BarcodeDetector` + manual entry | `@capacitor-mlkit/barcode-scanning` hardware scanner |
| Biometric | toggle only | `capacitor-native-biometric` Face ID / Touch ID / fingerprint |
| Status bar / splash | meta tags | `@capacitor/status-bar`, `@capacitor/splash-screen` |

`app.js` calls these only when `window.TOMNative.isNative` is true, so the same
codebase runs as a browser PWA **and** as the native app with no branching in
the screens.

## App icons / splash

`../frontend/icons/*.png` (generated by `../tools/make_icons.py`) seed the
adaptive icons. To regenerate all native icon/splash sizes from one source,
optionally use [`@capacitor/assets`](https://github.com/ionic-team/capacitor-assets):

```bash
npx @capacitor/assets generate --iconBackgroundColor '#2A6BCC' --splashBackgroundColor '#0E1623'
```

## CI / release notes

* **iOS:** archive in Xcode → upload to App Store Connect (TestFlight first).
* **Android:** `npm run build:android` → signed `.aab` → Play Console internal testing.
* Bump the version in `capacitor.config.json` is **not** where versions live —
  set them in `ios/App/App.xcodeproj` (CFBundleShortVersionString) and
  `android/app/build.gradle` (`versionName`/`versionCode`).
