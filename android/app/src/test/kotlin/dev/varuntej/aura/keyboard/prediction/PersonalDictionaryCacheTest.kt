package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the in-memory learned-words model behind the personal dictionary. */
class PersonalDictionaryCacheTest {

    // A fixed "now" and a day in millis, so the time-decayed completions are deterministic.
    private val now = 1_000_000_000_000L
    private val day = 86_400_000L

    @Test
    fun learn_addsThenIncrementsCount() {
        val c = PersonalDictionaryCache()
        assertEquals(1, c.learn("thiru", 1).count)
        assertEquals(2, c.learn("thiru", 2).count)
        assertEquals(1, c.size)
        assertTrue(c.contains("thiru"))
    }

    @Test
    fun contains_isCaseInsensitive_butDisplayTracksLatestCasing() {
        val c = PersonalDictionaryCache()
        c.learn("thiru", 1)
        assertTrue(c.contains("THIRU"))
        // Latest casing the user typed becomes the offered display form.
        c.learn("Thiru", 2)
        assertEquals("Thiru", c.completions("thi", 3, nowMs = 2).first().word)
    }

    @Test
    fun completions_rankByCountThenRecency_andRespectLimit() {
        val c = PersonalDictionaryCache()
        c.learn("there", 1)
        c.learn("there", 2)          // count 2
        c.learn("their", 3)          // count 1, but most recent
        c.learn("them", 4)           // count 1, even more recent
        // Read at the latest write time, so decay is negligible and count is what orders here.
        val out = c.completions("the", limit = 2, nowMs = 4).map { it.word }
        // "there"(count 2) first; then the most-recent count-1 word, "them".
        assertEquals(listOf("there", "them"), out)
    }

    @Test
    fun completions_onlyMatchThePrefix() {
        val c = PersonalDictionaryCache()
        c.learn("cat", 1)
        c.learn("car", 1)
        c.learn("dog", 1)
        assertEquals(setOf("cat", "car"), c.completions("ca", 5, nowMs = 1).map { it.word }.toSet())
    }

    @Test
    fun add_marksKnownWithoutCountingAUse() {
        val c = PersonalDictionaryCache()
        c.learn("kev", 1)
        c.learn("kev", 2)            // count 2
        assertEquals(2, c.add("kev", 3).count) // add does not bump the count
        assertTrue(c.contains("kev"))
        assertEquals(1, c.add("nora", 4).count) // a brand-new added word starts at 1
    }

    @Test
    fun remove_forgetsTheWord() {
        val c = PersonalDictionaryCache()
        c.learn("buddy", 1)
        assertTrue(c.remove("BUDDY"))
        assertFalse(c.contains("buddy"))
        assertFalse(c.remove("buddy")) // already gone
    }

    @Test
    fun snapshotAndLoad_roundTrip() {
        val c = PersonalDictionaryCache()
        c.learn("alpha", 10)
        c.learn("alpha", 11)
        c.learn("beta", 12)
        val snap = c.snapshot()

        val restored = PersonalDictionaryCache()
        restored.load(snap)
        assertEquals(2, restored.size)
        // The raw count survived the snapshot/load round-trip (frequency is now a decayed score,
        // so we check the persisted count directly).
        assertEquals(2, restored.snapshot().first { it.word.equals("alpha", true) }.count)
        assertTrue(restored.contains("beta"))
    }

    @Test
    fun emptyPrefix_returnsNothing() {
        val c = PersonalDictionaryCache()
        c.learn("hello", 1)
        assertTrue(c.completions("", 3, now).isEmpty())
    }

    @Test
    fun completions_decayOldWords_soRecentUseOutranksAStaleHigherCount() {
        val c = PersonalDictionaryCache()
        // "alpha": used 4 times but 200 days ago. "alphb": used once, today.
        repeat(4) { c.learn("alpha", now - 200 * day) }
        c.learn("alphb", now)
        val out = c.completions("alph", 2, now).map { it.word }
        assertEquals(listOf("alphb", "alpha"), out)
    }

    @Test
    fun completions_decayedScore_dropsToAboutOneOverEAfterTheTimeConstant() {
        val c = PersonalDictionaryCache()
        c.learn("fresh", now)              // count 1, today
        c.learn("stale", now - 90 * day)   // count 1, one time constant (90d) ago
        val fresh = c.completions("fresh", 1, now).first().frequency
        val stale = c.completions("stale", 1, now).first().frequency
        // e^-1 ~= 0.368: the 90-day-old word ranks at roughly 37% of the fresh one.
        val ratio = stale.toDouble() / fresh.toDouble()
        assertTrue("ratio=$ratio", ratio in 0.33..0.40)
    }
}
