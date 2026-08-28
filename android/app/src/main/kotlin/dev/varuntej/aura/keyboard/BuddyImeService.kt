package dev.varuntej.aura.keyboard

import android.animation.Animator
import android.animation.ObjectAnimator
import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.res.ColorStateList
import android.net.Uri
import android.graphics.Typeface
import android.inputmethodservice.InputMethodService
import android.media.AudioManager
import android.os.Handler
import android.os.Looper
import android.os.Bundle
import android.os.SystemClock
import android.text.TextUtils
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputConnection
import android.view.inputmethod.InputMethodManager
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.core.widget.TextViewCompat
import dev.varuntej.aura.R
import dev.varuntej.aura.keyboard.input.BackspaceTouchHandler
import dev.varuntej.aura.keyboard.input.DoubleSpacePeriod
import dev.varuntej.aura.keyboard.input.HorizontalSwipeScrollView
import dev.varuntej.aura.keyboard.input.KeyPopupOptions
import dev.varuntej.aura.keyboard.input.KeyPreview
import dev.varuntej.aura.keyboard.input.KeyTouchHandler
import dev.varuntej.aura.keyboard.input.PunctuationSpacer
import dev.varuntej.aura.keyboard.input.SentenceCapitalizer
import dev.varuntej.aura.keyboard.input.ShiftMode
import dev.varuntej.aura.keyboard.input.ShiftState
import dev.varuntej.aura.keyboard.performance.KeyboardPerformanceTrace
import dev.varuntej.aura.keyboard.prediction.BaseDictionary
import dev.varuntej.aura.keyboard.prediction.CachedAutocorrect
import dev.varuntej.aura.keyboard.prediction.CachedAutocorrectPolicy
import dev.varuntej.aura.keyboard.prediction.LexicalPredictionEngine
import dev.varuntej.aura.keyboard.prediction.KeyboardPersonalizationRepository
import dev.varuntej.aura.keyboard.prediction.NextWordPredictor
import dev.varuntej.aura.keyboard.prediction.OnDeviceReranker
import dev.varuntej.aura.keyboard.prediction.PersonalDictionary
import dev.varuntej.aura.keyboard.prediction.PersonalizationEvent
import dev.varuntej.aura.keyboard.prediction.PredictionCoordinator
import dev.varuntej.aura.keyboard.prediction.PredictionPayload
import dev.varuntej.aura.keyboard.prediction.PredictionRequest
import dev.varuntej.aura.keyboard.prediction.PredictionStage
import dev.varuntej.aura.keyboard.prediction.Suggestion
import dev.varuntej.aura.keyboard.prediction.SuggestionCommitPolicy
import dev.varuntej.aura.keyboard.prediction.SuggestionSource
import dev.varuntej.aura.keyboard.prediction.SystemUserDictionary
import dev.varuntej.aura.keyboard.prediction.WordComposer
import dev.varuntej.aura.keyboard.settings.KeyboardConsentStore
import dev.varuntej.aura.keyboard.settings.KeyboardRuntimeDiagnostics
import dev.varuntej.aura.keyboard.settings.KeyboardSettingsActivity
import dev.varuntej.aura.keyboard.settings.KeyboardSettingsSnapshot
import dev.varuntej.aura.keyboard.settings.KeyboardSettingsStore
import dev.varuntej.aura.keyboard.settings.KeyboardThemeContext
import org.json.JSONObject

// Falls back to prod when the app hasn't bridged a base URL yet (fresh install, or the keyboard
// used before the app's first authenticated launch). Matches the prod apiBaseUrl in
// lib/core/config/environment.dart.
private const val DEFAULT_API_BASE_URL = "https://juno-backend-620715294422.us-central1.run.app"

// The layouts that share the QWERTY letters page (so the home-row stagger applies and
// the symbols toggle is meaningful).
private val QWERTY_FAMILY = setOf(KeyLayout.QWERTY, KeyLayout.EMAIL, KeyLayout.URL)

// How many word suggestions the strip shows at once.
private const val SUGGESTION_LIMIT = 3

// How many recent voice caption lines stay on screen at once (Spotify-lyrics style: the
// newest is active, older ones fade upward before being dropped).
private const val MAX_VOICE_CAPTION_LINES = 4

// Two shift taps within this window latch caps lock.
private const val SHIFT_DOUBLE_TAP_WINDOW_MS = 300L

// Aura tuning points. Lexical work is short-debounced and conflated; the separate correction /
// neural pass waits inside the user-requested 150-250 ms measurement window.
private const val LEXICAL_DEBOUNCE_MS = 24L
private const val DEFERRED_PREDICTION_MS = 180L
private const val NO_PERSONALIZATION_GENERATION = -1L

// Two spaces within this window (between words) become ". ".
private const val DOUBLE_SPACE_WINDOW_MS = 500L

// A short local window for recognizing backspace-to-edit as feedback on the just-committed word.
private const val COMMIT_EDIT_GUARD_MS = 3_000L
private const val PERFORMANCE_SNAPSHOT_COMMAND =
    "dev.varuntej.aura.keyboard.PERFORMANCE_SNAPSHOT"

// How much of a long clipboard entry the paste chip previews.
private const val CLIPBOARD_PREVIEW_CHARS = 30

// How many recently-used emojis the emoji panel remembers.
private const val EMOJI_RECENTS_MAX = 32

/** How many writing-tool tiles fit across the panel before the row starts scrolling. */
private const val WRITING_TOOLS_VISIBLE = 4

// --- Writing-tool span limits ------------------------------------------------
// Hard ceiling on the text a draft may transform, matching CONTEXT_MAX_CHARS in
// backend/src/services/keyboard/drafter.py. Past this the server truncates silently, so the
// model would never see the tail while the keyboard happily deleted it.
private const val DRAFT_MAX_CHARS = 2000

// The ceiling on a span the keyboard picks BY ITSELF, with nothing selected. Lower than
// DRAFT_MAX_CHARS on purpose: the draft runs on the lite tier under a 6s server deadline and a
// grammar pass emits about as many tokens as it eats, so a 2000-char rewrite nobody asked for is
// a timeout risk. An explicit selection is the user's own call and gets the full ceiling.
private const val DRAFT_AUTO_WINDOW_MAX = 1200

// How much text is read on each side of the cursor. A read that comes back at exactly this
// length may have been truncated by the host, so its far edge is never trusted as a boundary.
private const val DRAFT_READ_CHARS = 2000

// Surrounding text sent for register but never replaced. Mirrors the server's
// CONTEXT_MAX_CHARS / CONTEXT_AFTER_MAX_CHARS.
private const val DRAFT_CONTEXT_BEFORE_MAX = 2000
private const val DRAFT_CONTEXT_AFTER_MAX = 500

// Panel header titles. Every takeover entry point sets one, so a panel never wears the
// title of whatever was opened before it.
private const val WHITEBOARD_TITLE = "Writing Tools"
private const val INTRO_PANEL_TITLE = "Your privacy"
private const val VOICE_PANEL_TITLE = "Talk to Buddy"
private const val CONSENT_PANEL_TITLE = "Heads up"

// The first-use privacy panel opens a little taller than the keyboard: it is a page of prose
// plus a choice, and at exact key-grid height it read as a sliver. Kept modest on purpose --
// the card is vertically centred, so every extra pixel here becomes dead space above the
// heading and below the buttons. Every other takeover stays at 1f. Capped at a fraction of
// the screen so a tall scale can never swallow the display.
private const val INTRO_PANEL_HEIGHT_SCALE = 1.08f
private const val MAX_PANEL_SCREEN_FRACTION = 0.72f
// Prefs (non-secure) for the emoji recents row.
// Canonical name lives with the deletion path so a new store cannot outlive clear-all.
private const val EMOJI_PREFS = KeyboardOwnedStores.EMOJI_PREFS
private const val EMOJI_RECENTS_KEY = "recents"

/**
 * Buddy Keyboard, the native Android IME.
 *
 * A real keyboard (QWERTY + symbols page) with ONE entry point on top: the Aura orb.
 * Default state is a clean keyboard with a slim "(orb) Buddy" bar. Tapping the orb does
 * a full takeover, the keys give way to the Buddy whiteboard, a focused panel where you
 * pick an action (Reply as me / Continue / Rewrite / Grammar / Translate), Buddy drafts
 * in your voice, and you tap a draft to drop it into your message.
 *
 * Both layers (typing stack + whiteboard) are pinned to the same height, so opening the
 * whiteboard never resizes the keyboard and the host app never reflows.
 *
 * Plain framework Views only (no Compose, no deprecated KeyboardView). Drafting calls
 * the same backend path as before ([KeyboardDraftClient] + [KeyboardCredentialStore]).
 */
class BuddyImeService : InputMethodService() {

    private enum class Mode { TYPING, WHITEBOARD, EMOJI }

    private var mode = Mode.TYPING
    // Shift is a real state machine now: NONE / one-shot SHIFTED / CAPS_LOCK, with auto-capitalize.
    private val shiftState = ShiftState()
    private var lastShiftTapAt = 0L   // for double-tap -> caps lock detection
    private var symbolsPage = false

    // Recomputed once per focus in onStartInputView: it decides the key layout, whether
    // the Buddy bar shows, whether memory drafting is allowed, and the draft field_type.
    private var fieldProfile: FieldProfile = FieldProfile.text()
    // The enter key's label, adapted to the field's IME action (Send / Search / Go ...).
    private var enterKeyLabel = "↵"

    @Volatile
    private var keyboardSettings = KeyboardSettingsSnapshot()
    private lateinit var keyboardUiContext: Context
    private var appliedKeyboardNightMode = 0
    private var keyPreview: KeyPreview? = null
    private val audioManager by lazy { getSystemService(Context.AUDIO_SERVICE) as AudioManager }
    private val suggestionsAllowed: Boolean
        get() = fieldProfile.predictionsAllowed && keyboardSettings.suggestions
    private val autocorrectAllowed: Boolean
        get() = fieldProfile.autocorrectAllowed && keyboardSettings.autocorrect
    private val learningAllowed: Boolean
        get() = fieldProfile.learningAllowed && keyboardSettings.learnNewWords
    private val predictionWorkAllowed: Boolean
        get() = fieldProfile.predictionsAllowed &&
            (keyboardSettings.suggestions || keyboardSettings.autocorrect)
    private val typingIntelligenceAllowed: Boolean
        get() = fieldProfile.predictionsAllowed &&
            (predictionWorkAllowed || learningAllowed)

    private lateinit var typingStack: LinearLayout
    private lateinit var collapsedBar: LinearLayout
    private lateinit var keysContainer: LinearLayout
    private lateinit var whiteboard: LinearLayout

    // Emoji panel (full takeover, like the whiteboard). Built once, repopulated per category.
    private lateinit var emojiContainer: LinearLayout
    private lateinit var emojiTabs: LinearLayout
    private lateinit var emojiGrid: LinearLayout
    // The tab strip's scroller and the grid's scroller. Held so a category swipe can pull the
    // selected tab into view and reset the grid to the top.
    private lateinit var emojiTabsScroll: HorizontalScrollView
    private lateinit var emojiGridScroll: HorizontalSwipeScrollView
    // The selected emoji category: -1 is the dynamic "recently used" tab, else an index into
    // EmojiData.categories.
    private var selectedEmojiCategory = -1

    // Whiteboard subviews, built once and repopulated by state.
    private lateinit var wbActions: LinearLayout
    private lateinit var wbSubRow: HorizontalScrollView
    private lateinit var wbSub: LinearLayout
    private lateinit var wbContext: TextView
    private lateinit var wbPreview: ScrollView
    private lateinit var wbCanvas: LinearLayout
    // The Regenerate + green "Use this" row, shown only while a draft fills the preview box.
    private lateinit var useThisRow: LinearLayout
    // The action-tile strip. Held so panels that show no tools can hide its fixed height
    // instead of leaving it as a dead band under the card.
    private lateinit var wbActionsScroll: HorizontalScrollView
    // The panel header title. Every entry point sets it, so no panel inherits another's.
    private lateinit var wbTitle: TextView

    // The writing tool (tone tab) currently selected in the panel.
    private var selectedTool: WritingTool? = null
    // The draft currently shown in the preview box, inserted on "Use this".
    private var previewText: String? = null
    // The exact span the pending draft transforms, resolved when the request goes out and
    // verified again before anything is deleted. Null means "append at the cursor" (Reply).
    private var draftTarget: DraftTarget? = null
    // Bumped on every field focus. A target stamped with an older value belongs to a field that
    // is no longer on screen and must never be written to.
    private var inputSession: Long = 0L
    // The last draft args, so "Regenerate" / "Try again" can repeat it.
    private var lastAction: BuddyAction? = null
    private var lastTone: String? = null
    private var lastLang: String? = null

    // Looping shimmer animators on the "drafting" placeholders; cancelled on every render.
    private val activeAnimators = mutableListOf<Animator>()

    // In-process voice to Buddy (native LiveKit/WebRTC). Lazily created on first Voice tap,
    // stopped when the field or the whiteboard closes. Lifecycle state only.
    private var voiceController: KeyboardVoiceController? = null

    // The live voice panel: a bottom-anchored caption column (Spotify-lyrics style) plus a
    // waveform meter pinned mid-right. Built once per session by [buildVoiceStage] and updated
    // in place as state + transcripts arrive, so running animations survive the whole turn.
    private var voiceStage: View? = null
    private var voiceWaveform: VoiceWaveformView? = null
    private var voiceCaptionStack: LinearLayout? = null
    private var voiceStatusLine: TextView? = null
    private val voiceCaptions = LinkedHashMap<String, CaptionLine>()

    // Letter keys whose label tracks the shift state, so we can recase them without
    // rebuilding the whole grid on every keystroke.
    private val letterKeyViews = mutableListOf<Pair<TextView, String>>()
    // The shift key view, so its glyph/highlight can reflect SHIFTED vs CAPS_LOCK without a rebuild.
    private var shiftKeyView: TextView? = null
    private var lastShiftKeyMode: ShiftMode? = null

    // The upper/lower case the 26 letter keys are currently rendered in, so refreshLetterCase can
    // skip relabeling every key when the case has not actually changed (the common per-keystroke
    // case). Reset to null on a key rebuild, which forces exactly one relabel for the fresh views.
    private var lastLetterCaseUpper: Boolean? = null

    // The in-progress word. Letters are COMMITTED to the field directly per keystroke (so they
    // land instantly with no underline, like Gboard); this buffer mirrors that word in memory as
    // the source of truth for prediction and autocorrect-on-separator. Engaged only when
    // fieldProfile.predictionsAllowed is true; in numeric / phone / PIN / password fields it stays
    // empty and the keyboard just commits every keystroke.
    private val composer = WordComposer()

    // We commit letters directly now (no composing region), so the framework no longer hands back
    // a composing span in onUpdateSelection to recognize our own edits. Instead we predict where
    // each of our edits leaves the cursor (advanceCursor) and compare: a matching update is ours
    // (ignore), anything else is a real external move (the composing word is dropped). resyncExpected
    // trusts the next update verbatim (after a focus change or a variable-length edit).
    private var expectedSelStart = -1
    private var expectedSelEnd = -1
    private var resyncExpected = true

    // The adaptive collapsed bar: the action hint ("✦ draft" / generate / talk) and a word
    // suggestion strip occupy the same center slot, one visible at a time. The strip's chips
    // are built once per focus and updated by setText each keystroke (no per-keystroke view
    // rebuild). currentSuggestions is the last rendered set, re-applied when the bar rebuilds.
    private lateinit var idleToolbar: LinearLayout
    private lateinit var suggestionStrip: LinearLayout
    private val suggestionChips = mutableListOf<TextView>()
    private val suggestionChipVisuals = mutableListOf<SuggestionChipVisual?>()
    private var currentSuggestions: List<Suggestion> = emptyList()
    private enum class SuggestionStripMode { EMPTY, SUGGESTIONS, UNDO, CLIPBOARD, INTRO, NOTICE }

    // Once the first-use line has been seen or has run out of showings, stop touching the
    // consent prefs on every field focus. Typing must never wait on a disk read.
    private var introBannerRetired = false
    private var suggestionStripMode = SuggestionStripMode.EMPTY
    private var renderedSuggestionPersonalizationGeneration = NO_PERSONALIZATION_GENERATION
    private data class SuggestionChipVisual(val text: String, val visibility: Int, val accent: Boolean)

    private val mainHandler = Handler(Looper.getMainLooper())
    private var isDestroyed = false
    private val personalizationGenerationListener: (Long) -> Unit = { generation ->
        mainHandler.post {
            if (!isDestroyed) onPersonalizationGenerationChanged(generation)
        }
    }

    // The user's encrypted locally-learned state, exposed through immutable in-memory snapshots.
    // Built lazily on the first eligible field so a numeric-only session never opens the store.
    // Its first touch is forced on the main thread in onStartInputView; reads then come from the
    // background
    // prediction thread, so the lazy is thread-safe. A new snapshot generation invalidates any
    // suggestion or autocorrect decision computed from the previous learned state.
    private val personalDictionaryLazy = lazy<PersonalDictionary> {
        KeyboardPersonalizationRepository.addGenerationListener(personalizationGenerationListener)
        KeyboardPersonalizationRepository.dictionary(applicationContext)
    }
    private val personalDictionary: PersonalDictionary by personalDictionaryLazy

    // One process-lifetime lexical engine and one conflated worker. Publishing a request from the
    // key path is constant-time; no Handler Runnable or executor job accumulates per keystroke.
    private val lexicalEngineLazy = lazy {
        LexicalPredictionEngine(
            personalDictionary,
            OnDeviceReranker(applicationContext),
            personalizationEnabled = { keyboardSettings.personalizedSuggestions },
            neuralRerankingEnabled = {
                keyboardSettings.suggestions && keyboardSettings.personalizedSuggestions
            },
        )
    }
    private val predictionCoordinatorLazy = lazy {
        PredictionCoordinator(
            lexicalDelayMs = LEXICAL_DEBOUNCE_MS,
            deferredDelayMs = DEFERRED_PREDICTION_MS,
            lexicalWork = lexicalEngineLazy.value::lexical,
            deferredWork = lexicalEngineLazy.value::deferred,
            deliver = { generation, stage, payload ->
                mainHandler.post { applyPrediction(generation, stage, payload) }
            },
            observer = KeyboardPerformanceTrace.predictionObserver,
            workerCleanup = { lexicalEngineLazy.value.close() },
        )
    }
    private val predictionCoordinator: PredictionCoordinator<PredictionRequest, PredictionPayload>
        by predictionCoordinatorLazy
    private var activePredictionGeneration = -1L
    private data class CachedAutocorrectState(
        val generation: Long,
        val personalizationGeneration: Long,
        val decision: CachedAutocorrect,
    )
    private var cachedAutocorrect: CachedAutocorrectState? = null

