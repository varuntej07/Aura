package dev.varuntej.aura.keyboard.prediction

/**
 * Local merge boundary reserved for a future privacy-reviewed delta design.
 *
 * Deliberately has no serializer, network transport, uploader, or implementation. Adding any of
 * those requires a separate privacy review; the current keyboard stores and applies only its
 * encrypted on-device [PersonalizationState].
 */
internal interface LocalPersonalizationDelta {
    val schemaVersion: Int
    val sourceGeneration: Long

    fun mergeInto(state: PersonalizationState)

    companion object {
        const val CURRENT_SCHEMA_VERSION = 1
    }
}
