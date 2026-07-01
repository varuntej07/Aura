package dev.varuntej.aura.assistant

import android.service.voice.VoiceInteractionService

/**
 * Registers Buddy as a selectable digital-assistant app. Once the user picks Buddy in
 * Settings -> Default apps -> Digital assistant, the assist gesture (power-button hold /
 * "Hey" gesture) routes to [BuddyVoiceInteractionSession], which opens Buddy voice.
 *
 * Entirely opt-in: this has zero effect unless the user explicitly selects Buddy as the
 * assistant. Buddy provides no system speech recognition of its own; it hands off to its
 * LiveKit voice via the aura://voice deep link.
 */
class BuddyVoiceInteractionService : VoiceInteractionService()
