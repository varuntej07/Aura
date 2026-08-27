package dev.varuntej.aura.keyboard.prediction

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.charset.StandardCharsets
import java.util.Locale

data class PackedLookup(
    val candidates: List<WordCandidate>,
    val edgeComparisons: Int,
    val labelCharacters: Int,
)

data class CorrectionCandidate(
    val word: String,
    val frequency: Int,
    val editDistance: Int,
    val proximityScore: Int,
)

/**
 * Immutable packed Patricia/radix dictionary backed directly by a read-only byte buffer.
 *
 * Prefix nodes cache their bounded best descendants. Lookup is O(m + k): it follows only the
 * compressed labels for the prefix and decodes at most [maxCachedCompletions] words. The backing
 * buffer may be a memory-mapped APK asset, so the 30K dictionary creates no heap graph of nodes or
 * strings. Corruption is rejected at construction and callers degrade to base typing.
 */
class PackedDictionary private constructor(private val data: ByteBuffer) {
    val maxCachedCompletions: Int
    val nodeCount: Int
    val edgeCount: Int
    val wordCount: Int
    val sourceSha256: ByteArray

    private val topIdCount: Int
    private val labelByteCount: Int
    private val wordByteCount: Int
    private val nodesOffset: Int
    private val edgesOffset: Int
    private val topsOffset: Int
    private val labelsOffset: Int
    private val wordRecordsOffset: Int
    private val wordsOffset: Int

    init {
        require(data.remaining() >= HEADER_SIZE) { "packed dictionary header is truncated" }
        val magic = ByteArray(MAGIC.size)
        readBytes(0, magic)
        require(magic.contentEquals(MAGIC)) { "packed dictionary magic mismatch" }
        require(intAt(8) == VERSION) { "unsupported packed dictionary version" }
        maxCachedCompletions = intAt(12)
        nodeCount = intAt(16)
        edgeCount = intAt(20)
        wordCount = intAt(24)
        topIdCount = intAt(28)
        labelByteCount = intAt(32)
        wordByteCount = intAt(36)
        sourceSha256 = ByteArray(32).also { readBytes(40, it) }
        require(maxCachedCompletions in 1..MAX_ALLOWED_TOP)
        require(nodeCount > 0 && edgeCount >= 0 && wordCount > 0 && topIdCount >= 0)
        require(labelByteCount >= 0 && wordByteCount >= 0)

        nodesOffset = HEADER_SIZE
        edgesOffset = checkedOffset(nodesOffset, nodeCount, NODE_BYTES)
        topsOffset = checkedOffset(edgesOffset, edgeCount, EDGE_BYTES)
        labelsOffset = checkedOffset(topsOffset, topIdCount, Int.SIZE_BYTES)
        wordRecordsOffset = checkedOffset(labelsOffset, labelByteCount, 1)
        wordsOffset = checkedOffset(wordRecordsOffset, wordCount, WORD_BYTES)
        val expectedSize = checkedOffset(wordsOffset, wordByteCount, 1)
        require(expectedSize == data.limit()) { "packed dictionary size mismatch" }
        validateRootAndRanges()
    }

    fun completions(prefix: String, limit: Int): List<WordCandidate> =
        completionsWithStats(prefix, limit).candidates

    fun completionsWithStats(prefix: String, limit: Int): PackedLookup {
        if (limit <= 0 || prefix.isEmpty()) return PackedLookup(emptyList(), 0, 0)
        val query = prefix.lowercase(Locale.ROOT)
        if (!query.all { it in 'a'..'z' }) return PackedLookup(emptyList(), 0, 0)
        var node = 0
        var position = 0
        var edgeComparisons = 0
        var labelCharacters = 0
        while (position < query.length) {
            val edge = findEdge(node, query[position].code) { edgeComparisons++ }
            if (edge < 0) return PackedLookup(emptyList(), edgeComparisons, labelCharacters)
            val labelOffset = edgeInt(edge, EDGE_LABEL_OFFSET)
            val labelLength = edgeInt(edge, EDGE_LABEL_LENGTH)
            val child = edgeInt(edge, EDGE_CHILD)
            for (labelIndex in 0 until labelLength) {
                if (position == query.length) {
                    return PackedLookup(topCompletions(child, limit), edgeComparisons, labelCharacters)
                }
                labelCharacters++
                if (labelByte(labelOffset + labelIndex).toInt() != query[position].code) {
                    return PackedLookup(emptyList(), edgeComparisons, labelCharacters)
                }
                position++
            }
            node = child
        }
        return PackedLookup(topCompletions(node, limit), edgeComparisons, labelCharacters)
    }

