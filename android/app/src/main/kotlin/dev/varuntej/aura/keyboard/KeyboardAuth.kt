package dev.varuntej.aura.keyboard

import com.google.firebase.auth.FirebaseAuth

/**
 * Mints a FRESH Firebase ID token for the Buddy Keyboard, inside the keyboard's own process.
 *
 * The IME cannot see the app's in-memory Firebase session, but Firebase Auth persists the
 * signed-in user to this app's private storage (shared across every process of the app's UID),
 * and FirebaseInitProvider auto-initialises the default FirebaseApp in every process at startup.
 * So FirebaseAuth.getInstance() here reflects the same user the app signed in, and getIdToken
 * refreshes a near-expiry token on demand. This is why the keyboard asks for a token here rather
 * than reading the app-bridged token off disk, which goes stale whenever the app has not run in
 * the last hour, the keyboard's normal state.
 *
 * Never throws: any failure (no signed-in user, Firebase not initialised, network) resolves to a
 * null token so the Buddy bar shows a graceful "open Aura" state instead of crashing.
 */
object KeyboardAuth {

    /**
     * Resolves a fresh ID token, or null when the user is not signed in or a token can't be
     * minted. The callback is delivered on the main thread (Firebase's default listener executor),
     * so it is safe to touch the keyboard's views from inside it.
     */
    fun freshIdToken(onResult: (String?) -> Unit) {
        try {
            val user = FirebaseAuth.getInstance().currentUser
            if (user == null) {
                onResult(null)
                return
            }
            user.getIdToken(false)
                .addOnSuccessListener { onResult(it.token) }
                .addOnFailureListener { onResult(null) }
        } catch (t: Throwable) {
            onResult(null)
        }
    }
}
