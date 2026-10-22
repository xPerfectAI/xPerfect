# xPerfect with LiveKit

**Reference integration.** Actual LiveKit 1.5.10 rooms passed synthetic speech input,
transcription, native Queen replies, TTS and received active audio. Partial answer text arrived
before completion. Speech interruption, the correct next answer and same-conversation recall
passed with echo-free input settings. Reconnect, full background callbacks and production voice
parity remain separate acceptance gates.

## Connect

Use an existing xPerfect installation with a connected AI account and its provider API credential.
The example does not require LibreChat or Viventium.

```sh
python -m pip install ./integrations/livekit
```

Set `XPERFECT_BASE_URL` to your provider API URL ending in `/v1`, `XPERFECT_API_KEY` to its
protected credential, and `XPERFECT_MODEL` to an exact supported model ID from `/v1/models`.
Use your existing LiveKit `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET`.
Set `LIVEKIT_STT` and `LIVEKIT_TTS` to supported LiveKit Inference model descriptors with your
chosen speech voice. No speech provider or paid connection is created by this integration.

```sh
python integrations/livekit/example.py dev
```

For an existing LiveKit agent, replace only its LLM:

```python
from xperfect_livekit import create_llm

model = create_llm(
    base_url="http://127.0.0.1:8766/v1",
    api_key=provider_credential,
    model=selected_model_id,
    reasoning_effort="high",
    conversation_id=existing_conversation_id,
)
# AgentSession(llm=model, stt=your_stt, tts=your_tts, ...)
```

Keep one LLM/client per call and a stable conversation ID. The adapter explicitly declares the existing
`main_context_owner=provider_legacy` API context owner; merely omitting Core headers does not enable
native early speech. Core-owned calls retain their accepted-source gates. Each `chat` gets typed voice metadata
and one message/stream/idempotency identity, retained across that stream's SDK retries. An explicit
caller-supplied identity remains the caller's responsibility. Close the LLM on call shutdown; the
example registers this cleanup. A supplied OpenAI client remains owned by its caller. Configure
its transport timeout/retries on that client; other options stay on the official plugin. Newly created OpenAI clients default to zero automatic transport retries. Normal LiveKit plugins, local speech
components, VAD, room options and supported LLM options remain available. The example uses an explicit configurable response budget (`XPERFECT_RESPONSE_TIMEOUT`, default
120 seconds), disables automatic model retries and speculative generation, and establishes that
ordinary replies are spoken in a live call. This avoids the SDK's short generic timeout replaying
accepted agent work. A timeout must be recovered through the existing request receipt, not blindly
resubmitted as a fresh action. The example's STT turn detector requires a provider with endpointing support; a host can instead use its normal
VAD/turn detector. If your STT's final transcript is its authoritative end-of-turn event, optional
`LIVEKIT_TRUST_STT_TURN_END=1` removes LiveKit's extra minimum endpoint delay using its native
setting. Leave it unset for STT providers that merely finalize transcript segments. This does not
change VAD, interruption detection or semantic STT thresholds. LiveKit also ignores user audio
for its default three-second AEC warmup after first speech starts. The example exposes the native
setting as `LIVEKIT_AEC_WARMUP_SECONDS` (default `3`). Set `0` for synthetic injection or a
known echo-free input path; retain suitable echo protection for open speakers. This is separate
from modern `turn_handling` options and does not require a new audio buffer. `create_llm` forwards official plugin options and preserves the requested model
and effort. A provider can reject an unsupported option; the adapter does not silently remap it.

## What it reuses

```text
Your microphone / input
  → normal LiveKit STT and turn detector
  → official LiveKit OpenAI-compatible LLM plugin
  → existing xPerfect conversation API and native Queen/worker tools
  → typed public answer and speech decision
  → this small speech projection
  → normal LiveKit TTS and room audio
```

xPerfect owns native harness execution, its configured tools/MCP, workspace and conversation
continuity. This adapter adds no planner, scheduler, memory store, worker manager or new model loop.
The HTTP API format being OpenAI-compatible does **not** mean the selected worker calls a model
directly without a native harness.

The official LiveKit plugin drops xPerfect's extended speech metadata. The adapter therefore wraps
only the supplied OpenAI client's already-parsed completion stream, before the official plugin
converts it to LiveKit chunks. It keeps the official SDK's HTTP/SSE parser, pooling, request options,
tool-call conversion, usage, streaming and close behavior. There is no private LiveKit parser patch.

## Speak the answer, keep quiet work quiet