    fun contains(word: String): Boolean = terminalWordId(word) >= 0

    fun frequencyOf(word: String): Int {
        val wordId = terminalWordId(word)
        return if (wordId >= 0) wordFrequency(wordId) else 0
    }

    /** Trie-guided bounded Damerau-Levenshtein search; no edit strings are materialized. */
    fun corrections(
        rawWord: String,
        limit: Int,
        maxEditDistance: Int = 2,
        cancellation: PredictionCancellation = PredictionCancellation.NEVER,
    ): List<CorrectionCandidate> {
        if (limit <= 0 || maxEditDistance !in 1..2) return emptyList()
        val target = rawWord.lowercase(Locale.ROOT)
        if (target.length < MIN_CORRECTION_LENGTH || !target.all { it in 'a'..'z' }) {
            return emptyList()
        }
        val initialRow = IntArray(target.length + 1) { it }
        val best = CorrectionAccumulator(limit)
        var work = 0

        fun visit(
            node: Int,
            previousRow: IntArray,
            rowBeforePrevious: IntArray?,
            previousTrieCharacter: Char?,
        ) {
            if (cancellation.isCancelled()) return
            val firstEdge = nodeInt(node, NODE_FIRST_EDGE)
            val count = nodeInt(node, NODE_EDGE_COUNT)
            for (edge in firstEdge until firstEdge + count) {
                if ((work++ and CANCELLATION_MASK) == 0 && cancellation.isCancelled()) return
                val labelOffset = edgeInt(edge, EDGE_LABEL_OFFSET)
                val labelLength = edgeInt(edge, EDGE_LABEL_LENGTH)
                var row = previousRow
                var priorRow = rowBeforePrevious
                var priorCharacter = previousTrieCharacter
                var pruned = false
                for (labelIndex in 0 until labelLength) {
                    val character = labelByte(labelOffset + labelIndex).toInt().toChar()
                    val current = IntArray(target.length + 1)
                    current[0] = row[0] + 1
                    var rowMinimum = current[0]
                    for (column in 1..target.length) {
                        val insertion = current[column - 1] + 1
                        val deletion = row[column] + 1
                        val substitution = row[column - 1] +
                            if (target[column - 1] == character) 0 else 1
                        var cost = minOf(insertion, deletion, substitution)
                        if (column > 1 && priorRow != null && priorCharacter != null &&
                            character == target[column - 2] && priorCharacter == target[column - 1]
                        ) {
                            cost = minOf(cost, priorRow[column - 2] + 1)
                        }
                        current[column] = cost
                        rowMinimum = minOf(rowMinimum, cost)
                    }
                    priorRow = row
                    row = current
                    priorCharacter = character
                    if (rowMinimum > maxEditDistance) {
                        pruned = true
                        break
                    }
                }
                if (pruned || cancellation.isCancelled()) continue
                val child = edgeInt(edge, EDGE_CHILD)
                val terminal = nodeInt(child, NODE_TERMINAL_WORD)
                val distance = row[target.length]
                if (terminal >= 0 && distance in 1..maxEditDistance) {
                    val candidate = wordAt(terminal)
                    best.offer(
                        CorrectionCandidate(
                            word = candidate,
                            frequency = wordFrequency(terminal),
                            editDistance = distance,
                            proximityScore = KeyboardGeometry.proximityScore(target, candidate),
                        )
                    )
                }
                if (row.minOrNull()!! <= maxEditDistance) {
                    visit(child, row, priorRow, priorCharacter)
                }
            }
        }

        visit(0, initialRow, null, null)
        return if (cancellation.isCancelled()) emptyList() else best.values()
    }

