package dev.varuntej.aura.keyboard.prediction

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OnnxValue
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtException
import ai.onnxruntime.OrtSession
import android.content.Context
import android.os.Trace
import java.io.Closeable
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.nio.channels.FileChannel
import java.util.Collections
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.ln

internal enum class KeyboardExecutionProvider { CPU, XNNPACK }

internal enum class NeuralRuntimeState { UNINITIALIZED, READY, FAILED, CLOSED }

internal data class NeuralRuntimeDiagnostics(
    val state: NeuralRuntimeState,
    val providerRequested: KeyboardExecutionProvider,
    val runtimeVersion: String?,
    val modelBytes: Long,
    val parameterCount: Int,
    val initializationMillis: Double?,
    val warmupMillis: Double?,
    val inferenceCount: Long,
    val inferenceSampleCount: Int,
    val inferenceP50Millis: Double?,
    val inferenceP95Millis: Double?,
    val inferenceP99Millis: Double?,
    val failureKind: String?,
)

/** Scores a bounded lexical payload. Implementations return the same reusable score array. */
internal fun interface NeuralCandidateScorer {
    fun score(
        request: PredictionRequest,
        candidates: List<Suggestion>,
        cancellation: PredictionCancellation,
    ): FloatArray?

    fun warm(cancellation: PredictionCancellation): Boolean = false
}

/**
 * Optional ONNX tier. It is initialized lazily on PredictionCoordinator's worker, never the IME
 * thread. The input and pinned output tensors wrap fixed direct buffers for their entire lifetime.
 */
