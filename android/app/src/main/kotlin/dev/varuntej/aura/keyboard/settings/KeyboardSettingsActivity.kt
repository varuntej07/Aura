package dev.varuntej.aura.keyboard.settings

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import dev.varuntej.aura.R
import dev.varuntej.aura.keyboard.prediction.KeyboardPersonalizationRepository

/** Native settings entry used by Android's IME picker and the keyboard toolbar. */
class KeyboardSettingsActivity : Activity() {
    private lateinit var content: LinearLayout
    private var sectionCard: LinearLayout? = null
    private var clearButton: TextView? = null
    private var clearStatusView: TextView? = null
    private var clearStatus: String? = null

    override fun attachBaseContext(newBase: Context) {
        val mode = KeyboardSettingsStore.read(newBase).themeMode
        super.attachBaseContext(KeyboardThemeContext.wrap(newBase, mode))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        clearStatus = savedInstanceState?.getString(CLEAR_STATUS_STATE)
        title = getString(R.string.aura_keyboard_settings_title)
        window.statusBarColor = color(R.color.buddy_kb_background)
        window.navigationBarColor = color(R.color.buddy_kb_background)
        WindowCompat.setDecorFitsSystemWindows(window, false)

        content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(10), dp(20), dp(36))
        }
        val scroll = ScrollView(this).apply {
            isFillViewport = true
            setBackgroundColor(color(R.color.buddy_kb_background))
            addView(
                content,
                ViewGroup.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ),
            )
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(color(R.color.buddy_kb_background))
            addView(buildHeader())
            addView(scroll, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1f,
            ))
        }
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(0, bars.top, 0, bars.bottom)
            insets
        }
        setContentView(root)
        ViewCompat.requestApplyInsets(root)
        rebuild()
    }

    override fun onResume() {
        super.onResume()
        if (::content.isInitialized) rebuild()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        clearStatus?.let { outState.putString(CLEAR_STATUS_STATE, it) }
        super.onSaveInstanceState(outState)
    }

    private fun rebuild() {
        content.removeAllViews()
        sectionCard = null
        val settings = KeyboardSettingsStore.read(this)

        addSection(R.string.keyboard_settings_typing)
        addSwitch(
            R.string.keyboard_settings_suggestions,
            R.string.keyboard_settings_suggestions_summary,
            settings.suggestions,
            KeyboardSettingsStore::setSuggestions,
        )
        addSwitch(
            R.string.keyboard_settings_autocorrect,
            R.string.keyboard_settings_autocorrect_summary,
            settings.autocorrect,
            KeyboardSettingsStore::setAutocorrect,
        )
        addSwitch(
            R.string.keyboard_settings_learn_words,
            R.string.keyboard_settings_learn_words_summary,
            settings.learnNewWords,
            KeyboardSettingsStore::setLearnNewWords,
        )
        addSwitch(
            R.string.keyboard_settings_personalized,
            R.string.keyboard_settings_personalized_summary,
            settings.personalizedSuggestions,
            KeyboardSettingsStore::setPersonalizedSuggestions,
        )
        addSwitch(
            R.string.keyboard_settings_haptic,
            R.string.keyboard_settings_haptic_summary,
            settings.hapticFeedback,
            KeyboardSettingsStore::setHapticFeedback,
        )
        addSwitch(
            R.string.keyboard_settings_sound,
            R.string.keyboard_settings_sound_summary,
            settings.keypressSound,
            KeyboardSettingsStore::setKeypressSound,
        )
        addSwitch(
            R.string.keyboard_settings_key_preview,
            R.string.keyboard_settings_key_preview_summary,
            settings.keyPreview,
            KeyboardSettingsStore::setKeyPreview,
        )

        addSection(R.string.keyboard_settings_appearance)
        addThemeRow(settings.themeMode)

        addSection(R.string.keyboard_settings_privacy)
        addInfo(
            R.string.keyboard_settings_local_title,
            R.string.keyboard_settings_local_summary,
        )
        addInfo(
            R.string.keyboard_settings_memory_title,
            R.string.keyboard_settings_memory_summary,
        )
        addInfo(
            R.string.keyboard_settings_ai_title,
            R.string.keyboard_settings_ai_summary,
        )
        // A consent you cannot withdraw is not consent. These mirror the in-keyboard
        // disclosures: turning one off here means Buddy asks again next time it would send.
        val consent = KeyboardConsentStore.read(this)
        addSwitch(
            R.string.keyboard_settings_ai_consent,
            R.string.keyboard_settings_ai_consent_summary,
            consent.aiText.isGranted,
            KeyboardConsentStore::setAiTextConsent,
        )
        addSwitch(
            R.string.keyboard_settings_voice_consent,
            R.string.keyboard_settings_voice_consent_summary,
            consent.voice.isGranted,
            KeyboardConsentStore::setVoiceConsent,
        )
        addClearControl()
        addAction(R.string.keyboard_settings_privacy_link) { openPrivacyPage() }

        addSection(R.string.keyboard_settings_advanced)
        addSwitch(
            R.string.keyboard_settings_advanced_diagnostics,
            R.string.keyboard_settings_advanced_diagnostics_summary,
            settings.advancedDiagnostics,
        ) { context, enabled ->
            KeyboardSettingsStore.setAdvancedDiagnostics(context, enabled)
            content.post(::rebuild)
        }
        if (settings.advancedDiagnostics) addDiagnostics()
        trimTrailingDivider()
    }

    private fun buildHeader(): View = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setPadding(dp(20), dp(8), dp(20), dp(8))
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            dp(76),
        )
        addView(TextView(this@KeyboardSettingsActivity).apply {
            text = "←"
            contentDescription = getString(R.string.keyboard_settings_back)
            setTextColor(color(R.color.buddy_kb_key_text))
            textSize = 25f
            gravity = Gravity.CENTER
            setBackgroundResource(R.drawable.aura_keyboard_settings_back_bg)
            isClickable = true
            isFocusable = true
            setOnClickListener { finish() }
        }, LinearLayout.LayoutParams(dp(44), dp(44)).apply { marginEnd = dp(14) })
        addView(LinearLayout(this@KeyboardSettingsActivity).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            addView(TextView(this@KeyboardSettingsActivity).apply {
                text = getString(R.string.aura_keyboard_settings_title)
                setTextColor(color(R.color.buddy_kb_key_text))
                setTypeface(typeface, Typeface.BOLD)
                textSize = 24f
            })
            addView(TextView(this@KeyboardSettingsActivity).apply {
                text = getString(R.string.keyboard_settings_subtitle)
                setTextColor(color(R.color.buddy_kb_text_muted))
                textSize = 13f
                setPadding(0, dp(2), 0, 0)
            })
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f))
    }

    private fun addSection(titleRes: Int) {
        trimTrailingDivider()
        content.addView(TextView(this).apply {
            text = getString(titleRes)
            setTextColor(color(R.color.buddy_kb_text_muted))
            setTypeface(typeface, Typeface.BOLD)
            textSize = 13f
            letterSpacing = 0.08f
            setPadding(dp(6), dp(22), dp(6), dp(10))
        })
        sectionCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.aura_keyboard_settings_card_bg)
        }.also { card ->
            content.addView(
                card,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { setMargins(0, 0, 0, dp(2)) },
            )
        }
    }

    private fun addToSection(view: View) {
        checkNotNull(sectionCard) { "Settings rows require a section" }.addView(view)
    }

    private fun addSwitch(
        titleRes: Int,
        summaryRes: Int,
        checked: Boolean,
        save: (Context, Boolean) -> Unit,
    ) {
        val toggle = Switch(this).apply {
            isChecked = checked
            contentDescription = getString(titleRes)
            showText = false
            thumbTintList = ColorStateList(
                arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()),
                intArrayOf(color(R.color.buddy_kb_accent), color(R.color.buddy_kb_key)),
            )
            trackTintList = ColorStateList(
                arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()),
                intArrayOf(color(R.color.buddy_kb_accent_soft), color(R.color.buddy_kb_border)),
            )
        }
        val row = settingsRow(titleRes, summaryRes).apply {
            addView(
                toggle,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { gravity = Gravity.CENTER_VERTICAL },
            )
            setOnClickListener { toggle.isChecked = !toggle.isChecked }
        }
        toggle.setOnCheckedChangeListener { _, enabled -> save(this, enabled) }
        addToSection(row)
        addDivider()
    }

    private fun addThemeRow(mode: KeyboardThemeMode) {
        val summary = when (mode) {
            KeyboardThemeMode.SYSTEM -> R.string.keyboard_settings_theme_system
            KeyboardThemeMode.LIGHT -> R.string.keyboard_settings_theme_light
            KeyboardThemeMode.DARK -> R.string.keyboard_settings_theme_dark
        }
        addToSection(settingsRow(R.string.keyboard_settings_theme, summary).apply {
            isClickable = true
            isFocusable = true
            setOnClickListener { showThemeDialog(mode) }
        })
        addDivider()
    }

    private fun showThemeDialog(current: KeyboardThemeMode) {
        val labels = arrayOf(
            getString(R.string.keyboard_settings_theme_system),
            getString(R.string.keyboard_settings_theme_light),
            getString(R.string.keyboard_settings_theme_dark),
        )
        val modes = KeyboardThemeMode.entries
        val group = RadioGroup(this).apply {
            orientation = RadioGroup.VERTICAL
            setPadding(dp(20), dp(8), dp(20), dp(8))
        }
        modes.forEachIndexed { index, mode ->
            group.addView(RadioButton(this).apply {
                id = View.generateViewId()
                text = labels[index]
                setTextColor(color(R.color.buddy_kb_key_text))
                isChecked = mode == current
                setOnClickListener {
                    KeyboardSettingsStore.setThemeMode(this@KeyboardSettingsActivity, mode)
                    recreate()
                }
            })
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.keyboard_settings_theme)
            .setView(group)
            .setNegativeButton(R.string.keyboard_settings_cancel, null)
            .show()
    }

    private fun addInfo(titleRes: Int, summaryRes: Int) {
        addToSection(settingsRow(titleRes, summaryRes))
        addDivider()
    }

    private fun addClearControl() {
        val wrapper = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(14), dp(16), dp(14))
        }
        clearButton = TextView(this).apply {
            text = getString(R.string.keyboard_settings_clear)
            setAllCaps(false)
            setTextColor(color(R.color.buddy_kb_accent))
            setTypeface(typeface, Typeface.BOLD)
            textSize = 14f
            gravity = Gravity.CENTER
            setBackgroundResource(R.drawable.aura_keyboard_settings_action_bg)
            setPadding(dp(14), dp(13), dp(14), dp(13))
            isClickable = true
            isFocusable = true
            setOnClickListener { confirmClear() }
        }
        clearStatusView = TextView(this).apply {
            text = clearStatus.orEmpty()
            visibility = if (clearStatus == null) View.GONE else View.VISIBLE
            setTextColor(color(R.color.buddy_kb_text_muted))
            textSize = 13f
            setPadding(dp(4), dp(8), dp(4), 0)
        }
        wrapper.addView(clearButton)
        wrapper.addView(clearStatusView)
        addToSection(wrapper)
        addDivider()
    }

    private fun confirmClear() {
        AlertDialog.Builder(this)
            .setTitle(R.string.keyboard_settings_clear_confirm_title)
            .setMessage(R.string.keyboard_settings_clear_confirm_message)
            .setNegativeButton(R.string.keyboard_settings_cancel, null)
            .setPositiveButton(R.string.keyboard_settings_clear_action) { _, _ -> clearLearnedData() }
            .show()
    }

    private fun clearLearnedData() {
        clearButton?.isEnabled = false
        showClearStatus(getString(R.string.keyboard_settings_clear_working))
        KeyboardPersonalizationRepository.dictionary(applicationContext).clearAll { success ->
            runOnUiThread {
                clearButton?.isEnabled = true
                showClearStatus(
                    getString(
                        if (success) {
                            R.string.keyboard_settings_clear_success
                        } else {
                            R.string.keyboard_settings_clear_failure
                        },
                    ),
                )
            }
        }
    }

    private fun showClearStatus(message: String) {
        clearStatus = message
        clearStatusView?.apply {
            text = message
            visibility = View.VISIBLE
        }
    }

    private fun addAction(labelRes: Int, action: () -> Unit) {
        addToSection(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(18), dp(17), dp(18), dp(17))
            isClickable = true
            isFocusable = true
            setOnClickListener { action() }
            addView(TextView(this@KeyboardSettingsActivity).apply {
                text = getString(labelRes)
                setTextColor(color(R.color.buddy_kb_accent))
                setTypeface(typeface, Typeface.BOLD)
                textSize = 15f
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(TextView(this@KeyboardSettingsActivity).apply {
                text = "↗"
                setTextColor(color(R.color.buddy_kb_text_muted))
                textSize = 18f
            })
        })
        addDivider()
    }

    private fun addDiagnostics() {
        addSection(R.string.keyboard_settings_diagnostics)
        val snapshot = KeyboardRuntimeDiagnostics.snapshot()
        addDiagnostic(R.string.keyboard_settings_model_status, snapshot.status)
        addDiagnostic(R.string.keyboard_settings_provider, snapshot.provider)
        addDiagnostic(R.string.keyboard_settings_model_version, snapshot.modelVersion)
        addDiagnostic(R.string.keyboard_settings_inference_count, snapshot.inferenceCount.toString())
        addDiagnostic(R.string.keyboard_settings_rerank_attempts, snapshot.rerankAttempts.toString())
        addDiagnostic(R.string.keyboard_settings_rerank_changes, snapshot.rerankTopOneChanges)
        addDiagnostic(R.string.keyboard_settings_rerank_fallbacks, snapshot.rerankLexicalFallbacks.toString())
        addDiagnostic(R.string.keyboard_settings_last_error, snapshot.lastErrorCategory)
        addToSection(TextView(this).apply {
            text = getString(R.string.keyboard_settings_diagnostics_note)
            setTextColor(color(R.color.buddy_kb_text_muted))
            textSize = 13f
            setPadding(dp(18), dp(14), dp(18), dp(12))
        })
        addAction(R.string.keyboard_settings_refresh_diagnostics, ::rebuild)
    }

    private fun addDiagnostic(labelRes: Int, value: String) {
        val row = settingsRow(labelRes, null)
        row.addView(TextView(this).apply {
            text = value
            setTextColor(color(R.color.buddy_kb_text_muted))
            textSize = 14f
            gravity = Gravity.END
        })
        addToSection(row)
        addDivider()
    }

    private fun settingsRow(titleRes: Int, summaryRes: Int?): LinearLayout {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(18), dp(15), dp(14), dp(15))
            minimumHeight = dp(72)
        }
        row.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(this@KeyboardSettingsActivity).apply {
                text = getString(titleRes)
                setTextColor(color(R.color.buddy_kb_key_text))
                textSize = 16f
            })
            if (summaryRes != null) {
                addView(TextView(this@KeyboardSettingsActivity).apply {
                    text = getString(summaryRes)
                    setTextColor(color(R.color.buddy_kb_text_muted))
                    textSize = 13f
                    setPadding(0, dp(4), dp(14), 0)
                    setLineSpacing(0f, 1.08f)
                })
            }
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        return row
    }

    private fun addDivider() {
        val divider = View(this).apply {
            setBackgroundColor(color(R.color.buddy_kb_border))
            tag = DIVIDER_TAG
        }
        checkNotNull(sectionCard) { "Settings dividers require a section" }.addView(
            divider,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)).apply {
                setMargins(dp(18), 0, dp(18), 0)
            },
        )
    }

    private fun trimTrailingDivider() {
        val card = sectionCard ?: return
        if (card.childCount == 0) return
        val last = card.getChildAt(card.childCount - 1) ?: return
        if (last.tag == DIVIDER_TAG) card.removeViewAt(card.childCount - 1)
    }

    private fun openPrivacyPage() {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(PRIVACY_URL)))
        } catch (_: Throwable) {
            Toast.makeText(
                this,
                R.string.keyboard_settings_privacy_link_error,
                Toast.LENGTH_SHORT,
            ).show()
        }
    }

    private fun color(resource: Int): Int = ContextCompat.getColor(this, resource)

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private companion object {
        // Anchored at the keyboard section, not the policy root: "Read the keyboard privacy
        // explanation" has to land on keyboard content, not on paragraph one of the app policy.
        const val PRIVACY_URL = "https://auravoiceapp.com/privacy-policy#aura-keyboard"
        const val CLEAR_STATUS_STATE = "keyboard_clear_status"
        const val DIVIDER_TAG = "keyboard_settings_divider"
    }
}
