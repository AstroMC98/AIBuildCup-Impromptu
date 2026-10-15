// Firebase Authentication, loaded only when the server runs AUTH_MODE=firebase.
// The browser signs in with Firebase, then trades the ID token for our own
// session cookie (POST /api/auth/firebase); nothing else talks to Firebase.
const SDK = "https://www.gstatic.com/firebasejs/12.19.0";

let ready = null;

async function load(config) {
  if (!ready) {
    ready = (async () => {
      const [{ initializeApp }, fa] = await Promise.all([
        import(`${SDK}/firebase-app.js`),
        import(`${SDK}/firebase-auth.js`),
      ]);
      const app = initializeApp(config);
      return { fa, auth: fa.getAuth(app) };
    })();
  }
  return ready;
}

const MESSAGES = {
  "auth/invalid-credential": "That email and password don't match.",
  "auth/wrong-password": "That email and password don't match.",
  "auth/user-not-found": "There's no account with that email yet. Create one instead.",
  "auth/email-already-in-use": "There's already an account with that email. Sign in instead.",
  "auth/weak-password": "Use at least 6 characters for your password.",
  "auth/invalid-email": "That doesn't look like an email address.",
  "auth/popup-closed-by-user": "The sign-in window was closed.",
  "auth/popup-blocked": "Your browser blocked the sign-in window. Allow pop-ups for this site and try again.",
  "auth/too-many-requests": "Too many tries. Wait a minute and try again.",
  "auth/unauthorized-domain": "This site isn't on the Firebase project's list of authorized domains yet.",
};

export function friendly(err) {
  return MESSAGES[err?.code] || err?.message || "Sign-in didn't work. Try again.";
}

export async function signInGoogle(config) {
  const { fa, auth } = await load(config);
  const cred = await fa.signInWithPopup(auth, new fa.GoogleAuthProvider());
  return cred.user.getIdToken();
}

export async function signInEmail(config, email, password) {
  const { fa, auth } = await load(config);
  const cred = await fa.signInWithEmailAndPassword(auth, email, password);
  if (!cred.user.emailVerified) {
    await fa.sendEmailVerification(cred.user).catch(() => {});
    throw Object.assign(new Error("Verify your email first: we've sent you a new link."), { code: "verify" });
  }
  return cred.user.getIdToken();
}

/** Creates the account and sends a verification email; they sign in after verifying. */
export async function createAccount(config, name, email, password) {
  const { fa, auth } = await load(config);
  const cred = await fa.createUserWithEmailAndPassword(auth, email, password);
  if (name) await fa.updateProfile(cred.user, { displayName: name }).catch(() => {});
  await fa.sendEmailVerification(cred.user);
  await fa.signOut(auth);
}

export async function resetPassword(config, email) {
  const { fa, auth } = await load(config);
  await fa.sendPasswordResetEmail(auth, email);
}

export async function signOutFirebase(config) {
  const { fa, auth } = await load(config);
  await fa.signOut(auth);
}
