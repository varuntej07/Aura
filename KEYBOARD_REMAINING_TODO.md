# Aura Keyboard — Remaining TODOs

## TODO 1 — Prove fast typing and model behavior safely

- [ ] Keep benchmark data isolated so a benchmark can never read, modify, or erase the user’s real learned keyboard data.
- [ ] Have the user run the benchmark directly on their phone; the agent must not run tests on remote ADB devices.
- [ ] Measure key release → committed character, committed character → app display, and suggestion latency at p50/p95/p99.
- [ ] Cover sustained fast typing, backspace bursts, capitalization, newline, field switching, and app switching.
- [ ] Report missed, duplicated, reordered, and delayed characters.
- [ ] Record ONNX initialization state, execution provider, inference count, initialization error category, and lexical fallback usage without recording typed text.
- [ ] Done when one user-run report proves the keyboard remains correct and responsive under the required scenarios.

## TODO 2 — Add first-use and just-in-time privacy disclosures

- [ ] On first keyboard setup, explain that ordinary typing and personalization stay on-device and let the user explicitly choose whether to enable local personalization.
- [ ] Before the first AI writing transmission, explain exactly what editor context will be sent, why it is needed, and provide a real decline path.
- [ ] Before the first voice transmission, explain what audio and any permitted editor context will be sent, why it is needed, and provide a real decline path.
- [ ] Keep the basic keyboard fully functional when either disclosure is declined.
- [ ] Never offer AI context transmission in password, PIN, secure, incognito, or no-personalized-learning fields.
- [ ] Done when consent is specific to each network feature and ordinary typing never depends on accepting it.

## TODO 3 — Update the Privacy Policy, Data Safety disclosure, and Terms

- [ ] Add an “Aura Keyboard” Privacy Policy section covering local keystrokes, encrypted personalization, retention, deletion, excluded secure fields, local ONNX inference, explicit AI/voice actions, transmitted context, providers, retention, and content-free telemetry.
- [ ] Use the accurate product wording: “Ordinary typing and personalized suggestions are processed locally. Text is sent only when you explicitly use an Aura AI or voice action.”
- [ ] Update the Google Play Data Safety answers to match the implemented behavior.
- [ ] Keep Terms limited to output accuracy, user review before sending, explicit AI processing under the Privacy Policy, prohibited use, availability, and liability.
- [ ] Do not add federated-learning consent before federated learning exists.
- [ ] Done when policy text, Play disclosures, Terms, and in-product wording describe the same behavior without contradictory claims.

## TODO 4 — Decide whether ONNX stays in the keyboard

**Status (2026-08-27): DEFERRED for the first beta, deliberately, with the evidence named.**

Keeping ONNX Runtime for the beta is a decision to measure, not a decision to skip. Two inputs
are now required before the keep/remove call is made, and both only exist once a beta bundle is
live:

1. **Real download delta.** Play Console → App bundle explorer → Download size, per ABI. The
   `.aab` on disk is not the user download: Play strips the R8 mapping and native symbols and
   delivers one ABI per device. `libonnxruntime.so` is ~28 MB uncompressed for `arm64-v8a` and
   ~20 MB for `armeabi-v7a`, so the delivered number is the only one that settles this.
2. **Whether it changes anything a user sees.** `NeuralRerankMetrics` now counts reranks
   attempted, reranks that changed the top suggestion, and reranks that fell back to lexical.
   They surface in keyboard settings → Advanced → Developer diagnostics. A 121-parameter model
   whose top-1 change rate is near zero is 28 MB buying nothing.

If the change rate does not justify the delivered size, the fallback is unchanged: reproduce the
121 parameters in Kotlin, verify numerical parity against the ONNX outputs, and delete the
runtime dependency and `keyboard_reranker_int8.onnx`.

Until then the model's only quality evidence remains `validation_top1 0.861` against its own
1,024-example synthetic holdout, which says nothing about a real person's suggestion strip.


- [ ] Confirm the model reaches READY on the user’s phone and that inference calls actually occur.
- [ ] Compare lexical-only ranking against ONNX ranking on the same evaluation set.
- [ ] Require a meaningful top-1, MRR, or correction-acceptance improvement with no noticeable typing-latency regression.
- [ ] Measure release AAB download size and runtime cost, not only the debug APK.
- [ ] Use CPU first; evaluate XNNPACK or NNAPI only with measured latency, size, and power evidence.
- [ ] If the tiny fixed model does not justify ONNX Runtime, reproduce its matrix operations in Kotlin, verify numerical parity, and remove the runtime dependency.
- [ ] Keep ONNX only if quality gains justify its cost or multiple future on-device models will reuse it.
- [ ] Done when the repository records a measured keep/remove decision and its evidence.