    private fun topCompletions(node: Int, requestedLimit: Int): List<WordCandidate> {
        val count = minOf(nodeInt(node, NODE_TOP_COUNT), requestedLimit, maxCachedCompletions)
        if (count <= 0) return emptyList()
        val start = nodeInt(node, NODE_TOP_START)
        return ArrayList<WordCandidate>(count).also { out ->
            repeat(count) { index ->
                val wordId = intAt(topsOffset + (start + index) * Int.SIZE_BYTES)
                out.add(WordCandidate(wordAt(wordId), wordFrequency(wordId)))
            }
        }
    }

    private fun terminalWordId(rawWord: String): Int {
        if (rawWord.isEmpty()) return -1
        val word = rawWord.lowercase(Locale.ROOT)
        if (!word.all { it in 'a'..'z' }) return -1
        var node = 0
        var position = 0
        while (position < word.length) {
            val edge = findEdge(node, word[position].code)
            if (edge < 0) return -1
            val labelOffset = edgeInt(edge, EDGE_LABEL_OFFSET)
            val labelLength = edgeInt(edge, EDGE_LABEL_LENGTH)
            for (labelIndex in 0 until labelLength) {
                if (position >= word.length ||
                    labelByte(labelOffset + labelIndex).toInt() != word[position].code
                ) {
                    return -1
                }
                position++
            }
            node = edgeInt(edge, EDGE_CHILD)
        }
        return nodeInt(node, NODE_TERMINAL_WORD)
    }

    private inline fun findEdge(node: Int, firstCharacter: Int, onCompare: () -> Unit = {}): Int {
        var low = nodeInt(node, NODE_FIRST_EDGE)
        var high = low + nodeInt(node, NODE_EDGE_COUNT) - 1
        while (low <= high) {
            val middle = (low + high) ushr 1
            onCompare()
            val current = edgeInt(middle, EDGE_FIRST_CHARACTER)
            when {
                current < firstCharacter -> low = middle + 1
                current > firstCharacter -> high = middle - 1
                else -> return middle
            }
        }
        return -1
    }

    private fun validateRootAndRanges() {
        for (node in 0 until nodeCount) {
            val firstEdge = nodeInt(node, NODE_FIRST_EDGE)
            val edges = nodeInt(node, NODE_EDGE_COUNT)
            val topStart = nodeInt(node, NODE_TOP_START)
            val tops = nodeInt(node, NODE_TOP_COUNT)
            val terminal = nodeInt(node, NODE_TERMINAL_WORD)
            require(firstEdge >= 0 && edges >= 0 && firstEdge + edges <= edgeCount)
            require(topStart >= 0 && tops in 0..maxCachedCompletions && topStart + tops <= topIdCount)
            require(terminal in -1 until wordCount)
            var lastFirst = -1
            for (edge in firstEdge until firstEdge + edges) {
                val labelOffset = edgeInt(edge, EDGE_LABEL_OFFSET)
                val labelLength = edgeInt(edge, EDGE_LABEL_LENGTH)
                val child = edgeInt(edge, EDGE_CHILD)
                val first = edgeInt(edge, EDGE_FIRST_CHARACTER)
                require(labelLength > 0 && labelOffset >= 0 && labelOffset + labelLength <= labelByteCount)
                require(child in 0 until nodeCount && first in 'a'.code..'z'.code && first > lastFirst)
                require(labelByte(labelOffset).toInt() == first)
                lastFirst = first
            }
        }
        for (index in 0 until topIdCount) require(intAt(topsOffset + index * 4) in 0 until wordCount)
        for (word in 0 until wordCount) {
            val offset = wordInt(word, WORD_OFFSET)
            val length = wordInt(word, WORD_LENGTH)
            require(offset >= 0 && length > 0 && offset + length <= wordByteCount)
            require(wordInt(word, WORD_FREQUENCY) >= 0)
        }
    }

