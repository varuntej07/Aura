package dev.varuntej.aura.keyboard.prediction

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.util.EnumMap

internal class PersonalizationState(
    var generation: Long = 0,
    var nextPendingId: Long = 1,
    val lexemes: MutableMap<String, PersonalLexeme> = LinkedHashMap(),
    val ngrams: MutableMap<String, NGramRecord> = LinkedHashMap(),
    val corrections: MutableMap<String, CorrectionEvidence> = LinkedHashMap(),
    val pending: MutableMap<Long, PendingPositive> = LinkedHashMap(),
    val provenanceCounters: MutableMap<LearningProvenance, Long> =
        EnumMap<LearningProvenance, Long>(LearningProvenance::class.java).apply {
            LearningProvenance.entries.forEach { put(it, 0) }
        },
) {
    fun snapshot(nowMillis: Long): PersonalizationSnapshot {
        val continuationIndex = ngrams.values
            .groupBy { it.context.joinToString(NGramRecord.CONTEXT_SEPARATOR) }
            .filterValues { records ->
                records.first().context.size == 1 ||
                    records.sumOf(NGramRecord::count) >= PersonalizationPolicy.MIN_TRIGRAM_EVIDENCE
            }
            .mapValues { (_, records) ->
                records.sortedWith(
                    compareByDescending<NGramRecord> { it.score(nowMillis) }
                        .thenByDescending { it.lastUsedMillis }
                        .thenBy { it.continuation },
                ).take(PER_CONTEXT_TOP).map { record ->
                    WordCandidate(
                        record.continuation,
                        (record.score(nowMillis) * SCORE_SCALE).toInt().coerceAtLeast(1),
                    )
                }
            }
        val matureCorrections = corrections.values
            .filter(CorrectionEvidence::isMature)
            .groupBy(CorrectionEvidence::rawWord)
            .mapValues { (_, evidence) ->
                evidence.maxWith(
                    compareBy<CorrectionEvidence> { it.manualEvidence - it.negativeEvidence }
                        .thenBy { it.lastUpdatedMillis }
                        .thenByDescending { it.finalWord },
                )
            }
        return PersonalizationSnapshot(
            generation = generation,
            prefixIndex = PersonalPrefixIndex.from(lexemes.values, nowMillis),
            lexemeKeys = lexemes.keys.toSet(),
            continuations = continuationIndex,
            matureCorrections = matureCorrections,
        )
    }

    fun enforceBounds(nowMillis: Long) {
        while (lexemes.size > PersonalizationPolicy.MAX_LEXEMES) {
            val victim = lexemes.values.minWith(
                compareBy<PersonalLexeme> { it.score(nowMillis) }
                    .thenBy { it.lastUsedMillis }
                    .thenBy { it.key },
            )
            lexemes.remove(victim.key)
        }
        while (ngrams.size > PersonalizationPolicy.MAX_NGRAMS) {
            val victim = ngrams.values.minWith(
                compareBy<NGramRecord> { it.score(nowMillis) }
                    .thenBy { it.lastUsedMillis }
                    .thenBy(NGramRecord::stableKey),
            )
            ngrams.remove(victim.stableKey())
        }
        while (corrections.size > PersonalizationPolicy.MAX_CORRECTIONS) {
            val victim = corrections.values.minWith(
                compareBy<CorrectionEvidence> { it.manualEvidence - it.negativeEvidence }
                    .thenBy { it.lastUpdatedMillis }
                    .thenBy(CorrectionEvidence::stableKey),
            )
            corrections.remove(victim.stableKey())
        }
        while (pending.size > PersonalizationPolicy.MAX_PENDING_EVENTS) {
            val victim = pending.values.minWith(
                compareBy<PendingPositive> { it.dueAtMillis }.thenBy { it.id },
            )
            pending.remove(victim.id)
        }
    }

    private companion object {
        const val PER_CONTEXT_TOP = 8
        const val SCORE_SCALE = 1_000.0
    }
}

internal data class PersonalizationMutation(
    val changed: Boolean,
    val addedPendingIds: List<Long> = emptyList(),
)

