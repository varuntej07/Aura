package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PersonalizationReducerTest {
    @Test
    fun manualCommitIsPendingUntilGuardMatures_thenBuildsBigramAndTrigram() {
        val state = PersonalizationState()
        val mutation = PersonalizationReducer.record(
            state,
            PersonalizationEvent.ManualWordCommitted("World", "hello", "say", 1_000),
        )
        assertTrue(state.lexemes.isEmpty())
        val pendingId = mutation.addedPendingIds.single()
        assertTrue(PersonalizationReducer.mature(state, pendingId, 3_000))
        val snapshot = state.snapshot(3_000)
        assertTrue(snapshot.lexemeKeys.contains("world"))
        assertEquals("world", snapshot.nextWords(listOf("say", "hello"), 1).single())
        assertEquals("world", snapshot.nextWords(listOf("hello"), 1).single())
    }

    @Test
    fun deletionCancelsPendingPositive() {
        val state = PersonalizationState()
        val pendingId = PersonalizationReducer.record(
            state,
            PersonalizationEvent.ManualWordCommitted("mistkae", null, null, 1_000),
        ).addedPendingIds.single()
        PersonalizationReducer.record(
            state,
            PersonalizationEvent.WordDeleted("mistkae", 1_100),
        )
        assertFalse(PersonalizationReducer.mature(state, pendingId, 3_000))
        assertFalse(state.snapshot(3_000).lexemeKeys.contains("mistkae"))
    }

    @Test
    fun correctionNeedsThreeIndependentManualEvents_andUndoSuppressesIt() {
        val state = PersonalizationState()
        val spacing = PersonalizationPolicy.CORRECTION_INDEPENDENCE_WINDOW_MS
        repeat(3) { index ->
            PersonalizationReducer.record(
                state,
                PersonalizationEvent.ManualCorrection("teh", "the", index * spacing),
            )
        }
        assertEquals("the", state.snapshot(spacing * 3).matureCorrections["teh"]?.finalWord)
        PersonalizationReducer.record(
            state,
            PersonalizationEvent.AutocorrectUndo("teh", "the", spacing * 4),
        )
        assertNull(state.snapshot(spacing * 4).matureCorrections["teh"])
    }

    @Test
    fun automaticCorrectionAloneNeverCreatesPositiveLabel() {
        val state = PersonalizationState()
        PersonalizationReducer.record(
            state,
            PersonalizationEvent.AutomaticCorrection("teh", "the", 100),
        )
        val snapshot = state.snapshot(200)
        assertTrue(snapshot.lexemeKeys.isEmpty())
        assertTrue(snapshot.matureCorrections.isEmpty())
        assertEquals(1L, state.provenanceCounters[LearningProvenance.AUTOMATIC_CORRECTION])
    }

    @Test
    fun codecRoundTripPreservesPendingCountersAndRankingState() {
        val state = PersonalizationState(generation = 7)
        val id = PersonalizationReducer.record(
            state,
            PersonalizationEvent.SuggestionAccepted("aur", "Aura", "hello", null, 1_000),
        ).addedPendingIds.single()
        PersonalizationReducer.mature(state, id, 3_000)
        PersonalizationReducer.record(
            state,
            PersonalizationEvent.ManualWordCommitted("Buddy", "Aura", "hello", 4_000),
        )
        val decoded = PersonalizationCodec.decode(PersonalizationCodec.encode(state))
        assertEquals(7, decoded.generation)
        assertEquals(state.provenanceCounters, decoded.provenanceCounters)
        assertEquals(state.pending.keys, decoded.pending.keys)
        assertEquals("Aura", decoded.snapshot(5_000).prefixIndex.completions("au", 1).single().word)
    }

    @Test
    fun malformedSecretsAndRemoteStyleTokensAreRejected() {
        assertNull(PersonalizationPolicy.normalizeLearnableToken("https://example.com"))
        assertNull(PersonalizationPolicy.normalizeLearnableToken("me@example.com"))
        assertNull(PersonalizationPolicy.normalizeLearnableToken("12345678"))
        assertNull(PersonalizationPolicy.normalizeLearnableToken("AbCdEfGhIjKlMnOpQrStUvWx"))
        assertNull(PersonalizationPolicy.normalizeLearnableToken("qwrtypsdfghjklzxcvbnm"))
        assertEquals("buddy", PersonalizationPolicy.normalizeLearnableToken("Buddy"))
        assertEquals("i", PersonalizationPolicy.normalizeContextToken("I"))
    }

    @Test
    fun ngramRankingDecays_andSparseTrigramFallsBackToBigram() {
        val now = 400L * 86_400_000L
        val state = PersonalizationState()
        val old = NGramRecord(listOf("hello"), "there", 20, 0)
        val recent = NGramRecord(listOf("hello"), "buddy", 2, now)
        state.ngrams[old.stableKey()] = old
        state.ngrams[recent.stableKey()] = recent
        val snapshot = state.snapshot(now)
        assertEquals(listOf("buddy", "there"), snapshot.nextWords(listOf("hello"), 2))
        assertEquals(
            listOf("buddy", "there"),
            snapshot.nextWords(listOf("unknown", "hello"), 2),
        )
    }

    @Test
    fun sparseTrigramFallsBackUntilIndependentEvidenceMatures() {
        val state = PersonalizationState()
        val bigram = NGramRecord(listOf("hello"), "buddy", 4, 100)
        val sparseTrigram = NGramRecord(listOf("say", "hello"), "world", 1, 200)
        state.ngrams[bigram.stableKey()] = bigram
        state.ngrams[sparseTrigram.stableKey()] = sparseTrigram
        assertEquals("buddy", state.snapshot(200).nextWords(listOf("say", "hello"), 1).single())
        state.ngrams[sparseTrigram.stableKey()] = sparseTrigram.copy(count = 2)
        assertEquals("world", state.snapshot(200).nextWords(listOf("say", "hello"), 1).single())
    }

    @Test
    fun deletionAndExplicitRemovalPurgePositiveNgramEvidence() {
        val state = PersonalizationState()
        val record = NGramRecord(listOf("hello"), "buddy", 1, 100)
        state.ngrams[record.stableKey()] = record
        state.lexemes["buddy"] = PersonalLexeme("buddy", "Buddy", 1, 0, 0, 0, 100)
        PersonalizationReducer.record(state, PersonalizationEvent.WordDeleted("buddy", 200))
        assertTrue(state.ngrams.isEmpty())
        state.ngrams[record.stableKey()] = record
        state.lexemes["buddy"] = PersonalLexeme("buddy", "Buddy", 1, 0, 0, 0, 100)
        PersonalizationReducer.record(state, PersonalizationEvent.ExplicitRemove("buddy", 300))
        assertTrue(state.ngrams.isEmpty())
        assertFalse(state.lexemes.containsKey("buddy"))
    }

    @Test
    fun globalCapsEvictTheDeterministicLowestValueEntries() {
        val state = PersonalizationState()
        repeat(PersonalizationPolicy.MAX_LEXEMES + 1) { index ->
            val key = "term" + index.toString().padStart(5, 'a')
            state.lexemes[key] = PersonalLexeme(
                key, key, manualCount = if (index == 0) 0 else 1,
                acceptedCount = 0, explicitCount = 0, legacyCount = 0,
                lastUsedMillis = index.toLong(),
            )
        }
        repeat(PersonalizationPolicy.MAX_NGRAMS + 1) { index ->
            val continuation = "word" + index.toString().padStart(5, 'a')
            val record = NGramRecord(listOf("context"), continuation, 1, index.toLong())
            state.ngrams[record.stableKey()] = record
        }
        state.enforceBounds(PersonalizationPolicy.MAX_NGRAMS.toLong())
        assertEquals(PersonalizationPolicy.MAX_LEXEMES, state.lexemes.size)
        assertEquals(PersonalizationPolicy.MAX_NGRAMS, state.ngrams.size)
        assertFalse(state.lexemes.containsKey("termaaaa0"))
        assertFalse(state.ngrams.values.any { it.continuation == "wordaaaa0" })
    }
}