    private fun wordAt(wordId: Int): String {
        val offset = wordInt(wordId, WORD_OFFSET)
        val length = wordInt(wordId, WORD_LENGTH)
        val bytes = ByteArray(length)
        readBytes(wordsOffset + offset, bytes)
        return String(bytes, StandardCharsets.US_ASCII)
    }

    private fun wordFrequency(wordId: Int): Int = wordInt(wordId, WORD_FREQUENCY)
    private fun nodeInt(node: Int, field: Int): Int = intAt(nodesOffset + node * NODE_BYTES + field * 4)
    private fun edgeInt(edge: Int, field: Int): Int = intAt(edgesOffset + edge * EDGE_BYTES + field * 4)
    private fun wordInt(word: Int, field: Int): Int = intAt(wordRecordsOffset + word * WORD_BYTES + field * 4)
    private fun labelByte(index: Int): Byte = data.get(labelsOffset + index)
    private fun intAt(index: Int): Int = data.getInt(index)

    private fun readBytes(index: Int, target: ByteArray) {
        data.duplicate().apply {
            position(index)
            get(target)
        }
    }

    companion object {
        private val MAGIC = "AURAPD01".toByteArray(StandardCharsets.US_ASCII)
        private const val VERSION = 1
        private const val HEADER_SIZE = 72
        private const val NODE_BYTES = 20
        private const val EDGE_BYTES = 16
        private const val WORD_BYTES = 12
        private const val MAX_ALLOWED_TOP = 32
        private const val MIN_CORRECTION_LENGTH = 3
        private const val CANCELLATION_MASK = 63

        private const val NODE_FIRST_EDGE = 0
        private const val NODE_EDGE_COUNT = 1
        private const val NODE_TOP_START = 2
        private const val NODE_TOP_COUNT = 3
        private const val NODE_TERMINAL_WORD = 4

        private const val EDGE_LABEL_OFFSET = 0
        private const val EDGE_LABEL_LENGTH = 1
        private const val EDGE_CHILD = 2
        private const val EDGE_FIRST_CHARACTER = 3

        private const val WORD_OFFSET = 0
        private const val WORD_LENGTH = 1
        private const val WORD_FREQUENCY = 2

        fun from(buffer: ByteBuffer): PackedDictionary {
            // asReadOnlyBuffer() resets the byte order on Android/JVM, so set little-endian last.
            val readOnly = buffer.slice().asReadOnlyBuffer().order(ByteOrder.LITTLE_ENDIAN)
            return PackedDictionary(readOnly)
        }

        private fun checkedOffset(start: Int, count: Int, width: Int): Int {
            val result = start.toLong() + count.toLong() * width
            require(result in 0..Int.MAX_VALUE.toLong()) { "packed dictionary offset overflow" }
            return result.toInt()
        }
    }

    private class CorrectionAccumulator(private val capacity: Int) {
        private val entries = ArrayList<CorrectionCandidate>(capacity)

        fun offer(candidate: CorrectionCandidate) {
            var index = 0
            while (index < entries.size && compare(entries[index], candidate) <= 0) index++
            if (index >= capacity) return
            entries.add(index, candidate)
            if (entries.size > capacity) entries.removeAt(entries.lastIndex)
        }

        fun values(): List<CorrectionCandidate> = entries.toList()

        private fun compare(left: CorrectionCandidate, right: CorrectionCandidate): Int {
            if (left.editDistance != right.editDistance) {
                return left.editDistance.compareTo(right.editDistance)
            }
            if (left.proximityScore != right.proximityScore) {
                return right.proximityScore.compareTo(left.proximityScore)
            }
            if (left.frequency != right.frequency) return right.frequency.compareTo(left.frequency)
            return left.word.compareTo(right.word)
        }
    }
}