Only text with a valid model-authored `audio=eligible` decision goes to TTS. `audio=skip`, missing
or invalid decisions, internal activity, previews and file/status metadata never become speech.
No keyword or status-text matching is used.

Optional `on_text(text)` receives every public text delta, including quiet text, for your existing
text surface. Optional `on_metadata(fields)` receives structured file/activity fields for the host's
existing UI. Keep these callbacks quick and do not log private conversation content by default.
The native conversation retains the complete answer; LiveKit's speech transcript contains the
spoken projection. These are distinct histories, not proof that every host context module migrated.

Per-part eligible text is released as it arrives. Mixed terminal text carries its existing exact
UTF-16 audio ranges **on the same content chunk**, so a consumer never has to speak quiet text
before learning it was quiet. Malformed or mismatched ranges fail closed with an error. No whole
answer buffer is added by this adapter. Provider-owned native streaming now checks the exact
current request, session, worker and active attempt, twice around the transcript read. Core-owned
accepted-family gates remain separate and unchanged. Admissions without an explicit
`main_context_owner=provider_legacy` declaration retain terminal-only behavior, including Core
follow-ups that omit Core headers and redeclare audio eligibility. Loaded room checks proved partial native answer text before completion. This is not a
first-word, glitch-free audio or sub-two-second guarantee.

Host-owned features such as Viventium Feelings, memory, cortices, account grants, files and callback
presentation are not magically supplied by this package. They remain available only through their
configured owning projections. Generic LiveKit-local function tools are not automatically native
xPerfect tools; configure native tools/MCP through xPerfect's existing capability controls. The
adapter's tool-call parser test proves transport preservation, not arbitrary API tool execution.
An SDK interruption or early close now stops only that SDK stream's exact native turn through the
existing authenticated idempotency-cancel endpoint. This also covers cancellation while waiting
for response headers. Normal terminal completion never sends Stop; unrelated worker jobs are not
enumerated or cancelled. Stop is bounded, does not retry, and reports its receipt through
`on_metadata({"native_cancel": receipt})`. Only `capacityReleased=true` proves release; a failed
receipt reports unknown capacity and must not be presented as successful Stop. Merely closing
an unrelated HTTP stream still has no such contract. Native history may retain generated text
that an interrupted listener never heard; this reference does not yet repair that history.
Speech barge-in and the correct next turn passed with zero AEC warmup on synthetic echo-free
input. Cancellation of an actual in-flight native model, reconnect and full background
callback-to-speech parity still need affected-path acceptance before calling this an always-on
voice product.

## Keep native work from delaying other calls

Native stdout, transcript previews and credential redaction share server resources with live
conversation streams. A completed Stop can therefore still delay another call if result processing
blocks the server. Putting CPU-heavy Python work in a thread alone does not prevent this.

Measure native termination, stopped-result processing, public-text forwarding and first received
audio separately. Check an independent event-loop heartbeat while replaying large retained output;
otherwise a pause caused by another worker can look like slow inference or TTS. Keep heartbeat and
stage evidence free of private conversation content.

A confirmed typed Stop uses the existing stopped-generation evidence path before normal completion
previews. Preserve exact native identity, death/lease proof, original private transcript/media and
credential protection. Normal successful and failed results retain their audits and redaction.
Optimize proven redundant work or pathological matching; do not skip privacy, drop context, wait
for background work before speaking, or add a Voice-specific orchestration path. The
[native generation and restart contract](../../docs/02_Architecture_and_Components.md#native-generation-and-restart-ownership)
owns those lifecycle rules. A replay improvement still needs a loaded live-control check; it is not
a conversational-latency or glitch-free-audio guarantee.

## Checks

```sh
PYTHONPATH=integrations/livekit python -m pytest integrations/livekit/tests -q
```

Tests exercise the real pinned LiveKit/OpenAI parsers over split streamed HTTP data, immediate
eligible output before completion, quiet and untyped text, activity, Unicode mixed ranges, malformed
coverage, tool calls, usage and forwarded model options. The native API test
`test_terminal_stream_delivers_mixed_audio_ranges_with_the_content` protects the same-chunk contract.

LiveKit versions are pinned to 1.5.10 and the OpenAI SDK to 2.32.0, matching the inspected installed SDK. Latest online docs may
show newer APIs; upgrade only with these boundary checks and the affected real voice path.

[LiveKit OpenAI-compatible plugin](https://docs.livekit.io/agents/models/llm/openai/) ·
[LiveKit voice quickstart](https://docs.livekit.io/agents/start/voice-ai/) ·
[xPerfect capabilities](../../docs/capability-matrix.md)
