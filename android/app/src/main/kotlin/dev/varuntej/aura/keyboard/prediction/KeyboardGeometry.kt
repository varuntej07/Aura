package dev.varuntej.aura.keyboard.prediction

/** Small deterministic QWERTY proximity signal used only to rank already-valid corrections. */
object KeyboardGeometry {
    private data class Position(val x2: Int, val y: Int)

    private val positions: Map<Char, Position> = buildMap {
        addRow("qwertyuiop", offsetX2 = 0, y = 0)
        addRow("asdfghjkl", offsetX2 = 1, y = 1)
        addRow("zxcvbnm", offsetX2 = 2, y = 2)
    }

    /** Higher means the edits are more plausible neighboring-key mistakes. */
    fun proximityScore(source: String, candidate: String): Int {
        var score = 0
        val length = minOf(source.length, candidate.length)
        for (index in 0 until length) {
            val from = source[index].lowercaseChar()
            val to = candidate[index].lowercaseChar()
            if (from == to) continue
            val a = positions[from] ?: continue
            val b = positions[to] ?: continue
            val dx = a.x2 - b.x2
            val dy = a.y - b.y
            val distance = dx * dx + 4 * dy * dy
            if (distance <= 8) score += 2
        }
        for (index in 0 until length - 1) {
            if (source[index].equals(candidate[index + 1], ignoreCase = true) &&
                source[index + 1].equals(candidate[index], ignoreCase = true)
            ) {
                score += 3
            }
        }
        return score
    }

    private fun MutableMap<Char, Position>.addRow(row: String, offsetX2: Int, y: Int) {
        row.forEachIndexed { index, character -> put(character, Position(offsetX2 + index * 2, y)) }
    }
}