internal class OnDeviceReranker(
    context: Context,
    private val provider: KeyboardExecutionProvider = KeyboardExecutionProvider.CPU,
    private val profilingOutputPrefix: String? = null,
    private val modelLoaderOverride: (() -> Pair<ByteBuffer, Long>)? = null,
    private val providerConfiguratorOverride: ((OrtSession.SessionOptions) -> Unit)? = null,
) : NeuralCandidateScorer, Closeable {
    private val appContext = context.applicationContext
    private val closed = AtomicBoolean(false)
    private val inputBuffer: FloatBuffer = directFloats(MAX_CANDIDATES * FEATURE_COUNT)
    private val outputBuffer: FloatBuffer = directFloats(MAX_CANDIDATES)
    private val scores = FloatArray(MAX_CANDIDATES)
    private val distanceRows = IntArray((MAX_TOKEN_LENGTH + 1) * 2)
    private val inferenceNanos = LongArray(MAX_RECORDED_INFERENCES)
    private val terminateActiveRun = fun() {
        try {
            runOptions?.setTerminate(true)
        } catch (_: Throwable) {
            // The generation guard still rejects a late result if native termination fails.
        }
    }

    @Volatile
    private var state = NeuralRuntimeState.UNINITIALIZED
    @Volatile
    private var failureKind: String? = null
    private var environment: OrtEnvironment? = null
    private var options: OrtSession.SessionOptions? = null
    private var session: OrtSession? = null
    private var runOptions: OrtSession.RunOptions? = null
    private var inputTensor: OnnxTensor? = null
    private var outputTensor: OnnxTensor? = null
    private var inputs: Map<String, OnnxTensor>? = null
    private var pinnedOutputs: Map<String, OnnxValue>? = null
    private var initializationNanos = 0L
    private var warmupNanos = 0L
    private var modelBytes = 0L
    private var parameterCount = 0
    private var runtimeVersion: String? = null
    @Volatile
    private var inferenceCount = 0L

    @Volatile
    internal var profilingResultPath: String? = null
        private set

    override fun warm(cancellation: PredictionCancellation): Boolean = when (state) {
        NeuralRuntimeState.READY -> true
        NeuralRuntimeState.UNINITIALIZED -> initialize(cancellation)
        else -> false
    }

    override fun score(
        request: PredictionRequest,
        candidates: List<Suggestion>,
        cancellation: PredictionCancellation,
    ): FloatArray? {
        if (closed.get() || candidates.size !in 2..MAX_CANDIDATES || cancellation.isCancelled()) {
            return null
        }
        if (state == NeuralRuntimeState.UNINITIALIZED && !initialize(cancellation)) return null
        if (state != NeuralRuntimeState.READY || cancellation.isCancelled()) return null
        encode(request, candidates)
        val activeSession = session ?: return null
        val activeRunOptions = runOptions ?: return null
        val activeInputs = inputs ?: return null
        val activeOutputs = pinnedOutputs ?: return null
        return try {
            activeRunOptions.setTerminate(false)
            cancellation.installCancellationCallback(terminateActiveRun)
            if (cancellation.isCancelled()) {
                cancellation.removeCancellationCallback(terminateActiveRun)
                return null
            }
            val startedAt = System.nanoTime()
            try {
                Trace.beginSection(TRACE_INFERENCE)
                activeSession.run(
                    activeInputs,
                    Collections.emptySet(),
                    activeOutputs,
                    activeRunOptions,
                ).close()
            } finally {
                Trace.endSection()
                cancellation.removeCancellationCallback(terminateActiveRun)
            }
            recordInference(System.nanoTime() - startedAt)
            if (cancellation.isCancelled()) return null
            repeat(MAX_CANDIDATES) { index -> scores[index] = outputBuffer.get(index) }
            scores
        } catch (error: OrtException) {
            if (!cancellation.isCancelled()) fail(error)
            null
        } catch (error: Throwable) {
            fail(error)
            null
        } finally {
            try {
                activeRunOptions.setTerminate(false)
            } catch (_: Throwable) {
                // A permanently unusable run-options object is handled by the next failed run.
            }
        }
    }

    private fun initialize(cancellation: PredictionCancellation): Boolean {
        if (state != NeuralRuntimeState.UNINITIALIZED || cancellation.isCancelled()) return false
        val startedAt = System.nanoTime()
        return try {
            Trace.beginSection(TRACE_INITIALIZE)
            val env = OrtEnvironment.getEnvironment()
            env.setTelemetry(false)
            environment = env
            val sessionOptions = OrtSession.SessionOptions()
            options = sessionOptions
            sessionOptions.apply {
                setExecutionMode(OrtSession.SessionOptions.ExecutionMode.SEQUENTIAL)
                setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
                setIntraOpNumThreads(1)
                setInterOpNumThreads(1)
                setMemoryPatternOptimization(true)
                if (providerConfiguratorOverride != null) {
                    providerConfiguratorOverride.invoke(this)
                } else if (provider == KeyboardExecutionProvider.XNNPACK) {
                    addXnnpack(mapOf("intra_op_num_threads" to "1"))
                }
                profilingOutputPrefix?.let(::enableProfiling)
            }
            val (model, bytes) = modelLoaderOverride?.invoke() ?: mapModel()
            if (cancellation.isCancelled()) {
                releaseRuntime()
                return false
            }
            val createdSession = env.createSession(model, sessionOptions)
            session = createdSession
            validateModel(createdSession)
            val createdRunOptions = OrtSession.RunOptions()
            runOptions = createdRunOptions
            val createdInput = OnnxTensor.createTensor(
                env, inputBuffer, longArrayOf(1, MAX_CANDIDATES.toLong(), FEATURE_COUNT.toLong()),
            )
            inputTensor = createdInput
            val createdOutput = OnnxTensor.createTensor(
                env, outputBuffer, longArrayOf(1, MAX_CANDIDATES.toLong()),
            )
            outputTensor = createdOutput
            inputs = Collections.singletonMap(INPUT_NAME, createdInput)
            pinnedOutputs = Collections.singletonMap<String, OnnxValue>(OUTPUT_NAME, createdOutput)
            modelBytes = bytes
            runtimeVersion = env.version
            initializationNanos = System.nanoTime() - startedAt
            inputBuffer.clear()
            repeat(inputBuffer.capacity()) { inputBuffer.put(it, 0f) }
            val warmStartedAt = System.nanoTime()
            createdSession.run(
                inputs!!, Collections.emptySet(), pinnedOutputs!!, createdRunOptions,
            ).close()
            warmupNanos = System.nanoTime() - warmStartedAt
            if (cancellation.isCancelled()) {
                releaseRuntime()
                return false
            }
            state = NeuralRuntimeState.READY
            true
        } catch (error: Throwable) {
            fail(error)
            false
        } finally {
            Trace.endSection()
        }
    }

    private fun validateModel(createdSession: OrtSession) {
        require(createdSession.inputNames == setOf(INPUT_NAME))
        require(createdSession.outputNames == setOf(OUTPUT_NAME))
        val metadata = createdSession.metadata.customMetadata
        require(metadata["aura.feature_schema"] == FEATURE_SCHEMA)
        require(metadata["aura.quantization"] == "dynamic int8 weights")
        require(metadata["aura.max_candidates"] == MAX_CANDIDATES.toString())
        require(metadata["aura.corpus_license"] == "MIT")
        require(metadata["aura.corpus_source"] == EXPECTED_CORPUS_SOURCE)
        require(metadata["aura.corpus_license_sha256"] == EXPECTED_LICENSE_SHA256)
        parameterCount = metadata.getValue("aura.parameter_count").toInt().also {
            require(it == EXPECTED_PARAMETER_COUNT)
        }
    }

    private fun mapModel(): Pair<ByteBuffer, Long> {
        appContext.assets.openFd(MODEL_ASSET).use { descriptor ->
            FileInputStream(descriptor.fileDescriptor).channel.use { channel ->
                return channel.map(
                    FileChannel.MapMode.READ_ONLY,
                    descriptor.startOffset,
                    descriptor.length,
                ) to descriptor.length
            }
        }
    }

    private fun encode(request: PredictionRequest, candidates: List<Suggestion>) {
        repeat(inputBuffer.capacity()) { inputBuffer.put(it, 0f) }
        val raw = when (request) {
            PredictionRequest.Warmup -> ""
            is PredictionRequest.CurrentWord -> request.rawWord.lowercase()
            is PredictionRequest.NextWord -> ""
        }
        val nextWord = request is PredictionRequest.NextWord
        candidates.forEachIndexed { rank, suggestion ->
            val candidate = suggestion.word.lowercase()
            val offset = rank * FEATURE_COUNT
            inputBuffer.put(offset + 0, 1f - rank.toFloat() / (MAX_CANDIDATES - 1))
            inputBuffer.put(
                offset + 1,
                (ln(BaseDictionary.frequencyOf(candidate).toDouble() + 1.0) /
                    MAX_LOG_FREQUENCY).toFloat().coerceIn(0f, 1f),
            )
            inputBuffer.put(offset + 2, commonPrefixRatio(raw, candidate))
            inputBuffer.put(offset + 3, editSimilarity(raw, candidate))
            inputBuffer.put(
                offset + 4,
                (KeyboardGeometry.proximityScore(raw, candidate).coerceAtMost(8) / 8f),
            )
            inputBuffer.put(
                offset + 5,
                if (raw.isEmpty()) 0f else
                    1f - abs(raw.length - candidate.length).coerceAtMost(8) / 8f,
            )
            inputBuffer.put(offset + 6, if (suggestion.source == SuggestionSource.PERSONAL) 1f else 0f)
            inputBuffer.put(offset + 7, if (nextWord) 1f else 0f)
        }
    }

    private fun commonPrefixRatio(left: String, right: String): Float {
        if (left.isEmpty() || right.isEmpty()) return 0f
        val limit = minOf(left.length, right.length)
        var count = 0
        while (count < limit && left[count] == right[count]) count++
        return count.toFloat() / limit
    }

    private fun editSimilarity(left: String, right: String): Float {
        if (left.isEmpty() || left.length > MAX_TOKEN_LENGTH || right.length > MAX_TOKEN_LENGTH) return 0f
        val stride = MAX_TOKEN_LENGTH + 1
        for (column in 0..right.length) distanceRows[column] = column
        for (row in 1..left.length) {
            val currentOffset = (row and 1) * stride
            val previousOffset = ((row - 1) and 1) * stride
            distanceRows[currentOffset] = row
            for (column in 1..right.length) {
                distanceRows[currentOffset + column] = minOf(
                    distanceRows[currentOffset + column - 1] + 1,
                    distanceRows[previousOffset + column] + 1,
                    distanceRows[previousOffset + column - 1] +
                        if (left[row - 1] == right[column - 1]) 0 else 1,
                )
            }
        }
        val distance = distanceRows[(left.length and 1) * stride + right.length].coerceAtMost(3)
        return 1f - distance / 3f
    }

    private fun recordInference(durationNanos: Long) {
        inferenceNanos[(inferenceCount % inferenceNanos.size).toInt()] = durationNanos
        inferenceCount++
    }

    fun diagnostics(): NeuralRuntimeDiagnostics {
        val count = minOf(inferenceCount, inferenceNanos.size.toLong()).toInt()
        val samples = LongArray(count)
        if (count > 0) {
            val start = (inferenceCount - count).coerceAtLeast(0)
            repeat(count) { index -> samples[index] = inferenceNanos[((start + index) % inferenceNanos.size).toInt()] }
            samples.sort()
        }
        return NeuralRuntimeDiagnostics(
            state,
            provider,
            runtimeVersion,
            modelBytes,
            parameterCount,
            initializationNanos.takeIf { it > 0 }?.div(1_000_000.0),
            warmupNanos.takeIf { it > 0 }?.div(1_000_000.0),
            inferenceCount,
            count,
            percentile(samples, 0.50),
            percentile(samples, 0.95),
            percentile(samples, 0.99),
            failureKind,
        )
    }

    private fun percentile(sorted: LongArray, quantile: Double): Double? {
        if (sorted.isEmpty()) return null
        val index = (ceil(sorted.size * quantile).toInt() - 1).coerceIn(sorted.indices)
        return sorted[index] / 1_000_000.0
    }

    private fun fail(error: Throwable) {
        failureKind = error.javaClass.simpleName
        state = NeuralRuntimeState.FAILED
        releaseRuntime()
    }

    private fun releaseRuntime() {
        try { outputTensor?.close() } catch (_: Throwable) {}
        try { inputTensor?.close() } catch (_: Throwable) {}
        try { runOptions?.close() } catch (_: Throwable) {}
        if (profilingOutputPrefix != null && profilingResultPath == null) {
            try { profilingResultPath = session?.endProfiling() } catch (_: Throwable) {}
        }
        try { session?.close() } catch (_: Throwable) {}
        try { options?.close() } catch (_: Throwable) {}
        pinnedOutputs = null
        inputs = null
        outputTensor = null
        inputTensor = null
        runOptions = null
        session = null
        options = null
        environment = null
    }

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        state = NeuralRuntimeState.CLOSED
        releaseRuntime()
    }

    private companion object {
        const val MODEL_ASSET = "models/keyboard_reranker_int8.onnx"
        const val INPUT_NAME = "features"
        const val OUTPUT_NAME = "scores"
        const val MAX_CANDIDATES = 8
        const val FEATURE_COUNT = 8
        const val MAX_TOKEN_LENGTH = 48
        const val MAX_RECORDED_INFERENCES = 512
        const val EXPECTED_PARAMETER_COUNT = 121
        const val EXPECTED_CORPUS_SOURCE = "https://github.com/hermitdave/FrequencyWords"
        const val EXPECTED_LICENSE_SHA256 =
            "a6a2371beb35dbb19bce28e50f2fd9463a6c055e3b1d2f73c4705f9eaffa5c7b"
        const val MAX_LOG_FREQUENCY = 17.175455019
        const val FEATURE_SCHEMA =
            "lexical_rank,log_frequency,common_prefix,edit_similarity," +
                "keyboard_proximity,length_similarity,personal_source,next_word"
        const val TRACE_INITIALIZE = "AuraIme:ORT initialize+warm"
        const val TRACE_INFERENCE = "AuraIme:ORT inference"

        fun directFloats(count: Int): FloatBuffer =
            ByteBuffer.allocateDirect(count * Float.SIZE_BYTES)
                .order(ByteOrder.nativeOrder())
                .asFloatBuffer()
    }
}

/** Pure conservative merge policy: the model can only reorder existing bounded candidates. */
internal object NeuralRerankPolicy {
    const val MIN_REORDER_MARGIN = 0.18f

    fun apply(candidates: List<Suggestion>, scores: FloatArray?): List<Suggestion> {
        if (scores == null || candidates.size < 2) return candidates
        var bestIndex = 0
        var bestScore = scores[0]
        var runnerUp = Float.NEGATIVE_INFINITY
        for (index in 1 until candidates.size) {
            val score = scores[index]
            if (!score.isFinite()) return candidates
            if (score > bestScore) {
                runnerUp = bestScore
                bestScore = score
                bestIndex = index
            } else if (score > runnerUp) {
                runnerUp = score
            }
        }
        if (bestIndex == 0 || bestScore - maxOf(runnerUp, scores[0]) < MIN_REORDER_MARGIN) {
            return candidates
        }
        return buildList(candidates.size) {
            add(candidates[bestIndex])
            candidates.forEachIndexed { index, candidate -> if (index != bestIndex) add(candidate) }
        }
    }
}
