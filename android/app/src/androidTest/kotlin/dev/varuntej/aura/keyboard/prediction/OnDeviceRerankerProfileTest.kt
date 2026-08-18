package dev.varuntej.aura.keyboard.prediction

import android.content.Context
import android.os.Debug
import android.os.SystemClock
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import java.io.FileNotFoundException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.FixMethodOrder
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.MethodSorters

@RunWith(AndroidJUnit4::class)
@FixMethodOrder(MethodSorters.NAME_ASCENDING)
class OnDeviceRerankerProfileTest {
    private val context: Context = ApplicationProvider.getApplicationContext()
    private val candidates = listOf(
        Suggestion("their", SuggestionSource.BASE),
        Suggestion("there", SuggestionSource.BASE),
        Suggestion("they", SuggestionSource.BASE),
    )

    @Test
    fun a_cpuProfile_recordsActualNodeProviderAndMeasuredRuntime() {
        profile(KeyboardExecutionProvider.CPU)
    }

    @Test
    fun b_xnnpackProfile_recordsSupportOrFailOpenEvidence() {
        profile(KeyboardExecutionProvider.XNNPACK)
    }

    @Test
    fun c_missingModel_failsOpenWithoutInference() {
        assertFailOpen(
            OnDeviceReranker(
                context,
                modelLoaderOverride = { throw FileNotFoundException("missing test model") },
            ),
        )
    }

    @Test
    fun d_corruptModel_failsOpenWithoutInference() {
        val corrupt = ByteBuffer.allocateDirect(32).order(ByteOrder.nativeOrder()).apply {
            put("not-an-onnx-model".toByteArray())
            flip()
        }
        assertFailOpen(
            OnDeviceReranker(context, modelLoaderOverride = { corrupt to corrupt.remaining().toLong() }),
        )
    }

    @Test
    fun e_unsupportedExecutionProviderRegistration_failsOpen() {
        assertFailOpen(
            OnDeviceReranker(
                context,
                providerConfiguratorOverride = {
                    throw UnsupportedOperationException("unsupported provider test")
                },
            ),
        )
    }

    private fun assertFailOpen(reranker: OnDeviceReranker) {
        try {
            assertFalse(reranker.warm(PredictionCancellation.NEVER))
            assertNull(
                reranker.score(
                    PredictionRequest.CurrentWord("thier", autocorrectAllowed = true),
                    candidates,
                    PredictionCancellation.NEVER,
                ),
            )
            assertEquals(NeuralRuntimeState.FAILED, reranker.diagnostics().state)
        } finally {
            reranker.close()
        }
    }

    private fun profile(provider: KeyboardExecutionProvider) {
        val dictionaryPssBeforeKb = Debug.getPss()
        BaseDictionary.ensureLoaded(context)
        val dictionaryDeadline = SystemClock.uptimeMillis() + 5_000
        while (!BaseDictionary.isLoaded && SystemClock.uptimeMillis() < dictionaryDeadline) {
            SystemClock.sleep(20)
        }
        val dictionaryPssAfterKb = Debug.getPss()
        val dictionaryInfo = BaseDictionary.runtimeInfo
        val dictionaryReport = File(context.filesDir, "packed_dictionary_diagnostics.json")
        if (!dictionaryReport.exists() && dictionaryInfo != null) {
            dictionaryReport.writeText(
                """{
                  "asset_bytes": ${dictionaryInfo.packagedBytes},
                  "node_count": ${dictionaryInfo.nodeCount},
                  "edge_count": ${dictionaryInfo.edgeCount},
                  "word_count": ${dictionaryInfo.wordCount},
                  "mapped_load_ms": ${dictionaryInfo.mappedLoadMillis},
                  "warm_pss_delta_kb": ${dictionaryPssAfterKb - dictionaryPssBeforeKb}
                }
                """.trimIndent() + "\n",
            )
        }
        val outputPrefix = File(
            context.filesDir,
            "ort_${provider.name.lowercase()}_profile_",
        ).absolutePath
        val pssBeforeKb = Debug.getPss()
        val reranker = OnDeviceReranker(context, provider, outputPrefix)
        val warmed = reranker.warm(PredictionCancellation.NEVER)
        if (provider == KeyboardExecutionProvider.CPU) assertTrue(warmed)
        repeat(1_000) {
            reranker.score(
                PredictionRequest.CurrentWord("thier", autocorrectAllowed = true),
                candidates,
                PredictionCancellation.NEVER,
            )
        }
        val diagnostics = reranker.diagnostics()
        val pssAfterKb = Debug.getPss()
        reranker.close()
        val profilePath = reranker.profilingResultPath
        if (diagnostics.state == NeuralRuntimeState.READY) {
            assertEquals(1_000L, diagnostics.inferenceCount)
            assertTrue(profilePath != null && File(profilePath).isFile)
        }
        File(context.filesDir, "ort_${provider.name.lowercase()}_diagnostics.json").writeText(
            """{
              "state": "${diagnostics.state}",
              "provider_requested": "$provider",
              "runtime_version": "${diagnostics.runtimeVersion}",
              "model_bytes": ${diagnostics.modelBytes},
              "parameter_count": ${diagnostics.parameterCount},
              "initialization_ms": ${diagnostics.initializationMillis},
              "warmup_ms": ${diagnostics.warmupMillis},
              "inference_count": ${diagnostics.inferenceCount},
              "inference_sample_count": ${diagnostics.inferenceSampleCount},
              "inference_p50_ms": ${diagnostics.inferenceP50Millis},
              "inference_p95_ms": ${diagnostics.inferenceP95Millis},
              "inference_p99_ms": ${diagnostics.inferenceP99Millis},
              "pss_delta_kb": ${pssAfterKb - pssBeforeKb},
              "profile_path": "$profilePath",
              "failure_kind": "${diagnostics.failureKind}"
            }
            """.trimIndent() + "\n",
        )
    }
}
