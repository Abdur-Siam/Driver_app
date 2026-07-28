/* TOM Driver — runtime API configuration.
 *
 * In the browser / installed PWA the app is served by the TOM backend itself,
 * so API calls stay same-origin and this file does nothing.
 *
 * In the native iOS/Android build (Capacitor) the same assets load from
 * capacitor://localhost, where relative "/api/..." URLs have no server —
 * so point them at the deployed TOM backend here. Edit NATIVE_API_BASE per
 * environment, then re-run `npx cap copy` and rebuild.
 */
(function () {
  'use strict';
  var NATIVE_API_BASE = 'https://driver.xtramile.example';   // ← set to the live TOM backend
  // (Simulator demo: 'http://127.0.0.1:5179' reaches a local `./run.sh` backend —
  //  the iOS simulator shares the Mac's loopback. Rebuild after editing.)
  var p = location.protocol;
  if (p === 'capacitor:' || p === 'ionic:') {
    window.TOM_API_BASE = NATIVE_API_BASE;
  }
})();