    // The last word committed by a separator or chosen from the strip, so next-word prediction
    // after a space can offer likely continuations. Cleared on a fresh field / new sentence.
    private var lastCommittedWord: String = ""
    private val committedWordHistory = ArrayDeque<String>(2)
    private var manualCorrectionOrigin: String? = null
    private data class PendingCommittedWord(
        val rawWord: String,
        val finalWord: String,
        val separator: String,
        val committedAtMillis: Long,
        val autocorrected: Boolean,
    )
    private var pendingCommittedWord: PendingCommittedWord? = null

    // The time of the last committed space, for the double-space-to-period window.
    private var lastSpaceCommitAt = 0L

    // The clipboard text the user pulled in via the 📋 affordance (full text to paste), shown as a
    // strip chip until the next keypress. Null when no clipboard chip is active.
    private var clipboardChip: String? = null

    // The last autocorrect, kept so the strip can offer a one-tap undo until the user types on.
    private data class PendingUndo(val original: String, val corrected: String, val separator: String)
    private data class FinalizedWord(
        val typedWord: String,
        val rawWord: String,
        val finalWord: String,
        val autocorrect: CachedAutocorrect?,
        val manualCorrectionOrigin: String?,
    )
    private var pendingUndo: PendingUndo? = null

    private val langOptions = listOf("English", "Spanish", "Hindi", "French", "German")

    override fun onCreate() {
        super.onCreate()
        keyboardSettings = KeyboardSettingsStore.read(this)
        refreshKeyboardUiContext()
        KeyboardRuntimeDiagnostics.attach(this) {
            lexicalEngineLazy.takeIf { it.isInitialized() }?.value?.neuralDiagnostics()
        }
    }

    override fun onCreateInputView(): View = createKeyboardInputView()

    private fun createKeyboardInputView(): View {
        keyPreview?.dismiss()
        keyPreview = if (keyboardSettings.keyPreview) KeyPreview(keyboardUiContext) else null
        val root = LayoutInflater.from(keyboardUiContext).inflate(R.layout.buddy_keyboard_view, null)
        typingStack = root.findViewById(R.id.typing_stack)
        collapsedBar = root.findViewById(R.id.collapsed_bar)
        keysContainer = root.findViewById(R.id.keys_container)
        whiteboard = root.findViewById(R.id.whiteboard_container)
        emojiContainer = root.findViewById(R.id.emoji_container)

        buildCollapsedBar()
        buildWhiteboard()
        buildEmojiPanel()
        rebuildKeys()
        return root
    }

    override fun onStartInputView(info: EditorInfo, restarting: Boolean) {
        super.onStartInputView(info, restarting)
        // Fresh field: back to typing, letters page, capitalized. Recompute the field
        // profile so the layout, the Buddy bar, and the enter label all fit this field.
        symbolsPage = false
        inputSession++
        draftTarget = null
        fieldProfile = FieldProfile.fromEditorInfo(info)
        enterKeyLabel = enterLabelFor(info)
        val latestSettings = KeyboardSettingsStore.read(this)
        val themeChanged = latestSettings.themeMode != keyboardSettings.themeMode ||
            KeyboardThemeContext.effectiveNightMode(this, latestSettings.themeMode) !=
            appliedKeyboardNightMode
        keyboardSettings = latestSettings
        if (themeChanged) {
            refreshKeyboardUiContext()
            setInputView(createKeyboardInputView())
        } else {
            keyPreview?.dismiss()
            keyPreview = if (keyboardSettings.keyPreview) KeyPreview(keyboardUiContext) else null
        }
        // Warm only local dictionaries off the UI thread the first time a prose field is focused.
        // Cloud UserAura vocabulary is deliberately absent from automatic typing/prediction.
        if (typingIntelligenceAllowed) {
            BaseDictionary.ensureLoaded(this)
            if (keyboardSettings.personalizedSuggestions) SystemUserDictionary.ensureFresh(this)
            if (suggestionsAllowed) NextWordPredictor.ensureLoaded(this)
            // Construction only starts the bounded personalization worker; Keystore, migration,
            // snapshot loading, and persistence all remain off the IME thread.
            personalDictionary
        }
        // Warm the credential cache off the main thread so a later draft/voice tap reads the API
        // base URL instantly instead of decrypting EncryptedSharedPreferences on the input thread.
        // Done for every field, since the mic (voice) is available in all of them.
        KeyboardCredentialStore.warmCache(applicationContext) { credential ->
            mainHandler.post { applyAccountBoundary(credential?.uid.orEmpty()) }
        }
        resetToTyping()
        if (suggestionsAllowed && keyboardSettings.personalizedSuggestions) {
            // Initialize and warm the optional ONNX session on the conflated prediction worker.
            // A first key cancels this request immediately; typing never waits for warmup.
            activePredictionGeneration = predictionCoordinator.submit(PredictionRequest.Warmup)
        }
        // Seed the expected cursor position from the field's initial selection so onUpdateSelection
        // can tell our own edits from external moves without a composing region. When unknown (-1),
        // trust the first update verbatim instead.
        if (info.initialSelStart >= 0) {
            expectedSelStart = info.initialSelStart
            expectedSelEnd = info.initialSelEnd
            resyncExpected = false
        } else {
            markResync()
        }
        // The Buddy bar is always present so the user can talk to Buddy from ANY field in
        // ANY app (the mic on the right is always available). Its LEFT action adapts: memory
        // drafting in text fields, generate-password in password fields, and a plain "Talk to
        // Buddy" elsewhere. Plain typing always works regardless.
        buildCollapsedBar()
        collapsedBar.visibility = View.VISIBLE
        rebuildKeys()
        // Auto-capitalize the first letter from the field's existing content (empty field -> caps).
        updateAutoCap()
        // AFTER buildCollapsedBar: that call replaces idleToolbar with a fresh instance, so a
        // banner shown before it would have its visibility change thrown away on every focus.
        maybeShowIntroChip()
    }

    override fun onFinishInputView(finishingInput: Boolean) {
        keyPreview?.dismiss()
        // Drop any in-progress word buffer before the field loses focus (the letters are already
        // committed to the field, so nothing is lost).
        finishComposing()
        // The field lost focus (or the keyboard hid): never leave a voice session live.
        voiceController?.stop()
        teardownVoiceStage()
        super.onFinishInputView(finishingInput)
    }

    override fun onAppPrivateCommand(action: String?, data: Bundle?) {
        if (action == PERFORMANCE_SNAPSHOT_COMMAND) {
            KeyboardPerformanceTrace.captureRuntimeSnapshot(
                lexicalEngineLazy.takeIf { it.isInitialized() }?.value?.neuralDiagnostics(),
            )
            return
        }
        super.onAppPrivateCommand(action, data)
    }

    /**
     * The cursor moved or the text changed. Letters commit directly now (no composing region), so
     * the framework no longer hands us a span to key off; we compare the reported selection against
     * the position we predicted for our own edits ([advanceCursor]). A match is our edit (ignore).
     * On a mismatch we do ONE verification read: if our word is still intact right before a collapsed
     * cursor it was a belated/coalesced self-update (keep composing); otherwise it is a real external
     * move and the composing word is stale, so we drop it.
     */
    override fun onUpdateSelection(
        oldSelStart: Int,
        oldSelEnd: Int,
        newSelStart: Int,
        newSelEnd: Int,
        candidatesStart: Int,
        candidatesEnd: Int,
    ) {
        super.onUpdateSelection(
            oldSelStart, oldSelEnd, newSelStart, newSelEnd, candidatesStart, candidatesEnd,
        )
        if (resyncExpected) {
            expectedSelStart = newSelStart
            expectedSelEnd = newSelEnd
            resyncExpected = false
            return
        }
        if (!composer.isComposing) {
            if (newSelStart != expectedSelStart || newSelEnd != expectedSelEnd) {
                if (predictionCoordinatorLazy.isInitialized()) predictionCoordinator.invalidate()
                activePredictionGeneration = -1L
                cachedAutocorrect = null
                committedWordHistory.clear()
                pendingCommittedWord = null
                manualCorrectionOrigin = null
                clearSuggestions()
            }
            expectedSelStart = newSelStart
            expectedSelEnd = newSelEnd
            return
        }
        if (newSelStart == expectedSelStart && newSelEnd == expectedSelEnd) {
            return // the move we predicted for our own edit
        }
        // Mismatch: verify whether our composing word is still intact before a collapsed cursor.
        val word = composer.current
        val intact = newSelStart == newSelEnd &&
            currentInputConnection?.getTextBeforeCursor(word.length, 0)?.toString() == word
        if (intact) {
            expectedSelStart = newSelStart
            expectedSelEnd = newSelEnd
            return
        }
        // Real external move / selection / host edit: drop the stale word.
        finishComposing()
        expectedSelStart = newSelStart
        expectedSelEnd = newSelEnd
        updateAutoCap()
    }

    /** Record that one of our own edits moved the cursor by [delta] (so the matching
     *  onUpdateSelection is recognized as ours). Falls back to a resync when unseeded. */
    private fun advanceCursor(delta: Int) {
        if (expectedSelStart < 0) { markResync(); return }
        expectedSelStart += delta
        expectedSelEnd = expectedSelStart
    }

    /** Trust the next onUpdateSelection verbatim. Used after a variable-length or non-hot-path edit
     *  (paste, draft insert, password, selection delete) where computing the delta is fragile. */
    private fun markResync() {
        resyncExpected = true
    }

    override fun onDestroy() {
        isDestroyed = true
        KeyboardRuntimeDiagnostics.detach(this)
        keyPreview?.dismiss()
        voiceController?.stop()
        voiceController = null
        teardownVoiceStage()
        mainHandler.removeCallbacksAndMessages(null)
        if (predictionCoordinatorLazy.isInitialized()) predictionCoordinator.close()
        if (personalDictionaryLazy.isInitialized()) {
            KeyboardPersonalizationRepository.removeGenerationListener(
                personalizationGenerationListener,
            )
        }
        super.onDestroy()
    }

    /** The enter key label, adapted to the field's IME action so it reads Send / Search
     *  / Go / Next / Done where the host asked for one, else the return glyph. */
    private fun enterLabelFor(info: EditorInfo?): String {
        val action = (info?.imeOptions ?: 0) and EditorInfo.IME_MASK_ACTION
        return when (action) {
            EditorInfo.IME_ACTION_SEND -> "Send"
            EditorInfo.IME_ACTION_SEARCH -> "Search"
            EditorInfo.IME_ACTION_GO -> "Go"
            EditorInfo.IME_ACTION_NEXT -> "Next"
            EditorInfo.IME_ACTION_DONE -> "Done"
            else -> "↵"
        }
    }

    // --- Key grid ----------------------------------------------------------------

    private fun rebuildKeys() {
        keysContainer.removeAllViews()
        letterKeyViews.clear()
        shiftKeyView = null
        lastShiftKeyMode = null
        val rows = currentRows()
        // Two separate things, deliberately not one flag. The row-height ladder follows the
        // four-row QWERTY SHAPE, which the symbols page shares exactly (row 2 ends in
        // backspace, row 3 is the same bottom bar), so both pages render at the same total
        // height and the space bar is the same size on each. The half-key home-row stagger is
        // letters-only: the symbols page's top row is a full ten keys and must stay flush.
        val isQwertyShapedPage = fieldProfile.layout in QWERTY_FAMILY
        val isLettersPage = !symbolsPage && isQwertyShapedPage
        rows.forEachIndexed { index, row ->
            val heightScale = when {
                isQwertyShapedPage && index == 2 -> 0.86f
                isQwertyShapedPage && index == 3 -> 0.80f
                else -> 1f
            }
            keysContainer.addView(
                buildRow(
                    row,
                    indentHalfKey = isLettersPage && index == 1,
                    heightScale = heightScale,
                ),
            )
        }
        // Fresh views were built in base (lower) case; force one relabel so an active SHIFTED /
        // CAPS_LOCK state is applied, then the per-keystroke skip in refreshLetterCase takes over.
        lastLetterCaseUpper = null
        refreshLetterCase()
    }

    /** The rows to render for the current field profile and symbols toggle. Numeric,
     *  phone, and PIN layouts ignore the symbols toggle (it never shows for them). */
    private fun currentRows(): List<List<Key>> = when (fieldProfile.layout) {
        KeyLayout.NUMERIC -> KeyboardLayout.numericRows
        KeyLayout.PHONE -> KeyboardLayout.phoneRows
        KeyLayout.PIN -> KeyboardLayout.pinRows
        KeyLayout.QWERTY, KeyLayout.EMAIL, KeyLayout.URL ->
            if (symbolsPage) {
                KeyboardLayout.symbolsRows
            } else when (fieldProfile.layout) {
                KeyLayout.EMAIL -> KeyboardLayout.emailRows
                KeyLayout.URL -> KeyboardLayout.urlRows
                else -> KeyboardLayout.lettersRows
            }
    }