internal object PersonalizationReducer {
    fun record(state: PersonalizationState, event: PersonalizationEvent): PersonalizationMutation {
        val provenance = when (event) {
            is PersonalizationEvent.ManualWordCommitted -> LearningProvenance.MANUAL_TYPED
            is PersonalizationEvent.SuggestionAccepted -> LearningProvenance.EXPLICIT_SUGGESTION
            is PersonalizationEvent.AutomaticCorrection -> LearningProvenance.AUTOMATIC_CORRECTION
            is PersonalizationEvent.AutocorrectUndo -> LearningProvenance.AUTOCORRECT_UNDO
            is PersonalizationEvent.ManualCorrection -> LearningProvenance.MANUAL_CORRECTION
            is PersonalizationEvent.WordDeleted -> LearningProvenance.WORD_DELETION
            is PersonalizationEvent.ExplicitAdd -> LearningProvenance.EXPLICIT_ADD
            is PersonalizationEvent.ExplicitRemove -> LearningProvenance.EXPLICIT_REMOVE
        }
        state.provenanceCounters[provenance] = state.provenanceCounters.getValue(provenance) + 1

        val mutation = when (event) {
            is PersonalizationEvent.ManualWordCommitted -> queuePositive(
                state,
                provenance,
                event.word,
                event.previousWord,
                event.previousPreviousWord,
                event.atMillis,
            )
            is PersonalizationEvent.SuggestionAccepted -> queuePositive(
                state,
                provenance,
                event.acceptedWord,
                event.previousWord,
                event.previousPreviousWord,
                event.atMillis,
            )
            is PersonalizationEvent.AutomaticCorrection -> PersonalizationMutation(changed = true)
            is PersonalizationEvent.AutocorrectUndo -> {
                cancelPendingWord(state, event.correctedWord)
                addNegativeCorrection(state, event.rawWord, event.correctedWord, event.atMillis)
                PersonalizationMutation(changed = true)
            }
            is PersonalizationEvent.ManualCorrection -> {
                addManualCorrection(state, event.rawWord, event.finalWord, event.atMillis)
                PersonalizationMutation(changed = true)
            }
            is PersonalizationEvent.WordDeleted -> {
                if (!cancelPendingWord(state, event.word)) {
                    decrementLexeme(state, event.word)
                    decrementNGrams(state, event.word)
                }
                PersonalizationMutation(changed = true)
            }
            is PersonalizationEvent.ExplicitAdd -> {
                addLexeme(state, event.word, provenance, event.atMillis)
                PersonalizationMutation(changed = true)
            }
            is PersonalizationEvent.ExplicitRemove -> {
                val key = PersonalizationPolicy.normalizeLearnableToken(event.word)
                if (key != null) {
                    state.lexemes.remove(key)
                    state.pending.values.filter { it.word == key }.forEach { state.pending.remove(it.id) }
                    state.corrections.values.filter { it.rawWord == key || it.finalWord == key }.forEach {
                        state.corrections.remove(it.stableKey())
                    }
                    state.ngrams.values.filter {
                        it.continuation == key || key in it.context
                    }.forEach { state.ngrams.remove(it.stableKey()) }
                }
                PersonalizationMutation(changed = true)
            }
        }
        state.enforceBounds(event.atMillis)
        return mutation
    }

    fun mature(state: PersonalizationState, pendingId: Long, nowMillis: Long): Boolean {
        val pending = state.pending.remove(pendingId) ?: return false
        addLexeme(state, pending.displayWord, pending.provenance, nowMillis)
        val previous = pending.previousWord?.let(PersonalizationPolicy::normalizeContextToken)
        val previousPrevious = pending.previousPreviousWord
            ?.let(PersonalizationPolicy::normalizeContextToken)
        if (previous != null) addNGram(state, listOf(previous), pending.word, nowMillis)
        if (previous != null && previousPrevious != null) {
            addNGram(state, listOf(previousPrevious, previous), pending.word, nowMillis)
        }
        state.enforceBounds(nowMillis)
        return true
    }

    private fun queuePositive(
        state: PersonalizationState,
        provenance: LearningProvenance,
        rawWord: String,
        previousWord: String?,
        previousPreviousWord: String?,
        atMillis: Long,
    ): PersonalizationMutation {
        val word = PersonalizationPolicy.normalizeLearnableToken(rawWord)
            ?: return PersonalizationMutation(changed = true)
        val id = state.nextPendingId++
        state.pending[id] = PendingPositive(
            id = id,
            provenance = provenance,
            word = word,
            displayWord = rawWord.trim(),
            previousWord = previousWord,
            previousPreviousWord = previousPreviousWord,
            dueAtMillis = atMillis + PersonalizationPolicy.PENDING_FEEDBACK_GUARD_MS,
        )
        return PersonalizationMutation(changed = true, addedPendingIds = listOf(id))
    }

    private fun addLexeme(
        state: PersonalizationState,
        rawWord: String,
        provenance: LearningProvenance,
        atMillis: Long,
    ) {
        val key = PersonalizationPolicy.normalizeLearnableToken(rawWord) ?: return
        val old = state.lexemes[key]
        state.lexemes[key] = PersonalLexeme(
            key = key,
            display = rawWord,
            manualCount = (old?.manualCount ?: 0) +
                if (provenance == LearningProvenance.MANUAL_TYPED) 1 else 0,
            acceptedCount = (old?.acceptedCount ?: 0) +
                if (provenance == LearningProvenance.EXPLICIT_SUGGESTION) 1 else 0,
            explicitCount = (old?.explicitCount ?: 0) +
                if (provenance == LearningProvenance.EXPLICIT_ADD) 1 else 0,
            legacyCount = old?.legacyCount ?: 0,
            lastUsedMillis = atMillis,
        )
    }

