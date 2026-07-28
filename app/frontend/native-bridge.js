/* TOM Driver — native bridge.
 *
 * This file ships with the web PWA but only "lights up" when the same web
 * assets are wrapped by Capacitor into the native iOS / Android app. On a
 * plain browser, `window.Capacitor` is absent, so isNative === false and every
 * method is a safe no-op — the PWA keeps using the standard web APIs in app.js.
 *
 * Inside the native build, Capacitor exposes its plugins on
 * window.Capacitor.Plugins.*, so we can use them without a bundler/build step
 * (the app stays a no-build vanilla-JS frontend). Plugins referenced:
 *   - Geolocation              (@capacitor/geolocation)            foreground GPS
 *   - BackgroundGeolocation    (@capacitor-community/background-geolocation) background GPS
 *   - PushNotifications        (@capacitor/push-notifications)     FCM / APNs
 *   - BarcodeScanner           (@capacitor-mlkit/barcode-scanning) hardware scan
 *   - NativeBiometric          (capacitor-native-biometric)        Face/Touch ID
 *   - StatusBar / App / Network are optional niceties.
 */
(function () {
  'use strict';
  const Cap = window.Capacitor;
  const P = (Cap && Cap.Plugins) || {};
  const isNative = !!(Cap && typeof Cap.isNativePlatform === 'function' && Cap.isNativePlatform());

  let _bgWatchId = null;

  const Native = {
    isNative,
    platform: (Cap && Cap.getPlatform && Cap.getPlatform()) || 'web',

    /* Called once at boot from app.js. Wires push + status bar; requests perms. */
    async init(opts) {
      if (!isNative) return;
      opts = opts || {};
      // Status bar style to match the dark default header.
      try { if (P.StatusBar) await P.StatusBar.setStyle({ style: 'DARK' }); } catch (e) {}

      // Push notifications (job offers, ops messages).
      try {
        if (P.PushNotifications) {
          const perm = await P.PushNotifications.requestPermissions();
          if (perm && perm.receive === 'granted') {
            await P.PushNotifications.register();
            P.PushNotifications.addListener('registration', (t) => { Native._pushToken = t && t.value; });
            P.PushNotifications.addListener('pushNotificationReceived', () => opts.onPush && opts.onPush());
            P.PushNotifications.addListener('pushNotificationActionPerformed', () => opts.onPush && opts.onPush());
          }
        }
      } catch (e) { /* push optional */ }
    },

    /* Device push token, if registered — POST this to TOM so it can target the driver. */
    pushToken() { return this._pushToken || null; },

    /* Background GPS: keeps streaming when the screen is off / app backgrounded. */
    async startBackgroundTracking(onPing) {
      if (!isNative) return false;
      try {
        if (P.BackgroundGeolocation) {
          _bgWatchId = await P.BackgroundGeolocation.addWatcher(
            { backgroundMessage: 'TOM Driver is sharing your location while on shift.',
              backgroundTitle: 'On shift', requestPermissions: true, stale: false, distanceFilter: 25 },
            (loc) => { if (loc) onPing({ lat: loc.latitude, lng: loc.longitude, spd: loc.speed, hdg: loc.bearing, acc: loc.accuracy }); }
          );
          return true;
        }
        // Fallback: foreground Geolocation plugin (still better perms UX than web).
        if (P.Geolocation) {
          _bgWatchId = await P.Geolocation.watchPosition({ enableHighAccuracy: true }, (pos) => {
            if (pos && pos.coords) onPing({ lat: pos.coords.latitude, lng: pos.coords.longitude, spd: pos.coords.speed, hdg: pos.coords.heading, acc: pos.coords.accuracy });
          });
          return true;
        }
      } catch (e) { /* fall back to web watcher in app.js */ }
      return false;
    },

    async stopBackgroundTracking() {
      if (_bgWatchId == null) return;
      try {
        if (P.BackgroundGeolocation) await P.BackgroundGeolocation.removeWatcher({ id: _bgWatchId });
        else if (P.Geolocation) await P.Geolocation.clearWatch({ id: _bgWatchId });
      } catch (e) {}
      _bgWatchId = null;
    },

    /* Hardware barcode scan — returns the decoded string, or null if unavailable. */
    async scanBarcode() {
      if (!isNative || !P.BarcodeScanner) return null;
      try {
        const granted = await P.BarcodeScanner.requestPermissions();
        if (granted && granted.camera !== 'granted' && granted.camera !== 'limited') return null;
        const res = await P.BarcodeScanner.scan();
        const codes = (res && res.barcodes) || [];
        return codes.length ? codes[0].rawValue : null;
      } catch (e) { return null; }
    },

    /* Biometric unlock (Face ID / Touch ID / fingerprint). Resolves true/false. */
    async biometricAuth(reason) {
      if (!isNative || !P.NativeBiometric) return false;
      try {
        const avail = await P.NativeBiometric.isAvailable();
        if (!avail || !avail.isAvailable) return false;
        await P.NativeBiometric.verifyIdentity({ reason: reason || 'Unlock TOM Driver', title: 'TOM Driver' });
        return true;
      } catch (e) { return false; }
    },
  };

  window.TOMNative = Native;
})();
