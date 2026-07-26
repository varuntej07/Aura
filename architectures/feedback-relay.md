# Feedback relay architecture

Feedback is captured as a silent tool side effect during text chat or voice. Firestore is the durable record; Telegram is only a best-effort founder alert.

## Component and data flow

```text
+-------------------+       model tool call       +---------------------+
| text or voice turn| --------------------------> | report_feedback     |
+-------------------+                             | shared ToolExecutor |
                                                  +----------+----------+
                                                             |
                                                             v
                                                  +---------------------+
                                                  | capture_feedback    |
                                                  | validate + enrich   |
                                                  +----------+----------+
                                                             |
                                     +-----------------------+-------------------+
                                     v                                           v
                          +----------------------+                    +-------------------+
                          | observed_feedback    |                    | detached Telegram |
                          | Firestore collection |                    | founder alert     |
                          +----------------------+                    +-------------------+
```

Profile data only enriches the report with available name, timezone, local time, and region. It is not required for capture.

## Failure, retry, and recovery

```text
Profile read fails -----------> use empty enrichment; continue capture
Firestore write fails --------> warn and swallow; never break chat or voice
Telegram is unconfigured -----> no-op; Firestore remains authoritative
Telegram is slow or down -----> timeout/warn in detached task
                              -> user turn is already unblocked
Duplicate model tool call ----> separate reports may be stored
                              -> no automatic retry or dedup contract today
```

## Obvious walkthrough: user reports a bug

1. During a normal response, the model recognizes product feedback and calls `report_feedback`.
2. The shared executor validates the structured category, area, summary, quote, and severity.
3. Capture enriches the report from the user profile when possible.
4. Firestore stores the durable report, and a detached Telegram task sends a convenience alert.

## Non-obvious walkthrough: Telegram outage

1. Firestore stores the report successfully.
2. Telegram times out or rejects the alert.
3. The alert task logs a warning and returns without raising.
4. Chat or voice continues normally, and the report is still available in `observed_feedback`.

## Code anchors

- `backend/src/services/feedback/feedback_capture.py`
- `backend/src/services/feedback/feedback_schema.py`
- `backend/src/services/feedback/telegram_client.py`
- `backend/src/services/tool_executor.py`
- `backend/src/handlers/mcp.py`
