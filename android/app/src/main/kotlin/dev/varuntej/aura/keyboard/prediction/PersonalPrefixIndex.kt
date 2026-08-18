package dev.varuntej.aura.keyboard.prediction

/** Immutable, cached-prefix trie snapshot for the bounded mutable personal overlay. */
class PersonalPrefixIndex private constructor(private val root: Node) {
    private data class RankedLexeme(val lexeme: PersonalLexeme, val score: Double)

    private class Node(
        val children: Map<Char, Node>,
        val top: List<RankedLexeme>,
        val terminal: PersonalLexeme?,
    )

    private class MutableNode {
        val children = sortedMapOf<Char, MutableNode>()
        var terminal: PersonalLexeme? = null
    }

    fun completions(prefix: String, limit: Int): List<WordCandidate> {
        if (prefix.isEmpty() || limit <= 0) return emptyList()
        var node = root
        for (character in prefix.lowercase()) {
            node = node.children[character] ?: return emptyList()
        }
        return node.top.asSequence().take(limit).map { ranked ->
            WordCandidate(
                ranked.lexeme.display,
                (ranked.score * SCORE_SCALE).toInt().coerceAtLeast(1),
            )
        }.toList()
    }

    fun contains(word: String): Boolean {
        var node = root
        for (character in word.lowercase()) node = node.children[character] ?: return false
        return node.terminal != null
    }

    companion object {
        private const val CACHED_TOP = 8

        val EMPTY = PersonalPrefixIndex(Node(emptyMap(), emptyList(), null))

        fun from(lexemes: Collection<PersonalLexeme>, nowMillis: Long): PersonalPrefixIndex {
            if (lexemes.isEmpty()) return EMPTY
            val mutableRoot = MutableNode()
            for (lexeme in lexemes) {
                var node = mutableRoot
                for (character in lexeme.key) node = node.children.getOrPut(character, ::MutableNode)
                node.terminal = lexeme
            }

            fun freeze(mutable: MutableNode): Node {
                val children = mutable.children.mapValues { freeze(it.value) }
                val candidates = ArrayList<RankedLexeme>()
                mutable.terminal?.let { candidates.add(RankedLexeme(it, it.score(nowMillis))) }
                children.values.forEach { child -> candidates.addAll(child.top) }
                val top = candidates
                    .distinctBy { it.lexeme.key }
                    .sortedWith(
                        compareByDescending<RankedLexeme> { it.score }
                            .thenByDescending { it.lexeme.lastUsedMillis }
                            .thenBy { it.lexeme.key },
                    )
                    .take(CACHED_TOP)
                return Node(children, top, mutable.terminal)
            }

            return PersonalPrefixIndex(freeze(mutableRoot))
        }

        private const val SCORE_SCALE = 1_000.0
    }
}

data class PersonalizationSnapshot(
    val generation: Long,
    val prefixIndex: PersonalPrefixIndex,
    val lexemeKeys: Set<String>,
    val continuations: Map<String, List<WordCandidate>>,
    val matureCorrections: Map<String, CorrectionEvidence>,
) {
    fun nextWords(history: List<String>, limit: Int): List<String> {
        if (limit <= 0) return emptyList()
        val normalized = history.mapNotNull(PersonalizationPolicy::normalizeContextToken)
        val contexts = buildList {
            if (normalized.size >= 2) add(normalized.takeLast(2).joinToString(NGramRecord.CONTEXT_SEPARATOR))
            if (normalized.isNotEmpty()) add(normalized.last())
        }
        for (context in contexts) {
            val ranked = continuations[context]
            if (!ranked.isNullOrEmpty()) return ranked.take(limit).map(WordCandidate::word)
        }
        return emptyList()
    }

    companion object {
        val EMPTY = PersonalizationSnapshot(0, PersonalPrefixIndex.EMPTY, emptySet(), emptyMap(), emptyMap())
    }
}
