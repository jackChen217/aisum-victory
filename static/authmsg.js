/* What the page says when Google sign-in fails. No Firebase imports, so Node can test it
 * (tests/test_auth_messages.py).
 *
 * The raw Firebase code only goes to the console: users can't act on
 * "auth/network-request-failed", and people kept reading the harmless
 * cancelled-popup one as the site being broken.
 */

// A second click while a popup is open cancels the first popup, and closing the popup
// is the user's own choice. Neither is a failure.
export const SILENT_CODES = new Set(['auth/cancelled-popup-request', 'auth/popup-closed-by-user']);

export const FRIENDLY = "Sign-in didn't complete. Please try again.";

// auth/unauthorized-domain is the one error with something to do about it, and what
// depends on which address the page was opened at.
function domainHint(e, location) {
  if (!e || e.code !== 'auth/unauthorized-domain' || !location) return '';
  const host = location.hostname;
  if (host === 'localhost') return '';
  if (host.endsWith('.run.app')) {
    return ` ${location.host} is not on the sign-in list yet. The organizers add Cloud Run ` +
           'addresses; if this stays after a while, send them this address.';
  }
  return ` Sign-in is not allowed on ${location.host}. Open http://localhost:8000 in your ` +
         'browser. 127.0.0.1, and addresses that VS Code port forwarding or a tunnel gives ' +
         'you, do not work.';
}

/** The text to show for a sign-in error, or null to show nothing. */
export function signInMessage(e, location) {
  if (e && SILENT_CODES.has(e.code)) return null;
  return FRIENDLY + domainHint(e, location);
}