    private fun decrementLexeme(state: PersonalizationState, rawWord: String) {
        val key = PersonalizationPolicy.normalizeLearnableToken(rawWord) ?: return
        val old = state.lexemes[key] ?: return
        val updated = old.copy(
            manualCount = (old.manualCount - 1).coerceAtLeast(0),
            acceptedCount = (old.acceptedCount - 1).coerceAtLeast(0),
        )
        if (updated.manualCount + updated.acceptedCount + updated.explicitCount + updated.legacyCount == 0) {
            state.lexemes.remove(key)
        } else {
            state.lexemes[key] = updated
        }
    }

    private fun decrementNGrams(state: PersonalizationState, rawWord: String) {
        val key = PersonalizationPolicy.normalizeLearnableToken(rawWord) ?: return
        state.ngrams.values.filter { it.continuation == key }.toList().forEach { record ->
            if (record.count <= 1) state.ngrams.remove(record.stableKey())
            else state.ngrams[record.stableKey()] = record.copy(count = record.count - 1)
        }
    }

    private fun addNGram(
        state: PersonalizationState,
        context: List<String>,
        continuation: String,
        atMillis: Long,
    ) {
        val normalizedContinuation = PersonalizationPolicy.normalizeLearnableToken(continuation) ?: return
        val record = NGramRecord(context, normalizedContinuation, 1, atMillis)
        val old = state.ngrams[record.stableKey()]
        state.ngrams[record.stableKey()] = record.copy(count = (old?.count ?: 0) + 1)
    }

    private fun addManualCorrection(
        state: PersonalizationState,
        raw: String,
        final: String,
        atMillis: Long,
    ) {
        val rawKey = PersonalizationPolicy.normalizeLearnableToken(raw) ?: return
        val finalKey = PersonalizationPolicy.normalizeLearnableToken(final) ?: return
        if (rawKey == finalKey) return
        val key = rawKey + NGramRecord.RECORD_SEPARATOR + finalKey
        val old = state.corrections[key]
        val independent = old == null ||
            atMillis - old.lastUpdatedMillis >= PersonalizationPolicy.CORRECTION_INDEPENDENCE_WINDOW_MS
        state.corrections[key] = CorrectionEvidence(
            rawWord = rawKey,
            finalWord = finalKey,
            manualEvidence = (old?.manualEvidence ?: 0) + if (independent) 1 else 0,
            negativeEvidence = old?.negativeEvidence ?: 0,
            lastUpdatedMillis = atMillis,
        )
    }

    private fun addNegativeCorrection(
        state: PersonalizationState,
        raw: String,
        corrected: String,
        atMillis: Long,
    ) {
        val rawKey = PersonalizationPolicy.normalizeLearnableToken(raw) ?: return
        val correctedKey = PersonalizationPolicy.normalizeLearnableToken(corrected) ?: return
        val key = rawKey + NGramRecord.RECORD_SEPARATOR + correctedKey
        val old = state.corrections[key]
        state.corrections[key] = CorrectionEvidence(
            rawWord = rawKey,
            finalWord = correctedKey,
            manualEvidence = old?.manualEvidence ?: 0,
            negativeEvidence = (old?.negativeEvidence ?: 0) + 1,
            lastUpdatedMillis = atMillis,
        )
    }

    private fun cancelPendingWord(state: PersonalizationState, rawWord: String): Boolean {
        val key = PersonalizationPolicy.normalizeLearnableToken(rawWord) ?: return false
        val pending = state.pending.values.lastOrNull { it.word == key } ?: return false
        state.pending.remove(pending.id)
        return true
    }
}

internal object PersonalizationCodec {
    private const val MAGIC = "AURAPS02"
    private const val VERSION = 2