    private fun buildRow(
        row: List<Key>,
        indentHalfKey: Boolean,
        heightScale: Float,
    ): LinearLayout {
        val rowLayout = LinearLayout(keyboardUiContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            )
        }
        if (indentHalfKey) rowLayout.addView(keySpacer(0.5f))
        for (key in row) rowLayout.addView(buildKey(key, heightScale))
        if (indentHalfKey) rowLayout.addView(keySpacer(0.5f))
        return rowLayout
    }

    /** A zero-width, weighted gap used to indent a row (the half-key home-row stagger). */
    private fun keySpacer(weight: Float): View = View(keyboardUiContext).apply {
        layoutParams = LinearLayout.LayoutParams(0, dp(1), weight)
    }

    private fun buildKey(key: Key, heightScale: Float): View {
        val view = TextView(keyboardUiContext).apply {
            gravity = Gravity.CENTER
            setBackgroundResource(keyBackground(key))
            setTextColor(
                color(
                    if (key is Key.Func && key.type == FuncType.ENTER) {
                        R.color.buddy_kb_accent_text
                    } else {
                        R.color.buddy_kb_key_text
                    },
                ),
            )
            text = keyLabel(key)
            applyFunctionIcon(this, key)
            contentDescription = when (key) {
                is Key.Char -> "aura_key_char_${key.output}"
                is Key.Func -> "aura_key_${key.type.name.lowercase()}"
            }
            textSize = keyTextSize(key)
            setAllCaps(false)
            includeFontPadding = false
        }
        attachKeyTouch(view, key)
        val rowKeyHeight = keyHeightPx(heightScale)
        val viewHeight = if (
            key is Key.Func &&
            (key.type == FuncType.SHIFT || key.type == FuncType.BACKSPACE)
        ) {
            (rowKeyHeight * 0.78f).toInt().coerceAtLeast(dp(34))
        } else {
            rowKeyHeight
        }
        val lp = LinearLayout.LayoutParams(0, viewHeight, keyWeight(key))
        // One margin for every row. Scaling this down on the shrunken rows made the gap above
        // the bottom row (two shrunken rows meeting) 4dp against 6dp everywhere else, which
        // read as the bottom row being glued to zxcvbnm.
        lp.setMargins(dp(1), dp(3), dp(1), dp(3))
        view.layoutParams = lp

        if (key is Key.Char && key.output.length == 1 && key.output[0].isLetter()) {
            letterKeyViews.add(view to key.output)
        }
        if (key is Key.Func && key.type == FuncType.SHIFT) shiftKeyView = view
        return view
    }

    /**
     * Wire a key's touch behavior. Character keys get [KeyTouchHandler] for long-press alternates,
     * pressed state, and optional shared preview/haptics. Function keys use a plain click with the
     * same preference-controlled feedback.
     */
    @SuppressLint("ClickableViewAccessibility")
    private fun attachKeyTouch(view: TextView, key: Key) {
        when (key) {
            is Key.Char -> view.setOnTouchListener(
                KeyTouchHandler(
                    context = keyboardUiContext,
                    keyView = view,
                    alternates = KeyPopupOptions.alternatesFor(key.output),
                    isShifted = { shiftState.isUpper },
                    onTap = { onKey(key) },
                    onAlternate = { alternate ->
                        playKeySound()
                        currentInputConnection?.let { connection ->
                            traceKeyHandler { commitChar(connection, alternate) }
                        }
                    },
                    keyPreview = keyPreview.takeUnless { fieldProfile.isSecure },
                    previewText = {
                        if (shiftState.isUpper) key.output.uppercase() else key.output
                    },
                    hapticFeedbackEnabled = keyboardSettings.hapticFeedback,
                ),
            )
            is Key.Func -> if (key.type == FuncType.BACKSPACE) {
                // Backspace gets hold-to-repeat (accelerating fast clear) + swipe-left word delete.
                view.setOnTouchListener(
                    BackspaceTouchHandler(
                        backspaceView = view,
                        onDeleteChar = {
                            currentInputConnection?.let { connection ->
                                traceKeyHandler { handleBackspace(connection) }
                            }
                        },
                        onDeleteWord = {
                            traceKeyHandler {
                                finishComposing()
                                currentInputConnection?.let { deletePreviousWord(it) }
                            }
                        },
                        hapticFeedbackEnabled = keyboardSettings.hapticFeedback,
                        onPressFeedback = ::playKeySound,
                    ),
                )
            } else {
                // A plain clickable view shows its pressed-state background on touch-down (the simple
                // highlight) and fires the click on release; no scale, no haptic.
                view.setOnTouchListener { _, event ->
                    if (event.actionMasked == MotionEvent.ACTION_DOWN) {
                        KeyboardPerformanceTrace.markActionDown(
                            KeyboardPerformanceTrace.KEY_KIND_FUNCTION,
                        )
                        if (keyboardSettings.hapticFeedback) {
                            view.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                        }
                    }
                    false
                }
                view.setOnClickListener { onKey(key) }
            }
        }
    }

    /** Delete the word immediately before the cursor, plus any spaces between it and the cursor
     *  (the swipe-left-on-backspace gesture). Reads a bounded window before the cursor. */
    private fun deletePreviousWord(ic: InputConnection) {
        val before = ic.getTextBeforeCursor(64, 0) ?: return
        if (before.isEmpty()) return
        var end = before.length
        while (end > 0 && before[end - 1] == ' ') end-- // trailing spaces
        while (end > 0 && before[end - 1] != ' ') end-- // the word itself
        val deleteCount = before.length - end
        if (deleteCount > 0) {
            ic.deleteSurroundingText(deleteCount, 0)
            advanceCursor(-deleteCount)
        }
    }

    private fun keyLabel(key: Key): String = when (key) {
        is Key.Char -> key.output
        is Key.Func -> if (key.type == FuncType.ENTER) enterKeyLabel else key.label
    }

    private fun applyFunctionIcon(view: TextView, key: Key) {
        val iconRes = when {
            key is Key.Func && key.type == FuncType.SHIFT -> R.drawable.ic_kb_shift
            key is Key.Func && key.type == FuncType.BACKSPACE -> R.drawable.ic_kb_backspace
            key is Key.Func && key.type == FuncType.EMOJI -> R.drawable.ic_kb_emoji
            key is Key.Func && key.type == FuncType.ENTER && enterKeyLabel == "↵" ->
                R.drawable.ic_kb_enter
            else -> return
        }
        setCenteredKeyIcon(
            view,
            iconRes,
            if (key is Key.Func && key.type == FuncType.ENTER) {
                R.color.buddy_kb_accent_text
            } else {
                R.color.buddy_kb_key_text
            },
        )
    }

    private fun setCenteredKeyIcon(view: TextView, iconRes: Int, tintRes: Int) {
        view.text = ""
        view.setCompoundDrawablesRelativeWithIntrinsicBounds(0, 0, 0, 0)
        view.foreground = ContextCompat.getDrawable(keyboardUiContext, iconRes)?.mutate()?.apply {
            setTint(color(tintRes))
        }
        view.foregroundGravity = Gravity.CENTER
    }

    private fun keyBackground(key: Key): Int = when {
        key is Key.Func && key.type == FuncType.ENTER -> R.drawable.buddy_kb_enter_bg
        key is Key.Func && key.type == FuncType.EMOJI -> R.drawable.buddy_kb_key_bg
        key is Key.Func -> R.drawable.buddy_kb_key_special_bg
        else -> R.drawable.buddy_kb_key_bg
    }

    private fun keyTextSize(key: Key): Float = when (key) {
        is Key.Char -> if (key.output.length == 1 && key.output[0].isLetter()) 25f else 21f
        is Key.Func -> when (key.type) {
            FuncType.SPACE -> 14.5f
            FuncType.ENTER -> if (enterKeyLabel.length > 2) 14.5f else 23f
            FuncType.SYMBOLS, FuncType.LETTERS -> 14.5f
            else -> 22f
        }
    }

    private fun keyWeight(key: Key): Float = when (key) {
        is Key.Char -> 1f
        is Key.Func -> when (key.type) {
            FuncType.SPACE -> 4f
            FuncType.SHIFT, FuncType.BACKSPACE, FuncType.SYMBOLS, FuncType.LETTERS, FuncType.ENTER -> 1.5f
            FuncType.GLOBE, FuncType.EMOJI -> 1.2f
        }
    }

    private fun refreshLetterCase() {
        val upper = shiftState.isUpper
        // Relabel the 26 letter keys only when the case actually flipped. After the first
        // (auto-capitalized) letter, shift drops to NONE and stays there, so every following
        // keystroke would otherwise rewrite all 26 TextViews for no visible change.
        if (lastLetterCaseUpper != upper) {
            for ((view, base) in letterKeyViews) {
                view.text = if (upper) base.uppercase() else base
            }
            lastLetterCaseUpper = upper
        }
        // The shift key itself is one view and its glyph depends on SHIFTED vs CAPS_LOCK (both
        // upper), so always refresh it.
        refreshShiftKey()
    }

    /** Reflect the shift state with stable vector artwork and a teal highlight when active. */
    private fun refreshShiftKey() {
        val view = shiftKeyView ?: return
        if (lastShiftKeyMode == shiftState.mode) return
        val active = shiftState.isUpper
        setCenteredKeyIcon(
            view,
            R.drawable.ic_kb_shift,
            if (active) R.color.buddy_kb_accent_text else R.color.buddy_kb_key_text,
        )
        view.setBackgroundResource(
            if (active) R.drawable.buddy_kb_key_active_bg else R.drawable.buddy_kb_key_special_bg,
        )
        view.setTextColor(color(if (active) R.color.buddy_kb_accent_text else R.color.buddy_kb_key_text))
        lastShiftKeyMode = shiftState.mode
    }

    private fun onKey(key: Key) {
        val ic = currentInputConnection ?: return
        playKeySound()
        traceKeyHandler {
            // Any keypress dismisses a pending clipboard paste chip.
            clipboardChip = null
            // Reset double-tap tracking on any non-shift key, so caps lock needs two *consecutive* taps.
            if (key !is Key.Func || key.type != FuncType.SHIFT) lastShiftTapAt = 0L
            when (key) {
                is Key.Char -> commitChar(ic, key.output)
                is Key.Func -> when (key.type) {
                    FuncType.SHIFT -> handleShift(ic)
                    FuncType.BACKSPACE -> handleBackspace(ic)
                    FuncType.SPACE -> handleSpace(ic)
                    FuncType.ENTER -> handleEnter(ic)
                    FuncType.SYMBOLS -> { symbolsPage = true; rebuildKeys() }
                    FuncType.LETTERS -> { symbolsPage = false; rebuildKeys() }
                    FuncType.GLOBE -> showKeyboardPicker()
                    FuncType.EMOJI -> openEmojiPanel()
                }
            }
        }
    }

    private fun playKeySound() {
        if (!keyboardSettings.keypressSound) return
        try {
            audioManager.playSoundEffect(AudioManager.FX_KEY_CLICK)
        } catch (_: Throwable) {
            // Feedback is optional; a muted/unavailable audio service must never block typing.
        }
    }

    private inline fun traceKeyHandler(action: () -> Unit) {
        KeyboardPerformanceTrace.beginKeyHandler()
        try {
            action()
        } finally {
            KeyboardPerformanceTrace.endKeyHandler()
        }
    }

    private fun commitChar(ic: InputConnection, raw: String) {
        val isLetter = raw.length == 1 && raw[0].isLetter()
        val out = if (shiftState.isUpper && isLetter) raw.uppercase() else raw
        // A fresh keypress closes any open autocorrect-undo window.
        pendingUndo = null
        when {
            typingIntelligenceAllowed && isLetter -> {
                // Commit the letter to the field NOW (no composing region, so it lands instantly with
                // no underline) and mirror it in the word buffer for prediction / autocorrect.
                composer.append(out)
                KeyboardPerformanceTrace.beginInputConnectionMutation()
                try {
                    ic.commitText(out, 1)
                } finally {
                    KeyboardPerformanceTrace.endInputConnectionMutation()
                }
                advanceCursor(out.length)
                if (predictionWorkAllowed) updatePredictions()
            }
            typingIntelligenceAllowed -> {
                // A non-letter ends the word: autocorrect + learn it, then commit the separator.
                commitSeparator(ic, out)
            }
            else -> {
                // Non-prediction field (numeric / phone / PIN / password): plain commit, as before.
                KeyboardPerformanceTrace.beginInputConnectionMutation()
                try {
                    ic.commitText(out, 1)
                } finally {
                    KeyboardPerformanceTrace.endInputConnectionMutation()
                }
                advanceCursor(out.length)
            }
        }
        // Consume a one-shot SHIFTED after a letter (CAPS_LOCK persists); recase the keys.
        if (isLetter) {
            shiftState.onTextCommitted()
            refreshLetterCase()
        }
    }

    private fun handleBackspace(ic: InputConnection) {
        // Backspace immediately after an autocorrect reverts it (Gboard parity): bring back the
        // word the user originally typed, without the separator, so they can keep editing it.
        val undo = pendingUndo
        if (undo != null && !composer.isComposing) {
            val tail = undo.corrected + undo.separator
            if (ic.getTextBeforeCursor(tail.length, 0)?.toString() == tail) {
                ic.deleteSurroundingText(tail.length, 0)
                ic.commitText(undo.original, 1)
                advanceCursor(undo.original.length - tail.length)
                pendingUndo = null
                // Re-enter the original as the in-progress word so further typing/backspace edits it
                // and it is not autocorrected again.
                composer.reset()
                composer.append(undo.original)
                manualCorrectionOrigin = undo.original
                pendingCommittedWord = null
                removeLastCommittedHistory(undo.corrected)
                if (learningAllowed) {
                    personalDictionary.record(
                        PersonalizationEvent.AutocorrectUndo(
                            undo.original,
                            undo.corrected,
                            System.currentTimeMillis(),
                        ),
                    )
                }
                updatePredictions()
                return
            }
            pendingUndo = null // guard failed: fall through to a normal backspace
        }
        val selected = ic.getSelectedText(0)
        if (!selected.isNullOrEmpty()) {
            ic.commitText("", 1)
            composer.reset()
            manualCorrectionOrigin = null
            pendingCommittedWord = null
            clearSuggestions()
            markResync()
            return
        }
        // While a word is composing, backspace deletes its last committed char and shortens the
        // buffer; once the buffer empties, further backspaces delete from the field as before.
        if (typingIntelligenceAllowed && composer.deleteLast()) {
            ic.deleteSurroundingText(1, 0)
            advanceCursor(-1)
            if (composer.isComposing) updatePredictions() else clearSuggestions()
            return
        }
        val committed = pendingCommittedWord
        if (committed != null && learningAllowed &&
            System.currentTimeMillis() - committed.committedAtMillis <= COMMIT_EDIT_GUARD_MS &&
            committed.separator.isNotEmpty() &&
            ic.getTextBeforeCursor(committed.separator.length, 0)?.toString() == committed.separator
        ) {
            ic.deleteSurroundingText(committed.separator.length, 0)
            advanceCursor(-committed.separator.length)
            val editableWord = if (committed.autocorrected) committed.rawWord else committed.finalWord
            if (committed.autocorrected && committed.rawWord != committed.finalWord) {
                ic.deleteSurroundingText(committed.finalWord.length, 0)
                ic.commitText(committed.rawWord, 1)
                advanceCursor(committed.rawWord.length - committed.finalWord.length)
            }
            composer.reset()
            composer.append(editableWord)
            manualCorrectionOrigin = editableWord
            pendingCommittedWord = null
            removeLastCommittedHistory(committed.finalWord)
            val feedback = if (committed.autocorrected) {
                PersonalizationEvent.AutocorrectUndo(
                    committed.rawWord,
                    committed.finalWord,
                    System.currentTimeMillis(),
                )
            } else {
                PersonalizationEvent.WordDeleted(committed.finalWord, System.currentTimeMillis())
            }
            personalDictionary.record(feedback)
            updatePredictions()
            return
        }
        ic.deleteSurroundingText(1, 0)
        advanceCursor(-1)
    }

    /**
     * Drop the in-progress word buffer. The letters are already committed to the field, so there is
     * nothing to finalize in the field; this just clears our tracking + strip so a stale word never
     * lingers across a cursor move, field switch, or the AI whiteboard / voice paths. A no-op when
     * nothing is composing (so secure / non-text fields are unaffected).
     */
    private fun finishComposing() {
        if (!composer.isComposing) return
        if (predictionCoordinatorLazy.isInitialized()) predictionCoordinator.invalidate()
        activePredictionGeneration = -1L
        cachedAutocorrect = null
        composer.reset()
        manualCorrectionOrigin = null
        pendingCommittedWord = null
        clearSuggestions()
    }

    /**
     * Finalize the composing word for a separator. This performs no dictionary traversal: it only
     * consumes an exact cached correction from the worker and returns the provenance needed for
     * delayed learning after the separator itself is committed.
     */
    private fun flushComposingWord(): FinalizedWord? {
        if (!composer.isComposing) return null
        val ic = currentInputConnection
        val word = composer.current
        val correctionOrigin = manualCorrectionOrigin
        val cachedState = cachedAutocorrect?.takeIf {
            it.personalizationGeneration == personalDictionary.generation
        }
        val cached = CachedAutocorrectPolicy.consume(
            decision = cachedState?.decision,
            decisionGeneration = cachedState?.generation,
            activeGeneration = activePredictionGeneration,
            rawWord = word,
            manualCorrectionPending = correctionOrigin != null,
        )
        if (predictionCoordinatorLazy.isInitialized()) predictionCoordinator.invalidate()
        activePredictionGeneration = -1L
        cachedAutocorrect = null
        var applied: CachedAutocorrect? = null
        // Separator-time autocorrect only consumes the exact cached worker decision. If it is not
        // ready or belongs to an older token, the user's word remains unchanged.
        var finalWord = word
        if (autocorrectAllowed && cached != null) {
            finalWord = cached.correctedWord
            applied = cached
        }
        // The word is already committed letter-by-letter, so to change it we delete it and re-commit.
        // GUARD: only if the typed word is still intact right before the cursor (this replaces the
        // self-correcting composing region the host used to own). On any desync, skip the correction
        // and leave the user's text untouched.
        if (finalWord != word && ic != null) {
            if (ic.getTextBeforeCursor(word.length, 0)?.toString() == word) {
                ic.deleteSurroundingText(word.length, 0)
                ic.commitText(finalWord, 1)
                advanceCursor(finalWord.length - word.length)
            } else {
                applied = null
                finalWord = word
            }
        }
        composer.reset()
        manualCorrectionOrigin = null
        clearSuggestions()
        lastCommittedWord = finalWord
        return FinalizedWord(
            typedWord = word,
            rawWord = correctionOrigin ?: word,
            finalWord = finalWord,
            autocorrect = applied,
            manualCorrectionOrigin = correctionOrigin,
        )
    }

    /**
     * Finalize the composing word (autocorrecting it) then commit the [separator]. After an
     * autocorrect, surface a one-tap undo; after a plain space, offer next-word suggestions so the
     * strip stays useful instead of going blank.
     */
    private fun commitSeparator(ic: InputConnection, separator: String) {
        val finalized = flushComposingWord()
        KeyboardPerformanceTrace.beginInputConnectionMutation()
        try {
            ic.commitText(separator, 1)
        } finally {
            KeyboardPerformanceTrace.endInputConnectionMutation()
        }
        advanceCursor(separator.length)
        if (finalized != null) recordFinalizedWord(finalized, separator)
        maybeAutoSpaceAfterPunctuation(ic, separator) // T5: ", " / ". " when text follows on the line
        updateAutoCap() // a new sentence after ". " starts capitalized
        if (separator == " ") lastSpaceCommitAt = SystemClock.uptimeMillis()
        when {
            finalized?.autocorrect != null -> showUndoChip(
                finalized.autocorrect.rawWord,
                finalized.autocorrect.correctedWord,
                separator,
            )
            separator == " " -> showNextWordSuggestions()
        }
    }

    /** Space key: convert a double space between words into ". " (T5), otherwise commit a space. */
    private fun handleSpace(ic: InputConnection) {
        if (!composer.isComposing && autocorrectAllowed) {
            val before = ic.getTextBeforeCursor(2, 0)
            val elapsed = SystemClock.uptimeMillis() - lastSpaceCommitAt
            if (DoubleSpacePeriod.shouldConvert(before, elapsed, DOUBLE_SPACE_WINDOW_MS)) {
                ic.deleteSurroundingText(1, 0) // remove the existing trailing space
                ic.commitText(". ", 1)
                advanceCursor(1) // net: -1 space + ". " = +1
                lastSpaceCommitAt = 0L // consume, so a third space doesn't re-trigger
                lastCommittedWord = "" // a new sentence: drop next-word context
                committedWordHistory.clear()
                pendingCommittedWord = null
                clearSuggestions()
                updateAutoCap()
                return
            }
        }
        commitSeparator(ic, " ")
    }

    /** T5: insert a space after a clause / sentence mark when more text follows on the line (the
     *  common case is inserting punctuation back into existing text or after a paste). */
    private fun maybeAutoSpaceAfterPunctuation(ic: InputConnection, separator: String) {
        if (!autocorrectAllowed || separator.length != 1) return
        val nextChar = ic.getTextAfterCursor(1, 0)?.firstOrNull()
        if (PunctuationSpacer.shouldInsertSpace(separator[0], nextChar)) {
            ic.commitText(" ", 1)
            advanceCursor(1)
        }
    }

    /** Fill the strip with likely next words after a space, so it never goes blank. Off-thread
     *  (the predictor's lookup), posted back under the prediction token. */
    private fun showNextWordSuggestions() {
        if (!suggestionsAllowed) return
        val prev = lastCommittedWord
        if (prev.isBlank()) return
        cachedAutocorrect = null
        activePredictionGeneration = submitPrediction(
            PredictionRequest.NextWord(prev, committedWordHistory.toList()),
        )
    }

    /** Set the shift state for the next letter from the text before the cursor: capitalize at a
     *  sentence start, but only in prose fields (never email / url / password / numeric). */
    private fun updateAutoCap() {
        val capitalize = fieldProfile.autocorrectAllowed &&
            SentenceCapitalizer.shouldCapitalize(currentInputConnection?.getTextBeforeCursor(64, 0))
        shiftState.applyAutoCap(capitalize)
        refreshLetterCase()
    }

    /** Shift key: with a selection active, uppercase the selection; otherwise advance the shift
     *  state machine (a double tap within the window latches caps lock). */
    private fun handleShift(ic: InputConnection) {
        val selected = ic.getSelectedText(0)
        if (!selected.isNullOrEmpty()) {
            ic.commitText(selected.toString().uppercase(), 1) // replaces the selection
            markResync() // the replacement length may differ; re-seed from the next update
            return
        }
        val now = SystemClock.uptimeMillis()
        val doubleTap = lastShiftTapAt != 0L && now - lastShiftTapAt <= SHIFT_DOUBLE_TAP_WINDOW_MS
        lastShiftTapAt = now
        shiftState.onShiftTap(doubleTap)
        refreshLetterCase()
    }

    private fun recordFinalizedWord(finalized: FinalizedWord, separator: String) {
        val previous = committedWordHistory.lastOrNull()
        val previousPrevious = committedWordHistory.elementAtOrNull(committedWordHistory.size - 2)
        val now = System.currentTimeMillis()
        if (learningAllowed) {
            val correction = finalized.autocorrect
            if (correction != null) {
                // Generated output is never vocabulary credit. It is only a provenance counter;
                // undo supplies the negative evidence and a later manual edit supplies a label.
                personalDictionary.record(
                    PersonalizationEvent.AutomaticCorrection(
                        correction.rawWord,
                        correction.correctedWord,
                        now,
                    ),
                )
            } else {
                finalized.manualCorrectionOrigin?.takeIf {
                    !it.equals(finalized.finalWord, ignoreCase = true)
                }?.let { origin ->
                    personalDictionary.record(
                        PersonalizationEvent.ManualCorrection(origin, finalized.finalWord, now),
                    )
                }
                personalDictionary.record(
                    PersonalizationEvent.ManualWordCommitted(
                        finalized.finalWord,
                        previous,
                        previousPrevious,
                        now,
                    ),
                )
            }
        }
        pushCommittedHistory(finalized.finalWord)
        pendingCommittedWord = if (learningAllowed) {
            PendingCommittedWord(
                rawWord = finalized.autocorrect?.rawWord ?: finalized.finalWord,
                finalWord = finalized.finalWord,
                separator = separator,
                committedAtMillis = now,
                autocorrected = finalized.autocorrect != null,
            )
        } else {
            null
        }
    }

    private fun pushCommittedHistory(word: String) {
        if (word.isBlank()) return
        while (committedWordHistory.size >= 2) committedWordHistory.removeFirst()
        committedWordHistory.addLast(word)
    }

    private fun removeLastCommittedHistory(word: String) {
        if (committedWordHistory.lastOrNull()?.equals(word, ignoreCase = true) == true) {
            committedWordHistory.removeLast()
        }
        lastCommittedWord = committedWordHistory.lastOrNull().orEmpty()
    }

    private fun handleEnter(ic: InputConnection) {
        val action = (currentInputEditorInfo?.imeOptions ?: 0) and EditorInfo.IME_MASK_ACTION
        if (action == EditorInfo.IME_ACTION_NONE || action == EditorInfo.IME_ACTION_UNSPECIFIED) {
            commitSeparator(ic, "\n")
        } else {
            // Honour the field's action (Send / Search / Next / Go) instead of a newline. Finalize
            // the word first (no undo chip: the field is about to act on the committed text).
            flushComposingWord()?.let { recordFinalizedWord(it, separator = "") }
            sendDefaultEditorAction(true)
            markResync() // the host may clear / submit the field; re-seed from the next update
        }
    }

    private fun showKeyboardPicker() {
        // The robust, version-safe "never get stuck" affordance: let the user pick
        // another keyboard from the system switcher.
        val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as? InputMethodManager
        imm?.showInputMethodPicker()
    }

    private fun openKeyboardSettings() {
        startActivity(
            Intent(this, KeyboardSettingsActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    }

    // --- Collapsed bar (the default state) ---------------------------------------

    /** The field-appropriate primary action shared by the Aura and writing-tools buttons: memory
     *  drafting in text fields, generate-password in password fields, talk-to-Buddy elsewhere. */
    private fun triggerBarAction() {
        when {
            fieldProfile.memoryActionsAllowed -> openWhiteboard()
            fieldProfile.passwordGenerate -> generateAndCommitPassword()
            else -> withVoiceConsent { openVoice() }
        }
    }

    private fun buildCollapsedBar() {
        collapsedBar.removeAllViews()
        suggestionChips.clear()
        suggestionChipVisuals.clear()
        collapsedBar.isClickable = false
        // Match the familiar Gboard toolbar rhythm while preserving Aura's existing actions.
        // The toolbar and suggestions occupy the same fixed-height frame, so prediction updates
        // never resize the IME or move the host app.
        idleToolbar = buildIdleToolbar().apply {
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
            )
        }
        suggestionStrip = buildSuggestionStrip().apply {
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER_VERTICAL,
            )
            visibility = View.GONE
        }
        val content = FrameLayout(keyboardUiContext).apply {
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            )
            addView(idleToolbar)
            addView(suggestionStrip)
        }
        collapsedBar.addView(content)

        // Restore whatever the strip last showed (empty after a field switch -> toolbar shows).
        renderSuggestions(currentSuggestions, renderedSuggestionPersonalizationGeneration)
    }

    private fun buildIdleToolbar(): LinearLayout = LinearLayout(keyboardUiContext).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        addView(makeAuraToolbarButton { triggerBarAction() })
        // GIF has no picker yet. It says so in the bar rather than doing nothing, which reads
        // as broken; the themed slot keeps the toolbar layout stable for the real feature.
        addView(makeToolbarLabel("GIF", "GIF") { showNoticeChip("GIFs are coming soon") })
        addView(makeToolbarIcon(R.drawable.ic_kb_sparkle, "Buddy writing tools") {
            triggerBarAction()
        })
        addView(makeToolbarIcon(R.drawable.ic_kb_settings, "Keyboard settings") {
            openKeyboardSettings()
        })
        addView(makeToolbarIcon(R.drawable.ic_widget_mic, "Talk to Buddy") {
            withVoiceConsent { openVoice() }
        })
    }

    /** The collapsed-bar clipboard affordance: one tap reads the clipboard and offers it as a
     *  paste chip. The read (which surfaces the OS paste notification) happens only on this tap. */
    private fun onClipboardButtonTapped() {
        if (!fieldProfile.predictionsAllowed) return
        val clip = clipboardText().trim()
        if (clip.isEmpty()) {
            Toast.makeText(this, "Clipboard is empty", Toast.LENGTH_SHORT).show()
            return
        }
        clipboardChip = clip
        showClipboardChip(clip)
    }

    /** Show the clipboard text as a single accented paste chip in the strip (truncated preview);
     *  tapping it commits the full text. Dismissed on the next keypress. */
    private fun showClipboardChip(fullText: String) {
        if (!::suggestionStrip.isInitialized || !::idleToolbar.isInitialized) return
        if (!fieldProfile.predictionsAllowed) return
        val preview = fullText.replace('\n', ' ').take(CLIPBOARD_PREVIEW_CHARS)
        val label = "📋 " + preview + if (fullText.length > CLIPBOARD_PREVIEW_CHARS) "…" else ""
        renderedSuggestionPersonalizationGeneration = NO_PERSONALIZATION_GENERATION
        suggestionStripMode = SuggestionStripMode.CLIPBOARD
        idleToolbar.visibility = View.GONE
        suggestionStrip.visibility = View.VISIBLE
        for (i in suggestionChips.indices) {
            if (i == 0) {
                updateSuggestionChip(i, label, View.VISIBLE, accent = true)
            } else {
                updateSuggestionChip(i, "", View.INVISIBLE, accent = false)
            }
        }
    }

    /**
     * The first-use line: one quiet chip in the suggestion strip saying where typing goes.
     *
     * Deliberately NOT a blocking sheet. Someone who just tapped a text field wants to type,
     * and a keyboard that refuses to work until it is read is a keyboard people replace. It
     * offers itself a few times, then stops.
     */
    /** Cheap in-process gate first, so the common case never reads prefs on a field focus. */
    private fun maybeShowIntroChip() {
        if (introBannerRetired) return
        val consent = KeyboardConsentStore.read(this)
        if (consent.localIntroSeen || consent.introPromptsShown >= KeyboardConsentStore.MAX_INTRO_PROMPTS) {
            introBannerRetired = true
            return
        }
        showIntroChip()
    }

    /**
     * A different Aura account is now signed in on this phone.
     *
     * The encrypted personalization snapshot is one device-local file with no account in it,
     * so without this the second person inherits the first person's learned words, n-grams and
     * corrections in their suggestion strip. Clear rather than namespace: it leaves nothing of
     * theirs on disk at all.
     *
     * Network consent goes with it. The person now holding the phone has agreed to nothing.
     * Signing OUT alone clears nothing: it is still your phone and still your data.
     */
    private fun applyAccountBoundary(uid: String) {
        if (uid.isBlank()) return
        val previous = KeyboardConsentStore.lastUid(this)
        KeyboardConsentStore.setLastUid(this, uid)
        if (previous.isBlank() || previous == uid) return
        KeyboardConsentStore.resetNetworkConsent(this)
        KeyboardPersonalizationRepository.dictionary(applicationContext).clearAll { }
    }

    private fun showIntroChip() {
        if (!::suggestionStrip.isInitialized || !::idleToolbar.isInitialized) return
        if (!fieldProfile.predictionsAllowed) return
        renderedSuggestionPersonalizationGeneration = NO_PERSONALIZATION_GENERATION
        suggestionStripMode = SuggestionStripMode.INTRO
        idleToolbar.visibility = View.GONE
        suggestionStrip.visibility = View.VISIBLE
        for (i in suggestionChips.indices) {
            if (i == 0) {
                updateSuggestionChip(i, "Your typing stays on this phone. Tap to see how", View.VISIBLE, accent = false)
            } else {
                // GONE, not INVISIBLE: every chip carries weight=1f, so leaving the other two
                // laid out would squeeze this one into a third of the strip and ellipsize it to
                // nothing. renderSuggestions() sets all three visibilities on the next render,
                // so this is fully recoverable.
                updateSuggestionChip(i, "", View.GONE, accent = false)
            }
        }
        KeyboardConsentStore.recordIntroPromptShown(this)
    }

    /** A one-off message in the suggestion strip, borrowing the intro chip's shape: slot 0
     *  carries the text and the other two go GONE, because every chip carries weight=1f and
     *  leaving them laid out would squeeze this into a third of the strip and ellipsize it to
     *  nothing. Tapping it, or the next keystroke, returns to the toolbar. */
    private fun showNoticeChip(message: String) {
        if (!::suggestionStrip.isInitialized || !::idleToolbar.isInitialized) return
        renderedSuggestionPersonalizationGeneration = NO_PERSONALIZATION_GENERATION
        suggestionStripMode = SuggestionStripMode.NOTICE
        idleToolbar.visibility = View.GONE
        suggestionStrip.visibility = View.VISIBLE
        for (i in suggestionChips.indices) {
            updateSuggestionChip(i, if (i == 0) message else "", if (i == 0) View.VISIBLE else View.GONE, accent = false)
        }
    }

    /**
     * What the first-use line opens: the local story, plus the one choice that actually
     * changes behaviour. Learning stays on unless the user turns it off here or in settings.
     */
    private fun openIntroPanel() {
        finishComposing()
        mode = Mode.WHITEBOARD
        selectedTool = null
        lastAction = null
        wbActions.removeAllViews()
        setActionsVisible(false)
        setPanelTitle(INTRO_PANEL_TITLE)
        hideSubRow()
        setUseThisVisible(false)
        wbContext.text = ""
        showWhiteboardPanel(heightScale = INTRO_PANEL_HEIGHT_SCALE)

        cancelAnimators()
        wbCanvas.removeAllViews()
        wbCanvas.addView(makeConsentTitle("Buddy types with you, on this phone"))
        wbCanvas.addView(
            makeCanvasLine(
                "Every key you press, every word Buddy learns, and every correction it picks " +
                    "up stays encrypted on this device. Text leaves only when you tap an Aura " +
                    "writing action or the mic, and Buddy asks first each time.",
            ),
        )
        wbCanvas.addView(
            LinearLayout(keyboardUiContext).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER
                setPadding(0, dp(6), 0, 0)
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                )
                addView(makeChip("Don't learn my words", accent = false) {
                    KeyboardSettingsStore.setLearnNewWords(this@BuddyImeService, false)
                    finishIntro()
                })
                addView(makeChip("Sounds good", accent = true) { finishIntro() })
            },
        )
    }

    private fun finishIntro() {
        KeyboardConsentStore.markLocalIntroSeen(this)
        introBannerRetired = true
        keyboardSettings = KeyboardSettingsStore.read(this)
        backToKeys()
    }

    private fun pasteClipboardChip() {
        val text = clipboardChip ?: return
        val ic = currentInputConnection ?: return
        finishComposing()
        ic.commitText(text, 1)
        markResync() // variable-length insert: re-seed the cursor from the next update
        clipboardChip = null
        clearSuggestions()
        updateAutoCap()
    }

    /** The three reusable suggestion chips, sharing the center slot equally. Their text is set
     *  per keystroke in [renderSuggestions]; the views themselves are never rebuilt while typing. */
    private fun buildSuggestionStrip(): LinearLayout {
        val strip = LinearLayout(keyboardUiContext).apply { orientation = LinearLayout.HORIZONTAL }
        repeat(SUGGESTION_LIMIT) { index ->
            val chip = makeSuggestionChip(index)
            suggestionChips.add(chip)
            suggestionChipVisuals.add(null)
            strip.addView(chip)
        }
        return strip
    }

    private fun makeSuggestionChip(index: Int): TextView = TextView(keyboardUiContext).apply {
        gravity = Gravity.CENTER
        maxLines = 1
        ellipsize = TextUtils.TruncateAt.END
        setAllCaps(false)
        textSize = 15f
        setTextColor(color(R.color.buddy_kb_key_text))
        setBackgroundResource(R.drawable.buddy_kb_action_bg)
        val padH = dp(8)
        val padV = dp(7)
        setPadding(padH, padV, padH, padV)
        isClickable = true
        setOnClickListener { onSuggestionChipClicked(index) }
        setOnLongClickListener { onSuggestionChipLongPressed(index) }
        layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            .apply { setMargins(dp(3), dp(2), dp(3), dp(2)) }
    }

    private fun onSuggestionChipClicked(index: Int) {
        when (suggestionStripMode) {
            SuggestionStripMode.CLIPBOARD -> if (index == 0) pasteClipboardChip()
            SuggestionStripMode.INTRO -> if (index == 0) openIntroPanel()
            SuggestionStripMode.NOTICE -> if (index == 0) clearSuggestions()
            SuggestionStripMode.UNDO -> if (index == 0) performUndo()
            SuggestionStripMode.SUGGESTIONS -> if (!discardStaleRenderedSuggestions()) {
                currentSuggestions.getOrNull(index)?.let { onSuggestionTapped(it.word) }
            }
            SuggestionStripMode.EMPTY -> Unit
        }
    }

    private fun onSuggestionChipLongPressed(index: Int): Boolean {
        if (suggestionStripMode != SuggestionStripMode.SUGGESTIONS) return false
        if (discardStaleRenderedSuggestions()) return true
        val suggestion = currentSuggestions.getOrNull(index) ?: return false
        onSuggestionLongPressed(suggestion.word)
        return true
    }

    // --- Suggestion strip state --------------------------------------------------

    /**
     * Refresh the suggestion strip for the current word WITHOUT touching the field (the letter is
     * already committed) and WITHOUT blocking the main thread. The heavy prediction (completions +
     * ranking, and the bounded correction pass) runs on the single conflated coordinator. Every
     * new request cancels the pending debounce and active generation, so stale work cannot queue.
     */
    private fun updatePredictions() {
        if (!predictionWorkAllowed) { clearSuggestions(); return }
        val word = composer.current
        if (word.isEmpty()) {
            if (predictionCoordinatorLazy.isInitialized()) predictionCoordinator.invalidate()
            KeyboardPerformanceTrace.invalidateSuggestionRequest()
            activePredictionGeneration = -1L
            cachedAutocorrect = null
            clearSuggestions()
            return
        }
        cachedAutocorrect = null
        activePredictionGeneration = submitPrediction(
            PredictionRequest.CurrentWord(word, autocorrectAllowed),
        )
    }

    private fun submitPrediction(request: PredictionRequest): Long {
        val generation = predictionCoordinator.submit(request)
        KeyboardPerformanceTrace.beginSuggestionRequest(generation)
        return generation
    }

    private fun applyPrediction(
        generation: Long,
        stage: PredictionStage,
        payload: PredictionPayload,
    ) {
        if (generation != activePredictionGeneration || !predictionCoordinator.isCurrent(generation)) return
        if (payload.personalizationGeneration != personalDictionary.generation) return
        when (val request = payload.request) {
            PredictionRequest.Warmup -> Unit
            is PredictionRequest.CurrentWord -> {
                if (composer.current != request.rawWord) return
                KeyboardPerformanceTrace.markSuggestionApplied(generation, stage)
                cachedAutocorrect = payload.autocorrect?.let {
                    CachedAutocorrectState(generation, payload.personalizationGeneration, it)
                }
                if (suggestionsAllowed &&
                    (stage == PredictionStage.LEXICAL || payload.suggestions.isNotEmpty())
                ) {
                    renderSuggestions(payload.suggestions, payload.personalizationGeneration)
                }
            }
            is PredictionRequest.NextWord -> {
                if (composer.isComposing || lastCommittedWord != request.previousWord) return
                KeyboardPerformanceTrace.markSuggestionApplied(generation, stage)
                if (suggestionsAllowed) {
                    renderSuggestions(payload.suggestions, payload.personalizationGeneration)
                }
            }
        }
    }

    /**
     * A learned-state generation is part of every prediction result's validity contract. When a
     * new immutable snapshot is published, cancel work and cached autocorrect from the previous
     * snapshot, then request fresh suggestions for the text the user is currently editing. Special
     * action chips remain visible; changing personalization must never disrupt an undo or paste.
     */
    private fun onPersonalizationGenerationChanged(generation: Long) {
        if (!personalDictionaryLazy.isInitialized() ||
            generation != personalDictionary.generation
        ) {
            return
        }

        if (predictionCoordinatorLazy.isInitialized()) predictionCoordinator.invalidate()
        KeyboardPerformanceTrace.invalidateSuggestionRequest()
        activePredictionGeneration = -1L
        cachedAutocorrect = null

        if (suggestionStripMode == SuggestionStripMode.UNDO ||
            suggestionStripMode == SuggestionStripMode.CLIPBOARD
        ) {
            return
        }
        if (suggestionStripMode == SuggestionStripMode.SUGGESTIONS &&
            renderedSuggestionPersonalizationGeneration != generation
        ) {
            clearSuggestions()
        }
        when {
            composer.isComposing -> updatePredictions()
            suggestionsAllowed && lastCommittedWord.isNotBlank() ->
                showNextWordSuggestions()
        }
    }

    /** Close the small main-thread window between snapshot publication and its posted callback. */
    private fun discardStaleRenderedSuggestions(): Boolean {
        if (!personalDictionaryLazy.isInitialized() ||
            renderedSuggestionPersonalizationGeneration == personalDictionary.generation
        ) {
            return false
        }
        clearSuggestions()
        when {
            composer.isComposing -> updatePredictions()
            suggestionsAllowed && lastCommittedWord.isNotBlank() ->
                showNextWordSuggestions()
        }
        return true
    }

    private fun clearSuggestions() = renderSuggestions(emptyList())

    /** Show [suggestions] in the strip (hiding the toolbar), or fall back to the toolbar when
     *  there is nothing to suggest or the field doesn't allow prediction. */
    private fun renderSuggestions(
        suggestions: List<Suggestion>,
        personalizationGeneration: Long = NO_PERSONALIZATION_GENERATION,
    ) {
        currentSuggestions = suggestions
        renderedSuggestionPersonalizationGeneration = personalizationGeneration
        pendingUndo = null // any normal strip render closes a pending undo window
        if (!::suggestionStrip.isInitialized || !::idleToolbar.isInitialized) return
        val show = suggestions.isNotEmpty() && suggestionsAllowed
        suggestionStripMode = if (show) SuggestionStripMode.SUGGESTIONS else SuggestionStripMode.EMPTY
        if (!show) {
            if (suggestionStrip.visibility != View.GONE) suggestionStrip.visibility = View.GONE
            if (idleToolbar.visibility != View.VISIBLE) idleToolbar.visibility = View.VISIBLE
            return
        }
        if (idleToolbar.visibility != View.GONE) idleToolbar.visibility = View.GONE
        if (suggestionStrip.visibility != View.VISIBLE) suggestionStrip.visibility = View.VISIBLE
        for (i in suggestionChips.indices) {
            val suggestion = suggestions.getOrNull(i)
            if (suggestion == null) {
                // Keep the empty slot laid out (stable widths) but inert.
                updateSuggestionChip(i, "", View.INVISIBLE, accent = false)
            } else {
                // The top correction is the autocorrect target a separator will apply, so accent
                // it (teal) to signal that; ordinary completions stay neutral.
                val isAutocorrectTarget = i == 0 && suggestion.source == SuggestionSource.CORRECTION
                updateSuggestionChip(i, suggestion.word, View.VISIBLE, isAutocorrectTarget)
            }
        }
    }

    private fun updateSuggestionChip(index: Int, text: String, visibility: Int, accent: Boolean) {
        val next = SuggestionChipVisual(text, visibility, accent)
        if (suggestionChipVisuals.getOrNull(index) == next) return
        val chip = suggestionChips[index]
        val previous = suggestionChipVisuals[index]
        if (previous?.accent != accent) styleChip(chip, accent)
        if (previous?.text != text) chip.text = text
        if (previous?.visibility != visibility) chip.visibility = visibility
        suggestionChipVisuals[index] = next
    }

    private fun styleChip(chip: TextView, accent: Boolean) {
        chip.setBackgroundResource(
            if (accent) R.drawable.buddy_kb_chip_bg else R.drawable.buddy_kb_action_bg,
        )
        chip.setTextColor(color(if (accent) R.color.buddy_kb_accent_text else R.color.buddy_kb_key_text))
    }

    /** Tapping a suggestion replaces the in-progress word with it plus a trailing space, and
     *  counts as using that word (a strong learning signal). */
    private fun onSuggestionTapped(word: String) {
        val ic = currentInputConnection ?: return
        if (predictionCoordinatorLazy.isInitialized()) predictionCoordinator.invalidate()
        activePredictionGeneration = -1L
        cachedAutocorrect = null
        // Replace the in-progress partial (a completion / correction) with the chosen word + a
        // space. The partial is already committed text now, so delete it first (guarded). A
        // next-word chip has no partial, so it just inserts the word.
        val partial = composer.current
        if (partial.isNotEmpty() &&
            ic.getTextBeforeCursor(partial.length, 0)?.toString() != partial
        ) {
            // The host moved or edited after the chip rendered. Preserve its text; never delete or
            // insert from a stale local mirror.
            finishComposing()
            markResync()
            return
        }
        val edit = SuggestionCommitPolicy.plan(partial, word)
        KeyboardPerformanceTrace.beginInputConnectionMutation()
        try {
            if (edit.deleteBeforeCursor > 0) {
                ic.deleteSurroundingText(edit.deleteBeforeCursor, 0)
            }
            ic.commitText(edit.committedText, 1)
        } finally {
            KeyboardPerformanceTrace.endInputConnectionMutation()
        }
        advanceCursor(edit.cursorDelta)
        composer.reset()
        clearSuggestions()
        val now = System.currentTimeMillis()
        val previous = committedWordHistory.lastOrNull()
        val previousPrevious = committedWordHistory.elementAtOrNull(committedWordHistory.size - 2)
        if (learningAllowed) {
            personalDictionary.record(
                PersonalizationEvent.SuggestionAccepted(
                    rawWord = partial,
                    acceptedWord = word,
                    previousWord = previous,
                    previousPreviousWord = previousPrevious,
                    atMillis = now,
                ),
            )
        }
        pushCommittedHistory(word)
        pendingCommittedWord = if (learningAllowed) {
            PendingCommittedWord(word, word, " ", now, autocorrected = false)
        } else {
            null
        }
        lastCommittedWord = word
        updateAutoCap()
        showNextWordSuggestions() // chain: offer the word likely to follow this one
    }

    /** Long-pressing a suggestion toggles it in the personal dictionary: a learned word is
     *  forgotten, anything else is pinned as known. One gesture, with a short confirmation. */
    private fun onSuggestionLongPressed(word: String) {
        if (personalDictionary.contains(word)) {
            personalDictionary.remove(word)
            Toast.makeText(this, "Removed “$word” from your words", Toast.LENGTH_SHORT).show()
        } else {
            personalDictionary.add(word)
            Toast.makeText(this, "Added “$word” to your words", Toast.LENGTH_SHORT).show()
        }
        updatePredictions()
    }

    // --- Autocorrect undo --------------------------------------------------------

    /** Show the one-tap undo affordance after an autocorrect: the strip becomes a single accented
     *  chip offering the word the user originally typed. */
    private fun showUndoChip(original: String, corrected: String, separator: String) {
        pendingUndo = PendingUndo(original, corrected, separator)
        if (!::suggestionStrip.isInitialized || !::idleToolbar.isInitialized) return
        if (!fieldProfile.predictionsAllowed) return
        renderedSuggestionPersonalizationGeneration = NO_PERSONALIZATION_GENERATION
        suggestionStripMode = SuggestionStripMode.UNDO
        idleToolbar.visibility = View.GONE
        suggestionStrip.visibility = View.VISIBLE
        for (i in suggestionChips.indices) {
            if (i == 0) {
                updateSuggestionChip(i, "↩ $original", View.VISIBLE, accent = true)
            } else {
                updateSuggestionChip(i, "", View.INVISIBLE, accent = false)
            }
        }
    }

    /** Revert the last autocorrect: replace "corrected<sep>" with "original<sep>", and remember
     *  the original word so it is not corrected again (stop fighting the user). */
    private fun performUndo() {
        val undo = pendingUndo ?: return
        val ic = currentInputConnection ?: return
        val removed = undo.corrected.length + undo.separator.length
        val added = undo.original.length + undo.separator.length
        ic.deleteSurroundingText(removed, 0)
        ic.commitText(undo.original + undo.separator, 1)
        advanceCursor(added - removed)
        pendingUndo = null
        clearSuggestions()
        updateAutoCap()
        removeLastCommittedHistory(undo.corrected)
        val now = System.currentTimeMillis()
        if (learningAllowed) {
            personalDictionary.record(
                PersonalizationEvent.AutocorrectUndo(undo.original, undo.corrected, now),
            )
            val previous = committedWordHistory.lastOrNull()
            val previousPrevious = committedWordHistory.elementAtOrNull(committedWordHistory.size - 2)
            personalDictionary.record(
                PersonalizationEvent.ManualWordCommitted(
                    undo.original,
                    previous,
                    previousPrevious,
                    now,
                ),
            )
        }
        pushCommittedHistory(undo.original)
        pendingCommittedWord = if (learningAllowed) {
            PendingCommittedWord(undo.original, undo.original, undo.separator, now, autocorrected = false)
        } else {
            null
        }
    }

    /** Generate a strong password locally and drop it into the field. Nothing is sent to
     *  the backend; the OS autofill provider offers to save it on submit. */
    private fun generateAndCommitPassword() {
        if (!fieldProfile.passwordGenerate) return
        val ic = currentInputConnection ?: return
        finishComposing()
        ic.commitText(StrongPassword.generate(), 1)
        markResync() // variable-length insert: re-seed the cursor from the next update
        Toast.makeText(this, "Strong password added", Toast.LENGTH_SHORT).show()
        // TODO(analytics): fire EVENT_KEYBOARD_PASSWORD_GENERATED (keyboard_password_generated)
        // once the IME process has an analytics path. The event carries no content.
    }

    // --- Whiteboard (full takeover) ----------------------------------------------

    private fun buildWhiteboard() {
        whiteboard.removeAllViews()

        // Standard app-bar header: a circular back control pinned to the left, with the title
        // centred against the full panel width rather than against the leftover space.
        val header = FrameLayout(keyboardUiContext).apply {
            layoutParams = rowParams(bottom = dp(8))
        }
        wbTitle = TextView(keyboardUiContext).apply {
            text = WHITEBOARD_TITLE
            textSize = 20f
            gravity = Gravity.CENTER
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
            // Reserve the back button's footprint on both sides so a long title stays optically
            // centred and can never slide under the arrow.
            setPadding(dp(56), 0, dp(56), 0)
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(color(R.color.buddy_kb_key_text))
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(44),
            ).apply { gravity = Gravity.CENTER }
        }
        header.addView(wbTitle)
        // A real vector arrow, centred inside the circle. The old "←" glyph was baseline-
        // positioned text, which is what made it sit off-centre.
        header.addView(ImageView(keyboardUiContext).apply {
            setImageResource(R.drawable.ic_kb_arrow_back)
            scaleType = ImageView.ScaleType.CENTER_INSIDE
            setBackgroundResource(R.drawable.buddy_kb_writing_back_bg)
            contentDescription = "Back to keyboard"
            isClickable = true
            isFocusable = true
            setOnClickListener { backToKeys() }
        }, FrameLayout.LayoutParams(dp(44), dp(44)).apply {
            gravity = Gravity.START or Gravity.CENTER_VERTICAL
        })
        whiteboard.addView(header)

        // Kept for state compatibility; the real editor context is shown inside the large card.
        wbContext = TextView(keyboardUiContext).apply {
            visibility = View.GONE
        }
        whiteboard.addView(wbContext)

        // The same centered card shows local editor text, the loading skeleton, and the result.
        wbCanvas = LinearLayout(keyboardUiContext).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(dp(18), dp(8), dp(18), dp(8))
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            )
        }
        wbPreview = ScrollView(keyboardUiContext).apply {
            isVerticalScrollBarEnabled = false
            isFillViewport = true
            setBackgroundResource(R.drawable.buddy_kb_writing_card_bg)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f,
            ).apply { setMargins(dp(2), 0, dp(2), dp(8)) }
            addView(wbCanvas)
        }
        whiteboard.addView(wbPreview)

        // Use-this row: Regenerate (left) + the green "Use this" (right). Shown only when a draft
        // fills the preview box.
        useThisRow = LinearLayout(keyboardUiContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            visibility = View.GONE
            layoutParams = rowParams(top = dp(8), bottom = dp(2))
        }
        useThisRow.addView(makeFooterButton("↻  Regenerate") {
            lastAction?.let { runDraft(it, lastTone, lastLang) }
        })
        useThisRow.addView(makeSpacer())
        useThisRow.addView(makeUseThisButton { previewText?.let { insertDraft(it) } })
        whiteboard.addView(useThisRow)

        // Sub-row (language for Translate); hidden until needed.
        wbSub = LinearLayout(keyboardUiContext).apply { orientation = LinearLayout.HORIZONTAL }
        wbSubRow = HorizontalScrollView(keyboardUiContext).apply {
            isHorizontalScrollBarEnabled = false
            visibility = View.GONE
            layoutParams = rowParams(top = dp(6))
            addView(wbSub)
        }
        whiteboard.addView(wbSubRow)

        // Large icon-first tiles stay below the card and scroll horizontally like the reference.
        wbActions = LinearLayout(keyboardUiContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        wbActionsScroll = HorizontalScrollView(keyboardUiContext).apply {
            isHorizontalScrollBarEnabled = false
            clipToPadding = false
            setPadding(0, 0, dp(8), 0)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(70),
            ).apply { setMargins(0, dp(6), 0, 0) }
            addView(wbActions)
        }
        whiteboard.addView(wbActionsScroll)
    }

    /** The header title for the panel being opened. */
    private fun setPanelTitle(title: String) {
        if (::wbTitle.isInitialized) wbTitle.text = title
    }

    /** Show or hide the action-tile strip. Its height is fixed at 94dp, so a panel with no
     *  tiles must hide it or that height is dead space the card can never use. */
    private fun setActionsVisible(visible: Boolean) {
        if (::wbActionsScroll.isInitialized) {
            wbActionsScroll.visibility = if (visible) View.VISIBLE else View.GONE
        }
    }

    /** The green "Use this" confirm button from the reference: commits the previewed draft. */
    private fun makeUseThisButton(onClick: () -> Unit): TextView = TextView(keyboardUiContext).apply {
        text = "✓  Use this"
        gravity = Gravity.CENTER
        setAllCaps(false)
        textSize = 14f
        setTypeface(typeface, Typeface.BOLD)
        setBackgroundResource(R.drawable.buddy_kb_use_bg)
        setTextColor(color(R.color.buddy_kb_use_text))
        val padH = dp(18)
        val padV = dp(10)
        setPadding(padH, padV, padH, padV)
        isClickable = true
        setOnClickListener { onClick() }
    }

    /** Show or hide the Regenerate + "Use this" row, and forget the preview draft when hiding. */
    private fun setUseThisVisible(visible: Boolean) {
        if (::useThisRow.isInitialized) {
            useThisRow.visibility = if (visible) View.VISIBLE else View.GONE
        }
        if (!visible) previewText = null
    }

    private fun openWhiteboard() {
        // The privacy invariant: memory drafting never opens in a secure or non-text
        // field (password / OTP / numeric / phone / email / url).
        if (!fieldProfile.memoryActionsAllowed) return
        // Commit the in-progress word so the field text is final before Buddy reads it.
        finishComposing()
        mode = Mode.WHITEBOARD
        selectedTool = null
        lastAction = null
        hideSubRow()
        populateWritingTools()
        updateContextPreview()
        renderIdle()
        showWhiteboardPanel()
    }

    /** Talk to Buddy from ANY field: open the takeover in voice-only mode (no draft chips)
     *  and start a live session. Reached from the always-present mic, so it works
     *  regardless of field type or host app. */
    private fun openVoice() {
        // Commit the in-progress word before the takeover reads / sends the field text.
        finishComposing()
        mode = Mode.WHITEBOARD
        selectedTool = null
        lastAction = null
        wbActions.removeAllViews()
        setActionsVisible(false)
        setPanelTitle(VOICE_PANEL_TITLE)
        hideSubRow()
        setUseThisVisible(false)
        wbContext.text = ""
        showWhiteboardPanel()
        startVoiceSession()
    }

    /** Pin a full-takeover panel (emoji / whiteboard) to the live typing keyboard's height, so
     *  opening it never resizes the IME window. Both panels are `match_parent` with a `weight=1`
     *  child, which otherwise expands to fill the whole available area (up to ~3/4 of the screen).
     *  Falls back to the XML `match_parent` if the typing layer hasn't been laid out yet.
     *
     *  [scale] above 1f is the one deliberate exception to "never resizes the IME window":
     *  `buddy_root` is `wrap_content`, so a panel taller than the (INVISIBLE but still measured)
     *  typing stack raises the input view, and the window with it. Only the first-use privacy
     *  panel passes it; it is opened once, never mid-typing, and the window shrinks back when
     *  [backToKeys] sets the panel GONE. Since the height is reassigned on every open, the
     *  default 1f from the other entry points also resets a container the intro panel grew. */
    private fun pinPanelHeight(panel: View, scale: Float = 1f) {
        val measured = typingStack.height
        if (measured <= 0) return
        val ceiling = (resources.displayMetrics.heightPixels * MAX_PANEL_SCREEN_FRACTION).toInt()
        val target = (measured * scale).toInt().coerceIn(measured, maxOf(measured, ceiling))
        val lp = panel.layoutParams
        if (lp.height != target) {
            lp.height = target
            panel.layoutParams = lp
        }
    }

    private fun showWhiteboardPanel(heightScale: Float = 1f) {
        // The typing layer drives the IME height, so flip its visibility SYNCHRONOUSLY (never from
        // an animation end-callback): a late callback from a previous toggle could otherwise land
        // us with typing INVISIBLE and the panel GONE, i.e. a blank "disappeared" keyboard.
        pinPanelHeight(whiteboard, heightScale)
        typingStack.animate().cancel()
        typingStack.visibility = View.INVISIBLE
        typingStack.alpha = 1f
        whiteboard.animate().cancel()
        whiteboard.alpha = 0f
        whiteboard.translationY = dp(12).toFloat()
        whiteboard.visibility = View.VISIBLE
        whiteboard.animate().alpha(1f).translationY(0f).setDuration(180).start()
    }

    private fun backToKeys() {
        cancelAnimators()
        voiceController?.stop()
        teardownVoiceStage()
        finishComposing()
        mode = Mode.TYPING
        typingStack.animate().cancel()
        typingStack.visibility = View.VISIBLE
        typingStack.alpha = 0f
        typingStack.animate().alpha(1f).setDuration(180).start()
        whiteboard.animate().cancel()
        whiteboard.animate().alpha(0f).translationY(dp(12).toFloat()).setDuration(140)
            .withEndAction {
                // Only hide if we're still in typing mode (a fast re-open may have run since).
                if (mode != Mode.WHITEBOARD) {
                    whiteboard.visibility = View.GONE
                    whiteboard.alpha = 1f
                    whiteboard.translationY = 0f
                }
            }.start()
    }

    private fun resetToTyping() {
        cancelAnimators()
        // A fresh field starts with no composing word (the old field's InputConnection is gone)
        // and an empty strip, so the bar shows its action hint until the user types.
        if (predictionCoordinatorLazy.isInitialized()) predictionCoordinator.invalidate()
        activePredictionGeneration = -1L
        cachedAutocorrect = null
        composer.reset()
        // A fresh field: until the next onUpdateSelection (or the initial-selection seed in
        // onStartInputView) we have no trusted cursor position.
        expectedSelStart = -1
        expectedSelEnd = -1
        resyncExpected = true
        currentSuggestions = emptyList()
        renderedSuggestionPersonalizationGeneration = NO_PERSONALIZATION_GENERATION
        lastCommittedWord = ""
        committedWordHistory.clear()
        pendingCommittedWord = null
        manualCorrectionOrigin = null
        clipboardChip = null
        shiftState.reset()
        lastShiftTapAt = 0L
        mode = Mode.TYPING
        selectedTool = null
        lastAction = null
        teardownVoiceStage()
        if (::whiteboard.isInitialized) {
            whiteboard.visibility = View.GONE
            whiteboard.alpha = 1f
            whiteboard.translationY = 0f
        }
        if (::emojiContainer.isInitialized) {
            emojiContainer.visibility = View.GONE
            emojiContainer.alpha = 1f
            emojiContainer.translationY = 0f
        }
        if (::typingStack.isInitialized) {
            typingStack.visibility = View.VISIBLE
            typingStack.alpha = 1f
        }
        if (::wbSubRow.isInitialized) hideSubRow()
    }

    private fun populateWritingTools() {
        setPanelTitle(WHITEBOARD_TITLE)
        setActionsVisible(true)
        wbActions.removeAllViews()
        for (tool in WritingTool.tabs) {
            wbActions.addView(
                makeWritingToolTile(tool.label, selected = tool == selectedTool) {
                    onToolSelected(tool)
                },
            )
        }
    }

    private fun makeWritingToolTile(
        label: String,
        selected: Boolean,
        onClick: () -> Unit,
    ): View = LinearLayout(keyboardUiContext).apply {
        orientation = LinearLayout.VERTICAL
        gravity = Gravity.CENTER
        setBackgroundResource(
            if (selected) {
                R.drawable.buddy_kb_writing_tile_selected_bg
            } else {
                R.drawable.buddy_kb_writing_tile_bg
            },
        )
        layoutParams = LinearLayout.LayoutParams(
            writingToolTileWidth(),
            dp(62),
        ).apply { setMargins(dp(3), 0, dp(3), 0) }
        addView(TextView(keyboardUiContext).apply {
            text = writingToolGlyph(label)
            gravity = Gravity.CENTER
            textSize = 20f
            setTextColor(
                color(if (selected) R.color.buddy_kb_accent_text else R.color.buddy_kb_key_text),
            )
        })
        addView(TextView(keyboardUiContext).apply {
            text = label
            gravity = Gravity.CENTER
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
            textSize = 12f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(
                color(if (selected) R.color.buddy_kb_accent_text else R.color.buddy_kb_key_text),
            )
            setPadding(dp(4), dp(2), dp(4), 0)
        })
        contentDescription = label
        isClickable = true
        isFocusable = true
        setOnClickListener { onClick() }
    }

    /** Tile width derived from the real panel width so exactly [WRITING_TOOLS_VISIBLE] fit
     *  across; the rest of the row is reached by scrolling. A fixed dp width fit ~2.7 tiles and
     *  left the preview card, the thing the user actually reads, squeezed. */
    private fun writingToolTileWidth(): Int {
        // The whiteboard container's 8dp side padding (both edges) plus the action row's own
        // 8dp trailing padding.
        val usable = keyboardUiContext.resources.displayMetrics.widthPixels - dp(8) * 3
        return (usable / WRITING_TOOLS_VISIBLE - dp(3) * 2).coerceAtLeast(dp(64))
    }

    private fun writingToolGlyph(label: String): String = when (label) {
        "Proofread" -> "A✓"
        "Rephrase" -> "≡✎"
        "Professional" -> "▣"
        "Friendly" -> "⌁"
        "Reply as me" -> "↩"
        "Translate" -> "文"
        else -> "✦"
    }

    private fun onToolSelected(tool: WritingTool) {
        selectedTool = tool
        populateWritingTools()
        updateContextPreview()
        if (tool.needsLanguage) {
            showSubRow(langOptions) { lang -> runDraft(tool.action, tone = null, targetLang = lang) }
            renderMessage("Pick a language to translate into", retry = false)
        } else {
            hideSubRow()
            runDraft(tool.action, tone = tool.tone, targetLang = null)
        }
    }

    /**
     * Start (or restart) a live in-process voice session and drive the voice panel. Talk to
     * Buddy IN-PROCESS: a native LiveKit/WebRTC duplex straight from the keyboard to the same
     * tuned voice agent the app uses, so the user never leaves the app they're typing in.
     *
     * The on-screen text rides the data channel as screen_context ONLY for normal text
     * fields; it is never sent from a password / numeric / secure field. Not signed in routes
     * to sign-in; low-RAM devices (or any init failure) fall back to the app's proven voice.
     */
    private fun startVoiceSession() {
        // Finalize any composing word so the screen_context we may send is the real field text.
        finishComposing()
        val includeContext = fieldProfile.memoryActionsAllowed
        val context = if (includeContext) {
            currentInputConnection?.getTextBeforeCursor(2000, 0)?.toString()?.trim().orEmpty().take(2000)
        } else {
            ""
        }
        val app = currentInputEditorInfo?.packageName
        val fieldType = fieldProfile.fieldTypeWire

        if (isLowRamDevice()) {
            handoffToAppVoice(context, fieldType, app)
            return
        }
        val baseUrl = KeyboardCredentialStore.cachedCredential()?.apiBaseUrl ?: DEFAULT_API_BASE_URL
        val screenContext = JSONObject().apply {
            put("type", "screen_context")
            if (context.isNotEmpty()) put("context_before", context)
            if (!fieldType.isNullOrBlank()) put("field_type", fieldType)
            if (!app.isNullOrBlank()) put("app", app)
        }
        val controller = voiceController
            ?: KeyboardVoiceController(applicationContext).also { voiceController = it }
        renderVoice(KeyboardVoiceController.State.CONNECTING, null)
        controller.start(
            baseUrl,
            screenContext,
            onState = { state, detail ->
                if (mode == Mode.WHITEBOARD) {
                    when (state) {
                        // The IME can't request the mic permission (no Activity); hand to the
                        // app, which can prompt and then run the same voice.
                        KeyboardVoiceController.State.NO_MIC -> handoffToAppVoice(context, fieldType, app)
                        // Not signed in: route to sign-in via the app.
                        KeyboardVoiceController.State.NO_CREDENTIAL -> renderSignInPrompt()
                        else -> renderVoice(state, detail)
                    }
                }
            },
            onTranscript = { fromBuddy, text, _, segmentId ->
                if (mode == Mode.WHITEBOARD) onVoiceTranscript(fromBuddy, text, segmentId)
            },
        )
        // TODO(analytics): fire EVENT_KEYBOARD_VOICE_STARTED (keyboard_voice_started)
        // once the IME process has an analytics path. The event carries no content.
    }

    /** Not signed in: the keyboard can't (and for security shouldn't) show a sign-in form itself,
     *  so it shows a tappable button that opens the app, whose router lands an unauthenticated user
     *  on the sign-in screen. Used by both the voice and the draft paths. */
    private fun renderSignInPrompt(message: String = "Sign in to talk to Buddy") {
        cancelAnimators()
        setUseThisVisible(false)
        wbCanvas.removeAllViews()
        wbCanvas.addView(makeCanvasLine(message))
        wbCanvas.addView(makeChip("Sign in to Aura", accent = true) { launchAppForSignIn() })
    }

    // --- Just-in-time disclosure ---------------------------------------------------
    //
    // Ordinary typing, autocorrect, suggestions and the emoji panel never reach this code:
    // they never leave the phone, so there is nothing to disclose and nothing to accept.
    // Only the two paths that transmit are gated, each the first time it would send.

    /** Which transmitting feature a disclosure panel is asking about. */
    private enum class ConsentAsk { AI_TEXT, VOICE }

    /**
     * Open the takeover purely to ask. Used for the voice disclosure, which is reachable from
     * the collapsed bar where no panel is showing yet.
     */
    private fun openConsentPanel(ask: ConsentAsk, onAccept: () -> Unit) {
        finishComposing()
        mode = Mode.WHITEBOARD
        selectedTool = null
        lastAction = null
        wbActions.removeAllViews()
        setActionsVisible(false)
        hideSubRow()
        setUseThisVisible(false)
        wbContext.text = ""
        showWhiteboardPanel()
        renderConsentAsk(ask, action = null, onAccept = onAccept)
    }

    /**
     * The disclosure itself: what leaves this phone, stated concretely, with a real decline.
     *
     * The copy is field- and action-aware because a vague promise is worse than none. Reply
     * reads the clipboard rather than the field, and in a password or OTP field the voice
     * session sends no text at all ([FieldProfile.memoryActionsAllowed] already enforces
     * that), so the panel must not claim otherwise in either direction.
     *
     * Declining is remembered, returns to the keys, and leaves every local feature working.
     * It is not permanent: tapping the action again re-offers this panel.
     */
    private fun renderConsentAsk(ask: ConsentAsk, action: BuddyAction?, onAccept: () -> Unit) {
        cancelAnimators()
        setPanelTitle(CONSENT_PANEL_TITLE)
        setUseThisVisible(false)
        hideSubRow()
        wbCanvas.removeAllViews()

        val title: String
        val body: String
        val acceptLabel: String
        when (ask) {
            ConsentAsk.AI_TEXT -> {
                title = "This one sends your text"
                body = if (action == BuddyAction.REPLY) {
                    "To write a reply, Buddy sends the message you copied to Aura's servers. " +
                        "Your typing stays on this phone."
                } else {
                    "To do this, Buddy sends what you've typed in this field (up to 2000 " +
                        "characters) to Aura's servers. Your typing stays on this phone."
                }
                acceptLabel = "Send it"
            }
            ConsentAsk.VOICE -> {
                title = "This starts a live conversation"
                body = if (fieldProfile.memoryActionsAllowed) {
                    "This isn't dictation or read-aloud: Buddy talks back, in real time. Your " +
                        "microphone streams to Aura while the mic is on, along with the text in " +
                        "this field so Buddy knows what you're working on."
                } else {
                    "This isn't dictation or read-aloud: Buddy talks back, in real time. Your " +
                        "microphone streams to Aura while the mic is on. This field is private, " +
                        "so nothing you've typed here is sent."
                }
                acceptLabel = "Start talking"
            }
        }

        wbCanvas.addView(makeConsentTitle(title))
        wbCanvas.addView(makeCanvasLine(body))
        wbCanvas.addView(makeConsentButtons(acceptLabel, ask, onAccept))
    }

    private fun makeConsentTitle(text: String): TextView = TextView(keyboardUiContext).apply {
        this.text = text
        textSize = 16f
        gravity = Gravity.CENTER
        setTypeface(typeface, Typeface.BOLD)
        setTextColor(color(R.color.buddy_kb_key_text))
        setPadding(dp(12), dp(2), dp(12), dp(2))
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )
    }

    /** Decline and accept side by side, with decline first so it is never the accidental tap. */
    private fun makeConsentButtons(
        acceptLabel: String,
        ask: ConsentAsk,
        onAccept: () -> Unit,
    ): View = LinearLayout(keyboardUiContext).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER
        setPadding(0, dp(6), 0, 0)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )
        addView(makeChip("Not now", accent = false) {
            recordConsent(ask, granted = false)
            backToKeys()
        })
        addView(makeChip(acceptLabel, accent = true) {
            recordConsent(ask, granted = true)
            onAccept()
        })
    }

    private fun recordConsent(ask: ConsentAsk, granted: Boolean) {
        when (ask) {
            ConsentAsk.AI_TEXT -> KeyboardConsentStore.setAiTextConsent(this, granted)
            ConsentAsk.VOICE -> KeyboardConsentStore.setVoiceConsent(this, granted)
        }
    }

    /**
     * Run [start] only once the user has agreed to the microphone disclosure, otherwise show
     * it. Deliberately wraps the CALL SITES rather than living inside the voice methods, so the
     * hard voice boundary those methods define stays as small as it already is.
     */
    private fun withVoiceConsent(start: () -> Unit) {
        if (KeyboardConsentStore.voiceGranted(this)) start() else openConsentPanel(ConsentAsk.VOICE, start)
    }

    private fun launchAppForSignIn() {
        val launch = packageManager.getLaunchIntentForPackage(packageName)
            ?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        try {
            if (launch != null) startActivity(launch) else launchAppVoice()
        } catch (_: Throwable) {
            renderMessage("Open Aura and sign in to talk to Buddy", retry = false)
        }
    }

    /** The voice-from-keyboard fallback: stash the on-screen text and open the app's voice
     *  via aura://voice (used on low-RAM devices and when the IME lacks the mic grant). */
    private fun handoffToAppVoice(context: String, fieldType: String?, app: String?) {
        KeyboardVoiceHandoff.write(this, context, fieldType, app)
        if (launchAppVoice()) {
            renderMessage("Opening Buddy voice…", retry = false)
        } else {
            renderMessage("Open Aura to talk to Buddy", retry = false)
        }
    }

    /** One caption in the voice lyric stack: the rendered line plus who said it (so it can be
     *  recoloured as it advances). */
    private class CaptionLine(val view: TextView, var fromBuddy: Boolean)

    /** Drive the in-keyboard voice panel. While the session is live the panel shows a live
     *  caption stream (Spotify-lyrics style) plus a waveform meter pinned mid-right; the meter's
     *  energy reads the state, so no static "Listening…" line is needed. Ended / error states
     *  tear the stage down and show one bounded, action-pointing message. */
    private fun renderVoice(state: KeyboardVoiceController.State, detail: String?) {
        val live = state == KeyboardVoiceController.State.CONNECTING ||
            state == KeyboardVoiceController.State.LISTENING ||
            state == KeyboardVoiceController.State.SPEAKING
        if (live) {
            ensureVoiceStage()
            voiceWaveform?.setEnergy(
                when (state) {
                    KeyboardVoiceController.State.SPEAKING -> VoiceWaveformView.Energy.SPEAKING
                    KeyboardVoiceController.State.LISTENING -> VoiceWaveformView.Energy.LISTENING
                    else -> VoiceWaveformView.Energy.IDLE // CONNECTING
                },
            )
            // A faint hint only until real speech fills the lyrics; then it gets out of the way.
            voiceStatusLine?.let { line ->
                val hint = when {
                    voiceCaptions.isNotEmpty() -> ""
                    state == KeyboardVoiceController.State.CONNECTING -> "Connecting to Buddy…"
                    else -> "Listening… just start talking"
                }
                line.text = hint
                line.visibility = if (hint.isEmpty()) View.GONE else View.VISIBLE
            }
            return
        }
        // Ended / error: drop the stage and show one bounded message.
        teardownVoiceStage()
        cancelAnimators()
        setUseThisVisible(false)
        wbCanvas.removeAllViews()
        val title: String
        val sub: String
        val restartLabel: String
        if (state == KeyboardVoiceController.State.ENDED) {
            title = "Voice ended"
            sub = "Start another session whenever you're ready."
            restartLabel = "Start again"
        } else {
            title = if (detail == "no_agent") "Buddy didn't pick up" else "Voice hit a snag"
            sub = "Give it another go, or open Aura."
            restartLabel = "Try again"
        }
        wbCanvas.addView(makeCanvasLine(title))
        wbCanvas.addView(makeCanvasLine(sub))
        // The old copy said "tap the mic", but this panel covers the collapsed bar the mic lives
        // on, so there was no mic on screen to tap. Give the state a real control.
        wbCanvas.addView(
            makeChip(restartLabel, accent = true) { startVoiceSession() }.apply {
                setCompoundDrawablesRelativeWithIntrinsicBounds(R.drawable.ic_widget_mic, 0, 0, 0)
                compoundDrawablePadding = dp(8)
                TextViewCompat.setCompoundDrawableTintList(
                    this,
                    ColorStateList.valueOf(color(R.color.buddy_kb_accent_text)),
                )
            },
        )
    }

    /** Build the live voice stage once: a bottom-anchored caption column with a waveform meter
     *  pinned to the vertical centre of the right edge, and a Stop control beneath. Rebuilt only
     *  when a session (re)starts; updated in place afterward so animations survive the turn. */
    private fun ensureVoiceStage() {
        val stage = voiceStage
        if (stage != null && stage.parent === wbCanvas) return
        buildVoiceStage()
    }

    private fun buildVoiceStage() {
        cancelAnimators()
        setUseThisVisible(false)
        voiceCaptions.clear()
        wbCanvas.removeAllViews()

        // Centred caption column, anchored to the bottom. Right padding keeps the lyrics clear of
        // the waveform + Stop rail on the right edge.
        val captions = LinearLayout(keyboardUiContext).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
            setPadding(dp(12), dp(8), dp(58), dp(8))
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM,
            )
        }
        voiceCaptionStack = captions

        val status = TextView(keyboardUiContext).apply {
            textSize = 13f
            gravity = Gravity.CENTER
            setTextColor(color(R.color.buddy_kb_text_muted))
            text = "Connecting to Buddy…"
            val p = dp(4)
            setPadding(p, dp(6), p, dp(6))
        }
        voiceStatusLine = status
        captions.addView(status)

        // The waveform meter (teal, the brand accent) with the Stop button directly beneath it,
        // the whole rail pinned to the vertical centre of the right edge.
        val meter = VoiceWaveformView(keyboardUiContext).apply {
            setBarColor(color(R.color.buddy_kb_accent))
            setEnergy(VoiceWaveformView.Energy.IDLE)
            layoutParams = LinearLayout.LayoutParams(dp(32), dp(40)).apply {
                gravity = Gravity.CENTER_HORIZONTAL
            }
        }
        voiceWaveform = meter

        val rail = LinearLayout(keyboardUiContext).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            addView(meter)
            addView(makeStopButton())
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER_VERTICAL or Gravity.END,
            ).apply { rightMargin = dp(10) }
        }

        val stage = FrameLayout(keyboardUiContext).apply {
            minimumHeight = dp(168)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            )
            addView(captions)
            addView(rail)
        }
        voiceStage = stage
        wbCanvas.addView(stage)
    }

    /** The compact Stop control that sits under the waveform on the right rail: a small teal
     *  pill with a stop glyph. */
    private fun makeStopButton(): View = ImageView(keyboardUiContext).apply {
        // A vector centred by layout, not a "■" text glyph centred by font metrics -- the glyph
        // sat visibly off-centre in the pill.
        setImageResource(R.drawable.ic_kb_stop)
        scaleType = ImageView.ScaleType.CENTER_INSIDE
        setBackgroundResource(R.drawable.buddy_kb_chip_bg)
        contentDescription = "Stop talking"
        layoutParams = LinearLayout.LayoutParams(dp(44), dp(36)).apply { topMargin = dp(8) }
        isClickable = true
        isFocusable = true
        setOnClickListener { stopVoice() }
    }

    /** Stop the waveform and forget the live panel views (called when voice ends or the panel
     *  closes). The views themselves are removed by the caller's [wbCanvas] rebuild. */
    private fun teardownVoiceStage() {
        voiceWaveform?.release()
        voiceWaveform = null
        voiceCaptionStack = null
        voiceStatusLine = null
        voiceStage = null
        voiceCaptions.clear()
    }

    /** Fold a transcript segment into the lyric stack. Interim updates of the same segment id
     *  update the active line in place; a new segment id slides a fresh line in and demotes the
     *  rest, capping history to [MAX_VOICE_CAPTION_LINES]. */
    private fun onVoiceTranscript(fromBuddy: Boolean, text: String, segmentId: String) {
        val stack = voiceCaptionStack ?: return
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return
        voiceStatusLine?.visibility = View.GONE

        val existing = voiceCaptions[segmentId]
        if (existing != null) {
            existing.fromBuddy = fromBuddy
            existing.view.text = trimmed
            return
        }

        val line = CaptionLine(makeCaptionLine(trimmed), fromBuddy)
        voiceCaptions[segmentId] = line
        stack.addView(line.view)
        // Spotify-style entrance: the new active line rises into place (alpha is owned by restyle).
        line.view.alpha = 0f
        line.view.translationY = dp(10).toFloat()
        line.view.animate().translationY(0f).setDuration(220).start()

        while (voiceCaptions.size > MAX_VOICE_CAPTION_LINES) {
            val oldestId = voiceCaptions.keys.first()
            val oldest = voiceCaptions.remove(oldestId)
            if (oldest != null) {
                val view = oldest.view
                view.animate().alpha(0f).translationY(-dp(8).toFloat()).setDuration(180)
                    .withEndAction { stack.removeView(view) }.start()
            }
        }
        restyleCaptionLines()
    }

    /** Style each caption by its depth from the active (newest) line: the active line is large
     *  and bright, older lines progressively smaller and dimmer, fading upward like lyrics.
     *  Buddy speaks in teal, the user in charcoal. */
    private fun restyleCaptionLines() {
        val items = voiceCaptions.values.toList()
        val lastIndex = items.size - 1
        items.forEachIndexed { index, line ->
            val depth = lastIndex - index
            val view = line.view
            val targetAlpha: Float
            when (depth) {
                0 -> {
                    view.textSize = 19f
                    view.setTypeface(null, Typeface.BOLD)
                    view.maxLines = 3
                    targetAlpha = 1f
                }
                1 -> {
                    view.textSize = 16f
                    view.setTypeface(null, Typeface.NORMAL)
                    view.maxLines = 2
                    targetAlpha = 0.55f
                }
                2 -> {
                    view.textSize = 14f
                    view.setTypeface(null, Typeface.NORMAL)
                    view.maxLines = 1
                    targetAlpha = 0.30f
                }
                else -> {
                    view.textSize = 13f
                    view.setTypeface(null, Typeface.NORMAL)
                    view.maxLines = 1
                    targetAlpha = 0.16f
                }
            }
            view.ellipsize = TextUtils.TruncateAt.END
            // Buddy in the teal brand accent, the user in charcoal: both readable on the cream
            // card (buddy_kb_accent_text is white, which was invisible here).
            view.setTextColor(
                color(if (line.fromBuddy) R.color.buddy_kb_accent else R.color.buddy_kb_key_text),
            )
            view.animate().alpha(targetAlpha).setDuration(180).start()
        }
    }

    /** A single caption line in the voice lyric stack. Styling (size / alpha / colour) is set
     *  by [restyleCaptionLines] from its position; this just establishes the box. */
    private fun makeCaptionLine(text: String): TextView = TextView(keyboardUiContext).apply {
        this.text = text
        setAllCaps(false)
        gravity = Gravity.CENTER
        setTextColor(color(R.color.buddy_kb_key_text))
        textSize = 19f
        ellipsize = TextUtils.TruncateAt.END
        val padV = dp(5)
        setPadding(dp(4), padV, dp(4), padV)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
        )
    }

    private fun stopVoice() {
        voiceController?.stop()
        if (mode == Mode.WHITEBOARD) renderVoice(KeyboardVoiceController.State.ENDED, null)
    }

    private fun isLowRamDevice(): Boolean {
        val am = getSystemService(Context.ACTIVITY_SERVICE) as? android.app.ActivityManager
        return am?.isLowRamDevice == true
    }

    /** Open the app's voice via the aura://voice deep link. Returns false if no activity
     *  can handle it (then the user is told to open Aura). */
    private fun launchAppVoice(): Boolean = try {
        startActivity(
            Intent(Intent.ACTION_VIEW, Uri.parse("aura://voice"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        true
    } catch (t: Throwable) {
        false
    }

    private fun runDraft(action: BuddyAction, tone: String?, targetLang: String?) {
        // The single choke point for text leaving this device: tool taps, the Translate
        // language sub-row, Regenerate and the "Try again" chip all arrive here. Nothing is
        // read or sent until the user has seen the disclosure and accepted it. Re-entrant on
        // accept, with the same arguments, so the tap they made still happens.
        if (!KeyboardConsentStore.aiTextGranted(this)) {
            renderConsentAsk(ConsentAsk.AI_TEXT, action) { runDraft(action, tone, targetLang) }
            return
        }
        // Commit the in-progress word so getTextBeforeCursor below sees the final field text.
        finishComposing()
        lastAction = action
        lastTone = tone
        lastLang = targetLang
        // Read the local context only at the moment of the action (privacy contract: nothing
        // leaves the keyboard except on an explicit tap). Reply answers a copied message; every
        // other action works on the field.
        val target = try {
            buildDraftTarget(action)
        } catch (e: SelectionTooLarge) {
            renderMessage("That's a lot of text at once. Select a smaller piece.", retry = false)
            return
        }
        draftTarget = target
        val sourceText = sourceTextFor(action, target)
        if (sourceText.isEmpty()) {
            renderMessage(
                if (action == BuddyAction.REPLY) "Copy the message you got, then tap Reply as me"
                else "Type something first, then pick an action",
                retry = false,
            )
            return
        }
        val hostApp = currentInputEditorInfo?.packageName
        // base URL rides the app-bridged credential (so a --dart-define candidate reaches the
        // keyboard); falls back to prod when nothing has been bridged yet.
        val baseUrl = KeyboardCredentialStore.cachedCredential()?.apiBaseUrl ?: DEFAULT_API_BASE_URL
        renderThinking()
        // Mint a FRESH Firebase ID token in the keyboard's own process rather than reusing the
        // app-bridged token, which goes stale when the app hasn't run in the last hour (the
        // keyboard's normal state). Both callbacks land on the main thread.
        KeyboardAuth.freshIdToken { token ->
            if (mode != Mode.WHITEBOARD) return@freshIdToken
            val credential = token?.takeIf { it.isNotBlank() }?.let {
                KeyboardCredentialStore.Credential(idToken = it, uid = "", apiBaseUrl = baseUrl)
            }
            KeyboardDraftClient.draft(
                credential = credential,
                action = action.wire,
                // Reply has no on-screen span, so its clipboard source rides context_before as
                // before; everything else sends the span the keyboard will actually replace.
                contextBefore = if (target == null) sourceText else target.contextBefore,
                hostApp = hostApp,
                tone = tone,
                targetLang = targetLang,
                fieldType = fieldProfile.fieldTypeWire,
                selectedText = if (target == null) "" else sourceText,
                contextAfter = target?.contextAfter.orEmpty(),
            ) { result ->
                // The user may have closed the panel before the draft returned.
                if (mode != Mode.WHITEBOARD) return@draft
                when (result) {
                    is KeyboardDraftClient.Result.Success ->
                        if (result.suggestions.isEmpty()) {
                            renderMessage(reasonCopy(result.reason), retry = result.reason != "empty_context")
                        } else {
                            renderPreview(result.suggestions)
                        }
                    is KeyboardDraftClient.Result.Failure ->
                        renderMessage(reasonCopy(result.reason), retry = true)
                    KeyboardDraftClient.Result.NoCredential ->
                        renderSignInPrompt("Sign in to draft in your voice")
                }
            }
        }
    }

    /**
     * The span of the user's text a writing tool transforms, and therefore the exact span
     * [insertDraft] is allowed to delete. Sending one range and deleting another destroys text in
     * somebody else's app, so these two always come from the same object.
     *
     * [hostSelection] means the host already has the span selected, so `commitText` replaces it
     * natively and no delete is needed. Otherwise the span straddles the cursor: [beforeText] is
     * behind it, [afterText] ahead of it. [contextBefore] / [contextAfter] are sent so the model
     * keeps the register of the surrounding writing, and are never replaced.
     */
    private data class DraftTarget(
        val beforeText: String,
        val afterText: String,
        val hostSelection: Boolean,
        val contextBefore: String,
        val contextAfter: String,
        val inputSession: Long,
    ) {
        val chunk: String get() = beforeText + afterText
    }

    /** Raised when the user's own selection is larger than the model will accept, so the honest
     *  answer is to ask for less rather than transform a prefix and delete the whole thing. */
    private class SelectionTooLarge : Exception()

    /**
     * Resolve what this action works on, in priority order:
     *  1. Reply -> null. Its source is the clipboard, not the field: there is nothing on screen to
     *     replace, so it appends at the cursor as it always has.
     *  2. An explicit host selection wins. The user has stated the blast radius; do not second-
     *     guess it. Over [DRAFT_MAX_CHARS] we refuse rather than silently transform a prefix.
     *  3. Nothing selected and the whole field fits -> the whole field.
     *  4. Otherwise a boundary-snapped window around the cursor, with the rest sent as context.
     */
    private fun buildDraftTarget(action: BuddyAction): DraftTarget? {
        if (action == BuddyAction.REPLY) return null
        val ic = currentInputConnection ?: return null

        val selected = ic.getSelectedText(0)?.toString().orEmpty()
        if (selected.isNotEmpty()) {
            if (selected.length > DRAFT_MAX_CHARS) throw SelectionTooLarge()
            return DraftTarget(
                beforeText = selected,
                afterText = "",
                hostSelection = true,
                contextBefore = ic.getTextBeforeCursor(DRAFT_CONTEXT_BEFORE_MAX, 0)
                    ?.toString().orEmpty(),
                contextAfter = ic.getTextAfterCursor(DRAFT_CONTEXT_AFTER_MAX, 0)
                    ?.toString().orEmpty(),
                inputSession = inputSession,
            )
        }

        val before = ic.getTextBeforeCursor(DRAFT_READ_CHARS, 0)?.toString().orEmpty()
        val after = ic.getTextAfterCursor(DRAFT_READ_CHARS, 0)?.toString().orEmpty()
        if (before.isEmpty() && after.isEmpty()) return null

        // A read that came back at exactly the requested length may have been cut off by the host
        // (getTextBeforeCursor is documented as best-effort), so its outer edge is not a real
        // boundary and must not be used as the start of the span.
        val beforeTruncated = before.length >= DRAFT_READ_CHARS
        val afterTruncated = after.length >= DRAFT_READ_CHARS

        if (!beforeTruncated && !afterTruncated &&
            before.length + after.length <= DRAFT_AUTO_WINDOW_MAX
        ) {
            return DraftTarget(
                beforeText = before,
                afterText = after,
                hostSelection = false,
                contextBefore = "",
                contextAfter = "",
                inputSession = inputSession,
            )
        }

        // Split the window budget across the cursor, then snap each edge outward to a real
        // boundary so neither end lands mid-sentence or mid-word.
        val halfBudget = DRAFT_AUTO_WINDOW_MAX / 2
        val beforeBudget = minOf(before.length, maxOf(halfBudget, DRAFT_AUTO_WINDOW_MAX - after.length))
        val afterBudget = minOf(after.length, DRAFT_AUTO_WINDOW_MAX - beforeBudget)
        val beforeStart = snapWindowStart(before, before.length - beforeBudget, beforeTruncated)
        val afterEnd = snapWindowEnd(after, afterBudget, afterTruncated)

        return DraftTarget(
            beforeText = before.substring(beforeStart),
            afterText = after.substring(0, afterEnd),
            hostSelection = false,
            contextBefore = before.substring(0, beforeStart).takeLast(DRAFT_CONTEXT_BEFORE_MAX),
            contextAfter = after.substring(afterEnd).take(DRAFT_CONTEXT_AFTER_MAX),
            inputSession = inputSession,
        )
    }

    /**
     * Move [from] forward to the first real boundary in [text] -- a paragraph break, else the end
     * of a sentence, else a word gap -- so the span starts where a human would start reading.
     * When the read was [truncated] the text's own index 0 is mid-sentence in writing we cannot
     * see, so it is never accepted as a boundary and we fall forward to the first word gap.
     */
    private fun snapWindowStart(text: String, from: Int, truncated: Boolean): Int {
        if (from <= 0) return if (truncated) firstWordStart(text) else 0
        val paragraph = text.indexOf('\n', from)
        if (paragraph in from until text.length) return paragraph + 1
        var i = from
        while (i < text.length - 1) {
            if (text[i] in ".!?" && text[i + 1].isWhitespace()) return skipSpaces(text, i + 1)
            i++
        }
        return firstWordStart(text.substring(from)).let { from + it }
    }

    /** Move [to] forward to the next boundary so the span ends on a finished sentence or word. */
    private fun snapWindowEnd(text: String, to: Int, truncated: Boolean): Int {
        if (to >= text.length) return text.length
        var i = to
        while (i < text.length) {
            if (text[i] == '\n') return i
            if (text[i] in ".!?" && (i + 1 >= text.length || text[i + 1].isWhitespace())) return i + 1
            i++
        }
        // No sentence end ahead: stop on the last word gap rather than mid-word, unless the read
        // ran to the host's limit, in which case the tail is not a real end of text.
        if (!truncated) return text.length
        val gap = text.lastIndexOf(' ', to)
        return if (gap > 0) gap else to
    }

    private fun firstWordStart(text: String): Int {
        val gap = text.indexOfFirst { it.isWhitespace() }
        return if (gap < 0) 0 else skipSpaces(text, gap)
    }

    private fun skipSpaces(text: String, from: Int): Int {
        var i = from
        while (i < text.length && text[i].isWhitespace()) i++
        return i
    }

    /** The text the action operates on. Reply answers a copied message (it lives in another
     *  app's chat bubble, which an IME cannot read), so it reads the clipboard; every other
     *  action works on the resolved span. */
    private fun sourceTextFor(action: BuddyAction, target: DraftTarget?): String = when (action) {
        // Reply works off the copied message. Skip it when the clipboard looks like a secret (an
        // OTP or a generated password/token), so a credential the user copied for some other app is
        // never uploaded as draft context. An empty result just prompts them to copy a message.
        BuddyAction.REPLY -> clipboardText().let { if (looksLikeSecret(it)) "" else it }
        else -> target?.chunk.orEmpty()
    }.trim().take(DRAFT_MAX_CHARS)

    /** A best-effort guard so a copied credential is never sent as REPLY context. A real message to
     *  reply to is prose (it has whitespace); a bare no-whitespace token that is a short all-digit
     *  code (OTP/PIN) or a long letters+digits string (a generated password/token) is treated as a
     *  secret and skipped. Conservative: anything with spaces is always allowed through. */
    private fun looksLikeSecret(text: String): Boolean {
        val token = text.trim()
        if (token.isEmpty() || token.any { it.isWhitespace() }) return false
        if (token.length in 4..10 && token.all { it.isDigit() }) return true
        return token.length in 12..128 && token.any { it.isDigit() } && token.any { it.isLetter() }
    }

    private fun clipboardText(): String {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return ""
        val clip = cm.primaryClip ?: return ""
        if (clip.itemCount == 0) return ""
        return clip.getItemAt(0)?.coerceToText(this)?.toString().orEmpty()
    }

    /**
     * Put the draft into the field, REPLACING the span it was built from.
     *
     * The span is re-read and compared before a single character is deleted. If the field moved
     * under us -- the user typed while the request was in flight, the host edited it, focus
     * changed -- nothing is deleted and nothing is inserted, because appending on a mismatch is
     * exactly the duplicate-text bug this path exists to fix. A null target (Reply) appends by
     * design: its source is the clipboard, so there is nothing on screen to replace.
     */
    private fun insertDraft(text: String) {
        finishComposing()
        val ic = currentInputConnection ?: return
        val target = draftTarget
        if (target != null && target.inputSession != inputSession) {
            renderMessage("That was a different field, so nothing was replaced.", retry = false)
            return
        }
        // One batch, so the host sees a single edit instead of a delete then an insert.
        ic.beginBatchEdit()
        try {
            when {
                target == null -> ic.commitText(text, 1)
                target.hostSelection -> {
                    if (ic.getSelectedText(0)?.toString() != target.beforeText) {
                        return renderReplaceAborted()
                    }
                    ic.commitText(text, 1) // a live selection is replaced by the commit itself
                }
                else -> {
                    // Never ask for zero characters: a host is free to answer null to that, which
                    // would read as "the text changed" for the commonest case of all, a cursor
                    // sitting at the very end of what the user typed.
                    if (!spanStillIntact(ic, target)) return renderReplaceAborted()
                    // Code points, not UTF-16 units: deleteSurroundingText would split a surrogate
                    // pair sitting on either edge of the span and leave a replacement glyph.
                    ic.deleteSurroundingTextInCodePoints(
                        target.beforeText.codePointCount(0, target.beforeText.length),
                        target.afterText.codePointCount(0, target.afterText.length),
                    )
                    ic.commitText(text, 1)
                }
            }
        } finally {
            ic.endBatchEdit()
        }
        draftTarget = null
        markResync() // variable-length edit: re-seed the cursor from the next update
        backToKeys()
    }

    /** Is the field still holding exactly the span the draft was built from? An empty side is
     *  skipped rather than queried, because a zero-length read is allowed to come back null and
     *  would otherwise look like an edit. */
    private fun spanStillIntact(ic: InputConnection, target: DraftTarget): Boolean {
        if (target.beforeText.isNotEmpty() &&
            ic.getTextBeforeCursor(target.beforeText.length, 0)?.toString() != target.beforeText
        ) {
            return false
        }
        if (target.afterText.isNotEmpty() &&
            ic.getTextAfterCursor(target.afterText.length, 0)?.toString() != target.afterText
        ) {
            return false
        }
        return true
    }

    /** The field changed between sending the draft and applying it. Touch nothing and say so:
     *  a silent append would put the old text and the new text in the field together. */
    private fun renderReplaceAborted() {
        renderMessage("Your text changed, so nothing was replaced.", retry = true)
    }

    // --- Whiteboard canvas states ------------------------------------------------

    private fun renderIdle() {
        cancelAnimators()
        setUseThisVisible(false)
        wbCanvas.removeAllViews()
        // Preview the span a tool would actually act on, so the card shows the blast radius
        // rather than an arbitrary 2000 characters behind the cursor.
        val editorText = try {
            buildDraftTarget(BuddyAction.GRAMMAR)?.chunk?.trim().orEmpty()
        } catch (e: SelectionTooLarge) {
            ""
        }
        wbCanvas.addView(
            makePreviewText(
                editorText.ifEmpty { "Type something, then choose a writing tool." },
                muted = editorText.isEmpty(),
            ),
        )
    }

    private fun renderThinking() {
        cancelAnimators()
        setUseThisVisible(false)
        wbCanvas.removeAllViews()
        wbCanvas.addView(makeCanvasLine("Aura is drafting…"))
        listOf(1f, 0.82f, 0.58f).forEachIndexed { index, fill ->
            val placeholder = makeSkeletonLine(fill)
            wbCanvas.addView(placeholder)
            val pulse = ObjectAnimator.ofFloat(placeholder, View.ALPHA, 1f, 0.35f).apply {
                duration = 650
                startDelay = (index * 140).toLong()
                repeatMode = ObjectAnimator.REVERSE
                repeatCount = ObjectAnimator.INFINITE
            }
            pulse.start()
            activeAnimators.add(pulse)
        }
    }

    /** Show the draft in the single preview box and reveal "Use this". Long-press the text to copy.
     *  The backend may return more than one suggestion; the box shows the first and Regenerate
     *  fetches a fresh take, matching the reference's single-preview UX. */
    private fun renderPreview(suggestions: List<String>) {
        cancelAnimators()
        wbCanvas.removeAllViews()
        val text = suggestions.firstOrNull().orEmpty()
        previewText = text
        wbCanvas.addView(makePreviewText(text))
        setUseThisVisible(true)
    }

    private fun renderMessage(message: String, retry: Boolean) {
        cancelAnimators()
        setUseThisVisible(false)
        wbCanvas.removeAllViews()
        wbCanvas.addView(makeCanvasLine(message))
        if (retry) {
            wbCanvas.addView(makeChip("↻ Try again", accent = false) {
                lastAction?.let { runDraft(it, lastTone, lastLang) }
            })
        }
    }

    private fun updateContextPreview() {
        // Reply works off the message the user copied (an IME can't read the chat bubble it's
        // answering); the clipboard itself is read later, at draft time, to avoid an extra read.
        if (selectedTool?.action == BuddyAction.REPLY) {
            wbContext.text = "on: the message you copied"
            return
        }
        val raw = currentInputConnection?.getTextBeforeCursor(160, 0)?.toString()?.trim().orEmpty()
        wbContext.text = if (raw.isEmpty()) {
            "Type or open a message, then pick an action"
        } else {
            "on: “$raw”"
        }
    }

    // --- Sub-row (tone / language) -----------------------------------------------

    private fun showSubRow(options: List<String>, onPick: (String) -> Unit) {
        wbSub.removeAllViews()
        for (option in options) {
            wbSub.addView(makeChip(option, accent = false) { onPick(option) })
        }
        wbSubRow.visibility = View.VISIBLE
    }

    private fun hideSubRow() {
        wbSub.removeAllViews()
        wbSubRow.visibility = View.GONE
    }

    // --- Emoji panel -------------------------------------------------------------

    /** Build the emoji panel once: a category tab strip, a scrollable grid, and a bottom row with
     *  ABC (back to keys) + backspace. The grid is repopulated per category in [renderEmojiGrid]. */
    private fun buildEmojiPanel() {
        emojiContainer.removeAllViews()

        // Category tabs (Recent + each category's representative glyph), horizontally scrollable.
        emojiTabs = LinearLayout(keyboardUiContext).apply { orientation = LinearLayout.HORIZONTAL }
        emojiTabsScroll = HorizontalScrollView(keyboardUiContext).apply {
            isHorizontalScrollBarEnabled = false
            layoutParams = rowParams(bottom = dp(2))
            addView(emojiTabs)
        }
        emojiContainer.addView(emojiTabsScroll)

        // The scrollable emoji grid, filling the available height. Vertical drags scroll it as
        // any ScrollView would; a clearly horizontal drag pages between categories.
        emojiGrid = LinearLayout(keyboardUiContext).apply { orientation = LinearLayout.VERTICAL }
        emojiGridScroll = HorizontalSwipeScrollView(keyboardUiContext).apply {
            isVerticalScrollBarEnabled = false
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)
            onSwipeLeft = { stepEmojiCategory(1) }
            onSwipeRight = { stepEmojiCategory(-1) }
            addView(emojiGrid)
        }
        emojiContainer.addView(emojiGridScroll)

        // Bottom row: ABC (back to keys) on the left, backspace on the right.
        val bottom = LinearLayout(keyboardUiContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            layoutParams = rowParams(top = dp(2))
        }
        bottom.addView(makeFooterButton("ABC") { closeEmojiPanel() })
        bottom.addView(makeSpacer())
        bottom.addView(TextView(keyboardUiContext).apply {
            text = "⌫"
            textSize = 20f
            gravity = Gravity.CENTER
            setTextColor(color(R.color.buddy_kb_key_text))
            val p = dp(10)
            setPadding(p, p, p, p)
            isClickable = true
            setOnClickListener { currentInputConnection?.let { handleBackspace(it) } }
        })
        emojiContainer.addView(bottom)
    }

    private fun openEmojiPanel() {
        finishComposing()
        mode = Mode.EMOJI
        // Open on Recent when there is history, else the first category.
        selectedEmojiCategory = if (recentEmojis().isEmpty()) 0 else -1
        renderEmojiTabs()
        renderEmojiGrid()
        showEmojiPanel()
    }

    private fun showEmojiPanel() {
        // Pin to the typing keyboard's height so the panel never balloons to ~3/4 of the screen,
        // and flip the height-driving typing layer's visibility SYNCHRONOUSLY (see showWhiteboardPanel).
        pinPanelHeight(emojiContainer)
        typingStack.animate().cancel()
        typingStack.visibility = View.INVISIBLE
        typingStack.alpha = 1f
        emojiContainer.animate().cancel()
        emojiContainer.alpha = 0f
        emojiContainer.translationY = dp(12).toFloat()
        emojiContainer.visibility = View.VISIBLE
        emojiContainer.animate().alpha(1f).translationY(0f).setDuration(160).start()
    }

    private fun closeEmojiPanel() {
        mode = Mode.TYPING
        typingStack.animate().cancel()
        typingStack.visibility = View.VISIBLE
        typingStack.alpha = 0f
        typingStack.animate().alpha(1f).setDuration(160).start()
        emojiContainer.animate().cancel()
        emojiContainer.animate().alpha(0f).translationY(dp(12).toFloat()).setDuration(130)
            .withEndAction {
                // Only hide if we didn't re-open the panel in the meantime.
                if (mode != Mode.EMOJI) {
                    emojiContainer.visibility = View.GONE
                    emojiContainer.alpha = 1f
                    emojiContainer.translationY = 0f
                }
            }.start()
    }

    /** The category tab strip: a "🕘" recents tab + each category's glyph; the active tab is accented. */
    private fun renderEmojiTabs() {
        emojiTabs.removeAllViews()
        emojiTabs.addView(makeEmojiTab("🕘", selected = selectedEmojiCategory == -1) {
            selectEmojiCategory(-1)
        })
        EmojiData.categories.forEachIndexed { index, category ->
            emojiTabs.addView(makeEmojiTab(category.label, selected = selectedEmojiCategory == index) {
                selectEmojiCategory(index)
            })
        }
    }

    private fun makeEmojiTab(glyph: String, selected: Boolean, onClick: () -> Unit): TextView =
        TextView(keyboardUiContext).apply {
            text = glyph
            textSize = 20f
            gravity = Gravity.CENTER
            val p = dp(8)
            setPadding(p, p, p, p)
            setBackgroundResource(
                if (selected) R.drawable.buddy_kb_chip_bg else R.drawable.buddy_kb_action_bg,
            )
            isClickable = true
            setOnClickListener { onClick() }
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { setMargins(dp(3), dp(2), dp(3), dp(2)) }
        }

    private fun selectEmojiCategory(index: Int) {
        selectedEmojiCategory = index
        renderEmojiTabs()
        renderEmojiGrid()
        // A new category always starts at the top; keeping the old offset lands a swipe
        // somewhere in the middle of a grid the user has not seen yet.
        emojiGridScroll.scrollTo(0, 0)
        revealSelectedEmojiTab()
    }

    /** Step one category along, clamped at both ends. Recents is index -1; wrapping from the
     *  last category back to it reads as a glitch, so a swipe past the end simply stops. */
    private fun stepEmojiCategory(delta: Int) {
        val next = (selectedEmojiCategory + delta).coerceIn(-1, EmojiData.categories.lastIndex)
        if (next != selectedEmojiCategory) selectEmojiCategory(next)
    }

    /** Scroll the tab strip so the active tab is on screen, otherwise swiping a few categories
     *  along leaves the highlight off the left edge. */
    private fun revealSelectedEmojiTab() {
        val tab = emojiTabs.getChildAt(selectedEmojiCategory + 1) ?: return
        emojiTabsScroll.post {
            emojiTabsScroll.smoothScrollTo(
                (tab.left - (emojiTabsScroll.width - tab.width) / 2).coerceAtLeast(0),
                0,
            )
        }
    }

    /** Fill the grid with the selected category's emojis (or recents), chunked into fixed columns. */
    private fun renderEmojiGrid() {
        emojiGrid.removeAllViews()
        val emojis = if (selectedEmojiCategory == -1) {
            recentEmojis()
        } else {
            EmojiData.categories.getOrNull(selectedEmojiCategory)?.emojis.orEmpty()
        }
        if (emojis.isEmpty()) {
            emojiGrid.addView(makeCanvasLine("No recent emojis yet. Tap one and it shows up here."))
            return
        }
        val cols = emojiColumns()
        for (rowEmojis in emojis.chunked(cols)) {
            val row = LinearLayout(keyboardUiContext).apply {
                orientation = LinearLayout.HORIZONTAL
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                )
            }
            for (emoji in rowEmojis) row.addView(makeEmojiCell(emoji))
            // Pad the last row so its cells stay the same width as the full rows above.
            repeat(cols - rowEmojis.size) {
                row.addView(View(keyboardUiContext).apply {
                    // Same margins as a real cell, or the last row's columns drift out of
                    // alignment with the full rows above it.
                    layoutParams = LinearLayout.LayoutParams(0, dp(1), 1f).apply {
                        setMargins(dp(2), dp(2), dp(2), dp(2))
                    }
                })
            }
            emojiGrid.addView(row)
        }
    }

    private fun makeEmojiCell(emoji: String): TextView = TextView(keyboardUiContext).apply {
        text = emoji
        textSize = 24f
        gravity = Gravity.CENTER
        isClickable = true
        setOnClickListener {
            if (keyboardSettings.hapticFeedback) {
                performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
            }
            playKeySound()
            onEmojiTapped(emoji)
        }
        layoutParams = LinearLayout.LayoutParams(0, dp(44), 1f).apply {
            setMargins(dp(2), dp(2), dp(2), dp(2))
        }
    }

    private fun onEmojiTapped(emoji: String) {
        val ic = currentInputConnection ?: return
        ic.commitText(emoji, 1)
        markResync()
        pushRecentEmoji(emoji)
    }

    /** Emoji grid columns, sized to the screen width (each cell ~ 40dp). */
    private fun emojiColumns(): Int {
        val dm = keyboardUiContext.resources.displayMetrics
        val widthDp = dm.widthPixels / dm.density
        return (widthDp / 40f).toInt().coerceIn(6, 10)
    }

    private fun recentEmojis(): List<String> =
        getSharedPreferences(EMOJI_PREFS, Context.MODE_PRIVATE)
            .getString(EMOJI_RECENTS_KEY, "")
            .orEmpty()
            .split(" ")
            .filter { it.isNotBlank() }

    /** Move [emoji] to the front of the recents list (deduped, capped). */
    private fun pushRecentEmoji(emoji: String) {
        val updated = (listOf(emoji) + recentEmojis().filter { it != emoji }).take(EMOJI_RECENTS_MAX)
        getSharedPreferences(EMOJI_PREFS, Context.MODE_PRIVATE).edit()
            .putString(EMOJI_RECENTS_KEY, updated.joinToString(" "))
            .apply()
    }

    // --- View builders -----------------------------------------------------------

    private fun makeOrb(size: Int): ImageView = ImageView(keyboardUiContext).apply {
        setImageResource(R.mipmap.ic_launcher)
        scaleType = ImageView.ScaleType.CENTER_CROP
        setBackgroundResource(R.drawable.buddy_kb_orb_ring)
        clipToOutline = true
        layoutParams = LinearLayout.LayoutParams(size, size).apply { rightMargin = dp(8) }
    }

    private fun makeHint(label: String): TextView = TextView(keyboardUiContext).apply {
        text = label
        textSize = 13f
        setTextColor(color(R.color.buddy_kb_text_muted))
    }

    /** A tappable hint for the collapsed bar's left action (draft / generate / talk). */
    private fun makeBarAction(label: String, onClick: () -> Unit): TextView = makeHint(label).apply {
        val p = dp(6)
        setPadding(p, p, p, p)
        isClickable = true
        setOnClickListener { onClick() }
    }

    private fun makeAuraToolbarButton(onClick: () -> Unit): FrameLayout {
        val orb = makeOrb(dp(32)).apply {
            layoutParams = FrameLayout.LayoutParams(dp(32), dp(32), Gravity.CENTER)
        }
        return makeToolbarSlot(orb, "Aura", onClick)
    }

    /** A fixed, evenly weighted toolbar icon. Tint and pressed state both resolve through Aura's
     *  light/night resources; an absent listener intentionally leaves a visual placeholder inert. */
    private fun makeToolbarIcon(
        iconRes: Int,
        label: String,
        onClick: (() -> Unit)? = null,
    ): FrameLayout {
        val icon = ImageView(keyboardUiContext).apply {
            setImageResource(iconRes)
            imageTintList = ColorStateList.valueOf(color(R.color.buddy_kb_key_text))
            scaleType = ImageView.ScaleType.CENTER_INSIDE
            layoutParams = FrameLayout.LayoutParams(dp(24), dp(24), Gravity.CENTER)
        }
        return makeToolbarSlot(icon, label, onClick)
    }

    private fun makeToolbarLabel(
        label: String,
        contentLabel: String,
        onClick: (() -> Unit)? = null,
    ): FrameLayout {
        val textView = TextView(keyboardUiContext).apply {
            text = label
            textSize = 14f
            gravity = Gravity.CENTER
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(color(R.color.buddy_kb_key_text))
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER,
            )
        }
        return makeToolbarSlot(textView, contentLabel, onClick)
    }

    private fun makeToolbarSlot(
        content: View,
        label: String,
        onClick: (() -> Unit)?,
    ): FrameLayout = FrameLayout(keyboardUiContext).apply {
        layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f)
        setBackgroundResource(R.drawable.buddy_kb_toolbar_button_bg)
        addView(content)
        if (onClick == null) {
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        } else {
            contentDescription = label
            isClickable = true
            isFocusable = true
            setOnClickListener { onClick() }
        }
    }

    private fun makeSpacer(): View = View(keyboardUiContext).apply {
        layoutParams = LinearLayout.LayoutParams(0, 1, 1f)
    }

    private fun makeFooterButton(label: String, onClick: () -> Unit): TextView =
        TextView(keyboardUiContext).apply {
            text = label
            textSize = 13f
            setTextColor(color(R.color.buddy_kb_text_muted))
            setTypeface(typeface, Typeface.BOLD)
            val padH = dp(8)
            val padV = dp(6)
            setPadding(padH, padV, padH, padV)
            isClickable = true
            setOnClickListener { onClick() }
        }

    private fun makeCanvasLine(text: String): TextView = TextView(keyboardUiContext).apply {
        this.text = text
        textSize = 14f
        gravity = Gravity.CENTER
        setTextColor(color(R.color.buddy_kb_text_muted))
        val p = dp(12)
        setPadding(p, dp(10), p, dp(10))
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )
    }

    private fun makeSkeletonLine(fill: Float): View = LinearLayout(keyboardUiContext).apply {
        orientation = LinearLayout.HORIZONTAL
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            dp(14),
        ).apply { setMargins(dp(10), dp(5), dp(10), dp(5)) }
        val weight = fill.coerceIn(0.1f, 1f)
        addView(View(keyboardUiContext).apply {
            setBackgroundResource(R.drawable.buddy_kb_skeleton_bg)
        }, LinearLayout.LayoutParams(0, dp(14), weight))
        if (weight < 1f) {
            addView(View(keyboardUiContext), LinearLayout.LayoutParams(0, 1, 1f - weight))
        }
    }

    /** The draft text inside the preview box. Read-only (editing happens in the real field after
     *  "Use this"); long-press copies it. */
    private fun makePreviewText(text: String, muted: Boolean = false): TextView =
        TextView(keyboardUiContext).apply {
        this.text = text
        textSize = 16f
        gravity = Gravity.CENTER
        setTextColor(color(if (muted) R.color.buddy_kb_text_muted else R.color.buddy_kb_key_text))
        setAllCaps(false)
        val padH = dp(12)
        val padV = dp(10)
        setPadding(padH, padV, padH, padV)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
        )
        isClickable = !muted
        if (!muted) setOnLongClickListener { copyToClipboard(text); true }
        }

    private fun makeChip(label: String, accent: Boolean, onClick: () -> Unit): TextView =
        TextView(keyboardUiContext).apply {
            text = label
            gravity = Gravity.CENTER
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
            maxWidth = dp(240)
            textSize = 14f
            setAllCaps(false)
            setBackgroundResource(
                if (accent) R.drawable.buddy_kb_chip_bg else R.drawable.buddy_kb_action_bg
            )
            setTextColor(color(if (accent) R.color.buddy_kb_accent_text else R.color.buddy_kb_key_text))
            val padH = dp(16)
            val padV = dp(10)
            setPadding(padH, padV, padH, padV)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { setMargins(dp(4), dp(4), dp(4), dp(4)) }
            setOnClickListener { onClick() }
        }

    /** Friendly, action-pointing copy for a draft that returned no suggestions. */
    private fun reasonCopy(reason: String): String = when (reason) {
        "empty_context" -> "Type or open a message first"
        "timeout" -> "That took too long. Try again"
        "unauthorized" -> "Open Aura to refresh your sign-in"
        "network_error" -> "No connection. Check your internet"
        else -> "Couldn't draft that. Try again"
    }

    private fun copyToClipboard(text: String) {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        cm.setPrimaryClip(ClipData.newPlainText("Buddy draft", text))
        Toast.makeText(this, "Copied", Toast.LENGTH_SHORT).show()
    }

    // --- helpers -----------------------------------------------------------------

    private fun cancelAnimators() {
        for (a in activeAnimators) a.cancel()
        activeAnimators.clear()
    }

    private fun rowParams(top: Int = 0, bottom: Int = 0): LinearLayout.LayoutParams =
        LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { setMargins(0, top, 0, bottom) }

    private fun refreshKeyboardUiContext() {
        appliedKeyboardNightMode =
            KeyboardThemeContext.effectiveNightMode(this, keyboardSettings.themeMode)
        keyboardUiContext = KeyboardThemeContext.wrap(this, keyboardSettings.themeMode)
    }

    private fun color(resId: Int): Int = ContextCompat.getColor(keyboardUiContext, resId)

    private fun dp(value: Int): Int =
        (value * keyboardUiContext.resources.displayMetrics.density).toInt()

    /** Letter-key height, sized to the screen like Gboard (clamped per device) so the keyboard
     *  fills a comfortable footprint instead of looking small on a tall phone. */
    private fun keyHeightPx(scale: Float): Int {
        val dm = keyboardUiContext.resources.displayMetrics
        val screenHeightDp = dm.heightPixels / dm.density
        // 0.061 rather than 0.058, with the clamps moved by the same ~5%: measured against
        // Gboard the keys sat roughly a centimetre short, which costs typing accuracy.
        val baseKeyDp = (screenHeightDp * 0.061f).coerceIn(52.5f, 63f)
        val keyDp = (baseKeyDp * scale).coerceAtLeast(46f)
        return (keyDp * dm.density).toInt()
    }
}
