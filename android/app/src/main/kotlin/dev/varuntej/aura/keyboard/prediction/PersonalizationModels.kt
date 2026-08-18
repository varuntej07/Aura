package dev.varuntej.aura.keyboard.prediction

import kotlin.math.exp

enum class LearningProvenance {
    MANUAL_TYPED,
    EXPLICIT_SUGGESTION,
    AUTOMATIC_CORRECTION,
    AUTOCORRECT_UNDO,
    MANUAL_CORRECTION,
    WORD_DELETION,
    EXPLICIT_ADD,
    EXPLICIT_REMOVE,
    LEGACY_IMPORT,
}

sealed interface PersonalizationEvent {
    val atMillis: Long

    data class ManualWordCommitted(
        val word: String,
        val previousWord: String?,
        val previousPreviousWord: String?,
        override val atMillis: Long,
    ) : PersonalizationEvent

    data class SuggestionAccepted(
        val rawWord: String,
        val acceptedWord: String,
        val previousWord: String?,
        val previousPreviousWord: String?,
        override val atMillis: Long,
    ) : PersonalizationEvent

    data class AutomaticCorrection(
        val rawWord: String,
        val correctedWord: String,
        override val atMillis: Long,
    ) : PersonalizationEvent

    data class AutocorrectUndo(
        val rawWord: String,
        val correctedWord: String,
        override val atMillis: Long,
    ) : PersonalizationEvent

    data class ManualCorrection(
        val rawWord: String,
        val finalWord: String,
        override val atMillis: Long,
    ) : PersonalizationEvent

    data class WordDeleted(
        val word: String,
        override val atMillis: Long,
    ) : PersonalizationEvent

    data class ExplicitAdd(
        val word: String,
        override val atMillis: Long,
    ) : PersonalizationEvent

    data class ExplicitRemove(
        val word: String,
        override val atMillis: Long,
    ) : PersonalizationEvent
}

data class PersonalLexeme(
    val key: String,
    val display: String,
    val manualCount: Int,
    val acceptedCount: Int,
    val explicitCount: Int,
    val legacyCount: Int,
    val lastUsedMillis: Long,
) {
    fun score(nowMillis: Long): Double {
        val weightedCount = manualCount * 1.0 + acceptedCount * 0.8 +
            explicitCount * 4.0 + legacyCount * 0.25
        val elapsedDays = (nowMillis - lastUsedMillis).coerceAtLeast(0).toDouble() / MILLIS_PER_DAY
        return weightedCount * exp(-elapsedDays / LEXEME_DECAY_DAYS)
    }

    companion object {
        private const val MILLIS_PER_DAY = 86_400_000.0
        private const val LEXEME_DECAY_DAYS = 90.0
    }
}

data class NGramRecord(
    val context: List<String>,
    val continuation: String,
    val count: Int,
    val lastUsedMillis: Long,
) {
    init {
        require(context.size in 1..2)
    }

    fun score(nowMillis: Long): Double {
        val elapsedDays = (nowMillis - lastUsedMillis).coerceAtLeast(0).toDouble() / MILLIS_PER_DAY
        return count * exp(-elapsedDays / NGRAM_DECAY_DAYS)
    }

    fun stableKey(): String = context.joinToString(CONTEXT_SEPARATOR) + RECORD_SEPARATOR + continuation

    companion object {
        const val CONTEXT_SEPARATOR = "\u0001"
        const val RECORD_SEPARATOR = "\u0000"
        private const val MILLIS_PER_DAY = 86_400_000.0
        private const val NGRAM_DECAY_DAYS = 45.0
    }
}

data class CorrectionEvidence(
    val rawWord: String,
    val finalWord: String,
    val manualEvidence: Int,
    val negativeEvidence: Int,
    val lastUpdatedMillis: Long,
) {
    val isMature: Boolean
        get() = manualEvidence - negativeEvidence >= PersonalizationPolicy.CORRECTION_MATURITY_EVIDENCE

    fun stableKey(): String = rawWord + NGramRecord.RECORD_SEPARATOR + finalWord
}

data class PendingPositive(
    val id: Long,
    val provenance: LearningProvenance,
    val word: String,
    val displayWord: String,
    val previousWord: String?,
    val previousPreviousWord: String?,
    val dueAtMillis: Long,
)

object PersonalizationPolicy {
    const val MAX_LEXEMES = 10_000
    const val MAX_NGRAMS = 20_000
    const val MAX_CORRECTIONS = 5_000
    const val MAX_PENDING_EVENTS = 128
    const val MAX_TOKEN_LENGTH = 48
    const val PENDING_FEEDBACK_GUARD_MS = 1_500L
    const val CORRECTION_MATURITY_EVIDENCE = 3
    const val MIN_TRIGRAM_EVIDENCE = 2
    const val CORRECTION_INDEPENDENCE_WINDOW_MS = 30_000L
    const val MAX_EVENT_QUEUE = 256
    const val PERSIST_IDLE_MS = 750L

    fun normalizeLearnableToken(raw: String): String? {
        val trimmed = raw.trim()
        if (trimmed.length !in 2..MAX_TOKEN_LENGTH) return null
        if (trimmed.any { it.isISOControl() || it.isWhitespace() }) return null
        if ('@' in trimmed || "://" in trimmed || trimmed.startsWith("www.", ignoreCase = true)) {
            return null
        }
        val letters = trimmed.count(Char::isLetter)
        if (letters * 10 < trimmed.length * 7) return null
        if (!trimmed.all { it.isLetter() || it == '\'' || it == '-' }) return null
        if (looksHighEntropy(trimmed)) return null
        return trimmed.lowercase(java.util.Locale.ROOT)
    }

    fun normalizeContextToken(raw: String): String? {
        val trimmed = raw.trim()
        if (trimmed.length == 1 && (trimmed.equals("a", true) || trimmed.equals("i", true))) {
            return trimmed.lowercase(java.util.Locale.ROOT)
        }
        return normalizeLearnableToken(trimmed)
    }

    private fun looksHighEntropy(token: String): Boolean {
        if (token.length < 16) return false
        val uniqueRatio = token.lowercase().toSet().size.toDouble() / token.length
        val transitions = token.zipWithNext().count { (a, b) ->
            a.isLowerCase() != b.isLowerCase() || a.isDigit() != b.isDigit()
        }
        val vowelRatio = token.count { it.lowercaseChar() in "aeiouy" }.toDouble() / token.length
        return uniqueRatio > 0.72 && (transitions >= 4 || vowelRatio < 0.20)
    }
}