    fun encode(state: PersonalizationState): ByteArray {
        val bytes = ByteArrayOutputStream()
        DataOutputStream(bytes).use { out ->
            out.write(MAGIC.toByteArray(Charsets.US_ASCII))
            out.writeInt(VERSION)
            out.writeLong(state.generation)
            out.writeLong(state.nextPendingId)
            out.writeInt(state.lexemes.size)
            out.writeInt(state.ngrams.size)
            out.writeInt(state.corrections.size)
            out.writeInt(state.pending.size)
            out.writeInt(LearningProvenance.entries.size)
            LearningProvenance.entries.forEach { out.writeLong(state.provenanceCounters[it] ?: 0) }
            state.lexemes.toSortedMap().values.forEach { lexeme ->
                out.writeUTF(lexeme.key)
                out.writeUTF(lexeme.display)
                out.writeInt(lexeme.manualCount)
                out.writeInt(lexeme.acceptedCount)
                out.writeInt(lexeme.explicitCount)
                out.writeInt(lexeme.legacyCount)
                out.writeLong(lexeme.lastUsedMillis)
            }
            state.ngrams.toSortedMap().values.forEach { ngram ->
                out.writeInt(ngram.context.size)
                ngram.context.forEach(out::writeUTF)
                out.writeUTF(ngram.continuation)
                out.writeInt(ngram.count)
                out.writeLong(ngram.lastUsedMillis)
            }
            state.corrections.toSortedMap().values.forEach { correction ->
                out.writeUTF(correction.rawWord)
                out.writeUTF(correction.finalWord)
                out.writeInt(correction.manualEvidence)
                out.writeInt(correction.negativeEvidence)
                out.writeLong(correction.lastUpdatedMillis)
            }
            state.pending.toSortedMap().values.forEach { pending ->
                out.writeLong(pending.id)
                out.writeInt(pending.provenance.ordinal)
                out.writeUTF(pending.word)
                out.writeUTF(pending.displayWord)
                writeNullable(out, pending.previousWord)
                writeNullable(out, pending.previousPreviousWord)
                out.writeLong(pending.dueAtMillis)
            }
        }
        return bytes.toByteArray()
    }

    fun decode(bytes: ByteArray): PersonalizationState {
        DataInputStream(ByteArrayInputStream(bytes)).use { input ->
            val magic = ByteArray(MAGIC.length).also(input::readFully)
            require(String(magic, Charsets.US_ASCII) == MAGIC)
            require(input.readInt() == VERSION)
            val generation = input.readLong().also { require(it >= 0) }
            val nextPendingId = input.readLong().also { require(it > 0) }
            val lexemeCount = boundedCount(input.readInt(), PersonalizationPolicy.MAX_LEXEMES)
            val ngramCount = boundedCount(input.readInt(), PersonalizationPolicy.MAX_NGRAMS)
            val correctionCount = boundedCount(input.readInt(), PersonalizationPolicy.MAX_CORRECTIONS)
            val pendingCount = boundedCount(input.readInt(), PersonalizationPolicy.MAX_PENDING_EVENTS)
            require(input.readInt() == LearningProvenance.entries.size)
            val counters = EnumMap<LearningProvenance, Long>(LearningProvenance::class.java)
            LearningProvenance.entries.forEach { counters[it] = input.readLong().also { value -> require(value >= 0) } }
            val state = PersonalizationState(generation, nextPendingId, provenanceCounters = counters)
            repeat(lexemeCount) {
                val key = input.readUTF()
                val display = input.readUTF()
                require(PersonalizationPolicy.normalizeLearnableToken(key) == key)
                state.lexemes[key] = PersonalLexeme(
                    key,
                    display,
                    nonNegative(input.readInt()),
                    nonNegative(input.readInt()),
                    nonNegative(input.readInt()),
                    nonNegative(input.readInt()),
                    input.readLong(),
                )
            }
            repeat(ngramCount) {
                val contextSize = input.readInt().also { size -> require(size in 1..2) }
                val context = List(contextSize) { input.readUTF() }
                val continuation = input.readUTF()
                val record = NGramRecord(context, continuation, nonNegative(input.readInt()), input.readLong())
                state.ngrams[record.stableKey()] = record
            }
            repeat(correctionCount) {
                val evidence = CorrectionEvidence(
                    input.readUTF(), input.readUTF(), nonNegative(input.readInt()),
                    nonNegative(input.readInt()), input.readLong(),
                )
                state.corrections[evidence.stableKey()] = evidence
            }
            repeat(pendingCount) {
                val id = input.readLong()
                val provenance = LearningProvenance.entries[input.readInt()]
                state.pending[id] = PendingPositive(
                    id,
                    provenance,
                    input.readUTF(),
                    input.readUTF(),
                    readNullable(input),
                    readNullable(input),
                    input.readLong(),
                )
            }
            require(input.available() == 0)
            return state
        }
    }

    private fun writeNullable(out: DataOutputStream, value: String?) {
        out.writeBoolean(value != null)
        if (value != null) out.writeUTF(value)
    }

    private fun readNullable(input: DataInputStream): String? =
        if (input.readBoolean()) input.readUTF() else null

    private fun boundedCount(value: Int, maximum: Int): Int = value.also { require(it in 0..maximum) }
    private fun nonNegative(value: Int): Int = value.also { require(it >= 0) }
}
